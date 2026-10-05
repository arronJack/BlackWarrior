"""文件系统工具。

路径规则：相对路径一律解析到沙箱内；绝对路径按策略层判定（写沙箱外需授权）。
这样 Agent 默认在"自己的地盘"里活动，不会误伤用户文件。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from ..registry import RISK_CAUTION, RISK_DANGER, RISK_SAFE


def _resolve(path: str, ctx: Any) -> Path:
    """把用户给的路径解析成本地绝对路径。"""
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        joined = ctx.paths.safe_join_sandbox(str(p))
        if joined is None:
            raise ValueError(f"非法路径：{path}")
        return joined
    return p


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

    reg.register("read_file", read_file, risk=RISK_SAFE, category="filesystem",
                 description="读取文件内容（相对路径解析到沙箱）")
    reg.register("write_file", write_file, risk=RISK_CAUTION, category="filesystem",
                 description="写入文件，自动创建父目录")
    reg.register("list_dir", list_dir, risk=RISK_SAFE, category="filesystem",
                 description="列出目录内容")
    reg.register("file_info", file_info, risk=RISK_SAFE, category="filesystem",
                 description="查看文件或目录信息")
    reg.register("delete_file", delete_file, risk=RISK_DANGER, category="filesystem",
                 description="删除文件或空目录（危险，需授权）")
