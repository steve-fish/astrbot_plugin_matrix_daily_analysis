"""
个人群报告命令处理模块
"""

from datetime import datetime

from astrbot.api import logger


class PersonalReportHandler:
    """个人报告命令处理器"""

    def __init__(self, context, config_manager, message_analyzer):
        self.context = context
        self.config_manager = config_manager
        self.message_analyzer = message_analyzer

    async def generate_personal_report(
        self, messages: list[dict], user_id: str, unified_msg_origin: str = None
    ) -> str | None:
        """生成个人分析报告"""
        from ..analysis.utils.llm_utils import (
            call_provider_with_retry,
            extract_response_text,
        )

        try:
            # 基础统计
            stats = self.message_analyzer.message_handler.calculate_statistics(messages)

            # 获取配置
            max_messages = self.config_manager.get_personal_report_max_messages()
            max_tokens = self.config_manager.get_personal_report_max_tokens()
            custom_prompt = self.config_manager.get_personal_report_prompt()

            # 提取用户消息内容用于 LLM 分析
            message_texts = []
            # History is chronological; retain the newest configured sample window.
            for msg in messages[-max_messages:]:
                if not isinstance(msg, dict):
                    continue
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
                        if text:
                            message_texts.append(text)

            if not message_texts:
                return self.format_personal_basic_report(stats, user_id)

            # 构建 prompt
            if custom_prompt:
                # 使用自定义 prompt，支持 {messages} 占位符
                prompt = custom_prompt.replace(
                    "{messages}", chr(10).join(message_texts[:50])
                )
            else:
                # 使用默认 prompt
                prompt = f"""分析以下用户在群聊中的发言，生成一份简短的个人画像报告。

用户消息样本：
{chr(10).join(message_texts[:50])}

请分析：
1. 用户的说话风格和特点（2-3 句话）
2. 用户可能的兴趣爱好（根据话题推断）
3. 给用户一个有趣的群聊称号
4. 一句话总结

请用简洁有趣的语言输出，不要使用 markdown 格式。"""

            llm_resp = await call_provider_with_retry(
                self.context,
                self.config_manager,
                prompt,
                max_tokens=max_tokens,
                temperature=0.7,
                umo=unified_msg_origin,
                provider_id_key="personal_report_provider_id",
            )

            if llm_resp:
                analysis_text = extract_response_text(llm_resp)
            else:
                analysis_text = ""

            # 格式化最终报告
            report = f"""
🎯 您的群聊个人报告
📅 {datetime.now().strftime("%Y年%m月%d日")}

📊 基础统计
• 消息总数：{stats.message_count}
• 总字符数：{stats.total_characters}
• 表情数量：{stats.emoji_count}
• 最活跃时段：{stats.most_active_period}

🔮 AI 分析
{analysis_text if analysis_text else "暂无 AI 分析结果"}
"""
            return report

        except Exception as e:
            logger.error(f"生成个人报告失败：{e}", exc_info=True)
            return None

    def format_personal_basic_report(self, stats, user_id: str) -> str:
        """格式化基础个人报告（无 LLM 分析时使用）"""
        return f"""
🎯 您的群聊个人报告
📅 {datetime.now().strftime("%Y年%m月%d日")}

📊 基础统计
• 消息总数：{stats.message_count}
• 总字符数：{stats.total_characters}
• 表情数量：{stats.emoji_count}
• 最活跃时段：{stats.most_active_period}

💡 提示：消息内容较少，无法进行深度分析
"""
