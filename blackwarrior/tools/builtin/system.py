"""系统工具：提醒、心跳自调节、状态查询、环境感知。

``set_tick_interval`` 是个有意思的工具：**智能体可以自己改自己的心跳节奏**。
比如它判断"我在等一个结果"，就把心跳临时调到 10 秒（TTL 到期自动恢复）。
这是自主性在调度层的体现——节奏不再只是人配的常量。
"""

from __future__ import annotations

import platform
import time
from typing import Any, Dict, List

from ..registry import RISK_CAUTION, RISK_SAFE


def register(reg: Any, ctx: Any) -> None:
    def set_reminder(title: str, minutes: float = 5, body: str = "") -> Dict[str, Any]:
        """设置一个提醒。

        参数:
            title: 提醒标题
            minutes: 多少分钟后触发
            body: 提醒正文
        """
        try:
            due = time.time() + float(minutes) * 60.0
            rid = ctx.store.add_reminder(str(title), due, body=str(body))
            return {"ok": True, "id": rid, "due_at": due,
                    "in_minutes": round(float(minutes), 2)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def list_reminders() -> Dict[str, Any]:
        """列出待触发的提醒。"""
        try:
            items = ctx.store.list_reminders(status="pending", limit=20)
            return {"ok": True, "items": items, "count": len(items)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def set_tick_interval(seconds: float, ttl: int = 1, reason: str = "") -> Dict[str, Any]:
        """临时调整自己的心跳节奏。

        参数:
            seconds: 心跳间隔秒数（最小 2 秒）
            ttl: 生效轮数，用完自动恢复默认
            reason: 调整原因（会显示在状态里）
        """
        try:
            ctx.scheduler.policy.set(max(2.0, float(seconds)),
                                     ttl=max(1, int(ttl)), reason=str(reason))
            return {"ok": True, **ctx.scheduler.policy.status()}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def get_time() -> Dict[str, Any]:
        """获取当前时间（本地时区 + 时间戳）。"""
        return {
            "ok": True,
            "timestamp": time.time(),
            "local": time.strftime("%Y-%m-%d %H:%M:%S"),
            "weekday": time.strftime("%A"),
        }

    def get_status() -> Dict[str, Any]:
        """查看黑武士自身运行状态（循环、记忆、模型、工具）。"""
        try:
            return {"ok": True, **ctx.status()}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def environment() -> Dict[str, Any]:
        """查看运行环境（操作系统、Python 版本、数据目录、沙箱）。"""
        try:
            return {
                "ok": True,
                "os": f"{platform.system()} {platform.release()}",
                "machine": platform.machine(),
                "python": platform.python_version(),
                "data_root": str(ctx.paths.data_root()),
                "sandbox": str(ctx.paths.sandbox_dir()),
                "hostname": platform.node(),
            }
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def list_tools() -> Dict[str, Any]:
        """列出当前可用工具。"""
        try:
            items = ctx.tools.describe()
            return {"ok": True, "items": items, "count": len(items)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def remember_feedback(kind: str, action: str = "") -> Dict[str, Any]:
        """接收用户反馈（praise/scold/poke/hug），用于塑形行为倾向。

        参数:
            kind: praise / scold / poke / hug / ignore
            action: 被反馈的动作名
        """
        try:
            weights = ctx.kernel.feedback(str(kind), action=(action or None))
            return {"ok": True, "kind": kind, "weights": weights}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    reg.register("set_reminder", set_reminder, risk=RISK_CAUTION,
                 category="system", description="设置一个定时提醒")
    reg.register("list_reminders", list_reminders, risk=RISK_SAFE,
                 category="system", description="列出待触发提醒")
    reg.register("set_tick_interval", set_tick_interval, risk=RISK_SAFE,
                 category="system",
                 description="临时调整心跳节奏（带 TTL）")
    reg.register("get_time", get_time, risk=RISK_SAFE, category="system",
                 description="获取当前时间")
    reg.register("get_status", get_status, risk=RISK_SAFE, category="system",
                 description="查看运行状态")
    reg.register("environment", environment, risk=RISK_SAFE, category="system",
                 description="查看运行环境信息")
    reg.register("list_tools", list_tools, risk=RISK_SAFE, category="system",
                 description="列出可用工具")
    reg.register("remember_feedback", remember_feedback, risk=RISK_SAFE,
                 category="memory", description="接收反馈以塑形行为")
