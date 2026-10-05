"""生成黑武士应用图标（electron-builder 用）。

只用标准库（zlib + struct）手写 PNG，避免为了一张图装 Pillow。
绘制内容：深空底 + 青色六边形环 + 紫色剑刃 + 红色横挡。
"""

from __future__ import annotations

import os
import struct
import zlib

CYAN = (55, 230, 255)
VIOLET = (139, 92, 255)
RED = (255, 59, 92)
DARK = (9, 12, 20)


def _hex_dist(x: float, y: float, r: float) -> float:
    """点到正六边形（平顶）边界的近似距离。"""
    ax, ay = abs(x), abs(y)
    return max(ax * 0.866025 + ay * 0.5, ay)


def pixel(x: int, y: int, size: int) -> "tuple[int, int, int, int]":
    """返回 (r,g,b,a)。坐标已归一化到 [-1, 1]。"""
    nx = (x + 0.5) / size * 2.0 - 1.0
    ny = (y + 0.5) / size * 2.0 - 1.0

    # 背景圆盘
    rr = (nx * nx + ny * ny) ** 0.5
    if rr > 0.985:
        return (0, 0, 0, 0)

    # 底色
    r, g, b, a = DARK[0], DARK[1], DARK[2], 255

    # 青色六边形环
    d = _hex_dist(nx, ny, 1.0)
    if 0.70 <= d <= 0.80:
        r, g, b = CYAN
    elif d < 0.70:
        r, g, b = (16, 22, 38)

    # 紫色剑刃（菱形）
    if abs(nx) * 2.6 + abs(ny) * 0.78 <= 0.62:
        r, g, b = VIOLET

    # 红色横挡
    if abs(ny) <= 0.075 and abs(nx) <= 0.52:
        r, g, b = RED

    # 中心亮点
    if (nx * nx + ny * ny) <= 0.022:
        r, g, b = (255, 255, 255)

    # 边缘柔化
    if rr > 0.93:
        a = int(255 * (0.985 - rr) / 0.055)
        a = max(0, min(255, a))

    return (r, g, b, a)


def write_png(path: str, size: int) -> None:
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0
        for x in range(size):
            raw.extend(pixel(x, y, size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    print(f"写入 {path}（{size}x{size}，{len(png)} 字节）")


def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(here, "electron", "assets", "icon.png")
    write_png(out, 512)
    small = os.path.join(here, "electron", "assets", "tray.png")
    write_png(small, 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
