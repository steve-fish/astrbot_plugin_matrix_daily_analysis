"""
金句分析模块
专门处理群聊金句提取和分析
"""

from astrbot.api import logger

from ...models.data_models import GoldenQuote, TokenUsage
from ...utils.time_utils import format_timestamp_hm
from ..utils import InfoUtils
from ..utils.json_utils import extract_golden_quotes_with_regex
from .base_analyzer import BaseAnalyzer


class GoldenQuoteAnalyzer(BaseAnalyzer):
    """
    金句分析器
    专门处理群聊金句的提取和分析
    """

    def get_provider_id_key(self) -> str:
        """获取 Provider ID 配置键名"""
        return "golden_quote_provider_id"

    def get_data_type(self) -> str:
        """获取数据类型标识"""
        return "金句"

    def get_max_count(self) -> int:
        """获取最大金句数量"""
        return self.config_manager.get_max_golden_quotes()

    def get_max_tokens(self) -> int:
        """获取最大 token 数"""
        return self.config_manager.get_golden_quote_max_tokens()

    def get_temperature(self) -> float:
        """获取温度参数"""
        return 0.7

    def build_prompt(self, messages: list[dict]) -> str:
        """
        构建金句分析提示词

        Args:
            messages: 群聊的文本消息列表

        Returns:
            提示词字符串
        """
        if not messages:
            return ""

        # 构建消息文本
        messages_text = "\n".join(
            [f"[{msg['time']}] {msg['sender']}: {msg['content']}" for msg in messages]
        )

        max_golden_quotes = self.get_max_count()

        # 从配置读取 prompt 模板（默认使用 "default" 风格）
        prompt_template = self.config_manager.get_golden_quote_analysis_prompt()

        if prompt_template:
            # 使用配置中的 prompt 并替换变量
            try:
                prompt = prompt_template.format(
                    max_golden_quotes=max_golden_quotes, messages_text=messages_text
                )
                logger.info("使用配置中的金句分析提示词")
                return prompt
            except KeyError as e:
                logger.warning(f"金句分析提示词变量格式错误：{e}")
            except Exception as e:
                logger.warning(f"应用金句分析提示词失败：{e}")

        logger.warning("未找到有效的金句分析提示词配置，请检查配置文件")
        return ""

    def extract_with_regex(self, result_text: str, max_count: int) -> list[dict]:
        """
        使用正则表达式提取金句信息

        Args:
            result_text: LLM 响应文本
            max_count: 最大提取数量

        Returns:
            金句数据列表
        """
        return extract_golden_quotes_with_regex(result_text, max_count)

    def create_data_objects(self, quotes_data: list[dict]) -> list[GoldenQuote]:
        """
        创建金句对象列表

        Args:
            quotes_data: 原始金句数据列表

        Returns:
            GoldenQuote 对象列表
        """
        try:
            quotes = []
            max_quotes = self.get_max_count()

            for quote_data in quotes_data[:max_quotes]:
                if not isinstance(quote_data, dict):
                    logger.warning(f"Skipping non-object golden quote: {quote_data!r}")
                    continue
                # 确保数据格式正确
                content = str(quote_data.get("content", "") or "").strip()
                sender = str(quote_data.get("sender", "") or "").strip()
                reason = str(quote_data.get("reason", "") or "").strip()

                # 验证必要字段
                if not content or not sender or not reason:
                    logger.warning(f"金句数据格式不完整，跳过：{quote_data}")
                    continue

                quotes.append(
                    GoldenQuote(content=content, sender=sender, reason=reason)
                )

            return quotes

        except Exception as e:
            logger.error(f"创建金句对象失败：{e}")
            return []

    def extract_interesting_messages(self, messages: list[dict]) -> list[dict]:
        """
        提取圣经的文本消息

        Args:
            messages: 群聊消息列表

        Returns:
            圣经的文本消息列表
        """
        try:
            interesting_messages = []
            bot_matrix_ids = (
                {
                    str(matrix)
                    for matrix in self.config_manager.get_bot_matrix_ids()
                    if matrix
                }
                if self.config_manager.should_skip_history_bots()
                else set()
            )

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                sender = msg.get("sender", {})
                if not isinstance(sender, dict):
                    continue
                if str(sender.get("user_id", "")) in bot_matrix_ids:
                    continue
                nickname = InfoUtils.get_user_nickname(self.config_manager, sender)
                msg_time = format_timestamp_hm(msg.get("time", 0))

                message_items = msg.get("message", [])
                if not isinstance(message_items, list):
                    continue
                for content in message_items:
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") == "text":
                        data = content.get("data", {})
                        if not isinstance(data, dict):
                            data = {}
                        text = str(data.get("text", "") or "").strip()
                        # 过滤长度适中、可能圣经的消息
                        if 5 <= len(text) <= 100 and not text.startswith(
                            ("http", "www", "/")
                        ):
                            if (
                                self.config_manager.get_threading_enabled()
                                and self.config_manager.get_thread_label_in_prompt()
                            ):
                                relation_type = (
                                    str(msg.get("relation_type", "") or "")
                                    .strip()
                                    .lower()
                                )
                                thread_root_id = str(
                                    msg.get("thread_root_id", "") or ""
                                ).strip()
                                if relation_type == "m.thread" and thread_root_id:
                                    short_tid = (
                                        thread_root_id[-8:]
                                        if len(thread_root_id) > 8
                                        else thread_root_id
                                    )
                                    text = f"[thread:{short_tid}] {text}"
                            interesting_messages.append(
                                {
                                    "sender": nickname,
                                    "time": msg_time,
                                    "content": text,
                                    "matrix": str(sender.get("user_id", "")),
                                    "event_id": str(msg.get("event_id", "") or ""),
                                    "relation_type": str(
                                        msg.get("relation_type", "") or ""
                                    ),
                                    "thread_root_id": str(
                                        msg.get("thread_root_id", "") or ""
                                    ),
                                    "reply_event_id": str(
                                        msg.get("reply_event_id", "") or ""
                                    ),
                                }
                            )

            return interesting_messages

        except Exception as e:
            logger.error(f"提取圣经消息失败：{e}")
            return []

    async def analyze_golden_quotes(
        self, messages: list[dict], umo: str = None
    ) -> tuple[list[GoldenQuote], TokenUsage]:
        """
        分析群聊金句

        Args:
            messages: 群聊消息列表
            umo: 模型唯一标识符

        Returns:
            (金句列表，Token 使用统计)
        """
        try:
            # 提取圣经的文本消息
            interesting_messages = self.extract_interesting_messages(messages)

            if not interesting_messages:
                logger.info("没有符合条件的圣经消息，返回空结果")
                return [], TokenUsage()

            logger.info(f"开始从 {len(interesting_messages)} 条圣经消息中提取金句")
            quotes, usage = await self.analyze(interesting_messages, umo)

            # 回填 matrix 号
            for quote in quotes:
                for msg in interesting_messages:
                    # 尝试匹配内容和发送者
                    # 注意：LLM 可能会微调内容，这里使用包含匹配或精确匹配
                    if (
                        quote.content in msg["content"]
                        or msg["content"] in quote.content
                    ) and quote.sender == msg["sender"]:
                        quote.matrix = str(msg.get("matrix", ""))
                        quote.event_id = str(msg.get("event_id", ""))
                        quote.relation_type = str(msg.get("relation_type", ""))
                        quote.thread_root_id = str(msg.get("thread_root_id", ""))
                        quote.reply_event_id = str(msg.get("reply_event_id", ""))
                        break

            return quotes, usage

        except Exception as e:
            logger.error(f"金句分析失败：{e}")
            return [], TokenUsage()
