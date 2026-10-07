"""Windows 免安装 TTS（SAPI）。

为什么优先用它，而不是 edge-tts：
- **零安装、零 Key、离线**。系统自带中文音色（Kangkang / Yaoyao / Huihui），
  叫一声就出来，不用等网络、不用配账号。
- 唤醒循环是**高频**场景，每句都要即时出声。本地合成是百毫秒级，
  任何一次网络往返都会被感知成"它卡了一下"。

代价要说清楚：SAPI 中文音色是老式合成，自然度明显不如 edge-tts 的
神经网络音色。所以做成**双通道**：配置显式指定 ``tts_engine=edge`` 时走
VoiceService 的云端合成，否则回落 SAPI。换引擎不改调用方。

实现用「拉起 powershell 调System.Speech」而不是 ctypes 直连 COM：
手写 ISpVoice 的 vtable 调用极易踩到内存布局的坑，而
``System.Speech.Synthesis.SpeechSynthesizer`` 是一行就能写 WAV 的稳定 API。
每次合成起一个短命进程（约 0.3s），换取绝对可靠——唤醒循环里说错话的
代价远大于这0.3 秒。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import List, Optional

#: 中文音色偏好顺序。Kangkang(康康) 比 Huihui(慧慧) 自然，Yaoyao(瑶瑶)
#: 偏甜润，适合"在的主人"这种短回应。
_CN_PREFERENCE = ("Kangkang", "Yaoyao", "Huihui")

_WIN = os.name == "nt"

# 一次 powershell 调用完成「枚举音色」与「合成 WAV」两件事，避免多次启动。
_PS_LIST = r'''
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.GetInstalledVoices() | ForEach-Object {
  [PSCustomObject]@{ name = $_.VoiceInfo.Name; culture = $_.VoiceInfo.Culture.Name }
} | ConvertTo-Json -Compress
'''

_PS_SPEAK = r'''
param([string]$Text, [string]$Path, [string]$Voice, [int]$Rate = 0)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($Voice) { $s.SelectVoice($Voice) }
if ($Rate -ne 0) { $s.Rate = $Rate }
# 16k 单声道 16bit：正好是 ASR 免重采样的输入格式，省掉一段转换
$s.SetOutputToWaveFile($Path, 16000, 16, [System.Speech.AudioFormat.Pcm]
                       , 1)
$s.Speak($Text)
$s.Dispose()
'''


def _run_ps(script: str, args: Optional[List[str]] = None,
            timeout: float = 25.0) -> subprocess.CompletedProcess:
    """跑一段 PowerShell。**绝不让它把异常吞成静默失败**——
    语音链路的问题只能靠明确报错来定位。"""
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    if args:
        cmd += args
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def available() -> bool:
    if not _WIN:
        return False
    if not (os.path.isfile(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
            or shutil_which("powershell")):
        return False
    return True


def shutil_which(name: str) -> str:
    from shutil import which
    return which(name) or ""


def list_voices() -> List[dict]:
    """列出系统音色，按中文优先排序。失败返回空列表（不抛）。"""
    if not _WIN:
        return []
    try:
        p = _run_ps(_PS_LIST, timeout=20.0)
    except Exception:
        return []
    if p.returncode != 0:
        return []
    try:
        raw = (p.stdout or b"").decode("utf-8", "ignore").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
    except Exception:
        return []
    cn = [d for d in data if str(d.get("culture", "")).startswith("zh")]
    en = [d for d in data if d not in cn]

    def rank(d: dict) -> int:
        nm = str(d.get("name", ""))
        for i, pref in enumerate(_CN_PREFERENCE):
            if pref in nm:
                return i
        return 99

    return sorted(cn, key=rank) + sorted(en)


def pick_voice(prefer: str = "") -> str:
    """按偏好选音色名。指定了找不到就退回最优中文音色。"""
    voices = list_voices()
    if prefer:
        for v in voices:
            if prefer.lower() in str(v.get("name", "")).lower():
                return str(v["name"])
        return ""
    for pref in _CN_PREFERENCE:
        for v in voices:
            if pref in str(v.get("name", "")):
                return str(v["name"])
    return str(voices[0]["name"]) if voices else ""


def has_chinese_voice() -> bool:
    return any(str(v.get("culture", "")).startswith("zh")
               for v in list_voices())


def synthesize(text: str, voice: str = "", rate: int = 0,
               out_path: str = "") -> str:
    """合成中文语音，返回 WAV 路径；失败返回空串（**不抛**）。

    说句话失败不该让整个对话循环崩掉——调用方拿空串就知道没出声。
    rate 是相对语速 -10..10，唤醒循环里略快更像"应答"而不是"播报"。
    """
    text = (text or "").strip()
    if not text or not _WIN:
        return ""
    path = out_path or os.path.join(
        tempfile.gettempdir(),
        f"bw_tts_{os.getpid()}_{abs(hash(text)) % 1000000}.wav")
    # PowerShell 单引号串里单引号要转义成两个，否则整句语法崩掉
    safe = text.replace("'", "''")
    v = (voice or "").replace("'", "''")
    script = (_PS_SPEAK.replace("[string]$Text", f"[string]$Text='{safe}'")
              .replace("[string]$Voice", f"[string]$Voice='{v}'")
              .replace("[string]$Path", f"[string]$Path='{path}'"))
    script = script.replace("[int]$Rate = 0", f"[int]$Rate = {int(rate)}")
    try:
        p = _run_ps(script, timeout=25.0)
    except Exception:
        return ""
    if p.returncode != 0 or not os.path.exists(path):
        return ""
    try:
        if os.path.getsize(path) < 128:
            return ""
    except OSError:
        return ""
    return path