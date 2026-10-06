"""认知内核桥接层 —— 黑武士的"真脑子"。

这是黑武士与同类型桌面 Agent 最本质的差别。

多数桌面 Agent 的"智能"全部来自外部 LLM：
它们的自主行为 = 空闲时拿一句提示词去问模型，记忆 = SQLite 检索，
情绪 = 没有。脑子是**租来的**。

黑武士把 PASM 认知内核接进来，让下面这些能力**不依赖 LLM 也真实存在**：

- 语义记忆召回（中文同义扩展 + 可插拔向量后端）
- 艾宾浩斯遗忘曲线 + 复习效应（久未唤起的记忆自然降权）
- 睡眠式记忆巩固（相似记忆蒸馏成要点）
- 话题焦点栈（带衰减，解决长对话跑题）
- 情绪动力学（ valence 随事件演化，影响语气与行为倾向）
- 行为学习（反馈塑形动作偏好）

档位与降级原则
--------------
PASM 内核是**可选依赖**。装了就用真内核（``bionic``/``core``），
没装就用内置等价实现（``light``），并在 ``/status`` 里**明确标注档位**——
绝不假装自己用了真内核。这条与 PASM 基座的"永不隐藏降级"是同一条铁律。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .. import paths
from ..version import TIER_BIONIC, TIER_CORE, TIER_LIGHT, TIER_NONE

# ---------------------------------------------------------------- 档位探测

_PASM_IMPORT_ERROR = ""


def _try_import_pasm():
    """尝试导入 PASM 基座。返回 (BaseAgent, enhance) 或 (None, None)。"""
    global _PASM_IMPORT_ERROR
    try:
        from pasm_skills.sdk import BaseAgent  # type: ignore
        from pasm_skills.cognition import enhance  # type: ignore
        _PASM_IMPORT_ERROR = ""
        return BaseAgent, enhance
    except Exception as ex:  # pragma: no cover - 取决于环境
        _PASM_IMPORT_ERROR = f"{type(ex).__name__}: {ex}"
        return None, None


BaseAgent, enhance = _try_import_pasm()
HAS_PASM = BaseAgent is not None


def pasm_import_error() -> str:
    """PASM 导入失败的原因（排查用）。"""
    return _PASM_IMPORT_ERROR


# ---------------------------------------------------------------- 智能体本体

if HAS_PASM:

    class WarriorAgent(BaseAgent):  # type: ignore[misc]
        """黑武士的 PASM 智能体本体。

        继承 PASM 的 ``BaseAgent``：记忆层、学习层、情绪系统全部由基座提供。
        这里只负责黑武士特有的性格与渲染。
        """

        #: 黑武士的动作池（按成长阶段逐步解锁）
        STAGE_ACTIONS: "list[list[str]]" = [
            ["observe", "recall", "reply"],
            ["observe", "recall", "reply", "plan", "reflect"],
            ["observe", "recall", "reply", "plan", "reflect", "consolidate", "explore"],
        ]

        def action_pool(self) -> List[str]:
            stage = int(getattr(self.state, "growth_stage", 0) or 0)
            idx = min(stage, len(self.STAGE_ACTIONS) - 1)
            return list(self.STAGE_ACTIONS[idx])

        def _render_reply(self, text: str, facts: List[Dict[str, Any]],
                          mood: float) -> str:
            """无 LLM 时的模板渲染兜底。

            有 LLM 时走 :class:`~blackwarrior.llm.turn.TurnRunner`，
            这里保证**断网/无密钥也能给出有记忆、有性格的回复**——
            这是纯 LLM Agent 做不到的。
            """
            name = self.persona.get("name", "黑武士")
            if facts:
                top = facts[0]
                known = f"我记得{top.get('title', '')}：{top.get('brief', '')}。"
            else:
                known = "这件事我还没有印象。"

            if mood > 0.25:
                tone = "状态不错。"
            elif mood < -0.25:
                tone = "刚才那件事让我有点在意。"
            else:
                tone = "我在。"

            return f"{known}{tone}——{name}收到了：{text[:60]}"

        def bootstrap_event(self) -> List[Dict[str, Any]]:
            return [{
                "title": "黑武士已启动",
                "brief": "认知内核上线，开始持续运行。",
                "tags": ["系统", "启动"],
                "salience": 3,
                "category": "系统",
            }]

else:

    class WarriorAgent:  # type: ignore[no-redef]
        """PASM 缺失时的占位实现（接口与真本体一致）。"""

        STAGE_ACTIONS = [["observe", "recall", "reply"]]

        def __init__(self, agent_id: str, persona: Dict[str, Any],
                     persist_dir: Optional[str | Path] = None, **kw: Any) -> None:
            self.agent_id = agent_id
            self.persona = dict(persona or {})
            self.persist_dir = Path(persist_dir or (Path.home() / ".pasm-agents" / agent_id))
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self.state = type("S", (), {"growth_stage": 0,
                                        "total_interactions": 0,
                                        "mood": 0.0})()

        def action_pool(self) -> List[str]:
            return list(self.STAGE_ACTIONS[0])

        def _render_reply(self, text: str, facts: List[Dict[str, Any]],
                          mood: float) -> str:
            return f"黑武士收到了：{text[:60]}"


# ---------------------------------------------------------------- 内置降级内核

class FallbackCognition:
    """无 PASM 时的内置认知实现。

    实现与 PASM **同一套语义**（重要度淘汰、遗忘衰减、字面+字符级检索、
    焦点加权、巩固聚类），这样上层调用方感受不到档位差异。
    """

    CAP = 300

    def __init__(self, agent: Any, persist_dir: Optional[str | Path] = None,
                 **kw: Any) -> None:
        import json

        self.agent = agent
        self.persist_dir = Path(persist_dir or getattr(agent, "persist_dir", "."))
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.persist_dir / "fallback_episodes.json"
        self._focus_path = self.persist_dir / "fallback_focus.json"
        self._episodes: List[Dict[str, Any]] = self._load(self._path, [])
        self._focus: List[Dict[str, Any]] = self._load(self._focus_path, [])
        self._rehearsal: Dict[str, int] = {}
        self._json = json

    # -- 持久化 ----

    def _load(self, p: Path, default: Any) -> Any:
        if p.exists():
            try:
                return self._json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return default
        return default

    def _save(self) -> None:
        self._path.write_text(
            self._json.dumps(self._episodes, ensure_ascii=False), encoding="utf-8")
        self._focus_path.write_text(
            self._json.dumps(self._focus[-20:], ensure_ascii=False), encoding="utf-8")

    def save(self) -> Dict[str, str]:
        self._save()
        return {"episodes": str(self._path)}

    # -- 记忆 ----

    def observe(self, title: str, brief: str = "", tags: Optional[Iterable[str]] = None,
                *, salience: int = 1, category: str = "日常",
                topic: Optional[str] = None) -> None:
        self._episodes.insert(0, {
            "title": title, "brief": brief, "tags": list(tags or []),
            "cat": category, "sal": max(1, min(5, int(salience))),
            "ts": time.time(), "hits": 0,
        })
        if len(self._episodes) > self.CAP:
            order = sorted(range(len(self._episodes)),
                           key=lambda i: (self._episodes[i].get("sal", 1), -i))
            doomed = set(order[: len(self._episodes) - self.CAP])
            self._episodes[:] = [e for i, e in enumerate(self._episodes)
                                 if i not in doomed]
        if topic or title:
            self._focus.append({"topic": topic or title, "ts": time.time()})
            self._focus = self._focus[-20:]
        self._save()

    def recall(self, query: str, k: int = 5, **kw: Any) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        q_tokens = set(q)
        now = time.time()
        scored = []
        for e in self._episodes:
            blob = f"{e.get('title','')} {e.get('brief','')} {' '.join(e.get('tags', []))}"
            hit = sum(1 for t in q_tokens if t and t in blob)
            # 遗忘衰减：半衰期 2 天，重要度与复习次数延长半衰期
            age_days = max(0.0, (now - float(e.get("ts", now))) / 86400.0)
            sal = max(1, min(5, int(e.get("sal", 1))))
            hits = int(e.get("hits", 0))
            half_life = 2.0 * (1 + 0.15 * (sal - 1) + 0.1 * hits)
            retention = 0.5 ** (age_days / half_life)
            score = 0.62 * (hit / max(1, len(q_tokens))) \
                + 0.24 * retention + 0.14 * (sal / 5.0)
            if hit > 0 or score > 0.2:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for sc, e in scored[: max(1, k)]:
            e = dict(e)
            e["score"] = round(sc, 4)
            e["retention"] = round(0.5 ** (max(0.0, now - float(e.get("ts", now)))
                                           / 86400.0 / 2.0), 4)
            e.setdefault("_key", f"fb-{id(e)}")
            e["hits"] = int(e.get("hits", 0)) + 1
            out.append(e)
        # 复习效应写回
        for e in out:
            for orig in self._episodes:
                if orig.get("title") == e.get("title") and \
                        orig.get("brief") == e.get("brief"):
                    orig["hits"] = int(orig.get("hits", 0)) + 1
                    break
        return out

    def consolidate(self, apply: bool = False) -> Any:
        """内置的轻量级巩固：按标题聚类，合并重复项。"""
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for e in self._episodes:
            key = (e.get("title") or "").strip()
            groups.setdefault(key, []).append(e)
        dup = {k: v for k, v in groups.items() if len(v) > 1 and k}
        report = type("R", (), {"groups": len(dup), "merged": sum(len(v) for v in dup.values()),
                                "gists": [], "archived": []})()
        if apply and dup:
            for k, items in dup.items():
                items.sort(key=lambda x: x.get("ts", 0), reverse=True)
                for old in items[1:]:
                    try:
                        self._episodes.remove(old)
                    except ValueError:
                        pass
            self._save()
        return report

    def snapshot(self) -> Dict[str, Any]:
        return {
            "engine": "fallback",
            "memory": {"episodes": len(self._episodes)},
            "semantic": {"backend": "literal"},
        }

    # -- 焦点 ----

    @property
    def focus(self) -> "FallbackCognition":
        return self

    def push(self, topic: str, **kw: Any) -> None:
        self._focus.append({"topic": topic, "ts": time.time()})
        self._focus = self._focus[-20:]

    def peek(self) -> Optional[Dict[str, Any]]:
        return self._focus[-1] if self._focus else None

    def to_prompt(self) -> str:
        if not self._focus:
            return ""
        recent = self._focus[-5:]
        topics = "、".join(str(f.get("topic", "")) for f in recent)
        return f"当前焦点：{topics}"

    def current_entities(self) -> List[str]:
        out = []
        for f in self._focus[-5:]:
            t = str(f.get("topic", ""))
            if t:
                out.append(t)
        return out

    def load(self, *a: Any, **kw: Any) -> None:
        return None

    @property
    def index(self) -> "FallbackCognition":
        return self

    def __len__(self) -> int:
        return len(self._episodes)


# ---------------------------------------------------------------- 内核门面

class CognitiveKernel:
    """认知内核门面。

    上层（主循环 / 工具 / API）只跟它打交道，不关心背后是 PASM 真内核还是内置实现。
    """

    def __init__(self, config: Any = None, *, agent_id: str = "blackwarrior",
                 persist_dir: Optional[str | Path] = None,
                 enabled: bool = True) -> None:
        self.config = config
        self.agent_id = agent_id
        self.persist_dir = Path(persist_dir or paths.cognition_dir())
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.tier = TIER_NONE
        self.engine = "none"
        self.reason = ""
        self._hub: Any = None
        self._agent: Any = None

        if not enabled:
            self.tier = TIER_NONE
            self.reason = "cognition_enabled=False（配置关闭）"
            self._build_fallback()
            return

        self._build(agent_id)

    # ------- 构建 -------------------------------------------------

    def _build(self, agent_id: str) -> None:
        persona = self._persona()
        if HAS_PASM:
            try:
                agent = WarriorAgent(agent_id, persona, persist_dir=self.persist_dir)
                hub = enhance(agent, persist_dir=self.persist_dir)
                self._agent = agent
                self._hub = hub
                # BaseAgent 会在 tier 里写明 core/light；我们再加情绪维度判定 bionic
                base_tier = str(getattr(agent, "tier", TIER_LIGHT))
                self.tier = TIER_BIONIC if base_tier == TIER_BIONIC else (
                    TIER_CORE if base_tier == TIER_CORE else TIER_LIGHT)
                self.engine = "pasm"
                self.reason = ""
                return
            except Exception as ex:
                self.reason = f"PASM 装配失败，已降级：{type(ex).__name__}: {ex}"
        else:
            self.reason = f"未安装 PASM 基座（{pasm_import_error() or 'pasm-skills 不可导入'}）"
        self._build_fallback()

    def _build_fallback(self) -> None:
        if not self.persist_dir.exists():
            self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._agent = WarriorAgent(self.agent_id, self._persona(),
                                   persist_dir=self.persist_dir)
        self._hub = FallbackCognition(self._agent, persist_dir=self.persist_dir)
        if self.tier == TIER_NONE:
            self.tier = TIER_LIGHT
        # 注意：`self.engine or "builtin"` 是错的——初始值是字符串 "none"，
        # 非空字符串是真值，会被保留，导致档位永远显示 none。显式判定。
        if not self.engine or self.engine == "none":
            self.engine = "builtin"

    def _persona(self) -> Dict[str, Any]:
        if self.config is not None:
            try:
                p = self.config.persona
                if p:
                    return p
            except Exception:
                pass
        return {"name": "黑武士", "role": "持续运行的认知智能体",
                "temper": 0.62, "energy": 0.70, "play": 0.35}

    @property
    def agent(self) -> Any:
        """底层智能体本体。

        离线渲染（无 LLM 时用内核直接回复）需要拿到它。
        早期只暴露了 ``_agent``，外部拿不到就会静默退回更差的兜底文案——
        这类"看起来能用、其实没用上"的问题最难受，所以这里显式公开。
        """
        return self._agent

    @property
    def hub(self) -> Any:
        """认知中枢（PASM CognitionHub 或内置实现）。"""
        return self._hub

    # ------- 记忆 -------------------------------------------------

    def observe(self, title: str, brief: str = "", tags: Optional[Iterable[str]] = None,
                *, salience: int = 1, category: str = "日常",
                topic: Optional[str] = None) -> None:
        try:
            self._hub.observe(title, brief, tags=tags, salience=salience,
                              category=category, topic=topic)
        except Exception as ex:
            # 写记忆失败绝不能打断对话主流程
            from ..events import error
            error("cognition", f"observe 失败：{ex}")

    def recall(self, query: str, k: int = 5, **kw: Any) -> List[Dict[str, Any]]:
        try:
            return list(self._hub.recall(query, k=k, **kw) or [])
        except Exception:
            return []

    def consolidate(self, apply: bool = True) -> Dict[str, Any]:
        """记忆巩固。返回可读报告。"""
        try:
            rep = self._hub.consolidate(apply=apply)
            return {
                "groups": int(getattr(rep, "groups", 0) or 0),
                "merged": int(getattr(rep, "merged", 0) or 0),
                "archived": int(len(getattr(rep, "archived", []) or [])),
                "gists": list(getattr(rep, "gists", []) or [])[:5],
            }
        except Exception as ex:
            return {"groups": 0, "merged": 0, "archived": 0, "gists": [],
                    "error": str(ex)}

    # ------- 焦点 -------------------------------------------------

    def focus_push(self, topic: str, **kw: Any) -> None:
        try:
            self._hub.focus.push(topic, **kw)
        except Exception:
            try:
                self._hub.push(topic)
            except Exception:
                pass

    def focus_prompt(self) -> str:
        try:
            return str(self._hub.focus.to_prompt() or "")
        except Exception:
            return ""

    def focus_items(self) -> List[str]:
        try:
            ents = self._hub.focus.current_entities()
            return [str(e) for e in (ents or [])][-8:]
        except Exception:
            return []

    # ------- 情绪与行为 -------------------------------------------

    @property
    def mood(self) -> float:
        agent = self._agent
        try:
            m = getattr(agent, "mood", None)
            if isinstance(m, (int, float)):
                return float(m)
        except Exception:
            pass
        return 0.0

    def feel(self, event: str, valence: float) -> None:
        try:
            self._agent.feel(event, float(valence))
        except Exception:
            pass

    def feedback(self, kind: str, action: Optional[str] = None) -> Dict[str, float]:
        try:
            return dict(self._agent.feedback(kind, action=action) or {})
        except Exception:
            return {}

    def act(self) -> str:
        try:
            return str(self._agent.act() or "")
        except Exception:
            return ""

    # ------- 快照 -------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """认知快照：给 UI 的"大脑实时状态"。

        这是黑武士独有的面板——没有认知内核的 Agent 也就无从展示。
        """
        snap: Dict[str, Any] = {
            "tier": self.tier,
            "engine": self.engine,
            "reason": self.reason,
            "mood": round(self.mood, 3),
            "focus": self.focus_items(),
            "focus_prompt": self.focus_prompt(),
        }
        try:
            inner = self._hub.snapshot() if hasattr(self._hub, "snapshot") else {}
            if isinstance(inner, dict):
                snap.update(inner)
        except Exception:
            pass
        try:
            counts = self._agent.summary() if hasattr(self._agent, "summary") else {}
            if isinstance(counts, dict):
                snap["agent"] = counts
        except Exception:
            pass
        snap.setdefault("memory", {})
        return snap

    # ------- 持久化 -----------------------------------------------

    def save(self) -> None:
        try:
            self._hub.save()
        except Exception:
            pass
        try:
            if hasattr(self._agent, "save"):
                self._agent.save()
        except Exception:
            pass

    def close(self) -> None:
        self.save()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CognitiveKernel tier={self.tier} engine={self.engine}>"


def build_kernel(config: Any = None, **kw: Any) -> CognitiveKernel:
    """按配置构建内核。"""
    enabled = True
    if config is not None:
        try:
            enabled = bool(config.get("cognition_enabled", True))
        except Exception:
            enabled = True
    return CognitiveKernel(config, enabled=enabled, **kw)
