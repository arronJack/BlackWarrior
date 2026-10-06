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

from .service import VoiceService, build_voice_service

__all__ = ["VoiceService", "build_voice_service"]
