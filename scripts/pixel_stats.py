"""截图里到底有没有画出东西 —— 逐图数非透明像素与主色。

断言看的是 `probe()` 的账（art=done.svg…），账对 ≠ 像素画出来了；点阵化缓存改的正是绘制路径，
所以必须自己数像素。用法：.venv/bin/python scripts/pixel_stats.py docs/evidence/0*.png ...
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from AppKit import NSBitmapImageRep, NSData, NSImage


def stats(path: Path) -> str:
    data = NSData.dataWithContentsOfFile_(str(path))
    if data is None:
        return f"{path.name}: 读不到"
    rep = NSBitmapImageRep.imageRepWithData_(data)
    if rep is None:
        return f"{path.name}: 不是位图"
    w, h = int(rep.pixelsWide()), int(rep.pixelsHigh())
    opaque = 0
    colors: Counter[tuple[int, int, int]] = Counter()
    for y in range(h):
        for x in range(w):
            px = rep.colorAtX_y_(x, y)
            if px is None:
                continue
            r, g, b, a = px.redComponent(), px.greenComponent(), px.blueComponent(), px.alphaComponent()
            if a > 0.35:
                opaque += 1
                colors[(int(r * 15), int(g * 15), int(b * 15))] += 1
    total = w * h
    top = "、".join(f"#{r * 17:02x}{g * 17:02x}{b * 17:02x}×{n}" for (r, g, b), n in colors.most_common(4))
    return f"{path.name}: {w}×{h}  不透明 {opaque}/{total}（{opaque / total * 100:.1f}%）  主色 {top}"


def main() -> int:
    paths = [Path(a) for a in sys.argv[1:]]
    if not paths:
        print(__doc__)
        return 2
    for path in paths:
        print(stats(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
