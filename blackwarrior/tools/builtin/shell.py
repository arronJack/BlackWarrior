"""Shell 工具。

安全策略（三层）::

    1. 黑名单：命中即拒绝（格式化、递归删除系统目录、关机等）
    2. 超时：默认 30s，最长 120s，超时强杀
    3. 工作目录：默认在沙箱内执行

这不等于真正的沙箱（没有 seccomp / 容器），但对桌面 Agent 场景够用，
且比"直接 subprocess 跑任何东西"安全一个数量级。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict, List

from ..registry import RISK_DANGER

#: 命中即拒绝的模式（小写匹配）
BLOCKED_PATTERNS: List[str] = [
    "format ", "mkfs", ":(){", "fork bomb",
    "rm -rf /", "rm -rf ~", "rm -rf c:", "rm -rf c:\\",
    "del /f /s /q c:", "rd /s /q c:",
    "shutdown", "shutdown /s", "init 0", "halt",
    "reg delete hklm", "reg delete hkcr",
    "taskkill /f /im system",
    "> /dev/sda", "dd if=/dev/zero",
    "chmod -r 777 /", "chown -r",
]

#: 需要 danger 授权的模式
DANGER_PATTERNS: List[str] = [
    "rm ", "rmdir", "del ", "erase ",
    "mv ", "move ", "sudo ", "su -",
    "apt ", "apt-get ", "brew ", "pip uninstall",
    "npm uninstall", "choco uninstall",
    "kill ", "killall", "taskkill",
    "git push --force", "git reset --hard",
]


def _classify(command: str) -> str:
    """判定命令危险等级：``blocked`` / ``danger`` / ``normal``。"""
    low = (command or "").lower()
    for pat in BLOCKED_PATTERNS:
        if pat in low:
            return "blocked"
    for pat in DANGER_PATTERNS:
        if pat in low:
            return "danger"
    return "normal"


def register(reg: Any, ctx: Any) -> None:
    def run_command(command: str, timeout: int = 30, cwd: str = "") -> Dict[str, Any]:
        """执行一条 Shell 命令。

        参数:
            command: 要执行的命令
            timeout: 超时秒数（最大 120）
            cwd: 工作目录，默认沙箱
        """
        level = _classify(command)
        if level == "blocked":
            return {"ok": False, "error": f"命令被安全策略拒绝：{command}"}

        timeout = max(1, min(int(timeout or 30), 120))
        work_dir = str(cwd or ctx.paths.sandbox_dir())

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=work_dir,
                capture_output=True,
                timeout=timeout,
                text=True,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"命令超时（{timeout}s）", "command": command}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": (proc.stdout or "")[:8000],
            "stderr": (proc.stderr or "")[:2000],
            "command": command,
            "risk": level,
        }

    def which(program: str) -> Dict[str, Any]:
        """检查一个可执行程序是否在 PATH 中。

        参数:
            program: 程序名，如 git / python
        """
        import shutil

        found = shutil.which(program)
        return {"program": program, "found": found or "", "ok": bool(found)}

    reg.register("run_command", run_command, risk=RISK_DANGER, category="shell",
                 description="执行 Shell 命令（受安全策略与超时约束）")
    reg.register("which", which, risk=RISK_DANGER, category="shell",
                 description="查找可执行程序路径",
                 parameters={"type": "object",
                             "properties": {"program": {"type": "string"}},
                             "required": ["program"]})
