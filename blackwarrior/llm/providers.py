"""模型服务商预设表。

全部走 **OpenAI 兼容协议**（``POST /chat/completions``），因此网关只需要一套实现。
覆盖主流国内外服务商 + 本地 Ollama + 自定义端点。

``requires_key=False`` 的 provider（本地 / 自定义无鉴权端点）不需要 API Key，
这样黑武士在**完全离线**的环境下也能跑（配 Ollama）。
"""

from __future__ import annotations

from typing import Any, Dict, List


class Provider:
    """一个模型服务商。"""

    def __init__(self, key: str, name: str, base_url: str, default_model: str,
                 *, env: str = "", requires_key: bool = True,
                 models: "list[str]" = None, note: str = "") -> None:
        self.key = key
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.env = env
        self.requires_key = requires_key
        self.models = list(models or [default_model])
        self.note = note

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "env": self.env,
            "requires_key": self.requires_key,
            "models": self.models,
            "note": self.note,
        }


PROVIDERS: Dict[str, Provider] = {}


def _reg(*args: Any, **kw: Any) -> None:
    p = Provider(*args, **kw)
    PROVIDERS[p.key] = p


_reg("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat",
     env="DEEPSEEK_API_KEY",
     models=["deepseek-chat", "deepseek-reasoner"],
     note="国内直连，性价比高，默认选择")

_reg("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini",
     env="OPENAI_API_KEY",
     models=["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"])

_reg("qwen", "通义千问（阿里云）",
     "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
     env="DASHSCOPE_API_KEY",
     models=["qwen-plus", "qwen-turbo", "qwen-max"])

_reg("moonshot", "Moonshot（月之暗面）", "https://api.moonshot.cn/v1",
     "moonshot-v1-8k", env="MOONSHOT_API_KEY",
     models=["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"])

_reg("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash",
     env="ZHIPU_API_KEY",
     models=["glm-4-flash", "glm-4-plus", "glm-4-air"])

_reg("minimax", "MiniMax", "https://api.minimax.chat/v1", "MiniMax-Text-01",
     env="MINIMAX_API_KEY",
     models=["MiniMax-Text-01", "abab6.5s-chat"])

_reg("mimo", "MiMo（小米）", "https://api.mimo.chat/v1", "mimo-7b-rl",
     env="MIMO_API_KEY",
     models=["mimo-7b-rl", "mimo-7b-sft"])

_reg("siliconflow", "硅基流动", "https://api.siliconflow.cn/v1",
     "Qwen/Qwen2.5-7B-Instruct", env="SILICONFLOW_API_KEY",
     models=["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"])

_reg("ollama", "Ollama（本地）", "http://127.0.0.1:11434/v1", "qwen2.5:7b",
     env="", requires_key=False,
     models=["qwen2.5:7b", "qwen3:8b", "gemma2:9b", "llama3.1:8b"],
     note="本地推理，无需联网与密钥——离线档位的基础")

_reg("local", "本地 OpenAI 兼容端点", "http://127.0.0.1:8000/v1", "local-model",
     env="", requires_key=False,
     models=["local-model"],
     note="自建 vLLM / LM Studio / FastChat 等")

_reg("custom", "自定义端点", "", "custom-model",
     env="", requires_key=False,
     models=["custom-model"],
     note="完全自定义 base_url 与模型名")


def get_provider(key: str) -> Provider:
    return PROVIDERS.get(str(key or "").lower().strip(), PROVIDERS["custom"])


def list_providers() -> List[Dict[str, Any]]:
    return [p.as_dict() for p in PROVIDERS.values()]


def provider_names() -> List[str]:
    return list(PROVIDERS.keys())


def resolve(cfg_provider: str, cfg_base_url: str = "",
            base_url_provider: str = "") -> Provider:
    """解析出实际使用的 provider。

    ★``base_url_provider`` 是为修一个真实的坑加的（2026-10-07）：
    用户把 provider 从 ollama 切到 deepseek 时，**旧的 base_url 还留在
    配置里**（``http://127.0.0.1:11434/v1``），而 ``resolve`` 一直是
    "base_url 非空就以它为准"——于是 deepseek 的请求被发到本地 ollama，
    对方当然回 ``404 model not found``。用户看到的现象是"换了模型还是
    报 model 错"，根因与模型名毫无关系。

    现在 base_url **带归属**：只在它本来就是为当前 provider 配的时候
    才生效。切换 provider 后旧地址自动失效，用户自己为新 provider 填的
    地址（走代理/中转）依然保留。
    """
    p = get_provider(cfg_provider)
    if cfg_base_url:
        owner = (base_url_provider or "").strip()
        if not owner or owner == p.key:
            # 用户手填了 base_url（或旧配置没记归属，保守沿用）：
            # 沿用该 provider 的其它属性，但地址以手填为准
            return Provider(p.key, p.name, cfg_base_url, p.default_model,
                            env=p.env, requires_key=p.requires_key,
                            models=p.models, note=p.note)
        # ★base_url 属于别的 provider ——忽略它，用新 provider 的预设。
    return p
