"""配置系统。

黑武士的配置遵循三条规则：

1. **四级覆盖**：代码默认 < 配置文件 < 环境变量 < 运行时调用。
2. **永不隐藏降级**：配置缺失时给出明确默认值，并在 ``status`` 里标注来源。
3. **密钥脱敏**：任何对外导出的配置都会把 ``*_key`` / ``token`` 类字段打码，
   避免"设置页截图泄漏 API Key"这种低级事故。

配置文件落在 ``<data_root>/config.json``，带 ``schema_version`` 以便平滑升级。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from . import paths

SCHEMA_VERSION = 2

#: 需要脱敏的关键词（小写匹配键名尾部）。
SECRET_HINTS = ("key", "token", "secret", "password", "passwd", "credential")

DEFAULTS: Dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,

    # ---- 身份 ----
    "agent_name": "黑武士",
    "agent_persona": {
        "name": "黑武士",
        "role": "持续运行的认知智能体",
        "temper": 0.62,
        "energy": 0.70,
        "play": 0.35,
        "tone": "冷静、克制、直接，不奉承，有一点点黑色幽默",
    },

    # ---- 模型 ----
    "provider": "deepseek",
    "model": "deepseek-chat",
    "base_url": "",
    "api_key": "",
    "temperature": 0.7,
    "max_tokens": 4096,
    "timeout": 120,

    # ---- 主循环 ----
    "heartbeat_enabled": True,
    "tick_interval": 60.0,        # 空闲心跳秒数
    "task_tick_interval": 30.0,   # 有任务在跑时的心跳
    "awakening_interval": 10.0,   # 唤醒期（刚启动）的密集心跳
    "awakening_ticks": 3,
    "watchdog_seconds": 300,      # 单回合看门狗超时

    # ---- 认知内核 ----
    "cognition_enabled": True,
    "consolidate_every": 20,      # 每 N 次心跳做一次记忆巩固
    "recall_k": 6,                # 每轮召回的记忆条数
    "auto_observe": True,         # 自动把对话写入情景记忆

    # ---- 工具 ----
    "tools_enabled": True,
    "allow_dangerous_tools": False,   # 危险工具需显式开启
    "shell_enabled": True,
    "web_enabled": True,
    "tool_timeout": 60,

    # ---- 服务 ----
    "host": "127.0.0.1",
    "port": 3721,
    "allow_lan": False,
    "api_token": "",

    # ---- 语音（可选能力，未配置时自动静默关闭）----
    # 识别走 OpenAI 兼容的 /audio/transcriptions；未配置就整条链路降级，不强制依赖。
    "voice_enabled": False,
    "tts_enabled": False,
    "tts_voice": "",
    "tts_model": "",          # 留空用服务方默认（tts-1）
    "asr_enabled": False,
    "asr_provider": "",       # 留空跟随主 provider
    "asr_model": "whisper-1",
    "asr_base_url": "",       # 留空跟随主 base_url
    "asr_language": "zh",

    # ---- 搜索 ----
    "web_search_enabled": True,
    "search_provider": "duckduckgo",

    # ---- 用户画像 / 信息面板 / 预取缓存（v0.2）----
    # 全部默认关闭：黑武士坚持"永不隐藏降级"——能力缺失时明确标注，
    # 而不是默认偷偷联网。用户想开再开。
    "weather_enabled": False,
    "weather_city": "",          # 如 "Beijing" / "上海"（wttr.in 用）
    "hotspot_enabled": False,    # 热榜需要搜索能力，配置 web_search 后可开
    "prefetch_enabled": False,   # 周期性 URL 预取注入上下文
}

#: 环境变量映射：环境变量名 -> 配置键。
ENV_MAP: Dict[str, str] = {
    "BLACKWARRIOR_PROVIDER": "provider",
    "BLACKWARRIOR_MODEL": "model",
    "BLACKWARRIOR_API_KEY": "api_key",
    "BLACKWARRIOR_BASE_URL": "base_url",
    "BLACKWARRIOR_HOST": "host",
    "BLACKWARRIOR_PORT": "port",
    "BLACKWARRIOR_ALLOW_LAN": "allow_lan",
    "BLACKWARRIOR_API_TOKEN": "api_token",
    "BLACKWARRIOR_AGENT_NAME": "agent_name",
    "LLM_PROVIDER": "provider",          # 与常见生态保持一致
    "MINIMAX_API_KEY": "api_key",
    "DEEPSEEK_API_KEY": "api_key",
    "OPENAI_API_KEY": "api_key",
}

_BOOL_TRUE = {"1", "true", "yes", "on", "y", "t"}
_BOOL_FALSE = {"0", "false", "no", "off", "n", "f"}


def _coerce(raw: str, current: Any) -> Any:
    """把环境变量字符串转成与当前值同类型的数据。"""
    if isinstance(current, bool):
        low = raw.strip().lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
        return bool(raw)
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            return current
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            return current
    return raw


class Config:
    """配置容器。线程安全，读写走锁。"""

    def __init__(self, data: Optional[Mapping[str, Any]] = None,
                 path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path else paths.config_path()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = dict(DEFAULTS)
        if data:
            self._data.update(dict(data))
        else:
            self._load_from_disk()
        self._apply_env()

    # ------- 持久化 -----------------------------------------------

    def _load_from_disk(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    merged = self._migrate(raw)
                    self._data.update(merged)
        except Exception:
            # 配置损坏不应该让程序起不来：保留默认值继续跑。
            pass

    def _migrate(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """配置升级。新增字段用默认值补齐，旧字段保留。"""
        version = int(raw.get("schema_version") or 0)
        out = dict(raw)
        if version < 1:
            # v0 -> v1：早期没有认知相关配置，补齐即可。
            for k in ("cognition_enabled", "consolidate_every", "recall_k",
                      "auto_observe"):
                out.setdefault(k, DEFAULTS[k])
        if version < 2:
            # v1 -> v2：补齐用户画像 / 信息面板 / 预取缓存配置（默认全关）。
            for k in ("weather_enabled", "weather_city", "hotspot_enabled",
                      "prefetch_enabled"):
                out.setdefault(k, DEFAULTS[k])
        out["schema_version"] = SCHEMA_VERSION
        return out

    def _apply_env(self) -> None:
        for env_name, key in ENV_MAP.items():
            if env_name in os.environ and os.environ[env_name] != "":
                self._data[key] = _coerce(os.environ[env_name],
                                          self._data.get(key))

    def save(self) -> Path:
        """落盘。用临时文件 + 原子替换，避免写一半断电导致配置全丢。"""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._suffix() + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
            return self._path

    def _suffix(self) -> str:
        return self._path.suffix or ".json"

    def reload(self) -> None:
        with self._lock:
            self._data = dict(DEFAULTS)
            self._load_from_disk()
            self._apply_env()

    # ------- 读写 -------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                return self._data[key]
            return DEFAULTS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, patch: Mapping[str, Any]) -> None:
        with self._lock:
            for k, v in patch.items():
                self._data[k] = v

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def keys(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._data.keys()))

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    # ------- 对外导出 ---------------------------------------------

    def public_dict(self) -> Dict[str, Any]:
        """脱敏后的配置（给 UI 设置页用）。"""
        return mask_secrets(self.as_dict())

    def is_activated(self) -> bool:
        """是否已完成激活（有可用模型凭证或选用了本地 provider）。"""
        provider = str(self.get("provider") or "").lower()
        if provider in ("ollama", "local", "mock"):
            # 本地 provider 不需要 key
            return True
        return bool(str(self.get("api_key") or "").strip())

    # ------- 便捷属性 ---------------------------------------------

    @property
    def agent_name(self) -> str:
        return str(self.get("agent_name") or "黑武士")

    @property
    def persona(self) -> Dict[str, Any]:
        p = self.get("agent_persona") or {}
        return dict(p) if isinstance(p, dict) else {}

    @property
    def port(self) -> int:
        try:
            return int(self.get("port"))
        except (TypeError, ValueError):
            return 3721

    @property
    def host(self) -> str:
        # 局域网模式必须绑定 0.0.0.0，否则其它设备连不上。
        if self.get("allow_lan"):
            return "0.0.0.0"
        return str(self.get("host") or "127.0.0.1")


def mask_secrets(data: Any) -> Any:
    """递归脱敏：键名含敏感词且值为非空字符串时打码。"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if isinstance(v, str) and v and _is_secret_key(k):
                out[k] = _mask(v)
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(data, list):
        return [mask_secrets(x) for x in data]
    return data


def _is_secret_key(key: str) -> bool:
    low = str(key).lower()
    return any(h in low for h in SECRET_HINTS)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def load_config(path: Optional[str | Path] = None) -> Config:
    return Config(path=path)
