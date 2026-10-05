"""记忆服务 —— 认知内核与持久化副本之间的粘合层。

为什么要两份存储
----------------
``pasm-skills`` 的认知侧车（语义索引、复习计数、巩固归档）是为**检索质量**服务的；
SQLite 里的 ``memories`` 表是为**人**服务的——UI 要能浏览、编辑、删除、归档。

两者通过 ``memories.key`` 关联，服务层负责让它们保持一致：

- 写入时：先写内核（得到语义索引），再写 SQLite 副本（得到可浏览条目）；
- 召回时：内核出结果（带语义分/保留度/重要度），SQLite 负责累计命中数；
- 删除时：SQLite 标记归档，内核侧同步隐藏。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

from ..db.store import Store


class MemoryService:
    """记忆读写门面。"""

    def __init__(self, kernel: Any, store: Store) -> None:
        self.kernel = kernel
        self.store = store

    # ------- 写 ---------------------------------------------------

    def remember(self, title: str, brief: str = "", tags: Optional[Iterable[str]] = None,
                 *, salience: int = 1, category: str = "日常",
                 source: str = "chat") -> Dict[str, Any]:
        """写一条记忆（内核 + 副本）。"""
        tags = list(tags or [])
        self.kernel.observe(title, brief, tags=tags, salience=salience,
                            category=category, topic=title)
        mem_id = self.store.add_memory(
            title, brief, tags=tags, category=category,
            salience=salience, source=source,
        )
        return {"id": mem_id, "title": title, "brief": brief,
                "tags": tags, "salience": salience, "category": category}

    # ------- 读 ---------------------------------------------------

    def recall(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """认知召回。

        内核给出的结果带 ``score/semantic/retention`` 三个分数——
        这三个数是黑武士"为什么想起这件事"的解释，UI 会直接展示。
        内核不可用时退回 SQLite 字面检索。
        """
        hits = self.kernel.recall(query, k=k)
        if hits:
            self._bump_by_title(hits)
            return hits

        # 兜底：内核没命中，用 SQLite 字面检索（可能内核侧车还没同步完）
        rows = self.store.search_memories(query, limit=k)
        for r in rows:
            r.setdefault("score", 0.3)
            r.setdefault("semantic", 0.0)
            r.setdefault("retention", float(r.get("retention", 1.0)))
            try:
                self.store.bump_memory_hits(int(r["id"]))
            except Exception:
                pass
        return rows

    def _bump_by_title(self, hits: List[Dict[str, Any]]) -> None:
        """按标题把命中数同步回 SQLite（用于 UI 展示"被想起过几次"）。"""
        for h in hits:
            title = str(h.get("title") or "")
            if not title:
                continue
            row = self.store.query_one(
                "SELECT id FROM memories WHERE title=? AND archived=0"
                " ORDER BY ts DESC LIMIT 1", (title,))
            if row:
                try:
                    self.store.bump_memory_hits(int(row["id"]))
                except Exception:
                    pass

    def list(self, limit: int = 100, category: Optional[str] = None,
             include_archived: bool = False) -> List[Dict[str, Any]]:
        return self.store.list_memories(limit=limit, category=category,
                                        include_archived=include_archived)

    def get(self, mem_id: int) -> Optional[Dict[str, Any]]:
        return self.store.get_memory(mem_id)

    def update(self, mem_id: int, **fields: Any) -> bool:
        return self.store.update_memory(mem_id, **fields)

    def delete(self, mem_id: int) -> bool:
        return self.store.delete_memory(mem_id)

    def archive(self, mem_id: int) -> bool:
        return self.store.update_memory(mem_id, archived=1)

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self.store.search_memories(query, limit=limit)

    def clear(self) -> int:
        return self.store.clear_memories()

    # ------- 巩固 -------------------------------------------------

    def consolidate(self, apply: bool = True) -> Dict[str, Any]:
        """记忆巩固（睡眠回放）。

        把相似的零散记忆蒸馏成要点。这是黑武士"越用越聪明"的机制之一：
        20 条"用户提过 X"会被蒸馏成一条高重要度的结论。
        """
        return self.kernel.consolidate(apply=apply)

    # ------- 视图 -------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        snap = {}
        try:
            snap = self.kernel.snapshot() or {}
        except Exception:
            pass
        return {
            "count": self.store.count_memories(),
            "tier": snap.get("tier", "unknown"),
            "engine": snap.get("engine", "unknown"),
            "memory": snap.get("memory", {}),
            "semantic_backend": (snap.get("semantic") or {}).get("backend", ""),
        }

    def to_prompt(self, query: str, k: int = 5) -> str:
        """把召回结果渲染成可注入提示词的文本块。"""
        hits = self.recall(query, k=k)
        if not hits:
            return ""
        lines = []
        for h in hits:
            title = str(h.get("title") or "").strip()
            brief = str(h.get("brief") or "").strip()
            if not (title or brief):
                continue
            tag = f"[{h.get('category', '日常')}]" if h.get("category") else ""
            lines.append(f"- {tag}{title}：{brief}".rstrip("："))
        return "\n".join(lines)
