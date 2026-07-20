"""
PDF 工具模块
负责 PDF 相关的安装和管理功能
"""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor

from astrbot.api import logger


class PDFInstaller:
    """PDF 功能安装器"""

    # 类级别的线程池，用于异步下载任务
    _executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="playwright_install"
    )
    _install_status = {
        "in_progress": False,
        "completed": False,
        "failed": False,
        "error_message": None,
    }
    _install_task: asyncio.Task | None = None

    @staticmethod
    def _handle_install_task_done(task: asyncio.Task) -> None:
        if PDFInstaller._install_task is task:
            PDFInstaller._install_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug("Playwright 安装后台任务已取消")
        except Exception as e:
            logger.error(f"Playwright 安装后台任务异常退出：{e}")

    @staticmethod
    async def install_playwright(config_manager):
        """安装 Playwright 依赖"""
        try:
            logger.info("开始安装 Playwright...")

            # 1. 安装 pip 包
            logger.info("正在运行 pip install playwright...")
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pip",
                "install",
                "playwright>=1.40.0",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode()
                logger.error(f"playwright pip 安装失败：{error_msg}")
                return f"❌ pip install playwright 失败：{error_msg}"

            logger.info("pip 包安装成功，检查是否需要安装浏览器内核...")

            # 2. 检查自定义路径
            from pathlib import Path

            custom_path = config_manager.get_browser_path()
            if custom_path and Path(custom_path).exists():
                logger.info(
                    f"检测到自定义浏览器路径：{custom_path}，将跳过 Chromium 内核安装。"
                )
                return f"✅ Playwright 包安装成功。检测到自定义浏览器路径 `{custom_path}`，已跳过浏览器内核安装。您可以现在尝试生成 PDF。"

            # 3. 安装浏览器内核
            return await PDFInstaller.install_system_deps()

        except Exception as e:
            logger.error(f"安装 playwright 时出错：{e}")
            return f"❌ 安装过程中出错：{str(e)}"

    @staticmethod
    async def install_system_deps():
        """安装系统依赖 (运行 playwright install chromium)"""
        try:
            # 检查是否已经在安装中
            if PDFInstaller._install_status["in_progress"]:
                return "⏳ 浏览器内核正在后台安装中，请稍候..."

            PDFInstaller._install_status["in_progress"] = True
            PDFInstaller._install_status["completed"] = False
            PDFInstaller._install_status["failed"] = False
            PDFInstaller._install_status["error_message"] = None

            logger.info("启动后台任务安装 Chromium...")
            install_task = asyncio.create_task(
                PDFInstaller._background_playwright_install()
            )
            PDFInstaller._install_task = install_task
            install_task.add_done_callback(PDFInstaller._handle_install_task_done)

            return """🚀 浏览器内核安装任务已启动

正在运行 `playwright install chromium`...
这可能需要几分钟时间，取决于网络速度。
安装过程不会阻塞 Bot 的正常运行。
下载完成后平台日志会显示安装完成的日志。
"""

        except Exception as e:
            PDFInstaller._install_status["in_progress"] = False
            logger.error(f"启动安装任务失败：{e}")
            return f"❌ 启动安装任务失败：{e}"

    @staticmethod
    async def cancel_background_install() -> None:
        install_task = PDFInstaller._install_task
        if install_task and not install_task.done():
            install_task.cancel()
            try:
                await install_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"取消 Playwright 安装任务失败：{e}")
        PDFInstaller._install_task = None
        PDFInstaller._install_status["in_progress"] = False

    @staticmethod
    async def _background_playwright_install():
        """后台运行 playwright install"""
        try:
            logger.info("开始运行 playwright install chromium...")

            # 使用 shell 命令确保能找到 path 中的 playwright
            # 或者使用 python -m playwright install chromium
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # 等待完成，设置较长的超时
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                PDFInstaller._install_status["completed"] = True
                logger.info(f"✅ Playwright Chromium 安装成功：{stdout.decode()}")

                # 尝试安装系统依赖 (Linux only，通常不需要 root 无法执行，但尝试一下无妨或者提示用户)
                if sys.platform.startswith("linux"):
                    logger.info("正在尝试安装系统依赖 (install-deps)...")
                    # 无需 await 阻塞太久，这步通常需要 sudo，可能会失败，仅做尝试或提示
                    # 真正的系统依赖安装通常由 Dockerfile 或用户手动完成
                    # 这里我们仅记录日志建议
                    logger.info(
                        "💡 如果 Linux 下仍无法生成 PDF，请尝试运行：sudo playwright install-deps"
                    )

            else:
                PDFInstaller._install_status["failed"] = True
                PDFInstaller._install_status["error_message"] = stderr.decode()
                logger.error(f"❌ Playwright Chromium 安装失败：{stderr.decode()}")

        except Exception as e:
            PDFInstaller._install_status["failed"] = True
            PDFInstaller._install_status["error_message"] = str(e)
            logger.error(f"Playwright 安装后台任务出错：{e}")
        finally:
            PDFInstaller._install_status["in_progress"] = False

    @staticmethod
    def get_pdf_status(config_manager) -> str:
        """获取 PDF 功能状态"""
        if config_manager.playwright_available:
            version = config_manager.playwright_version or "未知版本"

            status = f"✅ PDF 功能可用 (playwright {version})"

            if PDFInstaller._install_status["in_progress"]:
                status += "\n⏳ 正在后台安装浏览器内核..."
            elif PDFInstaller._install_status["failed"]:
                status += f"\n❌ 上次浏览器安装失败：{PDFInstaller._install_status.get('error_message', '未知错误')}"

            return status
        else:
            return "❌ PDF 功能不可用 - 请输入 /安装 PDF 进行安装"
