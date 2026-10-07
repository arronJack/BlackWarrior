"""云端 TTS 桥（edge-tts，微软免费音色，无需 API Key）。

和 :mod:`.asr_local` 同一套设计：内核纯标准库，重依赖隔离在子进程里。

音色选择：中文里xiaoxiao（晓晓）最自然、 Yunxi（云希）偏男声更贴
"贾维斯"。默认按配置 ``tts_voice``，没配就用云希——唤醒应答
「在的主人」用男声比女声更贴合这个角色。

失败一律返回空串：说句话失败不该让对话循环崩掉，调用方拿空串
就知道没出声（前端会退化成只显示文字）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_tts_worker.py")

#: 推荐音色（都在 edge-tts 的免费列表里，不需要 Key）
RECOMMENDED = [
    "zh-CN-YunxiNeural",     # 男声，贴合"助手"角色
    "zh-CN-XiaoxiaoNeural",  # 女声，最自然
    "zh-CN-YunjianNeural",   # 男声，浑厚
    "zh-CN-XiaoyiNeural",    # 女声，清亮
]


def find_venv(data_root: str = "") -> str:
    from .asr_local import find_venv as _f
    return _f(data_root)


class EdgeTTS:
    """常驻 TTS 客户端。``tts_engine`` 配置项控制走哪条路。"""

    def __init__(self, config: Any = None, data_root: str = "",
                 voice: str = "", timeout: float = 30.0) -> None:
        self.config = config
        self.data_root = data_root
        self.voice = voice or self._cfg("tts_voice", "") or "zh-CN-YunxiNeural"
        self.timeout = float(self._cfg("tts_timeout", timeout) or timeout)
        self.py = find_venv(data_root)
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self.ready: bool = False
        self.last_error: str = ""
        self.calls: int = 0
        self.engine: str = self._cfg("tts_engine", "auto") or "auto"
        self.active_engine: str = ""

    def _cfg(self, key: str, default: Any = "") -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def available(self) -> bool:
        return bool(self.py) and os.path.exists(_WORKER)

    def status(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "active_engine": self.active_engine or "未启动",
            "available": self.available(),
            "ready": self.ready,
            "voice": self.voice,
            "calls": self.calls,
            "last_error": self.last_error[:200],
        }

    def start(self) -> bool:
        with self._lock:
            if self.ready:
                return True
            if not self.available():
                self.last_error = "找不到 TTS 环境"
                return False
            try:
                self.proc = subprocess.Popen(
                    [self.py, _WORKER, "--engine", self.engine,
                     "--data-root", self.data_root or ""],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as ex:
                self.last_error = f"{type(ex).__name__}: {ex}"
                return False
            threading.Thread(target=self._await_ready, name="tts-ready",
                             daemon=True).start()
            return True

    def _await_ready(self) -> None:
        try:
            line = self._readline(timeout=60.0)
            msg = json.loads(line) if line else {}
        except Exception as ex:
            self.last_error = f"TTS 启动失败: {ex}"
            return
        if msg.get("event") == "ready":
            self.ready = True
            self.active_engine = str(msg.get("engine") or "")
        else:
            self.last_error = str(msg.get("error", line))[:200]

    def _readline(self, timeout: float) -> str:
        box: Dict[str, Any] = {}

        def _rd() -> None:
            try:
                box["line"] = self.proc.stdout.readline().decode(
                    "utf-8", "ignore").strip()
            except Exception as ex:
                box["err"] = ex

        t = threading.Thread(target=_rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"TTS 超过 {timeout:.0f}s 未返回")
        if "err" in box:
            raise box["err"]
        return box.get("line", "")

    def speak(self, text: str, voice: str = "", rate: str = "+0%") -> str:
        """合成并返回音频文件路径；失败返回空串。"""
        text = (text or "").strip()
        if not text:
            return ""
        if not self.ready and not self.start():
            return ""
        with self._lock:
            try:
                self.proc.stdin.write((json.dumps({
                    "text": text, "voice": voice or self.voice,
                    "rate": rate}) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
                line = self._readline(self.timeout)
            except Exception as ex:
                self.last_error = f"{type(ex).__name__}: {ex}"
                return ""
        if not line:
            return ""
        try:
            msg = json.loads(line)
        except Exception:
            self.last_error = f"无法解析: {line[:120]}"
            return ""
        if msg.get("event") == "error":
            self.last_error = str(msg.get("error", ""))[:200]
            return ""
        self.calls += 1
        return str(msg.get("path") or "")

    def stop(self) -> None:
        with self._lock:
            if self.proc is None:
                return
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
            self.ready = False