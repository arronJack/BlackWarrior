"""工具执行策略 —— "该不该调"的判定层。

注册表只管"怎么调"，策略层管"能不能调"。分开的意义在于：
安全规则可以独立演进和测试，不必散落在每个工具的实现里。

规则来源（按优先级）::

    1. 全局开关      tools_enabled / shell_enabled / web_enabled
    2. 风险等级      danger 工具默认拒绝，需 allow_dangerous_tools
    3. 能力类别      未启用的类别整体拒绝
    4. 路径边界      写操作逃逸沙箱 → 拒绝
    5. 调用频率      同一工具短时间内高频调用 → 节流（防模型死循环）
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from .. import paths
from .registry import RISK_DANGER, RISK_SAFE, ToolSpec


class ToolPolicy:
    """工具执行策略。"""

    #: 同一工具的最小调用间隔（秒），防模型陷入"反复调同一个工具"的死循环
    MIN_INTERVAL: Dict[str, float] = {}

    def __init__(self, config: Any) -> None:
        self.config = config
        self._last_call: Dict[str, float] = {}
        self._denied: Dict[str, int] = {}

    def _get(self, key: str, default: Any) -> Any:
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def check(self, spec: ToolSpec, args: Optional[Dict[str, Any]] = None,
              *, confirm_danger: bool = False) -> Tuple[bool, str]:
        """返回 ``(是否允许, 原因)``。"""
        args = args or {}

        # 1) 全局开关
        if not self._get("tools_enabled", True):
            return False, "工具系统已关闭"

        # 2) 能力类别开关
        cat = spec.category
        if cat == "shell" and not self._get("shell_enabled", True):
            return False, "Shell 能力已关闭（设置 → 工具）"
        if cat == "web" and not self._get("web_enabled", True):
            return False, "联网能力已关闭（设置 → 工具）"

        # 3) 风险等级
        if spec.risk == RISK_DANGER:
            if not self._get("allow_dangerous_tools", False) and not confirm_danger:
                return False, (f"危险工具 {spec.name} 需要显式授权"
                               "（开启 allow_dangerous_tools 或逐次确认）")

        # 4) 路径边界：写类操作不允许逃逸"允许区"
        path_arg = args.get("path") or args.get("file") or args.get("target")
        if path_arg and cat in ("filesystem", "shell"):
            if spec.risk != RISK_SAFE and not paths.within_sandbox(path_arg):
                # 沙箱外 + 授权目录外 + 非 full_fs：必须走 danger 授权
                if not self._get("allow_dangerous_tools", False):
                    return False, (
                        f"拒绝写允许区外的路径：{path_arg}"
                        "（用 grant_access 授权该目录，或开启危险工具授权）")

        # 5) 频率节流
        min_interval = self.MIN_INTERVAL.get(spec.name, 0.0)
        if min_interval > 0:
            now = time.time()
            last = self._last_call.get(spec.name, 0.0)
            if now - last < min_interval:
                return False, f"调用过于频繁（{spec.name} 冷却 {min_interval}s）"

        return True, ""

    def record(self, name: str) -> None:
        """记录一次成功放行（用于节流计时）。"""
        self._last_call[name] = time.time()

    def deny(self, name: str) -> None:
        self._denied[name] = self._denied.get(name, 0) + 1

    def stats(self) -> Dict[str, Any]:
        return {
            "denied": dict(self._denied),
            "last_call": {k: round(time.time() - v, 1)
                          for k, v in self._last_call.items()},
        }
