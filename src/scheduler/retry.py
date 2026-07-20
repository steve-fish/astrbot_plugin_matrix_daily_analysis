import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger


@dataclass
class RetryTask:
    """重试任务数据类"""

    html_content: str
    analysis_result: dict  # 保存原始分析结果，用于文本回退
    group_id: str
    platform_id: str  # 需要保存 platform_id 以便找回 Bot
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()


class RetryManager:
    """
    重试管理器

    实现了一个简单的延迟队列 + 死信队列机制：
    1. 任务加入队列
    2. Worker 取出任务，尝试执行
    3. 失败则指数退避（延迟）后放回队列
    4. 超过最大重试次数放入死信队列
    """

    def __init__(self, bot_manager, html_render_func: Callable, report_generator=None):
        self.bot_manager = bot_manager
        self.html_render_func = html_render_func
        self.report_generator = report_generator  # 用于生成文本报告
        self.queue = asyncio.Queue()
        self.running = False
        self.worker_task = None
        self._requeue_tasks: set[asyncio.Task] = set()
        self._dlq = []  # 死信队列 (Failures)

    def _handle_worker_task_done(self, task: asyncio.Task) -> None:
        if self.worker_task is task:
            self.worker_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug("[RetryManager] Worker 任务已取消")
        except Exception as e:
            logger.error(f"[RetryManager] Worker 任务异常退出：{e}", exc_info=True)
            self.running = False

    def _handle_requeue_task_done(self, task: asyncio.Task) -> None:
        self._requeue_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[RetryManager] 延迟重试任务异常退出：{e}", exc_info=True)

    def _resolve_retry_bot_instance(
        self, platform_id: str
    ) -> tuple[str | None, object | None]:
        normalized = str(platform_id or "").strip()
        if normalized:
            bot = self.bot_manager.get_bot_instance(normalized)
            plugin_enabled = True
            if hasattr(self.bot_manager, "is_plugin_enabled"):
                plugin_enabled = self.bot_manager.is_plugin_enabled(
                    normalized, "astrbot_plugin_matrix_daily_analysis"
                )
            if (
                bot
                and self.bot_manager.is_matrix_platform_id(normalized)
                and plugin_enabled
            ):
                return normalized, bot

        bot_instances = getattr(self.bot_manager, "_bot_instances", {})
        if isinstance(bot_instances, dict):
            for fallback_platform_id, bot in bot_instances.items():
                plugin_enabled = True
                if hasattr(self.bot_manager, "is_plugin_enabled"):
                    plugin_enabled = self.bot_manager.is_plugin_enabled(
                        fallback_platform_id,
                        "astrbot_plugin_matrix_daily_analysis",
                    )
                if (
                    bot
                    and self.bot_manager.is_matrix_platform_id(fallback_platform_id)
                    and plugin_enabled
                ):
                    return str(fallback_platform_id), bot
        return None, None

    async def start(self):
        """启动重试工作进程"""
        if self.running:
            return
        self.running = True
        self.worker_task = asyncio.create_task(
            self._worker(),
            name="matrix-daily-analysis-retry-worker",
        )
        self.worker_task.add_done_callback(self._handle_worker_task_done)
        logger.info("[RetryManager] 图片重试管理器已启动")

    async def stop(self):
        """停止重试工作进程"""
        self.running = False
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        self.worker_task = None

        pending_requeue_tasks = [
            task for task in self._requeue_tasks if not task.done()
        ]
        for task in pending_requeue_tasks:
            task.cancel()
        if pending_requeue_tasks:
            await asyncio.gather(*pending_requeue_tasks, return_exceptions=True)
        self._requeue_tasks.clear()

        # 检查剩余任务
        pending_count = self.queue.qsize()
        if pending_count > 0:
            logger.warning(
                f"[RetryManager] 停止时仍有 {pending_count} 个任务在队列中 pending"
            )

        logger.info("[RetryManager] 图片重试管理器已停止")

    async def add_task(
        self, html_content: str, analysis_result: dict, group_id: str, platform_id: str
    ):
        """添加重试任务"""
        if not self.running:
            logger.warning(
                "[RetryManager] 警告：添加任务时管理器未运行，正在尝试启动..."
            )
            await self.start()

        task = RetryTask(
            html_content=html_content,
            analysis_result=analysis_result,
            group_id=group_id,
            platform_id=platform_id,
            created_at=time.time(),
        )
        await self.queue.put(task)
        logger.info(f"[RetryManager] 已添加群 {group_id} 的重试任务")

    async def _worker(self):
        """工作进程循环"""
        while self.running:
            task: RetryTask | None = None
            try:
                task = await self.queue.get()

                # 延迟策略：指数回退 (5s, 10s, 20s...) + 随机波动 (1~5s)
                jitter = random.uniform(1, 5)
                delay = 5 * (2**task.retry_count) + jitter

                logger.info(
                    f"[RetryManager] 处理群 {task.group_id} 的重试任务 (第 {task.retry_count + 1} 次尝试)"
                )

                success = await self._process_task(task)

                if success:
                    logger.info(f"[RetryManager] 群 {task.group_id} 重试成功")
                else:
                    task.retry_count += 1
                    if task.retry_count < task.max_retries:
                        logger.warning(
                            f"[RetryManager] 群 {task.group_id} 重试失败，{delay}秒后再次尝试"
                        )
                        self._schedule_requeue_after_delay(task, delay)
                    else:
                        logger.error(
                            f"[RetryManager] 群 {task.group_id} 超过最大重试次数，移入死信队列并尝试文本回退"
                        )
                        self._dlq.append(task)
                        # 尝试发送文本回退
                        await self._send_fallback_text(task)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RetryManager] Worker 异常：{e}", exc_info=True)
                await asyncio.sleep(1)
            finally:
                if task is not None:
                    self.queue.task_done()

    def _schedule_requeue_after_delay(self, task: RetryTask, delay: float) -> None:
        if not self.running:
            return
        requeue_task = asyncio.create_task(
            self._requeue_after_delay(task, delay),
            name=f"retry-requeue-{task.group_id}",
        )
        self._requeue_tasks.add(requeue_task)
        requeue_task.add_done_callback(self._handle_requeue_task_done)

    async def _requeue_after_delay(self, task: RetryTask, delay: float):
        try:
            await asyncio.sleep(delay)
            if not self.running:
                return
            await self.queue.put(task)
        except asyncio.CancelledError:
            logger.debug(f"[RetryManager] 群 {task.group_id} 延迟重试任务已取消")
            raise

    async def _process_task(self, task: RetryTask) -> bool:
        """执行具体的渲染和发送逻辑"""
        try:
            # 1. 尝试渲染
            image_options = {
                "full_page": True,
                "type": "jpeg",
                "quality": 85,
            }
            logger.debug(f"[RetryManager] 正在重新渲染群 {task.group_id} 的图片...")

            # A local file avoids requiring the Matrix client to reach a private URL.
            rendered_image = await self.html_render_func(
                task.html_content,
                {},
                False,
                image_options,
            )

            if not rendered_image:
                logger.warning(
                    f"[RetryManager] 重新渲染失败（返回空数据）{task.group_id}"
                )
                return False

            if isinstance(rendered_image, (bytes, bytearray)):
                image_data = bytes(rendered_image)
            else:
                image_path = Path(str(rendered_image))
                try:
                    if not await asyncio.to_thread(image_path.is_file):
                        logger.warning(
                            f"[RetryManager] Render retry returned an invalid file: "
                            f"{rendered_image}"
                        )
                        return False
                    if (
                        await asyncio.to_thread(image_path.stat)
                    ).st_size > 10 * 1024 * 1024:
                        logger.warning(
                            "[RetryManager] Rendered image exceeds the 10MB limit"
                        )
                        return False
                    image_data = await asyncio.to_thread(image_path.read_bytes)
                except (OSError, ValueError) as e:
                    logger.warning(f"[RetryManager] Failed to read rendered image: {e}")
                    return False

            if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
                content_type = "image/png"
                filename = "report.png"
                display_name = "Daily Report.png"
            elif image_data.startswith(b"\xff\xd8\xff"):
                content_type = "image/jpeg"
                filename = "report.jpg"
                display_name = "Daily Report.jpg"
            else:
                logger.warning("[RetryManager] Rendered output is not PNG or JPEG")
                return False

            # 2. 获取 Bot 实例（优先使用任务记录的平台，不可用时回退到可用 Matrix 平台）
            resolved_platform_id, bot = self._resolve_retry_bot_instance(
                task.platform_id
            )
            if not bot:
                logger.error(
                    f"[RetryManager] 平台 {task.platform_id} 的 Bot 实例未找到，无法重试"
                )
                return False  # 无法重试，因为 Bot 已离线

            if resolved_platform_id and resolved_platform_id != task.platform_id:
                logger.info(
                    "[RetryManager] 重试任务平台已重定向："
                    f"{task.platform_id} -> {resolved_platform_id}"
                )
                task.platform_id = resolved_platform_id

            # 3. 发送图片（Matrix 上传 + 发送）
            logger.info(
                f"[RetryManager] 正在向群 {task.group_id} 发送重试图片 (Matrix 上传模式)..."
            )
            client = bot.api if hasattr(bot, "api") else bot
            if not (hasattr(client, "upload_file") and hasattr(client, "send_message")):
                logger.warning(
                    "[RetryManager] Bot 缺少 Matrix 发送接口，无法发送图片。"
                )
                return False

            try:
                upload_resp = await client.upload_file(
                    image_data, content_type, filename
                )
                content_uri = (
                    upload_resp.get("content_uri")
                    if isinstance(upload_resp, dict)
                    else None
                )
                if not content_uri:
                    logger.warning("[RetryManager] 图片上传失败：未返回 content_uri")
                    return False

                await client.send_message(
                    task.group_id,
                    "m.room.message",
                    {
                        "msgtype": "m.text",
                        "body": "📊 每日群聊分析报告（重试发送）：",
                    },
                )
                await client.send_message(
                    task.group_id,
                    "m.room.message",
                    {
                        "msgtype": "m.image",
                        "body": display_name,
                        "url": content_uri,
                    },
                )
                return True
            except Exception as e:
                logger.error(f"[RetryManager] Matrix 图片发送失败：{e}")
                return False

        except Exception as e:
            logger.error(f"[RetryManager] 处理任务时发生意外错误：{e}", exc_info=True)
            return False

    async def _send_fallback_text(self, task: RetryTask):
        """发送文本回退报告（使用合并转发）"""
        if not self.report_generator:
            logger.warning("[RetryManager] 未配置 ReportGenerator，无法发送文本回退")
            return

        try:
            logger.info(f"[RetryManager] 正在为群 {task.group_id} 生成文本回退报告...")
            text_report = self.report_generator.generate_text_report(
                task.analysis_result
            )

            bot = self.bot_manager.get_bot_instance(task.platform_id)
            if not bot:
                return

            client = bot.api if hasattr(bot, "api") else bot
            if not hasattr(client, "send_message"):
                logger.warning(
                    "[RetryManager] Bot 缺少 Matrix room_send，无法发送回退文本"
                )
                return

            await client.send_message(
                task.group_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": f"⚠️ 图片报告多次生成失败，为您呈现文本版报告：\n{text_report}",
                },
            )

        except Exception as e:
            logger.error(f"[RetryManager] 文本回退发送失败：{e}", exc_info=True)
