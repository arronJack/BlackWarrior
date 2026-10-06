"""本地媒体库（v0.5）。

纯标准库实现的媒体元数据解析与索引，见:mod:`.library`。
"""

from .library import CATEGORIES, MediaLibrary, category_of, probe, selftest

__all__ = ["CATEGORIES", "MediaLibrary", "category_of", "probe", "selftest"]