"""环境自检与一键修复（doctor）。

解决一个很具体的糟糕体验：桌面壳启动失败时，用户看到的是
「内核启动失败」外加一段 Python 报错，既不知道缺什么，也不知道怎么补。
于是要么放弃，要么去搜索引擎里瞎找。

这里把这件事变成**一张可执行的清单**：

    GET  /api/doctor        → 逐项体检，返回 {id, 等级, 详情, 修法}
    POST /api/doctor/fix    → 用户点某一项的「安装」才真的动手

两条不可让步的原则：

1. **绝不自动安装。** 任何 pip / 下载都要用户点一下才执行。理由很实际：
   黑武士能自己往机器上装东西，那它和恶意软件的区别只剩一个用户勾选。
   医疗级场景下这条是生死线。
2. **如实分级。** ``block`` = 用不了这个功能；``warn`` = 能用但打了折扣；
   ``ok`` = 正常。把「语音识别不可用」说成「一切正常」比报错更糟。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import paths as _paths
from .events import emit

OK = "ok"
WARN = "warn"
BLOCK = "block"

# 国内镜像：直连 pypi/HF 在国内会卡到超时，用户看到的是"安装很慢"
# 而不是"为什么这么慢"。清华源 / hf-mirror 是实测可用的。
_PYPI_MIRROR = os.environ.get("BW_PYPI_MIRROR",
                              "https://pypi.tuna.tsinghua.edu.cn/simple")
_HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

_PIPER_MODEL = "zh_CN-huayan-medium"


# ============================================================ 进度流

class _Job:
    """一次修复任务的进度。装大包要几分钟，没有进度用户会以为卡死了。"""

    def __init__(self, jid: str, title: str) -> None:
        self.id = jid
        self.title = title
        self.state = "running"      # running | ok | failed | cancelled
        self.lines: List[str] = []
        self.started = time.time()
        self.finished: Optional[float] = None
        self.error: str = ""

    def log(self, text: str) -> None:
        self.lines.append(str(text)[-400:])
        emit("doctor_progress",
             {"job_id": self.id, "title": self.title,
              "line": str(text)[-400:], "n": len(self.lines)})

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "state": self.state,
            "lines": self.lines[-40:], "error": self.error,
            "elapsed": int((self.finished or time.time()) - self.started),
        }


class Doctor:
    """自检 + 修复。**修复只在用户明确点击后发生。**"""

    def __init__(self, core: Any = None, config: Any = None) -> None:
        self.core = core
        self.config = config if config is not None else (
            core.config if core is not None else None)
        self._jobs: Dict[str, _Job] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------- 工具

    def _cfg(self, key: str, default: Any = "") -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _cfg_set(self, key: str, value: Any) -> None:
        try:
            if self.config is not None:
                self.config.set(key, value)
                self.config.save()
        except Exception:
            pass

    @staticmethod
    def _has_module(name: str) -> bool:
        """不真的 import 一遍（pasm2 这种会有副作用），用 find_spec 探。"""
        import importlib.util

        try:
            return importlib.util.find_spec(name) is not None
        except Exception:
            return False

    @staticmethod
    def _dir_size_mb(path: str) -> float:
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return round(total / 1048576.0, 1)

    def _free_mb(self, path: str) -> int:
        try:
            return int(shutil.disk_usage(path).free / 1048576)
        except Exception:
            return -1

    # -------------------------------------------------- 体检项

    def check(self) -> Dict[str, Any]:
        items: List[Dict[str, Any]] = []
        for fn in (self._c_python, self._c_kernel, self._c_numpy,
                   self._c_pasm2, self._c_llm, self._c_asr_venv,
                   self._c_asr_model, self._c_tts_model, self._c_space,
                   self._c_config):
            try:
                items.append(fn())
            except Exception as ex:
                items.append({
                    "id": getattr(fn, "_cid", fn.__name__),
                    "name": getattr(fn, "_cname", fn.__name__),
                    "level": BLOCK, "detail": f"检查本身出错：{ex}",
                    "fix": None,
                })

        levels = [i["level"] for i in items]
        overall = (BLOCK if BLOCK in levels
                   else WARN if WARN in levels else OK)
        return {
            "ok": overall == OK,
            "overall": overall,
            "items": items,
            "summary": _summarize(items),
            "checked_at": time.time(),
            "python": sys.version.split()[0],
            "exe": sys.executable,
            "data_root": str(_paths.data_root()),
        }

    # ---- Python 解释器 ----
    def _c_python(self) -> Dict[str, Any]:
        v = sys.version_info
        if v < (3, 9):
            return {"id": "python", "name": "Python 解释器",
                    "level": BLOCK,
                    "detail": f"版本 {v.major}.{v.minor} 过低，需要 3.9+",
                    "fix": None}
        return {"id": "python", "name": "Python 解释器", "level": OK,
                "detail": f"{v.major}.{v.minor}.{v.micro} · {sys.executable}",
                "fix": None}

    def _c_kernel(self) -> Dict[str, Any]:
        try:
            from .version import __version__
        except Exception:
            return {"id": "kernel", "name": "黑武士内核", "level": BLOCK,
                    "detail": "内核模块无法导入", "fix": None}
        return {"id": "kernel", "name": "黑武士内核", "level": OK,
                "detail": f"v{__version__} 已就位", "fix": None}

    # ---- numpy：PASM 十九层底座的硬依赖 ----
    def _c_numpy(self) -> Dict[str, Any]:
        if self._has_module("numpy"):
            return {"id": "numpy", "name": "numpy（认知底座依赖）",
                    "level": OK, "detail": "已安装", "fix": None}
        return {
            "id": "numpy", "name": "numpy（认知底座依赖）",
            "level": WARN,
            "detail": "未安装 —— 认知引擎会**降级**到 builtin/light 档，"
                      "记不住长期记忆、没有真实情绪演化",
            "fix": {"kind": "pip", "label": "安装 numpy",
                    "packages": ["numpy"], "size_mb": 20,
                    "note": "装完需重启内核生效"},
        }

    # ---- pasm2：真正的十九层 ----
    def _c_pasm2(self) -> Dict[str, Any]:
        if self._has_module("pasm2"):
            return {"id": "pasm2", "name": "PASM V2 十九层底座",
                    "level": OK, "detail": "已安装", "fix": None}
        return {
            "id": "pasm2", "name": "PASM V2 十九层底座",
            "level": WARN,
            "detail": "未安装 —— engine 会退回 builtin，"
                      "认知能力只有内核自带那一层",
            "fix": {"kind": "pip_local", "label": "从源码安装 PASM",
                    "path": r"E:\AI\pasm\code\PASM",
                    "size_mb": 30,
                    "note": "从本机源码仓库装（--no-deps），不联网"},
        }

    # ---- LLM ----
    def _c_llm(self) -> Dict[str, Any]:
        provider = str(self._cfg("provider", "") or "")
        model = str(self._cfg("model", "") or "")
        api_key = str(self._cfg("api_key", "") or "")
        if provider == "ollama":
            # base_url 通常是 .../v1（OpenAI 兼容层），而 ollama 原生
            # 的 /api/tags 在**不带 /v1** 的根上。直接拼会 404，
            # 于是把"在线"误判成"离线"——这类假阴性最误导人。
            base = str(self._cfg("base_url", "http://127.0.0.1:11434") or "")
            for suffix in ("/v1", "/v1/"):
                if base.rstrip("/").endswith(suffix.rstrip("/")):
                    base = base.rstrip("/")[: -len(suffix.rstrip("/"))]
                    break
            alive = _port_alive(base)
            if alive:
                return {"id": "llm", "name": "思考引擎",
                        "level": OK,
                        "detail": f"本地模型 {model}（ollama 在线）",
                        "fix": None}
            return {
                "id": "llm", "name": "思考引擎", "level": BLOCK,
                "detail": "provider=ollama 但本地模型服务没在跑",
                "fix": {"kind": "manual",
                        "label": "启动 ollama",
                        "note": "在终端执行 `ollama serve`，"
                                "再确认已 `ollama pull " + (model or "qwen2.5:7b") + "`"},
            }
        if api_key:
            return {"id": "llm", "name": "思考引擎",
                    "level": OK,
                    "detail": f"云端 {provider or 'deepseek'} / {model}",
                    "fix": None}
        return {
            "id": "llm", "name": "思考引擎", "level": BLOCK,
            "detail": f"provider={provider or '未配置'} 但没有 API Key —— "
                      "它无法思考，只能当记事本",
            "fix": {"kind": "manual", "label": "在设置里填 API Key",
                    "note": "或者把 provider 改成 ollama 走完全离线"},
        }

    # ---- 本地语音识别环境 ----
    def _venv(self) -> str:
        try:
            from .voice.asr_local import find_venv
            return find_venv(str(_paths.data_root()))
        except Exception:
            return ""

    def _c_asr_venv(self) -> Dict[str, Any]:
        py = self._venv()
        if py and os.path.exists(py):
            size = self._dir_size_mb(os.path.dirname(os.path.dirname(py)))
            return {"id": "asr_venv", "name": "本地语音识别环境",
                    "level": OK,
                    "detail": f"已就位（约 {size:.0f} MB）", "fix": None}
        free = self._free_mb(str(_paths.data_root()))
        return {
            "id": "asr_venv", "name": "本地语音识别环境",
            "level": WARN,
            "detail": "未安装 —— 无法听懂语音（文字对话不受影响）"
                      + (f"；{_paths.data_root()} 所在盘剩余 {free} MB"
                         if free > 0 else ""),
            "fix": {"kind": "asr_venv", "label": "安装语音识别环境",
                    "size_mb": 260,
                    "note": "装 faster-whisper 到独立虚拟环境，不污染内核"},
        }

    def _asr_model_dir(self) -> str:
        """faster-whisper 的模型缓存目录。

        注意实际落点是 ``~/.cache/huggingface/hub``（多一层 hub）。
        之前只查 ``~/.cache/huggingface``，结果**明明能识别却报"未下载"**——
        会说谎的体检比没有体检更糟。
        """
        root = os.environ.get("HF_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface")
        for cand in (os.path.join(root, "hub"), root):
            if os.path.isdir(cand):
                try:
                    if any("whisper" in n.lower() for n in os.listdir(cand)):
                        return cand
                except OSError:
                    continue
        return os.path.join(root, "hub")

    def _c_asr_model(self) -> Dict[str, Any]:
        if not (self._venv() and os.path.exists(self._venv())):
            return {"id": "asr_model", "name": "语音识别模型",
                    "level": OK, "detail": "随语音环境一起装", "fix": None}
        d = self._asr_model_dir()
        # faster-whisper 默认落到 HF 缓存；找不到就当没下（让它重新拉）
        if os.path.isdir(d) and "whisper" in " ".join(os.listdir(d)).lower():
            return {"id": "asr_model", "name": "语音识别模型",
                    "level": OK, "detail": "已下载", "fix": None}
        return {
            "id": "asr_model", "name": "语音识别模型",
            "level": WARN,
            "detail": "首次识别时会自动下载（约 150 MB，走 hf-mirror）",
            "fix": {"kind": "asr_model", "label": "立即下载", "size_mb": 150,
                    "note": "也可以不装，第一次说话时它会自己下"},
        }

    def _c_tts_model(self) -> Dict[str, Any]:
        for d in (os.path.expanduser("~/.local/share/piper"),
                  os.path.join(str(_paths.data_root()), "piper")):
            if os.path.exists(os.path.join(d, _PIPER_MODEL + ".onnx")):
                return {"id": "tts_model", "name": "语音合成模型",
                        "level": OK, "detail": "已就位（本地离线）",
                        "fix": None}
        return {
            "id": "tts_model", "name": "语音合成模型", "level": WARN,
            "detail": "未下载 —— 它会出声但没有本地音色（回落 edge-tts 云端，"
                      "约 3.5 秒延迟）",
            "fix": {"kind": "tts_model", "label": "下载 piper 中文音色",
                    "size_mb": 63, "note": "装完合成只要 0.2 秒且完全离线"},
        }

    def _c_space(self) -> Dict[str, Any]:
        free = self._free_mb(str(_paths.data_root()))
        if free < 0:
            return {"id": "space", "name": "磁盘空间", "level": OK,
                    "detail": "无法探测", "fix": None}
        need = 600   # ASR venv + 模型 + 音色
        if free < need:
            return {
                "id": "space", "name": "磁盘空间", "level": BLOCK,
                "detail": f"数据目录所在盘只剩 {free} MB，"
                          f"装齐语音组件约需 {need} MB",
                "fix": {"kind": "manual", "label": "清理空间或换数据目录",
                        "note": "可在设置里改数据目录到另一个盘"},
            }
        return {"id": "space", "name": "磁盘空间", "level": OK,
                "detail": f"剩余 {free} MB（语音组件约需 {need} MB）",
                "fix": None}

    def _c_config(self) -> Dict[str, Any]:
        p = str(_paths.config_path())
        try:
            import json as _j
            ok = bool(_j.loads(open(p, encoding="utf-8").read()))
        except Exception:
            return {"id": "config", "name": "配置文件", "level": WARN,
                    "detail": f"读取异常（{p}）", "fix": None}
        return {"id": "config", "name": "配置文件", "level": OK,
                "detail": p if ok else p, "fix": None}

    # -------------------------------------------------- 修复

    def jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [j.as_dict() for j in self._jobs.values()][-8:]

    def start_fix(self, item_id: str) -> Dict[str, Any]:
        """按体检项 id 启动修复。**只有用户点了才会走到这里。**"""
        item = None
        for it in self.check()["items"]:
            if it["id"] == item_id:
                item = it
                break
        if item is None:
            return {"error": f"没有这项体检：{item_id}"}
        fix = item.get("fix")
        if not fix:
            return {"error": f"{item['name']} 不需要修（或需手动处理）",
                    "item": item}

        jid = f"{item_id}-{int(time.time())}"
        job = _Job(jid, f"安装 {item['name']}")
        with self._lock:
            self._jobs[jid] = job
        threading.Thread(target=self._run_fix, args=(job, fix),
                         name="doctor-fix", daemon=True).start()
        return {"job_id": jid, "job": job.as_dict()}

    def _run_fix(self, job: _Job, fix: Dict[str, Any]) -> None:
        kind = str(fix.get("kind") or "")
        try:
            if kind == "pip":
                self._fix_pip(job, list(fix.get("packages") or []),
                              self._venv() or sys.executable)
            elif kind == "pip_local":
                self._fix_pip(job, ["--no-deps", "-e", str(fix.get("path"))],
                              sys.executable)
            elif kind == "asr_venv":
                self._fix_asr_venv(job)
            elif kind == "asr_model":
                self._fix_asr_model(job)
            elif kind == "tts_model":
                self._fix_tts_model(job)
            else:
                job.state = "failed"
                job.error = "这一项需要手动处理，清单里已给出说明"
                job.log("⚠ 需要手动处理：" + str(fix.get("note") or ""))
                return
            job.state = "ok"
            job.finished = time.time()
            emit("doctor_done", job.as_dict())
        except Exception as ex:
            job.state = "failed"
            job.error = f"{type(ex).__name__}: {ex}"
            job.finished = time.time()
            emit("doctor_done", job.as_dict())

    # ---- pip 安装（带镜像与实时输出）----
    def _fix_pip(self, job: _Job, args: List[str], python: str) -> None:
        if not os.path.exists(python):
            raise RuntimeError(f"找不到解释器：{python}")
        cmd = [python, "-m", "pip", "install", "--disable-pip-version-check"]
        cmd += args
        if not any(a.startswith("-") and a in ("-e", "--editable")
                   for a in args):
            cmd += ["-i", _PYPI_MIRROR]
        job.log("执行：" + " ".join(cmd[:6]) + " …")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="ignore",
                                creationflags=getattr(
                                    subprocess, "CREATE_NO_WINDOW", 0))
        for line in (proc.stdout or []):
            s = line.strip()
            if s and not s.startswith("  "):
                job.log(s[-200:])
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"pip 退出码 {proc.returncode}")

    def _fix_asr_venv(self, job: _Job) -> None:
        root = str(_paths.data_root())
        proj = os.path.dirname(root)
        venv = os.path.join(proj, ".venv-asr")
        if not os.path.exists(os.path.join(venv, "Scripts", "python.exe")):
            job.log("创建虚拟环境：" + venv)
            os.makedirs(proj, exist_ok=True)
            r = subprocess.run([sys.executable, "-m", "venv", venv],
                               capture_output=True, text=True,
                               creationflags=getattr(
                                   subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                raise RuntimeError("创建虚拟环境失败：" +
                                   (r.stderr or "")[-200:])
        py = os.path.join(venv, "Scripts", "python.exe")
        job.log("安装 faster-whisper（较大，约 200 MB，请耐心）…")
        # ★av 必须钉在 <=14：19.x 移除了 metadata_errors 参数，
        # faster-whisper 1.2.1 会直接 TypeError（实测踩过）。
        self._fix_pip(job, ["faster-whisper", "edge-tts", "av<15"], py)
        job.log("✓ 语音识别环境就绪")

    def _fix_asr_model(self, job: _Job) -> None:
        py = self._venv()
        if not (py and os.path.exists(py)):
            raise RuntimeError("语音识别环境还没装好，请先装那项")
        job.log("下载 whisper base 模型（走 hf-mirror）…")
        env = dict(os.environ, HF_ENDPOINT=_HF_ENDPOINT)
        code = (
            "from faster_whisper import WhisperModel;"
            "m=WhisperModel('base',device='cpu',compute_type='int8');"
            "print('ok')"
        )
        r = subprocess.run([py, "-c", code], capture_output=True,
                           text=True, env=env, timeout=1800,
                           creationflags=getattr(subprocess,
                                                "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise RuntimeError("模型下载失败：" + _tail(r.stderr))
        job.log("✓ 模型已就绪")

    def _fix_tts_model(self, job: _Job) -> None:
        py = self._venv()
        if not (py and os.path.exists(py)):
            raise RuntimeError("语音识别环境还没装好，请先装那项")
        job.log("下载 piper 中文音色（约 63 MB，走 hf-mirror）…")
        base = ("https://hf-mirror.com/rhasspy/piper-voices/resolve/main/"
                "zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx")
        d = os.path.expanduser("~/.local/share/piper")
        os.makedirs(d, exist_ok=True)
        env = dict(os.environ, HF_ENDPOINT=_HF_ENDPOINT)
        # ★必须带 User-Agent：hf-mirror 会**拒绝 urllib 的默认 UA**（403）。
        # 之前用 urlretrieve裸下载，一直报 403，用户只能看到一句
        # "音色下载失败"——其实加个请求头就好了。
        code = (
            "import urllib.request as R\n"
            "def get(u,p):\n"
            "    r=R.Request(u,headers={'User-Agent':'Mozilla/5.0',"
            "'Accept':'*/*'})\n"
            "    open(p,'wb').write(R.urlopen(r,timeout=180).read())\n"
            f"get('{base}', r'{os.path.join(d, _PIPER_MODEL + '.onnx')}')\n"
            f"get('{base}.json', r'{os.path.join(d, _PIPER_MODEL + '.onnx.json')}')\n"
            "print('done')"
        )
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           env=env, timeout=1800,
                           creationflags=getattr(subprocess,
                                                "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise RuntimeError("音色下载失败：" + _tail(r.stderr))
        job.log("✓ 音色已就绪")


# ============================================================ 辅助

def _tail(text: str, limit: int = 300) -> str:
    """从可能很长的 stderr 里取出**最后那行真正的错误**。

    直接取 [-200:] 常常正好切在 Python traceback 的中间，
    剩下 `^^^^^` 这种没有信息量的东西——用户看不到真实原因。
    """
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return "（无输出）"
    bad = [ln for ln in lines
           if any(k in ln for k in ("Error", "error", "Exception", "失败",
                                    "403", "404", "No such", "cannot"))]
    return (bad[-1] if bad else lines[-1])[:limit]


def _port_alive(base_url: str) -> bool:
    import urllib.request

    url = str(base_url or "").rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _summarize(items: List[Dict[str, Any]]) -> str:
    n_ok = sum(1 for i in items if i["level"] == OK)
    n_warn = sum(1 for i in items if i["level"] == WARN)
    n_block = sum(1 for i in items if i["level"] == BLOCK)
    if n_block:
        return (f"{n_block} 项导致功能不可用，{n_warn} 项打了折扣，"
                f"{n_ok} 项正常")
    if n_warn:
        return f"核心可用，{n_warn} 项可选组件没装（点对应项可一键安装）"
    return f"全部正常（{n_ok} 项）"


def build_doctor(core: Any = None) -> Doctor:
    return Doctor(core)