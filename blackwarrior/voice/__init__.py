"""语音能力（可选）。

设计前提：**浏览器内置的 SpeechRecognition 在 Electron 里不可用**
（打包的 Chromium 不带该云服务的 API key），所以识别不能依赖它。

黑武士走的路线——和成熟桌面 Agent 一致：

    麦克风 → MediaRecorder 录音 → 上传内核 → 转发 OpenAI 兼容
    /audio/transcriptions → 文本

好处：桌面壳内真能用；且**不配就不启用**，零强制依赖。
朗读（TTS）优先用浏览器本地合成（离线可用），也可选走云端合成换取更好音质。
"""

from __future__ import annotations

from typing import Any, Optional

from .service import VoiceService, build_voice_service
from .wake import (DEFAULT_WAKE_WORD, HIGH_RISK, LOW_RISK, TurnSegmenter,
                   VoiceState, classify_tool, gate_calls, match_wake)

__all__ = [
    "VoiceService", "build_voice_service",
    "DEFAULT_WAKE_WORD", "HIGH_RISK", "LOW_RISK",
    "TurnSegmenter", "VoiceState", "classify_tool", "gate_calls", "match_wake",
    "build_asr",
]


def build_asr(config: Any = None, data_root: str = "") -> Any:
    """本地 ASR（离线）。**延迟导入**：faster-whisper 在独立 venv 里，
    导入失败必须降级而不是让整个内核起不来。"""
    try:
        from .asr_local import LocalASR
        return LocalASR(config, data_root=data_root)
    except Exception:
        return None
