"""把角色包 7 个状态的素材拼成一张对照图：原尺寸 + 60px + 32px 三档。

    .venv/bin/python scripts/art_montage.py

为什么要拼图：单看一张图只能判断「好不好看」，拼起来才能同时判断两件事 ——
「7 档是不是一眼能区分」和「缩到 60px 是不是还认得出来」，而这两条正是
「胶囊机甲 · 面罩表情 + 底部灯环」这个方向当初被选中的理由。
顺带每张都会过一遍 NSImage 加载，加载不了说明那个 SVG 写坏了。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSMakeRect,
    NSPNGFileType,
)
from Foundation import NSString

ROOT = Path(__file__).resolve().parents[1]
STATES = ("idle", "thinking", "working", "waiting", "done", "error", "offline")

CELL_W = 188.0
BIG_SCALE = 1.25
THUMBS = (60.0, 32.0)
ART_W, ART_H = 132.0, 140.0


def _draw_label(text: str, x: float, y: float, size: float = 11.0, gray: float = 0.25) -> None:
    NSString.stringWithString_(text).drawAtPoint_withAttributes_(
        (x, y),
        {NSFontAttributeName: NSFont.systemFontOfSize_(size),
         NSForegroundColorAttributeName: NSColor.colorWithCalibratedRed_green_blue_alpha_(
             gray, gray, gray, 1.0)},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="角色素材对照图")
    parser.add_argument("--characters", default=str(ROOT / "src/xiaocc/characters/xiaocc"),
                        help="角色包目录（含 assets/<state>.svg）")
    parser.add_argument("--out", default=str(ROOT / "docs/evidence/13-art-states-montage.png"))
    args = parser.parse_args()

    char_dir = Path(args.characters)
    images = {}
    for state in STATES:
        path = char_dir / "assets" / f"{state}.svg"
        image = NSImage.alloc().initWithContentsOfFile_(str(path)) if path.exists() else None
        if image is None:
            print(f"{state}: 加载失败 → {path}")
            return 1
        size = image.size()
        print(f"{state}: {path.name} → NSImage {size.width:.0f}x{size.height:.0f}")
        images[state] = image

    big_w, big_h = ART_W * BIG_SCALE, ART_H * BIG_SCALE
    header = 22.0
    total_w = CELL_W * len(STATES)
    total_h = header + big_h + 20 + max(THUMBS) + 18

    # PyObjC 的选择器名太长，用 getattr 拼，免得折行折出语法错
    init_selector = (
        "initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample"
        "_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_"
    )
    rep = getattr(NSBitmapImageRep.alloc(), init_selector)(
        None, int(total_w), int(total_h), 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    # 浅灰打底：透明像素和「被画布裁掉的部分」才分得出来
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.94, 0.94, 0.95, 1.0).set()
    NSBezierPath.bezierPathWithRect_(NSMakeRect(0, 0, total_w, total_h)).fill()

    _draw_label("原尺寸 132×140", 6, total_h - 16)
    _draw_label("60px / 32px 缩略：缩到 60px 还认得出来，是这个方向被选中的硬指标", 130,
                total_h - 16)

    thumb_row_y = 20.0
    for index, state in enumerate(STATES):
        image = images[state]
        x = index * CELL_W + (CELL_W - big_w) / 2
        y = total_h - header - big_h
        image.drawInRect_(NSMakeRect(x, y, big_w, big_h))
        _draw_label(state, x + 4, y - 14, 12.0, 0.1)

        for offset, thumb in enumerate(THUMBS):
            tx = index * CELL_W + 14 + offset * 84
            image.drawInRect_(NSMakeRect(tx, thumb_row_y, thumb, thumb * ART_H / ART_W))
            _draw_label(f"{thumb:.0f}px", tx, 4, 10.0, 0.4)

    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSPNGFileType, {})
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data.writeToFile_atomically_(str(out), True)
    print(f"拼图 {total_w:.0f}x{total_h:.0f} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
