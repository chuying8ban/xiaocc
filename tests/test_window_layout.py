"""贴边/命中/动作曲线的单元测试 —— 不需要图形库，任何 CI 上都能跑。

重点守两条曾经把人坑惨的不变量：
  1. 展开后的窗口必须盖住把手条（否则鼠标还停在条上，展开瞬间就又收起 → 抖动）
  2. 收起后必须「鼠标先离开一次」才允许悬停展开（同上，防抖）
"""

from __future__ import annotations

import itertools
import math

import pytest

from xiaocc.backends import window_layout as wl
from xiaocc.characters import KNOWN_MOTIONS

SCREEN = wl.Rect(0, 0, 1512, 982)
CANVAS = (220, 240)


def _body_at(x: float, y: float) -> wl.Rect:
    size = wl.window_size_for(CANVAS)
    return wl.body_rect_of_window(wl.Rect(x, y, *size), canvas=CANVAS)


# —— 几何 ————————————————————————————————————————————————————————————————


def test_window_size_keeps_caption_inside():
    width, height = wl.window_size_for(CANVAS, 1.0)
    assert width == 220 + 2 * wl.PAD
    assert height == 240 + 2 * wl.PAD + wl.CAPTION_BAND


def test_body_rect_excludes_padding_and_caption_band():
    size = wl.window_size_for(CANVAS)
    frame = wl.Rect(100, 100, *size)
    body = wl.body_rect_of_window(frame, canvas=CANVAS)
    assert (body.x, body.y, body.width, body.height) == (114, 114, 220, 240)
    assert body.bottom < frame.bottom  # 文案带在角色下方，不重叠


def test_contains_respects_slack():
    rect = wl.Rect(10, 10, 100, 50)
    assert rect.contains(wl.Point(60, 30))
    assert not rect.contains(wl.Point(0, 30))
    assert rect.contains(wl.Point(5, 30), slack=6)


def test_clamped_into_pulls_window_back_on_screen():
    frame = wl.Rect(-40, 900, 240, 280)
    fixed = frame.clamped_into(SCREEN)
    assert fixed.x == 0
    assert fixed.bottom == SCREEN.height


# —— 贴边判定 ——————————————————————————————————————————————————————————————


@pytest.mark.parametrize(
    "position,expected",
    [
        ((SCREEN.right - 220 - 5, 400), wl.Edge.RIGHT),  # 右边缘附近
        ((5, 400), wl.Edge.LEFT),
        ((600, 2), wl.Edge.TOP),
        ((600, SCREEN.height - 240 - 3), wl.Edge.BOTTOM),
        ((600, 400), wl.Edge.NONE),  # 屏幕正中，谁都不挨
        ((600, 26), wl.Edge.NONE),  # 只是「接近顶部」但还没到吸附距离
    ],
)
def test_choose_edge(position, expected):
    assert wl.choose_edge(_body_at(*position), SCREEN) == expected


def test_choose_edge_uses_body_not_window():
    """窗口带 14px 留白 + 26px 文案带：用窗口判会提前 40px 就收起。"""
    size = wl.window_size_for(CANVAS)
    window = wl.Rect(SCREEN.right - size[0] - 30, 400, *size)
    # 窗口右沿离边 30px（>24 不该吸附），角色本体离边 44px —— 更不该
    assert wl.choose_edge(wl.body_rect_of_window(window, canvas=CANVAS), SCREEN) == wl.Edge.NONE


def test_collapsed_rect_is_flush_and_clamped_at_corner():
    body = _body_at(600, SCREEN.height - 240 - 2)
    strip = wl.collapsed_rect(wl.Edge.BOTTOM, SCREEN, body)
    assert strip.bottom == SCREEN.height
    assert strip.height == wl.HANDLE_THICKNESS
    assert SCREEN.contains(strip.center)

    # 贴着右下角时，把手条不能跑到屏幕外
    corner = _body_at(SCREEN.right - 220, SCREEN.height - 240)
    strip_r = wl.collapsed_rect(wl.Edge.RIGHT, SCREEN, corner)
    assert strip_r.right == SCREEN.right
    assert strip_r.y >= 0 and strip_r.bottom <= SCREEN.height


def test_expanded_window_always_covers_its_handle():
    """核心不变量：展开后窗口必须盖住把手条 —— 否则展开即收起，抖动。"""
    for edge, body in {
        wl.Edge.RIGHT: _body_at(SCREEN.right - 220, 300),
        wl.Edge.LEFT: _body_at(0, 300),
        wl.Edge.TOP: _body_at(700, 0),
        wl.Edge.BOTTOM: _body_at(700, SCREEN.height - 240),
    }.items():
        strip = wl.collapsed_rect(edge, SCREEN, body)
        size = wl.window_size_for(CANVAS)
        expanded = wl.docked_rect(edge, SCREEN, size, center=_cross_center(edge, strip))
        assert expanded.contains(strip.center, wl.HOVER_GRACE), edge
        # 贴边的那一侧仍然贴着边（不能展开后飘到屏幕中间）
        assert _touches_edge(expanded, edge), edge


def _cross_center(edge: wl.Edge, rect: wl.Rect) -> float:
    return rect.center.y if edge in (wl.Edge.LEFT, wl.Edge.RIGHT) else rect.center.x


def _touches_edge(rect: wl.Rect, edge: wl.Edge) -> bool:
    if edge is wl.Edge.RIGHT:
        return rect.right == SCREEN.right
    if edge is wl.Edge.LEFT:
        return rect.x == SCREEN.x
    if edge is wl.Edge.TOP:
        return rect.y == SCREEN.y
    return rect.bottom == SCREEN.height


# —— 鼠标命中 ——————————————————————————————————————————————————————————————


def test_inside_ellipse_lets_corner_clicks_pass_through():
    rect = wl.Rect(0, 0, 200, 200)
    assert wl.inside_ellipse(wl.Point(100, 100), rect)
    assert not wl.inside_ellipse(wl.Point(2, 2), rect)  # 透明四角 → 点穿
    assert wl.inside_ellipse(wl.Point(2, 100), rect)  # 上下中点是角色本体


# —— 收起/展开时序 ——————————————————————————————————————————————————————————


def test_drop_near_edge_docks_and_stays_collapsed_while_cursor_is_on_handle():
    dock = wl.Dock()
    body = _body_at(SCREEN.right - 220, 300)
    assert dock.drop(body, SCREEN) == wl.Edge.RIGHT
    assert dock.state == "collapsed"

    strip = wl.collapsed_rect(wl.Edge.RIGHT, SCREEN, body)
    # 松手时鼠标就压在把手条上：必须保持收起，不能立刻弹开
    for _ in range(5):
        assert dock.update(strip.center, strip) == wl.DockAction.NONE
    assert dock.state == "collapsed"


def test_hover_expands_only_after_cursor_left_once_then_leave_collapses():
    dock = wl.Dock()
    body = _body_at(SCREEN.right - 220, 300)
    dock.drop(body, SCREEN)
    strip = wl.collapsed_rect(wl.Edge.RIGHT, SCREEN, body)

    dock.update(wl.Point(600, 600), strip)  # 鼠标挪开 → 上膛
    assert dock.update(strip.center, strip) == wl.DockAction.EXPAND
    assert dock.state == "expanded"

    size = wl.window_size_for(CANVAS)
    expanded = wl.docked_rect(wl.Edge.RIGHT, SCREEN, size, center=strip.center.y)

    # 鼠标还在窗口里 → 不动
    assert dock.update(expanded.center, expanded) == wl.DockAction.NONE
    # 鼠标离开窗口 → 重新收起
    assert dock.update(wl.Point(200, 500), expanded) == wl.DockAction.COLLAPSE
    assert dock.state == "collapsed"


def test_floating_pet_never_collapses():
    dock = wl.Dock()
    body = _body_at(600, 400)
    assert dock.drop(body, SCREEN) == wl.Edge.NONE
    assert dock.state == "floating"
    size = wl.window_size_for(CANVAS)
    window = wl.Rect(600, 400, *size)
    assert dock.update(wl.Point(10, 10), window) == wl.DockAction.NONE
    assert not dock.collapsed


def test_dragging_suspends_judgement():
    dock = wl.Dock()
    dock.drop(_body_at(SCREEN.right - 220, 300), SCREEN)
    strip = wl.collapsed_rect(wl.Edge.RIGHT, SCREEN, _body_at(SCREEN.right - 220, 300))
    dock.update(wl.Point(600, 600), strip)  # 上膛
    assert dock.update(strip.center, strip, dragging=True) == wl.DockAction.NONE
    assert dock.state == "collapsed"


def test_drag_started_expands_and_reset_floats():
    dock = wl.Dock(collapsed=True, docked=True, armed=False, edge=wl.Edge.RIGHT)
    dock.drag_started()
    assert dock.state == "expanded"
    dock.reset()
    assert dock.state == "floating" and dock.edge is wl.Edge.NONE


# —— 动作曲线 ——————————————————————————————————————————————————————————————


def test_motion_poses_cover_every_known_motion():
    """characters.KNOWN_MOTIONS 是契约，这里必须一个不漏地实现（防止两边漂移）。"""
    assert set(wl.MOTION_POSES) == set(KNOWN_MOTIONS)


@pytest.mark.parametrize("motion", sorted(wl.MOTION_POSES))
def test_pose_is_bounded_and_continuous(motion):
    values = [wl.pose_for(motion, i / 60.0) for i in range(360)]  # 6 秒，60fps
    for pose in values:
        assert -20 <= pose.dx <= 20
        assert -20 <= pose.dy <= 20
        assert 0.5 <= pose.scale <= 1.5
        assert -15 <= pose.rotation <= 15
        assert 0.0 <= pose.glow <= 1.0
    # 相邻帧不能跳变（跳变在屏幕上看就是闪）
    for a, b in itertools.pairwise(values):
        assert abs(a.dy - b.dy) <= 3.0
        assert abs(a.dx - b.dx) <= 3.0
        assert abs(a.rotation - b.rotation) <= 3.0


def test_motions_actually_move():
    """除了待机以外的动作，六秒内都该动起来（防止常量 Pose 蒙混过关）。"""
    for motion in sorted(wl.MOTION_POSES):
        samples = [wl.pose_for(motion, i / 30.0) for i in range(180)]
        spread = max(abs(p.dy) + abs(p.dx) + abs(p.rotation) for p in samples)
        assert spread > 0.5, motion


def test_unknown_motion_falls_back_to_idle():
    assert wl.pose_for("sparkle", 1.0) == wl.pose_for("float", 1.0)


def test_pose_blend_endpoints_and_midpoint():
    a, b = wl.Pose(dx=0, dy=0, glow=0.0), wl.Pose(dx=10, dy=-4, glow=1.0)
    assert a.blend_to(b, 0.0).dx == 0
    assert a.blend_to(b, 1.0).dx == 10
    assert a.blend_to(b, 0.5).dx == pytest.approx(5)


def test_sleep_breaths_slowly_and_cheer_hops_up():
    assert all(wl.pose_for("sleep", t / 20).dy <= 2.3 for t in range(80))
    assert any(wl.pose_for("cheer", t / 20).dy < -4 for t in range(80))  # 向上跳 = dy 负


# —— 初始停靠点 —————————————————————————————————————————————————————————————

_W, _H = wl.window_size_for(CANVAS)
_MARGIN = wl.ANCHOR_MARGIN
_TOP = wl.ANCHOR_TOP_OFFSET


def _anchor(text: str | None) -> wl.Rect:
    return wl.parse_anchor(text, SCREEN, (_W, _H))


def test_parse_anchor_respects_usable_area():
    """自己挑的位置必须落在「可用区」里 —— 只按整屏算，at=bottom-* 会掉进 Dock 带。

    实测（macOS visibleFrame）：1512x982 上底部让出 90px(Dock)、顶部 33px(菜单栏)。
    这里不依赖真屏幕，直接把可用区当参数喂进去。
    """
    usable = wl.Rect(0, 33, 1512, 859)  # 顶部 33 菜单栏、底部到 892
    size = (160.0, 194.0)
    bottom_right = wl.parse_anchor("bottom-right", SCREEN, size, usable=usable)
    assert bottom_right.bottom == pytest.approx(usable.bottom - wl.ANCHOR_MARGIN)
    assert bottom_right.bottom <= usable.bottom
    for name in wl.ANCHOR_CHOICES:
        rect = wl.parse_anchor(name, SCREEN, size, usable=usable)
        assert rect.y >= usable.y, name
        assert rect.bottom <= usable.bottom, name
        assert rect.x >= usable.x and rect.right <= usable.right, name


def test_parse_anchor_without_usable_keeps_old_behaviour():
    """没给可用区（纯几何测试/无图形会话）时退回整屏口径 —— 老行为不许变。"""
    size = (160.0, 194.0)
    assert wl.parse_anchor("bottom-right", SCREEN, size).bottom == pytest.approx(
        SCREEN.bottom - wl.ANCHOR_MARGIN
    )
    assert wl.parse_anchor("top-right", SCREEN, size).y == pytest.approx(wl.ANCHOR_TOP_OFFSET)


def test_parse_anchor_xy_is_clamped_into_usable():
    """绝对坐标写过头时也要夹进可用区（不能跑到屏幕外，也不能压在 Dock 底下）。"""
    usable = wl.Rect(0, 33, 1512, 859)
    rect = wl.parse_anchor("120,5000", SCREEN, (160.0, 194.0), usable=usable)
    assert rect.bottom <= usable.bottom


@pytest.mark.parametrize(
    "name,expected",
    [
        ("top-right", (SCREEN.right - _W - _MARGIN, SCREEN.y + _TOP)),
        ("top-left", (SCREEN.x + _MARGIN, SCREEN.y + _TOP)),
        ("bottom-right", (SCREEN.right - _W - _MARGIN, SCREEN.bottom - _H - _MARGIN)),
        ("bottom-left", (SCREEN.x + _MARGIN, SCREEN.bottom - _H - _MARGIN)),
        ("center", ((SCREEN.width - _W) / 2.0, (SCREEN.height - _H) / 2.0)),
    ],
)
def test_parse_anchor_named_positions(name, expected):
    """五个方位都按 margin / top_offset 手算得出，且落在屏幕内。"""
    frame = _anchor(name)
    assert (frame.x, frame.y) == pytest.approx(expected)
    assert (frame.width, frame.height) == (_W, _H)
    assert SCREEN.contains(frame.center)


def test_parse_anchor_default_is_top_right():
    """没配 = 配了默认值：None / 空串 / 大小写与首尾空白都归到 top-right。"""
    assert _anchor(None) == _anchor("top-right")
    assert _anchor("") == _anchor("top-right")
    assert _anchor("  Top-Right  ") == _anchor("top-right")


def test_parse_anchor_accepts_absolute_coordinates():
    assert _anchor("120,300") == wl.Rect(120, 300, _W, _H)
    assert _anchor("120, 300") == _anchor("120,300")


def test_parse_anchor_clamps_coordinates_far_off_screen():
    frame = _anchor("5000,5000")
    assert frame.right == SCREEN.right  # 夹回屏幕内，右下角对齐
    assert frame.bottom == SCREEN.bottom
    assert SCREEN.contains(frame.center)


@pytest.mark.parametrize("bad", ["middle", "120", "a,b"])
def test_parse_anchor_rejects_garbage(bad):
    with pytest.raises(ValueError) as excinfo:
        _anchor(bad)
    message = str(excinfo.value)
    assert "top-right" in message
    for name in wl.ANCHOR_CHOICES:  # 报错要把合法取值一次列全
        assert name in message
    assert "x,y" in message


def test_parse_anchor_survives_window_bigger_than_screen():
    huge = (SCREEN.width * 3.0, SCREEN.height * 3.0)
    for text in (None, "center", "bottom-right", "5000,-20"):
        frame = wl.parse_anchor(text, SCREEN, huge)  # 不抛异常
        assert (frame.width, frame.height) == huge
        assert frame.x == SCREEN.x and frame.y == SCREEN.y  # 夹不住 → 贴左上


# —— 颜色 ————————————————————————————————————————————————————————————————


def test_parse_hex_variants():
    assert wl.parse_hex("#ffffff") == (1.0, 1.0, 1.0)
    assert wl.parse_hex("#000") == (0.0, 0.0, 0.0)
    r, g, b = wl.parse_hex("#6FD3E8")
    assert (round(r, 3), round(g, 3), round(b, 3)) == (0.435, 0.827, 0.91)


@pytest.mark.parametrize("bad", ["", "white", "#12345", "#gggggg", "6FD3E8"])
def test_parse_hex_rejects_garbage(bad):
    with pytest.raises(ValueError):
        wl.parse_hex(bad)


def test_clamp_handles_inverted_range():
    assert wl.clamp(5, 0, 10) == 5
    assert wl.clamp(-5, 0, 10) == 0
    assert wl.clamp(50, 0, 10) == 10
    assert wl.clamp(0, 10, 0) == 10


def test_rect_math_is_consistent():
    rect = wl.Rect(0, 0, 10, 20)
    assert rect.right == 10 and rect.bottom == 20
    assert rect.center == wl.Point(5, 10)
    assert rect.size == (10.0, 20.0)
    assert rect.moved(3, 4).x == 3
    assert math.isclose(rect.moved(3, 4).bottom, 24)


# —— 收起 ↔ 展开必须是个「定点」（第三条曾被真机抓出来的不变量）——————————
# 事故：贴着左/右边缘的桌宠每次收展都往上走 13px，日志里
# (1352,126) →[collapse](1500,152) →[expand](1352,113) →[collapse](1500,139) → …
# 根因是收起与展开用了**不同的居中基准**：collapsed_rect 居中在角色本体上，
# docked_rect(center=…) 居中在窗口上，而窗口比本体多一条固定高度的底部文案带。


def _cycle_once(win: wl.Rect, edge: wl.Edge, *, canvas=CANVAS, scale: float = 1.0) -> tuple[wl.Rect, wl.Rect]:
    """按 appkit 的顺序走一轮：窗口 → 本体 → 收起把手条 → 再展开成窗口。"""
    body = wl.body_rect_of_window(win, canvas=canvas, scale=scale)
    strip = wl.collapsed_rect(edge, SCREEN, body)
    cross = strip.center.y if edge in (wl.Edge.LEFT, wl.Edge.RIGHT) else strip.center.x
    center = wl.body_center_to_window_center(cross, edge)
    return strip, wl.docked_rect(edge, SCREEN, wl.window_size_for(canvas, scale), center=center)


def _cross(rect: wl.Rect, edge: wl.Edge) -> float:
    """跨轴（左/右是 y，上/下是 x）上的**起点**。"""
    return rect.y if edge in (wl.Edge.LEFT, wl.Edge.RIGHT) else rect.x


def _cross_mid(rect: wl.Rect, edge: wl.Edge) -> float:
    """跨轴上的**中心** —— 收起/展开对齐的就是它（别和上面 edge 在前的 _cross_center 混）。"""
    center = rect.center
    return center.y if edge in (wl.Edge.LEFT, wl.Edge.RIGHT) else center.x


@pytest.mark.parametrize("edge", [wl.Edge.LEFT, wl.Edge.RIGHT, wl.Edge.TOP, wl.Edge.BOTTOM])
@pytest.mark.parametrize("start_y", [40, 200, 480, 760])
def test_collapse_expand_cycle_stops_moving(edge, start_y):
    """来回收展，**第二轮之后**必须一动不动。

    第一轮允许挪一次：窗口比把手条大，贴近屏幕角时居中会被夹取（越界不允许），
    这是几何上必须的；真正要守的是**没有每循环重复的漂移** ——
    事故版就是每循环稳定地往上 13px，一路爬出屏幕。
    """
    win = wl.Rect(1352, start_y, *wl.window_size_for(CANVAS))
    seen = []
    for _ in range(5):
        strip, win = _cycle_once(win, edge)
        seen.append(_cross(strip, edge))
    assert max(seen[1:]) - min(seen[1:]) < 0.01, f"{edge} 边每循环都在漂：{seen}"


@pytest.mark.parametrize("edge", [wl.Edge.LEFT, wl.Edge.RIGHT])
def test_vertical_edges_swap_reference_by_half_caption_band(edge):
    """左/右两条边的跨轴是 y —— 窗口比本体多半个文案带，必须补上，且不随缩放变化。"""
    for scale in (1.0, 1.5):
        assert wl.body_center_to_window_center(300.0, edge) == pytest.approx(300.0 + wl.CAPTION_BAND / 2)


@pytest.mark.parametrize("edge", [wl.Edge.TOP, wl.Edge.BOTTOM])
def test_horizontal_edges_keep_center_as_is(edge):
    """上/下两条边的跨轴是 x —— 窗口只多两侧等宽留白，本体中心相对窗口中心不变。"""
    assert wl.body_center_to_window_center(300.0, edge) == 300.0


@pytest.mark.parametrize("edge", [wl.Edge.LEFT, wl.Edge.RIGHT, wl.Edge.TOP, wl.Edge.BOTTOM])
def test_expanded_body_is_centred_on_the_strip(edge):
    """展开后角色本体的中心必须落在把手条的中心上（这才是「定点」的直接成因）。

    取屏幕中间的位置：贴角时居中会被夹取，那不是这条不变量管的。
    """
    win = wl.Rect(600, 300, *wl.window_size_for(CANVAS))
    strip, expanded = _cycle_once(win, edge)
    body = wl.body_rect_of_window(expanded, canvas=CANVAS)
    assert _cross_mid(body, edge) == pytest.approx(_cross_mid(strip, edge))
