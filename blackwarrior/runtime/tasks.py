"""任务续跑引擎（v0.6）—— 让"多步骤目标"跨进程存活。

## 为什么需要

黑武士 v0.1 就有``tasks`` 表，但**只有骨架没有血肉**：db 层有 4 个方法，
运行时没有任何东西去驱动它，模型也看不到任务相关的工具。结果是
"用户交代一件要做的事→ 聊完就没了 → 下次重开还得从头说"。

而这恰恰是桌面 Agent 相对聊天窗口的核心价值：你说"把这批文件整理好"，
不该指望它一次对话做完。

## 设计要点

**1. 任务 = 有序步骤的集合，每步有自己的 prompt 模板。**

不是让模型自由发挥"推进任务"，而是给它一个**明确的当前步骤**和
完成判据。自由发挥的问题是它会声称做完了，实际上没做。

**2. 状态全部落库，进程重启后自动恢复。**

``state`` 取值：``active`` / ``paused`` / ``done`` / ``failed`` / ``abandoned``。
恢复策略：**上次退出时 ``active`` 的任务，重启后转 ``paused``**——
不自动接着跑，因为那可能在你不在的时候做错事。要继续由用户/模型显式恢复。
（这是"不猜用户意图"的体现。）

**3. ★ 绝不自动判定任务完成。**

每步做完只是``step_done``，任务是否完成必须由**模型显式调**
``task_complete``并给出证据（``evidence`` 字段）。理由：自检式判定
"我大概做完了"是自欺欺人。这条与白马的做法一致，但我在代码里写明原因。

**4. 有任务时心跳加快。**

通过 ``tick_scale_hint()`` 告知主循环把节奏调快一档，让任务在合理
时间内推进完，而不是等下一个空闲周期。

**5. 步骤失败不炸整个任务。**

一步失败记录 ``last_error`` 并停在原地，任务转 ``blocked``——
让人来决定是跳过、换方案还是放弃。自动重试会放大错误。
"""

from __future__ import annotations

import functools
import json
import threading
import time
from typing import Any, Dict, List, Optional

#: 任务状态机
ST_ACTIVE = "active"
ST_PAUSED = "paused"
ST_DONE = "done"
ST_FAILED = "failed"
ST_BLOCKED = "blocked"     # 某步反复失败，等人决策
ST_ABANDONED = "abandoned"

#: 步骤状态
SS_PENDING = "pending"
SS_RUNNING = "running"
SS_DONE = "done"
SS_FAILED = "failed"
SS_SKIPPED = "skipped"

#: 终态（不可再推进）
TERMINAL_TASK_STATES = (ST_DONE, ST_FAILED, ST_ABANDONED)

#: 有活跃任务时，心跳节奏缩放建议（越小于 1 = 越快）
TICK_SCALE_WITH_TASK = 0.75


def _now() -> float:
    return time.time()


def _serialized(fn):
    """给「读 payload → 改 → 写回」型方法串行化。

    本引擎被三方并发调用：HTTP 线程（/api/tasks/action）、主循环
    （心跳 to_prompt / tick_scale_hint）、工具线程（模型调 task_step_done）。
    store 层有锁能防数据库损坏，但**防不了两个线程读到同一份 payload
    各自改完互相覆盖**（丢更新）。所以用一把可重入锁把整个方法包起来。
    与 Pasm2Bridge 同一考量。
    """

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)

    return wrapper


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return "{}"


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return {} if default is None else default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {} if default is None else default


class TaskEngine:
    """任务续跑引擎。挂在 :class:`~blackwarrior.core.WarriorCore` 上。"""

    #: 单个任务最多多少步骤（防御模型写出无限步骤的清单）
    MAX_STEPS = 60
    #: 同一任务连续失败多少次转 blocked
    MAX_STEP_FAILURES = 3

    def __init__(self, store: Any, config: Any = None) -> None:
        self.store = store
        self.config = config
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 开关

    def _enabled(self) -> bool:
        if self.config is None:
            return True
        try:
            return bool(self.config.get("tasks_enabled", True))
        except Exception:
            return True

    # ------------------------------------------------------------------ 生命周期

    @_serialized
    def recover(self) -> Dict[str, Any]:
        """进程启动时调用：把上次遗留的 active 任务转为 paused。

        **不自动继续跑**。原因：进程可能在用户不知情时重启（比如崩溃后被
        守护拉起），此时自动推进多步骤任务可能做错事、产生副作用。
        宁可让用户说一句"继续那个任务"。
        """
        if not self._enabled():
            return {"recovered": 0, "note": "任务功能已关闭"}
        try:
            rows = self.store.query(
                "SELECT * FROM tasks WHERE state=?", (ST_ACTIVE,))
        except Exception:
            return {"recovered": 0}
        n = 0
        for row in rows:
            # 重启时正在跑的那一步也退回 pending，否则会卡在 running
            payload = self._payload(row)
            steps = payload.get("steps") or []
            for st in steps:
                if st.get("status") == SS_RUNNING:
                    st["status"] = SS_PENDING
            try:
                self.store.execute(
                    "UPDATE tasks SET state=?, payload=?, updated_at=?"
                    " WHERE id=?",
                    (ST_PAUSED, _dumps(payload), _now(), int(row["id"])))
                n += 1
            except Exception:
                continue
        if n:
            from ..events import emit

            emit("tasks_recovered", {"count": n})
        return {"recovered": n,
                "note": ("上次有 %d 个任务在运行，已转为暂停；"
                         "说「继续任务 N」即可恢复" % n) if n else ""}

    # ------------------------------------------------------------------ 创建

    @_serialized
    def create(self, title: str, steps: List[Any],
               goal: str = "", channel: str = "ui") -> Dict[str, Any]:
        """新建任务。``steps`` 可以是字符串列表或 {title, prompt, done_when} 列表。"""
        if not self._enabled():
            return {"ok": False, "reason": "任务功能已关闭（配置 tasks_enabled=false）"}
        title = str(title or "").strip()
        if not title:
            return {"ok": False, "reason": "缺少任务标题"}
        norm: List[Dict[str, Any]] = []
        for i, raw in enumerate(list(steps or [])[:self.MAX_STEPS]):
            if isinstance(raw, str):
                norm.append({"index": i, "title": raw[:120],
                             "prompt": raw, "done_when": "", "status": SS_PENDING,
                             "result": "", "attempts": 0, "last_error": ""})
            elif isinstance(raw, dict):
                t = str(raw.get("title") or raw.get("name") or "").strip()
                if not t:
                    continue
                norm.append({
                    "index": len(norm), "title": t[:120],
                    "prompt": str(raw.get("prompt") or t),
                    "done_when": str(raw.get("done_when") or "")[:300],
                    "status": SS_PENDING, "result": "",
                    "attempts": 0, "last_error": "",
                })
        if not norm:
            return {"ok": False, "reason": "任务至少需要一个步骤"}

        payload = {
            "steps": norm,
            "cursor": 0,
            "goal": str(goal or "")[:500],
            "channel": str(channel or "ui")[:20],
            "step_failures": 0,
        }
        tid = self.store.add_task(
            title, {**payload, "state": ST_ACTIVE})
        from ..events import emit

        emit("task_created", {"task_id": tid, "title": title,
                              "steps": len(norm)})
        return {"ok": True, "task_id": tid, "title": title,
                "steps": len(norm),
                "first_step": norm[0]["title"]}

    @_serialized
    def resume(self, task_id: int) -> Dict[str, Any]:
        """恢复一个暂停/阻塞的任务。"""
        row = self._row(task_id)
        if row is None:
            return {"ok": False, "reason": f"任务不存在：{task_id}"}
        state = str(row.get("state") or "")
        if state in TERMINAL_TASK_STATES:
            return {"ok": False,
                    "reason": f"任务已处于终态（{state}），无法恢复"}
        payload = self._payload(row)
        # 恢复时把失败的当前步重置为 pending，给它重来的机会
        for st in payload.get("steps") or []:
            if st.get("status") == SS_RUNNING:
                st["status"] = SS_PENDING
        # ★ 全部步骤都已执行完的，不要"恢复"成 active ——
        #   那样它会占着 active_task() 让心跳一直加快，却无步可做，
        #   用户看到"恢复成功"却什么都没发生（2026-10-06 复盘实测踩到）。
        #   这种任务只差一句 task_complete，应当明确提示而不是假装能继续。
        if self._current_of(payload) is None:
            return {"ok": False, "state": state,
                    "reason": "所有步骤已执行完毕，无需恢复 —— "
                              "请核对后调用 task_complete 收尾",
                    "needs": "task_complete"}
        self._save(task_id, state=ST_ACTIVE, payload=payload)
        from ..events import emit

        emit("task_resumed", {"task_id": int(task_id)})
        return {"ok": True, "task_id": int(task_id),
                "state": ST_ACTIVE, "current": self._current_of(payload)}

    @_serialized
    @_serialized
    def pause(self, task_id: int, reason: str = "") -> Dict[str, Any]:
        """主动暂停任务（可恢复）。

        为什么需要它：v0.6.0 初版只有 ``abandon``（终态、不可逆）而没有
        pause，用户想"先搁着明天再说"只能选"放弃"—— 而放弃是终态，
        误点就找不回来了。pause 补上了这个中间态。
        """
        row = self._row(task_id)
        if row is None:
            return {"ok": False, "reason": f"任务不存在：{task_id}"}
        state = str(row.get("state") or "")
        if state in TERMINAL_TASK_STATES:
            return {"ok": False, "reason": f"任务已终态（{state}），无法暂停"}
        payload = self._payload(row)
        if reason:
            payload.setdefault("pause_reason", str(reason)[:200])
        # 暂停时把正在跑的那一步退回 pending，恢复后能重新开始这一步
        for st in payload.get("steps") or []:
            if st.get("status") == SS_RUNNING:
                st["status"] = SS_PENDING
        self._save(task_id, state=ST_PAUSED, payload=payload)
        from ..events import emit

        emit("task_paused", {"task_id": int(task_id),
                             "reason": str(reason)[:120]})
        return {"ok": True, "task_id": int(task_id), "state": ST_PAUSED,
                "current": self._current_of(payload)}

    def abandon(self, task_id: int, reason: str = "") -> Dict[str, Any]:
        """放弃任务（不删记录，保留可查）。"""
        row = self._row(task_id)
        if row is None:
            return {"ok": False, "reason": f"任务不存在：{task_id}"}
        self._save(task_id, state=ST_ABANDONED,
                   payload=self._payload(row))
        from ..events import emit

        emit("task_abandoned", {"task_id": int(task_id),
                                "reason": str(reason)[:200]})
        return {"ok": True, "task_id": int(task_id), "state": ST_ABANDONED}

    @_serialized
    def complete(self, task_id: int, evidence: str = "") -> Dict[str, Any]:
        """★ 显式完成任务。**必须给证据**（为什么认为做完了）。

        这是刻意设计的：不允许运行时靠"看起来都处理过了"自动判定完成。
        自动判定 = 自欺欺人。证据会写进任务记录供日后审计。
        """
        row = self._row(task_id)
        if row is None:
            return {"ok": False, "reason": f"任务不存在：{task_id}"}
        ev = str(evidence or "").strip()
        if not ev:
            return {"ok": False,
                    "reason": "完成任务必须给 evidence（凭什么认为做完了？）",
                    "hint": "把你实际做了什么、结果是什么写进 evidence"}
        payload = self._payload(row)
        payload["evidence"] = ev[:800]
        payload["finished_at"] = _now()
        self._save(task_id, state=ST_DONE, payload=payload)
        from ..events import emit

        emit("task_done", {"task_id": int(task_id), "title": row.get("title")})
        return {"ok": True, "task_id": int(task_id), "state": ST_DONE}

    # ------------------------------------------------------------------ 推进

    def current_step(self, task_id: int) -> Dict[str, Any]:
        """取当前待办步骤（不改变状态）。"""
        row = self._row(task_id)
        if row is None:
            return {"error": f"任务不存在：{task_id}"}
        payload = self._payload(row)
        return self._current_of(payload) or {
            "error": "所有步骤都已处理，需要显式调用 task_complete 收尾"}

    @_serialized
    def step_done(self, task_id: int, result: str = "",
                  advance: bool = True) -> Dict[str, Any]:
        """标记当前步骤完成，游标前进到下一步。"""
        row = self._row(task_id)
        if row is None:
            return {"ok": False, "reason": f"任务不存在：{task_id}"}
        state = str(row.get("state") or "")
        if state in TERMINAL_TASK_STATES:
            return {"ok": False, "reason": f"任务已终态（{state}）"}
        payload = self._payload(row)
        steps = payload.get("steps") or []
        cur = int(payload.get("cursor") or 0)
        if cur >= len(steps):
            return {"ok": False, "reason": "已无当前步骤，请调用 task_complete"}
        if advance:
            steps[cur]["status"] = SS_DONE
            steps[cur]["result"] = str(result or "")[:600]
            payload["cursor"] = cur + 1
            payload["step_failures"] = 0
        self._save(task_id, state=state, payload=payload)
        nxt = self._current_of(payload)
        from ..events import emit

        emit("task_step_done", {
            "task_id": int(task_id), "step": cur,
            "title": steps[cur].get("title"), "next": nxt,
            "all_steps_done": nxt is None,
        })
        return {"ok": True, "task_id": int(task_id),
                "step_index": cur, "next": nxt,
                "note": ("所有步骤已执行完毕 —— 请核对后显式调用 "
                         "task_complete 收尾（系统不会替你判定完成）")
                if nxt is None else ""}

    @_serialized
    def step_failed(self, task_id: int, error: str = "") -> Dict[str, Any]:
        """当前步骤失败。连续失败达阈值 → 任务转blocked（等人工决策）。"""
        row = self._row(task_id)
        if row is None:
            return {"ok": False, "reason": f"任务不存在：{task_id}"}
        payload = self._payload(row)
        steps = payload.get("steps") or []
        cur = int(payload.get("cursor") or 0)
        if cur >= len(steps):
            return {"ok": False, "reason": "无当前步骤"}
        err = str(error or "").strip() or "未说明原因"
        steps[cur]["attempts"] = int(steps[cur].get("attempts") or 0) + 1
        steps[cur]["last_error"] = err[:400]
        fails = int(payload.get("step_failures") or 0) + 1
        payload["step_failures"] = fails
        state = str(row.get("state") or ST_ACTIVE)
        if fails >= self.MAX_STEP_FAILURES:
            state = ST_BLOCKED
        self._save(task_id, state=state, payload=payload)
        from ..events import emit

        emit("task_step_failed", {
            "task_id": int(task_id), "step": cur, "error": err[:200],
            "state": state, "failures": fails})
        return {"ok": True, "task_id": int(task_id), "state": state,
                "failures": fails,
                "note": (f"连续失败 {fails} 次，任务已转为 blocked。"
                         "请人工决定：修好再 resume、跳过该步、或放弃。")
                if state == ST_BLOCKED else
                f"已记录失败（第 {fails} 次），可修正后重试或 step_failed 再报"}

    @_serialized
    def skip_step(self, task_id: int, reason: str = "") -> Dict[str, Any]:
        """跳过当前步骤（记录原因，游标前进）。"""
        row = self._row(task_id)
        if row is None:
            return {"ok": False, "reason": f"任务不存在：{task_id}"}
        payload = self._payload(row)
        steps = payload.get("steps") or []
        cur = int(payload.get("cursor") or 0)
        if cur >= len(steps):
            return {"ok": False, "reason": "无当前步骤"}
        steps[cur]["status"] = SS_SKIPPED
        steps[cur]["last_error"] = str(reason or "")[:400]
        payload["cursor"] = cur + 1
        payload["step_failures"] = 0
        state = str(row.get("state") or ST_ACTIVE)
        if state == ST_BLOCKED:
            state = ST_ACTIVE
        self._save(task_id, state=state, payload=payload)
        return {"ok": True, "task_id": int(task_id), "step_index": cur,
                "next": self._current_of(payload)}

    # ------------------------------------------------------------------ 查询

    def list_tasks(self, state: str = "", limit: int = 30) -> List[Dict[str, Any]]:
        """列任务（含进度与当前步骤），用于 UI 与上下文注入。"""
        try:
            rows = self.store.query(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit or 30), 200)),))
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for r in rows:
            if state and str(r.get("state")) != state:
                continue
            payload = self._payload(r)
            steps = payload.get("steps") or []
            done = sum(1 for s in steps
                       if s.get("status") in (SS_DONE, SS_SKIPPED))
            out.append({
                "id": int(r.get("id") or 0),
                "title": r.get("title"),
                "state": r.get("state"),
                "total_steps": len(steps),
                "done_steps": done,
                "progress": round(done / len(steps), 3) if steps else 0.0,
                "current": self._current_of(payload),
                "updated_at": r.get("updated_at"),
            })
        return out

    def active_task(self) -> Optional[Dict[str, Any]]:
        """当前活跃任务（若有）。主循环用它决定是否加快节奏。"""
        try:
            rows = self.store.query(
                "SELECT * FROM tasks WHERE state IN (?, ?) "
                " ORDER BY updated_at DESC LIMIT 1", (ST_ACTIVE, ST_BLOCKED))
        except Exception:
            return None
        if not rows:
            return None
        payload = self._payload(rows[0])
        steps = payload.get("steps") or []
        done = sum(1 for s in steps if s.get("status") in (SS_DONE, SS_SKIPPED))
        return {
            "id": int(rows[0].get("id") or 0),
            "title": rows[0].get("title"),
            "state": rows[0].get("state"),
            "current": self._current_of(payload),
            "progress": round(done / len(steps), 3) if steps else 0.0,
        }

    def tick_scale_hint(self, base: float = 1.0) -> float:
        """有活跃任务时建议的心跳缩放系数（<1 = 更快）。"""
        if not self._enabled():
            return base
        t = self.active_task()
        if not t:
            return base
        try:
            return round(float(base) * TICK_SCALE_WITH_TASK, 3)
        except (TypeError, ValueError):
            return TICK_SCALE_WITH_TASK

    def to_prompt(self, limit: int = 3) -> str:
        """把任务状态注入上下文（让模型知道自己有未完成的事）。"""
        if not self._enabled():
            return ""
        try:
            tasks = [t for t in self.list_tasks(limit=20)
                     if t.get("state") in (ST_ACTIVE, ST_BLOCKED, ST_PAUSED)]
        except Exception:
            return ""
        if not tasks:
            return ""
        lines = ["[任务]"]
        for t in tasks[:limit]:
            mark = {"active": "进行中", "blocked": "受阻",
                    "paused": "已暂停"}.get(str(t.get("state")), "")
            cur = t.get("current") or {}
            cur_txt = (f"当前步：{cur.get('title')}" if cur.get("title")
                       else "所有步骤已执行，待收尾")
            lines.append(f"#{t['id']} {t.get('title')}（{mark} · "
                         f"{t.get('done_steps')}/{t.get('total_steps')}）"
                         f"{cur_txt}")
        return "｜".join(lines)

    # ------------------------------------------------------------------ 内部

    def _row(self, task_id: int) -> Optional[Dict[str, Any]]:
        try:
            return self.store.query_one("SELECT * FROM tasks WHERE id=?",
                                        (int(task_id),))
        except Exception:
            return None

    def _payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        p = _loads(row.get("payload"), {})
        if not isinstance(p, dict):
            p = {}
        p.setdefault("steps", [])
        p.setdefault("cursor", 0)
        return p

    def _save(self, task_id: int, *, state: str = "",
              payload: Optional[Dict[str, Any]] = None) -> None:
        if payload is not None:
            self.store.execute(
                "UPDATE tasks SET state=?, payload=?, updated_at=? WHERE id=?",
                (state or ST_ACTIVE, _dumps(payload), _now(), int(task_id)))
        else:
            self.store.execute(
                "UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                (state or ST_ACTIVE, _now(), int(task_id)))

    @staticmethod
    def _current_of(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        steps = payload.get("steps") or []
        cur = int(payload.get("cursor") or 0)
        if cur >= len(steps):
            return None
        s = steps[cur]
        return {"index": cur, "title": s.get("title"),
                "prompt": s.get("prompt"),
                "done_when": s.get("done_when"),
                "attempts": s.get("attempts", 0),
                "last_error": s.get("last_error", "")}


# ====================================================================== 自检


def selftest() -> bool:
    """纯内存自检：不依赖 db / 网络。"""
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(f"  {'v' if cond else 'x'} {msg}")
        ok = ok and bool(cond)

    # ★ 用**真实 sqlite**（临时文件）而不是内存桩。
    #   最初用内存 dict 桩，结果它的 query 分支匹配顺序与真实 SQL 不同，
    #   导致 selftest 假失败（_row 返回 None）——测试桩自身的偏差
    #   比产品 bug 更难识别。任务状态机重度依赖 SQL 语义
    #   （ORDER BY / LIMIT / WHERE state IN），就该用真引擎测。
    import os
    import tempfile as _tf

    from ..db.store import Store as _StoreCls

    _dir = _tf.mkdtemp(prefix="bw-tasks-")
    _store = _StoreCls(path=os.path.join(_dir, "t.db"))
    en = TaskEngine(_store, None)


    # 1) 创建
    r = en.create("整理资料", ["扫描下载目录", "按类型归档", "生成索引"])
    check(r.get("ok") and r.get("steps") == 3, f"创建 3 步任务（{r.get('steps')}）")
    tid = r["task_id"]

    # 2) 推进
    cur = en.current_step(tid)
    check(cur.get("index") == 0 and cur.get("title") == "扫描下载目录",
          f"当前步为第 0 步（{cur.get('title')}）")
    en.step_done(tid, "扫出 42 个文件")
    check(en.current_step(tid).get("index") == 1, "完成后前进到第 1 步")

    # 3) 失败与 blocked
    for i in range(en.MAX_STEP_FAILURES):
        en.step_failed(tid, f"第 {i + 1} 次失败")
    row = en._row(tid)
    check(row["state"] == ST_BLOCKED, f"连续失败后转 blocked（{row['state']}）")
    check(en.tick_scale_hint(1.0) < 1.0, "有任务时心跳加快（tick_scale<1）")

    # 4) 恢复
    en.resume(tid)
    check(en._row(tid)["state"] == ST_ACTIVE, "blocked 任务可恢复")

    # 5) 跳过
    en.skip_step(tid, "该目录不存在")
    check(en.current_step(tid).get("index") == 2, "跳过后前进")

    # 6) 完成必须给证据
    en.step_done(tid, "索引已生成")
    # ★ current_step 在"无当前步"时返回带 error 的**提示字典**而不是 None，
    #   这样模型/UI 能拿到可读原因（而不是一个光秃秃的 None）。
    done_cursor = en.current_step(tid)
    check("error" in (done_cursor or {}),
          "所有步骤执行完 → current_step 给出可读提示")
    bad = en.complete(tid, "")
    check(bad.get("ok") is False, "★无证据不允许标记完成")
    good = en.complete(tid, "42 个文件已归档并生成 index.md")
    check(good.get("ok") and en._row(tid)["state"] == ST_DONE,
          "带证据可完成")

    # 7) 重启恢复：active → paused（不自动跑）
    t2 = en.create("另一个任务", ["第一步", "第二步"])["task_id"]
    check(en._row(t2) is not None
          and en._row(t2)["state"] == ST_ACTIVE, "新任务为 active")
    rec = en.recover()
    check(rec.get("recovered", 0) >= 1, f"recover 识别出遗留任务（{rec}）")
    check(en._row(t2)["state"] == ST_PAUSED,
          "★重启后遗留 active 任务转 paused（不自动继续）")
    check("继续任务" in (rec.get("note") or ""), "恢复提示告知用户如何继续")

    # 7b) pause 可恢复（与 abandon 终态相反）
    p1 = en.create("可暂停任务", ["x", "y"])
    ptid = p1["task_id"]
    pr = en.pause(ptid, "先搁着")
    check(pr.get("ok") and en._row(ptid)["state"] == ST_PAUSED,
          "★pause 可暂停（abandon 是终态，pause 可回来）")
    check(en.resume(ptid).get("ok") is True, "paused 可恢复")
    en.abandon(ptid, "不做了")
    check(en.resume(ptid).get("ok") is False, "abandon 仍是终态不可恢复")

    # 8) 注入上下文
    en.resume(t2)
    p = en.to_prompt()
    check("任务" in p and str(t2) in p, f"任务状态可注入上下文（{p[:50]}…）")

    # 9) 终态不可恢复
    done_id = en.create("终态任务", ["x"])["task_id"]
    en.complete(done_id, "已完成")
    check(en.resume(done_id).get("ok") is False, "终态任务不可恢复")

    # 10) 边界
    check(en.create("空任务", []).get("ok") is False, "空步骤被拒绝")
    check(en.create("", ["x"]).get("ok") is False, "无标题被拒绝")
    check(en.step_done(99999).get("ok") is False, "不存在的任务不崩")
    many = en.create("超长", [f"步骤{i}" for i in range(200)])
    check(many.get("steps") == en.MAX_STEPS, "步骤数被上限截断")
    return ok
