"""路径与目录布局。

黑武士把所有可写状态收敛到**一个数据根**下面（开发态在仓库内、
打包态在用户目录），这样"打包后找不到数据库"这类问题不会发生。

布局::

    <data_root>/
        blackwarrior.db      主数据库（对话/记忆/行动/提醒/配置）
        config.json          持久化配置
        cognition/           PASM 认知内核侧车文件（索引/复习计数）
        sandbox/             Agent 工作区（生成文件、下载、媒体产物）
        logs/                运行日志
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

#: 环境变量可覆盖数据根，便于测试与多实例隔离。
ENV_DATA_ROOT = "BLACKWARRIOR_DATA_ROOT"
ENV_SANDBOX = "BLACKWARRIOR_SANDBOX"

_APP_NAME = "BlackWarrior"


def is_packaged() -> bool:
    """是否运行在冻结环境（PyInstaller / Nuitka / Electron 打包的内核）。"""
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def _default_root() -> Path:
    if is_packaged():
        # 打包态：落到用户目录，绝不写到 Program Files（那里通常不可写）。
        base = Path.home()
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / _APP_NAME
        elif sys.platform == "darwin":
            base = base / "Library" / "Application Support" / _APP_NAME
        else:
            base = base / ".config" / _APP_NAME.lower()
        return base

    # 开发态：仓库根下的 data/（已被 .gitignore 排除）。
    here = Path(__file__).resolve().parent.parent
    return here / "data"


def data_root() -> Path:
    """数据根目录（可通过环境变量覆盖）。"""
    override = os.environ.get(ENV_DATA_ROOT)
    root = Path(override) if override else _default_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def sandbox_dir() -> Path:
    """Agent 工作区。生成的文件、下载、媒体产物都放这里。"""
    override = os.environ.get(ENV_SANDBOX)
    p = Path(override) if override else (data_root() / "sandbox")
    p.mkdir(parents=True, exist_ok=True)
    return p


def cognition_dir() -> Path:
    """PASM 认知内核的落盘目录（索引、复习计数、巩固产物）。"""
    p = data_root() / "cognition"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = data_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    """主数据库文件路径。"""
    return data_root() / "blackwarrior.db"


def config_path() -> Path:
    return data_root() / "config.json"


def skills_dir() -> Path:
    """技能包目录（用户自装技能 / 工具市场产物）。"""
    p = data_root() / "skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def within_sandbox(path: str | Path) -> bool:
    """判断路径是否位于沙箱内。

    工具层的写操作用它做边界检查——Agent 可以随便在沙箱里造东西，
    但想往沙箱外写就必须走策略确认。
    """
    try:
        target = Path(path).resolve()
        root = sandbox_dir().resolve()
    except Exception:
        return False
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def safe_join_sandbox(rel: str) -> Optional[Path]:
    """把相对路径安全地拼到沙箱下，拒绝 ``..`` 逃逸。返回 None 表示非法。"""
    if not rel:
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        return None
    return sandbox_dir() / rel
