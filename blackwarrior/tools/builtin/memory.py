"""记忆工具 —— 让模型能主动"记笔记"和"回想"。

多数 Agent 的记忆是隐式的（对话历史塞进上下文）。黑武士把记忆显式化：
模型可以主动决定"这件事值得记住"，也可以主动检索。

检索结果带 ``score / semantic / retention`` 三个分数——模型能看到
"我为什么想起这条"，而不是拿到一堆黑箱结果。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..registry import RISK_CAUTION, RISK_SAFE


def register(reg: Any, ctx: Any) -> None:
    def memory_write(title: str, brief: str = "", tags: str = "",
                     salience: int = 3, category: str = "日常") -> Dict[str, Any]:
        """写入一条长期记忆。

        参数:
            title: 记忆标题（简短）
            brief: 记忆内容
            tags: 标签，逗号分隔
            salience: 重要度 1-5，越高越不容易被遗忘
            category: 分类
        """
        tag_list = [t.strip() for t in str(tags or "").split(",") if t.strip()]
        try:
            rec = ctx.memory.remember(
                str(title), str(brief), tags=tag_list,
                salience=max(1, min(5, int(salience))),
                category=str(category or "日常"),
                source="agent",
            )
            return {"ok": True, "id": rec["id"], "title": title}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def memory_recall(query: str, k: int = 5) -> Dict[str, Any]:
        """检索长期记忆（语义 + 保留度 + 重要度融合）。

        参数:
            query: 检索词
            k: 返回条数
        """
        try:
            hits = ctx.memory.recall(str(query), k=max(1, int(k)))
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        items: List[Dict[str, Any]] = []
        for h in hits:
            items.append({
                "title": h.get("title", ""),
                "brief": h.get("brief", ""),
                "category": h.get("cat") or h.get("category", ""),
                "score": h.get("score"),
                "semantic": h.get("semantic"),
                "retention": h.get("retention"),
            })
        return {"ok": True, "query": query, "items": items, "count": len(items)}

    def memory_consolidate() -> Dict[str, Any]:
        """执行一次记忆巩固：把相似的零散记忆蒸馏成要点。"""
        try:
            rep = ctx.memory.consolidate(apply=True)
            return {"ok": True, **rep}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def memory_list(limit: int = 20) -> Dict[str, Any]:
        """列出最近的记忆条目。

        参数:
            limit: 条数
        """
        try:
            items = ctx.memory.list(limit=max(1, int(limit)))
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        slim = [{"id": i.get("id"), "title": i.get("title"),
                 "category": i.get("category"), "salience": i.get("salience"),
                 "hits": i.get("hits")} for i in items]
        return {"ok": True, "items": slim, "count": len(slim)}

    def memory_stats() -> Dict[str, Any]:
        """查看记忆系统状态（条数、档位、向量后端）。"""
        try:
            return {"ok": True, **ctx.memory.stats()}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    reg.register("memory_write", memory_write, risk=RISK_CAUTION,
                 category="memory",
                 description="写入一条长期记忆")
    reg.register("memory_recall", memory_recall, risk=RISK_SAFE,
                 category="memory",
                 description="检索长期记忆（语义+保留度+重要度融合）")
    reg.register("memory_consolidate", memory_consolidate, risk=RISK_CAUTION,
                 category="memory",
                 description="记忆巩固：相似记忆蒸馏成要点")
    reg.register("memory_list", memory_list, risk=RISK_SAFE,
                 category="memory", description="列出最近记忆")
    reg.register("memory_stats", memory_stats, risk=RISK_SAFE,
                 category="memory", description="记忆系统状态")
