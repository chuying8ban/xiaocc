"""点阵化缓存：把每帧都要重画的东西先画一次，之后每帧只贴。

**为什么要有这个模块**：角色图是 SVG，`NSImage.drawInRect_` 每次调用都会把里面的径向渐变
和抗锯齿路径重新光栅化一遍 —— 实测 **522µs/帧**（`scripts/draw_phases.py`），占每帧成本的
绝大部分，而窗口每秒要画 30~60 次。点阵化之后每帧只剩一次贴图。

只缓存「内容不变的图」：状态切了才重建（key 用图源路径 + 目标尺寸），动画（位移/旋转/
缩放）仍然由调用方在**画的时候**施加，所以缓存不影响动作。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from AppKit import NSCompositingOperationSourceOver, NSImage, NSMakeRect, NSZeroRect

log = logging.getLogger(__name__)

#: 条目上限：7 个状态 × 少量尺寸就够；超了说明 key 漏了尺寸维度，丢最旧的即可。
MAX_ENTRIES = 32


class BitmapCache:
    """按 key 缓存点阵化后的 ``NSImage``（值是只读的，可以安全复用）。"""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._items: dict[Any, Any] = {}
        self._max_entries = max(1, int(max_entries))

    def get(self, key: Any) -> Any | None:
        return self._items.get(key)

    def put(self, key: Any, value: Any) -> None:
        if len(self._items) >= self._max_entries and key not in self._items:
            # 最旧的先走（dict 保序）：正常情况永远命中，走不到这里。
            self._items.pop(next(iter(self._items)))
        self._items[key] = value

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


def pointize(size: tuple[float, float], draw: Callable[[float, float], None]) -> Any | None:
    """建一张 ``size``（点）的透明位图，交给 ``draw`` 画一次，返回位图。

    ``draw(width, height)`` 在该位图的上下文里画，坐标原点在左下（AppKit 惯例）、y 向上。
    ``lockFocus`` 按当前显示器的 backing scale 建 rep，所以 Retina 上不会糊，也不用自己算 2x。
    失败返回 ``None``，调用方回退到每帧现画（慢但不会崩）。
    """
    width, height = float(size[0]), float(size[1])
    if width <= 0 or height <= 0:
        return None
    target = NSImage.alloc().initWithSize_((width, height))
    try:
        target.lockFocus()
        try:
            draw(width, height)
        finally:
            target.unlockFocus()
    except Exception as exc:  # noqa: BLE001 —— 点阵化失败不该让桌宠停下，回退慢路径即可
        log.debug("点阵化失败，这一帧直接现画：%s", exc)
        return None
    return target


def pointize_image(image: Any, size: tuple[float, float]) -> Any | None:
    """``pointize`` 的常用形态：把（可能是 SVG 的）``NSImage`` 画进位图。"""
    return pointize(size, lambda w, h: image.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0.0, 0.0, w, h), NSZeroRect, NSCompositingOperationSourceOver, 1.0))
