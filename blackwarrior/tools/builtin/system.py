"""系统工具：提醒、心跳自调节、状态查询、环境感知。

``set_tick_interval`` 是个有意思的工具：**智能体可以自己改自己的心跳节奏**。
比如它判断"我在等一个结果"，就把心跳临时调到 10 秒（TTL 到期自动恢复）。
这是自主性在调度层的体现——节奏不再只是人配的常量。

v0.7.0 新增开机自启（``setup_autostart``）：让黑武士**开机就在**，
不用人记得点图标——这是"住进电脑里"最字面的一步。
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any, Dict, List

from ..registry import RISK_CAUTION, RISK_SAFE

#: 开机自启脚本文件名（放 Windows 启动文件夹）
AUTOSTART_NAME = "BlackWarrior_autostart.vbs"


def _startup_dir() -> str:
    """Windows 启动文件夹（登录即执行）。"""
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def _autostart_path() -> str:
    return os.path.join(_startup_dir(), AUTOSTART_NAME)


def _launch_command() -> Dict[str, Any]:
    """算出"启动黑武士服务"的命令。

    打包态直接跑 exe；开发态跑 ``python -m blackwarrior serve`` 并锁定
    工作目录到仓库根，否则 subprocess 找不到包。
    """
    import sys

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return {"exe": sys.executable, "args": [], "cwd": "",
                "kind": "packaged"}
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    return {"exe": sys.executable, "args": ["-m", "blackwarrior", "serve"],
            "cwd": repo, "kind": "source"}


def _build_vbs() -> str:
    """生成启动脚本内容。

    用 .vbs 而不是 .bat/.cmd：bat 会闪一个黑框，vbs 走 WScript.Shell
    以隐藏窗口启动，体验上才像"常驻后台"而不是"开机弹个命令行"。
    """
    from ... import paths as paths_mod

    cmd = _launch_command()
    exe = cmd["exe"].replace('"', '""')
    args = " ".join(f'"{a}"' if " " in a else a for a in cmd["args"])
    line = f'"{exe}" {args}'.rstrip()
    data_root = str(paths_mod.data_root())
    vbs = [
        "' BlackWarrior 开机自启（由黑武士自己生成，删掉即可取消）",
        "Set sh = CreateObject(\"WScript.Shell\")",
        f'sh.CurrentDirectory = "{cmd["cwd"] or data_root}"',
        f'sh.Environment("PROCESS")("BLACKWARRIOR_DATA_ROOT") = "{data_root}"',
        f'sh.Run """{line}""", 0, False',
        "",
    ]
    return "\r\n".join(vbs)


def register(reg: Any, ctx: Any) -> None:
    def set_reminder(title: str, minutes: float = 5, body: str = "") -> Dict[str, Any]:
        """设置一个提醒。

        参数:
            title: 提醒标题
            minutes: 多少分钟后触发
            body: 提醒正文
        """
        try:
            due = time.time() + float(minutes) * 60.0
            rid = ctx.store.add_reminder(str(title), due, body=str(body))
            return {"ok": True, "id": rid, "due_at": due,
                    "in_minutes": round(float(minutes), 2)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def list_reminders() -> Dict[str, Any]:
        """列出待触发的提醒。"""
        try:
            items = ctx.store.list_reminders(status="pending", limit=20)
            return {"ok": True, "items": items, "count": len(items)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def set_tick_interval(seconds: float, ttl: int = 1, reason: str = "") -> Dict[str, Any]:
        """临时调整自己的心跳节奏。

        参数:
            seconds: 心跳间隔秒数（最小 2 秒）
            ttl: 生效轮数，用完自动恢复默认
            reason: 调整原因（会显示在状态里）
        """
        try:
            ctx.scheduler.policy.set(max(2.0, float(seconds)),
                                     ttl=max(1, int(ttl)), reason=str(reason))
            return {"ok": True, **ctx.scheduler.policy.status()}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def get_time() -> Dict[str, Any]:
        """获取当前时间（本地时区 + 时间戳）。"""
        return {
            "ok": True,
            "timestamp": time.time(),
            "local": time.strftime("%Y-%m-%d %H:%M:%S"),
            "weekday": time.strftime("%A"),
        }

    def get_status() -> Dict[str, Any]:
        """查看黑武士自身运行状态（循环、记忆、模型、工具）。"""
        try:
            return {"ok": True, **ctx.status()}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def environment() -> Dict[str, Any]:
        """查看运行环境（操作系统、Python 版本、数据目录、沙箱）。"""
        try:
            return {
                "ok": True,
                "os": f"{platform.system()} {platform.release()}",
                "machine": platform.machine(),
                "python": platform.python_version(),
                "data_root": str(ctx.paths.data_root()),
                "sandbox": str(ctx.paths.sandbox_dir()),
                "hostname": platform.node(),
            }
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def list_tools() -> Dict[str, Any]:
        """列出当前可用工具。"""
        try:
            items = ctx.tools.describe()
            return {"ok": True, "items": items, "count": len(items)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def remember_feedback(kind: str, action: str = "") -> Dict[str, Any]:
        """接收用户反馈（praise/scold/poke/hug），用于塑形行为倾向。

        参数:
            kind: praise / scold / poke / hug / ignore
            action: 被反馈的动作名
        """
        try:
            weights = ctx.kernel.feedback(str(kind), action=(action or None))
            return {"ok": True, "kind": kind, "weights": weights}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def open_app(name: str) -> Dict[str, Any]:
        """启动电脑上的应用（个人智能助理的必备能力）。

        查找顺序：PATH 可执行文件 → Windows 开始菜单快捷方式（.lnk）。
        支持英文名（notepad / calc / wechat）与中文名（记事本 / 微信）。
        """
        import os
        import shutil
        import subprocess

        n = str(name or "").strip().strip('"')
        if not n:
            return {"ok": False, "error": "缺少 name 参数（如 notepad / 微信）"}

        # 1) PATH 里直接找
        exe = shutil.which(n)
        if not exe and not n.lower().endswith(".exe"):
            exe = shutil.which(n + ".exe")
        if exe:
            try:
                subprocess.Popen([exe])
                return {"ok": True, "app": n, "via": exe}
            except Exception as ex:
                return {"ok": False, "app": n,
                        "error": f"{type(ex).__name__}: {ex}"}

        # 2) Windows：开始菜单快捷方式（覆盖"微信/网易云"这类不在 PATH 的应用）
        if os.name == "nt":
            import glob as _glob
            roots = [
                os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                             "Microsoft", "Windows", "Start Menu", "Programs"),
                os.path.join(os.environ.get("APPDATA", ""),
                             "Microsoft", "Windows", "Start Menu", "Programs"),
            ]
            needle = n.lower()
            for root in roots:
                if not os.path.isdir(root):
                    continue
                best = ""
                for lnk in _glob.glob(os.path.join(root, "**", "*.lnk"),
                                      recursive=True):
                    base = os.path.basename(lnk).lower()
                    if base == needle + ".lnk":
                        best = lnk
                        break          # 完全同名：最优，立刻用
                    if not best and needle in base:
                        best = lnk     # 包含关系：先记下，继续找更精确的
                if best:
                    try:
                        os.startfile(best)  # noqa: S606 — 用户明确要求启动
                        return {"ok": True, "app": n, "via": best}
                    except Exception as ex:
                        return {"ok": False, "app": n, "candidate": best,
                                "error": f"{type(ex).__name__}: {ex}"}

        return {"ok": False, "app": n,
                "error": "找不到应用。可改用完整路径（.exe/.lnk），或用 open_url 打开网站"}

    def setup_autostart() -> Dict[str, Any]:
        """设置开机自启，让我开机就自动在后台运行（不用你记得点图标）。

        装一个隐藏窗口的启动脚本到 Windows 启动文件夹。
        想取消就说「取消开机自启」，或直接删掉那个脚本。
        """
        if os.name != "nt":
            return {"ok": False,
                    "error": f"当前系统（{platform.system()}）暂未支持自动配置，"
                             "请手动把 blackwarrior serve 加入开机项"}
        path = _autostart_path()
        try:
            os.makedirs(_startup_dir(), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(_build_vbs())
        except Exception as ex:
            return {"ok": False, "error": f"写入启动脚本失败：{ex}"}
        cmd = _launch_command()
        return {
            "ok": True,
            "script": path,
            "mode": cmd["kind"],
            "command": " ".join([cmd["exe"], *cmd["args"]]),
            "note": "下次开机生效；想立刻验证可手动双击该脚本",
        }

    def cancel_autostart() -> Dict[str, Any]:
        """取消开机自启。"""
        path = _autostart_path()
        try:
            if os.path.isfile(path):
                os.remove(path)
                return {"ok": True, "removed": path}
            return {"ok": True, "removed": "",
                    "note": "本来就没有开机自启"}
        except Exception as ex:
            return {"ok": False, "error": f"删除启动脚本失败：{ex}"}

    def autostart_status() -> Dict[str, Any]:
        """查看开机自启状态与当前位置。"""
        path = _autostart_path()
        exists = os.path.isfile(path)
        return {
            "ok": True,
            "supported": os.name == "nt",
            "enabled": exists,
            "script": path if exists else "",
            "startup_dir": _startup_dir(),
            "system": platform.system(),
        }

    reg.register("set_reminder", set_reminder, risk=RISK_CAUTION,
                 category="system", description="设置一个定时提醒")
    reg.register("list_reminders", list_reminders, risk=RISK_SAFE,
                 category="system", description="列出待触发提醒")
    reg.register("set_tick_interval", set_tick_interval, risk=RISK_SAFE,
                 category="system",
                 description="临时调整心跳节奏（带 TTL）")
    reg.register("get_time", get_time, risk=RISK_SAFE, category="system",
                 description="获取当前时间")
    reg.register("get_status", get_status, risk=RISK_SAFE, category="system",
                 description="查看运行状态")
    reg.register("environment", environment, risk=RISK_SAFE, category="system",
                 description="查看运行环境信息")
    reg.register("open_app", open_app, risk=RISK_CAUTION, category="system",
                 description="启动电脑上的应用（如 notepad / calc / 微信 / WeChat）。"
                             "用户说『帮我打开XX应用』时用这个；打开网站用 open_url")
    reg.register("list_tools", list_tools, risk=RISK_SAFE, category="system",
                 description="列出可用工具")
    reg.register("remember_feedback", remember_feedback, risk=RISK_SAFE,
                 category="memory", description="接收反馈以塑形行为")
    reg.register("setup_autostart", setup_autostart, risk=RISK_CAUTION,
                 category="system",
                 description="设置开机自启（开机即在后台运行，不用手动打开）")
    reg.register("cancel_autostart", cancel_autostart, risk=RISK_CAUTION,
                 category="system", description="取消开机自启")
    reg.register("autostart_status", autostart_status, risk=RISK_SAFE,
                 category="system", description="查看开机自启状态")
