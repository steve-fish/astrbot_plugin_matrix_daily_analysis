"""
配置管理模块
负责处理插件配置和 PDF 依赖检查
"""

import sys
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

MAX_ANALYSIS_DAYS = 31


def get_default_reports_dir():
    """获取插件报告目录（Path）"""
    try:
        plugin_name = "astrbot_plugin_matrix_daily_analysis"
        data_path = get_astrbot_data_path()
        return data_path / "plugin_data" / plugin_name / "reports"
    except Exception:
        from pathlib import Path

        return Path("data/plugins/astrbot_plugin_matrix_daily_analysis/reports")


class ConfigManager:
    """配置管理器"""

    def __init__(self, config: AstrBotConfig):
        self.config = config
        self._playwright_available = False
        self._playwright_version = None
        self._check_playwright_availability()
        self._sentinel = object()

    def _get_nested(
        self, path: tuple[str, ...], default=None, legacy_key: str | None = None
    ):
        if legacy_key is not None:
            legacy_value = self.config.get(legacy_key, self._sentinel)
            if legacy_value is not self._sentinel:
                return legacy_value

        current = self.config.get(path[0], self._sentinel)
        if current is self._sentinel:
            return default
        for key in path[1:]:
            if not isinstance(current, dict):
                return default
            current = current.get(key, self._sentinel)
            if current is self._sentinel:
                return default
        return current

    def _set_nested(self, path: tuple[str, ...], value):
        root_key = path[0]
        root = self.config.get(root_key, None)
        if not isinstance(root, dict):
            root = {}
        current = root
        for key in path[1:-1]:
            child = current.get(key)
            if not isinstance(child, dict):
                child = {}
            current[key] = child
            current = child
        current[path[-1]] = value
        self.config[root_key] = root
        self.config.save_config()

    @staticmethod
    def _normalize_bool(value: object, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        raw = str(value).strip().lower()
        if raw in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if raw in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return default

    @staticmethod
    def _normalize_int(
        value: object,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = default
        if minimum is not None:
            normalized = max(minimum, normalized)
        if maximum is not None:
            normalized = min(maximum, normalized)
        return normalized

    def get_group_list_mode(self) -> str:
        """获取群组列表模式 (whitelist/blacklist/none)"""
        raw_mode = (
            str(
                self._get_nested(("group_access", "mode"), "none", "group_list_mode")
                or "none"
            )
            .strip()
            .lower()
        )
        if raw_mode in {"whitelist", "blacklist", "none"}:
            return raw_mode
        return "none"

    def get_group_list(self) -> list[str]:
        """获取群组列表（用于黑白名单）"""
        raw_list = self._get_nested(("group_access", "list"), [], "group_list")
        if not isinstance(raw_list, list):
            return []
        return [str(item) for item in raw_list if str(item or "").strip()]

    def is_group_allowed(self, group_id: str) -> bool:
        """根据配置的白/黑名单判断是否允许在该群聊中使用"""
        mode = self.get_group_list_mode().lower()
        if mode not in ("whitelist", "blacklist", "none"):
            mode = "none"

        # none 模式下，不进行黑白名单检查，由调用方决定（通常是回退到 enabled_groups）
        if mode == "none":
            return True

        glist = [str(g) for g in self.get_group_list()]
        group_id_str = str(group_id)

        if mode == "whitelist":
            return group_id_str in glist if glist else False
        if mode == "blacklist":
            return group_id_str not in glist if glist else True

        return True

    def get_max_concurrent_tasks(self) -> int:
        """获取自动分析最大并发数"""
        value = self._get_nested(
            ("analysis", "max_concurrent_tasks"), 3, "max_concurrent_tasks"
        )
        return self._normalize_int(value, 3, minimum=1)

    def get_max_messages(self) -> int:
        """获取最大消息数量"""
        value = self._get_nested(("analysis", "max_messages"), 1000, "max_messages")
        return self._normalize_int(value, 1000, minimum=1)

    def get_analysis_days(self) -> int:
        """获取分析天数"""
        days = self._get_nested(("analysis", "days"), 1, "analysis_days")
        return self._normalize_int(
            days,
            1,
            minimum=1,
            maximum=MAX_ANALYSIS_DAYS,
        )

    def get_history_filter_prefixes(self) -> list[str]:
        """获取历史消息过滤前缀（全局）"""
        raw_value = self._get_nested(
            ("analysis", "history_filters", "prefixes"),
            [],
            "history_filter_prefixes",
        )
        if not isinstance(raw_value, list):
            return []
        return [str(item) for item in raw_value if str(item or "").strip()]

    def get_history_filter_users(self) -> list[str]:
        """获取历史消息过滤用户（全局）"""
        raw_value = self._get_nested(
            ("analysis", "history_filters", "users"),
            [],
            "history_filter_users",
        )
        if not isinstance(raw_value, list):
            return []
        return [str(item) for item in raw_value if str(item or "").strip()]

    def should_skip_history_bots(self) -> bool:
        """判断是否全局跳过机器人发言"""
        value = self._get_nested(
            ("analysis", "history_filters", "skip_bots"),
            True,
            "history_filter_skip_bots",
        )
        return self._normalize_bool(value, True)

    def get_auto_analysis_time(self) -> str:
        """获取自动分析时间"""
        auto_time = self._get_nested(
            ("auto_analysis", "time"), "09:00", "auto_analysis_time"
        )
        normalized = self._normalize_auto_analysis_time(auto_time)
        if normalized is None:
            logger.warning(f"自动分析时间配置无效：{auto_time!r}，已回退默认值 09:00")
            return "09:00"
        return normalized

    def get_enable_auto_analysis(self) -> bool:
        """获取是否启用自动分析"""
        value = self._get_nested(
            ("auto_analysis", "enabled"), False, "enable_auto_analysis"
        )
        return self._normalize_bool(value, False)

    def get_output_format(self) -> str:
        """获取输出格式"""
        raw_format = (
            str(
                self._get_nested(("output", "format"), "image", "output_format")
                or "image"
            )
            .strip()
            .lower()
        )
        if raw_format in {"image", "text", "pdf"}:
            return raw_format
        return "image"

    def get_min_messages_threshold(self) -> int:
        """获取最小消息阈值"""
        value = self._get_nested(
            ("analysis", "min_messages_threshold"), 50, "min_messages_threshold"
        )
        return self._normalize_int(value, 50, minimum=1)

    def get_topic_analysis_enabled(self) -> bool:
        """获取是否启用话题分析"""
        value = self._get_nested(
            ("analysis", "topic", "enabled"), True, "topic_analysis_enabled"
        )
        return self._normalize_bool(value, True)

    def get_user_title_analysis_enabled(self) -> bool:
        """获取是否启用用户称号分析"""
        value = self._get_nested(
            ("analysis", "user_title", "enabled"), True, "user_title_analysis_enabled"
        )
        return self._normalize_bool(value, True)

    def get_golden_quote_analysis_enabled(self) -> bool:
        """获取是否启用金句分析"""
        value = self._get_nested(
            ("analysis", "golden_quote", "enabled"),
            True,
            "golden_quote_analysis_enabled",
        )
        return self._normalize_bool(value, True)

    def get_threading_enabled(self) -> bool:
        """是否启用线程语义识别"""
        value = self._get_nested(
            ("analysis", "threading", "enabled"),
            True,
            "threading_enabled",
        )
        return self._normalize_bool(value, True)

    def get_thread_label_in_prompt(self) -> bool:
        """是否在提示词文本中加入线程标签"""
        value = self._get_nested(
            ("analysis", "threading", "label_in_prompt"),
            True,
            "thread_label_in_prompt",
        )
        return self._normalize_bool(value, True)

    def get_max_topics(self) -> int:
        """获取最大话题数量"""
        value = self._get_nested(("analysis", "topic", "max_topics"), 5, "max_topics")
        return self._normalize_int(value, 5, minimum=1)

    def get_max_user_titles(self) -> int:
        """获取最大用户称号数量"""
        value = self._get_nested(
            ("analysis", "user_title", "max_titles"), 8, "max_user_titles"
        )
        return self._normalize_int(value, 8, minimum=1)

    def get_max_golden_quotes(self) -> int:
        """获取最大金句数量"""
        value = self._get_nested(
            ("analysis", "golden_quote", "max_quotes"), 5, "max_golden_quotes"
        )
        return self._normalize_int(value, 5, minimum=1)

    def get_llm_timeout(self) -> int:
        """获取 LLM 请求超时时间（秒）"""
        value = self._get_nested(("llm", "timeout"), 100, "llm_timeout")
        return self._normalize_int(value, 100, minimum=1)

    def get_llm_retries(self) -> int:
        """获取 LLM 请求重试次数"""
        value = self._get_nested(("llm", "retries"), 2, "llm_retries")
        return self._normalize_int(value, 2, minimum=0)

    def get_llm_backoff(self) -> int:
        """获取 LLM 请求重试退避基值（秒），实际退避会乘以尝试次数"""
        value = self._get_nested(("llm", "backoff"), 2, "llm_backoff")
        return self._normalize_int(value, 2, minimum=0)

    def get_topic_max_tokens(self) -> int:
        """获取话题分析最大 token 数"""
        value = self._get_nested(
            ("analysis", "topic", "max_tokens"), 12288, "topic_max_tokens"
        )
        return self._normalize_int(value, 12288, minimum=1)

    def get_golden_quote_max_tokens(self) -> int:
        """获取金句分析最大 token 数"""
        value = self._get_nested(
            ("analysis", "golden_quote", "max_tokens"),
            4096,
            "golden_quote_max_tokens",
        )
        return self._normalize_int(value, 4096, minimum=1)

    def get_user_title_max_tokens(self) -> int:
        """获取用户称号分析最大 token 数"""
        value = self._get_nested(
            ("analysis", "user_title", "max_tokens"),
            4096,
            "user_title_max_tokens",
        )
        return self._normalize_int(value, 4096, minimum=1)

    def get_llm_provider_id(self) -> str:
        """获取主 LLM Provider ID"""
        return str(
            self._get_nested(("llm", "provider_id"), "", "llm_provider_id") or ""
        ).strip()

    def get_use_reaction_for_progress(self) -> bool:
        """是否使用 reaction 替代进度提示"""
        value = self._get_nested(
            ("interaction", "use_reaction_for_progress"),
            False,
            "use_reaction_for_progress",
        )
        return self._normalize_bool(value, False)

    def get_progress_reaction_emoji(self) -> str:
        """进度提示使用的 reaction 表情"""
        raw_emoji = str(
            self._get_nested(
                ("interaction", "progress_reaction_emoji"),
                "🫪",
                "progress_reaction_emoji",
            )
            or ""
        ).strip()
        return raw_emoji or "🫪"

    def get_topic_provider_id(self) -> str:
        """获取话题分析专用 Provider ID"""
        return str(
            self._get_nested(
                ("analysis", "topic", "provider_id"), "", "topic_provider_id"
            )
            or ""
        ).strip()

    def get_dialogue_poll_max_tokens(self) -> int:
        """对话投票生成的最大 token 限制"""
        value = self._get_nested(
            ("analysis", "dialogue_poll", "max_tokens"),
            400,
            "dialogue_poll_max_tokens",
        )
        return self._normalize_int(value, 400, minimum=1)

    def get_dialogue_poll_max_options(self) -> int:
        """对话投票生成的候选数量"""
        value = self._get_nested(
            ("analysis", "dialogue_poll", "max_options"),
            5,
            "dialogue_poll_max_options",
        )
        return self._normalize_int(value, 5, minimum=2)

    def get_dialogue_poll_prompt(self) -> str:
        """对话投票生成的提示词模板"""
        return self._get_nested(
            ("analysis", "dialogue_poll", "prompt"),
            """你是群聊文风模仿器。根据下面的聊天记录，生成一个单选投票：给出一个简短的问题 (question)，以及 {option_count} 条候选发言 (options)。候选发言必须是‘嘎啦给目’风格，语气俏皮、有点碎碎念，但不要冒犯。不要@具体用户，不要包含隐私或敏感信息。每条候选发言 6-20 字。只输出 JSON 数组，且只包含一个对象，格式如下：[{\"question\":\"...\",\"options\":[\"...\",\"...\"]}]。\\n\\n聊天记录：\\n{history_text}""",
            "dialogue_poll_prompt",
        )

    def get_user_title_provider_id(self) -> str:
        """获取用户称号分析专用 Provider ID"""
        return str(
            self._get_nested(
                ("analysis", "user_title", "provider_id"),
                "",
                "user_title_provider_id",
            )
            or ""
        ).strip()

    def get_golden_quote_provider_id(self) -> str:
        """获取金句分析专用 Provider ID"""
        return str(
            self._get_nested(
                ("analysis", "golden_quote", "provider_id"),
                "",
                "golden_quote_provider_id",
            )
            or ""
        ).strip()

    def get_personal_report_provider_id(self) -> str:
        """获取个人报告分析专用 Provider ID"""
        return str(
            self._get_nested(
                ("analysis", "personal_report", "provider_id"),
                "",
                "personal_report_provider_id",
            )
            or ""
        ).strip()

    def get_personal_report_max_tokens(self) -> int:
        """获取个人报告分析最大 token 数"""
        value = self._get_nested(
            ("analysis", "personal_report", "max_tokens"),
            800,
            "personal_report_max_tokens",
        )
        return self._normalize_int(value, 800, minimum=1)

    def get_personal_report_max_messages(self) -> int:
        """获取个人报告分析的最大消息数"""
        value = self._get_nested(
            ("analysis", "personal_report", "max_messages"),
            100,
            "personal_report_max_messages",
        )
        return self._normalize_int(value, 100, minimum=1)

    def get_personal_report_prompt(self) -> str:
        """获取个人报告分析提示词模板"""
        # 先尝试从 prompts 对象中获取
        prompts_config = self._get_nested(
            ("analysis", "personal_report", "prompts"), {}, "personal_report_prompts"
        )
        if isinstance(prompts_config, dict):
            prompt = prompts_config.get("personal_report_prompt")
            if prompt:
                return prompt
        # 兼容旧配置（直接使用 prompt 字段）
        legacy_prompt = self._get_nested(
            ("analysis", "personal_report", "prompt"),
            "",
            "personal_report_prompt",
        )
        return legacy_prompt or ""

    def get_reports_dir(self):
        """获取报告输出目录（固定为插件数据目录）"""
        return get_default_reports_dir()

    def get_bot_matrix_ids(self) -> list[str]:
        """获取 bot matrix 号列表"""
        raw_value = self._get_nested(
            ("auto_analysis", "bot_matrix_ids"), [], "bot_matrix_ids"
        )
        if isinstance(raw_value, str):
            raw_value = [raw_value]
        if not isinstance(raw_value, (list, tuple, set)):
            return []
        normalized = []
        seen = set()
        for item in raw_value:
            matrix_id = str(item or "").strip()
            if not matrix_id or matrix_id in seen:
                continue
            seen.add(matrix_id)
            normalized.append(matrix_id)
        return normalized

    def get_pdf_filename_format(self) -> str:
        """获取 PDF 文件名格式"""
        value = self._get_nested(
            ("output", "pdf", "filename_format"),
            "群聊分析报告_{group_id}_{date}.pdf",
            "pdf_filename_format",
        )
        return str(value or "群聊分析报告_{group_id}_{date}.pdf")

    def get_topic_analysis_prompt(self, style: str = "topic_prompt") -> str:
        """
        获取话题分析提示词模板

        Args:
            style: 提示词风格，默认为 "topic_prompt"

        Returns:
            提示词模板字符串
        """
        # 直接从配置中获取 prompts 对象
        prompts_config = self._get_nested(
            ("analysis", "topic", "prompts"), {}, "topic_analysis_prompts"
        )
        # 获取指定的 prompt
        if isinstance(prompts_config, dict):
            prompt = prompts_config.get(style) or prompts_config.get("topic_prompt")
            if prompt:
                return prompt
        # 兼容旧配置
        return self.config.get("topic_analysis_prompt", "")

    def get_user_title_analysis_prompt(self, style: str = "user_title_prompt") -> str:
        """
        获取用户称号分析提示词模板

        Args:
            style: 提示词风格，默认为 "user_title_prompt"

        Returns:
            提示词模板字符串
        """
        # 直接从配置中获取 prompts 对象
        prompts_config = self._get_nested(
            ("analysis", "user_title", "prompts"), {}, "user_title_analysis_prompts"
        )
        # 获取指定的 prompt
        if isinstance(prompts_config, dict):
            prompt = prompts_config.get(style) or prompts_config.get(
                "user_title_prompt"
            )
            if prompt:
                return prompt
        # 兼容旧配置
        return self.config.get("user_title_analysis_prompt", "")

    def get_golden_quote_analysis_prompt(
        self, style: str = "golden_quote_prompt"
    ) -> str:
        """
        获取金句分析提示词模板

        Args:
            style: 提示词风格，默认为 "golden_quote_prompt"

        Returns:
            提示词模板字符串
        """
        # 直接从配置中获取 prompts 对象
        prompts_config = self._get_nested(
            ("analysis", "golden_quote", "prompts"), {}, "golden_quote_analysis_prompts"
        )
        # 获取指定的 prompt
        if isinstance(prompts_config, dict):
            prompt = prompts_config.get(style) or prompts_config.get(
                "golden_quote_prompt"
            )
            if prompt:
                return prompt
        # 兼容旧配置
        return self.config.get("golden_quote_analysis_prompt", "")

    def set_topic_analysis_prompt(self, prompt: str):
        """设置话题分析提示词模板"""
        self._set_nested(("analysis", "topic", "prompts", "topic_prompt"), prompt)

    def set_user_title_analysis_prompt(self, prompt: str):
        """设置用户称号分析提示词模板"""
        self._set_nested(
            ("analysis", "user_title", "prompts", "user_title_prompt"), prompt
        )

    def set_golden_quote_analysis_prompt(self, prompt: str):
        """设置金句分析提示词模板"""
        self._set_nested(
            ("analysis", "golden_quote", "prompts", "golden_quote_prompt"), prompt
        )

    def set_output_format(self, format_type: str):
        """设置输出格式"""
        self._set_nested(("output", "format"), format_type)

    def set_group_list_mode(self, mode: str):
        """设置群组列表模式"""
        self._set_nested(("group_access", "mode"), mode)

    def set_group_list(self, groups: list[str]):
        """设置群组列表"""
        self._set_nested(("group_access", "list"), groups)

    def set_max_concurrent_tasks(self, count: int):
        """设置自动分析最大并发数"""
        self._set_nested(("analysis", "max_concurrent_tasks"), count)

    def set_max_messages(self, count: int):
        """设置最大消息数量"""
        self._set_nested(("analysis", "max_messages"), count)

    def set_analysis_days(self, days: int):
        """设置分析天数"""
        normalized_days = self._normalize_int(
            days,
            1,
            minimum=1,
            maximum=MAX_ANALYSIS_DAYS,
        )
        self._set_nested(("analysis", "days"), normalized_days)

    def set_auto_analysis_time(self, time_str: str):
        """设置自动分析时间"""
        normalized = self._normalize_auto_analysis_time(time_str)
        if normalized is None:
            logger.warning(
                f"尝试设置无效的自动分析时间：{time_str!r}，已回退默认值 09:00"
            )
            normalized = "09:00"
        self._set_nested(("auto_analysis", "time"), normalized)

    @staticmethod
    def _normalize_auto_analysis_time(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.strptime(raw, "%H:%M")
        except ValueError:
            return None
        return parsed.strftime("%H:%M")

    def set_enable_auto_analysis(self, enabled: bool):
        """设置是否启用自动分析"""
        self._set_nested(("auto_analysis", "enabled"), enabled)

    def set_min_messages_threshold(self, threshold: int):
        """设置最小消息阈值"""
        self._set_nested(("analysis", "min_messages_threshold"), threshold)

    def set_topic_analysis_enabled(self, enabled: bool):
        """设置是否启用话题分析"""
        self._set_nested(("analysis", "topic", "enabled"), enabled)

    def set_user_title_analysis_enabled(self, enabled: bool):
        """设置是否启用用户称号分析"""
        self._set_nested(("analysis", "user_title", "enabled"), enabled)

    def set_golden_quote_analysis_enabled(self, enabled: bool):
        """设置是否启用金句分析"""
        self._set_nested(("analysis", "golden_quote", "enabled"), enabled)

    def set_threading_enabled(self, enabled: bool):
        """设置是否启用线程语义识别"""
        self._set_nested(("analysis", "threading", "enabled"), enabled)

    def set_thread_label_in_prompt(self, enabled: bool):
        """设置是否在提示词文本中加入线程标签"""
        self._set_nested(("analysis", "threading", "label_in_prompt"), enabled)

    def set_max_topics(self, count: int):
        """设置最大话题数量"""
        self._set_nested(("analysis", "topic", "max_topics"), count)

    def set_max_user_titles(self, count: int):
        """设置最大用户称号数量"""
        self._set_nested(("analysis", "user_title", "max_titles"), count)

    def set_max_golden_quotes(self, count: int):
        """设置最大金句数量"""
        self._set_nested(("analysis", "golden_quote", "max_quotes"), count)

    def set_pdf_filename_format(self, format_str: str):
        """设置 PDF 文件名格式"""
        self._set_nested(("output", "pdf", "filename_format"), format_str)

    def get_report_template(self) -> str:
        """获取报告模板名称"""
        return self._get_nested(("output", "template"), "scrapbook", "report_template")

    def set_report_template(self, template_name: str):
        """设置报告模板名称"""
        self._set_nested(("output", "template"), template_name)

    @property
    def playwright_available(self) -> bool:
        """检查 playwright 是否可用"""
        return self._playwright_available

    @property
    def playwright_version(self) -> str | None:
        """获取 playwright 版本"""
        return self._playwright_version

    def _check_playwright_availability(self):
        """检查 playwright 可用性"""
        try:
            import importlib.util

            if importlib.util.find_spec("playwright") is None:
                raise ImportError

            # 尝试导入以确保完整性
            import playwright
            from playwright.async_api import async_playwright  # noqa: F401

            self._playwright_available = True

            # 检查版本
            try:
                self._playwright_version = playwright.__version__
                logger.info(f"使用 playwright {self._playwright_version} 作为 PDF 引擎")
            except AttributeError:
                self._playwright_version = "unknown"
                logger.info("使用 playwright (版本未知) 作为 PDF 引擎")

        except ImportError:
            self._playwright_available = False
            self._playwright_version = None
            logger.warning(
                "playwright 未安装，PDF 功能将不可用。请使用 pip install playwright 安装，并运行 playwright install chromium"
            )

    def get_browser_path(self) -> str:
        """获取自定义浏览器路径"""
        return self._get_nested(("output", "pdf", "browser_path"), "", "browser_path")

    def set_browser_path(self, path: str):
        """设置自定义浏览器路径"""
        self._set_nested(("output", "pdf", "browser_path"), path)

    def reload_playwright(self) -> bool:
        """重新加载 playwright 模块"""
        try:
            logger.info("开始重新加载 playwright 模块...")

            # 移除所有 playwright 相关模块
            modules_to_remove = [
                mod for mod in sys.modules.keys() if mod.startswith("playwright")
            ]
            logger.info(f"移除模块：{modules_to_remove}")
            for mod in modules_to_remove:
                del sys.modules[mod]

            # 强制重新导入
            try:
                import playwright

                # 更新全局变量
                self._playwright_available = True
                try:
                    self._playwright_version = playwright.__version__
                    logger.info(
                        f"重新加载成功，playwright 版本：{self._playwright_version}"
                    )
                except AttributeError:
                    self._playwright_version = "unknown"
                    logger.info("重新加载成功，playwright 版本未知")

                return True

            except ImportError:
                logger.info("playwright 重新导入可能需要重启 AstrBot")
                self._playwright_available = False
                self._playwright_version = None
                return False
            except Exception:
                logger.info("playwright 重新导入失败")
                self._playwright_available = False
                self._playwright_version = None
                return False

        except Exception as e:
            logger.error(f"重新加载 playwright 时出错：{e}")
            return False

    def save_config(self):
        """保存配置到 AstrBot 配置系统"""
        try:
            self.config.save_config()
            logger.info("配置已保存")
        except Exception as e:
            logger.error(f"保存配置失败：{e}")

    def reload_config(self):
        """重新加载配置"""
        try:
            # 重新从 AstrBot 配置系统读取所有配置
            logger.info("重新加载配置...")
            # 配置会自动从 self.config 中重新读取
            logger.info("配置重载完成")
        except Exception as e:
            logger.error(f"重新加载配置失败：{e}")
