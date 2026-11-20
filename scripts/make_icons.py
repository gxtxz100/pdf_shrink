#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Windows (.ico) 与 macOS (.icns) 应用图标。"""

from pathlib import Path
from PIL import Image, ImageDraw


def draw_icon(size: int = 512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 深蓝色圆角底板
    margin = int(size * 0.06)
    radius = int(size * 0.22)
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius,
        fill=(28, 78, 168, 255),
    )

    # 白色文档页
    doc_l = int(size * 0.24)
    doc_t = int(size * 0.16)
    doc_r = int(size * 0.76)
    doc_b = int(size * 0.84)
    fold = int(size * 0.13)
    draw.polygon(
        [
            (doc_l, doc_t),
            (doc_r - fold, doc_t),
            (doc_r, doc_t + fold),
            (doc_r, doc_b),
            (doc_l, doc_b),
        ],
        fill=(255, 255, 255, 255),
    )
    # 折角阴影
    draw.polygon(
        [
            (doc_r - fold, doc_t),
            (doc_r - fold, doc_t + fold),
            (doc_r, doc_t + fold),
        ],
        fill=(203, 213, 225, 255),
    )

    # 红色 PDF 标签条
    badge_l = int(size * 0.17)
    badge_r = int(size * 0.62)
    badge_t = int(size * 0.28)
    badge_b = int(size * 0.44)
    draw.rounded_rectangle(
        [badge_l, badge_t, badge_r, badge_b],
        radius=int(size * 0.04),
        fill=(220, 38, 38, 255),
    )

    # 向内压缩双箭头（象征文档体积缩小）
    cx = int(size * 0.50)
    cy = int(size * 0.65)
    stroke = max(4, int(size * 0.036))
    arrow_color = (28, 78, 168, 255)

    # 左侧向右箭头
    draw.line([(int(size * 0.29), cy), (cx - int(size * 0.06), cy)], fill=arrow_color, width=stroke)
    draw.polygon(
        [
            (cx - int(size * 0.03), cy),
            (cx - int(size * 0.12), cy - int(size * 0.075)),
            (cx - int(size * 0.12), cy + int(size * 0.075)),
        ],
        fill=arrow_color,
    )

    # 右侧向左箭头
    draw.line([(int(size * 0.71), cy), (cx + int(size * 0.06), cy)], fill=arrow_color, width=stroke)
    draw.polygon(
        [
            (cx + int(size * 0.03), cy),
            (cx + int(size * 0.12), cy - int(size * 0.075)),
            (cx + int(size * 0.12), cy + int(size * 0.075)),
        ],
        fill=arrow_color,
    )

    # 中间分隔竖线
    draw.line(
        [(cx, cy - int(size * 0.10)), (cx, cy + int(size * 0.10))],
        fill=(148, 163, 184, 255),
        width=max(2, int(size * 0.018)),
    )

    return img


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = draw_icon(512)
    base.save(
        out_dir / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    base.save(out_dir / "icon.icns", format="ICNS")
    print(f"Icons generated in {out_dir}")


if __name__ == "__main__":
    main()
