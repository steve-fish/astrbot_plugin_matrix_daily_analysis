"""
设置命令处理模块
"""

import asyncio
import os

from astrbot.api import logger

from ..utils.pdf_utils import PDFInstaller


class SettingsHandler:
    """设置命令处理器"""

    def __init__(self, config_manager, plugin_dir: str):
        self.config_manager = config_manager
        self.plugin_dir = plugin_dir

    def get_output_format_info(self) -> str:
        """获取输出格式信息"""
        current_format = self.config_manager.get_output_format()
        pdf_status = (
            "✅"
            if self.config_manager.playwright_available
            else "❌ (需安装 Playwright)"
        )
        return f"""📊 当前输出格式：{current_format}

可用格式：
• image - 图片格式 (默认)
• text - 文本格式
• pdf - PDF 格式 {pdf_status}

用法：/设置格式 [格式名称]"""

    def set_output_format(self, format_type: str) -> tuple[bool, str]:
        """设置输出格式"""
        format_type = format_type.lower()
        if format_type not in ["image", "text", "pdf"]:
            return False, "❌ 无效的格式类型，支持：image, text, pdf"

        if format_type == "pdf" and not self.config_manager.playwright_available:
            return False, "❌ PDF 格式不可用，请使用 /安装 PDF 命令安装依赖"

        self.config_manager.set_output_format(format_type)
        return True, f"✅ 输出格式已设置为：{format_type}"

    async def list_templates(self) -> list[str]:
        """获取可用模板列表"""
        template_base_dir = os.path.join(self.plugin_dir, "src", "reports", "templates")

        def _list_templates_sync():
            if os.path.exists(template_base_dir):
                return sorted(
                    [
                        d
                        for d in os.listdir(template_base_dir)
                        if os.path.isdir(os.path.join(template_base_dir, d))
                        and not d.startswith("__")
                    ]
                )
            return []

        return await asyncio.to_thread(_list_templates_sync)

    def get_template_info(self, available_templates: list[str]) -> str:
        """获取模板信息"""
        current_template = self.config_manager.get_report_template()
        template_list_str = "\n".join(
            [f"【{i}】{t}" for i, t in enumerate(available_templates, start=1)]
        )
        return f"""🎨 当前报告模板：{current_template}

可用模板：
{template_list_str}

用法：/设置模板 [模板名称或序号]
💡 使用 /查看模板 查看预览图"""

    async def set_template(
        self, template_input: str, available_templates: list[str]
    ) -> tuple[bool, str]:
        """设置模板"""
        template_base_dir = os.path.join(self.plugin_dir, "src", "reports", "templates")

        # 判断输入是序号还是模板名称
        template_name = template_input
        if template_input.isdigit():
            index = int(template_input)
            if 1 <= index <= len(available_templates):
                template_name = available_templates[index - 1]
            else:
                return (
                    False,
                    f"❌ 无效的序号 '{template_input}'，有效范围：1-{len(available_templates)}",
                )

        # 检查模板是否存在
        template_dir = os.path.join(template_base_dir, template_name)
        template_exists = await asyncio.to_thread(os.path.exists, template_dir)
        if not template_exists:
            return False, f"❌ 模板 '{template_name}' 不存在"

        self.config_manager.set_report_template(template_name)
        return True, f"✅ 报告模板已设置为：{template_name}"

    def get_template_preview_path(self, template_name: str) -> str | None:
        """获取模板预览图路径"""
        assets_dir = os.path.join(self.plugin_dir, "assets")
        preview_image_path = os.path.join(assets_dir, f"{template_name}-demo.jpg")
        if os.path.exists(preview_image_path):
            return preview_image_path
        return None

    async def install_pdf_deps(self) -> str:
        """安装 PDF 依赖"""
        try:
            result = await PDFInstaller.install_playwright(self.config_manager)
            return result
        except Exception as e:
            logger.error(f"安装 PDF 依赖失败：{e}", exc_info=True)
            return f"❌ 安装过程中出现错误：{str(e)}"

    def get_analysis_status(self, group_id: str) -> str:
        """获取分析状态信息"""
        is_allowed = self.config_manager.is_group_allowed(group_id)
        status = "已启用" if is_allowed else "未启用"
        mode = self.config_manager.get_group_list_mode()

        auto_status = (
            "已启用" if self.config_manager.get_enable_auto_analysis() else "未启用"
        )
        auto_time = self.config_manager.get_auto_analysis_time()

        pdf_status = PDFInstaller.get_pdf_status(self.config_manager)
        output_format = self.config_manager.get_output_format()
        min_threshold = self.config_manager.get_min_messages_threshold()

        return f"""📊 当前群分析功能状态：
• 群分析功能：{status} (模式：{mode})
• 自动分析：{auto_status} ({auto_time})
• 输出格式：{output_format}
• PDF 功能：{pdf_status}
• 最小消息数：{min_threshold}

💡 可用命令：enable, disable, status, reload, test
💡 支持的输出格式：image, text, pdf (图片和 PDF 包含活跃度可视化)
💡 其他命令：/设置格式，/安装 PDF"""

    def handle_enable_group(self, group_id: str) -> str:
        """启用群组"""
        mode = self.config_manager.get_group_list_mode()
        if mode == "whitelist":
            glist = self.config_manager.get_group_list()
            if group_id not in glist:
                glist.append(group_id)
                self.config_manager.set_group_list(glist)
                return "✅ 已将当前群加入白名单"
            else:
                return "ℹ️ 当前群已在白名单中"
        elif mode == "blacklist":
            glist = self.config_manager.get_group_list()
            if group_id in glist:
                glist.remove(group_id)
                self.config_manager.set_group_list(glist)
                return "✅ 已将当前群从黑名单移除"
            else:
                return "ℹ️ 当前群不在黑名单中"
        else:
            return "ℹ️ 当前为无限制模式，所有群聊默认启用"

    def handle_disable_group(self, group_id: str) -> str:
        """禁用群组"""
        mode = self.config_manager.get_group_list_mode()
        if mode == "whitelist":
            glist = self.config_manager.get_group_list()
            if group_id in glist:
                glist.remove(group_id)
                self.config_manager.set_group_list(glist)
                return "✅ 已将当前群从白名单移除"
            else:
                return "ℹ️ 当前群不在白名单中"
        elif mode == "blacklist":
            glist = self.config_manager.get_group_list()
            if group_id not in glist:
                glist.append(group_id)
                self.config_manager.set_group_list(glist)
                return "✅ 已将当前群加入黑名单"
            else:
                return "ℹ️ 当前群已在黑名单中"
        else:
            return "ℹ️ 当前为无限制模式，如需禁用请切换到黑名单模式"
