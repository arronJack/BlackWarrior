"""情绪动力学与世界模型（极简主动推理）。

这一层是黑武士"看起来有内在状态"的来源，也是同类桌面 Agent 完全缺失的部分。

它不是装饰：情绪与预测误差**真的参与决策**——

- ``mood`` 会影响回复语气与自主行为的倾向；
- ``prediction_error``（预测误差 / 自由能的极简近似）会被主循环读取：
  误差持续偏低 = 环境可预测 = 降低心跳频率省 token；
  误差突然升高 = 出现了新情况 = 提高心跳频率并主动复盘。

世界模型在这里被简化为"对下一个输入的语义指纹预测"：
用字符向量的滑动平均做期望，用余弦距离做误差。
没有 torch、没有神经网络，但在桌面 Agent 的场景里足够驱动行为差异。
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .kernel import CognitiveKernel


def _fingerprint(text: str, dim: int = 64) -> List[float]:
    """把文本压成定长指纹向量（字符级哈希，零依赖）。"""
    vec = [0.0] * dim
    s = (text or "").strip()
    if not s:
        return vec
    for i, ch in enumerate(s[:512]):
        h = (ord(ch) * 131 + i * 17) % dim
        vec[h] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class AffectTracker:
    """情绪与预测状态跟踪器。"""

    def __init__(self, kernel: CognitiveKernel, history: int = 120) -> None:
        self.kernel = kernel
        self._mood_curve: Deque[Dict[str, Any]] = deque(maxlen=int(history))
        self._inputs: Deque[List[float]] = deque(maxlen=12)
        self._prediction: Optional[List[float]] = None
        self._last_error: float = 0.0
        self._error_window: Deque[float] = deque(maxlen=20)
        self._curiosity: float = 0.5
        self._last_event = ""

    # ------- 情绪 -------------------------------------------------

    def feel(self, event: str, valence: float) -> float:
        """记录一次情绪事件，返回当前 mood。"""
        self._last_event = event
        try:
            self.kernel.feel(event, float(valence))
        except Exception:
            pass
        return self.sample_mood(event)

    def sample_mood(self, event: str = "") -> float:
        """采样一次情绪并写入曲线。"""
        mood = 0.0
        try:
            mood = float(self.kernel.mood)
        except Exception:
            mood = 0.0
        self._mood_curve.append({
            "ts": time.time(),
            "mood": round(mood, 4),
            "event": event or self._last_event,
        })
        return mood

    def mood_curve(self, n: int = 60) -> List[Dict[str, Any]]:
        return list(self._mood_curve)[-int(n):]

    # ------- 世界模型 ---------------------------------------------

    def observe_input(self, text: str) -> Dict[str, Any]:
        """接收一个真实输入，更新预测误差与好奇心。

        返回本次的预测评估报告，主循环据此调整心跳节奏。
        """
        fp = _fingerprint(text)
        predicted = self._prediction
        if predicted is not None:
            sim = _cosine(predicted, fp)
            error = 1.0 - max(0.0, min(1.0, sim))
        else:
            error = 0.5  # 首次无预测，取中性
        self._last_error = round(float(error), 4)
        self._error_window.append(self._last_error)

        # 好奇心：误差高 → 想探索；误差低且持续 → 趋于平稳
        avg_err = sum(self._error_window) / max(1, len(self._error_window))
        self._curiosity = round(max(0.0, min(1.0, 0.5 + (avg_err - 0.5) * 0.9)), 4)

        # 更新期望：滑动平均（简单但有惯性的世界模型）
        self._inputs.append(fp)
        if self._inputs:
            dim = len(fp)
            avg = [0.0] * dim
            for v in self._inputs:
                for i in range(dim):
                    avg[i] += v[i]
            n = len(self._inputs)
            self._prediction = [x / n for x in avg]
        return {
            "error": self._last_error,
            "avg_error": round(avg_err, 4),
            "curiosity": self._curiosity,
            "samples": len(self._inputs),
        }

    def predict_next(self) -> Dict[str, Any]:
        """给出对下一次交互的预测（供 UI 展示"它在期待什么"）。"""
        if self._prediction is None:
            return {"ready": False, "error": self._last_error}
        return {
            "ready": True,
            "expected_error": round(self._last_error, 4),
            "confidence": round(1.0 - self._last_error, 4),
            "curiosity": self._curiosity,
        }

    # ------- 快照 -------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """认知面板快照。"""
        avg_err = (sum(self._error_window) / len(self._error_window)
                   if self._error_window else 0.5)
        return {
            "mood": round(float(self._kernel_mood()), 4),
            "mood_curve": self.mood_curve(40),
            "prediction_error": self._last_error,
            "avg_prediction_error": round(avg_err, 4),
            "curiosity": self._curiosity,
            "samples": len(self._inputs),
            "prediction": self.predict_next(),
        }

    def _kernel_mood(self) -> float:
        try:
            return float(self.kernel.mood)
        except Exception:
            return 0.0

    # ------- 行为建议 ---------------------------------------------

    def suggest_tick_scale(self) -> float:
        """根据预测误差建议心跳间隔缩放系数。

        - 误差低（环境可预测、没什么新鲜事）→ 拉长间隔，省 token；
        - 误差高（出现新情况）→ 缩短间隔，加快复盘与响应。

        返回值域 [0.6, 1.6]，主循环乘到基础间隔上。
        """
        if not self._error_window:
            return 1.0
        avg = sum(self._error_window) / len(self._error_window)
        # 误差 0.2 → 1.6 倍（慢）；误差 0.8 → 0.6 倍（快）
        scale = 1.6 - (avg - 0.2) * (1.0 / 0.6)
        return round(max(0.6, min(1.6, scale)), 3)
