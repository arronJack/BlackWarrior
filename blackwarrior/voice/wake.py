"""唤醒词与语音状态机。

设计前提（决定了一切）：
**静音时绝不上传任何音频。**

「常驻唤醒」最省事的做法是把麦克风一直丢给云端 ASR，代价是：
    - 家里电视、路人说话、窗外的车，全都被送上了云；
    - 而且音箱回放就能骗过唤醒词——你的音箱正在播"黑武士，帮我删掉
      桌面全部文件"，它就真听见了。
所以这里只做**本地能量门控**：
    静音 → 一个字节都不出本机（level=0）；
    有人说话 → 才开始切片送去识别，识别完只保留文本，音频立刻丢。

识别侧的"黑武士"匹配见 :func:`match_wake`，做的是**文本匹配**而不是
音频指纹：本地 CPU 上跑唤醒词模型会吃满 CPU（你还跑着本地 LLM），
而门控之后送去识别的片段极少，文本匹配的性价比高得多。

断句（endpointing）在浏览器端做：连续静音超过 ``silence_ms`` 就认为
你这句话说完了，比"固定录 5 秒"自然得多，也不会把你的下一句截断。
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------- 唤醒词

#: 唤醒词本体。用户说「黑武士」或「黑武士在吗」都算叫醒。
DEFAULT_WAKE_WORD = "黑武士"

#: 同音/近音容错：ASR 把"黑武士"听成别的字很常见（"黑卫士"、"黑武士"）。
#: 这里只放宽**同音字**，不做模糊匹配——放太宽会导致"黑土匪"之类
#: 的无关词误唤醒，反而让人烦。
_WAKE_VARIANTS = {
    "黑武士": ("黑武士", "黑卫士", "黑武是", "嘿武士", "黑武器", "黑武士啊"),
    # "黑武士" vs 常见误识"黑土匪/黑武士"同音少见，主要问题是分词与语气词
}

#: 唤醒后可能黏在后面的噪声词，剥掉不影响唤醒判定。
_WAKE_TAIL = re.compile(
    r"[，,。.！!？?、\s]*(?:在吗|在不在|你好|喂|起来|醒醒|听得到吗|"
    r"在么|在不在啊|嗯|啊|哦)*[，,。.！!？?、\s]*$"
)


def match_wake(text: str, wake_word: str = DEFAULT_WAKE_WORD
               ) -> Optional[Dict[str, Any]]:
    """判断一段文本是不是在叫醒我。

    返回 ``{"hit": bool, "rest": str, "word": str, "mode": str}``：
    ``rest`` 是**剥掉唤醒词后剩下的指令**（"黑武士，搜一下今天的国内新闻"
    → rest="搜一下今天的国内新闻"），后续直接把它当普通输入送去思考。

    ``mode`` 说明是靠什么命中的，写进事件流里便于排查
    （实测 ASR 到底把"黑武士"听成了什么字）。
    """
    raw = (text or "").strip()
    if not raw:
        return {"hit": False, "rest": "", "word": wake_word, "mode": "empty"}
    norm = _normalize(raw)
    variants = _WAKE_VARIANTS.get(wake_word, (wake_word,))

    for v in variants:
        vn = _normalize(v)
        if norm == vn:
            return {"hit": True, "rest": "", "word": v, "mode": "exact"}
        if norm.startswith(vn):
            rest = _strip_prefix(raw, v)
            return {"hit": True, "rest": rest, "word": v, "mode": "prefix"}

    # 「嘿」「喂」这类唤醒前缀 + 稍后才出现的名字，允许中间夹语气词。
    # 只在句首位置判断，避免正文里出现"黑武士"就误触发。
    m = re.match(r"^(?:嘿|喂|哎|诶|那个|喂喂)\s*(.{1,12}?)\s*$", norm)
    if m:
        tail = m.group(1)
        for v in variants:
            vn = _normalize(v)
            if vn in tail or tail in vn and len(tail) >= 2:
                return {"hit": True, "rest": "", "word": v, "mode": "prefix_intro"}

    # 名字在句中出现（例如「那个谁，黑武士」），只在很短且整体像唤醒
    # 的句子里放宽，避免长句里提到"黑武士"就误触发。
    if len(norm) <= 14:
        for v in variants:
            if _normalize(v) in norm:
                head, _, _tail2 = norm.partition(_normalize(v))
                if len(head) <= 4:
                    return {"hit": True,
                            "rest": _rest_after_chars(raw, head and len(head) + len(v)),
                            "word": v, "mode": "loose"}

    # ★同音容错的最后一道：ASR 把"黑武士"听成"黑午市"是常态而非意外。
    # 精确匹配全部落空后才走这里，所以不会放宽到乱唤醒的程度。
    hit, _rest, v, mode = _fuzzy_wake(norm, variants)
    if hit:
        return {"hit": True, "rest": _rest_after_chars(raw, len(v)),
                "word": v, "mode": mode}
    return {"hit": False, "rest": raw, "word": wake_word, "mode": "none"}


def _rest_after_chars(raw: str, n: int) -> str:
    """丢掉原始文本里前 ``n`` 个「有效字符」之后剩下的内容。

    不能简单按 ``n`` 切片原文——因为匹配是在**归一化后**（去标点、
    繁转简）做的，原文里可能有标点和繁体字。必须走一遍同样的归一化
    规则数有效字符，才知道该从哪里切。

    切出来还**统一转成简体**：这段文字接下来要交给 LLM、要在面板上显示，
    让下游自己处理繁体是甩锅（实测 ASR 默认吐繁体）。
    """
    rest = ""
    if n <= 0:
        rest = raw
    else:
        cnt = 0
        for i, ch in enumerate(raw.translate(_T2S).lower()):
            if ch.isalnum():
                cnt += 1
                if cnt >= n:
                    rest = raw[i + 1:]
                    break
    out = _WAKE_TAIL.sub("", rest).strip("，,。. 　")
    return out.translate(_T2S)


#: 同音字组：ASR 把"黑武士"听成"黑午市"是**必然会发生**的，不是意外——
#: 实测（TTS 合成标准音 → faster-whisper 识别）稳定得到"黑午市"。
#: 所以这里不做死板的变体枚举，而是按**同音组**容忍替换：只要错的那个字
#: 与原字同音，就算命中。
#:
#: 代价是必须防住"形近但不同音"的误唤醒（"黑土匪"含"黑"却不该唤醒），
#: 见 :func:`_homophone_mismatch` 与替换次数上限。
_HOMOPHONE_GROUPS = (
    {"武", "午", "物", "五", "舞", "伍", "捂"},
    {"士", "市", "是", "事", "式", "试", "视", "室", "示", "氏"},
    {"黑", "嘿"},
    {"卫", "位", "为", "威", "喂", "微"},
    {"斯", "思", "死", "四", "司"},
)

_HOMOPHONE_INDEX: Dict[str, int] = {}
for _gi, _grp in enumerate(_HOMOPHONE_GROUPS):
    for _ch in _grp:
        _HOMOPHONE_INDEX.setdefault(_ch, _gi)

#: 繁体→简体。ASR 默认吐繁体（实测"幫我搜一下今天的國內新聞"），
#: 不转换的话唤醒词之外的比较全在比两种字形。这里只收唤醒/指令里
#: 真正常出现的字，不引入 opencc 这种重依赖。
_T2S = str.maketrans({
    "幫": "帮", "國": "国", "內": "内", "聞": "闻", "這": "这", "個": "个",
    "們": "们", "時": "时", "說": "说", "會": "会", "對": "对", "現": "现",
    "開": "开", "關": "关", "動": "动", "務": "务", "數": "数", "據": "据",
    "電": "电", "話": "话", "機": "机", "腦": "脑", "網": "网", "東": "东",
    "車": "车", "門": "门", "問": "问", "題": "题", "讀": "读", "寫": "写",
    "書": "书", "買": "买", "賣": "卖", "錢": "钱", "銀": "银", "鐵": "铁",
    "長": "长", "短": "短", "風": "风", "飛": "飞", "電": "电", "傳": "传",
    "輸": "输", "線": "线", "經": "经", "濟": "济", "產": "产", "業": "业",
    "員": "员", "報": "报", "紙": "纸", "廣": "广", "播": "播", "節": "节",
    "目": "目", "視": "视", "聽": "听", "見": "见", "覺": "觉", "應": "应",
    "該": "该", "讓": "让", "給": "给", "從": "从", "來": "来", "後": "后",
    "點": "点", "擊": "击", "雙": "双", "邊": "边", "級": "级", "類": "类",
})


def _normalize(s: str) -> str:
    """去掉标点空白、全角转半角、繁转简，让匹配不受格式影响。"""
    s = (s or "").translate(_T2S).strip().lower()
    return "".join(ch for ch in s if ch.isalnum())


def _homophone_mismatch(a: str, b: str) -> bool:
    """两个字是否算"错得不该算命中"。同音组内不算错。"""
    if a == b:
        return False
    gi = _HOMOPHONE_INDEX.get(a)
    return gi is None or gi != _HOMOPHONE_INDEX.get(b)


def _fuzzy_wake(norm_text: str, variants) -> tuple:
    """同音容忍的唤醒匹配。返回 ``(是否命中, 剩余原文, 命中词, 模式)``。

    容忍上限 = ``max(1, len//3)``（3 字词容忍 1 个字错）。这条线是安全
    底线：实测"黑午市"两个错字都与原字同音，必须放行；而"黑土匪"只有
    1 个字同音、1 个字不同音，会被拒——不然后台放着歌的歌词里出现
    "黑"字就唤醒，用两天就没人想用了。
    """
    best: Optional[tuple] = None
    for v in variants:
        vn = _normalize(v)
        if not vn:
            continue
        # ★只比对**句首那一段**，不是整句。唤醒词后面通常还跟着指令
        # （"黑午市，帮我搜一下今天的国内新闻" 归一化后有 12 个字），
        # 拿 3 字词去比整句必然超长失配。
        head = norm_text[:len(vn)]
        if not head:
            continue
        for off in (0, 1):
            tgt = vn[off:off + len(head)]
            if not tgt or abs(len(tgt) - len(head)) > 1:
                continue
            mismatch = sum(1 for x, y in zip(tgt, head)
                           if _homophone_mismatch(x, y))
            # 三字唤醒词要求至少 2 个字同音对上，且只允许 1 个字错。
            # 这条线是实测调出来的：
            #   "黑午市"（3/3 同音）→ 放行，ASR 的常态错字
            #   "黑土"  （1/2 同音）→ 拒绝，形近不同音的误唤醒
            #   "黑"    （1/1 但词长不足）→ 拒绝，咳嗽/背景音
            need = len(vn) - 1
            if (len(head) >= need and len(tgt) >= need
                    and (len(head) - mismatch) >= need
                    and mismatch <= 1
                    and (best is None or mismatch < best[0])):
                best = (mismatch, off, v)
    if best is None:
        return (False, "", "", "none")
    mismatch, off, v = best
    return (True, "", v, f"fuzzy{mismatch}")


def _strip_prefix(raw: str, prefix: str) -> str:
    """从原始文本里去掉唤醒词，保留标点与剩余内容。"""
    i = raw.find(prefix)
    if i < 0:
        # 原文可能有标点差异，退化为"按比例截掉开头"
        return raw[len(prefix):]
    rest = raw[i + len(prefix):]
    return _WAKE_TAIL.sub("", rest).strip("，,。. 　")


# ---------------------------------------------------------------- 授权分级

#: 低危：可逆、只读、影响范围限于本机且立刻可撤销。
#: 这一档允许**语音直接执行**，不打断——贾维斯感就来自这里不啰嗦。
LOW_RISK = "low"

#: 高危：不可逆或会外发/花钱。
#: 语音只负责"进入待确认"，**必须人在面板上点一下**才真执行。
#: 理由见模块开头：唤醒词能被音箱/电视回放骗过，所以语音授权不能作为
#: 唯一凭据。
HIGH_RISK = "high"

#: 高危**精确名单**（需人工点击确认）。
#:
#: 为什么用精确名单而不是"关键词包含"：第一版用 substring 匹配，
#: 结果把``list_tasks`` / ``list_reminders`` / ``autostart_status`` /
#: ``find_command`` 全判成高危——它们都是只读的。免提模式里每问一句
#: 就要点一次确认，用两次就没人用了。
#:
#: 名单是照着工具注册表里自带的 risk 字段逐个定的：
#:   danger 档 → 全要确认（delete_file / run_command / which）
#:   caution 档里**会改变系统或对外**的 → 要确认
#:   其余（safe 档、纯查询类）→ 语音直接放行
_HIGH_RISK_EXACT = {
    # —— danger 档：不可逆或直接执行外部程序 ——
    "delete_file": "删除文件不可逆",
    "run_command": "执行 Shell 命令",
    # —— caution 档里会改状态 / 对外 / 花钱的 ——
    "write_file": "写入会覆盖已有内容",
    "grant_access": "扩大权限范围",
    "revoke_access": "变更权限范围",
    "setup_autostart": "会改开机行为",
    "cancel_autostart": "会改开机行为",
    "open_app": "会在你电脑上启动应用",
    "set_reminder": "会产生外部提醒",
    "memory_write": "会写入长期记忆",
    "memory_consolidate": "会改写已有记忆",
}

#: 未知工具名的兜底特征。只在**不在**精确名单里时生效，
#: 用来防"以后新增了危险工具却忘了登记"。
_HIGH_RISK_PATTERNS = (
    ("delete", "删除操作不可逆"),
    ("rmdir", "删除目录不可逆"),
    ("rename", "重命名会打断你正在用的东西"),
    ("exec", "执行外部程序"),
    ("shell", "执行系统命令"),
    ("uninstall", "卸载会改变系统"),
    ("format", "会格式化磁盘"),
    ("shutdown", "会关机/重启"),
    ("pay", "涉及花钱"),
    ("transfer", "涉及资金转账"),
)

#: 明确低危的查询类工具（含复数形式，第一版漏了这些导致误判）。
_LOW_RISK_EXACT = {
    "list_tools", "find_tool", "list_workspaces", "get_time", "get_status",
    "get_panel", "get_profile", "web_search", "web_read", "web_headlines",
    "open_url", "read_file", "list_dir", "file_info", "text_stats",
    "which", "find_command", "check_port", "environment", "system_probe",
    "dev_env", "diagnose_network", "list_software", "list_reminders",
    "list_tasks", "task_current", "autostart_status", "memory_list",
    "memory_recall", "memory_stats", "memory_audit", "list_clues",
    "link_clue", "list_media", "media_stats", "probe_media", "scan_media",
    "add_media_root", "remove_media_root", "list_prefetch", "add_prefetch",
    "push_background", "remember_feedback", "set_profile", "set_tick_interval",
}


def classify_tool(name: str) -> tuple:
    """给工具定级，返回 ``(等级, 理由)``。

    判定顺序：低危白名单 → 高危精确名单 → 高危特征 → MCP → 默认低危。
    默认低危是刻意的：绝大多数工具是只读的，让每个都要点确认会把
    免提变成点按地狱。真正危险的那十来个由名单兜住，新来的外部工具
    （MCP）一律按高危——外部服务器今天返回只读、明天可能改。
    """
    n = (name or "").strip()
    low = n.lower()
    if low in _LOW_RISK_EXACT:
        return (LOW_RISK, "只读查询")
    if low in _HIGH_RISK_EXACT:
        return (HIGH_RISK, _HIGH_RISK_EXACT[low])
    if low.startswith("mcp__"):
        return (HIGH_RISK, "来自外部 MCP 服务器，参数不可预判")
    for frag, why in _HIGH_RISK_PATTERNS:
        if frag in low:
            return (HIGH_RISK, why)
    return (LOW_RISK, "只读操作")


def gate_calls(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一轮工具调用做授权分流。

    返回 ``{"allowed": [...], "pending": [...]}``：
    - ``allowed``  低危，直接执行；
    - ``pending``  高危，转面板等确认。
    """
    allowed: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for c in calls or []:
        name = (c or {}).get("name", "")
        level, why = classify_tool(name)
        if level == HIGH_RISK:
            pending.append({"name": name, "args": (c or {}).get("arguments", {}),
                            "reason": why})
        else:
            allowed.append(c)
    return {"allowed": allowed, "pending": pending}


# ---------------------------------------------------------------- 状态机


class VoiceState:
    """面板/循环共用的五态。字符串常量集中在这里，避免各处写错字。"""

    IDLE = "idle"            # 静默：一条直线
    WAITING = "waiting"      # 门控开着，等你说话：微弱起伏
    LISTENING = "listening"  # 正在接收语句
    THINKING = "thinking"    # 在思考：波形内缩、缓慢脉动
    SPEAKING = "speaking"    # 在说话：随音量起伏的活跃波
    CONFIRM = "confirm"      # 等你点确认：明显区别于其它态

    ALL = (IDLE, WAITING, LISTENING, THINKING, SPEAKING, CONFIRM)


class TurnSegmenter:
    """把连续音频切成"一句一句"。

    纯本地判定，只看音量与静音时长，不做任何识别：
    - ``speech`` 上阈值持续 ``onset_ms`` → 进入讲话；
    - ``speech`` 下阈值持续 ``silence_ms`` → 句子结束，触发断句。

    双阈值（迟滞）很重要：只用单阈值的话，句中换气会被切一刀，
    说出的长句被截成两半送去识别，语义就断了。
    """

    def __init__(self, *, onset_ms: int = 220, silence_ms: int = 750,
                 onset_db: float = -42.0, speech_db: float = -34.0,
                 min_speech_ms: int = 240, max_utterance_ms: int = 15000,
                 tail_ms: int = 320) -> None:
        self.onset_ms = onset_ms
        self.silence_ms = silence_ms
        self.onset_db = onset_db
        self.speech_db = speech_db
        self.min_speech_ms = min_speech_ms
        self.max_utterance_ms = max_utterance_ms
        self.tail_ms = tail_ms
        self.reset()

    def reset(self) -> None:
        self.speaking = False
        self._voice_ms = 0
        self._quiet_ms = 0
        self._started_at = 0.0
        self._frames = 0

    def push(self, level: float, now_ms: float = 0.0) -> Dict[str, Any]:
        """喂一帧音量，返回状态事件。

        ``level`` 是0..1 的归一化音量。返回值可能带
        ``{"event": "onset"|"end"|"timeout", "duration_ms": int}``。
        """
        db = _to_db(level)
        now = now_ms or (time.time() * 1000.0)
        ev: Dict[str, Any] = {}

        if not self.speaking:
            if db >= self.speech_db or (self._voice_ms > 0 and db >= self.onset_db):
                self._voice_ms += 1000.0 / 50.0   # 以 50fps 为基准折算
                if self._voice_ms >= self.onset_ms and now - self._started_at < 2000:
                    self.speaking = True
                    self._started_at = now
                    self._frames = 1
                    self._voice_ms = 0
                    self._quiet_ms = 0
                    ev = {"event": "onset"}
            else:
                self._voice_ms = 0
                if now - self._started_at >= 2000:
                    self._started_at = now
        else:
            self._frames += 1
            if db >= self.onset_db:
                self._voice_ms += 20.0
                self._quiet_ms = 0
            else:
                self._voice_ms = 0
                self._quiet_ms += 20.0
            if self._quiet_ms >= self.silence_ms:
                dur = now - self._started_at
                self.speaking = False
                self._started_at = now
                self._voice_ms = self._quiet_ms = 0
                # 太短的"蹦一下"多半是咳嗽/敲桌子，不值得送去识别花钱
                if dur >= self.min_speech_ms:
                    ev = {"event": "end", "duration_ms": int(dur)}
                else:
                    ev = {"event": "discard", "duration_ms": int(dur)}
            elif (now - self._started_at) >= self.max_utterance_ms:
                dur = now - self._started_at
                self.speaking = False
                self._started_at = now
                self._voice_ms = self._quiet_ms = 0
                # 兜底上限：一直不停地说也必须在 15 秒后切一刀，
                # 否则会攒出一个超长音频，既超 ASR 时限又难识别。
                ev = {"event": "timeout", "duration_ms": int(dur)}

        ev["speaking"] = self.speaking
        ev["level"] = level
        ev["db"] = round(db, 1)
        return ev


def _to_db(level: float) -> float:
    """把0..1 音量换成 dB，用于双阈值比较。"""
    v = max(1e-5, min(1.0, float(level or 0.0)))
    import math
    return 20.0 * math.log10(v)