"""预取缓存 —— 周期性信息预热，注入上下文。

周期性 URL 预取能让"有效期内"的信息自动注入上下文，适合天气、新闻、价格、榜单，
也能省掉逐轮重新查询的轮次成本。这里补上：

- 用户/工具可登记预取 URL 与有效期；
- 主循环心跳时刷新到期项（网络失败则保留旧值，不阻断）；
- 上下文装配把"有效期内"的内容预喂给模型，降低逐轮重新查询的轮次成本。
"""

from __future__ import annotations

import time
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_TTL = 3600.0


class PrefetchCache:
    """预取缓存门面。"""

    def __init__(self, config: Any, store: Any) -> None:
        self.config = config
        self.store = store

    # ------- 写 ---------------------------------------------------

    def add(self, url: str, *, content: Optional[str] = None,
            ttl: float = DEFAULT_TTL) -> Dict[str, Any]:
        """登记/更新一条预取。若未给 content，则立即抓取一次。"""
        url = str(url or "").strip()
        if not url:
            return {"ok": False, "error": "缺少 url"}
        if content is None:
            content = self._fetch(url)
        self.store.add_prefetch(url, content or "", ttl=float(ttl))
        return {"ok": True, "url": url, "ttl": float(ttl),
                "fetched": content is not None}

    def refresh_due(self) -> int:
        """心跳时刷新到期项。返回本次实际刷新的条数。"""
        if not bool(self.config.get("prefetch_enabled", False)):
            return 0
        rows = self.store.list_prefetch()
        now = time.time()
        refreshed = 0
        for r in rows:
            due = float(r.get("fetched_at", 0)) + float(r.get("ttl", DEFAULT_TTL))
            if due <= now:
                content = self._fetch(r["url"])
                if content is not None:
                    self.store.add_prefetch(r["url"], content,
                                            float(r.get("ttl", DEFAULT_TTL)))
                    refreshed += 1
        return refreshed

    # ------- 读 ---------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        return self.store.list_prefetch()

    def serve(self) -> List[Dict[str, Any]]:
        """返回有效期内内容（供上下文注入）。"""
        return self.store.serve_prefetch()

    def to_prompt(self) -> str:
        items = self.serve()
        if not items:
            return ""
        bits = ["[预取缓存] 以下信息已预先准备好："]
        for it in items:
            bits.append(f"- {it['url']}：{it['content'][:160]}")
        return "\n".join(bits)

    def clear(self) -> int:
        return self.store.purge_prefetch()

    # ------- 内部 -------------------------------------------------

    @staticmethod
    def _fetch(url: str) -> Optional[str]:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "BlackWarrior/0.2"})
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            # 只保留纯文本摘要，避免把整页 HTML 灌进上下文
            import re
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:600]
        except Exception:
            return None
