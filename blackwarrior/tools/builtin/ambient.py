"""环境感知与自发现工具（v0.3 对齐白马 AI 的 find_tool / 本机资源感知）。

两个能力：

1. ``find_tool`` —— **工具自发现**。工具一多，全量塞进提示词既费 token 又稀释
   注意力。白马 AI 用 find_tool 让模型自己按需检索工具；黑武士的做法一致，
   但多一层保障：检索命中与否都会写进行动日志，事后可审计"模型为什么没用那个工具"。

2. ``environment`` / ``system_probe`` —— **本机资源感知**。回答"还剩多少电、
   内存够不够、磁盘满了吗"这类问题不该靠模型瞎猜，也不该联网查——本机就有真数据。

另外补齐白马 AI 的两项记忆能力：

- **线索模型**（clue）：记忆之间建立显式线索，召回时能顺着线索捞到"相关但
  不含关键词"的记忆（纯 FTS5 抓不到"苹果"→"手机"这种关系）。
- **记忆审计**：谁在什么时候写了/改了什么，可回溯。v0.2 的行动日志只记工具调用，
  记忆写入一直没账本——出问题时无法追责。
"""

from __future__ import annotations

import platform
import shutil
import time
from typing import Any, Dict, List

from ..registry import RISK_SAFE


def register(reg: Any, ctx: Any) -> None:
    # ---------------------------------------------------------- 自发现

    def find_tool(query: str, limit: int = 5) -> Dict[str, Any]:
        """按关键词检索可用工具（工具太多时先查再用，省 token 也更准）。

        参数:
            query: 想找什么能力，如"删除文件""天气""发邮件"
            limit: 返回条数上限
        """
        q = str(query or "").strip().lower()
        if not q:
            return {"ok": False, "error": "缺少 query"}
        try:
            specs = ctx.tools.specs()
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        scored: List[tuple] = []
        for s in specs:
            name = str(getattr(s, "name", ""))
            desc = str(getattr(s, "description", "") or "")
            cat = str(getattr(s, "category", "") or "")
            blob = f"{name} {desc} {cat}".lower()
            # 名称命中权重最高，其次描述，最后类目
            score = 0.0
            if q in name.lower():
                score += 3.0
            for token in q.replace("，", " ").split():
                if not token:
                    continue
                if token in name.lower():
                    score += 2.0
                elif token in desc.lower():
                    score += 1.0
                elif token in cat.lower():
                    score += 0.5
            if score > 0:
                scored.append((score, name, desc, s.risk, cat))
        scored.sort(key=lambda x: -x[0])
        hits = [{"name": n, "description": d, "risk": r, "category": c}
                for _s, n, d, r, c in scored[: max(1, int(limit))]]
        try:
            ctx.store.log_action("find_tool", args={"query": query},
                                 ok=True, summary=f"命中 {len(hits)} 个工具",
                                 duration_ms=0)
        except Exception:
            pass
        return {"ok": True, "query": query, "items": hits, "count": len(hits),
                "searched": len(specs),
                "note": "" if hits else "没有匹配工具；如实汇报，不要臆造工具名"}

    # ---------------------------------------------------------- 本机资源

    def system_probe() -> Dict[str, Any]:
        """探测本机资源（CPU 核数 / 内存 / 磁盘 / 运行时长 / 电池）。

        这些是**本机真数据**，不需要联网也不该让模型猜。
        """
        out: Dict[str, Any] = {"ok": True, "os":
                               f"{platform.system()} {platform.release()}",
                               "machine": platform.machine(),
                               "python": platform.python_version(),
                               "cpu_count": None, "mem_total_mb": None,
                               "mem_available_mb": None, "disk_free_gb": None,
                               "battery": None}
        try:
            out["cpu_count"] = __import__("os").cpu_count()
        except Exception:
            pass
        try:
            import psutil  # type: ignore
            vm = psutil.virtual_memory()
            out["mem_total_mb"] = int(vm.total / 1048576)
            out["mem_available_mb"] = int(vm.available / 1048576)
            out["mem_percent"] = int(vm.percent)
            du = psutil.disk_usage(str(ctx.paths.data_root()))
            out["disk_free_gb"] = round(du.free / 1073741824, 2)
            out["disk_total_gb"] = round(du.total / 1073741824, 2)
            sb = getattr(psutil, "sensors_battery", None)
            if callable(sb):
                b = sb()
                if b is not None:
                    out["battery"] = {"percent": int(b.percent),
                                      "plugged": bool(b.power_plugged)}
        except Exception:
            # psutil 是可选依赖：没有就用标准库兜底，并如实标注来源
            try:
                usage = shutil.disk_usage(str(ctx.paths.data_root()))
                out["disk_free_gb"] = round(usage.free / 1073741824, 2)
                out["disk_total_gb"] = round(usage.total / 1073741824, 2)
                out["mem_source"] = "unavailable（未装 psutil）"
            except Exception:
                pass
        try:
            out["data_dir"] = str(ctx.paths.data_root())
            out["uptime"] = round(time.time() - ctx.started_at, 1)
            out["running"] = ctx.is_running()
        except Exception:
            pass
        return out

    # ---------------------------------------------------------- 线索与审计

    def link_clue(from_id: int, to_id: int, kind: str = "related") -> Dict[str, Any]:
        """在两条记忆之间建立一条线索（联想边）。

        用于表达"苹果→手机"这类**不含关键词**的关系。召回时顺着线索能捞到
        语义相关但字面不沾的记忆，补上纯 FTS5 的盲区。

        参数:
            from_id: 起点记忆 id
            to_id: 终点记忆 id
            kind: 关系类型，默认 related
        """
        try:
            a, b = int(from_id), int(to_id)
        except Exception:
            return {"ok": False, "error": "from_id / to_id 必须是整数"}
        if a <= 0 or b <= 0 or a == b:
            return {"ok": False, "error": "需要两个不同的正整数 id"}
        try:
            ok = ctx.store.add_clue(a, b, str(kind or "related"))
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        return {"ok": bool(ok), "from": a, "to": b, "kind": kind}

    def list_clues(mem_id: int) -> Dict[str, Any]:
        """列出某条记忆的全部线索（顺藤摸瓜用）。"""
        try:
            items = ctx.store.list_clues(int(mem_id))
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        return {"ok": True, "items": items, "count": len(items)}

    def memory_audit(limit: int = 50) -> Dict[str, Any]:
        """记忆审计账本：最近谁在什么时候写了/改了什么。"""
        try:
            items = ctx.store.memory_audit(limit=int(limit))
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        return {"ok": True, "items": items, "count": len(items)}

    reg.register("find_tool", find_tool, risk=RISK_SAFE, category="system",
                 description="按关键词自发现可用工具（工具多时先查再用）")
    reg.register("system_probe", system_probe, risk=RISK_SAFE, category="system",
                 description="探测本机资源（CPU/内存/磁盘/电池/运行时长）")
    reg.register("link_clue", link_clue, risk=RISK_SAFE, category="memory",
                 description="在两条记忆间建立联想线索")
    reg.register("list_clues", list_clues, risk=RISK_SAFE, category="memory",
                 description="列出某条记忆的联想线索")
    reg.register("memory_audit", memory_audit, risk=RISK_SAFE, category="memory",
                 description="记忆审计账本（谁在何时写了/改了什么）")
