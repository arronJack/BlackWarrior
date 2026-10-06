"""PASM V2.0 接入层 —— 把十九层认知底座接进黑武士。

为什么是 V2 而不是 V1
--------------------
黑武士 v0.1/v0.2 接的是 PASM **V1**（``pasm_skills.BaseAgent``），只用到了
6 项能力：observe / recall / consolidate / focus / mood / feedback。
说白了，V1 在黑武士里只承担了"记忆 + 一点情绪"。

V1 的能力边界（这也是"为什么要接 PASM"这个问题在 v0.2 仍答不好的原因）：

    记忆检索 · 遗忘衰减 · 记忆巩固 · 焦点栈 · 情绪效价 · 反馈塑形

V2（``pasm2``，独立包，与 ``pasm/`` 平行不侵入）把这件事从"一个认知引擎"
升级为"**十九层认知底座 + 安全层 + 成长闭环**"：

| 层 | 模块 | V1 | V2 带来的东西 |
|---|---|---|---|
| 层 -2 | safety | ✗ | 边界约束 / 欺骗检测 / 伦理审查 / 资源上限 / 提示注入筛查 |
| 层 -1 | brainstem | ✗ | 多巴胺·血清素·去甲肾上腺素·乙酰胆碱四通道调质 |
| 层 0.5 | thalamus | ✗ | 注意力门控 + 新颖度直通（防漏新事物） |
| 层 1 | working_memory | ✗ | 前额叶-顶叶工作记忆（容量 7±2）+ 前瞻意图 |
| 层 2 | memory | 部分 | 情绪记忆 / 模式分离 / 模式完成 / 再巩固 / 自传体 |
| 层 2.5 | cerebellum | ✗ | 转移间隔 EMA + 前馈补偿 + 时序惊讶告警 |
| 层 3 | world_model | ✗ | 上下文预测器 + **反事实推理**（``what_if``） |
| 层 4 | emotion | 部分 | 显著性 + 边缘系统价值 + 情绪记忆绑定 |
| 层 4.5 | insula | ✗ | 内感受 → 躯体标记（"gut feeling" 偏置） |
| 层 5 | personality | ✗ | 慢时间尺度人格特质 EMA（探索/稳定/敏感/可塑） |
| 层 6 | metacognition | ✗ | **ACC 元认知**：冲突/错误监测 + 双系统快慢门控 |
| 层 6.5 | dmn | ✗ | DMN 自发思考 + 小脑 + 突显网络 |
| 层 7 | symbol* | ✗ | 真符号注册表 + 符号化梯度 + **三角校验**（反幻觉机制） |
| 层 7.5 | symbol_mapping | ✗ | SLH 单向桥 + 内生↔外部符号多对多映射 + 四路路由 |
| — | growth | ✗ | **成长闭环**：失衡告警 → 参数建议 → 灰度应用 |
| — | evalkit | ✗ | 成长日记 GrowthTrace + 十四项失衡监测 |

一句话回答"为什么接 PASM"：

    纯 LLM Agent 的智能 100% 租自模型——断网即哑火，且它不知道自己会错。
    PASM V2 给出的是**本地就成立**的认知器官：能记住、能遗忘、能巩固、
    能预判、能反事实推演、**能意识到自己出错了**（ACC）、能在输出前做
    四层校验（三角），还会在长期运行中**自己改进自己的参数**（成长闭环）。

降级原则（黑武士铁律，永不隐藏）
--------------------------------
``pasm2`` 需要 ``numpy``，且当前是 ``2.0.0a11`` 预览版。因此：

    numpy+pasm2 可用 → 档位 ``pasm2``（真底座）
    否则             → 档位 ``pasm1``（V1 引擎）或 ``builtin``（内置降级）

任何一档缺失都在 ``/status`` 与 UI「心智」页**明确标注原因**，
不把降级伪装成正常——这条与"永不隐藏降级"是同一条铁律。
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

#: 十九层仿生架构（与 pasm2.config.NINETEEN_LAYERS 对齐，用于 UI 展示）。
#: 每项 = (层号, 名称, 职责, V2 模块键)
LAYERS: Tuple[Tuple[str, str, str, str], ...] = (
    ("层 -2", "安全层", "价值锚定·边界约束·欺骗检测·人类优先", "safety"),
    ("层 -1", "脑干调质", "觉醒·疲劳·四通道调质", "brainstem"),
    ("层 0", "感知编码", "感觉皮层（连续潜态）", "perception"),
    ("层 0.5", "丘脑中继", "门控·筛选·新颖直通", "thalamus"),
    ("层 1", "工作记忆", "前额叶-顶叶（容量 7±2）", "working_memory"),
    ("层 2", "记忆体", "模式分离·完成·巩固·再巩固", "memory"),
    ("层 2.5", "小脑", "时序·协调·误差微调", "cerebellum"),
    ("层 3", "世界模型", "预测器·想象引擎·反事实", "world_model"),
    ("层 4", "情绪", "杏仁核显著性·边缘系统价值", "emotion"),
    ("层 4.5", "岛叶", "内感受·躯体标记", "insula"),
    ("层 5", "性格", "慢时间尺度特质", "personality"),
    ("层 6", "元认知", "ACC 冲突/错误·双系统门控", "metacognition"),
    ("层 6.5", "网络动态", "DMN·突显·中央执行", "network"),
    ("层 7", "符号层", "真符号注册·规则·三角校验", "symbol"),
    ("层 7.5", "符号映射", "内生↔外部符号·四路路由", "symbol_mapping"),
    ("层 8", "执行输出", "动作选择·工具调用", "output"),
    ("规划器", "CEM+MPC", "量子退火决策", "planner"),
    ("认知皮层", "对外执行", "经安全层的工具与输出校验", "cortex"),
    ("narrator", "语言输出", "经 LLM 桥的语言生成", "narrator"),
)

#: 象量维度（与 pasm2 的 entity_latent_dim=8 对齐）
Z_DIM = 8


def _jsonable(v: Any) -> Any:
    """把 dataclass / 复杂对象压成可 JSON 序列化的结构（深度 2，够用即可）。"""
    import dataclasses as _dc

    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if _dc.is_dataclass(v) and not isinstance(v, type):
        return {k: _jsonable(val) for k, val in _dc.asdict(v).items()}
    if isinstance(v, dict):
        return {str(k): _jsonable(val) for k, val in list(v.items())[:30]}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in list(v)[:30]]
    return str(v)[:200]


def _default_embedder() -> Any:
    """按可用性选语义嵌入后端（hash / lsa / onnx）。任何环境都不抛异常。"""
    try:
        from .embedding import build_embedder
        return build_embedder("auto")
    except Exception:
        return None


def num(v: Any) -> Optional[float]:
    """安全转 float：拿不到就返回 None（UI 显示为「—」），不编造 0。"""
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def probe() -> Dict[str, Any]:
    """探测 PASM V2 可用性。**只做探测，不导入重模块**（导入开销留给调用方）。

    返回 ``{available, reason, numpy, pasm2, version, stage, api, profiles}``。
    """
    out: Dict[str, Any] = {
        "available": False, "reason": "", "numpy": False, "pasm2": False,
        "version": "", "stage": "", "api": "", "profiles": [],
    }
    try:
        import numpy  # noqa: F401
        out["numpy"] = True
    except Exception as ex:
        out["reason"] = f"未安装 numpy（PASM V2 需要它做连续潜态运算）：{type(ex).__name__}"
        return out
    try:
        import pasm2
        out["pasm2"] = True
        out["version"] = str(getattr(pasm2, "__version__", "") or "")
        out["stage"] = str(getattr(pasm2, "__stage__", "") or "")
        out["api"] = str(getattr(pasm2, "__api_version__", "") or "")
    except Exception as ex:
        out["reason"] = (
            "未安装 pasm2（V2 底座；装法：pip install pasm2，"
            "只需 numpy 不拖 torch）"
            f"：{type(ex).__name__}: {ex}")
        return out
    try:
        from pasm2.skills.kit import PROFILES
        out["profiles"] = sorted(PROFILES)
    except Exception as ex:
        out["reason"] = f"pasm2 存在但 CognitiveKit 不可用：{type(ex).__name__}: {ex}"
        return out
    out["available"] = True
    out["reason"] = f"PASM V2 {out['version']}（{out['stage']}，api {out['api']}）就绪"
    return out


class Pasm2Bridge:
    """PASM V2 门面。

    对上层只暴露四个动作 + 三个只读快照，且**全部可降级**：
    V2 不可用时 :meth:`available` 为 False，:meth:`reason` 给出原因，
    所有方法返回空/None 而**不抛异常**——绝不因为 V2 是预览版就让整个 Agent 挂掉。
    """

    def __init__(self, profile: str = "full", *, data_root: Optional[str] = None,
                 tool_allowlist: Optional[List[str]] = None,
                 embedder: Any = None) -> None:
        self._probe = probe()
        self.profile = profile
        self.reason = self._probe["reason"]
        self._kit: Any = None
        self._agent: Any = None
        self._step = 0
        self._last_digest: Dict[str, Any] = {}
        self._last_verify: Dict[str, Any] = {}
        self._alerts: List[Dict[str, Any]] = []
        self._growth: Dict[str, Any] = {}
        # v0.4：语义嵌入后端（hash / lsa / onnx）。没有它就用纯标准库哈希兜底，
        # 语义检索相应降级——但状态里一定写明当前是哪一档。
        self._embedder = embedder if embedder is not None else _default_embedder()
        try:
            self._semantic = bool(getattr(self._embedder, "semantic", False))
        except Exception:
            self._embedder = None
            self._semantic = False
        self._data_root = data_root
        self._started_at = time.time()
        # 主循环调 observe()、HTTP 线程调 status()、工具线程调 verify_tool()
        # —— 同一桥接层被多线程共用，用一把可重入锁把状态读改写包起来，
        #    避免 _errors 追加丢失、digest 读到半更新值。
        self._lock = threading.RLock()
        # 安全层白名单：必须在构造时就给，否则 BoundaryGuard 建成"全拒"
        self._allowlist: List[str] = [str(n) for n in (tool_allowlist or []) if str(n).strip()]
        if self._probe["available"]:
            self._boot()

    # ------- 引导 -------------------------------------------------

    def _boot(self) -> None:
        try:
            from pasm2.skills.kit import PROFILES, CognitiveKit
            from pasm2.agent import CognitiveAgent
            from pasm2.config import PASM2Config
        except Exception as ex:
            self.reason = f"V2 组件导入失败，已降级：{type(ex).__name__}: {ex}"
            self._probe["available"] = False
            return
        # profile 名非法时回落到 standard，不让配置写错就崩
        profile = self.profile if self.profile in self._probe.get("profiles") else "standard"
        self.profile = profile
        flags = dict(PROFILES.get(profile) or {})

        # 组装基础配置。**必须在这里就把安全层打开并喂白名单**：
        #   · safety_boundary_lock=False → SafetyLayer.guard 压根不会被构造（None）
        #   · boundary_tool_allowlist=() → BoundaryGuard 建成也是"全拒"
        # 两者任一没配好，verify_tool 会把**正常工具也拦掉**（集成最易踩的坑）。
        from dataclasses import replace

        base = replace(
            PASM2Config(),
            safety_boundary_lock=bool(self._allowlist),
            boundary_tool_allowlist=tuple(self._allowlist),
        )
        try:
            self._kit = CognitiveKit(profile=profile, config=base)
        except TypeError:
            # 旧版 kit 不接受 config 参数，退回只传 profile
            self._kit = CognitiveKit(profile=profile)
        except Exception as ex:
            self.reason = f"CognitiveKit({profile}) 构建失败，已降级：{type(ex).__name__}: {ex}"
            self._probe["available"] = False
            return
        # 闭环认知体（阶段 10）：感知→决策→行动→回写 + 成长回路。
        # 复用同一 profile 开关，否则 what_if/growth_review 返回 *_disabled。
        try:
            cfg = replace(base, use_cognitive_kit=True, **flags)
            self._agent = CognitiveAgent(cfg)
        except Exception:
            self._agent = None      # 非必需，缺失只少一路能力

    @property
    def available(self) -> bool:
        return bool(self._probe.get("available")) and self._kit is not None

    # ------- 象量编码 ---------------------------------------------

    def encode(self, text: str) -> List[float]:
        """文本 → 8 维连续潜态 z（喂给 PASM V2 的象量）。

        v0.4 起走**语义嵌入后端**（LSA/ONNX），只有它们都不可用时才退回
        纯标准库的 sha256 哈希。哈希保证"同一句话→同一个象量"的可复现性，
        但**不保证语义相近的句子靠近**——所以用哈希时 :attr:`_semantic`
        恒为 False，UI 与 `/status` 都会如实标注，不会假装有语义。
        """
        if self._embedder is not None:
            try:
                # 嵌入后端内部维度更高（64），这里只取 PASM 要的前 8 维
                fn = getattr(self._embedder, "encode_pasm", None)
                z = list(fn(text) if callable(fn) else self._embedder.encode(text))
                if len(z) >= Z_DIM:
                    return [float(v) for v in z[:Z_DIM]]
            except Exception:
                pass
        return self._hash_encode(text)

    def learn(self, text: str) -> None:
        """喂一篇语料给嵌入后端（让它逐步学到本地语义）。"""
        fn = getattr(self._embedder, "feed", None)
        if not callable(fn):
            return
        try:
            fn(str(text or ""))
        except Exception:
            pass

    @staticmethod
    def _hash_encode(text: str) -> List[float]:
        """sha256 确定性哈希（纯标准库，任何环境都能跑）。"""
        raw = str(text or "").strip().encode("utf-8")
        digest = hashlib.sha256(raw).digest()
        z = [(int.from_bytes(digest[i * 2:i * 2 + 2], "big") / 32767.5) - 1.0
             for i in range(Z_DIM)]
        norm = math.sqrt(sum(v * v for v in z))
        return [v / norm for v in z] if norm > 1e-9 else z

    def embedding_status(self) -> Dict[str, Any]:
        """嵌入后端状态（供 UI 与 API 展示，必须能看出是语义还是哈希）。"""
        if self._embedder is None:
            return {"backend": "hash", "semantic": False,
                    "reason": "无可用嵌入后端，退回 sha256 哈希"}
        try:
            st = dict(self._embedder.status())
        except Exception as ex:
            return {"backend": "hash", "semantic": False,
                    "reason": f"嵌入后端状态读取失败：{type(ex).__name__}"}
        st.setdefault("semantic", bool(getattr(self._embedder, "semantic", False)))
        st["pasm_dim"] = Z_DIM
        return st

    # ------- 四个动作 ---------------------------------------------

    def observe(self, text: str, *, rpe: float = 0.0,
                interoception: Optional[Dict[str, float]] = None,
                anomaly: Optional[bool] = None) -> Dict[str, Any]:
        """感知一步：把这一轮对话喂给 V2，返回 digest。

        digest 的字段随开关变化（未开启的模块字段为 None）——
        这是 pasm2 的诚实约定，本层原样透传，不补齐、不伪造。
        """
        # 持续喂语料：嵌入后端从黑武士见过的文本里学本地语义
        self.learn(text)
        if not self.available:
            return {}
        try:
            z = self.encode(text)
            d = self._kit.observe(z, rpe=rpe,
                                  interoception=interoception,
                                  anomaly=anomaly)
            with self._lock:
                self._step = int(d.get("step") or self._step + 1)
                self._last_digest = dict(d or {})
            return dict(d or {})
        except Exception as ex:
            self._note_error("observe", ex)
            return {}

    def tell(self, reference: str) -> Dict[str, Any]:
        """注入外部指称（人话里的词）→ 经 SLH 桥挂到当前象量。

        这是 V2 独有的"符号化"入口：让黑武士能把"黑武士""PASM""融合"这些
        **语言标签**锚定到连续潜态上，从而支持"这个词和那个概念是同一件事"。
        """
        if not self.available:
            return {}
        try:
            return dict(self._kit.tell(str(reference or "")) or {})
        except Exception as ex:
            self._note_error("tell", ex)
            return {}

    def verify(self, proposal: Dict[str, Any],
               facts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """底层：把提案交给 V2 三角校验（预测 / 统计 / 逻辑 / 安全 四层）。

        ⚠ 提案字段决定校验哪些层——pasm2 的约定是**只校验提案里出现的字段**：
        ``next_entity``（预测/统计层）、``conclusion``（逻辑层）、
        ``tool``+``args``（安全层）。字段全不 present 时四层都是
        ``not_applicable``，``pass`` 真空为 True——那是"**无可校验**"，
        不是"校验通过"。所以外部请用 :meth:`verify_text` / :meth:`verify_tool`，
        它们会正确组装提案。

        返回三态：``pass=True/False/None``（None = 证据不足）。
        """
        if not self.available:
            return {}
        try:
            v = self._kit.ask(dict(proposal or {}), facts)
            self._last_verify = dict(v or {})
            return dict(v or {})
        except Exception as ex:
            self._note_error("verify", ex)
            return {}

    def verify_text(self, conclusion: str,
                    facts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """校验一条**结论性声明**（走逻辑层：能否由规则推出 / 是否与已知矛盾）。

        典型用法：黑武士准备说"你上个月说过 X"之前，先让 V2 判一次。

        ``facts`` 传记忆条目字典即可——内部会摊平成字符串事实集
        （pasm2 的前向链推理要的是 ``Iterable[str]``，直接传 dict 会抛
        unhashable，这层做掉这个转换）。
        """
        if not self.available:
            return {}
        flat: List[str] = []
        for f in (facts or []):
            if isinstance(f, str):
                flat.append(f)
            elif isinstance(f, dict):
                parts = [str(f.get("title") or ""), str(f.get("brief") or "")]
                cat = str(f.get("category") or f.get("cat") or "")
                if cat:
                    parts.append(cat)
                blob = " ".join(p for p in parts if p).strip()
                if blob:
                    flat.append(blob)
            else:
                flat.append(str(f))
        return self.verify({"conclusion": str(conclusion or "")}, flat)

    def verify_tool(self, tool: str, args: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
        """校验一次**工具调用**（走安全层）。

        这是 V2 给黑武士最实用的一条独立防线：安全层的边界约束、欺骗检测、
        人类优先是**内核级**的，不依赖 LLM 自觉——模型再怎么被提示词绕过，
        这一关仍在。只有应用层工具黑名单的方案，理论上可被绕过。

        ⚠ 前置条件：必须先 :meth:`set_tool_allowlist` 喂进真实工具名。
        pasm2 的 ``boundary_tool_allowlist`` **空元组 = 全拒**（保守起点），
        不喂白名单会把正常工具一起拦掉——这是集成时最容易踩的坑。
        """
        if not self.available:
            return {}
        return self.verify({"tool": str(tool or ""), "args": dict(args or {})})

    def set_tool_allowlist(self, names: List[str]) -> Dict[str, Any]:
        """更新工具白名单（运行期工具集变更时用，如开关 shell 后重载工具）。

        三个必须知道的 pasm2 行为（集成踩坑点）：

        1. 白名单位于 ``SafetyLayer.guard.allowlist``（``BoundaryGuard`` 上），
           **不在** ``SafetyLayer`` 本身；
        2. ``guard`` 只在 ``safety_boundary_lock=True`` 时才构造，
           所以桥接层**构造时**就要拿到工具名，构造完再补来不及；
        3. ``boundary_escalation_after``——**连续 N 次拒绝会进入
           ``locked_out``，此后连白名单内工具也全拒**（防探测式扫描）。
           调试期连拒多次后必须 :meth:`reset_safety_lockout` 复位。
        """
        if not self.available:
            return {"ok": False, "reason": self.reason}
        names = [str(n) for n in (names or []) if str(n).strip()]
        self._allowlist = names
        guard = self._guard()
        if guard is None:
            return {"ok": False, "count": len(names),
                    "reason": "安全层 guard 未构造（构造时白名单为空）；需重建桥接层"}
        try:
            guard.allowlist = frozenset(names)
        except Exception as ex:
            return {"ok": False, "count": len(names),
                    "reason": f"{type(ex).__name__}: {ex}"}
        return {"ok": True, "count": len(names), "names": names,
                "locked_out": bool(getattr(guard, "_locked_out", False))}

    def _guard(self) -> Any:
        """取 BoundaryGuard（白名单与限流都在它上面）。"""
        safety = getattr(getattr(self._kit, "_mind", None), "safety", None) \
            if self.available else None
        return getattr(safety, "guard", None) if safety is not None else None

    def safety_state(self) -> Dict[str, Any]:
        """安全层运行态：是否锁死、连续拒绝次数、白名单规模、检查/拦截计数。"""
        guard = self._guard()
        if guard is None:
            return {"available": False,
                    "reason": "安全层 guard 未构造" if self.available else self.reason}
        return {
            "available": True,
            "locked_out": bool(getattr(guard, "_locked_out", False)),
            "consecutive_blocks": int(getattr(guard, "_consecutive_blocks", 0) or 0),
            "escalation_after": getattr(guard, "escalation_after", None),
            "allowlist_size": len(getattr(guard, "allowlist", ()) or ()),
            "total_checks": int(getattr(guard, "_total", 0) or 0),
            "blocked": int(getattr(guard, "_blocked", 0) or 0),
            "injection_blocks": int(getattr(guard, "_injection_blocks", 0) or 0),
        }

    def reset_safety_lockout(self) -> Dict[str, Any]:
        """复位连续拒绝导致的锁死（调试/长跑看门狗用）。"""
        guard = self._guard()
        fn = getattr(guard, "reset_lockout", None) if guard is not None else None
        if not callable(fn):
            return {"ok": False, "reason": "安全层 guard 未构造或无复位接口"}
        try:
            was = bool(fn())
            return {"ok": True, "was_locked_out": was}
        except Exception as ex:
            return {"ok": False, "reason": f"{type(ex).__name__}: {ex}"}

    def night(self) -> Dict[str, Any]:
        """睡眠巩固：符号升降级 + 失衡告警 + 巩固摘要。

        返回的 ``imbalance_alerts`` 是 V2 的**十四项失衡监测**结果，
        也就是"黑武士发现自己哪里不对劲"——V1 完全没有这个概念。
        """
        if not self.available:
            return {}
        try:
            out = dict(self._kit.night() or {})
            alerts = out.get("imbalance_alerts")
            if alerts:
                with self._lock:
                    self._alerts = (alerts if isinstance(alerts, list)
                                    else [alerts])[-20:]
            return out
        except Exception as ex:
            self._note_error("night", ex)
            return {}

    # ------- V2 闭环与成长（阶段 10）--------------------------------

    def growth_review(self) -> Dict[str, Any]:
        """成长复盘：读失衡告警 → 提议参数调整（**纯函数，不改运行中引擎**）。

        返回只保留**可 JSON 序列化**的部分：原样返回里带 ``PASM2Config``
        dataclass，直接透传会让 ``/status`` 序列化失败。
        """
        if self._agent is None:
            return {}
        try:
            r = self._agent.growth_review() or {}
            slim: Dict[str, Any] = {
                "n_alerts": int(r.get("n_alerts") or 0),
                "by_code": dict(r.get("by_code") or {}),
                "proposals": _jsonable(r.get("proposals")) or [],
                "changes": _jsonable(r.get("changes")) or [],
                "ok": bool(r.get("ok", True)),
            }
            with self._lock:
                self._growth = slim
            return slim
        except Exception as ex:
            self._note_error("growth_review", ex)
            return {}

    def what_if(self, context_seed: List[int], steps: int = 6) -> Dict[str, Any]:
        """反事实推演：「如果那样做会怎样」。

        V2 的层 3 世界模型能力。证据不足时内部会诚实拒编，这里原样透传。
        """
        if self._agent is None:
            return {}
        try:
            return dict(self._agent.what_if(list(context_seed or []), steps=steps) or {})
        except Exception as ex:
            self._note_error("what_if", ex)
            return {}

    # ------- 只读快照 ---------------------------------------------

    def status(self) -> Dict[str, Any]:
        """心智状态快照（给 /status 与「心智」视图）。"""
        with self._lock:
            alerts = list(self._alerts)
            digest = dict(self._last_digest)
            verify = dict(self._last_verify)
            growth = dict(self._growth)
        base: Dict[str, Any] = {
            "available": self.available,
            "reason": self.reason,
            "profile": self.profile,
            "version": self._probe.get("version", ""),
            "stage": self._probe.get("stage", ""),
            "api": self._probe.get("api", ""),
            # 实时读，不用构造时的快照：冷启动时 False，
            # 语料攒够后前端刷新就能看到 True
            "semantic_embedding": bool(
                getattr(self._embedder, "semantic", self._semantic)),
            "embedding": self.embedding_status(),
            "uptime": round(time.time() - self._started_at, 1),
            "alerts": alerts,
        }
        if not self.available:
            base["layers"] = self._layers_offline()
            return base
        try:
            st = self._kit.status()
            base.update({
                "step": st.get("step"),
                "entities": st.get("entities"),
                "symbols": st.get("symbols"),
                "prediction_hit_rate": st.get("prediction_hit_rate"),
                "memory_graph_edges": st.get("memory_graph_edges"),
                "slh_one_to_one_ratio": st.get("slh_one_to_one_ratio"),
                "safety": st.get("safety") or {},
                "digest": digest,
                "verify": verify,
                "growth": growth,
            })
        except Exception:
            pass
        base["layers"] = self._layers_live()
        return base

    def _layers_offline(self) -> List[Dict[str, Any]]:
        """V2 不可用时：十九层全部标注为"未接入"，不假装有。"""
        return [{"layer": a, "name": b, "role": c, "module": d,
                 "active": False, "value": None,
                 "note": self.reason or "PASM V2 未接入"} for a, b, c, d in LAYERS]

    def _layers_live(self) -> List[Dict[str, Any]]:
        """V2 可用时：由 digest + 开关状态推十九层活跃度（0..1 或 None）。"""
        with self._lock:
            d = dict(self._last_digest)
        emo = d.get("emotion") or {}
        pred = d.get("prediction") or {}
        val = emo.get("valence")
        aro = emo.get("arousal")
        hit = pred.get("hit")
        rpe = pred.get("rpe")
        gate = d.get("gate_passed")
        gut = d.get("gut_feeling")
        nov = d.get("novelty")

        # (模块键, 活跃值, 人类可读说明)
        live: Dict[str, Tuple[Optional[float], str]] = {
            "safety": (1.0, "常开（价值锚点不可关）"),
            "brainstem": (num(val), f"效价 {num(val)} / 唤醒 {num(aro)}"),
            "perception": (num(d.get("sim")), f"象量相似度 {num(d.get('sim'))}"),
            "thalamus": (None if gate is None else (1.0 if gate else 0.0),
                     "门控通过" if gate else "被门控拦下"),
            "working_memory": (num(aro), "工作记忆活跃度以唤醒近似"),
            "memory": (num(d.get("sim")), f"命中象量 #{d.get('entity')}"),
            "cerebellum": (num(rpe), f"时序误差 {num(rpe)}"),
            "world_model": (None if hit is None else float(bool(hit)),
                        "预测命中" if hit else ("预测落空" if hit is not None else "样本不足")),
            "emotion": (num(val), f"效价 {num(val)}"),
            "insula": (num(gut), f"躯体标记 {num(gut)}"),
            "personality": (None, "慢时间尺度，按周演化"),
            "metacognition": (num(rpe), f"ACC 预测误差 {num(rpe)}"),
            "network": (None, "DMN/突显按需触发"),
            "symbol": (None, "睡眠时升降级"),
            "symbol_mapping": (None, "tell() 时挂载"),
            "output": (num(val), "输出层"),
            "planner": (None, "CEM+MPC，按需"),
            "cortex": (1.0, "对外执行经安全层"),
            "narrator": (None, "经 LLM 桥出话"),
        }
        out: List[Dict[str, Any]] = []
        for a, b, c, key in LAYERS:
            v, note = live.get(key, (None, "未开启"))
            out.append({"layer": a, "name": b, "role": c, "module": key,
                        "active": v is not None, "value": v, "note": note})
        # 新颖度单独挂到丘脑，说明"为什么这次不一样"
        if nov is not None:
            for it in out:
                if it["module"] == "thalamus":
                    it["note"] += f"（新颖度 {num(nov)}）"
        return out

    def to_prompt(self) -> str:
        """注入上下文（ACI）。只给模型**真正用得上的**认知信号。"""
        if not self.available:
            return ""
        d = self._last_digest
        if not d:
            return ""
        bits: List[str] = []
        emo = d.get("emotion") or {}
        pred = d.get("prediction") or {}
        if emo:
            bits.append(f"情绪 效价 {num(emo.get('valence'))} / 唤醒 {num(emo.get('arousal'))}")
        if pred:
            state = "预测命中" if pred.get("hit") else (
                "预测落空（可能出乎意料）" if pred.get("hit") is not None else "预测样本不足")
            bits.append(f"世界模型 {state}｜RPE {num(pred.get('rpe'))}")
        if d.get("gate_passed") is False:
            bits.append("本次输入被丘脑门控判为低相关，可简短回应")
        if d.get("gut_feeling") is not None:
            bits.append(f"躯体标记 {num(d.get('gut_feeling'))}（直觉倾向，供参考不必盲从）")
        if not bits:
            return ""
        return "[PASM V2 认知] " + "｜".join(bits)

    # ------- 内部 -------------------------------------------------

    def _note_error(self, where: str, ex: Exception) -> None:
        """记录单次调用失败，但**不拖垮整个桥接层**。

        pasm2 的约定是"能力不可用就返回 ``ok:False`` + reason"，
        而不是抛异常。所以这里抛出来 = 集成有 bug；记下来便于排查，
        但绝不让一次调用失败把 V2 永久降级（那会让用户以为"没装 V2"）。
        """
        with self._lock:
            self._errors = getattr(self, "_errors", [])
            self._errors.append({"op": where,
                                 "error": f"{type(ex).__name__}: {ex}"})
            self._errors = self._errors[-20:]

    def _degrade(self, where: str, ex: Exception) -> None:
        """真正致命的失败（V2 底座本身不可用）才走这里：标注原因并降级。"""
        self._probe["available"] = False
        self._kit = None
        self.reason = f"PASM V2 在 {where} 处失败，已降级：{type(ex).__name__}: {ex}"


def build_bridge(profile: str = "full", **kw: Any) -> Pasm2Bridge:
    """按 profile 名构建桥接层。"""
    return Pasm2Bridge(profile, **kw)