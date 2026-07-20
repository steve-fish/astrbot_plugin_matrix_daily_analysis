"""Reliability tests for Matrix history and LLM analysis helpers."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astrbot.core.provider.entities import TokenUsage as AstrBotTokenUsage

from data.plugins.astrbot_plugin_matrix_daily_analysis.src.analysis.analyzers.golden_quote_analyzer import (
    GoldenQuoteAnalyzer,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.analysis.analyzers.user_title_analyzer import (
    UserTitleAnalyzer,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.analysis.statistics import (
    UserAnalyzer,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.analysis.utils.json_utils import (
    fix_json,
    parse_json_response,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.analysis.utils.llm_utils import (
    call_provider_with_retry,
    extract_token_usage,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.commands.dialogue_poll import (
    DialoguePollHandler,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.core.config import (
    ConfigManager,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.core.message_handler import (
    MessageHandler,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.reports.generators import (
    ReportGenerator,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.scheduler.auto_scheduler import (
    AutoScheduler,
)
from data.plugins.astrbot_plugin_matrix_daily_analysis.src.scheduler.retry import (
    RetryManager,
    RetryTask,
)


class FakeHistoryConfig:
    """Minimal configuration used by history pipeline tests."""

    def __init__(self, *, skip_bots: bool) -> None:
        """Store the bot filtering mode.

        Args:
            skip_bots: Whether configured bot users should be excluded.
        """
        self.skip_bots = skip_bots

    def get_max_messages(self) -> int:
        """Return the retained history limit."""
        return 20

    def get_history_filter_prefixes(self) -> list[str]:
        """Return a mixed-case command prefix filter."""
        return ["/SeCrEt"]

    def get_history_filter_users(self) -> list[str]:
        """Return a Matrix user exclusion."""
        return ["@Muted:Example.org"]

    def should_skip_history_bots(self) -> bool:
        """Return the configured bot filtering mode."""
        return self.skip_bots

    def get_threading_enabled(self) -> bool:
        """Disable thread extraction for these focused tests."""
        return False

    def get_bot_matrix_ids(self) -> list[str]:
        """Return the configured bot user IDs."""
        return ["@bot:example.org"]


class FakeBotManager:
    """Bot identity filter used by the message handler."""

    def __init__(self) -> None:
        """Initialize the known bot identity set."""
        self.bot_ids = {"@bot:example.org"}

    def has_bot_matrix_id(self) -> bool:
        """Report that a bot identity is already configured."""
        return bool(self.bot_ids)

    def set_bot_matrix_ids(self, bot_ids: list[str]) -> None:
        """Replace configured bot identities.

        Args:
            bot_ids: Matrix user IDs to retain.
        """
        self.bot_ids = set(bot_ids)

    def should_filter_bot_message(self, sender_id: str) -> bool:
        """Check whether a sender is a configured bot.

        Args:
            sender_id: Matrix sender user ID.

        Returns:
            Whether the sender is a configured bot.
        """
        return sender_id in self.bot_ids


class FakeMatrixClient:
    """Small Matrix history client returning deterministic pages."""

    def __init__(self, events: list, *, repeated_token: bool = False) -> None:
        """Store events and pagination behavior.

        Args:
            events: Matrix events returned on each page.
            repeated_token: Whether every page repeats the same end token.
        """
        self.events = events
        self.repeated_token = repeated_token
        self.calls = 0

    async def get_room_members(self, room_id: str) -> dict:
        """Return one display name mapping.

        Args:
            room_id: Matrix room ID.

        Returns:
            Matrix member-state response.
        """
        return {
            "chunk": [
                None,
                {
                    "type": "m.room.member",
                    "state_key": "@alice:example.org",
                    "content": {"displayname": "Alice"},
                },
            ]
        }

    async def room_messages(self, **kwargs) -> dict:
        """Return one page, or repeat it to exercise token guards.

        Args:
            **kwargs: Matrix pagination arguments.

        Returns:
            Matrix room history response.
        """
        self.calls += 1
        if not self.repeated_token and self.calls > 1:
            return {"chunk": []}
        return {
            "chunk": self.events,
            "end": "repeat" if self.repeated_token else None,
        }


class HistoryFilteringTests(unittest.IsolatedAsyncioTestCase):
    """Verify global filters are applied before every analysis path."""

    @staticmethod
    def make_event(event_id: str, sender: str, body: str, timestamp_ms: int) -> dict:
        """Build a Matrix text event.

        Args:
            event_id: Matrix event ID.
            sender: Matrix sender ID.
            body: Plain message body.
            timestamp_ms: Matrix origin timestamp in milliseconds.

        Returns:
            Matrix room-message event.
        """
        return {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "content": {"msgtype": "m.text", "body": body},
        }

    async def test_global_filters_and_bot_toggle_apply_during_fetch(self) -> None:
        """Prefixes/users are always excluded while bot filtering is configurable."""
        now_ms = int(time.time() * 1000)
        events = [
            None,
            {"type": "m.room.message", "origin_server_ts": "bad"},
            self.make_event("$bot", "@bot:example.org", "bot text", now_ms),
            self.make_event("$muted", "@MUTED:example.org", "muted text", now_ms - 1),
            self.make_event(
                "$prefix", "@alice:example.org", "  /secret command", now_ms - 2
            ),
            self.make_event("$ok", "@alice:example.org", "hello", now_ms - 3),
        ]

        included_handler = MessageHandler(
            FakeHistoryConfig(skip_bots=False), FakeBotManager()
        )
        included = await included_handler.fetch_group_messages(
            FakeMatrixClient(events), "!room:example.org", 1
        )
        self.assertEqual([message["event_id"] for message in included], ["$ok", "$bot"])
        self.assertEqual(included[0]["sender"]["nickname"], "Alice")

        excluded_handler = MessageHandler(
            FakeHistoryConfig(skip_bots=True), FakeBotManager()
        )
        excluded = await excluded_handler.fetch_group_messages(
            FakeMatrixClient(events), "!room:example.org", 1
        )
        self.assertEqual([message["event_id"] for message in excluded], ["$ok"])

    async def test_repeated_pagination_token_stops_without_duplicates(self) -> None:
        """A broken homeserver token must not loop or duplicate retained events."""
        event = self.make_event(
            "$only", "@alice:example.org", "hello", int(time.time() * 1000)
        )
        client = FakeMatrixClient([event], repeated_token=True)
        handler = MessageHandler(FakeHistoryConfig(skip_bots=False), FakeBotManager())

        messages = await handler.fetch_group_messages(client, "!room:example.org", 1)

        self.assertEqual([message["event_id"] for message in messages], ["$only"])
        self.assertEqual(client.calls, 2)

    def test_user_statistics_respect_bot_filter_toggle(self) -> None:
        """Direct analysis should follow skip_bots even without a history fetch."""
        messages = [
            {
                "time": time.time(),
                "sender": {"user_id": "@bot:example.org", "nickname": "Bot"},
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        ]

        included = UserAnalyzer(FakeHistoryConfig(skip_bots=False)).analyze_users(
            messages
        )
        excluded = UserAnalyzer(FakeHistoryConfig(skip_bots=True)).analyze_users(
            messages
        )

        self.assertIn("@bot:example.org", included)
        self.assertNotIn("@bot:example.org", excluded)


class JsonParsingTests(unittest.TestCase):
    """Verify structured LLM output is parsed without data corruption."""

    def test_nested_array_and_escaped_quotes_parse_directly(self) -> None:
        """Topic contributor arrays and quoted content should remain valid."""
        raw = (
            "notes [draft]\nresult:\n```json\n"
            '[{"topic":"A","contributors":["\u7532","\u4e59"],'
            '"detail":"\u4ed6\u8bf4\\"hi\\"\uff0c\u5e76\u4fdd\u7559\uff1a\uff08\uff09"}]'
            "\n```"
        )

        success, data, error = parse_json_response(raw, "\u8bdd\u9898")

        self.assertTrue(success, error)
        self.assertEqual(data[0]["contributors"], ["\u7532", "\u4e59"])
        self.assertEqual(
            data[0]["detail"],
            '\u4ed6\u8bf4"hi"\uff0c\u5e76\u4fdd\u7559\uff1a\uff08\uff09',
        )

    def test_common_smart_quote_and_trailing_comma_errors_are_repaired(self) -> None:
        """Tolerant parsing should repair common non-JSON punctuation."""
        raw = "[{\u201ctopic\u201d: \u201cA\u201d, \u201ccontributors\u201d: [\u201c\u7532\u201d], \u201cdetail\u201d: \u201c\u5185\u5bb9\u201d,},]"

        success, data, error = parse_json_response(raw, "\u8bdd\u9898")

        self.assertTrue(success, error)
        self.assertEqual(
            data, [{"topic": "A", "contributors": ["\u7532"], "detail": "\u5185\u5bb9"}]
        )

    def test_valid_json_fix_preserves_semantic_punctuation(self) -> None:
        """The repair helper must not rewrite valid chat text."""
        raw = '[{"detail":"\u4f60\u597d\uff0c\u539f\u56e0\uff1a\uff08\u6d4b\u8bd5\uff09"}]'

        self.assertEqual(json.loads(fix_json(raw)), json.loads(raw))

    def test_dialogue_poll_parser_handles_nested_options_array(self) -> None:
        """Poll output should use the same balanced-array parsing behavior."""
        handler = DialoguePollHandler(SimpleNamespace(), SimpleNamespace())
        raw = (
            "```json\n"
            '[{"question":"\u9009\u62e9","options":["\u7b54\u6848 A","\u7b54\u6848 B"]}]'
            "\n```"
        )

        self.assertEqual(
            handler.parse_dialogue_poll_json(raw),
            ("\u9009\u62e9", ["\u7b54\u6848 A", "\u7b54\u6848 B"]),
        )


class AnalyzerOutputValidationTests(unittest.TestCase):
    """Verify one malformed LLM item cannot discard earlier valid output."""

    def test_mixed_golden_quote_items_keep_valid_records(self) -> None:
        """Golden quote conversion should skip non-object array elements."""
        config = SimpleNamespace(get_max_golden_quotes=lambda: 5)
        analyzer = GoldenQuoteAnalyzer(SimpleNamespace(), config)

        quotes = analyzer.create_data_objects(
            [
                {"content": "quote", "sender": "Alice", "reason": "reason"},
                None,
            ]
        )

        self.assertEqual([quote.content for quote in quotes], ["quote"])

    def test_mixed_user_title_items_keep_valid_records(self) -> None:
        """User title conversion should skip non-object array elements."""
        config = SimpleNamespace(get_max_user_titles=lambda: 5)
        analyzer = UserTitleAnalyzer(SimpleNamespace(), config)

        titles = analyzer.create_data_objects(
            [
                {
                    "name": "Alice",
                    "matrix": "@alice:example.org",
                    "title": "Helper",
                    "mbti": "INTJ",
                    "reason": "reason",
                },
                None,
            ]
        )

        self.assertEqual([title.name for title in titles], ["Alice"])


class FakeLLMConfig:
    """Configuration for deterministic LLM retry tests."""

    def __init__(self, retries: int) -> None:
        """Store the requested retry count.

        Args:
            retries: Additional attempts after the initial request.
        """
        self.retries = retries

    def get_llm_timeout(self) -> int:
        """Return a short but nonzero timeout."""
        return 1

    def get_llm_retries(self) -> int:
        """Return configured additional retries."""
        return self.retries

    def get_llm_backoff(self) -> int:
        """Disable delay in unit tests."""
        return 0

    def get_llm_provider_id(self) -> str:
        """Return a configured provider ID."""
        return "provider"


class FakeLLMContext:
    """Context that returns configured failures before succeeding."""

    def __init__(self, outcomes: list) -> None:
        """Store call outcomes.

        Args:
            outcomes: Return values or exceptions for successive calls.
        """
        self.outcomes = list(outcomes)
        self.calls = 0

    def get_provider_by_id(self, provider_id: str):
        """Return a provider sentinel for the expected ID.

        Args:
            provider_id: Requested provider ID.

        Returns:
            Provider sentinel when the ID is valid.
        """
        return object() if provider_id == "provider" else None

    async def llm_generate(self, **kwargs):
        """Return or raise the next configured outcome.

        Args:
            **kwargs: LLM generation parameters.

        Returns:
            Configured successful response.

        Raises:
            Exception: Configured failure for the current attempt.
        """
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class LLMUtilityTests(unittest.IsolatedAsyncioTestCase):
    """Verify retry semantics and AstrBot token accounting compatibility."""

    async def test_zero_retries_still_performs_initial_request(self) -> None:
        """A retry count of zero means one total provider request."""
        context = FakeLLMContext(["ok"])

        result = await call_provider_with_retry(
            context,
            FakeLLMConfig(0),
            "prompt",
            max_tokens=20,
            temperature=0.1,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(context.calls, 1)

    async def test_retry_count_is_additional_to_initial_request(self) -> None:
        """Two retries should allow a third request to recover."""
        context = FakeLLMContext(
            [RuntimeError("one"), RuntimeError("two"), "recovered"]
        )

        result = await call_provider_with_retry(
            context,
            FakeLLMConfig(2),
            "prompt",
            max_tokens=20,
            temperature=0.1,
        )

        self.assertEqual(result, "recovered")
        self.assertEqual(context.calls, 3)

    def test_current_astrbot_token_usage_fields_are_mapped(self) -> None:
        """AstrBot input/output counters should populate report token totals."""
        response = SimpleNamespace(
            usage=AstrBotTokenUsage(input_other=7, input_cached=3, output=5)
        )

        self.assertEqual(
            extract_token_usage(response),
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )


class FakeUploadClient:
    """Matrix client that records media uploads and message sends."""

    def __init__(self) -> None:
        """Initialize upload and send call logs."""
        self.uploads: list[tuple[bytes, str, str]] = []
        self.messages: list[dict] = []

    async def upload_file(self, data: bytes, content_type: str, filename: str) -> dict:
        """Record an uploaded file.

        Args:
            data: Image bytes.
            content_type: Detected image MIME type.
            filename: Upload filename.

        Returns:
            A Matrix content URI response.
        """
        self.uploads.append((data, content_type, filename))
        return {"content_uri": "mxc://example/report"}

    async def send_message(self, room_id: str, event_type: str, content: dict) -> dict:
        """Record a Matrix room message.

        Args:
            room_id: Matrix room ID.
            event_type: Matrix event type.
            content: Matrix message content.

        Returns:
            A Matrix event response.
        """
        self.messages.append(
            {"room_id": room_id, "event_type": event_type, "content": content}
        )
        return {"event_id": "$sent"}


class FakeSendBotManager:
    """Bot manager exposing one Matrix upload client."""

    def __init__(self, client: FakeUploadClient) -> None:
        """Store the only Matrix client.

        Args:
            client: Client used for report delivery.
        """
        self.client = client
        self._bot_instances = {"matrix": client}

    def is_matrix_platform_id(self, platform_id: str) -> bool:
        """Recognize the test Matrix platform.

        Args:
            platform_id: Candidate platform ID.

        Returns:
            Whether the ID is the test Matrix platform.
        """
        return platform_id == "matrix"

    def is_plugin_enabled(self, platform_id: str, plugin_name: str) -> bool:
        """Report that the plugin is enabled.

        Args:
            platform_id: Candidate platform ID.
            plugin_name: Plugin identifier.

        Returns:
            Always ``True`` for the test client.
        """
        return True

    def get_bot_instance(self, platform_id: str):
        """Return the test client for Matrix.

        Args:
            platform_id: Requested platform ID.

        Returns:
            Test upload client, or ``None`` for another platform.
        """
        return self._bot_instances.get(platform_id)


class ReportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Verify image reports use local files and delivery failures propagate."""

    async def test_report_generator_requests_a_local_rendered_file(self) -> None:
        """Initial rendering should avoid a URL that must be downloaded again."""
        generator = ReportGenerator.__new__(ReportGenerator)
        generator.html_templates = SimpleNamespace(
            get_image_template_async=mock.AsyncMock(return_value="<html></html>")
        )
        generator._prepare_render_data = mock.AsyncMock(return_value={})
        generator._render_html_template = mock.Mock(return_value="<html></html>")
        renderer = mock.AsyncMock(return_value="/tmp/report.png")

        image_source, _ = await generator.generate_image_report(
            {}, "!room:example.org", renderer
        )

        self.assertEqual(image_source, "/tmp/report.png")
        self.assertFalse(renderer.await_args.args[2])

    async def test_scheduler_uploads_a_local_png_without_http_download(self) -> None:
        """A local renderer result should be read and uploaded directly."""
        png_data = b"\x89PNG\r\n\x1a\nlocal-image"
        client = FakeUploadClient()
        scheduler = AutoScheduler(
            SimpleNamespace(),
            None,
            None,
            None,
            FakeSendBotManager(client),
            None,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "report.png"
            image_path.write_bytes(png_data)

            sent = await scheduler._send_image_message(
                "!room:example.org", str(image_path)
            )

        self.assertTrue(sent)
        self.assertEqual(client.uploads, [(png_data, "image/png", "report.png")])
        self.assertEqual(client.messages[-1]["content"]["msgtype"], "m.image")

    async def test_pdf_filename_stays_inside_report_directory(self) -> None:
        """Room IDs and custom separators must produce a portable basename."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                get_reports_dir=lambda: Path(temp_dir),
                get_pdf_filename_format=lambda: "../report:{group_id}",
            )
            generator = ReportGenerator.__new__(ReportGenerator)
            generator.config_manager = config
            generator.html_templates = SimpleNamespace(
                get_pdf_template_async=mock.AsyncMock(return_value="<html></html>")
            )
            generator._prepare_render_data = mock.AsyncMock(return_value={})
            generator._render_html_template = mock.Mock(return_value="<html></html>")
            generator._html_to_pdf = mock.AsyncMock(return_value=True)

            result = await generator.generate_pdf_report(
                {}, "!room:example.org"
            )

        result_path = Path(result)
        self.assertEqual(result_path.parent, Path(temp_dir))
        self.assertTrue(result_path.name.endswith(".pdf"))
        self.assertNotRegex(result_path.name, r'[<>:"/\\|?*]')

    async def test_retry_renderer_reads_local_file_before_upload(self) -> None:
        """Retry rendering paths must not be passed to upload_file as text."""
        jpeg_data = b"\xff\xd8\xfflocal-image"
        client = FakeUploadClient()
        bot_manager = FakeSendBotManager(client)
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "report.jpg"
            image_path.write_bytes(jpeg_data)
            renderer = mock.AsyncMock(return_value=str(image_path))
            manager = RetryManager(bot_manager, renderer)

            succeeded = await manager._process_task(
                RetryTask("<html></html>", {}, "!room:example.org", "matrix")
            )

        self.assertTrue(succeeded)
        self.assertEqual(client.uploads, [(jpeg_data, "image/jpeg", "report.jpg")])

    async def test_text_delivery_failure_is_returned_to_scheduler(self) -> None:
        """Scheduled analysis must not report success when every send fails."""
        config = SimpleNamespace(get_output_format=lambda: "text")
        report_generator = SimpleNamespace(generate_text_report=lambda result: "report")
        scheduler = AutoScheduler(
            config,
            None,
            None,
            report_generator,
            SimpleNamespace(),
            None,
        )
        scheduler._send_text_message = mock.AsyncMock(return_value=False)

        sent = await scheduler._send_analysis_report("!room:example.org", {}, "matrix")

        self.assertFalse(sent)


class ConfigurationTests(unittest.TestCase):
    """Verify code fallbacks stay aligned with the public schema."""

    def test_optimized_schema_defaults_match_runtime_fallbacks(self) -> None:
        """Token and concurrency defaults should not change when keys are absent."""
        schema_path = Path(__file__).parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        manager = ConfigManager({})

        self.assertEqual(
            manager.get_max_concurrent_tasks(),
            schema["analysis"]["items"]["max_concurrent_tasks"]["default"],
        )
        self.assertEqual(
            manager.get_llm_timeout(), schema["llm"]["items"]["timeout"]["default"]
        )
        self.assertEqual(
            manager.get_dialogue_poll_max_tokens(),
            schema["analysis"]["items"]["dialogue_poll"]["items"]["max_tokens"][
                "default"
            ],
        )
        self.assertEqual(
            manager.get_personal_report_max_messages(),
            schema["analysis"]["items"]["personal_report"]["items"]["max_messages"][
                "default"
            ],
        )
        self.assertEqual(
            manager.get_user_title_max_tokens(),
            schema["analysis"]["items"]["user_title"]["items"]["max_tokens"]["default"],
        )
        self.assertEqual(
            manager.get_golden_quote_max_tokens(),
            schema["analysis"]["items"]["golden_quote"]["items"]["max_tokens"][
                "default"
            ],
        )

    def test_bot_matrix_ids_are_normalized(self) -> None:
        """Malformed scalar and duplicate entries should not leak downstream."""
        scalar = ConfigManager({"auto_analysis": {"bot_matrix_ids": " @bot:x "}})
        multiple = ConfigManager(
            {"auto_analysis": {"bot_matrix_ids": [" @bot:x ", "@bot:x", ""]}}
        )

        self.assertEqual(scalar.get_bot_matrix_ids(), ["@bot:x"])
        self.assertEqual(multiple.get_bot_matrix_ids(), ["@bot:x"])
