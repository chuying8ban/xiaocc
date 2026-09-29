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

import contextlib
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from .. import device
from .. import settings as settings_store
from ..characters import Character
from ..engine import Render
from ..protocol import STATE_TTL, State
from ..quota import default_quota_path
from ..quota.badge import bubble_candidates
from ..quota.store import load as load_quota_report
from . import appkit_art
from . import window_layout as wl
from .anchor_store import anchor_path as _anchor_path
from .anchor_store import load_anchor, save_anchor
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
        NSMakePoint,
        NSMakeRect,
        NSMenu,
        NSMenuItem,
        NSMutableParagraphStyle,
        NSObject,
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

#: 谁在推动画面 —— 写进自证据，让外部判据不必靠「圈速低但像素在动」去猜实现形态。
#: ``self``：本进程每帧重画（判据看圈速 ≈ fps）；``compositor``：交给窗口服务器（判据看像素在动）。
DRIVER = "self"

#: 收起时把手的呼吸周期（秒）。指纹按这个周期量化相位 —— 呼吸动画一帧都不能少。
_HANDLE_PULSE_PERIOD = 1.6

#: 动画量的量化精度（小数位）。3 位足够把相邻两帧的动作分开，又吃得掉无意义的抖动。
_POSE_PRECISION = 3

#: 一圈最多消费多少个**就绪**事件：拖拽时鼠标事件成串到达，一次只取一个会跟不上手；
#: 给个上限是防止极端事件洪水把这一帧拖长。
_MAX_EVENTS_PER_LOOP = 64

#: 判定「位置真的变了」的阈值（逻辑像素）。低于它就是浮点噪声，不值得打一行日志。
_MOVE_EPSILON = 0.5

#: 允许「不回锚」的位移理由 —— 只有用户交互算正常位移：拖着走、贴边收起、贴边展开。
#: 其余理由（``anchor`` = 摆到锚点、``restore`` = 回锚）一旦目标不在锚点上，
#: 就说明有代码在乱挪窗口，:meth:`AppKitBackend._set_window_rect` 当场改成回锚。
_ANCHOR_FREE_REASONS = frozenset({"drag", "collapse", "expand"})

#: 漂移自检的容差（逻辑像素）。比 :data:`_MOVE_EPSILON` 松一档，用来吃掉窗口服务器的取整。
_DRIFT_TOLERANCE = 1.0

#: 漂移自检的节拍（秒）：**累计**够这么久才查一次，不是每帧查 —— 查一次要戳 ObjC 拿真实 frame。
_DRIFT_CHECK_PERIOD = 1.0

#: 拖拽态事件循环的睡眠上限（秒）。拖拽时帧预算（``1/fps``）必须让位：
#: **鼠标事件的排空节奏就是跟手的节奏** —— 一圈睡 66ms 就等于把窗口位置更新压到 15 次/秒
#: （部署形态 fps=15 实测 16.5~18.7 次/秒，手上就是台阶感）。压到 4ms 让位置按设备速率落下去。
#: 内容重绘不走这条路（见 :meth:`AppKitBackend._paint` 的帧预算闸 + ``_set_window_rect`` 里
#: 拖动不做强制同步重绘），所以圈速提上来**不会**把软件光栅化的次数一起抬上去。
_DRAG_MAX_SLEEP = 0.004

#: 拖拽兜底：鼠标键**已经全松开**但 ``mouseUp`` 没到（事件丢了、窗口被 orderOut、被别的
#: 事件循环吃掉）⇒ 自己收尾。拖拽态圈速被抬到 ~250 圈/s（见 ``_DRAG_MAX_SLEEP``），
#: 漏一次 mouseUp 不再是「白烧一点 CPU」：``_poll`` 在 ``_dragging`` 时直接返回，
#: 悬停/贴边/锚点写入会**一直冻到重启**，同时以 ~15% 烧一个核（2026-09-29 那个跑了
#: 3.5 小时、9.6% 的遗留实例就是同一类病）。按住不放时按钮非 0，所以
#: 「手停住不动的拖动」不会被误判成松手。宽度取 80ms：真实拖动的相邻事件间隔是
#: 几毫秒量级，而窗口服务器切换按钮状态是同帧的。
_TAP_SLOP = 3.0  #: 按下到松开的位移 ≤ 这么多像素就算「点击」，不算拖动
#: 按住超过这么久就不当点击。**从 0.6 放到 1.5**（2026-09-29）：点击现在有语义了
#: （默认弹额度条），0.6 只比双击间隔 0.5 大 0.1s —— 慢慢点一下（0.6~1.2s 很正常的手感）
#: 以前最多「什么都不做」，现在会变成「点了没反应」。真正的分布等点击日志攒出来再定。
_TAP_MAX_HOLD_S = 1.5
#: 对话气泡在屏上停留几秒（用户指定 5 秒；不进引擎、不改 state）
_BADGE_TTL_S = 5.0
#: 最后这一小段里从 1.0 淡到 0.0（用户要「淡化消失」，不是啪一下没了）
_BADGE_FADE_S = 1.2
#: 气泡几何：两行文字 + 朝上指向角色的尖角。**气泡画在窗口内、窗口尺寸一个字不动** ——
#: 长高窗口会碰到「除用户拖动外任何位移都算 bug ⇒ 回锚 + 留痕」那套自检和 anchor_ok 判据。
_BUBBLE_PAD_X = 6.0
_BUBBLE_BOTTOM = 2.0
_BUBBLE_BODY_H = 40.0
_BUBBLE_TAIL_H = 7.0
_BUBBLE_LINE_H = 15.0
#: 两行文字离气泡左右内壁的内缩（**画字和量字必须用同一个数**，见 :func:`bubble_text_width_for`）
_BUBBLE_TEXT_INSET = 8.0
#: 设置文件的重读周期（只 stat 一下 mtime，变了才真读）
_SETTINGS_POLL_S = 5.0


def bubble_text_width_for(window_width: float) -> float:
    """气泡里每行文字可用宽度 —— **唯一一处算法**。

    运行时挑候选（``AppKitBackend._bubble_text_width``）与宽度笔
    （``scripts/measure_bubble_widths.py``）都调它。两处各写一个数就会漂：2026-09-29 @writer
    抓到笔自己写 148（照「窗口 160 − 左右各 6」），而画字还有左右各 8px 内缩 ⇒ 真机每行只有
    **132px**，笔会把 133~148px 的候选判成「放得下」，真机上却选不上、或选中后被截成半句话
    （`Credits1200.00 · 9 分钟前` 134.5px 就是那条）。一处数字、多处引用，才配当判据。
    """
    return window_width - _BUBBLE_PAD_X * 2.0 - _BUBBLE_TEXT_INSET * 2.0


def bubble_budget_for(character: Any) -> float:
    """按角色算气泡每行可用宽（**不用开窗口**：宽度笔用）。

    窗口宽走 ``window_layout.window_size_for`` —— 跟 ``AppKitBackend._window_size()`` 同一个函数，
    所以笔算出来的窗口宽必然等于真机那扇窗。
    """
    width, _height = wl.window_size_for(character.canvas, character.default_scale)
    return bubble_text_width_for(width)
_DRAG_BUTTON_UP_GRACE = 0.08

_IMAGE_SUFFIXES = (".png", ".svg", ".pdf", ".tiff", ".jpg", ".jpeg")


def _quantize_pose(pose: Any) -> tuple[Any, ...]:
    """把姿势量化成可比对的元组：浮点四舍五入到 ``_POSE_PRECISION`` 位，其余字段原样。

    量化的目的是「同一个动作别算成两个」，而 33ms 一帧的位移仍会落到不同格子里 ——
    所以浮动能照常播，不会被整帧跳过。
    """
    if is_dataclass(pose):
        values: tuple[Any, ...] = tuple(getattr(pose, field.name) for field in fields(pose))
    elif hasattr(pose, "__dict__"):  # 防御：布局层哪天换成普通类
        values = tuple(getattr(pose, name) for name in sorted(vars(pose)))
    else:
        values = (pose,)
    return tuple(
        round(value, _POSE_PRECISION) if isinstance(value, float) else value for value in values
    )


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

    @property
    def usable(self) -> wl.Rect | None:
        """可用区（macOS 的 ``visibleFrame``）：已经让开了菜单栏和 Dock。

        **自己挑的位置**（命令行 ``at=`` / 默认右上角）要落在里面 —— 否则
        ``at=bottom-right`` 会把窗口送进 Dock 带（实测底部让出 90px），而 Dock 的
        窗口层级比我们高，角色就埋在 Dock 底下点不到了。用户**手拖**的位置不在此列：
        那是用户的意图，不该被我们二次夹取。
        """
        ns = NSScreen.mainScreen()
        if ns is None:
            return None
        vis = ns.visibleFrame()
        rect = self.to_local_rect(vis)
        return rect if rect.width > 0 and rect.height > 0 else None

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

    def rightMouseDown_(self, event):
        """右键**显式**走自己的路：不进拖拽/tap 判定（不然右键会顺手把桌宠拖走）。"""
        if self._backend is not None:
            self._backend._right_mouse_down(event, view=self)

    def menuForEvent_(self, event):
        """系统默认的右键转菜单在这个无边框 + 非 key 窗口上不可靠，自己去弹。"""
        return


class _MenuTarget(NSObject):
    """右键菜单的动作接收者（NSMenu 只认 ObjC target，所以得有这么个壳）。"""

    def initWithBackend_(self, backend):
        self = objc.super(_MenuTarget, self).init()  # noqa: PLW0642 - pyobjc 的 initWith… 惯用法
        if self is None:
            return None
        self._backend = backend
        return self

    def openPanel_(self, _sender):
        self._backend._open_panel()

    def restartPet_(self, _sender):
        self._backend._restart_pet()

    def quitPet_(self, _sender):
        self._backend._quit_pet()

    def noop_(self, _sender):
        """设备状态那几行挂着它：**要 enabled 才是正常黑字**（disabled 会灰掉，像坏了），
        点了什么也不做，只把菜单收起来。"""

class AppKitBackend(Backend):
    """macOS 原生窗口显示层。用法：``xiaocc run --backend appkit``。

    初始位置（= 锚点）的优先级：``--backend-opt at=...`` > 用户上次拖动留下的锚点 >
    默认右上角。

    位置语义：**谁有权挪窗口**
    --------------------------
    窗口位置的变动只有两种性质，必须分开对待：

    * **用户拖动 = 有意搬家**。松手且没贴边时，把新位置落成锚点
      （``~/.xiaocc/anchor.json``，见 :mod:`xiaocc.backends.anchor_store`），
      下次启动还停在这儿 —— 绝不自己弹回默认角落。
    * **除拖动外的一切位移 = bug**。贴边收起/展开是明确的临时态（位置由贴边几何决定，
      不算搬家、也不改锚点）；其余「窗口不在锚点上」一律当漂移：立刻回锚，并在日志里
      留下 ``位置变化 [reason] ...`` / ``检测到窗口漂移 ...`` 的痕迹，别悄悄漂走。

    落地方式：所有位移收口到 :meth:`_set_window_rect`，必须报 ``reason``
    （``drag`` / ``collapse`` / ``expand`` / ``anchor`` / ``restore``）；
    :meth:`_pump` 里每秒最多一次拿 ``window.frame()`` 的**真实**坐标和锚点对账。
    锚点文件同时是运维 ``xiaoccctl doctor`` 判断「窗口还在不在该在的地方」的参照，
    :meth:`probe` 里的 ``anchor`` / ``anchor_ok`` / ``anchor_state`` 就是给它的自证据。
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
        #: 锚点来源在这里就定下来（谁说了算），但**矩形**要等屏幕尺寸和角色画布已知才算得出
        #: （见 :meth:`_initial_rect`）。优先级：① 运维显式传的 ``at=``；② 没传 ``at=`` 时，
        #: 用户上次拖动落盘的锚点；③ 都没有就默认右上角（与没有锚点机制时的行为一致）。
        #: 传了 ``at=`` 就不去读磁盘 —— 运维说了算，而且不覆写用户那份。
        self._saved_anchor: tuple[float, float] | None = None if at is not None else load_anchor()
        #: 注入光标来源 —— 自动化截图/回归脚本用它模拟「鼠标在哪」，正常跑用真实鼠标。
        self._cursor_override = cursor
        self._window: Any = None
        self._view: Any = None
        self._space_cache: _Space | None = None
        self._window_local: wl.Rect = wl.Rect(0.0, 0.0, 0.0, 0.0)
        self._frame: Render | None = None
        #: 引擎**每拍**递过来的当前帧（含内容没变的静默拍，见 :meth:`observe`）—— 只记不画。
        #: 和 ``_frame`` 的分工：``_frame`` 是「画上屏的那帧」（内容变化才更新）⇒ 答
        #: 「这个状态挂屏多久」；``_observed`` 每拍都刷 ⇒ 答「源这份报告多旧」。
        self._observed: Render | None = None
        self._character: Character | None = None
        self._dock = wl.Dock()
        #: 窗口的「家」（含窗口尺寸）：用户拖动后会更新并落盘（见 anchor_store），
        #: 其余任何位移都要回到这里。真值在 :meth:`_ensure_window` 里算出，
        #: 在那之前窗口也还不存在，所以先摆一个和 ``_window_local`` 同款的占位矩形。
        self._anchor: wl.Rect = wl.Rect(0.0, 0.0, 0.0, 0.0)
        #: 上次漂移自检的时刻 —— 每 :data:`_DRIFT_CHECK_PERIOD` 查一次（见 :meth:`_pump`）。
        #: 用**墙钟**而不是「这一圈干了多少活」：主循环一圈里大部分时间在 sleep，
        #: 按活计时间攒，1 秒的节拍要十几秒才攒够一次自检 —— 漂了却半天没人管。
        self._last_drift_check = time.monotonic()
        #: 自证据写盘失败只吵一次（诊断文件写不进去不该每秒刷屏，更不该影响桌宠）
        self._probe_warned = False
        self._dragging = False
        #: 这次拖拽是不是真鼠标起的（``start_drag`` 的 ``from_mouse``）—— 兜底只看这条
        self._drag_from_mouse = False
        self._drag_offset = (0.0, 0.0)
        self._started = time.monotonic()
        self._art_cache: dict[str, Any] = {}
        self._art_warnings: set[str] = set()
        #: 点阵化缓存（见 ``appkit_art``）：角色图这种每帧都画的内容，只光栅化一次。
        self._bitmaps = appkit_art.BitmapCache()
        self._last_art_key = ""
        self._last_art: str = "(程序化骨架)"
        self._last_paint_state: str | None = None
        self._last_caption_drawn: str = ""
        #: 上一次**真正画下去**那一帧的指纹；一致就跳过重绘（见 :meth:`_fingerprint`）
        self._last_fingerprint: tuple[Any, ...] | None = None
        #: 上一次真正重绘的时刻 —— 拖拽态的帧预算闸用它（见 :meth:`_paint`）
        self._last_paint_at = 0.0
        #: 拖拽兜底：鼠标键第一次读到「全松开」的时刻（见 ``_DRAG_BUTTON_UP_GRACE``）
        self._buttons_up_since: float | None = None
        #: 这次按下是不是「点击」（按下到松开没怎么动）—— 判定见 :meth:`_mouse_up`
        self._press_at: float | None = None
        self._press_point: wl.Point | None = None
        self._press_moved = False
        #: 按下期间**最大**位移（px）：_TAP_SLOP 该定多少得看真实分布，不能拍脑袋
        self._press_max_moved = 0.0
        #: 上一次点击的时刻（判双击用）与设置文件的重读时刻
        self._last_click_at = 0.0
        self._settings_checked_at = 0.0
        #: 当前设置（★唯一来源 ~/.xiaocc/settings.json，见 xiaocc.settings）
        self._settings = settings_store.load()
        self._settings_mtime: float | None = None
        #: 气泡那格的设备补数时刻（``None`` = 不用补）：点击时基线没攒够才排一次
        self._bubble_refill_at: float | None = None
        #: 补数时按哪一档重算字面
        self._bubble_action: str = "badge"
        #: 对话气泡：行、截止时刻、**真的画上去的那份**（自证据用，别和 caption 混）
        self._badge_lines: list[str] = []
        self._badge_text = ""
        self._badge_until = 0.0
        self._badge_drawn = ""
        self._badge_dirty = False
        #: 真画下去多少帧（每秒结算）—— 用来把「5% 还是 1.9%」那两档钉死：5% 那几拍若是
        #: 15 帧/秒、安静态是 0~3，差别就全在指纹闸上；两边一样就得往别处查（@researcher 的建议）。
        self._paints = 0
        self._paints_window_start = self._started
        self._paints_per_sec = 0.0
        self._menu_target: Any = None
        #: 设备状态采样器（右键菜单要弹时现问一次，TTL 缓存；不进 UI 循环）
        self._device = device.Sampler()
        #: 光标当前是否落在热区 —— 由 :meth:`_poll` 维护，指纹要用
        self._cursor_hot = False
        #: 事件循环的自证据：本秒累计圈数 / 上一秒结算出的圈速 / 上一圈真正睡了多久
        self._pump_loops = 0
        self._pump_loops_per_sec = 0.0
        self._pump_window_start = time.monotonic()
        self._pump_sleep_ms = 0.0
        #: 当前这一拍的起点时刻 —— :meth:`idle` 用它算「还差多久到下一拍」。
        #: 在 __init__ 里就初始化，免得第一轮还没有 render 过就读不到属性。
        self._last_tick = time.monotonic()

    # —— 生命周期 ——————————————————————————————————————————————————————————

    def render(self, frame: Render) -> None:
        """呈现新帧，并在本层的事件循环里把这一拍（``interval``）走掉。

        「谁负责走时间」的约定：本层 ``self_paced = True`` ⇒ 节拍归本层，``cli.py``
        不会再替我们补 ``time.sleep()``。所以有新帧的这一轮由这里走：``_pump(interval)``
        边跑事件循环边等，动画才连续；没有新帧的那一轮由 :meth:`idle` 补上剩余时间
        （见那里的说明，两边加起来必须**每轮都恰好走掉一拍**）。
        """
        self._ensure_window(frame.character)
        if frame.state is not self._last_paint_state:
            self._last_paint_state = frame.state
            # 状态一变，动作从这一秒重新开始 —— 否则切到「搞定」会从半截开始跳
            self._started = time.monotonic()
        self._frame = frame
        self._paint(force=True)  # 引擎推来的新帧一定画一次；之后的节拍由 _pump 按指纹决定
        beat_start = time.monotonic()
        self._pump(self.interval)  # 用这段节拍跑自己的事件循环 → 动画连续
        # 记「这一拍的起点」而不是终点：这一拍已经在上面的 _pump 里走完了，同一轮里
        # 紧跟其后的 idle() 算出 gap ≈ 0，就不会再多跑一段（否则有帧的那轮要花两拍）。
        self._last_tick = beat_start

    def observe(self, frame: Render) -> None:
        """记下引擎这一拍的当前帧 —— **只存不画**（画是 :meth:`render` 的事）。

        内容没变的静默拍里 ``tick()`` 返回 ``None``、``render()`` 不会被调用，但
        ``engine.frame`` 的 ``event.at`` 仍然是新鲜的。没有这个钩子，``_frame.event`` 会
        一直冻在「该状态最后一次**变化**的时刻」，:meth:`probe` 就只剩「挂屏多久」这一个
        读数 —— 长工具调用期间它必然超过保鲜期，doctor 于是把一个正在干活的桌宠报成
        「事件永不过期」。这里绝不调 ``_paint()``：静默拍占主循环的绝大多数轮次，
        每拍重画等于把引擎省下来的重画全还回去，CPU 回到忙等那一档。
        """
        self._observed = frame

    def idle(self) -> None:
        """本轮没有新帧：把「到下一个节拍」的剩余时间在自己的事件循环里走掉。

        约定同上：``self_paced = True`` ⇒ 节拍归本层，``render()`` 走掉一个
        ``interval``、本方法补上这一拍剩下的 ``gap``（``self.interval`` 是 CLI 注入的
        节拍，默认 0.25，实跑 1.0）。引擎只在状态变化时给帧，所以主循环里**绝大多数**
        轮次都走到这里 —— 也就是说这里**绝不能立刻返回**：立刻返回等于整个主循环没有任何
        限速，实测 5 秒空转 **732768 圈**（其中 732767 圈 ``tick()`` 返回 ``None``，
        既不 render 也不睡）、CPU **99.8%**，一个核吃满；把这一拍走掉之后同样的循环只有
        个位数百分比。注意 ``_pump()`` 内部还有一层帧预算限速（``budget`` + ``time.sleep``），
        那层管「一圈之内别空转」，这层管「没有新帧时也得有人把时间走掉」，两者都要。
        """
        gap = self.interval - (time.monotonic() - self._last_tick)
        if gap > 0.0:
            self._pump(gap)
        # 这一拍到这儿算走完，下一拍从现在开始计时（gap ≤ 0 时也要重置，
        # 否则 _last_tick 会越来越旧、算出的 gap 恒为负，又退回空转）。
        self._last_tick = time.monotonic()

    def linger(self, seconds: float) -> None:
        """保持窗口 N 秒（截图 / 肉眼验收）。期间动画照跑。"""
        if seconds > 0 and self._window is not None:
            self._pump(seconds)

    def close(self) -> None:
        if self._window is not None:
            # 最后一份快照要标 dead，否则外部读到的是一份「看门狗看着还活着」的旧文件
            self._write_probe(alive=False)
            try:
                self._window.setIgnoresMouseEvents_(True)
                self._window.orderOut_(None)
            except Exception:  # pragma: no cover - 退出阶段不值得炸
                log.debug("关闭窗口失败", exc_info=True)
        self._window = None
        self._view = None
        self._frame = None
        self._observed = None
        self._last_fingerprint = None

    # —— 供脚本/自动化调用（和鼠标走同一套代码路径）—————————————————————————

    def move_window_to(self, x: float, y: float) -> None:
        """把窗口挪到屏幕左上角坐标 (x, y) —— 等价于拖拽中的一帧。"""
        size = self._window_size()
        rect = wl.Rect(x, y, *size).clamped_into(self._space().screen)
        self._set_window_rect(rect, reason="drag")

    def start_drag(self, *, from_mouse: bool = False) -> None:
        """程序化拖拽的开始（等价于鼠标按下）—— 自动化脚本/回归用。

        **「等价于鼠标按下」不等于「按钮真的按下了」。** 这条路径上从来没有真按钮，所以拖拽兜底
        （:meth:`_drag_watchdog`，靠 ``pressedMouseButtons`` 判定）必须放过它：只有真实
        ``mouseDown`` 走过的那条路才传 ``from_mouse=True``。不区分的话，程序化 API 会在 80ms
        后被兜底 ``end_drag()`` 收掉 —— ``verify_drag_tracking.py --real`` 就是这么红的
        （「确实拖到了屏幕中央附近」失败：窗口拖到一半被松开）。

        和真鼠标走同一套状态迁移：从把手条上抓起就先弹成完整角色，
        然后交给 :meth:`end_drag` 判定贴边。
        """
        if self._handle_shown:
            self._expand_from_edge()
        self._dock.drag_started()
        self._dragging = True
        #: 这次拖拽是不是**真鼠标**起的 —— 兜底只对真鼠标那条路生效
        self._drag_from_mouse = from_mouse

    def end_drag(self) -> wl.Edge:
        """松手：判定是否贴边收起。返回落在哪条边上。

        **没贴边 = 用户有意搬家**：把新位置落成锚点（磁盘 + 内存一起换），下次启动就停在这儿，
        漂移自检也以这儿为准。贴边收起不算搬家 —— 那是临时态，把手条收起/展开都不该改锚点，
        否则用户把桌宠拖到边上收起来一次，「家」就永久变成屏幕边上了。
        """
        self._dragging = False
        body = self._body_in_screen()
        edge = self._dock.drop(body, self._space().screen, distance=self.snap_distance)
        if edge is not wl.Edge.NONE:
            self._set_window_rect(
                wl.collapsed_rect(edge, self._space().screen, body), reason="collapse"
            )
        else:
            self._save_anchor_from_window()
        return edge

    def _at_anchor(self, *, tolerance: float = _DRIFT_TOLERANCE) -> bool:
        """窗口**真实**frame 是否还压在锚点上（拿窗口服务器那份，不看自己记的簿）。

        比自己记的坐标可靠：判据来自 ``self._window.frame()``，外部（脚本、窗口服务器）
        挪过窗口也能发现 —— 这正是「漂走了却没人知道」的那条缝。
        """
        if self._window is None:
            return True
        origin = self._window.frame().origin
        want = self._space().to_ns_rect(self._anchor).origin
        return abs(origin.x - want.x) <= tolerance and abs(origin.y - want.y) <= tolerance

    def _anchor_relation(self) -> tuple[str, bool]:
        """窗口现在与锚点是什么关系 —— ``(anchor_state, anchor_ok)``，给 :meth:`probe` 用。

        ``anchor_ok`` 只在**没人碰它却漂在别处**时为 ``False``：拖拽中、贴边收起（把手条）、
        从把手条展开的贴边态都算正常交互态，否则运维的 doctor 会把正常交互误报成故障。
        """
        if self._dragging:
            return "drag", True
        if self._handle_shown or self._dock.docked:
            return "collapsed", True
        if self._at_anchor():
            return "anchor", True
        return "drifted", False

    def probe_path(self) -> Path:
        """自证据文件的路径 —— 默认 ``~/.xiaocc/probe.json``，``XIAOCC_PROBE_FILE`` 可覆盖。

        脚本和运维的 doctor 用它从**进程外**断言门槛，测试则必须覆盖它（别污染真实路径，
        锚点那次已经踩过这个坑）。路径也进 :meth:`probe`，省得外部再猜一遍。
        """
        override = os.environ.get("XIAOCC_PROBE_FILE")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".xiaocc" / "probe.json"

    def _write_probe(self, *, alive: bool = True) -> None:
        """每秒把 :meth:`probe` 的快照落盘（原子写），给进程外的把关者用。

        为什么非有不可：**CPU 低有两种可能** —— 真的省，或者被节流了（动画其实在卡）。
        ``ps`` 只给得出前者；「圈速 ≈ fps」只有进程内知道（``pump_loops_per_sec`` /
        ``pump_sleep_ms``），所以门槛的第二条必须靠这份文件从外面断言。
        契约：**写失败绝不影响运行** —— 只 ``log.warning`` 一次，桌宠照跑。
        """
        try:
            info = self.probe()
            info.update(
                {
                    "pid": os.getpid(),
                    "alive": alive,
                    "at": time.time(),
                    "fps": self.fps,
            #: 谁在推动画面：``self`` = 本进程每帧重画（圈速应与 fps 同量级）；
            #: ``compositor`` = 交给窗口服务器做动画（进程该睡着，外部像素判据看「画面在动」）。
            "driver": DRIVER,
                    "probe_interval_s": _DRIFT_CHECK_PERIOD,
                    "probe_file": str(self.probe_path()),
                    "anchor_file": str(_anchor_path()),
                }
            )
            target = self.probe_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            # 临时名必须唯一：面板和验证脚本可能同时在写这份自证据，固定叫 ``.tmp``
            # 会有两个进程抢同一个临时文件，其中一个 os.replace 时扑空（ENOENT）。
            # 和 anchor_store 一样用 mkstemp。
            fd, tmp_name = tempfile.mkstemp(
                prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(info, ensure_ascii=False, indent=2))
                os.replace(tmp_name, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
            self._probe_warned = False
        except Exception as exc:  # noqa: BLE001 —— 诊断文件写不进去不该带崩桌宠
            if not self._probe_warned:
                self._probe_warned = True
                log.warning("自证据落盘失败（不影响运行）：%s", exc)

    def _check_drift(self) -> None:
        """每秒一次的纠偏（``_pump`` 里调）：没人拖动、也不在贴边态，窗口却不在锚点上 → 回锚 + 留痕。

        运维实测过这种漂：启动瞬间 ``1324,96``，跑到约 3 分钟后外部抓到稳定的 ``1225,100``
        （左偏 99px）且自己弹不回来，而位置变化**不留痕**，他在机器外没法判断是正常交互还是卡住。
        所以这里既要把窗口拽回锚点，也要往日志里写一行到底漂了多少。
        """
        if self._window is None or self._dragging or self._dock.docked or self._handle_shown:
            return
        if self._at_anchor():
            return
        current = self._space().to_local_rect(self._window.frame())
        log.warning(
            "检测到窗口漂移（非拖动）(%d,%d) → 回到锚点 (%d,%d)",
            current.x, current.y, self._anchor.x, self._anchor.y,
        )
        self._set_window_rect(self._anchor, reason="restore")

    def _save_anchor_from_window(self) -> bool:
        """把窗口当前左上角落成锚点：内存里的「正确答案」和磁盘上那份一起换。

        内存那份必须同步换，否则下一秒漂移自检就会拿旧锚点把用户刚拖好的窗口拽回去。
        """
        rect = self._window_local
        if rect.x == self._anchor.x and rect.y == self._anchor.y:
            # 原地点一下（按下即松手、没移动）也会走到这 —— 位置没变就别重写一遍：
            # 否则日志里会多出一行一模一样的「锚点已更新」，运维做取证时分不清哪次是真搬家。
            return True
        self._anchor = rect
        if save_anchor(rect.x, rect.y):
            log.info("锚点已更新（用户拖动）: (%d,%d)", rect.x, rect.y)
            return True
        # 写不进磁盘不许静默：那意味着重启之后桌宠会弹回旧位置，用户只会觉得「我明明拖过去了」。
        log.warning(
            "锚点写盘失败（磁盘/权限？）: (%d,%d) 只在这次运行里有效", rect.x, rect.y
        )
        return False

    def probe(self) -> dict[str, Any]:
        """当前窗口的真实状态 —— 自动化证据用，别拿设计文档当结果。"""
        if self._window is None:
            return {"window": None, "driver": DRIVER}
        frame = self._window.frame()
        anchor_state, anchor_ok = self._anchor_relation()
        #: 两个事件、两个口径（都可能是 None：窗口已建、引擎还没推来第一帧，
        #: 所以下面每个 ``state_*`` 字段都得容得下它）：
        #:   ``event`` 取自 ``_frame``（**画上屏**的那帧，只在内容变化时更新）
        #:            ⇒ 答「这个状态**挂屏**多久」；
        #:   ``seen``  取自 ``_observed``（:meth:`observe` 每拍记下、静默拍也记）
        #:            ⇒ 答「源**这份报告**多旧」。
        #: 混用这两个口径就是 doctor ⑬「长工具调用被报成永不过期」的病因。
        event = self._frame.event if self._frame is not None else None
        seen = self._observed.event if self._observed is not None else None
        state = event.state if event is not None else None
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
            #: —— 位置语义的自证据：窗口现在和锚点是什么关系（运维 doctor 就看这三个）——
            #: 锚点的屏幕左上角坐标：用户拖动会更新它，其余任何位移都该回到它。
            "anchor": [round(self._anchor.x, 1), round(self._anchor.y, 1)],
            #: True = 在锚点上，或正处于拖拽/贴边这类**正常交互态**；
            #: False = 没人碰它却漂在别处（就是运维记录里那种「窗口漂走且不回锚点」）。
            "anchor_ok": anchor_ok,
            #: 四选一：``anchor``（在锚点）/ ``drag``（正被拖）/ ``collapsed``（贴边态 ——
            #: 收起的把手条、或从把手条展开的完整角色，细分看上面的 ``dock``）/ ``drifted``（漂了）。
            "anchor_state": anchor_state,
            #: 正在被拖动（真鼠标按住不放）—— 运行时保护要**排除**这种采样：
            #: 拖拽态事件循环被抬到设备速率，CPU 会短暂升到 10~20%（跟手换来的，
            #: 见 ``_DRAG_MAX_SLEEP``），拿它去撞看门狗阈值等于「用户多玩两下就停面板」。
            "dragging": self._dragging,
            "art": self._last_art,
            #: 实际画上去的文案（不是引擎的那份原文）—— 待机时应当为空
            "caption_drawn": self._last_caption_drawn,
            #: 单击贴上去的额度条/文案条**真的画上去了**的那份文字；没画就是空串。
            #: 单独一个字段的理由：``caption_drawn`` 的语义被截图套件的「待机时不挂文案」断言守着，
            #: 拿它去挂额度等于把一条早就验过的守卫悄悄废掉（同 MATCH / --linger 那两次）。
            "badge_drawn": self._badge_drawn,
            #: 气泡当前不透明度（淡化中会从 1.0 掉到 0.0）—— 判据要看「淡化中指纹逐帧变」，
            #: 这个数就是那件事的可读证据
            "badge_alpha": round(self._badge_alpha(), 2),
            #: 真正画下去多少帧/秒（@researcher 那个零成本判定实验：一个数就能把两档 CPU 钉死）
            "paints_per_sec": self._paints_per_sec,
            #: 单击小cc 时按设置做什么（badge / device / all / none）—— 取自 settings.json
            "click_action": self._click_action(),
            #: —— 状态新鲜度的自证据：doctor 从进程外判「画面是不是卡在某个状态不动」
            #: 就看这几行（典型病因：源把毫秒当秒写进 ``at``，事件于是永不过期）——
            #: 这个状态是**哪个源**说的（如 ``hermes:state.db``）；None = 还没有帧。
            "state_source": event.source if event is not None else None,
            #: **源给的**事件时间戳（epoch 秒，即 ``StatusEvent.at``），取自**画上屏**的那帧
            #: ⇒ 语义是「该状态最后一次**变化**的时刻」，不是「源最近一次报告的时刻」
            #: （那个是下面的 ``state_seen_at``）。
            #: 命名坑（别混）：它**不是** :meth:`_write_probe` 里那个 ``at`` ——
            #: 那个是**快照落盘的时刻**，这个是**事件发生的时刻**，
            #: 两者相差 ``state_changed_ago_s``。
            "state_at": event.at if event is not None else None,
            #: **该状态已挂屏多久**（秒）= 写盘那一刻 − ``state_at``。原名 ``state_age_s``，
            #: 名不副实所以改掉了：``render()`` 只在内容变化时被调（内容没变走引擎的静默更新
            #: 分支），所以这里量到的是「这个状态在桌面上挂了多久」，**不是**「源这份报告多旧」。
            #: 保留它是因为「挂屏多久」本身是有用的软信号，但**别拿它跟保鲜期比** ——
            #: 一次长工具调用（``working`` 挂 45s+）就足以让它超过 TTL，那不是病；
            #: 判「该退档没退」请用下面的 ``state_seen_age_s``。
            #: 另外 ``StatusEvent.age()`` 对负值做了夹取
            #: （``max(0.0, ...)``），所以「源给了未来时间戳」（毫秒当秒写就是这一类）
            #: 在这里只会显示 0 —— 光看它分不清「刚刚发生」和「时间戳写错」，
            #: 得拿 ``state_at`` 跟快照 ``at`` 比：``state_at`` 反而超前，就是源写错了。
            #: （``from_json`` 那道 60 秒护栏只挡得住 JSON 源；Python 侧直接构造
            #: ``StatusEvent(at=...)`` 的源没人挡，所以这行自证据必须有。）
            "state_changed_ago_s": round(event.age(), 3) if event is not None else None,
            #: 最近一次**轮询到**的事件的时间戳（:meth:`observe` 每拍记，静默拍也记）
            #: ⇒ 这才是「源最近一次说话的时刻」。
            "state_seen_at": seen.at if seen is not None else None,
            #: **源这份报告多旧**（秒）= 写盘那一刻 − ``state_seen_at``。
            #: 判「状态该退档没退」看它：源每拍都在刷新 ⇒ 长任务期间它也接近 0，
            #: 只有源真停了（或时间戳写坏）才会一直涨。与上面那个数的差 = 这个状态
            #: 演了多久但源一直在为它续命（长任务时两者会差出几十秒）。
            "state_seen_age_s": round(seen.age(), 3) if seen is not None else None,
            #: 这个状态的保鲜期（秒），照抄协议里的 ``STATE_TTL[state]``；
            #: 没有 TTL 的状态（``idle``/``offline``）写 None（JSON null）。
            #: ``state_seen_age_s`` 长期超过它 = 源早不说话了、画面却还卡在这个状态上。
            "state_ttl_s": STATE_TTL[state] if state is not None else None,
            #: 视图尺寸，应当与窗口尺寸一致（不一致说明绘制坐标系会错位）
            "view_size": [
                round(self._view.bounds().size.width, 1),
                round(self._view.bounds().size.height, 1),
            ]
            if self._view is not None
            else None,
            #: —— 事件循环的实测节拍：运维从进程外核对「还烧不烧核」就看这两个数 ——
            #: 上一秒真正跑了多少圈（用上一秒的完整计数，不是瞬时值）。应当 ≈ fps；
            #: 远高于 fps 就说明又退回忙等了。
            "pump_loops_per_sec": self._pump_loops_per_sec,
            #: 上一圈真正睡掉的毫秒数：≈ 1000/fps − 这一圈的开销。
            #: 长期接近 0 说明没在让出 CPU（或一帧画得太久）。
            "pump_sleep_ms": self._pump_sleep_ms,
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
        start = self._initial_rect(space, width, height)
        self._anchor = start
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
        # 开窗口的第一下也走同一个闸（reason=anchor）：万一 AppKit 把 create 时给的 frame
        # 动了（约束/取整），这里会把它按回锚点，而不是让窗口从第一秒起就和锚点对不上。
        self._set_window_rect(start, reason="anchor")
        self._started = time.monotonic()
        self._last_fingerprint = None  # 新窗口 = 画面从零开始，第一帧必须真画
        log.info("AppKit 窗口就绪：%sx%s @ %s", width, height, start)

    def _initial_rect(self, space: _Space, width: float, height: float) -> wl.Rect:
        """窗口初始位置 —— 优先级：命令行 ``at=`` > 落盘的锚点 > 默认右上角。

        用户拖动是**有意搬家**，所以那个位置会落盘（``~/.xiaocc/anchor.json``），下次启动
        还停在那儿；命令行显式给了 ``at=`` 就听命令行的（运维说了算），但不覆写磁盘上的锚点。
        """
        if self._saved_anchor is not None:
            rect = wl.Rect(*self._saved_anchor, width, height).clamped_into(space.screen)
            log.info("按上次拖动的锚点启动：(%d,%d)", rect.x, rect.y)
            return rect
        return wl.parse_anchor(self._at, space.screen, (width, height), usable=space.usable)

    def _set_window_rect(self, rect: wl.Rect, *, reason: str) -> None:
        """**所有**改窗口位置的地方都走这里，便于留痕与纠偏。

        ``reason`` 取 ``drag`` / ``collapse`` / ``expand`` / ``anchor`` / ``restore``：
        前三个是正常位移（拖动、贴边收起、贴身展开），后两个是「锚点本身」与「回锚」。
        除了这三个正常位移，任何偏离锚点的位置都当成 bug —— 直接改成回锚并写一行 warning。
        裸调 ``setFrame`` 会让窗口悄悄漂走而日志里什么都没有（运维实测过：跑到 3 分钟时
        窗口稳定停在 `1225,100`，比锚点左偏 99px，原因无从查起）。
        """
        assert self._window is not None
        anchor = self._anchor
        off_anchor = (
            abs(rect.x - anchor.x) > _MOVE_EPSILON or abs(rect.y - anchor.y) > _MOVE_EPSILON
        )
        if reason not in _ANCHOR_FREE_REASONS and off_anchor:
            log.warning(
                "位置偏离锚点，回锚：[%s] 想放到 (%d,%d)，锚点 (%d,%d)，偏离 (%+d,%+d)",
                reason, rect.x, rect.y, anchor.x, anchor.y,
                round(rect.x - anchor.x), round(rect.y - anchor.y),
            )
            # 就地改目标和理由，**不递归调用自己** —— 递归只会把一次纠偏打成两层日志。
            rect, reason = anchor, "restore"
        moved = (
            abs(rect.x - self._window_local.x) > _MOVE_EPSILON
            or abs(rect.y - self._window_local.y) > _MOVE_EPSILON
        )
        if moved:
            log.info(
                "位置变化 [%s] (%d,%d) → (%d,%d)",
                reason, self._window_local.x, self._window_local.y, rect.x, rect.y,
            )
        ns_rect = self._space().to_ns_rect(rect)
        if reason == "drag" and abs(rect.width - self._window_local.width) < 0.5 and abs(
            rect.height - self._window_local.height
        ) < 0.5:
            # 拖动只是**搬家**（尺寸不变）：走 setFrameOrigin 这条便宜的路，不重绘、
            # 也不触发尺寸重算。setFrame:display: 每次都是一整张软件光栅化 —— 拖拽态圈速
            # 被抬到设备速率（见 _DRAG_MAX_SLEEP）后，它会变成每秒几百次。
            # 内容的重绘由 :meth:`_paint` 的帧预算闸负责。
            self._window.setFrameOrigin_(ns_rect.origin)
        else:
            self._window.setFrame_display_(ns_rect, True)
        self._window_local = rect

    # —— 每个动画节拍：命中判定 + 收起/展开 ————————————————————————————————

    def _pump(self, seconds: float) -> None:
        """在自己的事件循环里待 ``seconds`` 秒：处理输入事件、推进动画、判定贴边。

        限速**只能**靠下面那句显式的 ``time.sleep()``，别指望 ``untilDate`` 帮你阻塞：
        本进程是没调用过 ``NSApp.run()`` 的 accessory 应用，
        ``nextEventMatchingMask_untilDate_inMode_dequeue_`` 在这种进程里**不按 untilDate 等待** ——
        队列里没有就绪事件就立刻返回 ``None``。那样 while 会以 CPU 极限速度空转，每圈还白跑一遍
        ``_poll()``（多次 ObjC 调用）+ ``displayIfNeeded()``（同步整窗重绘）：实测常驻吃满一个核
        （``%cpu`` 98~99%），而且把 fps 从 30 调到 10 毫无变化 —— 因为 fps 只改传给 untilDate 的
        间隔，压根不是限速器。所以每圈画完，把 ``1/fps`` 帧预算里没用完的余量睡掉：每秒圈数被
        硬性限在 fps 以内，CPU 占用只跟「画了多少」挂钩，不跟「CPU 有多快」挂钩。**别退回忙等。**
        """
        app = NSApplication.sharedApplication()
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            frame_start = time.monotonic()
            # 事件只取**已经就绪**的：untilDate 传当前时刻 = 立即返回，不指望它阻塞。
            # 一圈里把就绪事件取干净，是因为拖拽时鼠标事件成串到达，一次只取一个会跟不上手。
            for _ in range(_MAX_EVENTS_PER_LOOP):
                event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    NSEventMaskAny,
                    NSDate.date(),
                    NSDefaultRunLoopMode,
                    True,
                )
                if event is None:
                    break
                app.sendEvent_(event)
            self._drag_watchdog()
            # 设置重读：只 stat mtime，变了才读盘（别每圈读文件 —— 空闲 <5% 那条线）
            if time.monotonic() - self._settings_checked_at >= _SETTINGS_POLL_S:
                self._settings_checked_at = time.monotonic()
                self._reload_settings()
            # 气泡出现/淡化/消失都要重画（TTL 与淡化进度都在指纹里，到点那一刻得有人推一把）
            self._expire_badge()
            badge_dirty = self._badge_dirty
            self._badge_dirty = False
            changed = self._poll() or badge_dirty
            self._paint(force=changed)
            self._count_loop()
            # 漂移自检每秒最多一次（别每帧拿 frame() 去问窗口服务器）
            now = time.monotonic()
            if now - self._last_drift_check >= _DRIFT_CHECK_PERIOD:
                self._last_drift_check = now
                self._check_drift()
                self._write_probe()  # 同一拍落盘，别为诊断多加定时器
            # 帧预算的余量睡掉（不超过本次 _pump 的截止时间）—— 这一句就是限速器。
            # 拖拽态换用 _DRAG_MAX_SLEEP：**排空事件队列的节奏就是跟手的节奏**，
            # 睡满 1/fps 就等于把窗口位置更新压到 fps 次/秒（部署形态实测 18.7 次/秒，
            # 手上是台阶感）。每圈现算，因为拖动可能在这一圈中间开始或结束。
            budget = _DRAG_MAX_SLEEP if self._dragging else (1.0 / self.fps)
            nap = min(budget - (time.monotonic() - frame_start), deadline - time.monotonic())
            slept = 0.0
            if nap > 0.0:
                before = time.monotonic()
                time.sleep(nap)
                slept = time.monotonic() - before
            self._pump_sleep_ms = round(slept * 1000.0, 1)

    def _count_loop(self) -> None:
        """累计事件循环的圈数，满一秒结算一次 —— 对外报的是上一秒的完整计数，不是瞬时值。"""
        self._pump_loops += 1
        now = time.monotonic()
        elapsed = now - self._pump_window_start
        if elapsed >= 1.0:
            self._pump_loops_per_sec = round(self._pump_loops / elapsed, 1)
            self._pump_loops = 0
            self._pump_window_start = now
            self._paints_per_sec = round(self._paints / elapsed, 1)
            self._paints = 0
            self._paints_window_start = now

    def _paint(self, force: bool = False) -> None:
        """重画一帧。``displayIfNeeded()`` 是同步整窗重绘，画面没变就别白画。

        拖拽态另加一道**帧预算闸**：那时事件循环被抬到设备速率（见 ``_DRAG_MAX_SLEEP``），
        而窗口搬家**不需要**重画内容（背板跟着窗口走，见 :meth:`_set_window_rect`）——
        每圈都重画会把软件光栅化的次数从 15 次/秒抬到几百次/秒，正好把跟手省下的又烧回去。
        所以：指纹一样就跳过；指纹变了（姿势/呼吸动画）也最多 ``1/fps`` 重画一次。
        """
        if self._view is None or self._window is None:
            return
        now = time.monotonic()
        fingerprint: tuple[Any, ...] | None = None
        if not force:
            if self._dragging:
                # 拖拽态只看帧预算：位置每圈都在变 ⇒ 指纹必然变，算它纯浪费
                # （圈速被抬到 200+/s 时这笔开销要付 200 次）
                if (now - self._last_paint_at) < (1.0 / self.fps):
                    return
            else:
                fingerprint = self._fingerprint()
                if fingerprint == self._last_fingerprint:
                    return
        if fingerprint is None:
            fingerprint = self._fingerprint()
        self._last_paint_at = now
        self._last_fingerprint = fingerprint
        self._view.setNeedsDisplay_(True)
        self._window.displayIfNeeded()
        self._paints += 1  # 真画下去了才计数（被指纹闸跳过的那几次不算）

    def _fingerprint(self) -> tuple[Any, ...] | None:
        """「这一帧真要画什么」的指纹：指纹一致 = 画面一致 = 可以跳过重绘。

        覆盖所有会改变像素的输入 —— 状态、文案、贴边状态、是否显示把手、光标是否在热区、
        窗口几何，以及动画量（展开时是姿势，收起时是把手呼吸的相位；浮点一律四舍五入到
        ``_POSE_PRECISION`` 位再比，既吃得掉无意义的抖动，又不会把动画帧整帧跳过去）。
        """
        frame = self._frame
        character = self._character
        if frame is None or character is None:
            return None
        rect = self._window_local
        seconds = time.monotonic() - self._started
        handle = self._handle_shown
        animation: Any
        if handle:
            # 收起时画的是把手脉冲：按周期量化相位，呼吸照常一帧不落
            animation = round(seconds % _HANDLE_PULSE_PERIOD, _POSE_PRECISION)
        else:
            animation = _quantize_pose(wl.pose_for(character.spec(frame.state).motion, seconds))
        return (
            frame.state,
            frame.caption,
            # 气泡：出现/换字/消失要重画，**淡化进度也必须进指纹** —— 只带文字的话，淡化中
            # 文字一个字不变 ⇒ 指纹不变 ⇒ `_paint` 直接 return ⇒ 气泡卡在第一帧透明度上、
            # 5 秒后硬切消失，正好是用户要避免的那种「啪一下没了」。alpha 量化到两位小数
            # （同 animation 那条 round(..., _POSE_PRECISION) 的做法），既能逐帧变、又吃得掉抖动。
            self._badge_signature() if self._badge_active() else "",
            self._dock.state,
            self._dock.edge,
            handle,
            self._cursor_hot,
            (round(rect.x, 1), round(rect.y, 1), round(rect.width, 1), round(rect.height, 1)),
            animation,
        )

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
        # 收起时把手条是居中在**本体**上的，而 docked_rect 居中在**窗口**上 ——
        # 两者差半个文案带（左/右两条边）。不换算的话每次收/展都往上漂 13px。
        center = strip.center.y if edge in (wl.Edge.LEFT, wl.Edge.RIGHT) else strip.center.x
        self._set_window_rect(
            wl.docked_rect(
                edge,
                self._space().screen,
                self._window_size(),
                center=wl.body_center_to_window_center(center, edge),
            ),
            reason="expand",
        )

    def _poll(self) -> bool:
        """命中判定 + 收起/展开。返回**这一圈是否有变化**（变了就得强制重画一帧）。"""
        if self._window is None or self._dragging:
            return False
        cursor = self._cursor()
        hot = self._is_hot(cursor)
        changed = hot != self._cursor_hot
        self._cursor_hot = hot
        if bool(self._window.ignoresMouseEvents()) == hot:  # 只在需要时戳 ObjC
            self._window.setIgnoresMouseEvents_(not hot)
        action = self._dock.update(cursor, self._window_local)
        if action is wl.DockAction.COLLAPSE:
            assert self._character is not None
            body = self._body_in_screen()
            self._set_window_rect(
                wl.collapsed_rect(self._dock.edge, self._space().screen, body), reason="collapse"
            )
            log.debug("贴边收起：%s", self._dock.edge)
            changed = True
        elif action is wl.DockAction.EXPAND:
            self._expand_from_edge()
            log.debug("贴边展开：%s", self._dock.edge)
            changed = True
        return changed

    # —— 鼠标：拖拽就位 / 松手贴边 ——————————————————————————————————————————

    def _mouse_screen_point(self, event: Any) -> wl.Point:
        """鼠标事件 → **屏幕**坐标（左上原点，和 :mod:`window_layout` 同一口径）。

        拖动的位移基准**必须**是屏幕口径。这里用「事件局部坐标 + **当前**窗口原点」换算，
        而不是裸 ``NSEvent.mouseLocation()``：两者代数上等价，但这一路**假事件也能驱动**，
        回归脚本（``scripts/verify_drag_mouse.py``）因此能覆盖真机这条路径。

        为什么不能拿局部坐标当基准（2026-09-29 用户报「拖动卡顿」的真因）：``locationInWindow``
        是相对**当前** frame 算的，窗口一动它就反向平移，于是
        ``target = 按下时窗口原点 + (本帧局部坐标 − 按下时局部坐标)``
        解出来是 ``u_k = C − u_{k−1}`` —— DC 增益 ½、极点在 −1：
        跟手只有半速且滞后随已拖距离线性累加，鼠标停住也永远静不下来（真机日志里
        88% 的相邻位移方向相反，窗口在两三个位置间来回翻）。
        """
        point = event.locationInWindow()  # Cocoa 窗口坐标（原点在左下）
        origin = self._window_local
        return wl.Point(origin.x + point.x, origin.y + self._view_height() - point.y)

    def _mouse_button_down(self) -> bool:
        """左键是否仍按着 —— 只给拖拽兜底用。

        单独一个方法是为了**可注入**：假事件回归（``scripts/verify_drag_mouse.py``）里没有真
        鼠标，得能换成「一直按着」，否则 80ms 后兜底会把它的仿真拖动收掉。
        """
        return bool(NSEvent.pressedMouseButtons() & 1)

    def _drag_watchdog(self) -> None:
        """拖拽兜底：按钮全松开却还在 ``_dragging`` ⇒ 那次 ``mouseUp`` 丢了，自己收尾。

        见 ``_DRAG_BUTTON_UP_GRACE``：不这么做，一次丢失的 mouseUp 会让事件循环以
        ~250 圈/s 空转、且 ``_poll``（悬停/贴边/锚点写入）一直不跑，直到重启。
        """
        if not self._dragging or not self._drag_from_mouse or self._mouse_button_down():
            self._buttons_up_since = None
            return
        now = time.monotonic()
        if self._buttons_up_since is None:
            self._buttons_up_since = now
            return
        if now - self._buttons_up_since < _DRAG_BUTTON_UP_GRACE:
            return
        waited = now - self._buttons_up_since
        self._buttons_up_since = None
        log.warning(
            "拖拽中鼠标键已松开 %.0fms 但没收到 mouseUp —— 主动收尾（免得拖拽圈速一直空转）",
            waited * 1000,
        )
        self.end_drag()

    def _mouse_down(self, event: Any) -> None:
        self.start_drag(from_mouse=True)  # 真按钮按着 ⇒ 拖拽兜底对它生效
        point = self._mouse_screen_point(event)
        # 按下时指针在窗口内的位置（屏幕口径的偏移量），拖动期间保持不变
        self._drag_offset = (point.x - self._window_local.x, point.y - self._window_local.y)
        # 记下起点，好在松手时分辨「点一下」还是「拖一把」
        self._press_at = time.monotonic()
        self._press_point = point
        self._press_moved = False
        self._press_max_moved = 0.0
        # 边界行（INFO）：日志里的按压必须能机器切段。以前边界只有 debug 级的
        # 收尾行 ⇒ INFO 采集永远看不到，解析器只能靠坐标猜（同一份日志被数成
        # 227/245/249、68 段 ≤4px 只能人眼对，都是这个根因）。
        log.info("按下：屏幕=(%.0f,%.0f) 按钮=左", point.x, point.y)

    def _mouse_dragged(self, event: Any) -> None:
        if not self._dragging:
            return
        point = self._mouse_screen_point(event)
        if self._press_point is not None:
            self._press_max_moved = max(
                self._press_max_moved,
                max(abs(point.x - self._press_point.x), abs(point.y - self._press_point.y)),
            )
            if not self._press_moved:
                far = abs(point.x - self._press_point.x) > _TAP_SLOP or abs(
                    point.y - self._press_point.y
                ) > _TAP_SLOP
                if far:
                    self._press_moved = True
        self.move_window_to(point.x - self._drag_offset[0], point.y - self._drag_offset[1])

    def _mouse_up(self, event: Any = None) -> None:
        """松手：先判这次是不是「点击」，再决定要不要按拖拽收尾。

        三种手势的语义（2026-09-29 定，@researcher 的时序论证 + @ops 的三条风险都采纳）：

        * **单击** = 按设置显示（默认额度条）—— **立刻响应，不为了等双击而延迟**：
          用户刚抱怨过卡，再叠半秒很亏。
        * **双击**（两次点击间隔 ≤ 系统双击间隔） = 收起刚弹出的额度条 + 开控制面板。
          第一下已经出过额度条了，第二下把它收掉，所以双击不会留下一闪的残影。
        * **按住 > ``_TAP_MAX_HOLD_S``** = 不算点击（拖动仍然是拖动）。

        判定输入（位移/按住多久/是否双击）一律进日志 —— 这条路径以前只有「结果」，
        @ops 用合成事件测不进部署实例的按键，只能靠日志反推真实点击分布。
        """
        if not self._dragging:
            return
        now = time.monotonic()
        held_ms = None if self._press_at is None else (now - self._press_at) * 1000.0
        tap = (not self._press_moved) and held_ms is not None and held_ms / 1000.0 <= _TAP_MAX_HOLD_S
        moved_px = self._press_max_moved
        self._press_at = None
        self._press_point = None
        # 对称的边界行（INFO）：**拖动那一支也要有** —— 恰恰是「本想点、手抖到 4~15px
        # 被判成拖动」的那批（约 13% 的按压）以前一行都不产，「位移=」字段只活在
        # tap 分支里 ⇒ 按构造成全 ≤3px，拿它定 _TAP_SLOP 是幸存者偏差。
        # 判 _TAP_SLOP 要看的量是 `_press_max_moved`（按下以来**最大**位移），不是单步。
        log.info(
            "按下收尾：判定=%s 位移=%.0fpx held=%.0fms",
            "tap" if tap else "drag",
            moved_px,
            held_ms or 0.0,
        )
        if tap:
            # **点击不许改几何**（用户 2026-09-29 报的 bug：单击桌宠它会自己收回去）。
            # 以前这里无条件走 end_drag() ⇒ dock.drop() ⇒ 桌宠本来就在边上，
            # 「按下即松手」被判成「扔到边上」⇒ 立刻收成 12px 把手条，气泡还画在那条 12px 里
            # （19:10:56 的日志就是这条链：expand → collapse → 点击 → 气泡 → expand）。
            # 点击只收拖拽态：不 drop、不改锚点、不动窗口。按下那一下已经
            # `_expand_from_edge()` 展开过了，所以收起态点一下也能看到完整的角色 + 气泡。
            self._dragging = False
            log.debug("点击：只收拖拽态（贴边状态保持 %s）", self._dock.state)
        else:
            edge = self.end_drag()
            log.info("拖拽结束：落边=%s", edge)
            return
        interval = self._double_click_interval()
        double = (now - self._last_click_at) <= interval
        self._last_click_at = now
        action = "双击" if double else "单击"
        log.info(
            "点击桌宠：%s held=%.0fms 位移=%.0fpx 双击间隔=%.0fms ⇒ %s",
            action,
            held_ms or 0.0,
            moved_px,
            interval * 1000.0,
            "收起额度条+开面板" if double else f"按设置执行 click_action={self._click_action()!r}",
        )
        if double:
            self._hide_badge()
            self._open_panel()
        else:
            self._do_click_action()

    def _double_click_interval(self) -> float:
        """系统的双击间隔（这台机器实测 0.5s）。取不到就用 0.5 —— 不写死在逻辑里。"""
        try:
            value = float(NSEvent.doubleClickInterval())
        except Exception:  # noqa: BLE001 - 取不到就用系统默认
            return 0.5
        return value if 0.1 <= value <= 2.0 else 0.5

    def _click_action(self) -> str:
        return str(self._settings.get("click_action") or "badge")

    def _do_click_action(self) -> None:
        """单击按设置执行：``badge`` 额度 / ``device`` 电脑状态 / ``all`` 两样 / ``none`` 不显示。

        这是**唯一**的破例入口（单击不挂起等双击，2026-09-29 用户定的语义）—— 所以这里必须
        「立刻就有东西出来」，不许先做别的再显示。
        """
        # 别名（旧名 caption ⇒ device）在 `settings` 的**读入规范化**里已经做掉了 ⇒ 这里拿到的
        # 一定是新名，别再抄一份别名表（照抄一份就是第三处副本，加档时必漏）。
        action = self._click_action()
        if action == "none":
            self._hide_badge()
            log.debug("单击：设置是「不显示」，什么都不做")
            return
        # 设备数在**点击这一刻**采一次就冻结：气泡是每帧重画的（5 秒 ≈75 帧），把 get() 挪进
        # 绘制路径就会每 2 秒（TTL）在主线程掉一帧，而且数字跳变还会让气泡重新拆行 ⇒ 看起来在抖。
        #
        # 但**不许在这里等**：CPU 要 1s 的 tick 窗口，等它就是把「单击立刻有东西出来」变成
        # 「点一下卡一秒」（面板为完全相同的原因已经改成首帧不等）。基线没攒够就先不等、写
        # 「CPU 采集中」，攒够了由 :meth:`_refill_bubble_device` 填进仍显示着的气泡。
        device = None
        pending = False
        if action in ("device", "all"):
            pending = self._device.wait_remaining() > 0.0
            device = self._device.get(wait=not pending)
        self._bubble_action = action
        self._show_badge(lines=self._bubble_lines(action, device, device_pending=pending))
        # 真机上几乎够不到（启动→首次交互最短 6s，@ops 全天 10 次样本），但**回归脚本会在重启后
        # 1 秒内就点**（同一个采样窗口）——所以这一路的字面也得对：排一次补数。
        self._bubble_refill_at = (
            time.monotonic() + self._device.wait_remaining() + 0.15 if pending else None
        )

    def _show_badge(self, lines: Sequence[str] | None = None) -> None:
        """弹一枚 5 秒的对话气泡（默认额度档）。不碰引擎、不改 state。

        行数由 :func:`xiaocc.quota.badge.bubble_candidates` 按档位出候选、再按气泡可用宽度挑
        （最长那句放不下就退到更短的一条），所以气泡里不会出现「第一行撑满、第二行只剩两个字」。
        """
        if lines is None:
            lines = self._quota_badge_lines()
        picked = [str(line) for line in lines if str(line).strip()][:2]
        if not picked:
            return
        self._badge_lines = picked
        self._badge_text = " / ".join(picked)
        self._badge_until = time.monotonic() + _BADGE_TTL_S
        self._badge_dirty = True
        log.info(
            "气泡：%s（%gs 后开始淡化，共 %.1fs）",
            self._badge_text,
            _BADGE_TTL_S - _BADGE_FADE_S,
            _BADGE_TTL_S,
        )

    def _hide_badge(self) -> None:
        self._bubble_refill_at = None
        if self._badge_lines or self._badge_drawn:
            self._badge_lines = []
            self._badge_text = ""
            self._badge_until = 0.0
            self._badge_dirty = True

    def _expire_badge(self) -> None:
        """淡完了就把状态收干净（否则 ``_badge_text`` 留着，指纹会永远比「安静态」多一项）。"""
        if self._badge_lines and not self._badge_active():
            self._badge_lines = []
            self._badge_text = ""
            self._badge_dirty = True

    def _quota_badge_lines(self) -> list[str]:
        """额度档那两行（兼容入口：菜单/回归脚本在用的老名字）。"""
        return self._bubble_lines("badge")

    def _refill_bubble_device(self) -> None:
        """基线攒够后把设备数填进**仍显示着**的气泡（气泡每帧重画，改 ``_badge_lines`` 就会重绘）。

        只做一次（``_bubble_refill_at`` 立刻清掉），且只在气泡还在屏上时动手；TTL 不重置，
        所以「5 秒后淡出」的总时长不变。
        """
        self._bubble_refill_at = None
        if not self._badge_active():
            return
        lines = self._bubble_lines(self._bubble_action, self._device.get())
        picked = [str(line) for line in lines if str(line).strip()][:2]
        if picked and picked != self._badge_lines:
            self._badge_lines = picked
            self._badge_text = " / ".join(picked)
            self._badge_dirty = True
            log.info("气泡补数：%s", self._badge_text)

    def _bubble_lines(
        self, action: str, device: Any = None, *, device_pending: bool = False
    ) -> list[str]:
        """气泡里那两行：按档位从 ``quota.badge`` 的候选里挑**每行都放得下**的最长一条。

        读一次 ``quota.json``（本地小文件，点击才读）。``device`` 由调用方在**点击那一刻**采好
        传进来（冻结，见 :meth:`_do_click_action`）。
        """
        try:
            # 路径在**调用时**解析（不是 import 时），回归脚本才能用 XIAOCC_QUOTA_FILE 指到假报告上
            report, meta = load_quota_report(default_quota_path())
        except Exception as exc:  # noqa: BLE001 - 读不到就如实说「未采集」
            log.warning("读额度失败：%s", exc)
            report, meta = None, None
            if action in ("badge", "all"):  # 这两档额度是主菜，读不到就直说
                return ["额度未采集", "点开面板看详情"]
        candidates = bubble_candidates(
            action, report=report, meta=meta, device=device, device_pending=device_pending
        )
        width = self._bubble_text_width()
        for candidate in candidates:
            if candidate and all(self._text_fits(line, width) for line in candidate):
                return list(candidate)
        return list(candidates[-1]) if candidates else []

    @staticmethod
    def _text_fits(text: str, width: float) -> bool:
        try:
            attributes = {NSFontAttributeName: NSFont.systemFontOfSize_(11.0)}
            return float(NSString.stringWithString_(text).sizeWithAttributes_(attributes).width) <= width
        except Exception:  # noqa: BLE001 - 量不出来就当放得下（宁可截断也不要空着）
            return True

    def _reload_settings(self, *, force: bool = False) -> None:
        """设置文件变了就重读（只 stat mtime，没变不读盘）。"""
        path = settings_store.settings_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._settings_mtime:
            return
        self._settings_mtime = mtime
        before = self._click_action()
        self._settings = settings_store.load(path)
        after = self._click_action()
        if force or before != after:
            log.info("设置：click_action=%s（%s）", after, path)

    def _right_mouse_down(self, event: Any, view: Any = None) -> None:
        """右键 = 弹菜单。**菜单里只有一条「打开控制面板」**（用户 2026-09-29 的指令：不要菜单里
        的「显示额度」—— 单击就能看到；设置也并进面板了，所以「设置…」那条也没了）。

        **必须早退**：右键不进 :meth:`_mouse_down` 那条 tap/drag 判定，否则右键会顺手把桌宠
        拖走或触发贴边收展（@ops 点出来的那条）。菜单里不做「重启/退出」—— 那个要跟 launchd
        的 KeepAlive 策略一起定（现在配的是「只在非正常退出时拉起」），没定清楚之前不摆进去
        （@ops：`KeepAlive{SuccessfulExit:false}` 下「退出」是干净退出 0 ⇒ launchd 不会拉回来，
        用户以为退出、其实永久关掉；「重启」得走返回式收尾 + `execv`，`NSApp.terminate_` 不返回）。
        """
        menu = self._build_menu()
        location = event.locationInWindow()
        if view is not None:
            menu.popUpMenuPositioningItem_atLocation_inView_(None, location, view)
        log.info("右键菜单已弹出（%s 条）", menu.numberOfItems())

    def _build_menu(self) -> Any:
        """把右键菜单搭出来（单独一个方法，判据才能数条目、而不用真去点模态菜单）。

        用户 2026-09-29 要求「右键小cc显示设备状态」⇒ 上半是设备数据，下半是动作。
        数字**每次右键现采**（:class:`~xiaocc.device.Sampler`，TTL 缓存 2s）——
        菜单里放陈旧数字比不放更糟。
        """
        menu = NSMenu.alloc().init()
        if self._menu_target is None:
            self._menu_target = _MenuTarget.alloc().initWithBackend_(self)
        for title, value in self._device_rows():
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"{title}  {value}", b"noop:", ""
            )
            item.setTarget_(self._menu_target)
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("打开控制面板", b"openPanel:", "")
        item.setTarget_(self._menu_target)
        menu.addItem_(item)
        # 用户 2026-09-29 要求：菜单里要能**退出**和**重启**。
        # 用词写全名（「重启小cc」不是「重启」）：菜单是两步操作（开菜单 + 点），不再加二次确认，
        # 但名字必须让人一眼知道动的是谁。
        menu.addItem_(NSMenuItem.separatorItem())
        for title, selector in (("重启小cc", b"restartPet:"), ("退出小cc", b"quitPet:")):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
            item.setTarget_(self._menu_target)
            menu.addItem_(item)
        return menu

    def _device_rows(self) -> list[tuple[str, str]]:
        """设备状态行（CPU / 内存 / 磁盘 / 电池 / 已开机）。采不到写「未取到」，不补数。

        采样放在**右键那一刻**：这里不在每帧的循环里，采样器自己还有 TTL 缓存，
        所以常态下右键一次只多花一次 ``vm_stat`` + ``pmset``（~20ms）。
        """
        try:
            return self._device.get().lines()
        except Exception:  # 采不到也不许把右键菜单带走
            log.exception("设备状态采样失败")
            return [("设备状态", "未取到")]

    def _restart_pet(self) -> None:
        """重启小cc（右键菜单）：派一个独立会话去跑 ``xiaoccctl restart``。

        为什么**不自己先退**：那脚本会 ``bootout`` 掉我们这个作业（我们收 SIGTERM），走 cli 的收尾
        比"先自己 os._exit"干净；脚本在新会话里，我们死了它也照跑。
        """
        self._control_pet("restart")

    def _quit_pet(self) -> None:
        """退出小cc（右键菜单）：先请面板收窗，再派 ``xiaoccctl stop`` 把作业 bootout 掉。

        面板是**独立进程**（桌宠只是写请求文件拉它起来），我们不在了它也不会自己走 ⇒ 一起收。
        """
        try:
            from ..panel.paths import request_close

            request_close()
        except Exception as exc:  # noqa: BLE001 - 面板收不收得了不该挡住「退出」本身
            log.debug("请面板收窗失败（不影响退出）：%s", exc)
        self._control_pet("quit")

    def _control_pet(self, action: str) -> None:
        """退出/重启的公共入口（失败只记日志 —— 菜单点一下不该把桌宠带崩）。"""
        try:
            from .. import control

            ok, detail = control.perform_detached(action)
        except Exception as exc:  # noqa: BLE001
            log.warning("控制：%s 失败：%s", action, exc)
            return
        log.info("菜单：%s ⇒ %s（%s）", action, "已派出" if ok else "派不出去", detail)

    def _open_panel(self) -> None:
        """打开控制面板；已经开着就刷新并抬到前面。

        面板是**独立进程**（见 :mod:`xiaocc.panel`）：这里只写一个 request 文件，不 import
        AppKit 之外的东西、不阻塞事件循环；失败也只记一行日志，绝不让桌宠跟着出事。
        """
        try:
            from ..panel.paths import request_open

            result = request_open()
        except Exception as exc:  # noqa: BLE001 - 打不开面板不该影响桌宠本体
            log.warning("打开控制面板失败：%s", exc)
            return
        log.info("打开控制面板：%s", result)

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
            if self._bubble_refill_at is not None and time.monotonic() >= self._bubble_refill_at:
                self._refill_bubble_device()  # 基线攒够了：把设备数填进仍显示着的气泡
            if self._badge_active():
                self._last_caption_drawn = ""  # 这一帧画的是额度条，文案没上屏
                self._draw_badge(accent)
            else:
                # 没在画额度条 ⇒ 自证据必须清空，否则会留着上一轮那行字（判据会被它骗过）
                self._badge_drawn = ""
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
        pulse = 0.55 + 0.25 * (1.0 + math.sin(2 * math.pi * t / _HANDLE_PULSE_PERIOD)) / 2.0
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

    def _badge_signature(self) -> tuple[str, float]:
        """指纹里那一条：文字 + **量化后的 alpha**（淡化进度不进去，气泡就会卡在第一帧透明度）。"""
        return ("\n".join(self._badge_lines), round(self._badge_alpha(), 2))

    def _badge_alpha(self) -> float:
        """当前不透明度：前 ``TTL - FADE`` 秒是 1.0，最后一小段线性淡到 0.0。"""
        remaining = self._badge_until - time.monotonic()
        if remaining <= 0.0:
            return 0.0
        if remaining >= _BADGE_FADE_S:
            return 1.0
        return round(remaining / _BADGE_FADE_S, 3)

    def _badge_active(self) -> bool:
        """气泡此刻该不该占着画面（TTL 到了、淡完了就自己退出，不用定时器）。"""
        return bool(self._badge_lines) and self._badge_alpha() > 0.0

    def _bubble_text_width(self) -> float:
        """气泡里每行文字可用的宽度（量字用）。**算法只有一处**，见 :func:`bubble_text_width_for`。"""
        return bubble_text_width_for(self._window_local.width)

    def _draw_badge(self, accent: str) -> None:
        """单击小cc 时贴在角色下方的那枚**对话气泡**（额度 / 设备状态 / 额度+设备）。

        **走自己的通道**：只写 ``badge_drawn``，一个字节都不碰 ``caption_drawn`` ——
        后者被 ``appkit_screenshots.py`` 的「待机时不挂文案」断言守着，拿它去挂气泡等于把一条
        早验过的守卫悄悄废掉（同 MATCH / --linger 那两次的形状）。

        气泡画在**窗口内**（底部那块文案带的位置）、**不改窗口尺寸**：长高窗口会碰到漂移自检
        「除用户拖动外任何位移都算 bug ⇒ 回锚 + 留痕」和 ``anchor_ok`` 那套判据。尖角朝上指向角色，
        所以它整枚都跟着角色走（拖到哪气泡跟到哪，不会留在原地）。
        """
        self._badge_drawn = ""
        if not self._badge_lines:
            return
        alpha = self._badge_alpha()
        if alpha <= 0.0:
            return
        width = self._window_local.width - _BUBBLE_PAD_X * 2.0
        height = _BUBBLE_BODY_H + _BUBBLE_TAIL_H
        rect = wl.Rect(
            _BUBBLE_PAD_X,
            self._window_local.height - height - _BUBBLE_BOTTOM,
            width,
            height,
        )
        if self._draw_bubble(self._badge_lines, accent, rect, alpha):
            self._badge_drawn = " / ".join(self._badge_lines)

    @staticmethod
    def _bubble_path(width: float, height: float) -> Any:
        """气泡轮廓：**圆角矩形与尖角一笔成形**（所以描边会绕过尖角，不会在尖角根部横一道线）。

        分两笔画的版本在深色桌面上看着像「没有尾巴的方块」—— 尖角的两个斜面没有描边、只靠
        填色跟底色区分（实测在深色壁纸上几乎看不出来）。这里按上边 → 尖角 → 上边 → 圆角 →
        下边 → 圆角 的顺序串一条闭合路径，描边自然把尖角勾出来。
        """
        radius = 10.0
        half = 7.0
        top = height - _BUBBLE_TAIL_H
        apex = width / 2.0
        path = NSBezierPath.bezierPath()
        path.moveToPoint_(NSMakePoint(radius, top))
        path.lineToPoint_(NSMakePoint(apex - half, top))
        path.lineToPoint_(NSMakePoint(apex, height - 0.5))
        path.lineToPoint_(NSMakePoint(apex + half, top))
        path.lineToPoint_(NSMakePoint(width - radius, top))
        path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(width - radius, top - radius), radius, 90.0, 0.0
        )
        path.lineToPoint_(NSMakePoint(width, radius))
        path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(width - radius, radius), radius, 0.0, -90.0
        )
        path.lineToPoint_(NSMakePoint(radius, 0.0))
        path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(radius, radius), radius, 270.0, 180.0
        )
        path.lineToPoint_(NSMakePoint(0.0, top - radius))
        path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(radius, top - radius), radius, 180.0, 90.0
        )
        path.closePath()
        path.setLineWidth_(1.0)
        return path

    def _draw_bubble(self, lines: list[str], accent: str, rect: wl.Rect, alpha: float) -> bool:
        """圆角气泡 + 朝上尖角 + 两行居中文字。点阵化缓存（同 :meth:`_draw_caption_text` 那笔账），
        淡化只是取缓存再按 alpha 贴一次 —— 每帧不重新光栅化。"""
        width, height = rect.width, rect.height
        key = ("bubble", tuple(lines), accent, round(width, 1), round(height, 1))
        image = self._bitmaps.get(key)
        if image is None:
            paragraph = NSMutableParagraphStyle.alloc().init()
            paragraph.setAlignment_(NSTextAlignmentCenter)
            paragraph.setLineBreakMode_(NSLineBreakByTruncatingTail)
            attributes = {
                NSFontAttributeName: NSFont.systemFontOfSize_(11.0),
                NSForegroundColorAttributeName: self._color("#FFFFFF", 1.0),
                NSParagraphStyleAttributeName: paragraph,
            }
            labels = [NSString.stringWithString_(line) for line in lines]

            def paint(w: float, h: float, labels: Any = labels, attributes: Any = attributes) -> None:
                body_h = h - _BUBBLE_TAIL_H
                shape = self._bubble_path(w, h)
                self._color("#2B2E3A", 0.9).setFill()
                shape.fill()
                self._color(accent, 0.9).setStroke()
                shape.stroke()
                if len(labels) == 1:
                    rows = [(body_h - _BUBBLE_LINE_H) / 2.0 + 1.0]
                else:
                    rows = [body_h - _BUBBLE_LINE_H - 4.0, 4.0]
                for label, y in zip(labels, rows):
                    label.drawInRect_withAttributes_(
                        NSMakeRect(
                            _BUBBLE_TEXT_INSET,
                            y,
                            w - _BUBBLE_TEXT_INSET * 2.0,
                            _BUBBLE_LINE_H,
                        ),
                        attributes,
                    )

            image = appkit_art.pointize((width, height), paint)
            if image is None:  # 点阵化不可用：直接画，宁可不淡化也不要不显示
                self._draw_bubble_fallback(lines, accent, rect, alpha)
                return True
            self._bitmaps.put(key, image)
        image.drawInRect_fromRect_operation_fraction_(
            self._local(rect), NSZeroRect, NSCompositingOperationSourceOver, alpha
        )
        return True

    def _draw_bubble_fallback(
        self, lines: list[str], accent: str, rect: wl.Rect, alpha: float
    ) -> None:
        """不走点阵化时的直画版本（只在光栅缓存不可用时用到；位置/字号与缓存版一致）。"""
        local = self._local(rect)
        body_h = rect.height - _BUBBLE_TAIL_H
        shape = self._bubble_path(rect.width, rect.height)
        shift = NSAffineTransform.transform()
        shift.translateXBy_yBy_(local.x, local.y)
        shape.transformUsingAffineTransform_(shift)
        self._color("#2B2E3A", 0.9 * alpha).setFill()
        shape.fill()
        self._color(accent, 0.9 * alpha).setStroke()
        shape.stroke()
        paragraph = NSMutableParagraphStyle.alloc().init()
        paragraph.setAlignment_(NSTextAlignmentCenter)
        paragraph.setLineBreakMode_(NSLineBreakByTruncatingTail)
        attributes = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11.0),
            NSForegroundColorAttributeName: self._color("#FFFFFF", alpha),
            NSParagraphStyleAttributeName: paragraph,
        }
        rows = (
            [(body_h - _BUBBLE_LINE_H) / 2.0 + 1.0]
            if len(lines) == 1
            else [body_h - _BUBBLE_LINE_H - 4.0, 4.0]
        )
        for line, y in zip(lines, rows):
            NSString.stringWithString_(line).drawInRect_withAttributes_(
                NSMakeRect(local.x + 8.0, local.y + _BUBBLE_TAIL_H + y, rect.width - 16.0, 15.0),
                attributes,
            )

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
        self._draw_caption_text(text, accent, band)

    def _draw_caption_text(self, text: str, accent: str, band: wl.Rect) -> None:
        """把文案点阵化后贴上去（key 含文案与配色，所以只有换状态时才重建）。

        168µs/帧的那笔钱几乎全在建字体/段落/属性字典和重新排版上；文案一秒变一次都算勤的，
        没有理由每帧重排。
        """
        width, height = band.width, band.height
        key = ("caption", text, accent, round(width, 1), round(height, 1))
        image = self._bitmaps.get(key)
        if image is None:
            paragraph = NSMutableParagraphStyle.alloc().init()
            paragraph.setAlignment_(NSTextAlignmentCenter)
            paragraph.setLineBreakMode_(NSLineBreakByTruncatingTail)
            attributes = {
                NSFontAttributeName: NSFont.systemFontOfSize_(11.5),
                NSForegroundColorAttributeName: self._color(accent, 0.95),
                NSParagraphStyleAttributeName: paragraph,
            }
            label = NSString.stringWithString_(text)

            def paint(w: float, h: float, label: Any = label, attributes: Any = attributes) -> None:
                label.drawInRect_withAttributes_(NSMakeRect(0.0, 0.0, w, h), attributes)

            image = appkit_art.pointize((width, height), paint)
            if image is None:
                # 点阵化失败：退回每帧现画（慢，但至少不显示不出东西）
                NSString.stringWithString_(text).drawInRect_withAttributes_(self._local(band), attributes)
                return
            self._bitmaps.put(key, image)
        image.drawInRect_fromRect_operation_fraction_(
            self._local(band), NSZeroRect, NSCompositingOperationSourceOver, 1.0
        )

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
        self._last_art_key = key
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
        # 每帧都走这里：先看有没有点阵化过的那一份（同一张图 + 同一个目标尺寸只做一次）。
        # SVG 每次直接画都要重新光栅化渐变和路径（实测 522µs/帧），点阵化后只剩贴图。
        key = (self._last_art_key, round(width, 1), round(height, 1))
        cached = self._bitmaps.get(key)
        if cached is None:
            cached = appkit_art.pointize_image(image, (width, height)) or image
            self._bitmaps.put(key, cached)
        cached.drawInRect_fromRect_operation_fraction_(
            target, NSZeroRect, NSCompositingOperationSourceOver, 1.0
        )