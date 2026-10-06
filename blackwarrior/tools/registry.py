"""工具注册表。

黑武士的工具系统有三个多数同类方案缺失或做得不完整的地方：

1. **风险分级是一等公民**：每个工具注册时必须声明 ``risk``
   （``safe`` / ``caution`` / ``danger``），``danger`` 默认拒绝执行，
   需要显式开启或用户确认。而不是"全都能调，出事再说"。
2. **schema 从函数签名自动推导**：用 ``inspect`` + 类型标注生成
   OpenAI function-calling 的 JSON Schema，不用手写两遍参数说明
   （手写两遍 = 迟早对不上）。
3. **工具与能力分离**：注册表只管"怎么调"，不管"该不该调"，
   后者交给 :mod:`.policy`。

注：``pasm_skills.cognition.tools`` 也提供了一个 ``ToolRegistry``，
那是给 PASM 智能体用的轻量版；这里的是面向桌面 Agent 的完整版
（支持风险、审计、schema 推导、并发安全）。
"""

from __future__ import annotations

import inspect
import threading
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Optional

#: 风险等级
RISK_SAFE = "safe"          # 只读，随便调
RISK_CAUTION = "caution"    # 有副作用（写文件、执行常规命令）
RISK_DANGER = "danger"      # 破坏性 / 系统级（删除、格式化、安装软件）

RISK_ORDER = {RISK_SAFE: 0, RISK_CAUTION: 1, RISK_DANGER: 2}

_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_type(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty or annotation is None:
        return "string"
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        if origin in (list, set, tuple):
            return "array"
        if origin is dict:
            return "object"
    return _TYPE_MAP.get(annotation, "string")


class ToolSpec:
    """一个工具的规格。"""

    def __init__(self, name: str, handler: Callable[..., Any], *,
                 description: str = "", parameters: Optional[Dict[str, Any]] = None,
                 risk: str = RISK_SAFE, category: str = "general",
                 aliases: Optional[Iterable[str]] = None,
                 timeout: float = 60.0) -> None:
        self.name = name
        self.handler = handler
        self.description = description or (inspect.getdoc(handler) or "").strip() \
            or name
        self.parameters = parameters or _schema_from_callable(handler)
        self.risk = risk if risk in RISK_ORDER else RISK_SAFE
        self.category = category
        self.aliases = list(aliases or [])
        self.timeout = float(timeout)
        self.calls = 0
        self.errors = 0

    def schema(self) -> Dict[str, Any]:
        """OpenAI function-calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description[:1024],
                "parameters": self.parameters or {
                    "type": "object", "properties": {}, "required": [],
                },
            },
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "category": self.category,
            "aliases": self.aliases,
            "calls": self.calls,
            "errors": self.errors,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ToolSpec {self.name} risk={self.risk}>"


class ToolResult:
    """工具执行结果。"""

    def __init__(self, ok: bool, name: str, *, result: Any = None,
                 error: str = "", duration_ms: int = 0,
                 blocked: bool = False, reason: str = "") -> None:
        self.ok = ok
        self.name = name
        self.result = result
        self.error = error
        self.duration_ms = duration_ms
        self.blocked = blocked
        self.reason = reason

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "name": self.name,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "blocked": self.blocked,
            "reason": self.reason,
        }

    def text(self, limit: int = 4000) -> str:
        """给模型看的文本化结果。"""
        if self.blocked:
            return f"[{self.name}] 被拒绝：{self.reason}"
        if not self.ok:
            return f"[{self.name}] 执行失败：{self.error}"
        if self.result is None:
            return f"[{self.name}] 完成（无输出）"
        try:
            s = self.result if isinstance(self.result, str) else \
                _dumps(self.result)
        except Exception:
            s = str(self.result)
        if len(s) > limit:
            return s[:limit] + f"\n…（已截断，共 {len(s)} 字符）"
        return s

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ToolResult {self.name} ok={self.ok}>"


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _schema_from_callable(fn: Callable[..., Any]) -> Dict[str, Any]:
    """从函数签名推导 JSON Schema。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}, "required": []}

    props: Dict[str, Any] = {}
    required: List[str] = []
    doc = inspect.getdoc(fn) or ""
    param_docs = _parse_param_docs(doc)

    for pname, p in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        schema: Dict[str, Any] = {"type": _json_type(p.annotation)}
        if pname in param_docs:
            schema["description"] = param_docs[pname]
        if p.default is not inspect.Parameter.empty:
            schema["default"] = p.default
        else:
            required.append(pname)
        props[pname] = schema

    return {"type": "object", "properties": props, "required": required}


def _parse_param_docs(doc: str) -> Dict[str, str]:
    """从 docstring 的 ``参数`` / ``Args`` 段解析参数说明。"""
    out: Dict[str, str] = {}
    if not doc:
        return out
    lines = doc.splitlines()
    active = False
    for line in lines:
        s = line.strip()
        if s.lower().startswith(("参数", "args", "arguments", "parameters")):
            active = True
            continue
        if active:
            if not s:
                continue
            if ":" in s:
                k, v = s.split(":", 1)
                out[k.strip()] = v.strip()
            elif s.startswith("-") or s.startswith("返回") or s.startswith("Returns"):
                active = False
    return out


class ToolRegistry:
    """工具注册表。线程安全。"""

    def __init__(self, *, allow: Optional[Iterable[str]] = None) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        self._alias: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._allow: Optional[set] = set(allow) if allow is not None else None

    # ------- 注册 -------------------------------------------------

    def register(self, name: str, handler: Callable[..., Any], *,
                 description: str = "", parameters: Optional[Dict[str, Any]] = None,
                 risk: str = RISK_SAFE, category: str = "general",
                 aliases: Optional[Iterable[str]] = None,
                 timeout: float = 60.0) -> ToolSpec:
        spec = ToolSpec(name, handler, description=description,
                        parameters=parameters, risk=risk, category=category,
                        aliases=aliases, timeout=timeout)
        with self._lock:
            self._tools[name] = spec
            for a in spec.aliases:
                self._alias[a] = name
        return spec

    def tool(self, name: Optional[str] = None, *, description: str = "",
             risk: str = RISK_SAFE, category: str = "general",
             aliases: Optional[Iterable[str]] = None,
             timeout: float = 60.0) -> Callable[..., Any]:
        """装饰器形式注册。"""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(name or fn.__name__, fn, description=description,
                          risk=risk, category=category, aliases=aliases,
                          timeout=timeout)
            return fn

        return deco

    def unregister(self, name: str) -> bool:
        with self._lock:
            spec = self._tools.pop(name, None)
            if spec is None:
                return False
            for a, target in list(self._alias.items()):
                if target == name:
                    del self._alias[a]
            return True

    # ------- 白名单 -----------------------------------------------

    def allow(self, names: Iterable[str]) -> None:
        with self._lock:
            self._allow = set(names)

    def allow_all(self) -> None:
        with self._lock:
            self._allow = None

    def allowed(self, name: str) -> bool:
        with self._lock:
            if self._allow is None:
                return True
            return name in self._allow

    # ------- 查询 -------------------------------------------------

    def get(self, name: str) -> Optional[ToolSpec]:
        with self._lock:
            real = self._alias.get(name, name)
            return self._tools.get(real)

    def names(self) -> List[str]:
        with self._lock:
            return list(self._tools.keys())

    def specs(self, only_allowed: bool = True) -> List[ToolSpec]:
        with self._lock:
            items = list(self._tools.values())
        if only_allowed:
            items = [s for s in items if self.allowed(s.name)]
        return items

    def schemas(self, only_allowed: bool = True) -> List[Dict[str, Any]]:
        return [s.schema() for s in self.specs(only_allowed=only_allowed)]

    def describe(self, only_allowed: bool = True) -> List[Dict[str, Any]]:
        return [s.as_dict() for s in self.specs(only_allowed=only_allowed)]

    # ------- 执行 -------------------------------------------------

    def call(self, name: str, args: Optional[Dict[str, Any]] = None,
             *, timeout: Optional[float] = None) -> ToolResult:
        """执行一个工具。"""
        spec = self.get(name)
        if spec is None:
            return ToolResult(False, name, error=f"未知工具：{name}")
        if not self.allowed(name):
            return ToolResult(False, name, blocked=True,
                              reason=f"工具 {name} 不在白名单内")

        started = time.time()
        try:
            kwargs = dict(args or {})
            # 过滤掉模型可能塞进来的未知参数，避免直接 TypeError
            allowed_params = set((spec.parameters or {}).get("properties", {}).keys())
            if allowed_params:
                unknown = set(kwargs.keys()) - allowed_params
                for k in unknown:
                    kwargs.pop(k)
            with self._lock:
                spec.calls += 1
            result = spec.handler(**kwargs)
            return ToolResult(True, name, result=result,
                              duration_ms=int((time.time() - started) * 1000))
        except Exception as ex:
            with self._lock:
                spec.errors += 1
            return ToolResult(False, name,
                              error=f"{type(ex).__name__}: {ex}",
                              trace=traceback.format_exc(limit=3),
                              duration_ms=int((time.time() - started) * 1000))

    # ------- 运维 -------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        specs = self.specs(only_allowed=False)
        return {
            "total": len(specs),
            "by_risk": {
                r: sum(1 for s in specs if s.risk == r) for r in RISK_ORDER
            },
            "by_category": _count_by(specs, lambda s: s.category),
            "calls": sum(s.calls for s in specs),
            "errors": sum(s.errors for s in specs),
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None


def _count_by(items: List[ToolSpec], key) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i in items:
        k = str(key(i))
        out[k] = out.get(k, 0) + 1
    return out
