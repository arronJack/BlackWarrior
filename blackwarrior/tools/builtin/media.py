"""媒体库工具（v0.5）—— 让模型能查、能扫、能读单个文件的元数据。

设计原则与其它工具一致：

* 全部 ``safe`` 风险级别—— 只读元数据，不改用户文件，也不删任何东西；
* **不猜**：读不到的字段就是 ``null``，工具描述里写明"未解析"，
  避免模型把"专辑未知"说成"专辑是某张同名专辑"；
*扫描是显式动作（``scan_media``），不在心跳里自动跑 ——
  几万文件的目录不该在用户没要求时占满磁盘。
"""

from __future__ import annotations

from typing import Any, List

from ..registry import RISK_SAFE


def register(reg: Any, ctx: Any) -> List[str]:
    """注册媒体相关工具。"""
    names: List[str] = []

    def _slim(item: dict) -> dict:
        """裁剪成给模型看的精简结构（原始行有 17 列，浪费 token）。"""
        out = {
            "name": item.get("name"),
            "category": item.get("category"),
            "title": item.get("title"),
        }
        for key in ("artist", "album", "year"):
            if item.get(key):
                out[key] = item.get(key)
        if item.get("duration"):
            out["duration"] = item.get("duration")
        if item.get("width") and item.get("height"):
            out["size_px"] = f"{item['width']}x{item['height']}"
        elif item.get("sample_rate"):
            out["sample_rate"] = item.get("sample_rate")
        out["path"] = item.get("path")
        return out

    # ---------------------------------------------------------- 库概览

    def media_stats() -> dict:
        """媒体库总览：条目数、分类分布、总时长、已登记目录数。

        没登记过目录时返回 note 提示怎么用，不返回空壳。
        """
        st = ctx.media.stats()
        if not st.get("available"):
            return st
        if not st.get("total"):
            st["note"] = ("媒体库为空。用 add_media_root 登记一个目录，"
                          "再 scan_media 扫描。")
        return st

    reg.register("media_stats", media_stats, risk="safe", category="media",
                 description="媒体库总览：条目数/分类分布/总时长/已登记目录数")
    names.append("media_stats")

    # ---------------------------------------------------------- 登记目录

    def add_media_root(path: str, recursive: bool = True) -> dict:
        """登记一个媒体目录（音乐/视频/图片所在位置）。

        只登记不扫描 —— 扫描请显式调 scan_media，避免大目录占满磁盘。
        目录不存在 / 不是目录时**抛异常**（不当成成功返回），
        否则模型会以为登记好了，下一句"扫一下"只得到"0 个目录"。
        """
        r = ctx.media.add_root(path, recursive)
        if not r.get("ok"):
            raise ValueError(str(r.get("reason") or "登记失败"))
        return {"ok": True, "path": r.get("path", str(path)),
                "recursive": bool(r.get("recursive", recursive))}

    reg.register("add_media_root", add_media_root, risk="safe", category="media",
                 description="登记媒体目录（不立即扫描，扫描请用 scan_media）")
    names.append("add_media_root")

    def remove_media_root(path: str) -> dict:
        """取消登记一个媒体目录（不删除磁盘文件，只是不再索引）。"""
        return ctx.media.remove_root(path)

    reg.register("remove_media_root", remove_media_root, risk="safe",
                 category="media",
                 description="取消登记媒体目录（不删磁盘文件，只是不再索引）")
    names.append("remove_media_root")

    # ---------------------------------------------------------- 扫描

    def scan_media(full: bool = False) -> dict:
        """扫描已登记的媒体目录并更新索引。

        默认增量：只重新解析新增/改动过的文件。``full=True`` 强制全量重扫。
        目录很大时会有文件数与时间上限，届时 truncated=true 表示结果不完整。
        """
        return ctx.media.scan(full=bool(full))

    reg.register("scan_media", scan_media, risk="safe", category="media",
                 description="扫描已登记的媒体目录（默认增量，full=true 全量重扫）")
    names.append("scan_media")

    # ---------------------------------------------------------- 查询

    def list_media(category: str = "", query: str = "", limit: int = 30) -> dict:
        """列出媒体库条目。

        category 取 audio / video / image；query 按标题/艺术家/专辑/文件名模糊匹配。
        """
        if category and category not in ("audio", "video", "image"):
            return {"error": f"category 只能是 audio/video/image，收到 {category}"}
        items = ctx.media.list(category=category or None, query=query or None,
                               limit=int(limit or 30))
        return {"count": len(items), "items": [_slim(i) for i in items]}

    reg.register("list_media", list_media, risk="safe", category="media",
                 description="列出媒体库条目（可按 audio/video/image 分类与关键词过滤）")
    names.append("list_media")

    def probe_media(path: str) -> dict:
        """读取单个媒体文件的元数据（不依赖是否已入库）。

        字段读不到就是 null —— 这是内置解析器的诚实边界，不是 bug。
        """
        from ...media.library import probe as _probe

        return _probe(path)

    reg.register("probe_media", probe_media, risk="safe", category="media",
                 description="读取单个媒体文件元数据（标题/艺术家/时长/尺寸，未解析字段为 null）")
    names.append("probe_media")

    return names


def selftest() -> bool:
    """工具层自检：确认真实注册且元数据裁剪不丢关键字段。"""
    import os
    import struct
    import tempfile

    from ...config import Config
    from ...db.store import Store
    from ..registry import ToolRegistry

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(f"  {'v' if cond else 'x'} {msg}")
        ok = ok and bool(cond)

    d = tempfile.mkdtemp(prefix="bw-media-tools-")
    store = Store(path=os.path.join(d, "t.db"))
    cfg = Config(path=os.path.join(d, "config.json"))

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.store = store
    ctx.config = cfg
    from ...media.library import MediaLibrary

    ctx.media = MediaLibrary(store, cfg)

    reg = ToolRegistry()
    names = register(reg, ctx)
    for want in ("media_stats", "add_media_root", "remove_media_root",
                 "scan_media", "list_media", "probe_media"):
        check(want in names, f"工具 {want} 已注册")
    for spec in reg.specs():
        if spec.name in names:
            check(spec.risk == RISK_SAFE, f"{spec.name} 风险级别为 safe")

    # 端到端：登记 → 扫描 → 查询
    root = os.path.join(d, "lib")
    os.makedirs(root)
    t = "测试歌曲".encode("utf-8")
    pl = b"\x00" + t
    fr = b"TIT2" + struct.pack(">I", len(pl)) + b"\x00\x00" + pl
    n = len(fr)
    sy = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    with open(os.path.join(root, "s.mp3"), "wb") as f:
        f.write(b"ID3\x03\x00\x00" + sy + fr + b"\xff\xfb\x90\x00" + b"\x00" * 512)
    ihdr = struct.pack(">II", 800, 600)
    with open(os.path.join(root, "p.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + ihdr
                + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)

    check(ctx.media.add_root(root)["ok"], "add_root 成功")
    r = ctx.media.scan()
    check(r["added"] == 2, f"扫描入库 2 项（{r['added']}）")
    items = ctx.media.list(category="audio")
    check(len(items) == 1 and items[0]["title"] == "测试歌曲",
          f"查询 audio 得测试歌曲（{items[0]['title'] if items else '空'}）")
    imgs = ctx.media.list(category="image")
    check(bool(imgs) and imgs[0]["width"] == 800, "图片尺寸已解析")

    # 走 registry 真调用一次，确认 handler 签名能被 schema 推导
    res = reg.call("media_stats", {})
    check(res.ok and res.result.get("total") == 2,
          f"registry.call(media_stats) 可用（{res.result.get('total')}）")
    res2 = reg.call("list_media", {"category": "audio"})
    check(res2.ok and res2.result.get("count") == 1,
          "registry.call(list_media) 可用")
    res3 = reg.call("list_media", {"category": "bogus"})
    check((not res3.ok) or "error" in (res3.result or {}),
          "非法 category 被拒（不静默）")

    store.close()
    return ok
