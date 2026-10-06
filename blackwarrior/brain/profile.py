"""用户画像服务 —— 让黑武士"更懂你"。

白马 AI 维护结构化 user profile（角色 / 领域 / 专长 / 项目 / 偏好 / 沟通风格，带证据与置信度）。
黑武士此前只有通用记忆，没有结构化画像——这是它相对白马明显偏弱的一块。
这里补上：

- 自动从对话中**启发式抽取**画像（识别"我叫 X""我的工作是 Y""我喜欢 Z"等表达）；
- 可选 **LLM 增强抽取**（已激活时，把最近几轮丢给模型要结构化 JSON）；
- 每条画像带**证据 + 置信度**，并注入上下文，让回答贴合长期偏好；
- 支持手动 ``set_profile`` 工具纠偏。

设计原则沿用黑武士一贯的"永不隐藏降级"：离线（未激活）也能跑启发式抽取，
置信度保守；激活后 LLM 抽取给出更高置信度，但不会把网络失败伪装成成功。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

#: 支持的用户画像维度。键用于 DB，值用于 UI 标签。
ASPECT_LABELS = {
    "name": "姓名",
    "role": "身份/角色",
    "domain": "领域",
    "expertise": "专长",
    "projects": "项目",
    "preferences": "偏好",
    "communication_style": "沟通风格",
    "timezone": "时区",
}

_ASPECTS = tuple(ASPECT_LABELS.keys())


class UserProfile:
    """用户画像读写门面。"""

    def __init__(self, store: Any, config: Any, gateway: Any = None) -> None:
        self.store = store
        self.config = config
        self.gateway = gateway

    # ------- 写 ---------------------------------------------------

    def set(self, aspect: str, value: str, *, evidence: str = "",
            confidence: float = 0.6) -> bool:
        """显式设置某维度画像（手动纠偏或 LLM 抽取结果落地）。"""
        if aspect not in _ASPECTS:
            return False
        value = str(value or "").strip()
        if not value:
            return False
        self.store.upsert_profile(
            aspect, value, evidence=str(evidence or "")[:500],
            confidence=max(0.0, min(1.0, float(confidence))))
        return True

    def extract_from(self, text: str) -> Dict[str, str]:
        """启发式抽取（离线可用，零依赖、零网络）。

        只抓强信号，弱信号留给 LLM 抽取，避免把闲聊误判成画像。
        返回本次命中的维度->值，并写库（覆盖式）。
        """
        text = (text or "").strip()
        if not text:
            return {}
        found: Dict[str, str] = {}

        # 姓名：我叫/我是/我的名字是/我是叫 X
        m = re.search(
            r"(?:我叫|我是叫|我的名字[是为叫]+|我是)\s*"
            r"([A-Za-z\u4e00-\u9fa5][A-Za-z\u4e00-\u9fa5·•]{0,11})",
            text)
        if m and not _is_stopword(m.group(1)):
            cand = m.group(1).strip("·•")
            # 排除"我是做/我是学"这类不接人名的表达
            if len(cand) >= 1 and cand not in ("一", "你", "他", "她", "谁"):
                found["name"] = cand

        # 身份/工作：我的工作/职业/岗位是 X；我是做 X 的；我从事 X
        m = re.search(
            r"(?:我的工作|我的职业|我的岗位|我是做|我从事|我的身份[是为]+)\s*"
            r"[:：]?\s*([^\n，。；,.;！!？?]{1,40})", text)
        if m:
            v = m.group(1).strip(" 的是，。；,.;")
            if 1 < len(v) <= 40:
                found["role"] = v

        # 领域：我主要研究/做 X 领域；我关注 X 方向
        m = re.search(
            r"(?:我(?:主要)?研究|我关注|我专注|我深耕|我的方向[是为]+)\s*"
            r"[:：]?\s*([^\n，。；,.;！!？?]{1,30})", text)
        if m:
            v = m.group(1).strip(" 的是，。；,.;")
            if 1 < len(v) <= 30:
                found["domain"] = v

        # 偏好：我喜欢 X；我更习惯 X；我偏好 X
        m = re.search(
            r"(?:我喜欢|我更习惯|我偏好|我倾向|我一般[用使])\s*"
            r"[:：]?\s*([^\n，。；,.;！!？?]{1,40})", text)
        if m:
            v = m.group(1).strip(" 的是，。；,.;")
            if 1 < len(v) <= 40:
                found["preferences"] = v

        # 沟通风格：直接点/别绕弯/说人话/简单点
        if re.search(r"(?:直接点|别绕弯|说人话|简单点|别客套|直说)", text):
            found["communication_style"] = "直截了当，少客套"

        for aspect, value in found.items():
            self.set(aspect, value, evidence=text[:200], confidence=0.5)
        return found

    def extract_with_llm(self, messages: List[Dict[str, Any]], *,
                         max_messages: int = 8) -> Dict[str, str]:
        """LLM 增强抽取（已激活时）。把最近对话丢给模型要结构化画像。

        网络/模型失败时返回空字典并保留启发式结果，绝不静默伪造。
        """
        if self.gateway is None or not getattr(self.gateway, "ready", lambda: False)():
            return {}
        try:
            recent = [m for m in (messages or [])][-max_messages:]
            sysmsg = (
                "你是用户画像抽取器。根据对话，输出用户画像 JSON，"
                "键只能是：name/role/domain/expertise/projects/preferences/"
                "communication_style/timezone。没有把握的维度不要输出。"
                "只输出 JSON，不要解释。")
            res = self.gateway.complete(
                [{"role": "system", "content": sysmsg}] + recent,
                max_tokens=400, temperature=0.0)
            import json
            raw = res.get("content", "") or ""
            start = raw.find("{")
            end = raw.rfind("}")
            if start < 0 or end < 0:
                return {}
            obj = json.loads(raw[start:end + 1])
            got: Dict[str, str] = {}
            for k, v in obj.items():
                if k in _ASPECTS and isinstance(v, str) and v.strip():
                    if self.set(k, v.strip(), evidence="LLM 抽取", confidence=0.85):
                        got[k] = v.strip()
            return got
        except Exception:
            return {}

    # ------- 读 ---------------------------------------------------

    def get(self, aspect: Optional[str] = None) -> Any:
        rows = self.store.list_profile()
        out: Dict[str, Any] = {}
        for r in rows:
            out[r["aspect"]] = {
                "value": r["value"],
                "evidence": r.get("evidence", ""),
                "confidence": float(r.get("confidence", 0.5)),
                "updated_at": r.get("updated_at"),
            }
        return out if aspect is None else out.get(aspect)

    def to_prompt(self) -> str:
        """渲染成可注入提示词的文本块（标注置信度与依据）。"""
        p = self.get()
        if not p:
            return ""
        lines = ["[用户画像] 关于用户，黑武士已了解："]
        for aspect, info in p.items():
            label = ASPECT_LABELS.get(aspect, aspect)
            val = info.get("value", "")
            conf = float(info.get("confidence", 0.0))
            ev = str(info.get("evidence", ""))[:40]
            lines.append(f"- {label}：{val}（置信 {conf:.1f}，依据「{ev}」）")
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, Any]:
        return self.get() or {}


def _is_stopword(s: str) -> bool:
    return s.strip() in ("一", "你", "他", "她", "谁", "我们", "他们")
