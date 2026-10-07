"""文件系统工具。

路径规则（v0.7.0 起"让黑武士住进你的电脑"）：

    相对路径 → 一律解析到沙箱内；
    绝对路径 → 允许区 = 沙箱 ∪ **授权根目录** ∪（full_fs_access 时整台机器）。

"允许区"里的读写**不需要**危险授权（写沙箱外的旧行为被取消），
真正越出允许区的写操作仍需 danger 授权。用户用 ``grant_access``
授权自己的桌面/文档/项目目录，Agent 就能真正读写它们。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from ..registry import RISK_CAUTION, RISK_DANGER, RISK_SAFE


def _resolve(path: str, ctx: Any) -> Path:
    """把用户给的路径解析成本地绝对路径。

    相对路径 → 沙箱；绝对路径 → 规整并校验不越界（沙箱/授权目录/full_fs）。
    越界会被 :func:`paths.resolve_path` 抛 ``ValueError``，由注册表转成友好错误。
    """
    return ctx.paths.resolve_path(path)


def register(reg: Any, ctx: Any) -> None:
    """注册文件系统工具。"""

    def read_file(path: str, limit: int = 8000, offset: int = 0) -> str:
        """读取文件内容。

        参数:
            path: 文件路径（相对路径解析到沙箱）
            limit: 最多读取的字符数
            offset: 起始行号（从 0 开始）
        """
        p = _resolve(path, ctx)
        if not p.exists():
            return f"文件不存在：{p}"
        if p.is_dir():
            return f"这是一个目录：{p}"
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if offset:
            lines = lines[int(offset):]
        out = "\n".join(lines)
        if len(out) > int(limit):
            out = out[: int(limit)] + f"\n…（已截断，总 {len(text)} 字符）"
        return out

    def write_file(path: str, content: str, append: bool = False) -> str:
        """写入文件（会创建父目录）。

        参数:
            path: 文件路径（相对路径写入沙箱）
            content: 文件内容
            append: 是否追加
        """
        p = _resolve(path, ctx)
        p.parent.mkdir(parents=True, exist_ok=True)
        if append and p.exists():
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            p.write_text(content, encoding="utf-8")
        return f"已写入 {p}（{len(content)} 字符）"

    def list_dir(path: str = ".", max_items: int = 100) -> Dict[str, Any]:
        """列出目录内容。

        参数:
            path: 目录路径（默认沙箱根）
            max_items: 最多返回多少项
        """
        p = _resolve(path, ctx)
        if not p.exists():
            return {"error": f"路径不存在：{p}"}
        if p.is_file():
            return {"path": str(p), "type": "file",
                    "size": p.stat().st_size}
        items: List[Dict[str, Any]] = []
        for entry in sorted(p.iterdir())[: int(max_items)]:
            try:
                st = entry.stat()
                items.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": st.st_size if entry.is_file() else 0,
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                continue
        return {"path": str(p), "type": "dir", "items": items}

    def delete_file(path: str) -> str:
        """删除文件或空目录。

        参数:
            path: 要删除的路径
        """
        p = _resolve(path, ctx)
        if not p.exists():
            return f"路径不存在：{p}"
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                return f"目录非空，拒绝删除：{p}"
        else:
            p.unlink()
        return f"已删除 {p}"

    def file_info(path: str) -> Dict[str, Any]:
        """查看文件/目录信息。

        参数:
            path: 路径
        """
        p = _resolve(path, ctx)
        if not p.exists():
            return {"error": f"路径不存在：{p}"}
        st = p.stat()
        return {
            "path": str(p),
            "type": "dir" if p.is_dir() else "file",
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "in_sandbox": ctx.paths.within_sandbox(p),
        }

    def grant_access(path: str, reason: str = "") -> Dict[str, Any]:
        """授权我读写某个真实目录（桌面/文档/项目…），让我真正能帮你处理文件。

        参数:
            path: 要授权的目录绝对路径，如 C:\\Users\\你的用户名\\Desktop
            reason: 授权理由（会记入记忆，方便日后追溯）
        """
        raw = str(path or "").strip().strip('"')
        if not raw:
            return {"ok": False, "error": "缺少 path"}
        try:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                return {"ok": False,
                        "error": "请给绝对路径（如 C:\\Users\\你\\Desktop）"}
            p = p.resolve()
        except Exception as ex:
            return {"ok": False, "error": f"路径非法：{ex}"}
        # 指向文件时，授权其所在目录（更符合"授权一个地方"的语义）
        if p.is_file():
            p = p.parent
        if not p.is_dir():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception as ex:
                return {"ok": False,
                        "error": f"目录不存在且无法创建：{p}（{ex}）"}
        roots = list(ctx.config.get("allowed_paths", []) or [])
        s = str(p)
        if s in roots:
            return {"ok": True, "path": s, "already": True,
                    "allowed_paths": roots}
        roots.append(s)
        try:
            ctx.config.set("allowed_paths", roots)
            ctx.config.save()
            ctx.paths.set_allowed_roots(roots)
        except Exception as ex:
            return {"ok": False, "error": f"写入配置失败：{ex}"}
        note = ""
        if reason:
            try:
                ctx.memory.remember(f"授权访问目录 {s}", str(reason))
                note = "已记入长期记忆"
            except Exception:
                pass
        return {"ok": True, "path": s, "allowed_paths": roots, "note": note}

    def revoke_access(path: str) -> Dict[str, Any]:
        """撤销某个目录的读写授权。"""
        raw = str(path or "").strip().strip('"')
        if not raw:
            return {"ok": False, "error": "缺少 path"}
        try:
            target = str(Path(raw).expanduser().resolve())
        except Exception:
            target = raw
        roots = [r for r in (ctx.config.get("allowed_paths", []) or [])
                 if str(r).rstrip("\\/") != target.rstrip("\\/")]
        try:
            ctx.config.set("allowed_paths", roots)
            ctx.config.save()
            ctx.paths.set_allowed_roots(roots)
        except Exception as ex:
            return {"ok": False, "error": f"写入配置失败：{ex}"}
        return {"ok": True, "revoked": target, "allowed_paths": roots}

    def list_workspaces() -> Dict[str, Any]:
        """列出我现在能读写的所有目录（沙箱 + 你授权的目录）。"""
        try:
            return {
                "ok": True,
                "sandbox": str(ctx.paths.sandbox_dir()),
                # 必须转 str：allowed_roots() 给的是 Path 对象，
                # 直接丢给 json 序列化会炸（ToolResult.text 兜得住，
                # 但任何 JSON 接口都会 500）。
                "allowed_paths": [str(p) for p in ctx.paths.allowed_roots()],
                "full_fs_access": bool(ctx.paths.full_fs_access()),
                "note": ("full_fs_access=True：我能碰整台机器，请谨慎下达指令"
                         if ctx.paths.full_fs_access()
                         else "需要访问新目录时，用 grant_access 授权"),
            }
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    reg.register("read_file", read_file, risk=RISK_SAFE, category="filesystem",
                 description="读取文件内容（相对路径解析到沙箱；绝对路径需在允许区）")
    reg.register("write_file", write_file, risk=RISK_CAUTION, category="filesystem",
                 description="写入文件，自动创建父目录")
    reg.register("list_dir", list_dir, risk=RISK_SAFE, category="filesystem",
                 description="列出目录内容")
    reg.register("file_info", file_info, risk=RISK_SAFE, category="filesystem",
                 description="查看文件或目录信息")
    reg.register("delete_file", delete_file, risk=RISK_DANGER, category="filesystem",
                 description="删除文件或空目录（危险，需授权）")
    reg.register("grant_access", grant_access, risk=RISK_CAUTION,
                 category="filesystem",
                 description="授权我读写某个真实目录（桌面/文档/项目），"
                             "让我能真正帮你处理文件")
    reg.register("revoke_access", revoke_access, risk=RISK_CAUTION,
                 category="filesystem",
                 description="撤销某个目录的读写授权")
    reg.register("list_workspaces", list_workspaces, risk=RISK_SAFE,
                 category="filesystem",
                 description="列出我现在能读写的所有目录")
