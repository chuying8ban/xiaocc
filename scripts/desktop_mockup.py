"""把「只含窗口本体」的取证图合成到一块合成桌面上，给 README 首屏用。

为什么要有这个脚本：

  * `*.desktop.png` 是连桌面一起截的，会带上作者桌面的壁纸和其它窗口的内容，
    不能进公开仓库（`.gitignore` 里也排除着）；
  * 「窗口本体图」是带透明通道的，直接贴进 README 看不出它是浮在桌面上的；
  * 所以背景由这个脚本画，窗口从窗口本体图里取，位置用真机上的锚点坐标。

    .venv/bin/python scripts/desktop_mockup.py          # → docs/images/desktop-mockup.png

坐标用**屏幕左上角为原点**（跟 window_layout / CGWindow 一致），画的时候翻成 CoreGraphics
的左下原点。画完自带一次自检：**再渲染一遍「不带窗口」的版本，两版必须字节不同**，否则就
说明窗口压根没贴上去 —— 这个坑真踩过：`NSImage.drawInRect_` 在这条无 GUI 进程的路径上
静默不画（背景画得出来、图贴不上），所以这里改成直接用 CoreGraphics 贴 CGImage。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import Quartz
from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep

REPO = Path(__file__).resolve().parent.parent
WINDOW = REPO / "docs" / "evidence" / "01-idle-floating.png"
OUT = REPO / "docs" / "images" / "desktop-mockup.png"

#: 真机屏幕与窗口位置（1512×982 上的默认右上角锚点，见 docs/evidence/11-default-top-right.png）
ANCHOR = (1324, 96)
#: 只取右上角这一块：看得见「贴着屏幕边缘」，又不用把整屏塞进 README
CROP_LEFT, CROP_WIDTH, CROP_HEIGHT = 1100, 412, 400
#: 取证图是 2x（316×380 像素 = 158×190 点），按点画、按像素落盘，正好 1:1 不糊
WINDOW_SCALE = 2

#: 合成背景：深灰蓝渐变，白身子的角色在上面看得清；刻意不模仿任何真实壁纸
TOP = (0.106, 0.125, 0.176, 1.0)
BOTTOM = (0.157, 0.184, 0.243, 1.0)


def _load(path: Path):
    """读窗口本体图，返回 (CGImage, 像素宽, 像素高, 非透明像素占比)。

    顺带挡住一类很坑的输入：**全透明的源图**。显示器睡着时 `CGWindowListCreateImage` 会
    返回一张全空白的图（背景透明、什么都不画），拿它出首屏图会得到「一张干净的渐变」而
    脚本自己毫无察觉 —— 所以这里先数一遍 alpha，太空就直接拒绝。
    """
    rep = NSBitmapImageRep.imageRepWithContentsOfFile_(str(path))
    if rep is None:
        raise SystemExit(f"读不出这张图：{path}")
    image = rep.CGImage()
    if image is None:
        raise SystemExit(f"这张图解不出 CGImage：{path}")

    buf = bytes(rep.bitmapData())  # 强制解码，顺手拿到像素
    alpha = buf[3::4]
    opaque = sum(1 for a in alpha if a > 8)
    ratio = opaque / len(alpha) if alpha else 0.0
    if ratio < 0.02:
        raise SystemExit(
            f"源图几乎全透明（不透明像素 {ratio * 100:.2f}%）：{path}\n"
            "  多半是显示器睡着/锁屏时抓的空图，别拿它出首屏图 —— 屏亮时重抓一次。"
        )
    return image, rep.pixelsWide(), rep.pixelsHigh(), ratio


def _render(rect, image, size):
    """画一版：背景 +（给了 rect 才有的）窗口。返回 (PNG 字节, rep)。"""
    wide, high = size
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, wide, high, 8, 0, space, Quartz.kCGImageAlphaPremultipliedLast
    )
    gradient = Quartz.CGGradientCreateWithColorComponents(
        space, [*BOTTOM, *TOP], [0.0, 1.0], 2
    )
    Quartz.CGContextDrawLinearGradient(
        ctx, gradient, Quartz.CGPointMake(0, 0), Quartz.CGPointMake(0, high), 0
    )
    if rect is not None:
        x, y, w, h = rect
        Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(x, y, w, h), image)
    cg = Quartz.CGBitmapContextCreateImage(ctx)
    rep = NSBitmapImageRep.alloc().initWithCGImage_(cg)
    return bytes(rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})), rep


def main() -> int:
    parser = argparse.ArgumentParser(description="合成 README 首屏图（窗口本体 + 合成背景）")
    parser.add_argument("--window", default=str(WINDOW), help="窗口本体图（必须带透明通道）")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    image, art_w, art_h, opaque_ratio = _load(Path(args.window))
    win_w = art_w / WINDOW_SCALE
    win_h = art_h / WINDOW_SCALE
    if win_w > CROP_WIDTH or win_h > CROP_HEIGHT:
        raise SystemExit("截图比画布还大，先把 CROP_* 调大")

    # 屏幕坐标（左上原点）→ 画布坐标（左下原点）：画布顶边对齐屏幕 y=0
    rect = (ANCHOR[0] - CROP_LEFT, CROP_HEIGHT - ANCHOR[1] - win_h, win_w, win_h)
    size = (CROP_WIDTH * WINDOW_SCALE, CROP_HEIGHT * WINDOW_SCALE)
    scaled = tuple(v * WINDOW_SCALE for v in rect)

    data, rep = _render(scaled, image, size)
    blank, _ = _render(None, image, size)
    if data == blank:
        raise SystemExit("自检失败：窗口没被画上去（两版字节完全相同）")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    print(
        f"{out_path}  {rep.pixelsWide()}×{rep.pixelsHigh()} 像素（{CROP_WIDTH}×{CROP_HEIGHT} 点）\n"
        f"窗口 {win_w:.0f}×{win_h:.0f} 点，画在画布 ({rect[0]:.0f}, {rect[1]:.0f})"
        f"（= 屏幕坐标 {ANCHOR[0]}, {ANCHOR[1]}）\n"
        f"源图不透明像素 {opaque_ratio * 100:.1f}% ✓\n"
        f"自检：与「不带窗口」那版字节不同 ✓"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
