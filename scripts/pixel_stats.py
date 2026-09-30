"""截图里到底有没有画出东西 —— 逐图数像素：不透明占比、**alpha 均值**、主色。

断言看的是 `probe()` 的账（art=done.svg…），账对 ≠ 像素画出来了；点阵化缓存改的正是绘制路径，
所以必须自己数像素。

**为什么要有 alpha 均值（2026-09-30 加）**：原先只回答"有没有像素"（不透明占比），
**回答不了"淡化到什么程度"** —— 而气泡"5 秒后开始淡出、1.2 秒淡干净"这个用户看得见的动作，
只有强度能证明。实测三帧（满 / 淡化中 / 退干净）的 alpha 均值 **84.7 / 69.4 / 50.8**，
而三帧的**角色本体（alpha>200）恒为 18.8%**、差异全落在 8–200 的半透明带 ⇒ 单调递减 才是
"角色不动、气泡在褪"，不是同一张复制三遍。所以：

    .venv/bin/python scripts/pixel_stats.py --fade 气泡-满.png 气泡-淡化中.png 气泡-退干净.png

按 `满 − 中 ≥ 10` 且 `中 − 退 ≥ 10` 判定（阈值取自上面那组实测的 1/2 余量），rc=1 表示这三帧
不构成"真的在淡"。单张模式仍是纯测量、不做判断。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from AppKit import NSBitmapImageRep, NSData

#: 角色本体 / 微弱可见 的 alpha 分界（0–255 空间，与那份实测口径一致）
ALPHA_ROLE = 200 / 255
ALPHA_FAINT = 8 / 255
#: `--fade` 的判据：相邻两帧的 alpha 均值至少差这么多（实测 84.7→69.4→50.8，1/2 余量）
FADE_MIN_DROP = 10.0


def _measure(path: Path) -> dict | None:
    data = NSData.dataWithContentsOfFile_(str(path))
    if data is None:
        return None
    rep = NSBitmapImageRep.imageRepWithData_(data)
    if rep is None:
        return None
    w, h = int(rep.pixelsWide()), int(rep.pixelsHigh())
    opaque = role = above8 = 0
    alpha_sum = 0.0
    colors: Counter[tuple[int, int, int]] = Counter()
    for y in range(h):
        for x in range(w):
            px = rep.colorAtX_y_(x, y)
            if px is None:
                continue
            r, g, b, a = px.redComponent(), px.greenComponent(), px.blueComponent(), px.alphaComponent()
            alpha_sum += a
            if a > ALPHA_ROLE:
                role += 1
            if a > ALPHA_FAINT:
                above8 += 1
            if a > 0.35:
                opaque += 1
                colors[(int(r * 15), int(g * 15), int(b * 15))] += 1
    total = w * h or 1
    return {
        "name": path.name,
        "w": w,
        "h": h,
        "total": total,
        "opaque": opaque,
        "alpha_mean": alpha_sum / total * 255.0,
        "role_pct": role / total * 100.0,
        "above8_pct": above8 / total * 100.0,
        "colors": colors,
    }


def stats(path: Path) -> str:
    m = _measure(path)
    if m is None:
        return f"{path.name}: 读不到或不是位图"
    top = "、".join(
        f"#{r * 17:02x}{g * 17:02x}{b * 17:02x}×{n}" for (r, g, b), n in m["colors"].most_common(4)
    )
    return (
        f"{m['name']}: {m['w']}×{m['h']}  不透明 {m['opaque']}/{m['total']}"
        f"（{m['opaque'] / m['total'] * 100:.1f}%）  α均值 {m['alpha_mean']:.1f}"
        f"  角色α>200 {m['role_pct']:.1f}%  α>8 {m['above8_pct']:.1f}%  主色 {top}"
    )


def check_fade(paths: list[Path]) -> int:
    """三帧必须真的在淡：相邻 alpha 均值至少降 `FADE_MIN_DROP`。"""
    if len(paths) != 3:
        print("--fade 要三帧，按「满 / 淡化中 / 退干净」的顺序给")
        return 2
    means: list[float] = []
    for path in paths:
        m = _measure(path)
        if m is None:
            print(f"{path.name}: 读不到或不是位图")
            return 1
        means.append(m["alpha_mean"])
        print(
            f"{path.name}: α均值 {m['alpha_mean']:.1f}  角色α>200 {m['role_pct']:.1f}%"
            f"  α>8 {m['above8_pct']:.1f}%"
        )
    drops = [means[i] - means[i + 1] for i in range(len(means) - 1)]
    ok = all(drop >= FADE_MIN_DROP for drop in drops)
    print(
        f"相邻跌幅 {drops[0]:.1f} / {drops[1]:.1f}（判据 ≥ {FADE_MIN_DROP:g}）"
        f" ⇒ {'PASS：三帧真的是三个状态' if ok else 'FAIL：这三帧不构成「真的在淡」'}"
    )
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--fade":
        return check_fade([Path(a) for a in args[1:]])
    paths = [Path(a) for a in args]
    if not paths:
        print(__doc__)
        return 2
    for path in paths:
        print(stats(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
