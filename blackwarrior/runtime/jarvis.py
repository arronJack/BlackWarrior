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

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .. import paths as _paths
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
        #: 由 server/routes._jarvis() 置位，保证只启动一次
        self.started: bool = False

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
            asr = LocalASR(self.config, data_root=str(_paths.data_root()))
            if asr.available():
                threading.Thread(target=asr.start, name="jarvis-asr",
                                 daemon=True).start()
                self.asr = asr
        except Exception as ex:
            emit("jarvis", {"stage": "asr_init_failed", "error": str(ex)[:200]})

        try:
            from ..voice.tts_edge import EdgeTTS
            tts = EdgeTTS(self.config, data_root=str(_paths.data_root()))
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

        # ---- 3a. 有高危动作 → 高危的一律不执行，等人点 ----
        # 低危的**照常执行**：句子里常混着无害动作（"把桌面那个文件删了，
        # 顺便告诉我几点"），把 get_time 一起扣住等确认会让免提显得很蠢。
        if pending:
            self.stats["blocked"] += 1
            pid = uuid.uuid4().hex[:12]
            reasons = "；".join(sorted({p["reason"] for p in pending}))
            prelim = ""
            done_pre: List[str] = []
            if allowed:
                try:
                    pres = self.core.execute_calls(allowed)
                    done_pre = [str(c.get("name") or "") for c in allowed]
                    # ★这一段刻意**不交给模型润色**。实测本地 qwen2.5:7b
                    # 会无视"不要声称未完成动作"的指令，把"把文件删了，顺便
                    # 告诉我几点"汇报成"文件已删除，现在时间是16:31"——而文件
                    # 其实还在等确认。语音里用户会以为真删了。
                    # 凡是"我做完了/没做完"的措辞，只用我们自己从真实结果
                    # 生成的摘要（`_digest_results`），它只包含确实发生的事。
                    prelim = self._digest_results(allowed, pres)
                except Exception:
                    prelim = ""
            with self._lock:
                self._pending[pid] = {
                    "command": command, "heard": heard,
                    "calls": pending, "plan": plan, "created": time.time(),
                }
            reply = _CONFIRM_TMPL.format(reason=reasons)
            if prelim:
                reply = f"{prelim}。{reply}" if not prelim.endswith(("。","！","？")) else prelim+reply
            self.set_state(VoiceState.CONFIRM)
            audio = self._speak(reply)
            emit("jarvis_confirm_required",
                 {"pending_id": pid, "calls": pending, "reason": reasons,
                  "already_done": done_pre})
            return {"kind": "confirm", "pending_id": pid, "pending": pending,
                    "reason": reasons, "reply": reply, "audio": audio,
                    "asr_text": heard, "already_executed": done_pre}

        # ---- 3b. 全部低危 → 直接执行 ----
        reply = ""
        done: List[str] = []
        digest = ""
        if allowed:
            try:
                res = self.core.execute_calls(allowed)
                done = [str(c.get("name") or "") for c in allowed]
                digest = self._digest_results(allowed, res)
                reply = ""
            except Exception as ex:
                reply = f"执行的时候出错了：{ex}"
                done = []
        if not reply and digest:
            # ★绝不能把工具返回的原始 JSON 念给用户听。语音里读
            # `{"ok": true, "results": [{"title": ...` 荒谬且难懂。
            # 先让模型把「人话摘要」组织出来；模型不听话就用我们自己
            # 生成的摘要（本身已经是可读的），不让用户听到 JSON。
            reply = self._say_naturally(command, digest)
        if not reply.strip():
            # ★不许说"我办了"——此刻可能一个工具都没执行成功。
            reply = ("这件事我没能动手，也没有拿到能回复你的内容。"
                     "可以换个说法再问我一次。")

        self.stats["asked"] += 1
        self.set_state(VoiceState.SPEAKING)
        audio = self._speak(reply)
        self.set_state(VoiceState.WAITING)
        return {"kind": "ask", "reply": reply, "audio": audio,
                "asr_text": heard, "executed": done}

    def _say_naturally(self, command: str, digest: str) -> str:
        """把执行结果说成人话。失败就退回 digest（已是可读摘要）。"""
        try:
            text = self.core.summarize(command, digest)
        except Exception:
            return digest
        text = (text or "").strip()
        if not text:
            return digest
        # 语音不适合长篇大论：读超过 120 字会被截得很难听
        return text if len(text) <= 120 else text[:118] + "…"


    @staticmethod
    def _digest_results(calls: List[Dict[str, Any]], res: Any) -> str:
        """把工具执行结果压成一句**人类可读**的摘要（要被 TTS 念出来）。

        刻意不返回原始 JSON；而且这条摘要是**唯一被允许**用来汇报
        "我做了什么"的文本——交给模型润色它会臆断（实测把待确认的
        删除说成"文件已删除"）。具体模板见模块级 :func:`_one_line`。
        """
        if not isinstance(res, dict):
            return "已经执行完毕。"
        results = res.get("results") or []
        names = [str(c.get("name") or "") for c in (calls or [])]
        label = "、".join(names) if names else "工具"
        lines: List[str] = []
        for item in results:
            try:
                d = json.loads(item) if isinstance(item, str) else item
            except Exception:
                d = None
            if not isinstance(d, dict):
                lines.append(str(item)[:120])
                continue
            lines.append(_one_line(label, d))
        return " ".join(lines)[:400] if lines else f"{label} 已执行。"

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

# ------------------------------------------------------------ 模块级工具
# 放在文件**末尾**（类体外面）。别再往类体中间插顶层 def——那会让
# 整个类提前结束、后面的方法变孤儿，而 py_compile 照样通过。
# 自检里有守卫（WarriorCore / JarvisRuntime 方法齐全）盯这一条。

_WEEKDAY_CN = {
    "Monday": "星期一", "Tuesday": "星期二", "Wednesday": "星期三",
    "Thursday": "星期四", "Friday": "星期五",
    "Saturday": "星期六", "Sunday": "星期日",
}


def _one_line(label: str, d: Dict[str, Any]) -> str:
    """单个工具结果 → 一句人话（要被 TTS 念出来的）。

    已知返回形状各有模板；不认识就退回「做了什么 + 返回多少内容」，
    宁可平淡也**绝不念 JSON**。
    """
    if d.get("error"):
        return f"{label} 失败：{str(d['error'])[:100]}"
    if d.get("ok") is False:
        return f"{label} 没有成功。"

    # get_time：{ok, timestamp, local, weekday}
    if isinstance(d.get("local"), str) and d.get("weekday"):
        wd = _WEEKDAY_CN.get(str(d.get("weekday")), "")
        return f"现在是 {d['local']} {wd}".strip()
    # web_search：{ok, results:[{title,url}]}
    if isinstance(d.get("results"), list):
        rs = d["results"]
        if not rs:
            return f"{label} 没有搜到结果。"
        titles = []
        for r in rs[:5]:
            if isinstance(r, dict):
                t = str(r.get("title") or r.get("name") or "").strip()
                if t:
                    titles.append(t[:36])
        joined = "；".join(titles)
        return (f"{label} 找到 {len(rs)} 条：{joined}" if joined
                else f"{label} 返回 {len(rs)} 条。")
    # list_tasks / memory_list：{count, items:[]}
    if isinstance(d.get("items"), list):
        n = d.get("count")
        n = n if isinstance(n, int) else len(d["items"])
        return f"{label}：共 {n} 项。" + ("（当前为空）" if n == 0 else "")
    if isinstance(d.get("files"), list):
        return f"{label}：{len(d['files'])} 个文件。"
    # file_info：{path, type}
    if d.get("path") and d.get("type"):
        kind = "目录" if d.get("type") == "dir" else "文件"
        return f"{label}：{kind} {str(d['path'])[-60:]}"
    if isinstance(d.get("text"), str) and d["text"].strip():
        return f"{label}：{d['text'].strip()[:110]}"
    body = json.dumps(d, ensure_ascii=False)
    return f"{label} 已执行，返回 {len(body)} 字节内容。"
