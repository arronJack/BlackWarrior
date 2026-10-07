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

SCHEMA_VERSION = 7

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
    "hotspot_enabled": False,    # v0.6.7 起接真实榜单源（60s API，免 key）
    "hotspot_source": "weibo",   # weibo / zhihu / bili / douyin / news
    "hotspot_base": "https://60s.viki.moe",  # 可换成自部署 60s 地址
    "prefetch_enabled": False,   # 周期性 URL 预取注入上下文

    # ---- PASM V2 十九层认知底座（v0.3）----
    # V2 需要 numpy + pasm2（`pip install pasm2`），是**可选增强**：
    # 没装就降级到 V1/内置，
    # 并在「心智」页明确标注，绝不把降级伪装成正常。
    "pasm2_enabled": True,
    "pasm2_profile": "full",     # minimal / standard / full / brainwide
    "pasm2_tool_gate": True,     # 用 V2 安全层做工具调用前的内核级闸门
    "pasm2_verify_claims": False,  # 输出前对结论性声明做逻辑层校验（更严，也更慢）

    # ---- 本地语义嵌入（v0.4）----
    # 象量从"确定性哈希（无语义）"升级为"从本地语料学的真语义"。
    # hash  = 纯标准库兜底，零依赖但无语义（v0.3 的行为）
    # lsa   = numpy PPMI+SVD，从黑武士见过的文本里学，完全离线（推荐）
    # onnx  = 装了本地 ONNX 模型时用，质量最高
    "embedding_backend": "auto",   # auto / hash / lsa / onnx
    "embedding_model": "",         # onnx 后端的模型路径（留空=不用 onnx）
    "embedding_persist": True,     # 把学到的语义落盘，重启不丢

    # ---- 本地媒体库（v0.5）----
    # 纯标准库实现，打开即有；关掉则连工具都不注册（模型看不到）。
    "media_enabled": True,
    "media_auto_scan": False,# 启动时自动扫一次（默认关：大目录首次扫要几十秒）

    # ---- 任务续跑（v0.6）----
    # 关掉则连工具都不注册，模型看不到任务概念。
    "tasks_enabled": True,

    # ---- 资源感知与诊断（v0.6）----
    # list_software 读注册表/包管理器数据库，在个别环境可能较慢；
    # 如不需要可关掉（关掉则工具不注册，模型看不到）。
    "sysinfo_enabled": True,

    # ---- 真实世界访问权（v0.7.0：「让黑武士住进你的电脑」）----
    # allowed_paths：授权 Agent 读写的**真实目录**（桌面/文档/项目…）。
    #   里面的读写不需要危险授权；越出这些目录仍受沙箱+授权双重保护。
    #   可由用户手动填，也可在对话里让黑武士自调用 grant_access 授权。
    "allowed_paths": [],
    # full_fs_access：放开整台机器（危险）。仅在完全可信的自用环境开启，
    #   开启后 shell / 文件工具不再受路径边界限制。
    "full_fs_access": False,

    # ---- MCP 外部工具生态（v0.7.0）----
    # 声明要接入的 MCP 服务器，接进来的工具会自动注册成黑武士的工具。
    #   stdio：{"name":"fs","transport":"stdio","command":["npx","-y",
    #           "@modelcontextprotocol/server-filesystem","C:\\"]}
    #   http  ：{"name":"remote","transport":"http","url":"http://127.0.0.1:8931/mcp"}
    "mcp_enabled": True,
    "mcp_servers": [],
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
        # 损坏状态：任何构造路径都要有这两个属性，否则 status / public_dict
        # 在"配置是内存传入"的路径下会 AttributeError（自检才发现的坑）。
        self.corrupted: Optional[str] = None
        self.corrupted_detail: str = ""
        self.recovered_from_backup: bool = False
        if data:
            self._data.update(dict(data))
        else:
            self._load_from_disk()
        self._apply_env()

    # ------- 持久化 -----------------------------------------------

    def _load_from_disk(self) -> None:
        """读配置。**主文件坏了自动回退到 .bak**（2026-10-07 加）。

        事故背景：蓝屏打断写入，config.json 变成 2001 字节全 NUL，
        API Key 一起丢失、实例静默退回未激活——用户只看到"模型不可用"，
        不知道配置已经损坏。这里把"配置坏了"和"没配过"区分开：
        能恢复就自动恢复（并留证据），恢复不了才用默认值，并在
        ``corrupted`` 标记里如实说明。
        """
        self.corrupted: Optional[str] = None
        self.recovered_from_backup: bool = False
        for candidate in (self._path,
                          self._path.with_suffix(self._path.suffix + ".bak")):
            try:
                if not candidate.exists():
                    continue
                raw = candidate.read_text(encoding="utf-8")
                if not raw.strip():
                    raise ValueError("配置文件是空的")
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError(f"顶层不是对象（{type(parsed).__name__}）")
                merged = self._migrate(parsed)
                self._data.update(merged)
                if candidate != self._path:
                    self.recovered_from_backup = True
                    self.corrupted = (f"{self._path} 已损坏"
                                      f"（{self.corrupted_detail or '内容非法'}），"
                                      f"已从 {candidate.name} 自动恢复")
                return
            except FileNotFoundError:
                continue
            except Exception as ex:
                self.corrupted_detail = f"{type(ex).__name__}: {ex}"
                self.corrupted = (f"配置读取失败：{self._path}"
                                  f"（{type(ex).__name__}: {ex}）")
                continue
        # 都没有：第一次运行（正常）或全部损坏（异常，如实标注）

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
            # v0.6.7：热点真实榜单源配置
            for k in ("hotspot_source", "hotspot_base"):
                out.setdefault(k, DEFAULTS[k])
        if version < 3:
            # v2 -> v3：补齐 PASM V2 十九层认知底座配置。
            for k in ("pasm2_enabled", "pasm2_profile", "pasm2_tool_gate",
                      "pasm2_verify_claims"):
                out.setdefault(k, DEFAULTS[k])
        if version < 4:
            # v3 -> v4：补齐本地语义嵌入配置。
            for k in ("embedding_backend", "embedding_model",
                      "embedding_persist"):
                out.setdefault(k, DEFAULTS[k])
        if version < 5:
            # v4 -> v5：补齐本地媒体库配置。
            for k in ("media_enabled", "media_auto_scan"):
                out.setdefault(k, DEFAULTS[k])
        if version < 6:
            # v5 -> v6：补齐任务续跑与资源感知配置。
            for k in ("tasks_enabled", "sysinfo_enabled"):
                out.setdefault(k, DEFAULTS[k])
        if version < 7:
            # v6 -> v7：真实世界访问权 + MCP 外部工具生态。
            # allowed_paths 必须显式给默认值 []，不能继承旧配置的隐式沙箱——
            # 这里保持空列表，由用户/对话里 grant_access 逐步授权。
            for k in ("allowed_paths", "full_fs_access",
                      "mcp_enabled", "mcp_servers"):
                out.setdefault(k, DEFAULTS[k])
        out["schema_version"] = SCHEMA_VERSION
        return out

    def _apply_env(self) -> None:
        for env_name, key in ENV_MAP.items():
            if env_name in os.environ and os.environ[env_name] != "":
                self._data[key] = _coerce(os.environ[env_name],
                                          self._data.get(key))

    def save(self) -> Path:
        """落盘。**先验证再替换**，并保留上一份好配置做备份。

        为什么要这么讲究（2026-10-07 真实事故）：
        配置里存着 API Key，是这台机器上最难重建的一个值。原实现只做
        「写 tmp → os.replace」，看着已经原子了，但实测仍然出现过
        **2001 字节全为 NUL 的 config.json**——文件长度对、内容全空，
        于是 Key 一起没了，实例退回未激活。

        现在加三道保险，任何一道都能把"配置彻底报废"降级成"可恢复"：

        1. 写完 tmp 后 **重新读回来 json.loads 校验**，坏了就地放弃，
           绝不 replace——宁可这次设置没保存，也不能把好配置写坏；
        2. replace 之前把**当前好配置**另存为 ``config.json.bak``，
           万一真出事可以一行代码回滚；
        3. ``fsync`` 后再 replace，断电/蓝屏时数据真正落到盘上
           （否则 replace 可能成功、但内容还在页缓存里）。
        """
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            tmp = self._path.with_suffix(self._suffix() + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())      # 真正落盘，别只进页缓存
                # ★ 回读校验：坏内容绝不允许 replace 掉好配置
                json.loads(tmp.read_text(encoding="utf-8"))
            except Exception as ex:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
                raise RuntimeError(
                    f"配置写入校验失败，已放弃本次保存（原配置未动）："
                    f"{type(ex).__name__}: {ex}") from ex
            # 备份当前好配置（只在它本身是合法 JSON 时才备份）
            try:
                if self._path.exists():
                    old = self._path.read_text(encoding="utf-8")
                    json.loads(old)            # 坏文件不备份，别把垃圾存成"好"
                    self._path.with_suffix(
                        self._suffix() + ".bak").write_text(old, encoding="utf-8")
            except Exception:
                pass                          # 备份失败不阻断保存
            os.replace(tmp, self._path)
            return self._path

    def restore_backup(self) -> bool:
        """从 ``config.json.bak`` 恢复。返回是否真的恢复了。"""
        bak = self._path.with_suffix(self._suffix() + ".bak")
        try:
            if not bak.exists():
                return False
            raw = bak.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return False
            self._data.update(data)
            self.save()
            return True
        except Exception:
            return False

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
