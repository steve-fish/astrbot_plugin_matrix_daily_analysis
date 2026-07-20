"""
群分析命令处理模块
"""

from astrbot.api import logger


class GroupAnalysisHandler:
    """群分析命令处理器"""

    def __init__(
        self,
        config_manager,
        message_analyzer,
        report_generator,
        auto_scheduler,
        retry_manager,
        bot_manager,
    ):
        self.config_manager = config_manager
        self.message_analyzer = message_analyzer
        self.report_generator = report_generator
        self.auto_scheduler = auto_scheduler
        self.retry_manager = retry_manager
        self.bot_manager = bot_manager

    async def handle_image_report(
        self, event, analysis_result: dict, group_id: str, html_render_func
    ):
        """处理图片格式报告的生成和发送"""
        (
            image_url,
            html_content,
        ) = await self.report_generator.generate_image_report(
            analysis_result, group_id, html_render_func
        )

        if image_url:
            # Matrix 平台发送图片（上传后发送）
            try:
                logger.info(f"正在尝试发送图片报告：{image_url}")
                sent = await self.auto_scheduler._send_image_message(
                    group_id, image_url
                )
                if sent:
                    logger.info(f"图片报告发送成功：{group_id}")
                    return True, None
                elif html_content:
                    platform_id = await self.auto_scheduler.get_platform_id_for_group(
                        group_id
                    )
                    await self.retry_manager.add_task(
                        html_content, analysis_result, group_id, platform_id
                    )
                    return (
                        False,
                        "[AstrBot matrix 群日常分析总结插件] ⚠️ 图片报告发送失败，已加入重试队列。",
                    )
                else:
                    return False, "❌ 图片发送失败，且无法进行重试（无 HTML 内容）。"
            except Exception as send_err:
                logger.error(f"图片报告发送失败：{send_err}")
                if html_content:
                    platform_id = await self.auto_scheduler.get_platform_id_for_group(
                        group_id
                    )
                    await self.retry_manager.add_task(
                        html_content, analysis_result, group_id, platform_id
                    )
                    return (
                        False,
                        "[AstrBot matrix 群日常分析总结插件] ⚠️ 图片报告发送异常，已加入重试队列。",
                    )
                else:
                    return (
                        False,
                        f"❌ 图片发送失败：{send_err}，且无法进行重试（无 HTML 内容）。",
                    )

        elif html_content:
            # 生成失败但有 HTML，加入重试队列
            logger.warning("图片报告生成失败，加入重试队列")
            platform_id = await self.auto_scheduler.get_platform_id_for_group(group_id)
            await self.retry_manager.add_task(
                html_content, analysis_result, group_id, platform_id
            )
            return (
                False,
                "[AstrBot matrix 群日常分析总结插件] ⚠️ 图片报告暂无法生成，已加入重试队列，稍后将自动重试发送。",
            )
        else:
            # 如果图片生成失败且无 HTML，回退到文本报告
            logger.warning("图片报告生成失败（无 HTML），回退到文本报告")
            text_report = self.report_generator.generate_text_report(analysis_result)
            return (
                False,
                f"[AstrBot matrix 群日常分析总结插件] ⚠️ 图片报告生成失败，以下是文本版本：\n\n{text_report}",
            )

    async def handle_pdf_report(self, event, analysis_result: dict, group_id: str):
        """处理 PDF 格式报告的生成和发送"""
        if not self.config_manager.playwright_available:
            return False, "❌ PDF 功能不可用，请使用 /安装 PDF 命令安装依赖"

        pdf_path = await self.report_generator.generate_pdf_report(
            analysis_result, group_id
        )
        if pdf_path:
            sent = await self.auto_scheduler._send_pdf_file(group_id, pdf_path)
            if not sent:
                logger.warning("PDF 发送失败，回退到文本报告")
                text_report = self.report_generator.generate_text_report(
                    analysis_result
                )
                return False, f"\n📝 以下是文本版本的分析报告：\n\n{text_report}"
            return True, None
        else:
            # 回退到文本报告
            logger.warning("PDF 报告生成失败，回退到文本报告")
            text_report = self.report_generator.generate_text_report(analysis_result)
            return False, f"\n📝 以下是文本版本的分析报告：\n\n{text_report}"

    def handle_text_report(self, analysis_result: dict) -> str:
        """处理文本格式报告的生成"""
        return self.report_generator.generate_text_report(analysis_result)
