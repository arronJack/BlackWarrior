"""本地媒体库索引（v0.5）—— **纯标准库实现**。

## 为什么不用 mutagen

实测本机 `mutagen` / `Pillow` 都不可用。如果媒体库建立在这些库之上，
那么在绝大多数没装它们的机器上，这项能力会**整块消失** —— 违背黑武士
"永不隐藏降级"的立身之本。所以这里的做法是：**自己解析文件头**，
把"零依赖可用"作为底线；有 mutagen 时再借它提高精度（可选增强）。

## 支持的格式与能解到什么程度

| 格式 | 容器 | 内置能读| 装了 mutagen 能读更多 |
|---|---|---|---|
| `.mp3` | ID3v2 | 标题/艺术家/专辑/年份/时长 | 同（内置已够） |
| `.flac` | FLAC + Vorbis Comment | 全部标签 + 位深/采样率 | 同 |
| `.m4a`/`.mp4` | MP4 atoms | 标题/艺术家/专辑/时长 | 同 |
| `.wav` | RIFF | 时长/采样率/位深/声道 | 同 |
| `.ogg` | Ogg Vorbis | 标题/艺术家 | 同 |
| `.jpg`/`.png` | 图像 | 尺寸（纯读文件头） | EXIF（需 Pillow） |
| `.mp4`/`.mkv`/`.webm` | 视频容器 | 文件大小 + 时长（若容器可解析） | 同 |

**读不到就标 `None`，绝不编造。** ``reader`` 字段会说明这次元数据
是"内置解析"还是"mutagen"，让 UI 和模型都知道可信度。

## 增量扫描

媒体库最忌讳每次全盘重扫。这里按 ``(路径, 大小, mtime)`` 三元组做指纹，
只处理变化过的文件；``--full`` 可强制重建。扫描结果落``media_items`` 表，
UI 与工具都从库里读，不重复碰磁盘。
"""

from __future__ import annotations

import json
import os
import struct
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

#: 媒体分类 -> 扩展名。分类决定 UI 分组与工具语义。
CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "audio": (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".wma"),
    "video": (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v"),
    "image": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic"),
}

#: 反向索引：扩展名 -> 分类
_EXT2CAT: Dict[str, str] = {
    ext: cat for cat, exts in CATEGORIES.items() for ext in exts
}

#: 忽略的目录名（扫描时跳过，避免无谓遍历）
_SKIP_DIRS = {
    "$recycle.bin", "system volume information", ".git", "node_modules",
    "__pycache__", ".cache", "appdata", "$windows.~bt",
}


def category_of(path: str) -> Optional[str]:
    """按扩展名判定媒体分类，不是媒体文件返回 ``None``。"""
    return _EXT2CAT.get(Path(str(path)).suffix.lower())


# ---------------------------------------------------------------- 文本解码


def _decode_text(raw: bytes) -> str:
    """把标签字节解成字符串，尽量不抛异常。

    标签编码在现实世界里非常混乱：ID3 可能是 UTF-16 带 BOM、GBK、
    Latin-1 甚至全角。策略是逐个试，第一个"能解且不像乱码"的胜出。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "utf-16", "gb18030", "big5", "latin-1"):
        try:
            s = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 出现替换字符/控制字符就判定这个编码不对
        if "\ufffd" in s or sum(1 for c in s if ord(c) < 9) > len(s) * 0.05:
            continue
        return s.strip(" \x00")
    return raw.decode("latin-1", "replace").strip(" \x00")


def _clean(s: str, limit: int = 200) -> str:
    """清掉标签里常见的垃圾：NUL、控制字符、超长。"""
    s = "".join(ch for ch in str(s or "") if ch.isprintable() or ch == " ")
    return " ".join(s.split())[:limit]


# ---------------------------------------------------------------- 各格式解析


def _decode_text_frame(payload: bytes) -> str:
    """解析 ID3 文本帧。

    ★ 关键：帧首字节是**编码标识**，不是数据：
    ``0x00``=ISO-8859-1(latin1)``0x01``=UTF-16带BOM``0x02``=UTF-16BE
    ``0x03``=UTF-8（**ID3v2.4 才引入**）。

    现实里最坑的是：**v2.3 文件里出现 UTF-8 中文，但编码字节写0x00**
    （很多国产写入器的老 bug）。所以不能只信编码字节——
    先试 UTF-8，能干净解出就按UTF-8 走，解不出再按 latin1。
    这就是下面 latin1 分支放在最后的原因。
    """
    if not payload:
        return ""
    enc_byte = payload[0]
    data = payload[1:]
    if enc_byte == 0x01:                      # UTF-16带 BOM
        return _clean(_decode_text(data))
    if enc_byte == 0x02:                      # UTF-16BE 无 BOM
        try:
            return _clean(data.decode("utf-16-be"))
        except UnicodeDecodeError:
            pass
    if enc_byte == 0x03:                      # v2.4 明确 UTF-8
        return _clean(_decode_text(data))
    # enc_byte == 0x00 或非法值：先按 UTF-8 试（覆盖"标 0 实为 UTF-8"的老文件）
    try:
        s = data.decode("utf-8")
        if "\ufffd" not in s:
            return _clean(s)
    except UnicodeDecodeError:
        pass
    return _clean(data.decode("latin-1", "replace"))


def _read_id3v2(path: Path) -> Dict[str, Any]:
    """解析 MP3 的 ID3v2 标签（v2.2/2.3/2.4 通用）。"""
    out: Dict[str, Any] = {}
    with path.open("rb") as f:
        head = f.read(10)
        if len(head) < 10 or head[:3] != b"ID3":
            return out
        major = head[3]
        # ★ ID3 头 size 的编码**按版本不同**（2026-10 踩过）：
        #   v2.4 用「同步安全整数」——每字节只取低 7 位（0x7F 掩码）；
        #   v2.2/2.3 的 size 字节是**普通字节**，最高位可能为 1，
        #   若也套 0x7F 掩码，超过 128 字节的标签会被读成 0 → 整块标签解析失效。
        #   帧 size 同理（见下方循环）。
        if major >= 4:
            size = sum((head[6 + i] & 0x7F) << (21 - 7 * i) for i in range(4))
        else:
            size = int.from_bytes(head[6:10], "big")
        body = f.read(max(0, min(size, 4 * 1024 * 1024)))

    # ★ 帧头长度三版各不相同（2026-10 踩过，被坑了一次乱码）：
    #   v2.2：3 字节 ID + 3 字节 size               → 头长 6，**无 flags**
    #   v2.3：4 字节 ID + 4 字节 size + 2 字节 flags → 头长 10
    #   v2.4：4 字节 ID + 4 字节 size + 2 字节 flags → 头长 10
    # 漏掉 v2.3 的 2 字节 flags 会让 payload 起点前移 2 字节，
    # 解出来就是「测试歌曲」被截成「试歌曲」这种半截错位文本。
    if major == 2:
        id_len, size_len, flag_len = 3, 3, 0
    else:
        id_len, size_len, flag_len = 4, 4, 2
    head_len = id_len + size_len + flag_len

    fields = {"TIT2": "title", "TT2": "title", "TPE1": "artist",
              "TP1": "artist", "TALB": "album", "TAL": "album",
              "TYER": "year", "TDRC": "year", "TYE": "year"}
    i = 0
    while i + head_len <= len(body):
        fid = body[i:i + id_len]
        if not fid.strip(b"\x00"):
            break
        try:
            name = fid.decode("ascii")
        except UnicodeDecodeError:
            break
        chunk = body[i + id_len:i + id_len + size_len]
        if len(chunk) < size_len:
            break
        if major == 2:
            # v2.2 帧头 3 字节；普通字节
            fsize = int.from_bytes(chunk[:3], "big")
        elif major >= 4:
            # v2.4 帧头 4 字节；同步安全整数
            fsize = sum((chunk[j] & 0x7F) << (21 - 7 * j) for j in range(4))
        else:
            # v2.3 帧头 4 字节；普通字节（不能掩码，最高位可能是长度的一部分）
            fsize = int.from_bytes(chunk[:4], "big")
        fsize = max(0, min(fsize, len(body) - i - head_len))
        payload = body[i + head_len:i + head_len + fsize]
        key = fields.get(name)
        if key and payload:
            val = _decode_text_frame(payload)
            if val:
                out.setdefault(key, val)
        i += head_len + fsize
    return out


def _read_mp3_duration(path: Path) -> Optional[float]:
    """估算 MP3 时长：优先读 Xing/VBRI 帧，无则用文件大小/bitrate 粗估。"""
    size = path.stat().st_size
    try:
        with path.open("rb") as f:
            # 跳过 ID3v2
            head = f.read(10)
            offset = 10
            if len(head) >= 10 and head[:3] == b"ID3":
                if head[3] >= 4:
                    n = sum((head[6 + i] & 0x7F) << (21 - 7 * i)
                            for i in range(4))
                else:
                    n = 0
                    for i in range(4, 8):
                        n = (n << 7) | (head[i] & 0x7F)
                offset = 10 + n
            f.seek(offset)
            # Xing/Info 头在首帧内，读 4KB 足够覆盖
            buf = f.read(4096)
    except OSError:
        return None
    for tag in (b"Xing", b"Info"):
        i = buf.find(tag)
        if i > 0:
            try:
                flags = struct.unpack(">I", buf[i + 4:i + 8])[0]
                if flags & 0x1:            # 有帧数
                    frames = struct.unpack(">I", buf[i + 8:i + 12])[0]
                    # 采样率由首帧头推：这里用 CBR 常见 44.1k + 1152 帧/帧
                    br = _mp3_bitrate_from_header(buf, i)
                    if br > 0 and frames:
                        return round(frames * 1152 / br, 1)
            except (struct.error, IndexError):
                pass
    # 粗估：按 128 kbps 猜，并在结果上标注这是估算
    return round(size * 8 / 128_000, 1)


def _mp3_bitrate_from_header(buf: bytes, xing_at: int) -> int:
    """从首帧头推比特率（kbps）。失败返回 0。"""
    i = 0
    while i < min(xing_at, len(buf) - 4):
        if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
            ver = (buf[i + 1] >> 3) & 0x03
            layer = (buf[i + 1] >> 1) & 0x03
            if ver == 3 and layer == 1:      # MPEG1 Layer3
                br_idx = (buf[i + 2] >> 4) & 0x0F
                rates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224,
                         256, 320, 0]
                return rates[br_idx] if br_idx < len(rates) else 0
            return 0
        i += 1
    return 0


def _read_flac(path: Path) -> Dict[str, Any]:
    """解析 FLAC：STREAMINFO（采样率/位深/时长）+ Vorbis Comment 标签。"""
    out: Dict[str, Any] = {}
    with path.open("rb") as f:
        if f.read(4) != b"fLaC":
            return out
        while True:
            hdr = f.read(4)
            if len(hdr) < 4:
                break
            last = bool(hdr[0] & 0x80)
            btype = hdr[0] & 0x7F
            blen = int.from_bytes(hdr[1:4], "big")
            if btype == 0:                    # STREAMINFO
                body = f.read(blen)
                if len(body) >= 18:
                    # 20 bits 采样率 · 3 bits (channels-1) · 5 bits (bps-1) · 36 bits 样本数
                    rate = (body[10] << 12) | (body[11] << 4) | (body[12] >> 4)
                    channels = ((body[12] >> 1) & 0x07) + 1
                    bps = (((body[12] & 0x01) << 4) | (body[13] >> 4)) + 1
                    total = ((body[13] & 0x0F) << 32) | int.from_bytes(
                        body[14:18], "big")
                    out["sample_rate"] = rate
                    out["channels"] = channels
                    out["bit_depth"] = bps
                    if rate:
                        out["duration"] = round(total / rate, 1)
            elif btype == 4:                  # VORBIS_COMMENT
                body = f.read(blen)
                out.update(_parse_vorbis_comment(body))
                if last:
                    break
            else:
                f.seek(blen, os.SEEK_CUR)
            if last:
                break
    return out


def _parse_vorbis_comment(body: bytes) -> Dict[str, Any]:
    """解析 Vorbis Comment 块：4 字节厂商长度 + 串 + 4 字节条目数 + 条目。"""
    out: Dict[str, Any] = {}
    try:
        p = 0
        vlen = struct.unpack("<I", body[p:p + 4])[0]
        p += 4 + vlen
        count = struct.unpack("<I", body[p:p + 4])[0]
        p += 4
        keys = {"title": "title", "artist": "artist", "album": "album",
                "albumartist": "album_artist", "date": "year",
                "tracknumber": "track_no"}
        for _ in range(min(count, 64)):        # 防御：恶意文件声明超大条目数
            if p + 4 > len(body):
                break
            n = struct.unpack("<I", body[p:p + 4])[0]
            p += 4
            if n > len(body) - p or n < 0:
                break
            item = body[p:p + n]
            p += n
            if b"=" not in item:
                continue
            k, v = item.split(b"=", 1)
            key = keys.get(k.decode("latin-1").lower())
            val = _clean(_decode_text(v))
            if key and val:
                out.setdefault(key, val)
    except (struct.error, IndexError):
        pass
    return out


def _read_mp4(path: Path) -> Dict[str, Any]:
    """解析 MP4/M4A：走 atom 树找 moov.udta.meta 与 mvhd 时长。"""
    out: Dict[str, Any] = {}

    def _walk(f: Any, end: int, depth: int = 0) -> None:
        """深度优先找目标 atom。depth 上限防御畸形文件的深嵌套。"""
        if depth > 6:
            return
        while f.tell() < end - 8:
            start = f.tell()
            try:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return
                size = struct.unpack(">I", hdr[:4])[0]
                name = hdr[4:8]
                if size == 1:                # 64 位长度
                    ext = f.read(8)
                    if len(ext) < 8:
                        return
                    size = struct.unpack(">Q", ext)[0]
                if size == 0:
                    size = end - start
                if size < 8:
                    return
                body_end = min(start + size, end)
                if name in (b"moov", b"udta", b"trak", b"mdia"):
                    _walk(f, body_end, depth + 1)
                elif name == b"meta":
                    f.seek(start + 8)        # meta 是 FullBox，多 4 字节 version/flags
                    _read_mp4_meta(f, body_end, out)
                elif name == b"mvhd":
                    _read_mp4_mvhd(f, out)
                else:
                    f.seek(body_end)
            except (struct.error, OSError):
                return

    try:
        with path.open("rb") as f:
            _walk(f, path.stat().st_size)
    except OSError:
        pass
    return out


def _read_mp4_mvhd(f: Any, out: Dict[str, Any]) -> None:
    """mvhd 里有时长（timescale + duration）。"""
    data = f.read(32)
    if len(data) < 20:
        return
    ver = data[0]
    try:
        if ver == 1 and len(data) >= 32:
            scale = struct.unpack(">I", data[20:24])[0]
            dur = struct.unpack(">Q", data[24:32])[0]
        else:
            scale = struct.unpack(">I", data[12:16])[0]
            dur = struct.unpack(">I", data[16:20])[0]
        if scale:
            out["duration"] = round(dur / scale, 1)
    except struct.error:
        pass


_MP4_META_KEYS = {b"\xa9nam": "title", b"\xa9ART": "artist",
                  b"\xa9alb": "album", b"\xa9day": "year"}


def _read_mp4_meta(f: Any, end: int, out: Dict[str, Any]) -> None:
    """在 meta atom 里找 ilst 的 ilst 条目（标题/艺术家/专辑/年份）。"""
    while f.tell() < end - 8:
        try:
            hdr = f.read(8)
            if len(hdr) < 8:
                return
            size = struct.unpack(">I", hdr[:4])[0]
            name = hdr[4:8]
            if size < 8:
                return
            body_end = min(f.tell() - 8 + size, end)
            if name == b"ilst":
                while f.tell() < body_end - 8:
                    ih = f.read(8)
                    if len(ih) < 8:
                        break
                    isize = struct.unpack(">I", ih[:4])[0]
                    iname = ih[4:8]
                    if isize < 8:
                        break
                    ibody_end = min(f.tell() - 8 + isize, body_end)
                    key = _MP4_META_KEYS.get(iname)
                    if key:
                        _read_mp4_ilst_value(f, ibody_end, out, key)
                    else:
                        f.seek(ibody_end)
                return
            f.seek(body_end)
        except (struct.error, OSError):
            return


def _read_mp4_ilst_value(f: Any, end: int, out: Dict[str, Any], key: str) -> None:
    """ilst 条目里找 data atom（type=1 即 UTF-8 文本）。"""
    while f.tell() < end - 8:
        try:
            hdr = f.read(8)
            if len(hdr) < 8:
                return
            size = struct.unpack(">I", hdr[:4])[0]
            if size < 8:
                return
            body_end = min(f.tell() - 8 + size, end)
            if hdr[4:8] == b"data" and body_end - f.tell() > 8:
                payload = f.read(body_end - f.tell())
                if len(payload) > 8:
                    val = _clean(_decode_text(payload[8:]))
                    if val:
                        out.setdefault(key, val)
            else:
                f.seek(body_end)
        except (struct.error, OSError):
            return


def _read_wav(path: Path) -> Dict[str, Any]:
    """解析 WAV：fmt chunk 给采样率/位深/声道，data 给时长。"""
    out: Dict[str, Any] = {}
    try:
        with path.open("rb") as f:
            if f.read(4) != b"RIFF":
                return out
            f.read(4)
            if f.read(4) != b"WAVE":
                return out
            byte_rate = 0
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid = hdr[:4]
                size = struct.unpack("<I", hdr[4:])[0]
                body = f.read(min(size, 64))
                if cid == b"fmt " and len(body) >= 16:
                    channels, rate = struct.unpack("<HI", body[2:8])
                    bits = struct.unpack("<H", body[14:16])[0]
                    byte_rate = rate * channels * bits // 8
                    out["channels"] = channels
                    out["sample_rate"] = rate
                    out["bit_depth"] = bits
                elif cid == b"data":
                    if byte_rate:
                        out["duration"] = round(size / byte_rate, 1)
                    break
    except (OSError, struct.error):
        pass
    return out


def _read_image_size(path: Path) -> Dict[str, Any]:
    """纯读文件头拿图像尺寸，不解码像素。"""
    out: Dict[str, Any] = {}
    try:
        with path.open("rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
                out["width"], out["height"] = struct.unpack(">II", head[16:24])
            elif head[:2] == b"\xff\xd8":                     # JPEG：逐段扫 SOF
                f.seek(2)
                while True:
                    b = f.read(4)
                    if len(b) < 4 or b[0] != 0xFF:
                        break
                    marker = b[1]
                    if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                        continue
                    seg = f.read(2)
                    if len(seg) < 2:
                        break
                    ln = struct.unpack(">H", seg)[0]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                                  0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        body = f.read(7)
                        if len(body) >= 5:
                            out["height"], out["width"] = struct.unpack(
                                ">HH", body[1:5])
                        break
                    f.seek(ln - 2, os.SEEK_CUR)
            elif head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
                out["width"], out["height"] = struct.unpack(
                    "<HH", head[6:10])
            elif head[:2] == b"BM" and len(head) >= 26:
                out["width"], out["height"] = struct.unpack("<ii", head[18:26])
    except (OSError, struct.error):
        pass
    return out


def _read_ogg(path: Path) -> Dict[str, Any]:
    """Ogg Vorbis：定位注释头（头包类型 3）读 Vorbis Comment。"""
    out: Dict[str, Any] = {}
    try:
        with path.open("rb") as f:
            data = f.read(65536)
    except OSError:
        return out
    i = data.find(b"\x03vorbis")
    if i > 0:
        out.update(_parse_vorbis_comment(data[i + 7:]))
    return out


def _read_with_mutagen(path: Path) -> Optional[Dict[str, Any]]:
    """装了 mutagen 就借它提高精度（可选增强）。没装返回 None。"""
    try:
        import mutagen
    except Exception:
        return None
    try:
        obj = mutagen.File(str(path), easy=True)
        if obj is None:
            return None
        easy = dict(obj) or {}
        info: Dict[str, Any] = {}
        for key in ("title", "artist", "album", "date"):
            val = easy.get(key)
            if val:
                info[key] = _clean(str(val[0] if isinstance(val, list) else val))
        length = getattr(obj, "info", None)
        if length is not None:
            try:
                if float(length.length):
                    info["duration"] = round(float(length.length), 1)
            except (AttributeError, TypeError, ValueError):
                pass
        return info or None
    except Exception:
        # mutagen 对畸形文件抛的异常五花八门，一律降级到内置解析
        return None


#: 扩展名 -> 内置解析器
_READERS: Dict[str, Callable[[Path], Dict[str, Any]]] = {
    ".mp3": lambda p: {**_read_id3v2(p), "duration": _read_mp3_duration(p)},
    ".flac": _read_flac,
    ".m4a": _read_mp4, ".mp4": _read_mp4, ".m4v": _read_mp4,
    ".mov": _read_mp4,
    ".wav": _read_wav,
    ".ogg": _read_ogg,
    ".jpg": _read_image_size, ".jpeg": _read_image_size,
    ".png": _read_image_size, ".gif": _read_image_size,
    ".bmp": _read_image_size, ".webp": _read_image_size,
}


def probe(path: str) -> Dict[str, Any]:
    """探测单个媒体文件的元数据。

    返回里``available`` 恒为 ``True``—— 因为纯标准库路径永远能给出
    至少文件级信息（大小/类型）；拿不到的字段是 ``None``，不会编。
    """
    p = Path(path)
    cat = category_of(path)
    info: Dict[str, Any] = {
        "path": str(p),
        "name": p.name,
        "category": cat,
        "available": cat is not None,
    }
    try:
        st = p.stat()
        info["size"] = st.st_size
        info["mtime"] = st.st_mtime
    except OSError as ex:
        info.update({"available": False, "error": f"{type(ex).__name__}: {ex}"})
        return info
    if cat is None:
        info["error"] = "非媒体文件"
        return info

    suffix = p.suffix.lower()
    reader = _READERS.get(suffix)
    data: Dict[str, Any] = {}
    if reader is not None:
        try:
            data = reader(p) or {}
            info["reader"] = "builtin"
        except Exception as ex:
            # 解析器自身出错不影响其余字段（大小/类型仍有效）
            info["parse_error"] = f"{type(ex).__name__}: {ex}"

    # mutagen 是增强：有就用它补/覆盖，失败静默回落到内置结果
    if _read_with_mutagen is not None:
        extra = _read_with_mutagen(p)
        if extra:
            for k, v in extra.items():
                if v not in (None, ""):
                    data[k] = v
            info["reader"] = "builtin+mutagen"
    info["reader"] = info.get("reader", "builtin")

    # 统一字段，缺失就是 None（不编造）
    for key in ("title", "artist", "album", "year", "duration",
                "sample_rate", "bit_depth", "channels", "width", "height"):
        val = data.get(key)
        info[key] = val if val not in ("", None) else None
    info["title"] = info["title"] or p.stem
    return info


# ---------------------------------------------------------------- 库管理


class MediaLibrary:
    """媒体库：扫描 → 入库 → 查询。

    指纹策略：``(size, mtime)``。变了才重新解析，没变直接跳过 ——
    音乐库动辄几万文件，全量重解析一次要几十秒，不能每次查询都做。
    """

    #: 单次扫描最多处理多少文件（防御超大目录/网络盘卡死）
    MAX_FILES = 20000
    #: 扫描耗时上限（秒），超时就返回已扫部分并如实标注"已截断"
    TIME_BUDGET = 20.0

    def __init__(self, store: Any, config: Any = None) -> None:
        self.store = store
        self.config = config

    # -- 配置

    def _enabled(self, key: str, default: bool = False) -> bool:
        if self.config is None:
            return default
        try:
            return bool(self.config.get(key, default))
        except Exception:
            return default

    def roots(self) -> List[str]:
        """已登记的媒体根目录。"""
        try:
            return [str(r.get("path")) for r in self.store.list_media_roots()
                    if r.get("enabled", 1)]
        except Exception:
            return []

    def add_root(self, path: str, recursive: bool = True) -> Dict[str, Any]:
        """登记一个媒体目录。不立刻全扫（可能几万文件），只登记。"""
        p = Path(str(path)).expanduser()
        if not p.exists():
            return {"ok": False, "reason": f"目录不存在：{p}"}
        if not p.is_dir():
            return {"ok": False, "reason": f"不是目录：{p}"}
        self.store.add_media_root(str(p.resolve()), recursive)
        return {"ok": True, "path": str(p.resolve()), "recursive": bool(recursive)}

    def remove_root(self, path: str) -> Dict[str, Any]:
        """取消登记一个媒体目录（不删磁盘文件）。"""
        raw = str(path)
        # add_root 存的是 resolve 后的绝对路径，这里必须**同样归一**，
        # 否则用户换个路径写法（相对路径 / 尾斜杠 / ..）就删不掉。
        try:
            target = str(Path(raw).expanduser().resolve())
        except (OSError, ValueError, RuntimeError):
            target = raw
        ok = self.store.remove_media_root(target) or self.store.remove_media_root(raw)
        return {"ok": bool(ok), "path": target}

    # -- 扫描

    def scan(self, full: bool = False) -> Dict[str, Any]:
        """扫描所有已登记根目录，增量入库。

        返回里``truncated=True`` 表示触达文件数/时间上限提前收手——
        这时结果是不完整的，必须让调用方知道，不能装作扫完了。
        """
        if not self._enabled("media_enabled", True):
            return {"ok": False, "skipped": True,
                    "reason": "媒体库未启用（配置 media_enabled=false）",
                    "roots": 0, "scanned": 0, "added": 0, "updated": 0,
                    "removed": 0, "truncated": False}

        roots = self.roots()
        if not roots:
            return {"ok": True, "roots": 0, "scanned": 0, "added": 0,
                    "updated": 0, "removed": 0, "truncated": False,
                    "note": "尚未登记任何媒体目录"}

        t0 = time.time()
        seen = 0
        added = updated = 0
        truncated = False

        for root in roots:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            recursive = True
            try:
                row = self.store.query_one(
                    "SELECT recursive FROM media_roots WHERE path=?", (root,))
                recursive = bool((row or {}).get("recursive", 1))
            except Exception:
                pass
            walker = (root_path.rglob("*") if recursive
                      else root_path.glob("*"))
            for fp in walker:
                if seen >= self.MAX_FILES:
                    truncated = True
                    break
                if time.time() - t0 > self.TIME_BUDGET:
                    truncated = True
                    break
                if category_of(fp.name) is None:
                    continue
                seen += 1
                try:
                    fp_stat = fp.stat()
                except OSError:
                    continue
                try:
                    existing = self.store.query_one(
                        "SELECT size, mtime FROM media_items WHERE path=?",
                        (str(fp),))
                except Exception:
                    existing = None
                if existing and not full:
                    if (existing.get("size") == fp_stat.st_size
                            and abs((existing.get("mtime") or 0)
                                    - fp_stat.st_mtime) < 1.0):
                        continue                     # 未变化，跳过解析
                info = probe(str(fp))
                try:
                    if self.store.upsert_media(info):
                        if existing:
                            updated += 1
                        else:
                            added += 1
                except Exception:
                    continue
            if truncated:
                break

        removed = 0
        try:
            removed = self.store.prune_media(roots)
        except Exception:
            removed = 0

        return {
            "ok": True,
            "roots": len(roots),
            "scanned": seen,
            "added": added,
            "updated": updated,
            "removed": removed,
            "truncated": truncated,
            "elapsed": round(time.time() - t0, 2),
            "note": ("已达文件数/时间上限提前收手，结果不完整"
                     if truncated else ""),
        }

    # -- 查询

    def stats(self) -> Dict[str, Any]:
        """库概览：分类计数 + 总时长 + 解析器来源。"""
        try:
            return self.store.media_stats()
        except Exception as ex:
            return {"available": False,
                    "reason": f"{type(ex).__name__}: {ex}"}

    def list(self, category: str = "", query: str = "",
             limit: int = 50) -> List[Dict[str, Any]]:
        """按分类/关键词列出条目。"""
        try:
            return self.store.list_media(category or None, query or None,
                                         int(limit))
        except Exception as ex:
            return [{"error": f"{type(ex).__name__}: {ex}"}]

    def top(self, category: str = "", limit: int = 10) -> List[Dict[str, Any]]:
        """按时长排（音乐库最常用的"最长的几首"）。"""
        try:
            return self.store.list_media(category or None, None,
                                         int(limit), by_duration=True)
        except Exception:
            return []

    def to_prompt(self, limit: int = 8) -> str:
        """把库概览注入上下文（让模型知道用户有什么媒体资产）。"""
        try:
            st = self.stats()
        except Exception:
            return ""
        if not st.get("available") or not st.get("total"):
            return ""
        parts = [f"[媒体库] 共 {st['total']} 项"]
        by_cat = st.get("by_category") or {}
        for cat, n in by_cat.items():
            parts.append(f"{cat} {n}")
        hours = st.get("total_hours")
        if hours:
            parts.append(f"总时长约 {hours} 小时")
        recent = self.list(limit=limit)
        if recent:
            names = "、".join(
                f"{r.get('title') or r.get('name')}"
                for r in recent[:limit] if r.get("title") or r.get("name"))
            if names:
                parts.append(f"最近：{names}")
        return "｜".join(parts)


def selftest() -> bool:
    """纯本地自检：构造临时文件验证各格式解析，不依赖任何外部文件。"""
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(f"  {'v' if cond else 'x'} {msg}")
        ok = ok and bool(cond)

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="bw-media-"))

    # 1) MP3：造一个带 ID3v2.3 标题帧的文件
    frames = b""
    title = "测试歌曲".encode("utf-8")
    payload = b"\x00" + title
    frames += b"TIT2" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload
    artist = "测试歌手".encode("utf-8")
    ap = b"\x00" + artist
    frames += b"TPE1" + struct.pack(">I", len(ap)) + b"\x00\x00" + ap
    size = len(frames)
    sync = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                  (size >> 7) & 0x7F, size & 0x7F])
    mp3 = tmp / "t.mp3"
    mp3.write_bytes(b"ID3\x03\x00\x00" + sync + frames + b"\xff\xfb\x90\x00" + b"\x00" * 512)
    info = probe(str(mp3))
    check(info["category"] == "audio", "MP3 归类为 audio")
    check(info["title"] == "测试歌曲", f"MP3 标题解析（{info['title']}）")
    check(info["artist"] == "测试歌手", f"MP3 艺术家解析（{info['artist']}）")
    check(isinstance(info.get("duration"), (int, float)),
          f"MP3 时长可得（{info.get('duration')}）")

    # 1b) 回归：标签超过 128 字节时 ID3v2.3 的 size 字节最高位为 1。
    #曾统一套 0x7F 掩码导致整块标签读成 0 字节 → 标题/艺术家全丢。
    long_title = "很长的歌曲名字" * 20          # UTF-8 下远超 128 字节
    lp = b"\x00" + long_title.encode("utf-8")
    lframes = (b"TIT2" + struct.pack(">I", len(lp)) + b"\x00\x00" + lp)
    lsize = len(lframes)
    lsync = bytes([(lsize >> 21) & 0x7F, (lsize >> 14) & 0x7F,
                   (lsize >> 7) & 0x7F, lsize & 0x7F])
    big = tmp / "big.mp3"
    big.write_bytes(b"ID3\x03\x00\x00" + lsync + lframes + b"\xff\xfb\x90\x00"
                    + b"\x00" * 512)
    binfo = probe(str(big))
    # 注意 _clean 会截到 200 字符，所以只能比前缀
    check(binfo["title"] == long_title[:200],
          f"MP3 大标签（{lsize}B，size 字节最高位为 1）仍能解析"
          f"（{str(binfo['title'])[:14]}…）")

    # 2) FLAC：STREAMINFO + Vorbis Comment
    si = bytearray(34)
    si[10] = (44100 >> 12) & 0xFF
    si[11] = (44100 >> 4) & 0xFF
    si[12] = ((44100 & 0x0F) << 4) | (1 << 1) | ((((16 - 1) >> 4) & 0x01))
    si[13] = (((16 - 1) & 0x0F) << 4) | ((((44100 * 60) >> 32) & 0x0F))
    total = 44100 * 60
    si[14:18] = (total & 0xFFFFFFFF).to_bytes(4, "big")
    vc_items = ["TITLE=Flac 曲名", "ARTIST=Flac 歌手",
                "DATE=2024"]
    vc_items = [it.encode("utf-8") for it in vc_items]
    vc_body = struct.pack("<I", 6) + b"vendor" + struct.pack("<I", len(vc_items))
    for it in vc_items:
        vc_body += struct.pack("<I", len(it)) + it
    flac = tmp / "t.flac"
    flac.write_bytes(b"fLaC" + bytes([0x00]) + len(si).to_bytes(3, "big") + bytes(si)
                     + bytes([0x84]) + len(vc_body).to_bytes(3, "big") + vc_body)
    info = probe(str(flac))
    check(info["title"] == "Flac 曲名", f"FLAC 标题（{info['title']}）")
    check(info["artist"] == "Flac 歌手", f"FLAC 艺术家（{info['artist']}）")
    check(info["sample_rate"] == 44100, f"FLAC 采样率（{info['sample_rate']}）")
    check(info["duration"] == 60.0, f"FLAC 时长（{info['duration']}）")

    # 3) WAV：fmt + data
    pcm = b"\x00" * (44100 * 2 * 2// 10)      # 0.1 秒 16bit 立体声
    fmtc = struct.pack("<HHIIHH", 1, 2, 44100, 44100 * 4, 4, 16)
    wav = tmp / "t.wav"
    wav.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
                    + struct.pack("<I", len(fmtc)) + fmtc
                    + b"data" + struct.pack("<I", len(pcm)) + pcm)
    info = probe(str(wav))
    check(info["sample_rate"] == 44100, f"WAV 采样率（{info['sample_rate']}）")
    check(info["channels"] == 2, f"WAV 声道（{info['channels']}）")
    check(info["duration"] is not None, f"WAV 时长（{info['duration']}）")

    # 4) PNG：纯文件头读尺寸
    ihdr = struct.pack(">II", 320, 240)
    png = tmp / "t.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
                    + ihdr + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)
    info = probe(str(png))
    check(info["category"] == "image", "PNG 归类为 image")
    check((info["width"], info["height"]) == (320, 240),
          f"PNG 尺寸（{info['width']}x{info['height']}）")

    # 5) 非媒体文件与坏文件都不该崩
    txt = tmp / "a.txt"
    txt.write_text("不是媒体", encoding="utf-8")
    check(probe(str(txt))["category"] is None, "非媒体文件返回 None 分类")
    broken = tmp / "b.mp3"
    broken.write_bytes(b"ID3\x03\x00\x00\x7f\x7f\x7f\x7f")
    check(probe(str(broken))["category"] == "audio", "坏 MP3 不抛异常")
    check(probe(str(tmp / "nonexistent.mp3"))["available"] is False,
          "不存在的文件标available=False")

    # 6) 库：扫描 → 入库 → 查询 → 增量
    lib_root = tmp / "lib"
    lib_root.mkdir()
    blobs = {"a.mp3": mp3.read_bytes(), "b.flac": flac.read_bytes(),
             "c.png": png.read_bytes(), "skip.txt": b"x"}
    for name, blob in blobs.items():
        (lib_root / name).write_bytes(blob)
    lib = MediaLibrary(_MemStore(), config=None)
    lib.add_root(str(lib_root))
    r1 = lib.scan()
    check(r1["added"] == 3, f"首次扫描入库 3 个媒体（{r1['added']}）")
    r2 = lib.scan()
    check(r2["added"] == 0 and r2["updated"] == 0,
          f"二次扫描零解析（增量生效：{r2['added']}/{r2['updated']}）")
    check(lib.stats().get("total") == 3,
          f"库内共 3 项（{lib.stats().get('total')}）")
    check("媒体库" in lib.to_prompt(), "库概览可注入上下文")
    # 文件删掉后 prune 应清掉
    (lib_root / "b.flac").unlink()
    r3 = lib.scan()
    check(r3["removed"] == 1, f"删除的文件被清出库（{r3['removed']}）")

    return ok


class _MemStore:
    """selftest 用的最小内存实现（避免依赖真实 db）。"""

    def __init__(self) -> None:
        self._roots: Dict[str, int] = {}
        self._items: Dict[str, Dict[str, Any]] = {}

    def add_media_root(self, path: str, recursive: bool = True) -> None:
        self._roots[path] = 1 if recursive else 0

    def remove_media_root(self, path: str) -> bool:
        return self._roots.pop(path, None) is not None

    def list_media_roots(self) -> List[Dict[str, Any]]:
        return [{"path": p, "enabled": 1, "recursive": r}
                for p, r in self._roots.items()]

    def upsert_media(self, info: Dict[str, Any]) -> bool:
        self._items[info["path"]] = info
        return True

    def query_one(self, sql: str, args: Any = ()) -> Optional[Dict[str, Any]]:
        if "media_items" in sql and "WHERE path" in sql:
            return self._items.get(str(args[0]))
        return None

    def prune_media(self, roots: List[str]) -> int:
        # 真实实现删掉"文件已不存在"的条目；这里做同样的事，
        # 否则 selftest 里"删文件后应清出库"这条永远测不到真实逻辑。
        gone = [p for p in self._items
                if not os.path.exists(p)]
        for p in gone:
            del self._items[p]
        return len(gone)

    def media_stats(self) -> Dict[str, Any]:
        by: Dict[str, int] = {}
        hours = 0.0
        for it in self._items.values():
            cat = it.get("category") or "?"
            by[cat] = by.get(cat, 0) + 1
            if it.get("duration"):
                hours += float(it["duration"]) / 3600.0
        return {"available": True, "total": len(self._items),
                "by_category": by, "total_hours": round(hours, 2)}

    def list_media(self, category: Any = None, query: Any = None,
                   limit: int = 50, by_duration: bool = False
                   ) -> List[Dict[str, Any]]:
        items = list(self._items.values())
        if category:
            items = [i for i in items if i.get("category") == category]
        if query:
            q = str(query).lower()
            items = [i for i in items
                     if q in str(i.get("title", "")).lower()
                     or q in str(i.get("artist", "")).lower()
                     or q in str(i.get("name", "")).lower()]
        if by_duration:
            items.sort(key=lambda i: -(i.get("duration") or 0))
        return items[:limit]