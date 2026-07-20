"""
工具函数模块
包含 PDF 处理和通用工具函数
"""

from .time_utils import format_timestamp_hm, get_hour_from_timestamp, parse_timestamp

__all__ = [
    "PDFInstaller",
    "MessageAnalyzer",
    "parse_timestamp",
    "get_hour_from_timestamp",
    "format_timestamp_hm",
]


def __getattr__(name: str):
    """Lazily expose heavyweight utilities without creating import cycles.

    Args:
        name: Public attribute requested from this package.

    Returns:
        The requested utility class.

    Raises:
        AttributeError: If the requested name is not exported.
    """
    if name == "MessageAnalyzer":
        from .helpers import MessageAnalyzer

        return MessageAnalyzer
    if name == "PDFInstaller":
        from .pdf_utils import PDFInstaller

        return PDFInstaller
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
