"""
对话投票命令处理模块
"""

import importlib
import json
import re

from astrbot.api import logger

from ..utils.time_utils import parse_timestamp

DEFAULT_DIALOGUE_POLL_PROMPT = (
    "你是群聊文风模仿器。根据下面的聊天记录，生成一个单选投票：给出一个简短的问题 (question)，"
    "以及 {option_count} 条候选发言 (options)。候选发言必须是'嘎啦给目'风格，语气俏皮、有点碎碎念，但不要冒犯。"
    "不要@具体用户，不要包含隐私或敏感信息。每条候选发言 6-20 字。只输出 JSON 数组，且只包含一个对象，"
    '格式如下：[{"question":"...","options":["...","..."]}]。\\n\\n聊天记录：\\n{history_text}'
)
POLL_EVENT_TYPE_STABLE = "m.poll.start"
POLL_POLL_KEY_STABLE = "m.poll"
POLL_EVENT_TYPE_UNSTABLE = "org.matrix.msc3381.poll.start"
POLL_POLL_KEY_UNSTABLE = "org.matrix.msc3381.poll.start"


def _safe_import(module_path: str):
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        if module_path == e.name or module_path.startswith(f"{e.name}."):
            return None
        raise


def _import_matrix_adapter_module(module_path: str):
    for base in (
        "astrbot_plugin_matrix_adapter",
        "data.plugins.astrbot_plugin_matrix_adapter",
    ):
        module = _safe_import(f"{base}.{module_path}" if module_path else base)
        if module is not None:
            return module
    return None


class DialoguePollHandler:
    """对话投票命令处理器"""

    def __init__(self, config_manager, bot_manager):
        self.config_manager = config_manager
        self.bot_manager = bot_manager

    def format_messages_for_dialogue_prompt(
        self, messages: list[dict], max_messages: int = 120
    ) -> str:
        """将消息整理为对话提示词文本。"""
        prefixes = [
            prefix.casefold().strip()
            for prefix in self.config_manager.get_history_filter_prefixes()
            if isinstance(prefix, str) and prefix.strip()
        ]
        user_filters = {
            user.casefold().strip()
            for user in self.config_manager.get_history_filter_users()
            if isinstance(user, str) and user.strip()
        }
        skip_bot = self.config_manager.should_skip_history_bots()
        entries: list[tuple[float, str, str]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            sender_data = msg.get("sender", {})
            if not isinstance(sender_data, dict):
                sender_data = {}
            sender = sender_data.get("nickname") or sender_data.get("user_id") or "匿名"
            parsed_time = parse_timestamp(msg.get("time", 0))
            msg_time = parsed_time.timestamp() if parsed_time else 0.0
            sender_id = str(sender_data.get("user_id") or "").strip()
            message_items = msg.get("message", [])
            if not isinstance(message_items, list):
                continue
            for content in message_items:
                if not isinstance(content, dict):
                    continue
                if content.get("type") != "text":
                    continue
                content_data = content.get("data", {})
                if not isinstance(content_data, dict):
                    content_data = {}
                text = str(content_data.get("text", "") or "").strip()
                if not text:
                    continue
                if self._should_skip_history_message(
                    sender_id, text, prefixes, user_filters, skip_bot
                ):
                    continue
                if len(text) > 80:
                    text = text[:77] + "..."
                entries.append((msg_time, sender, text))

        if not entries:
            return ""

        entries.sort(key=lambda x: x[0])
        recent = entries[-max_messages:]
        lines = [f"{sender}: {text}" for _, sender, text in recent]
        return "\n".join(lines)

    def _should_skip_history_message(
        self,
        sender_id: str,
        text: str,
        prefixes: list[str],
        user_filters: set[str],
        skip_bot: bool,
    ) -> bool:
        """基于配置决定是否跳过该条历史消息。"""
        if skip_bot and sender_id and self.bot_manager:
            if self.bot_manager.should_filter_bot_message(sender_id):
                return True
        if sender_id and sender_id.casefold() in user_filters:
            return True
        lower_text = text.casefold().lstrip()
        for prefix in prefixes:
            if prefix and lower_text.startswith(prefix):
                return True
        return False

    def build_dialogue_poll_prompt(self, history_text: str, option_count: int) -> str:
        """构造对话投票的 LLM 提示词。"""
        template = (
            self.config_manager.get_dialogue_poll_prompt()
            or DEFAULT_DIALOGUE_POLL_PROMPT
        )
        try:
            return template.replace("{option_count}", str(option_count)).replace(
                "{history_text}", history_text
            )
        except Exception as e:
            logger.warning(f"对话投票提示词格式化失败，回退默认提示词：{e}")
            return DEFAULT_DIALOGUE_POLL_PROMPT.replace(
                "{option_count}", str(option_count)
            ).replace("{history_text}", history_text)

    def parse_dialogue_poll_json(self, text: str) -> tuple[str, list[str]] | None:
        """解析 LLM 输出的投票 JSON。"""
        from ..analysis.utils.json_utils import extract_json_array, fix_json

        if not text:
            return None
        json_text = extract_json_array(text)
        if not json_text:
            logger.warning("对话投票 JSON 匹配失败，未找到数组结构")
            return None
        json_text = fix_json(json_text)
        logger.debug(f"对话投票 JSON 修复后：{json_text}")
        try:
            data = json.loads(json_text)
        except Exception as e:
            logger.warning(
                f"Dialogue poll JSON parsing failed: {e} | "
                f"raw={text} | cleaned={json_text}"
            )
            data = None
        if data is None:
            return None
        if not isinstance(data, list) or not data:
            logger.warning("对话投票 JSON 内容异常（非列表或空）")
            return None
        first = data[0] if isinstance(data[0], dict) else None
        if not first:
            logger.warning("对话投票 JSON 第一个元素非对象或为空")
            return None
        question = str(first.get("question", "")).strip()
        options_raw = first.get("options", [])
        if not isinstance(options_raw, list):
            return None
        options: list[str] = []
        for item in options_raw:
            if not item:
                continue
            text_item = str(item).strip()
            if not text_item:
                continue
            if len(text_item) > 32:
                text_item = text_item[:29] + "..."
            if text_item not in options:
                options.append(text_item)
        if not question:
            question = "请选择下一句"
        if len(options) < 2:
            logger.warning("对话投票选项数量不足，LLM 输出：%s", options_raw)
            return None
        return question, options

    def parse_dialogue_poll_json_fallback(
        self, text: str
    ) -> tuple[str, list[str]] | None:
        """在 JSON 解析失败时尝试关键词提取 question/options。"""
        question_match = re.search(r'"question"\s*:\s*"([^"]+)"', text)
        options_match = re.search(r'"options"\s*:\s*\[([^\]]+)\]', text)
        if not question_match or not options_match:
            return None
        question = question_match.group(1).strip()
        candidate_block = options_match.group(1)
        options = []
        seen = set()
        for item in re.findall(r'"([^"]+)"', candidate_block):
            clean = item.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            if len(clean) > 32:
                clean = clean[:29] + "..."
            options.append(clean)
        if not question:
            question = "请选择下一句"
        if len(options) < 2:
            return None
        return question, options

    def build_poll_fallback_text(self, question: str, options: list[str]) -> str:
        """构建投票失败时的文本回退内容。"""
        safe_question = (question or "").strip() or "请选择"
        lines = [safe_question]
        lines.extend(
            [f"{idx + 1}. {opt}" for idx, opt in enumerate(options or []) if opt]
        )
        return "\n".join(lines).strip()

    async def send_dialogue_poll_via_adapter(
        self,
        event,
        platform_id: str | None,
        room_id: str,
        question: str,
        options: list[str],
    ) -> bool | None:
        """优先通过 Matrix 适配器直接发送投票。"""
        if hasattr(event, "client") and getattr(event, "client"):
            try:
                poll_module = _import_matrix_adapter_module(
                    "sender.handlers.poll",
                )
                if not poll_module or not hasattr(poll_module, "send_poll"):
                    raise RuntimeError("Matrix adapter poll handler not available")
                _send_poll = poll_module.send_poll

                is_encrypted_room = False
                if hasattr(event, "e2ee_manager") and event.e2ee_manager:
                    try:
                        is_encrypted_room = await event.client.is_room_encrypted(
                            room_id
                        )
                    except Exception as e:
                        logger.debug(f"检查房间加密状态失败：{e}")

                try:
                    await _send_poll(
                        event.client,
                        room_id,
                        question,
                        options,
                        reply_to=None,
                        thread_root=None,
                        use_thread=False,
                        is_encrypted_room=is_encrypted_room,
                        e2ee_manager=getattr(event, "e2ee_manager", None),
                        max_selections=1,
                        kind="m.disclosed",
                        event_type=POLL_EVENT_TYPE_UNSTABLE,
                        poll_key=POLL_POLL_KEY_UNSTABLE,
                    )
                    logger.info("对话投票已通过 Matrix 客户端发送（MSC3381）")
                    return True
                except Exception as e:
                    logger.warning(f"发送投票失败，尝试回退到稳定事件类型：{e}")

                try:
                    await _send_poll(
                        event.client,
                        room_id,
                        question,
                        options,
                        reply_to=None,
                        thread_root=None,
                        use_thread=False,
                        is_encrypted_room=is_encrypted_room,
                        e2ee_manager=getattr(event, "e2ee_manager", None),
                        max_selections=1,
                        kind="m.disclosed",
                        event_type=POLL_EVENT_TYPE_STABLE,
                        poll_key=POLL_POLL_KEY_STABLE,
                    )
                    logger.info("对话投票已通过 Matrix 客户端发送（稳定事件类型）")
                    return True
                except Exception as e:
                    logger.error(f"发送投票失败（稳定事件类型仍失败）：{e}")
                    return False
            except Exception as e:
                logger.debug(f"Matrix 客户端投票发送路径不可用：{e}")

        platform = None
        if self.bot_manager:
            platform = self.bot_manager.get_platform(
                platform_id=platform_id, platform_name="matrix"
            )
        if not platform:
            return None

        sender = getattr(platform, "sender", None)
        if not sender or not hasattr(sender, "send_poll"):
            return None

        try:
            await sender.send_poll(
                room_id,
                question=question,
                answers=options,
                max_selections=1,
                event_type=POLL_EVENT_TYPE_UNSTABLE,
                poll_key=POLL_POLL_KEY_UNSTABLE,
            )
            logger.info("对话投票已通过 Matrix 适配器发送（MSC3381）")
            return True
        except Exception as e:
            logger.warning(f"发送投票失败，尝试回退到稳定事件类型：{e}")

        try:
            await sender.send_poll(
                room_id,
                question=question,
                answers=options,
                max_selections=1,
                event_type=POLL_EVENT_TYPE_STABLE,
                poll_key=POLL_POLL_KEY_STABLE,
            )
            logger.info("对话投票已通过 Matrix 适配器发送（稳定事件类型）")
            return True
        except Exception as e:
            logger.error(f"发送投票失败（回退事件类型仍失败）：{e}")
            return False
