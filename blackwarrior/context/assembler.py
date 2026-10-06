"""上下文组装（ACI：Agent-Context Injection）。

每轮对话前，把"这一刻该知道的东西"挑出来拼进提示词。
挑什么、挑多少，直接决定回答质量和 token 成本。

注入块（按需，缺哪块都不硬塞）::

    [身份]     人设与语气
    [时间]     当前时刻（让"明天""下周"能被正确理解）
    [认知]     情绪、焦点、预测误差 —— ★黑武士独有
    [记忆]     语义召回的长期记忆（带分数）
    [环境]     操作系统、数据目录、沙箱位置
    [近况]     最近几轮对话
    [工具]     本轮可用的工具（按需注入，不每次全给）

★ 与同类项目的差别：我们把**认知状态**也注入进去了。
模型能看到"我现在情绪偏负面""这个话题已经聊了三轮""最近环境很可预测"，
从而调整语气与主动性。纯 LLM Agent 没有这一层，只能靠对话历史猜。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

MAX_RECENT_TURNS = 8


class ContextAssembler:
    """上下文组装器。"""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # ------- 主入口 -----------------------------------------------

    def build(self, user_text: str, *, recall_k: Optional[int] = None,
              include_tools: bool = True,
              recent_turns: int = MAX_RECENT_TURNS) -> Dict[str, Any]:
        """组装一轮的完整上下文。"""
        ctx = self.ctx
        k = int(recall_k if recall_k is not None
                else ctx.config.get("recall_k", 6) or 6)

        blocks: List[str] = []
        injected: Dict[str, Any] = {}

        blocks.append(self._identity_block())
        blocks.append(self._time_block())

        cog = self._cognition_block()
        if cog["text"]:
            blocks.append(cog["text"])
            injected["cognition"] = cog["data"]

        mem = self._memory_block(user_text, k)
        if mem["text"]:
            blocks.append(mem["text"])
            injected["memory"] = mem["data"]

        # v0.2：用户画像（让回答贴合长期偏好）+ 预取缓存（环境预热）
        prof = self._profile_block()
        if prof:
            blocks.append(prof)
            injected["profile"] = True
        pref = self._prefetch_block()
        if pref:
            blocks.append(pref)
            injected["prefetch"] = True

        # v0.3：PASM V2 十九层认知信号（情绪/预测/门控/躯体标记）
        v2 = self._pasm2_block()
        if v2:
            blocks.append(v2)
            injected["pasm2"] = True

        env = self._env_block()
        if env:
            blocks.append(env)

        system_prompt = "\n\n".join(b for b in blocks if b)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

        recent = self._recent_messages(recent_turns)
        messages.extend(recent)
        injected["recent_turns"] = len(recent)

        tools = []
        if include_tools:
            tools = self._select_tools(user_text)
            injected["tools"] = [t["function"]["name"] for t in tools]

        return {
            "messages": messages,
            "tools": tools,
            "system_prompt": system_prompt,
            "injected": injected,
        }

    # ------- 各个注入块 -------------------------------------------

    def _identity_block(self) -> str:
        cfg = self.ctx.config
        persona = cfg.persona or {}
        name = cfg.agent_name
        role = persona.get("role", "持续运行的认知智能体")
        tone = persona.get("tone", "冷静、直接")
        return (
            f"你是「{name}」，一个持续运行的桌面认知智能体。\n"
            f"定位：{role}\n"
            f"语气：{tone}\n"
            "你拥有真实的长期记忆：会记住、会遗忘、会巩固，情绪会随事件演化。\n"
            "回答要具体、不说套话。不确定就说不确定，不要编造。"
        )

    def _time_block(self) -> str:
        now = time.time()
        return (f"[时间] {time.strftime('%Y-%m-%d %H:%M:%S %A', time.localtime(now))}"
                f"（时间戳 {now:.0f}）")

    def _cognition_block(self) -> Dict[str, Any]:
        """认知状态块 —— 黑武士独有。"""
        ctx = self.ctx
        data: Dict[str, Any] = {}
        try:
            mood = float(ctx.kernel.mood)
            data["mood"] = round(mood, 3)
        except Exception:
            mood = 0.0
            data["mood"] = 0.0

        try:
            focus = ctx.kernel.focus_items()
            data["focus"] = focus
        except Exception:
            focus = []

        try:
            pred = ctx.affect.predict_next()
            data["prediction"] = pred
        except Exception:
            pred = {}

        try:
            err = float(getattr(ctx.affect, "_last_error", 0.5))
            data["prediction_error"] = round(err, 3)
        except Exception:
            err = 0.5

        try:
            tier = ctx.kernel.tier
            data["tier"] = tier
        except Exception:
            tier = "unknown"

        lines = [f"[认知状态] 情绪 {mood:+.2f}（-1 消极 ~ +1 积极）"
                 f"｜预测误差 {err:.2f}｜内核档位 {tier}"]
        if focus:
            lines.append("当前焦点：" + "、".join(str(f) for f in focus[-5:]))
        if pred.get("ready"):
            lines.append(f"对下一轮的预期：置信度 {pred.get('confidence', 0):.2f}"
                         f"｜好奇心 {pred.get('curiosity', 0):.2f}")

        # 给模型的语气指引（情绪真的会影响表达）
        if mood > 0.3:
            lines.append("你现在状态不错，可以积极一点。")
        elif mood < -0.3:
            lines.append("你现在有些在意之前的事，语气可以沉一点，但不要迁怒用户。")

        return {"text": "\n".join(lines), "data": data}

    def _memory_block(self, query: str, k: int) -> Dict[str, Any]:
        ctx = self.ctx
        try:
            hits = ctx.memory.recall(query, k=k)
        except Exception:
            hits = []
        if not hits:
            return {"text": "", "data": {"items": []}}

        lines = ["[长期记忆] 以下是与当前话题相关的记忆（含重要度与想起强度）"]
        items = []
        for h in hits:
            title = str(h.get("title") or "").strip()
            brief = str(h.get("brief") or "").strip()
            if not title and not brief:
                continue
            cat = h.get("cat") or h.get("category") or ""
            ret = h.get("retention")
            score = h.get("score")
            meta = []
            if cat:
                meta.append(str(cat))
            if ret is not None:
                meta.append(f"保留 {float(ret):.2f}")
            if score is not None:
                meta.append(f"相关 {float(score):.2f}")
            suffix = f"（{'，'.join(meta)}）" if meta else ""
            lines.append(f"- {title}：{brief}{suffix}".rstrip("："))
            items.append({"title": title, "brief": brief, "score": score})
        if len(lines) == 1:
            return {"text": "", "data": {"items": []}}
        return {"text": "\n".join(lines), "data": {"items": items, "count": len(items)}}

    def _profile_block(self) -> str:
        """用户画像块（v0.2）。空画像安静返回，不硬塞。"""
        try:
            return self.ctx.profile.to_prompt()
        except Exception:
            return ""

    def _prefetch_block(self) -> str:
        """预取缓存块（v0.2）。环境预热内容注入，降低逐轮重查成本。"""
        try:
            return self.ctx.prefetch.to_prompt()
        except Exception:
            return ""

    def _pasm2_block(self) -> str:
        """PASM V2 认知块（v0.3）。

        这是"接入 PASM"在**对话质量**上的落点：模型能看到这一轮的世界模型
        预测是否落空、丘脑门控是否放行、躯体标记偏置——于是它能解释
        "我为什么觉得这个回答不对劲"，而不只是凭对话历史猜。
        """
        try:
            bridge = getattr(self.ctx, "pasm2", None)
            if bridge is None or not getattr(bridge, "available", False):
                return ""
            return bridge.to_prompt()
        except Exception:
            return ""

    def _env_block(self) -> str:
        try:
            import platform

            return (f"[环境] {platform.system()} {platform.release()}"
                    f"｜Python {platform.python_version()}"
                    f"｜沙箱 {self.ctx.paths.sandbox_dir()}")
        except Exception:
            return ""

    def _recent_messages(self, limit: int) -> List[Dict[str, Any]]:
        try:
            rows = self.ctx.store.recent_messages(limit=int(limit) * 2)
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for r in rows:
            role = r.get("role")
            content = r.get("content") or ""
            if not content:
                continue
            if role == "user":
                out.append({"role": "user", "content": content})
            elif role == "agent":
                out.append({"role": "assistant", "content": content})
            elif role == "tool":
                out.append({"role": "user", "content": f"[工具结果] {content}"})
        return out[-int(limit):]

    def _select_tools(self, user_text: str) -> List[Dict[str, Any]]:
        """按需选工具。

        启发式：按关键词命中类别。工具少的时候（<12）直接全给，
        因为筛选省下的 token 还不如误伤带来的轮次成本。
        """
        try:
            all_tools = self.ctx.tools.schemas()
        except Exception:
            return []
        if len(all_tools) <= 12:
            return all_tools

        text = (user_text or "").lower()
        wanted = set()
        rules = {
            "filesystem": ("文件", "读取", "写入", "目录", "保存", "file", "dir"),
            "shell": ("运行", "执行", "命令", "终端", "shell", "cmd", "run"),
            "web": ("搜索", "网页", "联网", "查一下", "新闻", "search", "http"),
            "memory": ("记得", "记住", "之前", "回忆", "忘了", "记忆"),
            "system": ("提醒", "状态", "时间", "几点", "remind"),
        }
        for cat, kws in rules.items():
            if any(k in text for k in kws):
                wanted.add(cat)
        if not wanted:
            wanted = {"memory", "system"}

        try:
            specs = self.ctx.tools.specs()
            names = {s.name for s in specs if s.category in wanted}
            return [s.schema() for s in specs if s.name in names]
        except Exception:
            return all_tools
