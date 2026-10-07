"""TTS 常驻工作进程（piper 本地优先，edge-tts 云端可选）。

协议与 :mod:`._asr_worker` 一致（一行一个 JSON）：

    ← {"event": "ready", "engine": "piper", "voices": 1}
    → {"text": "在的主人"}
    ← {"path": "C:\\...\\x.wav", "ms": 172}

**为什么默认 piper 而不是 edge-tts**：实测同一句「在的主人」

    piper（本地 ONNX）0.17s     ← 完全离线
    edge-tts（微软云端）   3.47s  ← 每次重建 WebSocket

3.5 秒的网络往返在语音对话里是致命的——你说完话要干等三秒才听见回应，
那不像助手，像卡了。所以本地优先，云端只在配置显式指定时用（音质更好）。
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid

PIPER_MODEL = "zh_CN-huayan-medium"


def _emit(obj) -> None:
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _data_dirs(data_root: str = ""):
    out = [os.path.expanduser("~/.local/share/piper")]
    if data_root:
        out.append(os.path.join(data_root, "piper"))
    return out


def load_piper(data_root: str = ""):
    from piper import PiperVoice

    for d in _data_dirs(data_root):
        m = os.path.join(d, PIPER_MODEL + ".onnx")
        c = os.path.join(d, PIPER_MODEL + ".onnx.json")
        if os.path.exists(m) and os.path.exists(c):
            return PiperVoice.load(m, config_path=c, download_dir=d), d
    # 没有模型就现下一份（国内走 hf-mirror，否则 HF 直连会卡死）
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    d = _data_dirs(data_root)[0]
    os.makedirs(d, exist_ok=True)
    return PiperVoice.load(PIPER_MODEL, download_dir=d), d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="auto")   # auto | piper | edge
    ap.add_argument("--data-root", default="")
    args = ap.parse_args()

    tmp = os.environ.get("TEMP") or os.path.abspath(".")
    voice = None
    engine = "edge"

    # piper 优先；拿不到就退到 edge-tts（宁可慢也别不出声）
    if args.engine in ("auto", "piper"):
        try:
            voice, _d = load_piper(args.data_root)
            engine = "piper"
        except Exception as ex:
            if args.engine == "piper":
                _emit({"event": "error",
                       "error": f"piper 不可用: {type(ex).__name__}: {ex}"})
                return 1
    if engine == "edge":
        try:
            import edge_tts  # noqa: F401
        except Exception as ex:
            _emit({"event": "error",
                   "error": f"两种引擎都不可用（piper/edge-tts）: {ex}"})
            return 1
    _emit({"event": "ready", "engine": engine, "voice": PIPER_MODEL})

    import wave

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        text = str(req.get("text") or "").strip()
        if not text:
            continue
        started = time.time()
        if engine == "piper":
            path = os.path.join(tmp, f"bw_tts_{uuid.uuid4().hex[:12]}.wav")
            try:
                with wave.open(path, "wb") as w:
                    voice.synthesize_wav(text, w)
            except Exception as ex:
                _emit({"event": "error",
                       "error": f"合成失败: {type(ex).__name__}: {ex}"})
                continue
        else:
            path = os.path.join(tmp, f"bw_tts_{uuid.uuid4().hex[:12]}.mp3")
            ev_voice = str(req.get("voice") or "zh-CN-YunxiNeural")
            try:
                asyncio.run(_edge(text, path, ev_voice))
            except Exception as ex:
                _emit({"event": "error",
                       "error": f"合成失败: {type(ex).__name__}: {ex}"})
                continue
        if not os.path.exists(path):
            _emit({"event": "error", "error": "未产出音频"})
            continue
        _emit({"path": path, "bytes": os.path.getsize(path), "engine": engine,
               "ms": int((time.time() - started) * 1000)})
    return 0


async def _edge(text: str, path: str, voice: str) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice).save(path)


if __name__ == "__main__":
    sys.exit(main())