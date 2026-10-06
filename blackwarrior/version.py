"""版本与构建标识。

黑武士把版本信息集中在这里，Electron 壳、Python 内核、UI 三处共用同一个真值源，
避免"界面显示 0.1.0、后端实际 0.0.9"这类对不上的问题。
"""

from __future__ import annotations

__version__ = "0.5.1"

#: 代号。黑武士 = BlackWarrior，内部简称 BW。
CODENAME = "BlackWarrior"

#: 内核协议版本。UI 与内核握手时校验，防止旧壳连新内核出现静默错位。
PROTOCOL = 1

#: 认知内核档位说明用的常量（写进 /status，便于排查"为什么没用上真内核"）。
TIER_BIONIC = "bionic"   # PASM 真核心 + 情绪系统
TIER_CORE = "core"       # PASM 真核心（无 torch 情绪）
TIER_LIGHT = "light"     # 纯内置降级实现
TIER_NONE = "none"       # 未接入任何认知内核


def version_info() -> dict:
    """返回给 UI / API 的版本字典。"""
    return {
        "version": __version__,
        "codename": CODENAME,
        "protocol": PROTOCOL,
        "python": _python_version(),
    }


def _python_version() -> str:
    import platform

    return platform.python_version()


def banner() -> str:
    """启动横幅（控制台用）。"""
    return f"{CODENAME} v{__version__} (protocol {PROTOCOL})"
