#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 Pillow 绘制程序图标，输出多尺寸 .ico。

布局（自上而下）：顶部角色字样「客户端/服务端」→ 中部缩小后的图案（云朵+同步箭头+文件方块）→
底部作者名 By CallMeACookieWYQ（放大并带投影）。三部分各占净空，互不遮挡，整体均衡。

用法：
    python tools/make_icon.py client <输出目录>
    python tools/make_icon.py server <输出目录>
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

MASTER = 1024
SIZES = [16, 24, 32, 48, 64, 128, 256]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


def _find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    raise FileNotFoundError("未找到支持中文的系统字体，请检查 C:\\Windows\\Fonts")


def _gradient(size, top, bottom):
    img = Image.new("RGB", (size, size))
    dr = ImageDraw.Draw(img)
    r1, g1, b1 = top
    r2, g2, b2 = bottom
    for y in range(size):
        t = y / max(size - 1, 1)
        dr.line([(0, y), (size, y)], fill=(
            int(r1 + (r2 - r1) * t),
            int(g1 + (g2 - g1) * t),
            int(b1 + (b2 - b1) * t),
        ))
    return img


def _rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    dr = ImageDraw.Draw(mask)
    dr.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _draw_sync_arrows(dr, cx, cy, r, width, color):
    import math

    for start, end in ((-135, 45), (45, 225)):
        dr.arc([cx - r, cy - r, cx + r, cy + r], start=start, end=end,
               fill=color, width=width)

    def _arrow(start_deg):
        a0 = math.radians(start_deg)
        tip = (cx + r * math.cos(a0), cy + r * math.sin(a0))
        a1 = a0 + math.radians(25)
        a2 = a0 - math.radians(25)
        base = (cx + (r - width * 1.6) * math.cos(a0),
                cy + (r - width * 1.6) * math.sin(a0))
        p1 = (cx + (r - width * 0.4) * math.cos(a1),
              cy + (r - width * 0.4) * math.sin(a1))
        p2 = (cx + (r - width * 0.4) * math.cos(a2),
              cy + (r - width * 0.4) * math.sin(a2))
        dr.polygon([tip, p1, base, p2], fill=color)

    _arrow(-135)
    _arrow(225)


def _draw_cloud(dr, cx, cy, r, color):
    off = int(r * 0.55)
    dr.ellipse([cx - off - r, cy, cx - off + r, cy + 2 * r], fill=color)
    dr.ellipse([cx - r, cy - int(r * 0.35), cx + r, cy + r * 1.65], fill=color)
    dr.ellipse([cx + off - r, cy + int(r * 0.15), cx + off + r, cy + 2 * r], fill=color)


def _draw_file(dr, x0, y0, x1, y1, color, fold=60):
    dr.rounded_rectangle([x0, y0, x1, y1], radius=28, fill=color)
    dr.polygon([(x1 - fold, y0), (x1, y0 + fold), (x1 - fold, y0 + fold)],
               fill=(255, 255, 255, 120))


def build_icon(role: str, out_dir: str) -> str:
    if role not in ("client", "server"):
        raise ValueError("role 必须为 client 或 server")
    title = "客户端" if role == "client" else "服务端"
    author = "By CallMeACookieWYQ"
    if role == "client":
        top, bottom = (16, 185, 129), (5, 102, 84)      # 翡翠绿
        accent = (255, 255, 255)
    else:
        top, bottom = (59, 130, 246), (30, 58, 138)     # 靛蓝
        accent = (255, 255, 255)

    font_path = _find_font()
    S = MASTER
    img = _gradient(S, top, bottom).convert("RGBA")
    mask = _rounded_mask(S, int(S * 0.18))
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.paste(img, (0, 0), mask)
    dr = ImageDraw.Draw(canvas)

    # ---- 顶部角色字样 ----
    t_font = ImageFont.truetype(font_path, 100)
    tb = dr.textbbox((0, 0), title, font=t_font)
    dr.text(((S - (tb[2] - tb[0])) / 2, 62), title, font=t_font, fill=accent)

    # ---- 中部图案（整体缩小，为顶部字样与底部作者名预留净空） ----
    cx, cy = S // 2, 430
    _draw_cloud(dr, cx, cy, 100, (255, 255, 255, 235))
    _draw_sync_arrows(dr, cx, cy, 180, 34, accent)
    _draw_file(dr, cx - 95, 532, cx + 95, 662, (255, 255, 255, 215), fold=48)

    # ---- 底部作者名（放大 + 投影，图案下方净空区，清晰可读） ----
    a_font = ImageFont.truetype(font_path, 76)
    ab = dr.textbbox((0, 0), author, font=a_font)
    ax = (S - (ab[2] - ab[0])) / 2
    ay = 826
    dr.text((ax + 4, ay + 6), author, font=a_font, fill=(0, 0, 0, 120))
    dr.text((ax, ay), author, font=a_font, fill=(255, 255, 255, 242))

    os.makedirs(out_dir, exist_ok=True)
    ico_path = os.path.join(out_dir, f"icon_{role}.ico")
    canvas.save(ico_path, format="ICO", sizes=[(s, s) for s in SIZES])
    return ico_path


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python tools/make_icon.py <client|server> <输出目录>")
        sys.exit(1)
    path = build_icon(sys.argv[1].lower(), sys.argv[2])
    print(f"已生成图标: {path}")
