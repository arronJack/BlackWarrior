"""faster-whisper 常驻工作进程。

协议（stdin/stdout，一行一个 JSON）：

    ← {"event": "ready", "model": "base"}          启动后先吐这一行
    → {"path": "C:\\...\\x.wav"}
    ← {"text": "...", "duration_ms": 3200}

模型加载要 2 秒、占几百 MB，唤醒循环每句都要识别，所以必须**常驻**
而不是每句起一次进程。
"""
import argparse
import json
import os
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="base")
    args = ap.parse_args()

    # 中文：initial_prompt 用简体句子引导，否则默认吐繁体
    #（实测"幫我搜一下今天的國內新聞"，虽然能匹配上，但指令交给 LLM
    #  多一层字形转换没必要）
    zh_prompt = "以下是普通话的句子，请用简体中文输出。"

    try:
        from faster_whisper import WhisperModel
    except Exception as ex:
        _emit({"event": "error",
               "error": f"faster-whisper 不可用: {type(ex).__name__}: {ex}"})
        return 1

    t0 = time.time()
    try:
        model = WhisperModel(args.model, device="cpu", compute_type="int8",
                             cpu_threads=max(1, (os.cpu_count() or 4) - 1))
    except Exception as ex:
        _emit({"event": "error",
               "error": f"模型加载失败: {type(ex).__name__}: {ex}"})
        return 1
    _emit({"event": "ready", "model": args.model,
           "load_ms": int((time.time() - t0) * 1000)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        path = str(req.get("path") or "")
        if not path or not os.path.exists(path):
            _emit({"event": "error", "error": f"文件不存在: {path}"})
            continue
        started = time.time()
        try:
            segs, _info = model.transcribe(
                path, language="zh", beam_size=1,
                initial_prompt=zh_prompt,
                # 关掉上下文续写：上一句的错字会顺着往下污染下一句
                condition_on_previous_text=False,
                vad_filter=False,
            )
            text = "".join(s.text for s in segs).strip()
        except Exception as ex:
            _emit({"event": "error",
                   "error": f"识别失败: {type(ex).__name__}: {ex}"})
            continue
        dur = 0
        try:
            import wave
            with wave.open(path, "rb") as w:
                dur = int(w.getnframes() / float(w.getframerate()) * 1000)
        except Exception:
            pass
        _emit({"text": text, "duration_ms": dur,
               "ms": int((time.time() - started) * 1000)})
    return 0


def _emit(obj) -> None:
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())