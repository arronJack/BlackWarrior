"""用户画像 / 信息面板 / 预取缓存 工具（v0.2 新增）。

让模型与用户都能读写结构化用户画像、查看环境面板、管理预取缓存。
这套工具是黑武士相对白马 AI 补齐的"更懂你"能力入口。

全部低风险（safe）：只读或只写本机结构化数据，不触网、不改文件系统。
"""

from __future__ import annotations

from typing import Any, Dict


def register(reg: Any, ctx: Any) -> None:
    from ..registry import RISK_SAFE

    def set_profile(aspect: str, value: str, evidence: str = "") -> Dict[str, Any]:
        """设置或纠正某维度用户画像。

        参数:
            aspect: 维度，可选 name/role/domain/expertise/projects/
                    preferences/communication_style/timezone
            value: 该维度的取值
            evidence: 这条画像的依据（哪句话 / 哪个事实），便于溯源与纠偏
        """
        try:
            ok = ctx.profile.set(
                aspect, value, evidence=evidence, confidence=0.9)
            if not ok:
                return {"ok": False, "error": f"未知画像维度：{aspect}"}
            return {"ok": True, "aspect": aspect, "value": value}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def get_profile() -> Dict[str, Any]:
        """查看已了解的用户画像（带置信度与依据）。"""
        try:
            data = ctx.profile.get() or {}
            return {"ok": True, "items": data, "count": len(data)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def get_panel(kind: str = "weather") -> Dict[str, Any]:
        """查看信息面板（weather/hotspot/person）。

        参数:
            kind: 面板类型 weather / hotspot / person
        """
        try:
            payload = ctx.panels.get(kind)
            payload.pop("ts", None)
            return {"ok": True, **payload}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def add_prefetch(url: str, ttl: float = 3600.0) -> Dict[str, Any]:
        """登记一个预取 URL，心跳时自动刷新并注入上下文。

        参数:
            url: 要预取的网址（如天气、新闻、价格页）
            ttl: 有效秒数，默认 3600
        """
        try:
            return {"ok": True, **ctx.prefetch.add(url, ttl=float(ttl))}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def list_prefetch() -> Dict[str, Any]:
        """列出当前预取缓存。"""
        try:
            items = ctx.prefetch.list()
            return {"ok": True, "items": items, "count": len(items)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    reg.register("set_profile", set_profile, risk=RISK_SAFE,
                 category="system", description="设置/纠正用户画像维度")
    reg.register("get_profile", get_profile, risk=RISK_SAFE,
                 category="system", description="查看已了解的用户画像")
    reg.register("get_panel", get_panel, risk=RISK_SAFE,
                 category="system", description="查看信息面板（天气/热点/人物）")
    reg.register("add_prefetch", add_prefetch, risk=RISK_SAFE,
                 category="system", description="登记预取 URL 周期刷新")
    reg.register("list_prefetch", list_prefetch, risk=RISK_SAFE,
                 category="system", description="列出预取缓存")
