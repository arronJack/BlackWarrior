"""本机资源感知与诊断工具（v0.6）—— 把"这台机器是什么情况"变成可查的。

## 为什么补这组

黑武士 v0.5 只有``system_probe``（CPU/内存/磁盘/电池）。而用户真正
关心的"这台机器到底能干什么"还需要几类信息：

1. **已装软件** —— 回答"我机器上有/没有装 XX"（问错对象比问错答案更浪费时间）；
2. **PATH 可执行文件** —— 回答"能不能直接调某命令"；
3. **SSH / Git 配置** —— 开发类任务的必要前置；
4. **端口占用** —— "服务起不来"是最高频的支持问题之一；
5. **网络诊断** —— DNS 解析 / TCP 连通性，区分"断网"与"服务挂了"。

全部 ``safe``：只读，不改系统、不装东西、不发网络请求（TCP 探测是
**只建立连接后立刻关闭**，不发任何数据，且只连本机或用户显式指定的地址）。

## 隐私边界

``list_software`` 只读注册表/包管理器数据库，**不扫用户目录**。
返回里只给软件名与版本，**不给安装路径的全盘遍历结果** —— 后者既慢
又容易扫出隐私信息。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, List

from ..registry import RISK_SAFE

#: 探测 TCP 连通性的默认超时（秒）。设短一点：这是"快速判断"，
#: 不是"可靠测速"，用户要测速去用专门的工具。
TCP_PROBE_TIMEOUT = 2.5

#: 端口扫描时跳过的端口（Windows 保留端口，报出来也没意义）
_SKIP_PORTS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}


def register(reg: Any, ctx: Any) -> List[str]:
    names: List[str] = []

    # ------------------------------------------------------------ 软件清单

    def list_software(limit: int = 40, keyword: str = "") -> dict:
        """列出本机已安装的软件（名称 + 版本）。

        Windows 读卸载注册表项，Linux 读 dpkg/rpm 数据库，macOS 读
        /Applications。**不遍历用户目录**（既慢又可能带出隐私信息）。
        """
        out: List[Dict[str, Any]] = []
        try:
            import winreg  # type: ignore

            for root, path in (
                    (winreg.HKEY_LOCAL_MACHINE,
                     r"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
                    (winreg.HKEY_LOCAL_MACHINE,
                     r"SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
                    (winreg.HKEY_CURRENT_USER,
                     r"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall")):
                try:
                    with winreg.OpenKey(root, path) as k:
                        n = winreg.QueryInfoKey(k)[0]
                        for i in range(min(n, 800)):
                            try:
                                sub = winreg.EnumKey(k, i)
                                with winreg.OpenKey(k, sub) as sk:
                                    nm = str(winreg.QueryValueEx(
                                        sk, "DisplayName")[0] or "").strip()
                                    if not nm:
                                        continue
                                    try:
                                        ver = str(winreg.QueryValueEx(
                                            sk, "DisplayVersion")[0] or "")
                                    except OSError:
                                        ver = ""
                                    out.append({"name": nm, "version": ver})
                            except OSError:
                                continue
                except OSError:
                    continue
            out = _dedupe(out)
        except ImportError:
            # 非 Windows：退化到包管理器数据库
            try:
                r = subprocess.run(["dpkg-query", "-W", "-f=${Package} ${Version}\n"],
                                   capture_output=True, text=True, timeout=25)
                if r.returncode == 0:
                    out = [{"name": ln.split()[0], "version": (ln.split() + [""])[1]}
                           for ln in r.stdout.splitlines()[:800] if ln.strip()]
            except Exception:
                out = []
        kw = str(keyword or "").strip().lower()
        if kw:
            out = [x for x in out if kw in str(x["name"]).lower()]
        out.sort(key=lambda x: str(x["name"]).lower())
        return {"count": len(out),
                "items": out[:max(1, min(int(limit or 40), 300))],
                "source": "windows-uninstall-registry"
                if os.name == "nt" else "dpkg",
                "note": "" if out else "未读到软件清单（需要相应权限或平台不同）"}

    reg.register("list_software", list_software, risk=RISK_SAFE,
                 category="system",
                 description="列出本机已装软件（名称+版本，不扫用户目录）")
    names.append("list_software")

    # ------------------------------------------------------------ PATH 探查

    def find_command(name: str) -> dict:
        """查某个命令是否在 PATH 里，返回绝对路径与版本。

        比直接跑一次命令更安全：只查存在性，不执行。
        """
        nm = str(name or "").strip()
        if not nm:
            return {"ok": False, "reason": "缺少命令名"}
        path = shutil.which(nm)
        if not path:
            return {"ok": False, "found": False, "name": nm,
                    "reason": f"PATH 中未找到「{nm}」",
                    "path_hint": len(os.environ.get("PATH", "").split(
                        os.pathsep))}
        ver = ""
        try:
            r = subprocess.run([path, "--version"], capture_output=True,
                               text=True, timeout=6)
            ver = (r.stdout or r.stderr or "").strip().splitlines()[:1]
            ver = ver[0] if ver else ""
        except Exception:
            ver = ""                    # 拿不到版本不算失败
        return {"ok": True, "found": True, "name": nm, "path": path,
                "version": ver}

    reg.register("find_command", find_command, risk=RISK_SAFE, category="system",
                 description="查命令是否在 PATH（含绝对路径与版本，不执行）")
    names.append("find_command")

    # ------------------------------------------------------------ 网络诊断

    def diagnose_network(host: str = "www.baidu.com",
                         port: int = 443) -> dict:
        """分层诊断网络：DNS 能否解析 → TCP 能否连通。

        区分"断网/ DNS 坏"与"服务本身挂了" —— 这两种在聊天里都表现为
        "连不上"，但处置完全不同。**只解析域名 + 只建TCP 连接**，
        不发送任何 HTTP 请求。
        """
        h = str(host or "").strip()
        if not h:
            return {"ok": False, "reason": "缺少 host"}
        p = int(port or 443)
        out: Dict[str, Any] = {"host": h, "port": p}

        # 1) DNS
        t0 = time.time()
        try:
            infos = socket.getaddrinfo(h, p, proto=socket.IPPROTO_TCP)
            addrs = sorted({i[4][0] for i in infos})
            out["dns"] = {"ok": True, "ms": int((time.time() - t0) * 1000),
                          "addresses": addrs[:4]}
        except Exception as ex:
            out["dns"] = {"ok": False,
                          "error": f"{type(ex).__name__}: {ex}"}
            out["verdict"] = ("DNS 解析失败 —— 问题在域名解析（DNS 服务 / "
                              " hosts / 网络不通），还没到服务层")
            out["ok"] = False
            return out

        # 2) TCP
        t0 = time.time()
        s = None
        try:
            s = socket.create_connection((h, p), timeout=TCP_PROBE_TIMEOUT)
            out["tcp"] = {"ok": True, "ms": int((time.time() - t0) * 1000)}
            out["verdict"] = "DNS 与 TCP 均通 —— 网络本身没问题，若服务不可用请查服务/端口/防火墙"
            out["ok"] = True
        except Exception as ex:
            out["tcp"] = {"ok": False,
                          "error": f"{type(ex).__name__}: {ex}"}
            out["verdict"] = "DNS 通但 TCP 不通 —— 端口未开/ 被防火墙拦 / 服务没起"
            out["ok"] = False
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        return out

    reg.register("diagnose_network", diagnose_network, risk=RISK_SAFE,
                 category="system",
                 description="分层诊断网络（DNS 解析 → TCP 连通，不发 HTTP）")
    names.append("diagnose_network")

    # ------------------------------------------------------------ 端口占用

    def check_port(port: int = 8777, host: str = "127.0.0.1") -> dict:
        """检查本机端口是否被占用（"服务起不来"的高频原因）。"""
        p = int(port or 0)
        if not (0 < p < 65536) or p in _SKIP_PORTS:
            return {"ok": False, "reason": f"端口号无效：{port}"}
        h = str(host or "127.0.0.1")
        s = None
        try:
            s = socket.create_connection((h, p), timeout=1.5)
            return {"ok": True, "port": p, "in_use": True,
                    "verdict": f"端口 {p} 已被占用 —— 若要启动服务，先关掉占用者"
                               f"或换端口"}
        except Exception:
            return {"ok": True, "port": p, "in_use": False,
                    "verdict": f"端口 {p} 空闲，可用"}
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    reg.register("check_port", check_port, risk=RISK_SAFE, category="system",
                 description="检查本机端口是否被占用")
    names.append("check_port")

    # ------------------------------------------------------------ 开发资源

    def dev_env() -> dict:
        """开发环境速览：git / ssh 配置 / 常见运行时是否存在。

        只读不写：**不列出私钥内容**，只报"有没有配"。
        """
        out: Dict[str, Any] = {}

        # git
        git = shutil.which("git")
        if git:
            ver = ""
            try:
                r = subprocess.run([git, "--version"], capture_output=True,
                                   text=True, timeout=6)
                ver = (r.stdout or "").strip()
            except Exception:
                pass
            out["git"] = {"found": True, "path": git, "version": ver}
        else:
            out["git"] = {"found": False, "verdict": "未安装 git"}

        # ssh：只报存在性，不读私钥
        ssh_dir = os.path.expanduser("~/.ssh")
        info: Dict[str, Any] = {"dir": ssh_dir,
                               "dir_exists": os.path.isdir(ssh_dir)}
        if os.path.isdir(ssh_dir):
            try:
                names = os.listdir(ssh_dir)
                info["keys"] = sorted(n for n in names
                                      if n.endswith((".pub", ".pem")))[:8]
                info["has_ed25519"] = os.path.exists(
                    os.path.join(ssh_dir, "id_ed25519"))
                cfg = os.path.join(ssh_dir, "config")
                info["has_config"] = os.path.isfile(cfg)
                if info["has_config"]:
                    # 只读 Host 别名，不读 IdentityFile 等敏感路径
                    hosts = []
                    with open(cfg, "r", encoding="utf-8",
                              errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if line.lower().startswith("host "):
                                hosts.append(line.split(None, 1)[-1])
                    info["hosts"] = hosts[:12]
            except OSError as ex:
                info["error"] = f"{type(ex).__name__}: {ex}"
        out["ssh"] = info

        # 常见运行时
        runtimes = {}
        for name in ("python", "node", "npm", "go", "rustc", "java",
                     "docker", "uv"):
            exe = shutil.which(name)
            if exe:
                runtimes[name] = exe
        out["runtimes"] = runtimes
        out["available_count"] = len(runtimes)
        return out

    reg.register("dev_env", dev_env, risk=RISK_SAFE, category="system",
                 description="开发环境速览（git/SSH 配置存在性/常见运行时）")
    names.append("dev_env")

    # ------------------------------------------------------------ 文本统计

    def text_stats(path: str = "") -> dict:
        """统计一个文本文件的行数、字符数、最长行（快速评估文件规模）。"""
        raw = str(path or "").strip()
        if not raw:
            return {"ok": False, "reason": "缺少 path"}
        try:
            lines = 0
            chars = 0
            longest = 0
            with open(raw, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    lines += 1
                    n = len(ln)
                    chars += n
                    if n > longest:
                        longest = n
            size = os.path.getsize(raw)
            return {"ok": True, "path": raw, "lines": lines, "chars": chars,
                    "longest_line": longest, "size_bytes": size,
                    "avg_line": round(chars / lines, 1) if lines else 0}
        except OSError as ex:
            return {"ok": False, "reason": f"{type(ex).__name__}: {ex}"}

    reg.register("text_stats", text_stats, risk=RISK_SAFE, category="filesystem",
                 description="统计文本文件规模（行数/字符/最长行）")
    names.append("text_stats")

    return names


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一软件在 32/64 位注册表里各出现一次，按 名称+版本 去重。"""
    seen = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        key = (str(it.get("name") or "").strip().lower(),
               str(it.get("version") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def selftest() -> bool:
    """自检：真实注册 + 真实调用（不联网也能跑大部分）。"""
    import os
    import tempfile

    from ...config import Config
    from ...db.store import Store
    from ..registry import ToolRegistry

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(f"  {'v' if cond else 'x'} {msg}")
        ok = ok and bool(cond)

    d = tempfile.mkdtemp(prefix="bw-sys-")
    store = Store(path=os.path.join(d, "t.db"))
    cfg = Config(path=os.path.join(d, "config.json"))

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.store = store
    ctx.config = cfg

    reg = ToolRegistry()
    names = register(reg, ctx)
    want = ("list_software", "find_command", "diagnose_network",
            "check_port", "dev_env", "text_stats")
    for w in want:
        check(w in names, f"工具 {w} 已注册")
    for spec in reg.specs():
        if spec.name in names:
            check(spec.risk == RISK_SAFE, f"{spec.name} 风险为 safe")

    # find_command
    r = reg.call("find_command", {"name": "cmd"})
    check(r.ok and r.result.get("found") is True,
          f"find_command 找到 cmd（{r.result.get('path')}）")
    r2 = reg.call("find_command", {"name": "definitely-not-a-real-cmd-xyz"})
    check(r2.ok and r2.result.get("found") is False, "不存在命令如实报未找到")
    r3 = reg.call("find_command", {"name": ""})
    check(r3.ok is False or r3.result.get("ok") is False, "空命令名被拒")

    # check_port：用一个确定被占用的端口（临时起个监听）
    import socket as _sk
    srv = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        r4 = reg.call("check_port", {"port": busy})
        check(r4.ok and r4.result.get("in_use") is True,
              f"check_port 检出端口 {busy} 被占用")
    finally:
        srv.close()
    r5 = reg.call("check_port", {"port": 59999})
    check(r5.ok and r5.result.get("in_use") is False, "空闲端口如实报未占用")
    r6 = reg.call("check_port", {"port": 0})
    check(r6.ok is False or r6.result.get("ok") is False, "非法端口被拒")

    # diagnose_network：用一个必定解析失败的域名（不依赖外网）
    r7 = reg.call("diagnose_network",
                  {"host": "nonexistent-host-xyz-for-selftest.invalid",
                   "port": 80})
    check(r7.ok and r7.result.get("dns", {}).get("ok") is False,
          "DNS 失败如实报失败并给结论")
    check(bool(r7.result.get("verdict")), "DNS 失败时给出可读结论")

    # dev_env
    r8 = reg.call("dev_env", {})
    check(r8.ok and "git" in r8.result and "ssh" in r8.result,
          "dev_env 报 git/ssh 状态")
    # ★ 隐私守卫：返回里出现的文件名必须都是 .pub / .pem，
    #   私钥（无扩展名或 .key）绝不能进结果
    _keys = [str(k) for k in (r8.result.get("ssh", {}).get("keys") or [])]
    _leaked = [k for k in _keys
               if not k.endswith((".pub", ".pem"))]
    check(not _leaked,
          f"★ssh 只报公钥/证书名，私钥未泄露（{_keys[:4]}）")

    # text_stats
    f1 = os.path.join(d, "a.txt")
    with open(f1, "w", encoding="utf-8") as f:
        f.write("hello\nworld\n")
    r9 = reg.call("text_stats", {"path": f1})
    check(r9.ok and r9.result.get("lines") == 2, f"text_stats 行数正确（{r9.result.get('lines')}）")
    r10 = reg.call("text_stats", {"path": os.path.join(d, "nope.txt")})
    check(r10.ok is False or r10.result.get("ok") is False, "不存在文件被拒")

    # list_software 真跑（慢，但必须真跑一次）
    r11 = reg.call("list_software", {"limit": 5})
    check(r11.ok and "count" in r11.result,
          f"list_software 可用（读到 {r11.result.get('count')} 项/来源 "
          f"{r11.result.get('source')}）")

    store.close()
    return ok
