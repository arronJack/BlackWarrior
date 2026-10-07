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
from typing import List, Optional

#: 环境变量可覆盖数据根，便于测试与多实例隔离。
ENV_DATA_ROOT = "BLACKWARRIOR_DATA_ROOT"
ENV_SANDBOX = "BLACKWARRIOR_SANDBOX"

_APP_NAME = "BlackWarrior"

# ----------------------------------------------------------------------
# 授权根目录（"让黑武士住进你的电脑"的核心开关）
#
# 默认沙箱之外，用户可显式授权若干真实目录（桌面/文档/项目…），
# 让 Agent 真正能读写它们——而不是只能在自己的 sandbox 里打转。
# 这层状态由 core 启动时从 config 注入，工具层（policy/filesystem）
# 直接读这里，避免把 config 对象层层透传。
# ----------------------------------------------------------------------

_ALLOWED_ROOTS: List[Path] = []
_FULL_FS_ACCESS: bool = False


def set_allowed_roots(paths_list: Optional[List[str]]) -> None:
    """注入授权根目录（core 启动时调用，或 grant_access 工具运行时更新）。"""
    global _ALLOWED_ROOTS
    roots: List[Path] = []
    for p in (paths_list or []):
        try:
            pp = Path(str(p)).expanduser().resolve()
        except Exception:
            continue
        if pp not in roots:
            roots.append(pp)
    _ALLOWED_ROOTS = roots


def set_full_fs_access(value: bool) -> None:
    """放开整台机器（仅自用可信环境开启；默认 False）。"""
    global _FULL_FS_ACCESS
    _FULL_FS_ACCESS = bool(value)


def allowed_roots() -> List[Path]:
    return list(_ALLOWED_ROOTS)


def full_fs_access() -> bool:
    return _FULL_FS_ACCESS


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
    """判断路径是否位于"允许区"内。

    允许区 = 沙箱 ∪ 授权根目录 ∪（full_fs_access 时整台机器）。
    工具层的写操作用它做边界检查——Agent 可以在允许区内自由造东西，
    但想往允许区外写就必须走策略确认。
    """
    try:
        target = Path(path).resolve()
    except Exception:
        return False
    if _FULL_FS_ACCESS:
        return True
    try:
        target.relative_to(sandbox_dir().resolve())
        return True
    except ValueError:
        pass
    for root in _ALLOWED_ROOTS:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def resolve_path(path: str | Path) -> Path:
    """把用户给的路径规整成绝对路径（展开 ~、去符号链接、建父链）。

    相对路径解析到沙箱；绝对路径按原样解析并校验不逃逸允许区
    （逃逸且非 full_fs 时抛 ValueError，由调用方转成友好错误）。
    """
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        joined = safe_join_sandbox(str(p))
        if joined is None:
            raise ValueError(f"非法路径：{path}")
        return joined.resolve()
    resolved = p.resolve()
    if not within_sandbox(resolved):
        raise ValueError(
            f"路径越界：{resolved} 不在沙箱或授权目录内"
            "（用 grant_access 授权该目录，或开启 full_fs_access）")
    return resolved


def safe_join_sandbox(rel: str) -> Optional[Path]:
    """把相对路径安全地拼到沙箱下，拒绝 ``..`` 逃逸。返回 None 表示非法。"""
    if not rel:
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        return None
    return sandbox_dir() / rel
