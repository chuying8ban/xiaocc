"""macOS 原生显示层（AppKit）—— 透明无边框、置顶、贴边隐藏、点击穿透。

只有这一层碰 PyObjC。它做的全部事情：

1. 起一个**非激活**的透明窗口：无边框、不抢焦点、出现在所有桌面空间上
2. 把 :class:`~xiaocc.engine.Render` 画出来 —— 造型来自**角色包**，不写死在这里
3. 拖拽 → 松手贴边收成把手条 → 悬停展开 → 鼠标离开再收起
   （时序规则全在 :mod:`xiaocc.backends.window_layout`，那部分是纯逻辑、有单元测试）
4. 鼠标不在角色身上时整窗 ``ignoresMouseEvents``，点在透明四角上会**穿到底下的应用**

造型的取法（优先级从高到低，代码里不含任何角色私有形状）：

a. 角色包 ``assets.base`` / ``assets.<state>`` 指到的图片（PNG / SVG 都能被 NSImage 直接加载）
b. 角色包目录里自动发现的 ``assets/<state>.png|.svg``
c. 通用程序化骨架：只用 ``palette`` + ``canvas`` + ``motion`` 画「光环 + 球体 + 表情」，
   保证任何一个只填了配色的新角色包立刻能跑

也就是说：选定形象以后，把它做成 per-state 的图丢进角色包即可换脸，**本文件不动**。
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..characters import Character
from ..engine import Render
from ..protocol import State
from . import window_layout as wl
from .base import Backend

__all__ = ["AppKitBackend"]

log = logging.getLogger("xiaocc.backends.appkit")

try:  # PyObjC 只在真的要用这个显示层时才需要
    import objc
    from AppKit import (
        NSAffineTransform,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSColor,
        NSCompositingOperationSourceOver,
        NSDate,
        NSDefaultRunLoopMode,
        NSEvent,
        NSEventMaskAny,
        NSFloatingWindowLevel,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSGraphicsContext,
        NSImage,
        NSLineBreakByTruncatingTail,
        NSMakeRect,
        NSMutableParagraphStyle,
        NSPanel,
        NSParagraphStyleAttributeName,
        NSScreen,
        NSTextAlignmentCenter,
        NSView,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorIgnoresCycle,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
        NSZeroRect,
    )
    from Foundation import NSString
except ImportError as exc:  # pragma: no cover - 取决于环境
    raise ImportError("AppKit 显示层需要 PyObjC：pip install 'xiaocc[macos]'") from exc


#: 动画帧率上限。30fps 足够让动作连贯，又不会让笔记本风扇转起来。
DEFAULT_FPS = 30.0

_IMAGE_SUFFIXES = (".png", ".svg", ".pdf", ".tiff", ".jpg", ".jpeg")


@dataclass(frozen=True)
class _Space:
    """一块屏幕：把 AppKit 的全局左下原点坐标换算成布局层的「左上原点、y 向下」。"""

    origin_x: float
    origin_y: float
    width: float
    height: float

    @property
    def screen(self) -> wl.Rect:
        return wl.Rect(0.0, 0.0, self.width, self.height)

    def to_local_point(self, point: Any) -> wl.Point:
        return wl.Point(point.x - self.origin_x, self.origin_y + self.height - point.y)

    def to_local_rect(self, rect: Any) -> wl.Rect:
        return wl.Rect(
            rect.origin.x - self.origin_x,
            self.origin_y + self.height - (rect.origin.y + rect.size.height),
            rect.size.width,
            rect.size.height,
        )

    def to_ns_rect(self, rect: wl.Rect) -> Any:
        return NSMakeRect(
            self.origin_x + rect.x,
            self.origin_y + self.height - rect.y - rect.height,
            rect.width,
            rect.height,
        )

    def to_cg_rect(self, rect: wl.Rect, main_height: float) -> tuple[float, float, float, float]:
        """布局矩形 → CGWindowList 全局坐标（主屏左上原点、y 向下）。

        截图/取证脚本用。注意 CGWindow 的 ``CGRect`` 和 AppKit 的原点**不在同一角**，
        直接拿 ``window.frame()`` 去裁屏幕，会裁到一块完全不相干的桌面。
        """
        return (
            self.origin_x + rect.x,
            main_height - self.origin_y - self.height + rect.y,
            rect.width,
            rect.height,
        )


class _PetView(NSView):
    """内容视图。绘制全部委托给后端；鼠标事件也只做转交（粘贴板里不留逻辑）。"""

    def initWithFrame_(self, frame):
        # PyObjC 的惯例就是接收返回的 self（不是把自己改成另一个对象）
        self = objc.super(_PetView, self).initWithFrame_(frame)  # noqa: PLW0642
        if self is None:
            return None
        self._backend = None
        return self

    # 非激活面板也要能一次点到（不然第一次点击会被当成「唤醒窗口」吃掉）
    def acceptsFirstMouse_(self, _event):
        return True

    def isOpaque(self):
        return False

    def mouseDownCanMoveWindow(self):
        return False

    def drawRect_(self, rect):
        if self._backend is not None:
            self._backend._draw_view(self)

    def mouseDown_(self, event):
        if self._backend is not None:
            self._backend._mouse_down(event)

    def mouseDragged_(self, event):
        if self._backend is not None:
            self._backend._mouse_dragged(event)

    def mouseUp_(self, event):
        if self._backend is not None:
            self._backend._mouse_up(event)


class AppKitBackend(Backend):
    """macOS 原生窗口显示层。用法：``xiaocc run --backend appkit``。

    初始位置由 ``--backend-opt at=...`` 控制（默认右上角）。
    """

    name = "appkit"
    #: 窗口动画要靠自己的事件循环跑，节拍由本层消化 —— 见 Backend 文档。
    self_paced = True

    def __init__(
        self,
        *,
        fps: float = DEFAULT_FPS,
        scale: float | None = None,
        at: str | None = None,
        snap_distance: float = wl.EDGE_SNAP_DISTANCE,
        cursor: Callable[[], wl.Point] | None = None,
    ) -> None:
        self.fps = max(1.0, min(60.0, float(fps)))
        self.snap_distance = snap_distance
        self._scale_override = scale
        #: 初始停靠点（``--backend-opt at=...``）：方位名或 ``"x,y"``；``None`` = 默认右上角。
        self._at = at
        if at is not None:
            # 取值写错就在**加载阶段**报错：真等到开窗口时才炸，用户看到的只是一个空桌面。
            # 这里只为校验，真正的矩形要等屏幕尺寸和角色画布都已知（见 :meth:`_ensure_window`）。
            wl.parse_anchor(at, wl.Rect(0.0, 0.0, 0.0, 0.0), (0.0, 0.0))
        #: 注入光标来源 —— 自动化截图/回归脚本用它模拟「鼠标在哪」，正常跑用真实鼠标。
        self._cursor_override = cursor
        self._window: Any = None
        self._view: Any = None
        self._space_cache: _Space | None = None
        self._window_local: wl.Rect = wl.Rect(0.0, 0.0, 0.0, 0.0)
        self._frame: Render | None = None
        self._character: Character | None = None
        self._dock = wl.Dock()
        self._dragging = False
        self._drag_offset = (0.0, 0.0)
        self._started = time.monotonic()
        self._art_cache: dict[str, Any] = {}
        self._art_warnings: set[str] = set()
        self._last_art: str = "(程序化骨架)"
        self._last_paint_state: str | None = None
        self._last_caption_drawn: str = ""

    # —— 生命周期 ——————————————————————————————————————————————————————————

    def render(self, frame: Render) -> None:
        self._ensure_window(frame.character)
        if frame.state is not self._last_paint_state:
            self._last_paint_state = frame.state
            # 状态一变，动作从这一秒重新开始 —— 否则切到「搞定」会从半截开始跳
            self._started = time.monotonic()
        self._frame = frame
        self._paint()
        self._pump(self.interval)  # 用这段节拍跑自己的事件循环 → 动画连续

    def linger(self, seconds: float) -> None:
        """保持窗口 N 秒（截图 / 肉眼验收）。期间动画照跑。"""
        if seconds > 0 and self._window is not None:
            self._pump(seconds)

    def close(self) -> None:
        if self._window is not None:
            try:
                self._window.setIgnoresMouseEvents_(True)
                self._window.orderOut_(None)
            except Exception:  # pragma: no cover - 退出阶段不值得炸
                log.debug("关闭窗口失败", exc_info=True)
        self._window = None
        self._view = None
        self._frame = None

    # —— 供脚本/自动化调用（和鼠标走同一套代码路径）—————————————————————————

    def move_window_to(self, x: float, y: float) -> None:
        """把窗口挪到屏幕左上角坐标 (x, y) —— 等价于拖拽中的一帧。"""
        size = self._window_size()
        rect = wl.Rect(x, y, *size).clamped_into(self._space().screen)
        self._set_window_rect(rect)

    def start_drag(self) -> None:
        """程序化拖拽的开始（等价于鼠标按下）—— 自动化脚本/回归用。

        和真鼠标走同一套状态迁移：从把手条上抓起就先弹成完整角色，
        然后交给 :meth:`end_drag` 判定贴边。
        """
        if self._handle_shown:
            self._expand_from_edge()
        self._dock.drag_started()
        self._dragging = True

    def end_drag(self) -> wl.Edge:
        """松手：判定是否贴边收起。返回落在哪条边上。"""
        self._dragging = False
        body = self._body_in_screen()
        edge = self._dock.drop(body, self._space().screen, distance=self.snap_distance)
        if edge is not wl.Edge.NONE:
            self._set_window_rect(wl.collapsed_rect(edge, self._space().screen, body))
        return edge

    def probe(self) -> dict[str, Any]:
        """当前窗口的真实状态 —— 自动化证据用，别拿设计文档当结果。"""
        if self._window is None:
            return {"window": None}
        frame = self._window.frame()
        info: dict[str, Any] = {
            "window_number": int(self._window.windowNumber()),
            "visible": bool(self._window.isVisible()),
            "level": int(self._window.level()),
            "alpha": float(self._window.alphaValue()),
            "opaque": bool(self._window.isOpaque()),
            "has_shadow": bool(self._window.hasShadow()),
            "ignores_mouse_events": bool(self._window.ignoresMouseEvents()),
            "ns_frame": [
                round(frame.origin.x, 1),
                round(frame.origin.y, 1),
                round(frame.size.width, 1),
                round(frame.size.height, 1),
            ],
            "dock": self._dock.state,
            "edge": str(self._dock.edge),
            "art": self._last_art,
            #: 实际画上去的文案（不是引擎的那份原文）—— 待机时应当为空
            "caption_drawn": self._last_caption_drawn,
            #: 视图尺寸，应当与窗口尺寸一致（不一致说明绘制坐标系会错位）
            "view_size": [
                round(self._view.bounds().size.width, 1),
                round(self._view.bounds().size.height, 1),
            ]
            if self._view is not None
            else None,
        }
        if self._frame is not None:
            info["state"] = self._frame.state.value
            info["caption"] = self._frame.caption
        return info

    # —— 窗口搭建 ——————————————————————————————————————————————————————————

    def _space(self) -> _Space:
        if self._space_cache is None:
            screens = list(NSScreen.screens() or [])
            screen = NSScreen.mainScreen() or (screens[0] if screens else None)
            if screen is None:
                raise RuntimeError("没有可用的屏幕（无图形会话？）—— AppKit 显示层需要登录桌面")
            frame = screen.frame()
            self._space_cache = _Space(
                frame.origin.x, frame.origin.y, frame.size.width, frame.size.height
            )
        return self._space_cache

    def _scale(self) -> float:
        if self._scale_override is not None:
            return self._scale_override
        return self._character.default_scale if self._character else 1.0

    def _window_size(self) -> tuple[float, float]:
        assert self._character is not None
        return wl.window_size_for(self._character.canvas, self._scale())

    def _ensure_window(self, character: Character) -> None:
        if self._window is not None:
            return
        self._character = character
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # 不占 Dock、不抢焦点
        space = self._space()
        width, height = self._window_size()
        start = wl.parse_anchor(self._at, space.screen, (width, height))
        ns_rect = space.to_ns_rect(start)

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ns_rect,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setLevel_(NSFloatingWindowLevel)  # 置顶：浮在普通窗口之上
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setMovable_(False)
        panel.setIgnoresMouseEvents_(True)  # 先穿透，交给 _poll 按鼠标位置决定

        view = _PetView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        view._backend = self
        panel.setContentView_(view)

        app.finishLaunching()
        panel.orderFrontRegardless()  # 不激活 app 也能显示
        self._window, self._view = panel, view
        self._window_local = start
        self._started = time.monotonic()
        log.info("AppKit 窗口就绪：%sx%s @ %s", width, height, start)

    def _set_window_rect(self, rect: wl.Rect) -> None:
        assert self._window is not None
        self._window.setFrame_display_(self._space().to_ns_rect(rect), True)
        self._window_local = rect

    # —— 每个动画节拍：命中判定 + 收起/展开 ————————————————————————————————

    def _pump(self, seconds: float) -> None:
        """在自己的事件循环里待 ``seconds`` 秒：处理输入事件、推进动画、判定贴边。"""
        app = NSApplication.sharedApplication()
        step = 1.0 / self.fps
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                NSEventMaskAny,
                NSDate.dateWithTimeIntervalSinceNow_(min(step, remaining)),
                NSDefaultRunLoopMode,
                True,
            )
            if event is not None:
                app.sendEvent_(event)
            self._poll()
            self._paint()

    def _paint(self) -> None:
        if self._view is None:
            return
        self._view.setNeedsDisplay_(True)
        self._window.displayIfNeeded()

    def _cursor(self) -> wl.Point:
        if self._cursor_override is not None:
            return self._cursor_override()
        return self._space().to_local_point(NSEvent.mouseLocation())

    def _body_in_screen(self) -> wl.Rect:
        assert self._character is not None
        return wl.body_rect_of_window(
            self._window_local, canvas=self._character.canvas, scale=self._scale()
        )

    @property
    def _handle_shown(self) -> bool:
        """是否真的在显示把手条。

        光看 ``dock.collapsed`` 不够：那个标志和窗口几何是两处状态，任何一处不同步
        （脚本直接挪窗口、外部改 frame）就会出现「窗口很大却只画了一根条」的鬼影。
        这里拿几何复核一次：只有窗口**确实**细成一条时才算收起。
        """
        return self._dock.collapsed and min(self._window_local.size) <= wl.HANDLE_THICKNESS + 1.0

    def _is_hot(self, cursor: wl.Point) -> bool:
        if self._handle_shown:
            return self._window_local.contains(cursor, 2.0)  # 把手条整条都算
        return wl.inside_ellipse(cursor, self._body_in_screen(), slack=6.0)

    def _expand_from_edge(self) -> None:
        """把贴边的把手条弹回完整角色（跨轴位置不变，仍贴着同一条边）。"""
        strip = self._window_local
        edge = self._dock.edge
        center = strip.center.y if edge in (wl.Edge.LEFT, wl.Edge.RIGHT) else strip.center.x
        self._set_window_rect(
            wl.docked_rect(edge, self._space().screen, self._window_size(), center=center)
        )

    def _poll(self) -> None:
        if self._window is None or self._dragging:
            return
        cursor = self._cursor()
        hot = self._is_hot(cursor)
        if bool(self._window.ignoresMouseEvents()) == hot:  # 只在需要时戳 ObjC
            self._window.setIgnoresMouseEvents_(not hot)
        action = self._dock.update(cursor, self._window_local)
        if action is wl.DockAction.COLLAPSE:
            assert self._character is not None
            body = self._body_in_screen()
            self._set_window_rect(wl.collapsed_rect(self._dock.edge, self._space().screen, body))
            log.debug("贴边收起：%s", self._dock.edge)
        elif action is wl.DockAction.EXPAND:
            self._expand_from_edge()
            log.debug("贴边展开：%s", self._dock.edge)

    # —— 鼠标：拖拽就位 / 松手贴边 ——————————————————————————————————————————

    def _view_point(self, event: Any) -> wl.Point:
        """事件坐标（视图左下原点）→ 窗口内左上原点坐标。"""
        point = event.locationInWindow()
        return wl.Point(point.x, self._window_local.height - point.y)

    def _mouse_down(self, event: Any) -> None:
        self.start_drag()
        point = self._view_point(event)
        self._drag_offset = (point.x - self._window_local.x, point.y - self._window_local.y)

    def _mouse_dragged(self, event: Any) -> None:
        if not self._dragging:
            return
        point = self._view_point(event)
        self.move_window_to(point.x - self._drag_offset[0], point.y - self._drag_offset[1])

    def _mouse_up(self, _event: Any) -> None:
        if not self._dragging:
            return
        edge = self.end_drag()
        log.debug("拖拽结束：%s", edge)

    # —— 绘制 ————————————————————————————————————————————————————————————

    def _view_height(self) -> float:
        """视图当前高度 —— 窗口缩放后视图会跟着变，用窗口矩形推是错的。"""
        if self._view is not None:
            return float(self._view.bounds().size.height)
        return self._window_local.height

    def _local(self, rect: wl.Rect) -> Any:
        """**窗口内**左上原点矩形 → 视图坐标（左下原点）。

        传进来的必须是窗口内坐标（左上角为 0,0）。传屏幕坐标会画到视图外面去，
        表现为「窗口内容停在上一次的画面」——这种鬼影很难从代码上看出来，
        所以 ``probe()`` 里带了视图尺寸和最后一次绘制用的矩形，方便对拍。
        """
        return NSMakeRect(
            rect.x, self._view_height() - rect.bottom, rect.width, rect.height
        )

    def _color(self, hex_color: str, alpha: float = 1.0) -> Any:
        r, g, b = wl.parse_hex(hex_color)
        return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)

    def _draw_view(self, view: Any) -> None:
        frame = self._frame
        if frame is None or self._character is None:
            return
        character = self._character
        accent = character.accent(frame.state)
        palette = dict(character.palette)
        t = time.monotonic() - self._started

        NSGraphicsContext.saveGraphicsState()
        try:
            if self._handle_shown:
                self._draw_handle(accent, t)
                return
            body = wl.body_rect_of_window(
                wl.Rect(0.0, 0.0, *self._window_local.size),
                canvas=character.canvas,
                scale=self._scale(),
            )
            pose = wl.pose_for(character.spec(frame.state).motion, t)
            self._apply_pose(pose, self._local(body))
            art = self._art_for(frame.state, character)
            if art is not None:
                self._draw_art(art, self._local(body))
            else:
                self._draw_rig(body, palette, accent, pose)
            self._draw_caption(frame, character, accent)
        finally:
            NSGraphicsContext.restoreGraphicsState()

    def _apply_pose(self, pose: wl.Pose, body_ns: Any) -> None:
        center_x = body_ns.origin.x + body_ns.size.width / 2.0
        center_y = body_ns.origin.y + body_ns.size.height / 2.0
        move = NSAffineTransform.transform()
        move.translateXBy_yBy_(pose.dx, -pose.dy)  # 布局层 y 向下，这里翻回来
        move.concat()
        spin = NSAffineTransform.transform()
        spin.translateXBy_yBy_(center_x, center_y)
        spin.rotateByDegrees_(-pose.rotation)  # 屏幕坐标里正角是顺时针，AppKit 相反
        spin.scaleBy_(pose.scale)
        spin.translateXBy_yBy_(-center_x, -center_y)
        spin.concat()

    def _draw_rig(self, body: wl.Rect, palette: Mapping[str, str], accent: str, pose: wl.Pose) -> None:
        """通用程序化骨架：任何只填了 palette 的角色包都能立刻有张脸。

        这里**只**用角色包给的配色和动作，没有任何角色私有造型 —— 选定形象后
        由角色包提供 per-state 图片覆盖（见 :meth:`_art_for`）。
        """
        body_ns = self._local(body)
        ink = palette.get("ink", "#2B2E3A")
        base = palette.get("body", "#F4EFE6")
        shade = palette.get("shade", "#DCD3C4")
        glow = palette.get("glow", accent)

        # 光环：随 glow 强度呼吸
        for i, (grow, alpha) in enumerate(((0.18, 0.05), (0.12, 0.07), (0.06, 0.10))):
            ring = NSMakeRect(
                body_ns.origin.x - body.width * grow,
                body_ns.origin.y - body.height * grow,
                body.width * (1 + grow * 2),
                body.height * (1 + grow * 2),
            )
            self._color(glow, min(1.0, alpha * (0.35 + pose.glow))).setFill()
            NSBezierPath.bezierPathWithOvalInRect_(ring).fill()

        # 本体
        shell = NSBezierPath.bezierPathWithOvalInRect_(body_ns)
        self._color(base, 0.97).setFill()
        shell.fill()
        self._color(shade, 0.85).setStroke()
        shell.setLineWidth_(2.0)
        shell.stroke()

        # 高光
        gloss = NSMakeRect(
            body_ns.origin.x + body.width * 0.22,
            body_ns.origin.y + body.height * 0.62,
            body.width * 0.22,
            body.height * 0.14,
        )
        self._color("#FFFFFF", 0.30).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(gloss).fill()

        # 表情
        self._draw_face(body_ns, ink, accent, pose)

        # 状态色底环：一眼能看出档位
        band = NSMakeRect(
            body_ns.origin.x + body.width * 0.30,
            body_ns.origin.y + body.height * 0.10,
            body.width * 0.40,
            max(3.0, body.height * 0.045),
        )
        self._color(accent, 0.9).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            band, band.size.height / 2.0, band.size.height / 2.0
        ).fill()

    def _draw_face(self, body_ns: Any, ink: str, accent: str, pose: wl.Pose) -> None:
        width, height = body_ns.size.width, body_ns.size.height
        eye_y = body_ns.origin.y + height * 0.58
        eye_r = max(3.0, width * 0.075)
        gap = width * 0.19
        centers = (
            (body_ns.origin.x + width / 2.0 - gap, eye_y),
            (body_ns.origin.x + width / 2.0 + gap, eye_y),
        )
        eye = pose.eye
        # 弧线（笑眼/撇嘴）走的是**描边**色，必须显式设一遍；
        # 否则会沿用上面球壳的 shade 色，笑眼会淡成一道灰边。
        self._color(ink, 0.95).setFill()
        self._color(ink, 0.95).setStroke()
        for cx, cy in centers:
            if eye == "closed":
                bar = NSMakeRect(cx - eye_r, cy - 1.0, eye_r * 2, 2.0)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bar, 1.0, 1.0).fill()
            elif eye == "squint":
                bar = NSMakeRect(cx - eye_r, cy - eye_r * 0.25, eye_r * 2, eye_r * 0.5)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bar, eye_r * 0.25, eye_r * 0.25).fill()
            elif eye == "happy":
                arc = NSBezierPath.bezierPath()
                arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                    (cx, cy), eye_r, 20.0, 160.0
                )
                arc.setLineWidth_(max(2.0, eye_r * 0.45))
                arc.stroke()
            elif eye == "cross":
                size = eye_r * 1.1
                first = NSBezierPath.bezierPath()
                first.moveToPoint_((cx - size, cy - size))
                first.lineToPoint_((cx + size, cy + size))
                second = NSBezierPath.bezierPath()
                second.moveToPoint_((cx - size, cy + size))
                second.lineToPoint_((cx + size, cy - size))
                for path in (first, second):
                    path.setLineWidth_(max(2.0, eye_r * 0.4))
                    path.stroke()
            else:
                scale = 1.35 if eye in ("wide", "focus") else 1.0
                oval = NSMakeRect(cx - eye_r, cy - eye_r * scale, eye_r * 2, eye_r * 2 * scale)
                NSBezierPath.bezierPathWithOvalInRect_(oval).fill()

        # 嘴：日常一条平嘴（别一脸苦相），干活/完成是笑，出错才撇嘴
        mouth_y = body_ns.origin.y + height * 0.34
        mouth_w = width * 0.20
        mid_x = body_ns.origin.x + width / 2.0
        if eye == "cross":
            mouth = NSBezierPath.bezierPath()
            mouth.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                (mid_x, mouth_y - height * 0.04), mouth_w, 30.0, 150.0
            )
            self._color(ink, 0.75).setStroke()
            mouth.setLineWidth_(max(2.0, height * 0.012))
            mouth.stroke()
        elif eye in ("happy", "focus"):
            mouth = NSBezierPath.bezierPath()
            mouth.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                (mid_x, mouth_y + height * 0.06), mouth_w, 200.0, 340.0
            )
            self._color(ink, 0.8).setStroke()
            mouth.setLineWidth_(max(2.0, height * 0.012))
            mouth.stroke()
        else:
            bar = NSMakeRect(
                mid_x - mouth_w * 0.75,
                mouth_y,
                mouth_w * 1.5,
                max(2.5, height * 0.016),
            )
            self._color(ink, 0.7).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bar, bar.size.height / 2.0, bar.size.height / 2.0
            ).fill()

    def _draw_handle(self, accent: str, t: float) -> None:
        """收起状态：贴边的一条发光把手，提示「鼠标移过来」。"""
        # 窗口内坐标（不是屏幕坐标！）—— 收起时窗口本身就是那条把手
        rect = self._local(wl.Rect(0.0, 0.0, *self._window_local.size))
        pulse = 0.55 + 0.25 * (1.0 + math.sin(2 * math.pi * t / 1.6)) / 2.0
        radius = min(rect.size.width, rect.size.height) / 2.0
        bar = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
        self._color(accent, pulse).setFill()
        bar.fill()
        grip = NSMakeRect(
            rect.origin.x + rect.size.width * 0.32,
            rect.origin.y + rect.size.height * 0.32,
            rect.size.width * 0.36,
            rect.size.height * 0.36,
        )
        self._color("#FFFFFF", 0.75).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            grip, grip.size.height / 2.0, grip.size.height / 2.0
        ).fill()

    def _draw_caption(self, frame: Render, character: Character, accent: str) -> None:
        """底部状态文案。画在窗口内部（固定文案带），永远不出屏幕。"""
        self._last_caption_drawn = ""
        if frame.state is State.IDLE:
            return  # 待机时不挂字，保持桌面干净
        text = frame.caption or character.spec(frame.state).caption
        if not text:
            return
        self._last_caption_drawn = text
        band = wl.Rect(
            wl.PAD * 0.5,
            self._window_local.height - wl.CAPTION_BAND + 3.0,
            self._window_local.width - wl.PAD,
            wl.CAPTION_BAND - 6.0,
        )
        paragraph = NSMutableParagraphStyle.alloc().init()
        paragraph.setAlignment_(NSTextAlignmentCenter)
        paragraph.setLineBreakMode_(NSLineBreakByTruncatingTail)
        attributes = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11.5),
            NSForegroundColorAttributeName: self._color(accent, 0.95),
            NSParagraphStyleAttributeName: paragraph,
        }
        NSString.stringWithString_(text).drawInRect_withAttributes_(self._local(band), attributes)

    # —— 造型：角色包优先，程序化骨架兜底 ——————————————————————————————————

    def _art_for(self, state: State, character: Character) -> Any | None:
        """按角色包找这一状态的图：``assets.base`` / ``assets.<state>`` / ``assets/<state>.*``。

        ``assets.base`` 的语义是「所有状态共用的一张图」（状态只改配色和动作）；
        给了 ``assets.<state>`` 或自动发现的 ``assets/<state>.png|svg`` 就用分状态图。
        """
        path = self._art_path(state, character)
        if path is None:
            return None
        key = str(path)
        if key not in self._art_cache:
            image = NSImage.alloc().initWithContentsOfFile_(str(path))
            if image is None:
                if key not in self._art_warnings:
                    self._art_warnings.add(key)
                    log.warning("角色图加载失败，退回程序化骨架：%s", path)
                self._art_cache[key] = None
            else:
                self._art_cache[key] = image
        image = self._art_cache[key]
        self._last_art = path.name if image is not None else "(程序化骨架)"
        return image

    def _art_path(self, state: State, character: Character) -> Path | None:
        base_dir = character.path
        assets = dict(character.assets)
        for key in (state.value, "base"):
            rel = assets.get(key)
            if rel and base_dir is not None:
                candidate = base_dir / rel
                if candidate.is_file():
                    return candidate
        if base_dir is not None:
            folder = base_dir / "assets"
            for suffix in _IMAGE_SUFFIXES:
                candidate = folder / f"{state.value}{suffix}"
                if candidate.is_file():
                    return candidate
        return None

    def _draw_art(self, image: Any, body_ns: Any) -> None:
        size = image.size()
        if size.width <= 0 or size.height <= 0:
            return
        ratio = min(body_ns.size.width / size.width, body_ns.size.height / size.height)
        width, height = size.width * ratio, size.height * ratio
        target = NSMakeRect(
            body_ns.origin.x + (body_ns.size.width - width) / 2.0,
            body_ns.origin.y + (body_ns.size.height - height) / 2.0,
            width,
            height,
        )
        image.drawInRect_fromRect_operation_fraction_(
            target, NSZeroRect, NSCompositingOperationSourceOver, 1.0
        )
