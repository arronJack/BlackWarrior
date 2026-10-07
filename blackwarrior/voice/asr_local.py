"""本地 ASR 桥（faster-whisper，跑在独立 venv 里）。

为什么要开子进程而不是直接 import：
BlackWarrior 内核一直是**纯标准库**（这是刻意的约束，见llm/gateway.py
的注释）。faster-whisper 会拖进 ctranslate2 / onnxruntime / av 一大串
二进制依赖，装进内核环境就等于把这条约束毁掉。子进程隔离 + stdin/stdout
通信是最干净的边界：内核只认 stdio 协议，venv 里装什么都不影响内核。

同时它天然解决**模型常驻**：whisper 模型加载要2 秒、占几百 MB，
唤醒循环每句话都要识别，绝不能每次重新加载。子进程常驻 + 模型常驻，
每条指令的边际成本只有推理本身（实测 base 模型 CPU 上 1.4 秒）。

依赖缺失（没装 venv / 没下模型）时 :func:`build_asr` 返回 None，
上层自动回落到云端 ASR 或浏览器识别——**降级而不是崩溃**。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

#: ASR 工作进程脚本（与本文件同目录），负责加载模型并循环读 stdin。
_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_asr_worker.py")


def find_venv(data_root: str = "") -> str:
    """定位装了 faster-whisper 的 venv 解释器。

    依次找：项目内 ``.venv-asr`` → 环境变量 ``BLACKWARRIOR_ASR_PYTHON``。
    找不到就返回空字符串（上层据此判定本地 ASR 不可用）。
    """
    env = os.environ.get("BLACKWARRIOR_ASR_PYTHON", "")
    if env and os.path.exists(env):
        return env
    roots: List[str] = []
    if data_root:
        # data_root 一般是 <项目>/data，回退到项目根
        roots.append(os.path.dirname(os.path.abspath(data_root)))
        roots.append(os.path.dirname(os.path.dirname(os.path.abspath(data_root))))
    roots.append(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    for root in roots:
        for rel in (".venv-asr", "venv-asr"):
            p = os.path.join(root, rel, "Scripts", "python.exe")
            if os.path.exists(p):
                return p
            p2 = os.path.join(root, rel, "bin", "python")
            if os.path.exists(p2):
                return p2
    return ""


class LocalASR:
    """本地语音识别。常驻子进程，模型只加载一次。"""

    def __init__(self, config: Any = None, data_root: str = "",
                 model: str = "", timeout: float = 30.0) -> None:
        self.config = config
        self.data_root = data_root
        self.model = model or self._cfg("asr_model", "base")
        self.timeout = float(self._cfg("asr_timeout", timeout) or timeout)
        self.py = find_venv(data_root)
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self.last_error: str = ""
        self.ready: bool = False
        self.calls: int = 0

    def _cfg(self, key: str, default: Any = "") -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    # -------------------------------------------------- 生命周期

    def available(self) -> bool:
        return bool(self.py) and os.path.exists(_WORKER)

    def status(self) -> Dict[str, Any]:
        return {
            "engine": "faster-whisper",
            "available": self.available(),
            "ready": self.ready,
            "model": self.model,
            "calls": self.calls,
            "last_error": self.last_error[:200],
            "python": self.py or "",
        }

    def start(self) -> bool:
        """拉起常驻工作进程。**不阻塞启动**：模型要load 2 秒，
        放在调用方自己的线程里跑。"""
        with self._lock:
            if self.ready:
                return True
            if not self.available():
                self.last_error = (f"找不到本地 ASR 环境"
                                   f"（{self.py or 'venv 缺失'}）")
                return False
            env = dict(os.environ)
            # 国内直连 HF 会卡住，强制走镜像（模型已缓存时无感）
            env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            try:
                self.proc = subprocess.Popen(
                    [self.py, _WORKER, "--model", self.model],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as ex:
                self.last_error = f"{type(ex).__name__}: {ex}"
                self.proc = None
                return False
            # 等它把模型加载完（worker 会先吐一行 ready）
            threading.Thread(target=self._await_ready, name="asr-ready",
                             daemon=True).start()
            return True

    def _await_ready(self) -> None:
        try:
            line = self._readline(timeout=180.0)
        except Exception as ex:
            self.last_error = f"ASR 启动失败: {ex}"
            return
        if not line:
            self.last_error = "ASR 工作进程无响应"
            return
        try:
            msg = json.loads(line)
        except Exception:
            self.last_error = f"ASR 返回无法解析: {line[:120]}"
            return
        if msg.get("event") == "ready":
            self.ready = True
        else:
            self.last_error = str(msg.get("error", line))[:200]

    def _readline(self, timeout: float = 30.0) -> str:
        """带超时的读一行。子进程卡死时不能把主线程一起拖住。"""
        import threading as _t

        box: Dict[str, Any] = {}

        def _rd() -> None:
            try:
                box["line"] = self.proc.stdout.readline().decode(
                    "utf-8", "ignore").strip()
            except Exception as ex:
                box["err"] = ex

        t = _t.Thread(target=_rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"ASR 超过 {timeout:.0f}s 未返回")
        if "err" in box:
            raise box["err"]
        return box.get("line", "")

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

    # -------------------------------------------------- 识别

    def transcribe(self, wav_path: str) -> Dict[str, Any]:
        """识别一个 WAV 文件，返回 ``{text, ms, error}``。"""
        if not self.ready and not self.start():
            return {"text": "", "ms": 0,
                    "error": self.last_error or "本地 ASR 未就绪"}
        started = time.time()
        with self._lock:
            try:
                self.proc.stdin.write(
                    (json.dumps({"path": wav_path}) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
                line = self._readline(timeout=self.timeout)
            except Exception as ex:
                self.last_error = f"{type(ex).__name__}: {ex}"
                return {"text": "", "ms": int((time.time() - started) * 1000),
                        "error": self.last_error}
        ms = int((time.time() - started) * 1000)
        if not line:
            return {"text": "", "ms": ms, "error": "ASR 无返回"}
        try:
            msg = json.loads(line)
        except Exception:
            return {"text": "", "ms": ms,
                    "error": f"无法解析: {line[:120]}"}
        self.calls += 1
        if msg.get("event") == "error":
            self.last_error = str(msg.get("error", ""))[:200]
            return {"text": "", "ms": ms, "error": self.last_error}
        return {"text": (msg.get("text") or "").strip(), "ms": ms,
                "duration_ms": msg.get("duration_ms", 0), "error": ""}