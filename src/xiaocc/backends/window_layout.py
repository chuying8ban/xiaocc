"""贴边吸附、命中判定、动作曲线的纯几何层 —— 不 import 任何图形库。

为什么要单独一层：macOS / Windows / Web 三种显示层各写一遍窗口代码，但
「离边缘多近才收进去、鼠标算不算落在角色身上、某个动作在第 t 秒该是什么姿态」
这套规则只该有一份实现。这里零图形依赖 —— 任何 CI 上都能跑，不用开真窗口。

坐标约定：**原点在屏幕左上角、y 轴向下**（跟 CGWindow / Windows / Web 一致）。
AppKit 显示层在画之前翻一次 y，除此之外不关心平台差异。长度单位都是逻辑像素。

给第三方显示层作者的提醒：贴边收起／展开最容易写错的不是几何，而是**时序** ——
拖到边缘松手时鼠标还停在把手条上，如果立刻判定「鼠标在条上 → 展开」，
就会一收一展疯狂抖动。所以悬停展开必须**先把手肘挪开一次**（见 :class:`Dock` 的
``armed``）。这个坑有单元测试守着。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CAPTION_BAND",
    "EDGE_SNAP_DISTANCE",
    "HANDLE_LENGTH",
    "HANDLE_THICKNESS",
    "HOVER_GRACE",
    "MOTION_POSES",
    "PAD",
    "Dock",
    "DockAction",
    "Edge",
    "Point",
    "Pose",
    "Rect",
    "body_rect_of_window",
    "choose_edge",
    "clamp",
    "collapsed_rect",
    "docked_rect",
    "inside_ellipse",
    "parse_hex",
    "pose_for",
    "window_size_for",
]

#: 角色本体的外沿离屏幕边缘多近，才认为「用户想把它收进去」。
EDGE_SNAP_DISTANCE = 24.0
#: 收起后露在屏幕上的把手条厚度（逻辑像素）。太薄了鼠标不好戳，太厚了碍事。
HANDLE_THICKNESS = 12.0
#: 把手条长度（会被屏幕尺寸夹住）。
HANDLE_LENGTH = 116.0
#: 判定「鼠标还在窗口里」时的宽容边距，避免边界上抖动。
HOVER_GRACE = 6.0
#: 窗口内角色本体四周的透明留白（放光晕用）。
PAD = 14.0
#: 窗口底部留给状态文案的高度。
CAPTION_BAND = 26.0


class Edge(str, Enum):
    NONE = "none"
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"

    def __str__(self) -> str:  # pragma: no cover - 调试友好
        return self.value


def clamp(value: float, low: float, high: float) -> float:
    """把 value 夹进 [low, high]（high 比 low 小时返回 low，避免负宽度）。"""
    if high < low:
        return low
    return max(low, min(high, value))


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def size(self) -> tuple[float, float]:
        return (self.width, self.height)

    def contains(self, point: Point, slack: float = 0.0) -> bool:
        return (
            self.x - slack <= point.x <= self.right + slack
            and self.y - slack <= point.y <= self.bottom + slack
        )

    def moved(self, dx: float, dy: float) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.width, self.height)

    def clamped_into(self, bounds: Rect) -> Rect:
        """整体挪进 bounds（尺寸不变；比 bounds 还大就贴左上）。"""
        return Rect(
            clamp(self.x, bounds.x, bounds.right - self.width),
            clamp(self.y, bounds.y, bounds.bottom - self.height),
            self.width,
            self.height,
        )


# —— 贴边判定 ————————————————————————————————————————————————————————————————

#: 平手（比如正好在角落）时的优先级：左右优先于上下，右手边最优先。
_EDGE_ORDER = (Edge.RIGHT, Edge.LEFT, Edge.BOTTOM, Edge.TOP)


def choose_edge(
    body: Rect,
    screen: Rect,
    *,
    distance: float = EDGE_SNAP_DISTANCE,
) -> Edge:
    """离哪条边最近且足够近 → 返回那条边；都太远 → :attr:`Edge.NONE`。

    传进来的是**角色本体**的矩形，不是窗口矩形 —— 窗口带透明留白和文案带，
    用窗口去判距离会让用户在离边缘还有几十像素时就触发收起。
    """
    gaps: Mapping[Edge, float] = {
        Edge.LEFT: body.x - screen.x,
        Edge.RIGHT: screen.right - body.right,
        Edge.TOP: body.y - screen.y,
        Edge.BOTTOM: screen.bottom - body.bottom,
    }
    best = Edge.NONE
    best_gap = float("inf")
    for edge in _EDGE_ORDER:
        gap = gaps[edge]
        if gap <= distance and gap < best_gap:
            best, best_gap = edge, gap
    return best


def collapsed_rect(
    edge: Edge,
    screen: Rect,
    body: Rect,
    *,
    thickness: float = HANDLE_THICKNESS,
    length: float = HANDLE_LENGTH,
) -> Rect:
    """收起后那条把手条的矩形：贴着边、跨轴方向对齐角色原来的位置。"""
    if edge is Edge.NONE:
        return body
    if edge in (Edge.LEFT, Edge.RIGHT):
        height = min(length, screen.height)
        y = clamp(body.center.y - height / 2.0, screen.y, screen.bottom - height)
        x = screen.x if edge is Edge.LEFT else screen.right - thickness
        return Rect(x, y, thickness, height)
    width = min(length, screen.width)
    x = clamp(body.center.x - width / 2.0, screen.x, screen.right - width)
    y = screen.y if edge is Edge.TOP else screen.bottom - thickness
    return Rect(x, y, width, thickness)


def docked_rect(
    edge: Edge,
    screen: Rect,
    size: tuple[float, float],
    *,
    center: float | None = None,
) -> Rect:
    """展开状态贴在同一条边上时的窗口矩形。

    ``center`` 是跨轴方向的对齐基准，展开时应当传**把手条的中心**：
    展开后的窗口必须把把手条整个盖住（含 :data:`HOVER_GRACE`），
    否则鼠标还停在条上就落在窗口外了 —— 会立刻又收起。这条不变量有测试守着。
    """
    width, height = size
    if edge is Edge.NONE:
        return Rect(0.0, 0.0, width, height)
    if edge in (Edge.LEFT, Edge.RIGHT):
        x = screen.x if edge is Edge.LEFT else screen.right - width
        anchor = body_anchor(center, screen.y, screen.height, height)
        return Rect(x, anchor, width, height)
    y = screen.y if edge is Edge.TOP else screen.bottom - height
    anchor = body_anchor(center, screen.x, screen.width, width)
    return Rect(anchor, y, width, height)


def body_anchor(center: float | None, low: float, span: float, size: float) -> float:
    """把跨轴中心换算成「不越界」的起点。"""
    if center is None:
        return low + (span - size) / 2.0
    return clamp(center - size / 2.0, low, low + span - size)


def window_size_for(
    canvas: tuple[int, int],
    scale: float = 1.0,
    *,
    pad: float = PAD,
    caption_band: float = CAPTION_BAND,
) -> tuple[float, float]:
    """窗口尺寸 = 角色画布 × 缩放 + 四周留白 + 底部文案带。

    文案带是**固定**的：文字永远画在窗口内部，不会溢出到屏幕外
    （角色包换画布、换缩放都不影响这条）。
    """
    width, height = canvas
    return (width * scale + pad * 2.0, height * scale + pad * 2.0 + caption_band)


def body_rect_of_window(
    frame: Rect,
    *,
    canvas: tuple[int, int],
    scale: float = 1.0,
    pad: float = PAD,
) -> Rect:
    """窗口矩形 → 角色本体矩形（去掉留白，且不含底部文案带）。"""
    width, height = canvas
    return Rect(frame.x + pad, frame.y + pad, width * scale, height * scale)


def inside_ellipse(point: Point, rect: Rect, slack: float = 0.0) -> bool:
    """点是否落在椭圆内 —— 判定「用户点的是角色」用它。

    椭圆外的四角是透明的，点击必须穿到下面去；用矩形判会误判成「点到了角色」。
    """
    rx = rect.width / 2.0 + slack
    ry = rect.height / 2.0 + slack
    if rx <= 0 or ry <= 0:
        return False
    center = rect.center
    nx = (point.x - center.x) / rx
    ny = (point.y - center.y) / ry
    return nx * nx + ny * ny <= 1.0


# —— 收起/展开状态机 ——————————————————————————————————————————————————————————


class DockAction(str, Enum):
    NONE = "none"
    COLLAPSE = "collapse"
    EXPAND = "expand"


@dataclass
class Dock:
    """贴边隐藏的时序规则（纯逻辑，可单元测试）。

    ``collapsed`` 收起中 / ``docked`` 是否贴在某条边上 / ``armed`` 是否允许悬停展开。
    关键点：**每次收起都把 armed 清掉**，必须等鼠标离开窗口一次才重新武装。
    没有这一条，拖到边缘松手（鼠标正停在把手条上）会立刻展开又收起、反复抖动。
    """

    collapsed: bool = False
    docked: bool = False
    armed: bool = True
    edge: Edge = Edge.NONE

    @property
    def state(self) -> str:
        if not self.docked:
            return "floating"
        return "collapsed" if self.collapsed else "expanded"

    def drag_started(self) -> None:
        """开始拖拽：立刻展开（收起状态下一拖就应该是完整角色），并暂停判定。"""
        self.collapsed = False
        self.armed = True

    def drop(self, body: Rect, screen: Rect, *, distance: float = EDGE_SNAP_DISTANCE) -> Edge:
        """松手：够近就贴边收起，否则自由漂浮。返回这次落在哪条边上。"""
        edge = choose_edge(body, screen, distance=distance)
        self.edge = edge
        self.docked = edge is not Edge.NONE
        self.collapsed = self.docked
        self.armed = not self.collapsed  # 收起时先缴械：鼠标还压在条上呢
        return edge

    def update(self, cursor: Point, window: Rect, *, dragging: bool = False, grace: float = HOVER_GRACE) -> DockAction:
        """每个动画节拍调一次。``window`` 是**当前**窗口矩形（收起时就是把手条）。

        返回需要执行的动作用 :class:`DockAction`，由显示层去改窗口。
        """
        if dragging:
            return DockAction.NONE
        if self.collapsed:
            if not window.contains(cursor, grace):
                self.armed = True  # 鼠标让开了，下次悬停才允许展开
                return DockAction.NONE
            if self.armed:
                self.armed = False
                self.collapsed = False
                return DockAction.EXPAND
            return DockAction.NONE
        if self.docked and not window.contains(cursor, grace):
            self.collapsed = True
            self.armed = False
            return DockAction.COLLAPSE
        return DockAction.NONE

    def reset(self) -> None:
        """回到自由漂浮（角色被拖回屏幕中间）。"""
        self.collapsed = False
        self.docked = False
        self.armed = True
        self.edge = Edge.NONE


# —— 动作曲线 ————————————————————————————————————————————————————————————————

#: 眼睛表情，只给「程序化骨架」用；角色包自带美术（assets）时用不到。
Eye = str


@dataclass(frozen=True)
class Pose:
    """某一瞬间的姿态增量。显示层把它应用到角色图或程序化骨架上。"""

    dx: float = 0.0
    dy: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0  # 度，正数顺时针（左上原点坐标系）
    glow: float = 0.0  # 0..1，状态色光晕强度
    eye: Eye = "open"

    def blend_to(self, other: Pose, t: float) -> Pose:
        """线性插值 —— 状态切换时用，避免动作「跳」一下。"""
        t = clamp(t, 0.0, 1.0)
        mix = lambda a, b: a + (b - a) * t
        return Pose(
            dx=mix(self.dx, other.dx),
            dy=mix(self.dy, other.dy),
            scale=mix(self.scale, other.scale),
            rotation=mix(self.rotation, other.rotation),
            glow=mix(self.glow, other.glow),
            eye=other.eye if t >= 0.5 else self.eye,
        )


def _pose_float(t: float) -> Pose:
    s = math.sin(2 * math.pi * t / 2.6)
    return Pose(dy=3.0 * s, scale=1.0 + 0.012 * s, glow=0.10)


def _pose_ponder(t: float) -> Pose:
    s = math.sin(2 * math.pi * t / 3.4)
    return Pose(dy=2.4 * s, rotation=3.5 * s, scale=1.02, glow=0.20, eye="squint")


def _pose_busy(t: float) -> Pose:
    return Pose(
        dx=1.6 * math.sin(2 * math.pi * t / 0.28),
        dy=1.2 * math.sin(2 * math.pi * t / 0.5),
        glow=0.34,
        eye="focus",
    )


def _pose_beckon(t: float) -> Pose:
    s = abs(math.sin(2 * math.pi * t / 0.9))
    return Pose(dy=-2.0 * s, scale=1.0 + 0.05 * s, glow=0.46, eye="wide")


def _pose_cheer(t: float) -> Pose:
    hop = max(0.0, math.sin(2 * math.pi * t / 0.8))
    return Pose(dy=-9.0 * (hop**0.7), scale=1.0 + 0.04 * hop, glow=0.50, eye="happy")


def _pose_wobble(t: float) -> Pose:
    # 0.25s 一个来回：再快就会和 30fps 的绘制节拍打架（看起来像丢帧的抖动）
    s = math.sin(2 * math.pi * t / 0.25)
    return Pose(dx=4.0 * s, rotation=6.0 * s, glow=0.42, eye="cross")


def _pose_sleep(t: float) -> Pose:
    s = math.sin(2 * math.pi * t / 4.0)
    return Pose(dy=2.2 * s, scale=1.0 + 0.010 * s, glow=0.04, eye="closed")


#: 动作名 → 姿态函数。名字必须和 ``characters.KNOWN_MOTIONS`` 一致（有测试守着），
#: 这样第三方角色写错动作名时，加载阶段就会告警，而不是静默不动。
MOTION_POSES: Mapping[str, Callable[[float], Pose]] = {
    "float": _pose_float,
    "ponder": _pose_ponder,
    "busy": _pose_busy,
    "beckon": _pose_beckon,
    "cheer": _pose_cheer,
    "wobble": _pose_wobble,
    "sleep": _pose_sleep,
}

#: 角色写了不认识的动作名时，按待机演，别僵住。
FALLBACK_MOTION = "float"


def pose_for(motion: str, seconds: float) -> Pose:
    """第 ``seconds`` 秒时动作 ``motion`` 的姿态。未知动作按待机处理。"""
    return MOTION_POSES.get(motion, MOTION_POSES[FALLBACK_MOTION])(seconds)


# —— 颜色 ————————————————————————————————————————————————————————————————————

#: 和 ``characters`` 里的角色包校验用同一个形状：**必须带 #**，
#: 免得同一个颜色的两种写法在不同层里各判一套。
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_hex(color: str) -> tuple[float, float, float]:
    """``#RGB`` / ``#RRGGBB`` → 0..1 的三元组（角色包已校验过，这里再挡一道）。"""
    text = color.strip()
    if not _HEX.match(text):
        raise ValueError(f"不是合法颜色：{color!r}")
    digits = text[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return tuple(int(digits[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
