"""真鼠标事件路径的仿真回归：拖动必须**逐事件跟手**，鼠标停住必须**同帧静止**。

为什么要有这个脚本（2026-09-29，用户报「拖动还是卡顿」）：
``scripts/verify_drag_tracking.py`` 的 5 个调用点清一色是 ``backend.move_window_to()``，
``_mouse_down`` / ``_mouse_dragged`` **一次都没被驱动过** —— 于是它全绿，而真机拖动在
``panel.err.log`` 里 88% 的相邻位移方向相反、窗口在两三个位置之间来回跳（极点 −1，不收敛）。

这个脚本用**假鼠标事件对象**驱动 backend 的**真实鼠标处理函数**（不绕道 move_window_to），
假事件按 macOS 的真实语义给坐标：``locationInWindow()`` 是相对**当前** frame 算的窗口局部
坐标（原点左下）。真机之所以来回跳，正是因为位移基准用了这个会随窗口自身移动而变化的局部
坐标：``target = 按下时窗口原点 + 局部坐标差``，而局部坐标里已经减掉了窗口的位移。
⇒ 映射 ``u_k = C − u_{k−1}``，DC 增益 ½、极点在 −1。

判据（旧码必红，四条互不替代）：
  ① 逐事件 |Δ窗口 − Δ鼠标| ≤ 1px  —— 抓住「DC 增益只有一半」（跟手只有半速）
  ② 相邻位移方向反转数 = 0        —— 抓住「来回跳」
  ③ 鼠标停住后连发 3 个同坐标事件，窗口位移 ≤ 1px —— 抓住「极点 −1，永远静不下来」
  ④ 末位 = 起点 + 总位移（≤1px）  —— 抓住「越拖越落后」

用法（仓库 venv，需要屏幕醒着）：``.venv/bin/python scripts/verify_drag_mouse.py``
退出码 0 = 全过，1 = 有失败项。
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections import namedtuple
from itertools import pairwise
from pathlib import Path

_CocoaPoint = namedtuple("_CocoaPoint", "x y")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

# 卫生：**先**把锚点/自证据指到临时目录，再 import xiaocc —— 绝不碰用户真实
# ~/.xiaocc/anchor.json 与 probe.json（这条以前踩过坑）。
_TMP = Path(tempfile.mkdtemp(prefix="xiaocc-dragfix-"))
os.environ["XIAOCC_ANCHOR_FILE"] = str(_TMP / "anchor.json")
os.environ["XIAOCC_PROBE_FILE"] = str(_TMP / "probe.json")

import Quartz

from xiaocc.backends import window_layout as wl
from xiaocc.backends.appkit import AppKitBackend
from xiaocc.characters import load_character
from xiaocc.engine import Render
from xiaocc.protocol import State, StatusEvent

STEPS = 20
STEP_PX = 5.0
IDLE_EVENTS = 3  # 鼠标停住后连发几个同坐标事件
EPS = 1.0  # 逻辑像素

failures: list[str] = []
checks: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{' · ' + detail if detail else ''}")
    checks.append(name)
    if not ok:
        failures.append(name)


class FakeMouseEvent:
    """只实现 backend 真正用到的那个接口：``locationInWindow()``（Cocoa 窗口坐标，原点左下）。

    ``screen`` 是鼠标的**屏幕**坐标（左上原点，与 window_layout 同口径）；``origin`` 是事件
    被投递时窗口的屏幕矩形 —— 真机上 macOS 就是拿**当前** frame 算局部坐标的，所以这里必须
    每帧现算，不能复用按下时的那个。
    """

    def __init__(self, screen: wl.Point, origin: wl.Rect):
        self._local = (screen.x - origin.x, origin.height - (screen.y - origin.y))

    def locationInWindow(self):
        # PyObjC 的 NSPoint 是带 .x/.y 的结构体，假事件也得给同样的接口
        return _CocoaPoint(*self._local)


def server_rect(window_number: int) -> tuple[float, float, float, float] | None:
    """从**窗口服务器**读真实矩形（不是读 backend 自己记的簿）。"""
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionIncludingWindow, window_number
    )
    for info in infos or []:
        bounds = info.get("kCGWindowBounds")
        if bounds:
            return (bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"])
    return None


def event_for(mouse: wl.Point, rect: wl.Rect) -> FakeMouseEvent:
    return FakeMouseEvent(mouse, rect)


def run_axis(backend, number: int, label: str, axis: int, distance: float) -> list[dict]:
    """一次完整的 mouse-down → 拖动 → 停住 → mouse-up，逐事件记窗口服务器的真实位置。"""
    start = backend._window_local
    # 基准取**窗口服务器**那一份，不取 backend 自己记的簿：真 frame 比布局矩形多 1px 边框
    # （y 方向还会再多 1px，scripts/verify_drag_tracking.py 的边界项里已记过「含 1px 边框」），
    # 拿自己记的簿当基准会把这个常数偏差记到「跟手误差」头上。
    # 判据①看增量、不受常数影响；④要的是绝对位置，所以基准必须是外部真值。
    real = server_rect(number)
    assert real is not None, "窗口服务器读不到窗口"
    if (real[0], real[1]) != (start.x, start.y):
        print(f"   （基准校正：backend 记的 {start.x:.0f},{start.y:.0f} → 真 frame {real[0]:.0f},{real[1]:.0f}）")
    press = wl.Point(real[0] + real[2] / 2, real[1] + real[3] / 2)
    backend._mouse_down(event_for(press, backend._window_local))
    baseline = (real[0], real[1])

    rows: list[dict] = []

    def sample(mouse: wl.Point, tag: str) -> dict:
        backend.linger(0.02)  # 给窗口服务器一拍把新 frame 落到 CGWindowList
        rect = server_rect(number)
        assert rect is not None, "窗口服务器读不到窗口"
        m = mouse.x if axis == 0 else mouse.y
        base = press.x if axis == 0 else press.y
        start_at = baseline[axis]
        row = {
            "tag": tag,
            "mouse": m,
            # 期望：窗口原点 = 按下时的原点 + 鼠标自按下起的位移（DC 增益 1）
            "expect": start_at + (m - base),
            "actual": rect[axis],
        }
        rows.append(row)
        return row

    step = STEP_PX if distance > 0 else -STEP_PX
    for i in range(1, STEPS + 1):
        d = step * i
        mouse = wl.Point(press.x + d, press.y) if axis == 0 else wl.Point(press.x, press.y + d)
        backend._mouse_dragged(event_for(mouse, backend._window_local))
        sample(mouse, f"step{i}")

    # 鼠标停住：同坐标连发几个事件，窗口必须静止（旧码在这里落成 2 周期）
    last = wl.Point(press.x + step * STEPS, press.y) if axis == 0 else wl.Point(
        press.x, press.y + step * STEPS
    )
    idle_rows = [sample(last, f"idle{i}") for i in range(1, IDLE_EVENTS + 1)]

    backend._mouse_up(event_for(last, backend._window_local))
    backend.linger(0.2)

    # —— 打印表（红/绿都要一眼看出原因）——
    print(f"== {label}拖动 {distance:g}px ==")
    print(f"   {'帧':<8}{'鼠标':>8}{'期望窗口':>10}{'实际窗口':>10}{'偏差':>8}{'Δ窗口':>8}{'Δ鼠标':>8}")
    prev = None
    for r in rows:
        dw = "" if prev is None else f"{r['actual'] - prev['actual']:+.0f}"
        dm = "" if prev is None else f"{r['mouse'] - prev['mouse']:+.0f}"
        print(
            f"   {r['tag']:<8}{r['mouse']:>8.0f}{r['expect']:>10.1f}"
            f"{r['actual']:>10.1f}{r['actual'] - r['expect']:>+8.1f}{dw:>8}{dm:>8}"
        )
        prev = r

    # —— ①逐事件跟手：DC 增益必须是 1 ——
    bad = [
        (rows[i], rows[i + 1])
        for i in range(len(rows) - 1)
        if abs((rows[i + 1]["actual"] - rows[i]["actual"]) - (rows[i + 1]["mouse"] - rows[i]["mouse"])) > EPS
    ]
    worst = max(
        (
            abs((rows[i + 1]["actual"] - rows[i]["actual"]) - (rows[i + 1]["mouse"] - rows[i]["mouse"]))
            for i in range(len(rows) - 1)
        ),
        default=0.0,
    )
    _check(
        f"①{label}：逐事件 Δ窗口 == Δ鼠标（≤{EPS:g}px）",
        not bad,
        f"最大偏差 {worst:.0f}px，违反 {len(bad)}/{len(rows) - 1} 步",
    )

    # —— ②方向反转 ——
    deltas = [rows[i + 1]["actual"] - rows[i]["actual"] for i in range(len(rows) - 1)]
    rev = sum(1 for p, q in pairwise(deltas) if p * q < 0)
    _check(f"②{label}：不来回跳（方向反转 0）", rev == 0, f"反转 {rev}/{len(deltas)} 步")

    # —— ③停住就静止 ——
    idle_deltas = [idle_rows[i + 1]["actual"] - idle_rows[i]["actual"] for i in range(len(idle_rows) - 1)]
    _check(
        f"③{label}：鼠标停住后窗口静止（≤{EPS:g}px）",
        all(abs(d) <= EPS for d in idle_deltas),
        f"停住后位移 {[round(d) for d in idle_deltas]}",
    )

    # —— ④末位跟手 ——
    last_row = rows[-1]
    _check(
        f"④{label}：末位 = 起点 + 总位移（≤{EPS:g}px）",
        abs(last_row["actual"] - last_row["expect"]) <= EPS,
        f"偏差 {last_row['actual'] - last_row['expect']:+.1f}px",
    )
    return rows


def check_drag_watchdog(backend, number: int, real_button_down) -> None:
    """反向控制：一次**丢失的 mouseUp** 必须被拖拽兜底收掉。

    仿真的其余部分把钩子钉成「一直按着」（没有真鼠标），这里换回真实现 —— 无头脚本里真实现
    恒为 0，正好等于「mouseUp 丢了」那一刻的状态。不做这一步，兜底就是一段没人验证过的代码；
    而没有兜底，4ms 的拖拽圈速会让一次次丢失的 mouseUp 变成「~15% CPU 烧到重启、悬停/贴边
    全冻结」（2026-09-29 那个跑了 3.5 小时的遗留实例就是这类病）。
    """
    backend._mouse_button_down = real_button_down
    real = server_rect(number)
    assert real is not None, "窗口服务器读不到窗口"
    press = wl.Point(real[0] + real[2] / 2, real[1] + real[3] / 2)
    backend._mouse_down(event_for(press, backend._window_local))
    backend.linger(0.3)  # > _DRAG_BUTTON_UP_GRACE(80ms)
    _check(
        "⑤丢失的 mouseUp 被兜底收尾（0.3s 内 _dragging 归 False）",
        backend._dragging is False,
        f"_dragging={backend._dragging}",
    )

    # ⑥ 另一半反向控制：**程序化 API 不许被兜底收掉**（@lead 找到的那条回归）——
    #    start_drag() 只是「等价于按下」，这条路上没有真按钮，兜底必须放过它。
    #    去掉 from_mouse 区分时这条会红，而 verify_drag_tracking.py --real 也会跟着红。
    backend.start_drag()
    backend.linger(0.3)
    _check(
        "⑥程序化 start_drag() 不被兜底误收（0.3s 后 _dragging 仍为 True）",
        backend._dragging is True,
        f"_dragging={backend._dragging}",
    )
    backend.end_drag()
    backend._mouse_button_down = lambda: True


def main() -> int:
    character = load_character()
    # 指针钉在屏幕外：别让悬停/贴边逻辑插进来
    cursor = [wl.Point(-1000.0, -1000.0)]
    backend = AppKitBackend(cursor=lambda: cursor[0])
    # 假事件里没有真鼠标：把「键是否按着」的钩子钉成「一直按着」。不钉住的话，拖拽兜底
    # （appkit._DRAG_BUTTON_UP_GRACE=80ms）会把这里的仿真拖动当「mouseUp 丢了」收掉。
    # ⑤ 会换回真钩子做反向控制。
    real_button_down = backend._mouse_button_down
    backend._mouse_button_down = lambda: True
    try:
        backend.render(
            Render(event=StatusEvent(source="demo", state=State.IDLE), character=character)
        )
        backend.linger(0.3)
        screen = backend._space().screen
        number = int(backend.probe()["window_number"])

        # 先把窗口摆到屏幕正中（程序化路径，不是被测对象），确保展开且没停靠
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
        backend.linger(0.3)
        print(
            f"-- 起点 {backend._window_local} 停靠={backend.probe().get('dock')} "
            f"锚点文件={os.environ['XIAOCC_ANCHOR_FILE']} --"
        )

        run_axis(backend, number, "水平", 0, STEP_PX * STEPS)
        cursor[0] = wl.Point(-1000.0, -1000.0)
        backend.linger(0.2)
        run_axis(backend, number, "垂直", 1, STEP_PX * STEPS)
        cursor[0] = wl.Point(-1000.0, -1000.0)
        backend.linger(0.2)
        check_drag_watchdog(backend, number, real_button_down)
    finally:
        backend.close()
        cursor[0] = wl.Point(-1000.0, -1000.0)

    print()
    if failures:
        print(f"结果：FAIL（{len(failures)} 项）—— " + "；".join(failures))
        return 1
    print(f"结果：PASS（{len(checks)}/{len(checks)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
