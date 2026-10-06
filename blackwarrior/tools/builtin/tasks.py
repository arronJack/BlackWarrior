"""任务续跑工具（v0.6）—— 让模型能创建并推进多步骤任务。

## 为什么不给模型"自由推进任务"的权限

常见做法是给一个 ``task_advance(prompt)`` 之类的工具，让模型自己决定
下一步做什么。本模块刻意**不这么做**，而是把动作拆成几个语义明确的工具：

- :func:`task_create` 建任务时就要把步骤列清楚（人可审）；
- :func:`task_step_done` 只回答"这步做完了没有、结果是什么"；
- :func:`task_complete` **必须给 evidence** 才允许收尾。

理由：让模型"看着像做完了就宣布完成"是自欺欺人。拆开之后，
"完成"这件事有了外部约束和留痕，日后能审计。

## 风险级别

全部 ``safe`` —— 这些工具只改自己的任务表，不碰用户文件、不发网络请求。
唯一"有后果"的是 ``task_create`` 会让心跳加快（多步骤任务要推进），
但那是设计意图，不是副作用。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..registry import RISK_SAFE


def register(reg: Any, ctx: Any) -> List[str]:
    """注册任务相关工具。"""
    names: List[str] = []

    # ------------------------------------------------------------ 任务

    def task_create(title: str, steps: str, goal: str = "") -> dict:
        """创建一个多步骤任务（会被持久化，重启后可继续）。

        steps 用分号分隔，每步一个短句。例如
        ``task_create(title="整理下载目录", steps="扫描文件;按类型归档;生成索引")``

        建议在每步里写清"做完什么样算完成"，后续推进会以此为准。
        """
        raw = [s.strip() for s in str(steps or "").replace("\\n", ";").split(";")]
        raw = [s for s in raw if s]
        if not raw:
            return {"ok": False, "reason": "steps 不能为空（用分号分隔各步骤）"}
        return ctx.tasks.create(title, raw, goal=goal)

    reg.register("task_create", task_create, risk=RISK_SAFE, category="task",
                 description="创建多步骤任务（分号分隔步骤，持久化，重启可续）")
    names.append("task_create")

    def list_tasks(state: str = "", limit: int = 20) -> dict:
        """列出任务（可按状态过滤：active/paused/blocked/done）。"""
        items = ctx.tasks.list_tasks(state=state, limit=limit)
        return {"count": len(items), "items": items}

    reg.register("list_tasks", list_tasks, risk=RISK_SAFE, category="task",
                 description="列出任务及其进度与当前步骤")
    names.append("list_tasks")

    def task_current(task_id: int) -> dict:
        """查看某个任务当前该做什么（含这一步的完成判据）。"""
        return ctx.tasks.current_step(int(task_id))

    reg.register("task_current", task_current, risk=RISK_SAFE, category="task",
                 description="查看任务当前步骤、提示词与完成判据")
    names.append("task_current")

    def task_step_done(task_id: int, result: str = "") -> dict:
        """报告当前步骤已完成，并推进到下一步。

        result 请写实际做了什么（会留档）。所有步骤走完后，
        系统**不会**自动判定任务完成 —— 需要你核对后显式调task_complete。
        """
        return ctx.tasks.step_done(int(task_id), result=result)

    reg.register("task_step_done", task_step_done, risk=RISK_SAFE, category="task",
                 description="标记当前步骤完成并推进（留档结果，不自动收尾任务）")
    names.append("task_step_done")

    def task_step_failed(task_id: int, error: str = "") -> dict:
        """报告当前步骤失败。连续失败 3 次任务转 blocked，等人决策。"""
        if not str(error or "").strip():
            return {"ok": False, "reason": "请说明失败原因（error 不能为空）"}
        return ctx.tasks.step_failed(int(task_id), error=error)

    reg.register("task_step_failed", task_step_failed, risk=RISK_SAFE,
                 category="task",
                 description="报告步骤失败（连续 3 次转 blocked 等待决策）")
    names.append("task_step_failed")

    def task_complete(task_id: int, evidence: str) -> dict:
        """★ 完成任务。**必须给 evidence**（凭什么认为做完了）。

        不接受空证据：让"完成"有据可查，而不是模型自己说了算。
        """
        return ctx.tasks.complete(int(task_id), evidence=evidence)

    reg.register("task_complete", task_complete, risk=RISK_SAFE, category="task",
                 description="完成任务（必须提供 evidence 说明凭据）")
    names.append("task_complete")

    def task_resume(task_id: int) -> dict:
        """恢复一个暂停/受阻的任务。终态任务不可恢复。"""
        return ctx.tasks.resume(int(task_id))

    reg.register("task_resume", task_resume, risk=RISK_SAFE, category="task",
                 description="恢复暂停/受阻的任务（终态不可恢复）")
    names.append("task_resume")

    def task_skip_step(task_id: int, reason: str = "") -> dict:
        """跳过当前步骤（记录原因，游标前进）。"""
        return ctx.tasks.skip_step(int(task_id), reason=reason)

    reg.register("task_skip_step", task_skip_step, risk=RISK_SAFE,
                 category="task", description="跳过当前步骤并记录原因")
    names.append("task_skip_step")

    def task_abandon(task_id: int, reason: str = "") -> dict:
        """放弃任务（记录保留，可事后查）。"""
        return ctx.tasks.abandon(int(task_id), reason=reason)

    reg.register("task_abandon", task_abandon, risk=RISK_SAFE, category="task",
                 description="放弃任务（保留记录，不删除）")
    names.append("task_abandon")

    # ------------------------------------------------------------ 后台消息

    def push_background(text: str, source: str = "system") -> dict:
        """往主循环投一条后台消息（不打断用户对话）。

        用于"外部世界有事要告诉它"：定时检查结果、渠道消息、任务推进提示。
        后台消息优先级低于用户消息 —— 用户正在说话时会排队。
        """
        return ctx.push_background(text, source=source)

    reg.register("push_background", push_background, risk=RISK_SAFE,
                 category="task",
                 description="投一条后台消息进主循环（不打断用户对话）")
    names.append("push_background")

    return names


def selftest() -> bool:
    """工具层自检：真实注册 + 真库跑通一轮任务生命周期。"""
    import os
    import tempfile

    from ...config import Config
    from ...db.store import Store
    from ...runtime.tasks import TaskEngine
    from ..registry import ToolRegistry

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(f"  {'v' if cond else 'x'} {msg}")
        ok = ok and bool(cond)

    d = tempfile.mkdtemp(prefix="bw-tasktools-")
    store = Store(path=os.path.join(d, "t.db"))
    cfg = Config(path=os.path.join(d, "config.json"))

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.store = store
    ctx.config = cfg
    ctx.tasks = TaskEngine(store, cfg)

    pushed: list = []
    ctx.push_background = lambda text, source="system": (
        pushed.append((text, source)) or {"ok": True, "queued": True})

    reg = ToolRegistry()
    names = register(reg, ctx)
    want = ("task_create", "list_tasks", "task_current", "task_step_done",
            "task_step_failed", "task_complete", "task_resume",
            "task_skip_step", "task_abandon", "push_background")
    for w in want:
        check(w in names, f"工具 {w} 已注册")
    for spec in reg.specs():
        if spec.name in names:
            check(spec.risk == RISK_SAFE, f"{spec.name} 风险为 safe")

    # 走 registry 真调用一遍完整生命周期
    r = reg.call("task_create", {"title": "整理下载目录",
                             "steps": "扫描文件;按类型归档;生成索引"})
    check(r.ok and r.result.get("ok"), f"registry 建任务（{r.result}）")
    tid = int(r.result.get("task_id") or 0)

    r2 = reg.call("task_current", {"task_id": tid})
    check(r2.ok and r2.result.get("title") == "扫描文件",
          f"取当前步（{r2.result.get('title')}）")

    r3 = reg.call("task_step_done", {"task_id": tid, "result": "扫出 42 个"})
    check(r3.ok and r3.result.get("step_index") == 0, "step_done 推进")

    r4 = reg.call("task_step_failed", {"task_id": tid})
    check(r4.result.get("ok") is False, "step_failed 缺 error 被拒（不静默）")

    r5 = reg.call("task_complete", {"task_id": tid, "evidence": ""})
    check(r5.result.get("ok") is False, "★task_complete 无证据被拒")

    r6 = reg.call("list_tasks", {})
    check(r6.ok and r6.result.get("count", 0) >= 1, "list_tasks 能列出")

    r7 = reg.call("task_create", {"title": "", "steps": "a;b"})
    check((not r7.ok) or r7.result.get("ok") is False, "无标题被拒")

    r8 = reg.call("task_step_done", {"task_id": 99999})
    check((not r8.ok) or r8.result.get("ok") is False, "不存在的任务不崩")

    r9 = reg.call("push_background", {"text": "定时检查完成"})
    check(r9.ok and pushed and pushed[-1][0] == "定时检查完成",
          f"push_background 投递给 core（{pushed}）")

    store.close()
    return ok
