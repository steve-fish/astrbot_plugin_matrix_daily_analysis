"""
报告生成器模块
负责生成各种格式的分析报告
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

from ..visualization.activity_charts import ActivityVisualizer
from .templates import HTMLTemplates


class ReportGenerator:
    """报告生成器"""

    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.activity_visualizer = ActivityVisualizer()
        self.html_templates = HTMLTemplates(config_manager)  # 实例化 HTML 模板管理器

    async def generate_image_report(
        self, analysis_result: dict, group_id: str, html_render_func, avatar_getter=None
    ) -> tuple[str | None, str | None]:
        """
        生成图片格式的分析报告

        Returns:
            tuple[str | None, str | None]: (image_source, html_content)
            - image_source: Rendered local image path, or None on failure.
            - html_content: 生成的 HTML 内容，如果渲染失败但 HTML 生成成功，则返回此内容供重试
        """
        html_content = None
        try:
            # 准备渲染数据
            render_payload = await self._prepare_render_data(
                analysis_result,
                chart_template="activity_chart.html",
                avatar_getter=avatar_getter,
            )

            # 先渲染 HTML 模板（使用异步方法）
            image_template = await self.html_templates.get_image_template_async()
            html_content = self._render_html_template(image_template, render_payload)

            # 检查 HTML 内容是否有效
            if not html_content:
                logger.error("图片报告 HTML 渲染失败：返回空内容")
                return None, None

            logger.info(f"图片报告 HTML 渲染完成，长度：{len(html_content)} 字符")

            # 定义渲染策略
            render_strategies = [
                # 1. 第一策略：PNG, Ultra quality, Device scale
                {
                    "full_page": True,
                    "type": "png",
                    "scale": "device",
                    "device_scale_factor_level": "ultra",
                },
                # 2. 第二策略：JPEG, ultra, quality 100%, Device scale
                {
                    "full_page": True,
                    "type": "jpeg",
                    "quality": 100,
                    "scale": "device",
                    "device_scale_factor_level": "ultra",
                },
                # 3. 第三策略：JPEG, high, quality 80%, Device scale
                {
                    "full_page": True,
                    "type": "jpeg",
                    "quality": 95,
                    "scale": "device",
                    "device_scale_factor_level": "high",  # 尝试高分辨率
                },
                # 4. 第四策略：JPEG, normal quality, Device scale (后备)
                {
                    "full_page": True,
                    "type": "jpeg",
                    "quality": 80,
                    "scale": "device",
                    # normal quality
                },
            ]

            last_exception = None

            for image_options in render_strategies:
                try:
                    # Cleanse options
                    if image_options.get("type") == "png":
                        image_options["quality"] = None

                    logger.info(f"尝试渲染策略：{image_options}")
                    image_url = await html_render_func(
                        html_content,  # 渲染后的 HTML 内容
                        {},  # 空数据字典，因为数据已包含在 HTML 中
                        False,  # Download once and upload the local rendered file.
                        image_options,
                    )

                    if image_url:
                        logger.info(f"图片生成成功 ({image_options}): {image_url}")
                        return image_url, html_content
                    else:
                        logger.warning(f"渲染策略 {image_options} 返回空 URL")

                except Exception as e:
                    logger.warning(f"渲染策略 {image_options} 失败：{e}")
                    last_exception = e
                    logger.warning("尝试下一个策略")
                    continue

            # 如果所有策略都失败
            logger.error(f"所有渲染策略都失败。最后一个错误：{last_exception}")
            return None, html_content

        except Exception as e:
            logger.error(f"生成图片报告过程发生严重错误：{e}", exc_info=True)
            return None, html_content

    async def generate_pdf_report(
        self, analysis_result: dict, group_id: str, avatar_getter=None
    ) -> str | None:
        """生成 PDF 格式的分析报告"""
        try:
            # 确保输出目录存在（使用 asyncio.to_thread 避免阻塞）
            output_dir = Path(self.config_manager.get_reports_dir())
            await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)

            # 生成文件名
            current_date = datetime.now().strftime("%Y%m%d")
            filename_template = self.config_manager.get_pdf_filename_format()
            try:
                filename = filename_template.format(
                    group_id=group_id, date=current_date
                )
            except (AttributeError, IndexError, KeyError, TypeError, ValueError) as e:
                logger.warning(f"Invalid PDF filename template; using fallback: {e}")
                filename = f"群聊分析报告_{group_id}_{current_date}.pdf"
            # The Matrix room ID contains ':' and user templates may contain path
            # separators. Keep the report inside its data directory on every OS.
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(filename)).strip()
            filename = filename.rstrip(". ") or f"群聊分析报告_{current_date}.pdf"
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            if len(filename) > 180:
                filename = f"{filename[:176].rstrip()}.pdf"
            pdf_path = output_dir / filename

            # 准备渲染数据
            render_data = await self._prepare_render_data(
                analysis_result,
                chart_template="activity_chart_pdf.html",
                avatar_getter=avatar_getter,
            )
            logger.info(f"PDF 渲染数据准备完成，包含 {len(render_data)} 个字段")

            # 生成 HTML 内容（使用异步方法）
            pdf_template = await self.html_templates.get_pdf_template_async()
            html_content = self._render_html_template(pdf_template, render_data)

            # 检查 HTML 内容是否有效
            if not html_content:
                logger.error("PDF 报告 HTML 渲染失败：返回空内容")
                return None

            logger.info(f"HTML 内容生成完成，长度：{len(html_content)} 字符")

            # 转换为 PDF
            success = await self._html_to_pdf(html_content, str(pdf_path))

            if success:
                return str(pdf_path.absolute())
            else:
                return None

        except Exception as e:
            logger.error(f"生成 PDF 报告失败：{e}")
            return None

    def generate_text_report(self, analysis_result: dict) -> str:
        """生成文本格式的分析报告"""
        stats = analysis_result["statistics"]
        topics = analysis_result["topics"]
        user_titles = analysis_result["user_titles"]

        report = f"""
🎯 群聊日常分析报告
📅 {datetime.now().strftime("%Y年%m月%d日")}

📊 基础统计
• 消息总数：{stats.message_count}
• 参与人数：{stats.participant_count}
• 总字符数：{stats.total_characters}
• 表情数量：{stats.emoji_count}
• 最活跃时段：{stats.most_active_period}

💬 热门话题
"""

        max_topics = self.config_manager.get_max_topics()
        for i, topic in enumerate(topics[:max_topics], 1):
            contributors_str = "、".join(topic.contributors)
            report += f"{i}. {topic.topic}\n"
            report += f"   参与者：{contributors_str}\n"
            report += f"   {topic.detail}\n\n"

        report += "🏆 群友称号\n"
        max_user_titles = self.config_manager.get_max_user_titles()
        for title in user_titles[:max_user_titles]:
            report += f"• {title.name} - {title.title} ({title.mbti})\n"
            report += f"  {title.reason}\n\n"

        report += "💬 群圣经\n"
        max_golden_quotes = self.config_manager.get_max_golden_quotes()
        for i, quote in enumerate(stats.golden_quotes[:max_golden_quotes], 1):
            report += f'{i}. "{quote.content}" —— {quote.sender}\n'
            report += f"   {quote.reason}\n"
            event_id = str(getattr(quote, "event_id", "") or "")
            thread_root_id = str(getattr(quote, "thread_root_id", "") or "")
            if event_id:
                report += f"   event: {event_id}\n"
            if thread_root_id:
                report += f"   thread: {thread_root_id}\n"
            report += "\n"

        return report

    async def _prepare_render_data(
        self,
        analysis_result: dict,
        chart_template: str = "activity_chart.html",
        avatar_getter=None,
    ) -> dict:
        """准备渲染数据"""
        stats = analysis_result["statistics"]
        topics = analysis_result["topics"]
        user_titles = analysis_result["user_titles"]
        activity_viz = stats.activity_visualization

        # 使用 Jinja2 模板构建话题 HTML（批量渲染）
        max_topics = self.config_manager.get_max_topics()
        topics_list = []
        for i, topic in enumerate(topics[:max_topics], 1):
            topics_list.append(
                {
                    "index": i,
                    "topic": topic,
                    "contributors": "、".join(topic.contributors),
                }
            )

        topics_html = self.html_templates.render_template(
            "topic_item.html", topics=topics_list
        )
        logger.info(f"话题 HTML 生成完成，长度：{len(topics_html)}")

        # 使用 Jinja2 模板构建用户称号 HTML（批量渲染，包含头像）
        max_user_titles = self.config_manager.get_max_user_titles()
        titles_list = []
        for title in user_titles[:max_user_titles]:
            # 获取用户头像
            avatar_data = await self._get_user_avatar(str(title.matrix), avatar_getter)
            title_data = {
                "name": title.name,
                "title": title.title,
                "mbti": title.mbti,
                "reason": title.reason,
                "avatar_data": avatar_data,
            }
            titles_list.append(title_data)

        titles_html = self.html_templates.render_template(
            "user_title_item.html", titles=titles_list
        )
        logger.info(f"用户称号 HTML 生成完成，长度：{len(titles_html)}")

        # 使用 Jinja2 模板构建金句 HTML（批量渲染）
        max_golden_quotes = self.config_manager.get_max_golden_quotes()
        quotes_list = []
        for quote in stats.golden_quotes[:max_golden_quotes]:
            avatar_url = (
                await self._get_user_avatar(str(quote.matrix), avatar_getter)
                if quote.matrix
                else None
            )
            quotes_list.append(
                {
                    "content": quote.content,
                    "sender": quote.sender,
                    "reason": quote.reason,
                    "avatar_url": avatar_url,
                    "event_id": str(getattr(quote, "event_id", "") or ""),
                    "relation_type": str(getattr(quote, "relation_type", "") or ""),
                    "thread_root_id": str(getattr(quote, "thread_root_id", "") or ""),
                    "reply_event_id": str(getattr(quote, "reply_event_id", "") or ""),
                }
            )

        quotes_html = self.html_templates.render_template(
            "quote_item.html", quotes=quotes_list
        )
        logger.info(f"金句 HTML 生成完成，长度：{len(quotes_html)}")

        # 生成活跃度可视化 HTML
        chart_data = self.activity_visualizer.get_hourly_chart_data(
            activity_viz.hourly_activity
        )
        hourly_chart_html = self.html_templates.render_template(
            chart_template, chart_data=chart_data
        )
        logger.info(f"活跃度图表 HTML 生成完成，长度：{len(hourly_chart_html)}")

        # 准备最终渲染数据
        render_data = {
            "current_date": datetime.now().strftime("%Y年%m月%d日"),
            "current_datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message_count": stats.message_count,
            "participant_count": stats.participant_count,
            "total_characters": stats.total_characters,
            "emoji_count": stats.emoji_count,
            "most_active_period": stats.most_active_period,
            "topics_html": topics_html,
            "titles_html": titles_html,
            "quotes_html": quotes_html,
            "hourly_chart_html": hourly_chart_html,
            "total_tokens": stats.token_usage.total_tokens
            if stats.token_usage.total_tokens
            else 0,
            "prompt_tokens": stats.token_usage.prompt_tokens
            if stats.token_usage.prompt_tokens
            else 0,
            "completion_tokens": stats.token_usage.completion_tokens
            if stats.token_usage.completion_tokens
            else 0,
        }

        logger.info(f"渲染数据准备完成，包含 {len(render_data)} 个字段")
        return render_data

    def _render_html_template(self, template: str, data: dict) -> str:
        """HTML 模板渲染，使用 {{key}} 占位符格式

        Args:
            template: HTML 模板字符串
            data: 渲染数据字典
        """
        result = template

        for key, value in data.items():
            # 统一使用双大括号格式 {{key}}
            placeholder = "{{" + key + "}}"
            result = result.replace(placeholder, str(value))

        # 检查是否还有未替换的占位符
        if remaining_placeholders := re.findall(r"\{\{[^}]+\}\}", result):
            logger.warning(
                f"未替换的占位符 ({len(remaining_placeholders)}个): {remaining_placeholders[:10]}"
            )

        return result

    async def _get_user_avatar(self, user_id: str, avatar_getter=None) -> str | None:
        """获取用户头像的 base64 编码"""
        try:
            if avatar_getter:
                try:
                    avatar = await avatar_getter(user_id)
                    if avatar:
                        return avatar
                except Exception as e:
                    logger.warning(f"Avatar getter failed for {user_id}: {e}")

            return None

        except Exception as e:
            logger.error(f"获取用户头像失败 {user_id}: {e}")
            return None

    async def _html_to_pdf(self, html_content: str, output_path: str) -> bool:
        """将 HTML 内容转换为 PDF 文件"""
        try:
            # 动态导入 playwright
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                logger.error("playwright 未安装，无法生成 PDF")
                logger.info("💡 请尝试运行：pip install playwright")
                return False

            import os
            import sys

            logger.info("启动浏览器进行 PDF 转换 (使用 Playwright)")

            async with async_playwright() as p:
                browser = None

                executable_path = None

                # 0. 优先检查配置的自定义路径
                custom_browser_path = self.config_manager.get_browser_path()
                if custom_browser_path:
                    if Path(custom_browser_path).exists():
                        logger.info(
                            f"使用配置的自定义浏览器路径：{custom_browser_path}"
                        )
                        executable_path = custom_browser_path
                    else:
                        logger.warning(
                            f"配置的浏览器路径不存在：{custom_browser_path}，尝试自动检测..."
                        )

                # 1. 如果没有自定义路径，尝试自动检测系统浏览器
                if not executable_path:
                    system_browser_paths = []
                    if sys.platform.startswith("win"):
                        username = os.environ.get("USERNAME", "")
                        local_app_data = os.environ.get(
                            "LOCALAPPDATA", rf"C:\Users\{username}\AppData\Local"
                        )
                        program_files = os.environ.get(
                            "ProgramFiles", r"C:\Program Files"
                        )
                        program_files_x86 = os.environ.get(
                            "ProgramFiles(x86)", r"C:\Program Files (x86)"
                        )

                        system_browser_paths = [
                            os.path.join(
                                program_files, r"Google\Chrome\Application\chrome.exe"
                            ),
                            os.path.join(
                                program_files_x86,
                                r"Google\Chrome\Application\chrome.exe",
                            ),
                            os.path.join(
                                local_app_data, r"Google\Chrome\Application\chrome.exe"
                            ),
                            os.path.join(
                                program_files_x86,
                                r"Microsoft\Edge\Application\msedge.exe",
                            ),
                            os.path.join(
                                program_files, r"Microsoft\Edge\Application\msedge.exe"
                            ),
                        ]
                    elif sys.platform.startswith("linux"):
                        system_browser_paths = [
                            "/usr/bin/google-chrome",
                            "/usr/bin/google-chrome-stable",
                            "/usr/bin/chromium",
                            "/usr/bin/chromium-browser",
                            "/snap/bin/chromium",
                        ]
                    elif sys.platform.startswith("darwin"):
                        system_browser_paths = [
                            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                            "/Applications/Chromium.app/Contents/MacOS/Chromium",
                        ]

                    # 尝试找到可用的系统浏览器
                    for path in system_browser_paths:
                        if Path(path).exists():
                            executable_path = path
                            logger.info(f"使用系统浏览器：{path}")
                            break

                # 定义默认启动参数
                launch_kwargs = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--font-render-hinting=none",
                    ],
                }

                if executable_path:
                    launch_kwargs["executable_path"] = executable_path

                try:
                    if executable_path:
                        # 如果指定了路径，通常使用 chromium 启动
                        browser = await p.chromium.launch(**launch_kwargs)
                    else:
                        # 尝试直接启动，依赖 playwright install
                        logger.info("尝试启动 Playwright 托管的浏览器...")
                        browser = await p.chromium.launch(
                            headless=True, args=launch_kwargs["args"]
                        )

                except Exception as e:
                    logger.warning(f"浏览器启动失败：{e}")
                    if "Executable doesn't exist" in str(e) or "executable at" in str(
                        e
                    ):
                        logger.error("未找到可用的浏览器。")
                        logger.info(
                            "💡 请确保已安装 Playwright 浏览器：playwright install chromium"
                        )
                        logger.info("💡 或者安装 Google Chrome / Microsoft Edge")
                    return False

                if not browser:
                    return False

                try:
                    context = await browser.new_context(device_scale_factor=1)
                    page = await context.new_page()

                    # 设置页面内容
                    await page.set_content(
                        html_content, wait_until="networkidle", timeout=60000
                    )

                    # 生成 PDF
                    logger.info("开始生成 PDF...")
                    await page.pdf(
                        path=output_path,
                        format="A4",
                        print_background=True,
                        margin={
                            "top": "10mm",
                            "right": "10mm",
                            "bottom": "10mm",
                            "left": "10mm",
                        },
                    )
                    logger.info(f"PDF 生成成功：{output_path}")
                    return True

                except Exception as e:
                    logger.error(f"PDF 生成过程出错：{e}")
                    return False
                finally:
                    if browser:
                        await browser.close()

        except Exception as e:
            logger.error(f"Playwright 运行出错：{e}")
            return False
