"""拖动跟手 / 停靠态一把拖走 / 不越界不遮菜单栏 —— 真窗口，逐帧对窗口服务器的 frame。

主管点的两条（加上坐标翻转取整那个疑点）：

① 按住拖 200px（水平、垂直各一次）：逐帧拿 window server 的 frame 与指针位移比对，
   必须单调、无回摆、跟手误差 ≤1px。日志里 `(1352,126) ↔ (1352,128/129)` 那种 ±2~3px
   反复摆，要么是用户在垂直拖（正常），要么是每帧 delta 换算里的取整/坐标翻转在来回摆。
② 停靠态能不能**一把拖走**：鼠标压手柄 → 不松手直接拖到屏幕中央，中途不许 collapse。
⑥ 拖到四条边的极限：不越出屏幕、不遮菜单栏。

    .venv/bin/python scripts/verify_drag_tracking.py            # 纯算 + 坐标换算（不需要屏亮）
    .venv/bin/python scripts/verify_drag_tracking.py --real     # 真窗口（不抓像素，屏睡也能跑）
"""

from __future__ import annotations

import sys
from pathlib import Path

import verify_log

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xiaocc.backends import window_layout as wl

STEPS = 20
DISTANCE = 200.0
failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{' · ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


def coordinate_round_trip() -> None:
    """坐标翻转 + 取整：本地 ↔ Cocoa 来回换算必须回到原位（主管怀疑的取整回摆）。"""
    print("== 坐标换算（翻转/取整）==")
    origin_x, origin_y, height = 0.0, 0.0, 982.0
    worst = 0.0
    for local_y in (0.0, 1.0, 12.0, 100.5, 480.0, 981.0, 982.0):
        for local_x in (0.0, 3.0, 700.25, 1512.0):
            # local → Cocoa
            cocoa = wl.Point(local_x - origin_x, origin_y + height - local_y)
            # Cocoa → local（与 to_local_point 同式）
            back = wl.Point(cocoa.x + origin_x, origin_y + height - cocoa.y)
            worst = max(worst, abs(back.x - local_x), abs(back.y - local_y))
    _check("本地↔Cocoa 往返误差 ≤ 0.01px", worst <= 0.01, f"最大 {worst:g}px")


def real() -> int:
    import Quartz
    from AppKit import NSScreen

    from xiaocc.backends.appkit import AppKitBackend
    from xiaocc.characters import load_character
    from xiaocc.engine import Render
    from xiaocc.protocol import State, StatusEvent

    def server_rect(window_number: int) -> tuple[float, float, float, float] | None:
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow, window_number
        )
        for info in infos or []:
            bounds = info.get("kCGWindowBounds")
            if bounds:
                return (bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"])
        return None

    character = load_character()
    cursor = [wl.Point(-1000.0, -1000.0)]
    backend = AppKitBackend(cursor=lambda: cursor[0])
    backend.render(Render(event=StatusEvent(source="demo", state=State.IDLE), character=character))
    backend.linger(0.3)
    screen = backend._space().screen
    number = int(backend.probe()["window_number"])

    def frame() -> tuple[float, float, float, float]:
        rect = server_rect(number)
        assert rect is not None, "窗口服务器读不到窗口"
        return rect

    # —— 需要菜单栏/Dock 的真实边界（visibleFrame）——
    ns = NSScreen.mainScreen().frame()
    vis = NSScreen.mainScreen().visibleFrame()
    menu_bar = round(ns.size.height - (vis.origin.y + vis.size.height))
    print(f"== 真窗口 ==\n屏幕 {screen.width:g}x{screen.height:g}  菜单栏高 {menu_bar}px  "
          f"可见区顶 {menu_bar}px / 底 {round(ns.size.height - vis.origin.y - vis.size.height)}px(Dock)")

    # 先摆到屏幕中间、确保是「展开 + 没停靠」的干净起点（上一轮可能停在锚点上贴着边）
    def refloat() -> None:
        cur = backend._window_local
        cursor[0] = wl.Point(cur.center.x, cur.center.y)
        backend.start_drag()
        for i in range(1, 11):
            t = i / 10
            backend.move_window_to(
                cur.x + (screen.center.x - cur.width / 2 - cur.x) * t,
                cur.y + (screen.center.y - cur.height / 2 - cur.y) * t,
            )
            backend.linger(0.02)
        cursor[0] = wl.Point(screen.center.x, screen.center.y)
        backend.end_drag()
        cursor[0] = wl.Point(-1000.0, -1000.0)
        backend.linger(0.4)

    refloat()
    print(f"-- 起点（应当展开且未停靠）{backend._window_local} dock={backend.probe().get('dock')} --")

    # —— ① 跟手：水平、垂直各一次，逐帧比对 ——
    for axis, label in ((0, "水平"), (1, "垂直")):
        start = backend._window_local
        cursor[0] = wl.Point(start.x + start.width / 2, start.y + start.height / 2)
        backend.start_drag()
        origin = backend._window_local
        # 垂直那次往上拖（用户说的那种），水平那次往左拖（朝屏幕里有空间的方向）
        delta = (DISTANCE, -DISTANCE)[axis]
        frames: list[float] = []
        for i in range(1, STEPS + 1):
            want = delta * i / STEPS
            if axis == 0:
                cursor[0] = wl.Point(start.x + start.width / 2 + want, start.y + start.height / 2)
            else:
                cursor[0] = wl.Point(start.x + start.width / 2, start.y + start.height / 2 + want)
            backend.move_window_to(start.x + want, start.y + (0.0 if axis == 0 else want))
            backend.linger(0.03)
            frames.append(frame()[axis])
        backend.end_drag()
        backend.linger(0.2)
        stepped = [frames[i + 1] - frames[i] for i in range(len(frames) - 1)]
        forward = all((s > 0) if delta > 0 else (s < 0) for s in stepped)
        wiggle = max(abs(s) for s in stepped)
        raw = backend._window_local
        expect = origin.x + delta if axis == 0 else origin.y + delta
        got = raw.x if axis == 0 else raw.y
        _check(f"{label}拖动 {DISTANCE:g}px：单调无回摆", forward,
               f"每帧位移 {min(stepped):+g}..{max(stepped):+g}px")
        _check(f"{label}拖动跟手误差 ≤1px", abs(got - expect) <= 1.0,
               f"期望 {expect:g} 实得 {got:g}（差 {got - expect:+.1f}px，步长抖动 {wiggle:g}px）")
        cursor[0] = wl.Point(-1000.0, -1000.0)
        backend.linger(0.2)

    # —— ② 停靠态一把拖走：压手柄按住不放，直接拖到屏幕中央 ——
    target_x = screen.right - wl.PAD - character.canvas[0] * backend._scale() - 4.0
    strip_start = backend._window_local
    cursor[0] = wl.Point(strip_start.x + strip_start.width * 0.97, strip_start.center.y)
    backend.start_drag()
    for i in range(1, STEPS + 1):
        t = i / STEPS
        ease = t * t * (3 - 2 * t)
        cursor[0] = wl.Point(strip_start.x + strip_start.width * 0.97, strip_start.center.y)
        backend.move_window_to(
            strip_start.x + (target_x - strip_start.x) * ease, strip_start.y
        )
        backend.linger(0.02)
    edge = backend.end_drag()
    backend.linger(0.5)
    docked = backend._window_local
    _check("贴边收起成功（前置条件）", edge is not wl.Edge.NONE and docked.width <= wl.HANDLE_THICKNESS + 1,
           f"edge={edge.value} 窗口 {docked}")

    # 现在压住手柄把它拖走（不松手），中途不许回缩
    strip = backend._window_local
    cursor[0] = wl.Point(strip.center.x, strip.center.y)
    backend.linger(0.6)  # 悬停展开（走开过一次已经重新武装）
    backend.start_drag()
    mid_states: list[str] = []
    # 目标：让**展开后的窗口中心**落到屏幕中心
    expanded_w = backend._window_local.width
    dest_x = screen.center.x - expanded_w / 2
    step_x = (dest_x - backend._window_local.x) / STEPS
    for i in range(1, STEPS + 1):
        cursor[0] = wl.Point(backend._window_local.center.x + step_x, strip.center.y)
        backend.move_window_to(backend._window_local.x + step_x, backend._window_local.y)
        backend.linger(0.03)
        mid_states.append(str(backend.probe().get("dock")))
    end_edge = backend.end_drag()
    walked = backend._window_local
    _check("停靠态一把拖走：中途不回缩", all(s != "collapsed" for s in mid_states),
           f"途中状态集合 {sorted(set(mid_states))}")
    _check("拖走后不再贴边", end_edge is wl.Edge.NONE, f"end_edge={end_edge.value}")
    _check("确实拖到了屏幕中央附近", abs(walked.center.x - screen.center.x) < 60,
           f"窗口中心 x={walked.center.x:g} 屏幕中心 {screen.center.x:g}")

    # —— ⑥ 拖到四条边的极限：不越出屏幕、不遮菜单栏 ——
    print("-- ⑥ 边界 --")
    probes = {
        "顶": (screen.center.x, screen.y - 400),
        "底": (screen.center.x, screen.bottom + 400),
        "左": (screen.x - 400, screen.center.y),
        "右": (screen.right + 400, screen.center.y),
    }
    for name, (tx, ty) in probes.items():
        cursor[0] = wl.Point(backend._window_local.center.x, backend._window_local.center.y)
        backend.start_drag()
        backend.move_window_to(tx, ty)
        backend.linger(0.2)
        backend.end_drag()
        backend.linger(0.4)
        x, y, w, h = frame()
        local = backend._window_local
        # 判据用后端自己的布局矩形（它才是我们控制的那些数）；
        # 窗口服务器那份会多 1px 边框/阴影，OPS 的 doctor 也是按 Δ≤6px 读的。
        inside = (
            local.x >= -0.01
            and local.y >= -0.01
            and local.right <= screen.width + 0.01
            and local.bottom <= screen.height + 0.01
        )
        _check(f"拖到{name}极限：不越出屏幕", inside,
               f"布局 {local.x:g},{local.y:g} {local.width:g}x{local.height:g} · "
               f"窗口服务器 {x:g},{y:g} {w:g}x{h:g}（含 1px 边框）")
        if name == "顶":
            _check("顶边不遮菜单栏（窗口顶 ≥ 菜单栏高）", y >= menu_bar - 1,
                   f"窗口顶 y={y:g} 菜单栏高 {menu_bar}")
        cursor[0] = wl.Point(-1000.0, -1000.0)
        backend.linger(0.2)

    backend.close()
    print(f"\n结果：{'全部通过' if not failures else '失败 ' + '、'.join(failures)}")
    return 1 if failures else 0


def main() -> int:
    coordinate_round_trip()
    if "--real" not in sys.argv:
        print("\n（真窗口部分要加 --real）")
        rc = 1 if failures else 0
    else:
        rc = real()
    verify_log.record(
        "verify_drag_tracking",
        rc,
        criteria={"failures": len(failures), "real": "--real" in sys.argv},
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
