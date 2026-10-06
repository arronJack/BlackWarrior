"""内置工具集。

按需注册：关闭的能力（shell / web）不会注册，
所以模型**根本看不到**被禁用的工具——比"注册了再拒绝"更干净，
也避免模型反复尝试被拒的工具浪费轮次。
"""

from __future__ import annotations

from typing import Any, List

from . import ambient, filesystem, memory, profile, shell, system, web


def register_builtin(reg: Any, ctx: Any, config: Any = None) -> List[str]:
    """注册全部内置工具，返回注册的工具名列表。"""
    names: List[str] = []

    def _enabled(key: str, default: bool = True) -> bool:
        if config is None:
            return default
        try:
            return bool(config.get(key, default))
        except Exception:
            return default

    before = set(reg.names())

    filesystem.register(reg, ctx)
    if _enabled("shell_enabled", True):
        shell.register(reg, ctx)
    if _enabled("web_enabled", True):
        web.register(reg, ctx)
    memory.register(reg, ctx)
    system.register(reg, ctx)
    profile.register(reg, ctx)        # v0.2：画像/面板/预取
    ambient.register(reg, ctx)         # v0.3：工具自发现/本机资源/线索/审计

    names = [n for n in reg.names() if n not in before]
    return names


__all__ = ["register_builtin", "filesystem", "shell", "web", "memory",
           "system", "profile", "ambient"]
