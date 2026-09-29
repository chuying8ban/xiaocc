"""贴边「收起 ↔ 展开」循环的上漂回归 —— 先纯几何复现，再拿真窗口验一遍。

真机日志（ops 从 `panel.err.log` 抓的，每循环整体上移 13px）：

    [collapse] (1352,126) → (1500,152)
    [expand]   (1500,152) → (1352,113)
    [collapse] (1352,113) → (1500,139)
    [expand]   (1500,139) → (1352,100)

根因：`collapsed_rect` 把把手条居中在**角色本体**上，而 `docked_rect(center=…)` 居中的是
**窗口**；窗口比本体多一条固定高度的底部文案带 ⇒ 左/右两条边每收展一次就漂半个文案带。

    .venv/bin/python scripts/verify_dock_cycle.py          # 纯几何（任何机器都能跑）
    .venv/bin/python scripts/verify_dock_cycle.py --real   # 开真窗口跑一遍（不抓像素）
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xiaocc.backends import window_layout as wl

SCREEN = wl.Rect(0.0, 0.0, 1512.0, 982.0)
CANVAS = (132, 140)
SCALE = 1.0
WIN_SIZE = wl.window_size_for(CANVAS, SCALE)
START = wl.Rect(1352.0, 126.0, *WIN_SIZE)  # 起点取真机日志里的那一帧


def cycle(win: wl.Rect, edge: wl.Edge, *, fixed: bool) -> tuple[wl.Rect, wl.Rect]:
    body = wl.body_rect_of_window(win, canvas=CANVAS, scale=SCALE)
    strip = wl.collapsed_rect(edge, SCREEN, body)
    cross = strip.center.y if edge in (wl.Edge.LEFT, wl.Edge.RIGHT) else strip.center.x
    center = wl.body_center_to_window_center(cross, edge) if fixed else cross
    return strip, wl.docked_rect(edge, SCREEN, WIN_SIZE, center=center)


def cross_of(rect: wl.Rect, edge: wl.Edge) -> float:
    return rect.y if edge in (wl.Edge.LEFT, wl.Edge.RIGHT) else rect.x


def geometry() -> int:
    print(f"屏幕 {SCREEN.width:g}x{SCREEN.height:g}  画布 {CANVAS}  窗口 {WIN_SIZE}")
    print(f"半个文案带 = {wl.CAPTION_BAND / 2:g}px（= 左/右两条边每循环会漂掉的量）")
    bad = 0
    for edge in (wl.Edge.RIGHT, wl.Edge.LEFT, wl.Edge.TOP, wl.Edge.BOTTOM):
        for label, fixed in (("改前", False), ("改后", True)):
            win, seen = START, []
            for _ in range(4):
                strip, win = cycle(win, edge, fixed=fixed)
                seen.append(cross_of(strip, edge))
            drift = seen[-1] - seen[0]
            moves = abs(drift) >= 0.01
            if moves and fixed:
                bad += 1
            step = drift / (len(seen) - 1)
            verdict = f"每循环漂 {step:+.0f}px" if moves else "稳定（定点）"
            seq = " ".join(f"{v:g}" for v in seen)
            print(f"  {edge.value:<7}{label}  收起跨轴 [{seq}] → {verdict}")
    print("结果：" + ("仍会漂 ✗" if bad else "四条边都收得住 ✓"))
    return 1 if bad else 0


# —— 真窗口那一半：走和真鼠标同一条状态迁移（按下 → 多段平滑位移 → 松手）——


def _drag_to(backend, cursor: list, target_x: float, target_y: float, steps: int = 16) -> None:
    start = backend._window_local
    cursor[0] = wl.Point(start.x + start.width * 0.97, start.y + start.height * 0.5)
    backend.start_drag()
    start = backend._window_local
    for i in range(1, steps + 1):
        t = i / steps
        ease = t * t * (3 - 2 * t)
        backend.move_window_to(
            start.x + (target_x - start.x) * ease, start.y + (target_y - start.y) * ease
        )
        backend.linger(0.02)
    cursor[0] = wl.Point(target_x + start.width * 0.97, target_y + start.height * 0.5)


def _server_rect(window_number: int) -> tuple[float, float, float, float] | None:
    """窗口服务器的真话（不是后端自己的账）。"""
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionIncludingWindow, window_number
    )
    for info in infos or []:
        bounds = info.get("kCGWindowBounds")
        if bounds:
            return (bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"])
    return None


def real() -> int:
    from xiaocc.backends.appkit import AppKitBackend
    from xiaocc.characters import load_character
    from xiaocc.engine import Render
    from xiaocc.protocol import State, StatusEvent

    character = load_character()  # 内置角色包
    cursor = [wl.Point(-1000.0, -1000.0)]
    backend = AppKitBackend(cursor=lambda: cursor[0])
    backend.render(Render(event=StatusEvent(source="demo", state=State.IDLE), character=character))
    backend.linger(0.3)
    screen = backend._space().screen
    print(f"窗口尺寸 {backend._window_size()}  屏幕 {screen.width:g}x{screen.height:g}")

    # ① 拖到右边缘贴边收起（抓角色右侧推过去，跟真机上会抖的那种握法一致）
    target_x = screen.right - wl.PAD - character.canvas[0] * backend._scale() - 4.0
    _drag_to(backend, cursor, max(screen.x, target_x), backend._window_local.y)
    edge = backend.end_drag()
    backend.linger(0.5)
    print(f"拖到 {edge.value} 边收起后：{backend._window_local}")

    # ② 悬停展开 → 走开收起，来回四轮；每轮都看「收起后停在哪」
    rows = []
    for i in range(4):
        strip_before = backend._window_local
        cursor[0] = wl.Point(strip_before.center.x, strip_before.center.y)  # 悬停在把手条上
        backend.linger(0.9)
        expanded = backend._window_local
        cursor[0] = wl.Point(screen.center.x, screen.center.y)  # 走远
        backend.linger(0.9)
        strip_after = backend._window_local
        rows.append((strip_before.y, expanded.y, strip_after.y))
        print(
            f"  第{i + 1}轮  收起 y={strip_before.y:g} → 展开 y={expanded.y:g} "
            f"→ 收起 y={strip_after.y:g}"
        )

    ys = [r[2] for r in rows]
    drift = max(ys) - min(ys)
    info = backend.probe()
    seq = " ".join(f"{v:g}" for v in ys)
    print(f"四轮收起后 y 集合=[{seq}]  最大差 {drift:g}px")
    print(f"后端自报（Cocoa 左下原点）{info.get('ns_frame')}")
    if info.get("window_number"):
        print(f"窗口服务器实测 {_server_rect(int(info['window_number']))}")
    ok = drift < 0.01
    print("结果：" + ("收得住，没有上漂 ✓" if ok else f"仍在漂（{drift:g}px）✗"))
    backend.close()
    return 0 if ok else 1


def real_hover(seconds: float = 3.0) -> int:
    """用户报的那个现场：拖到右边缘松手（鼠标**就停在把手条上**），然后什么都不做。

    真机日志里这之后是一串自发的 collapse/expand 震荡（每次整体上移 13px）。
    这里原地守 N 秒，数「状态翻转」次数 —— 定点修好后应当只在收起那一刻翻一次。
    """
    import time

    from xiaocc.backends.appkit import AppKitBackend
    from xiaocc.characters import load_character
    from xiaocc.engine import Render
    from xiaocc.protocol import State, StatusEvent

    character = load_character()
    cursor = [wl.Point(-1000.0, -1000.0)]
    backend = AppKitBackend(cursor=lambda: cursor[0])
    backend.render(Render(event=StatusEvent(source="demo", state=State.IDLE), character=character))
    backend.linger(0.3)
    screen = backend._space().screen
    target_x = screen.right - wl.PAD - character.canvas[0] * backend._scale() - 4.0
    _drag_to(backend, cursor, max(screen.x, target_x), backend._window_local.y)
    backend.end_drag()
    print(f"松手时鼠标停在 {cursor[0]}，窗口 {backend._window_local}")
    # 收起后必须「鼠标先离开一次」才重新武装（防抖规则），所以先走开再回来 ——
    # 回来时鼠标压在把手条正中间，这才是「展开必须盖住鼠标、展开后不许自己缩回去」的现场
    cursor[0] = wl.Point(screen.center.x, screen.center.y)
    backend.linger(0.5)
    strip = backend._window_local
    cursor[0] = wl.Point(strip.center.x, strip.center.y)
    armed = backend.probe().get("dock")
    print(f"走开一次重新武装，再回到把手条中心 {cursor[0]}（{armed}）")

    samples: list[tuple[float, str]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        backend.linger(0.1)
        samples.append((round(backend._window_local.y, 1), str(backend.probe().get("dock"))))
    flips = sum(1 for a, b in itertools.pairwise(samples) if a[1] != b[1])
    ys = [s[0] for s in samples]
    print(f"{seconds:g}s 内采样 {len(samples)} 次：状态翻转 {flips} 次，y 跨度 {max(ys) - min(ys):g}px")
    print(f"先头几次采样 {samples[:6]}")
    # 期望：采样窗口内**一次都不许翻**（悬停展开发生在采样之前那段 linger 里），
    # 而且停在展开态、展开后仍盖住鼠标 —— 事故版的现场就是这里反复翻 + 每次上移 13px。
    inside = backend._window_local.contains(cursor[0], wl.HOVER_GRACE)
    expanded = str(backend.probe().get("dock")) == "expanded"
    stable = max(ys) - min(ys) < 0.01
    ok = flips == 0 and stable and inside and expanded
    print(f"停在展开态={expanded}  展开后仍盖住鼠标={inside}  采样窗口内一动不动={stable}")

    # ③ 离开要收起、但不能误触：走开 → 必须在 1.2s 内收起；收起后马上回到条上（没离开过）→ 不许展开
    cursor[0] = wl.Point(screen.center.x, screen.center.y)
    deadline = time.monotonic() + 1.2
    collapsed_in = None
    while time.monotonic() < deadline:
        backend.linger(0.1)
        if str(backend.probe().get("dock")) == "collapsed":
            collapsed_in = round(1.2 - (deadline - time.monotonic()), 2)
            break
    _ok_leave = collapsed_in is not None
    timing = f"{collapsed_in:.2f}s 内收起了" if _ok_leave else "1.2s 内没收起 ✗"
    print(f"  离开后收起：{timing}")

    # 再回到条上必须还能展开（不许卡死在收起态）。
    # 注：Dock.update 里「收起」只在鼠标**离开窗口**时才发生，所以离开过就一定会重新武装 ——
    # 缴械那道闸门防的是「窗口自己从鼠标底下挪走」那种抖动（就是刚修掉的 13px 上漂），
    # 那条有 window_layout 的单元测试守着。这里只验真窗口下不会卡死。
    strip = backend._window_local
    cursor[0] = wl.Point(strip.center.x, strip.center.y)
    backend.linger(0.8)
    state_now = str(backend.probe().get("dock"))
    _ok_reopen = state_now == "expanded"
    print(f"  收回后再悬停：{state_now} —— {'能再展开（没卡死）' if _ok_reopen else '卡在收起态 ✗'}")
    ok = ok and _ok_leave and _ok_reopen
    print("结果：" + ("悬停展开一次后彻底安静，不再自发震荡 ✓" if ok else "仍在震荡 ✗"))
    backend.close()
    return 0 if ok else 1


def main() -> int:
    if "--real-hover" in sys.argv:
        return real_hover()
    return real() if "--real" in sys.argv else geometry()


if __name__ == "__main__":
    raise SystemExit(main())
