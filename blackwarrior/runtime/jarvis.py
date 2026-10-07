"""贾维斯运行时：唤醒 → 授权分流 → 思考 → 开口。

这一层刻意做得很薄。认知、记忆、情绪、工具执行全都复用 :class:`WarriorCore`，
不重复实现任何东西——贾维斯模式的"新东西"只有三样：

1. **唤醒判定**：靠本地 VAD 门控 + 文本同音匹配（见 :mod:`..voice.wake`）；
2. **授权分流**：低危直接干，高危转人工点击确认（见 :mod:`..voice.wake`）；
3. **开口**：把回复交给 TTS 出声。

为什么授权必须卡在**点击**而不是语音：唤醒词能被音箱/电视回放骗过。
你的音箱正在播"黑武士，删掉桌面"，它就真听见了。所以语音只负责
"提出请求"，高危动作必须有人在屏幕上点一下。这是刻意的取舍：
牺牲一点电影感，换取不会因为一段背景音删掉你的文件。
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from ..config import data_root
from ..events import emit
from ..voice.wake import (DEFAULT_WAKE_WORD, HIGH_RISK, VoiceState, gate_calls,
                          match_wake)

#: 唤醒应答。按主人偏好可配（默认「在的主人」）。
GREETINGS = ("在的主人", "我在", "在，请吩咐")

#: 需要人工确认时的口头说明模板。
_CONFIRM_TMPL = ("这一步我需要你点一下确认：{reason}。我等你点。")


class JarvisRuntime:
    """免提对话循环的服务端。"""

    def __init__(self, core: Any, config: Any = None) -> None:
        self.core = core
        self.config = config if config is not None else core.config
        self.wake_word: str = self._cfg("wake_word", DEFAULT_WAKE_WORD)
        self.greeting: str = self._cfg("wake_greeting", "") or GREETINGS[0]
        self.enabled: bool = bool(self._cfg("jarvis_enabled", True))

        self.asr = None
        self.tts = None
        self.state: str = VoiceState.IDLE
        self.hint: str = "点「开启麦克风」开始"

        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.stats: Dict[str, int] = {
            "utterances": 0, "wakes": 0, "asked": 0,
            "blocked": 0, "confirmed": 0, "speak_fail": 0,
        }

    def _cfg(self, key: str, default: Any = "") -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    # -------------------------------------------------- 生命周期

    def start(self) -> None:
        """后台拉起 ASR / TTS 常驻进程。**永不阻断启动**——
        语音组件缺失只是"这个功能用不了"，不是"程序起不来"。"""
        if not self.enabled:
            return
        try:
            from ..voice.asr_local import LocalASR
            asr = LocalASR(self.config, data_root=data_root())
            if asr.available():
                threading.Thread(target=asr.start, name="jarvis-asr",
                                 daemon=True).start()
                self.asr = asr
        except Exception as ex:
            emit("jarvis", {"stage": "asr_init_failed", "error": str(ex)[:200]})

        try:
            from ..voice.tts_edge import EdgeTTS
            tts = EdgeTTS(self.config, data_root=data_root())
            if tts.available():
                threading.Thread(target=tts.start, name="jarvis-tts",
                                 daemon=True).start()
                self.tts = tts
        except Exception as ex:
            emit("jarvis", {"stage": "tts_init_failed", "error": str(ex)[:200]})

    def stop(self) -> None:
        for m in (self.asr, self.tts):
            try:
                if m:
                    m.stop()
            except Exception:
                pass

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "hint": self.hint,
            "wake_word": self.wake_word,
            "greeting": self.greeting,
            "asr": self.asr.status() if self.asr else {
                "available": False, "reason": "未启用本地 ASR"},
            "tts": self.tts.status() if self.tts else {
                "available": False, "reason": "未启用 TTS"},
            "pending": len(self._pending),
            "stats": dict(self.stats),
        }

    def set_state(self, state: str, hint: str = "") -> None:
        self.state = state
        if hint:
            self.hint = hint
        emit("jarvis_state", {"state": state, "hint": self.hint})

    # -------------------------------------------------- 主流程

    def handle_text(self, text: str) -> Dict[str, Any]:
        """处理一句（已转写好的）文本。

        返回值带 ``kind``：
        - ``wake``    只叫醒没带指令 → 应一声
        - ``ask``     正常执行了（可能顺带朗读回复）
        - ``confirm``撞上高危动作 → 等人点确认
        - ``ignore``  没叫醒我，也不是指令
        """
        text = (text or "").strip()
        if not text:
            return {"kind": "ignore", "reason": "空输入"}
        self.stats["utterances"] += 1
        emit("jarvis_heard", {"text": text[:200]})

        # ---- 1. 唤醒判定 ----
        wk = match_wake(text, self.wake_word)
        if not wk["hit"]:
            self.set_state(VoiceState.WAITING, f"没听见「{self.wake_word}」")
            return {"kind": "ignore", "reason": "未唤醒", "asr_text": text}

        self.stats["wakes"] += 1
        command = wk["rest"].strip()
        emit("jarvis_wake", {"word": wk["word"], "mode": wk["mode"],
                             "command": command[:200]})
        self.set_state(VoiceState.THINKING)

        # ---- 2. 只唤醒没指令 → 应一声 ----
        if not command:
            self.set_state(VoiceState.SPEAKING)
            audio = self._speak(self.greeting)
            self.set_state(VoiceState.WAITING, "请吩咐")
            return {"kind": "wake", "reply": self.greeting, "audio": audio,
                    "asr_text": text, "wake_mode": wk["mode"]}

        # ---- 3. 有指令 → 思考 + 授权分流 ----
        self.set_state(VoiceState.THINKING)
        return self._think_and_act(command, text)

    def _think_and_act(self, command: str, heard: str) -> Dict[str, Any]:
        """跑一个回合，并在**执行前**做授权分流。

        这里有个顺序上的关键点：``core.ask`` 会把工具调用**直接执行掉**，
        所以不能先跑再检查——那样危险动作已经发生了。必须先自己跑一轮
        拿到"打算调什么工具"，分流后才决定哪些能放行。

        为此用 :meth:`WarriorCore.plan_calls`（只推理不执行）拿到计划，
        低危的执行掉，高危的挂起等确认。
        """
        try:
            plan = self.core.plan_calls(command)
        except Exception as ex:
            self.set_state(VoiceState.SPEAKING)
            msg = f"我想事情的时候出了点问题：{ex}"
            audio = self._speak(msg)
            self.set_state(VoiceState.WAITING)
            return {"kind": "ask", "reply": msg, "audio": audio,
                    "asr_text": heard, "error": str(ex)[:200]}

        calls = list(plan.get("calls") or [])
        gated = gate_calls(calls)
        allowed = gated["allowed"]
        pending = gated["pending"]

        # ---- 3a. 有高危动作 → 一律不执行，等人点 ----
        if pending:
            self.stats["blocked"] += 1
            pid = uuid.uuid4().hex[:12]
            reasons = "；".join(sorted({p["reason"] for p in pending}))
            with self._lock:
                self._pending[pid] = {
                    "command": command, "heard": heard,
                    "calls": pending, "plan": plan, "created": time.time(),
                }
            reply = _CONFIRM_TMPL.format(reason=reasons)
            self.set_state(VoiceState.CONFIRM)
            audio = self._speak(reply)
            emit("jarvis_confirm_required",
                 {"pending_id": pid, "calls": pending, "reason": reasons})
            return {"kind": "confirm", "pending_id": pid, "pending": pending,
                    "reason": reasons, "reply": reply, "audio": audio,
                    "asr_text": heard}

        # ---- 3b. 全部低危 → 直接执行 ----
        reply = ""
        if allowed:
            try:
                res = self.core.execute_calls(allowed)
                reply = (res.get("summary") if isinstance(res, dict) else "") or ""
            except Exception as ex:
                reply = f"执行的时候出错了：{ex}"
        if not reply:
            reply = plan.get("text") or ""
        if not reply.strip():
            reply = "这件事我办了，但没什么要汇报的。"

        self.stats["asked"] += 1
        self.set_state(VoiceState.SPEAKING)
        audio = self._speak(reply)
        self.set_state(VoiceState.WAITING)
        return {"kind": "ask", "reply": reply, "audio": audio,
                "asr_text": heard, "executed": [c.get("name") for c in allowed]}

    # -------------------------------------------------- 人工确认

    def confirm(self, pending_id: str, ok: bool) -> Dict[str, Any]:
        with self._lock:
            item = self._pending.pop(pending_id, None)
        if item is None:
            return {"error": "没有待确认的操作（可能已过期或已处理）"}
        if not ok:
            reply = "好，已经取消了。"
            emit("jarvis_confirm_result",
                 {"pending_id": pending_id, "ok": False})
            return {"reply": reply, "audio": self._speak(reply)}

        self.stats["confirmed"] += 1
        try:
            res = self.core.execute_calls(item["calls"])
            reply = ((res.get("summary") if isinstance(res, dict) else "")
                     or "已按你的确认执行完毕。")
        except Exception as ex:
            reply = f"执行失败：{ex}"
        emit("jarvis_confirm_result",
             {"pending_id": pending_id, "ok": True, "n": len(item["calls"])})
        return {"reply": reply, "audio": self._speak(reply)}

    # -------------------------------------------------- 语音

    def transcribe(self, wav_path: str) -> Dict[str, Any]:
        """识别一段音频。返回文本给前端，由前端决定是否算唤醒。"""
        if not self.asr or not self.asr.ready:
            return {"text": "", "error": "本地 ASR 未就绪"}
        r = self.asr.transcribe(wav_path)
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass
        return r

    def _speak(self, text: str) -> str:
        """合成语音。**失败只记数不抛**——没出声也要把文字给到前端。"""
        if not self.tts or not text:
            return ""
        try:
            p = self.tts.speak(text)
            if not p:
                self.stats["speak_fail"] += 1
            return p or ""
        except Exception:
            self.stats["speak_fail"] += 1
            return ""

    # -------------------------------------------------- 文本直送

    def say_text(self, text: str) -> Dict[str, Any]:
        """不走 ASR，直接处理一段文本（打字问它同样能出声）。"""
        return self.handle_text(text if match_wake(text, self.wake_word)["hit"]
                                else f"{self.wake_word}，{text}")