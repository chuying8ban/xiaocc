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

import json
import logging
import os
import sys
import tempfile
import time
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
# 点击入口（⑦⑧）会让桌宠去「请求打开控制面板」—— 那两个文件同样指到临时目录，
# 并且伪造一个「已有面板在跑」的状态文件（pid 填本进程），免得检验本身拉出一个真窗口。
os.environ["XIAOCC_PANEL_REQUEST"] = str(_TMP / "panel.request")
os.environ["XIAOCC_PANEL_STATE"] = str(_TMP / "panel.json")
# 新增的三个交互要读设置、要读额度条 —— 同样不许碰用户真实的那两份文件
os.environ["XIAOCC_SETTINGS_FILE"] = str(_TMP / "settings.json")
os.environ["XIAOCC_QUOTA_FILE"] = str(_TMP / "quota.json")

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


def check_tap_opens_panel(backend, number: int) -> None:
    """⑦⑧ 单击 vs 拖一把：**只有「按下到松开没动」**才算点击（拖动绝不许触发动作）。

    单击的语义在 2026-09-29 改了：单击 = 按设置显示（默认额度条），**开面板归双击/右键**。
    所以这条判据跟着改 —— 它守的是「拖动不许被当成点击」这个反向控制，
    以及「单击不该顺手把面板也开了」（那会和双击抢同一件事）。

    走的是真机同一条处理函数路径（``_mouse_down`` / ``_mouse_dragged`` / ``_mouse_up``），
    而 ``request_open`` 落的是 ``XIAOCC_PANEL_REQUEST``（临时目录）+ 状态文件里伪造的活 pid，
    所以这条判据既不碰用户真实的面板状态、也不会真拉一个窗口起来。判据取双向：
    点一下必须请求、拖一把必须不请求 —— 单向的话「无脑开面板」也能绿。
    """
    from xiaocc.panel import paths

    req = paths.PANEL_REQUEST
    assert str(req).startswith(str(_TMP)), f"请求文件跑到临时目录外了：{req}"
    paths.PANEL_STATE.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8")

    real = server_rect(number)
    assert real is not None, "窗口服务器读不到窗口"
    center = wl.Point(real[0] + real[2] / 2, real[1] + real[3] / 2)

    req.unlink(missing_ok=True)
    backend._last_click_at = 0.0
    backend._mouse_down(event_for(center, backend._window_local))
    backend.linger(0.04)
    backend._mouse_up(None)
    backend.linger(0.05)
    _check(
        "⑦单击 ⇒ 不开面板（单击是「按设置显示」，开面板归双击）",
        not req.exists(),
        f"{req.name} exists={req.exists()}",
    )

    req.unlink(missing_ok=True)
    backend._mouse_down(event_for(center, backend._window_local))
    backend._mouse_dragged(event_for(wl.Point(center.x + 40.0, center.y), backend._window_local))
    backend.linger(0.04)
    backend._mouse_up(None)
    backend.linger(0.05)
    _check(
        "⑧拖一把桌宠 ⇒ 不开面板（反向控制）",
        not req.exists(),
        f"{req.name} exists={req.exists()}（拖动被当成点击了）",
    )




def check_click_gestures(backend, number: int, cursor: list) -> None:
    """⑨~㉑ 单击弹气泡 / 双击开面板 / 慢点不算点击 / 设置能换动作 / 右键不进拖拽 /
    菜单只剩一条 / 气泡两行且放得下 / 5 秒 + 淡化（淡化中指纹必须逐帧变）。

    为什么这几条必须走**真机同一条处理函数路径**：这三种手势是从同一串
    ``mouseDown_/mouseUp_`` 事件上分出来的，拿「直接调 _show_badge()」去验等于验了个别的。
    设置和额度文件都指到临时目录（见文件头），所以这里既不碰用户真实设置、也不会真拉窗口。
    """
    import json as _json

    from xiaocc import settings as settings_store
    from xiaocc.backends import appkit
    from xiaocc.panel import paths

    req = paths.PANEL_REQUEST
    assert str(req).startswith(str(_TMP)), f"请求文件跑到临时目录外了：{req}"
    paths.PANEL_STATE.write_text(
        _json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8"
    )

    real = server_rect(number)
    assert real is not None, "窗口服务器读不到窗口"
    center = wl.Point(real[0] + real[2] / 2, real[1] + real[3] / 2)

    def click(hold: float = 0.04) -> None:
        """按一下松开（按住 hold 秒）—— 走真机的 mouseDown_/mouseUp_ 路径。"""
        backend._mouse_down(event_for(center, backend._window_local))
        backend.linger(hold)
        backend._mouse_up(None)
        backend.linger(0.02)

    def tap(gap: float = 0.0) -> None:
        """单击（gap=0）或双击（gap=两下之间的间隔，必须 < 系统双击间隔）。"""
        backend._last_click_at = 0.0  # 每条判据自己起手，别吃上一条的余温
        click()
        if gap:
            backend.linger(gap)
            click()

    # —— ⑨ 单击（默认设置）⇒ 额度条，而且不许污染 caption_drawn ——
    settings_store.save({"click_action": "badge"}, Path(os.environ["XIAOCC_SETTINGS_FILE"]))
    backend._reload_settings(force=True)
    fake_report = {
        "schema": 1,
        "services": [
            {"name": "DeepSeek", "state": "ok", "items": [{"value": 75.0, "unit": "CNY"}]}
        ],
    }
    Path(os.environ["XIAOCC_QUOTA_FILE"]).write_text(
        _json.dumps(fake_report, ensure_ascii=False), encoding="utf-8"
    )
    req.unlink(missing_ok=True)
    tap()
    backend.linger(0.06)
    probe = backend.probe()
    _check(
        "⑨单击 ⇒ 贴出额度条（走真事件路径）",
        bool(probe.get("badge_drawn")) and "75.00" in str(probe.get("badge_drawn")),
        f"badge_drawn={probe.get('badge_drawn')!r} click_action={probe.get('click_action')!r}",
    )
    _check(
        "⑩额度条不占用 caption 那条带（待机时不挂文案的守卫还在）",
        not probe.get("caption_drawn"),
        f"caption_drawn={probe.get('caption_drawn')!r}",
    )

    # —— ⑪ 双击（两下间隔 < 系统双击间隔）⇒ 收起额度条 + 请求开面板 ——
    req.unlink(missing_ok=True)
    tap(gap=0.15)
    backend.linger(0.06)
    probe = backend.probe()
    _check(
        "⑪双击 ⇒ 收起额度条 + 请求打开控制面板",
        req.exists() and not probe.get("badge_drawn"),
        f"request={req.exists()} badge_drawn={probe.get('badge_drawn')!r}",
    )

    # —— ⑫ 按住 1.6s（>1.5s 上限）⇒ 什么都不做 ——
    req.unlink(missing_ok=True)
    backend._last_click_at = 0.0
    click(1.6)
    backend.linger(0.06)
    probe = backend.probe()
    _check(
        "⑫按住 1.6s 的慢点 ⇒ 既不出额度条也不开面板",
        (not req.exists()) and not probe.get("badge_drawn"),
        f"request={req.exists()} badge_drawn={probe.get('badge_drawn')!r}",
    )

    # —— ⑬ 设置成「状态文案」⇒ 贴的是文案、不是余额；设成「不显示」⇒ 什么都不贴 ——
    settings_store.save({"click_action": "caption"}, Path(os.environ["XIAOCC_SETTINGS_FILE"]))
    backend._reload_settings(force=True)
    backend.render(Render(event=StatusEvent(source="demo", state=State.WORKING), character=load_character()))
    backend.linger(0.2)
    tap()
    backend.linger(0.06)
    drawn = str(backend.probe().get("badge_drawn") or "")
    _check(
        "⑬设置=状态文案 ⇒ 贴文案（不是余额）",
        bool(drawn) and "75.00" not in drawn,
        f"badge_drawn={drawn!r}",
    )
    settings_store.save({"click_action": "none"}, Path(os.environ["XIAOCC_SETTINGS_FILE"]))
    backend._reload_settings(force=True)
    backend._hide_badge()
    tap()
    backend.linger(0.06)
    _check(
        "⑭设置=不显示 ⇒ 单击什么都不贴",
        not backend.probe().get("badge_drawn"),
        f"badge_drawn={backend.probe().get('badge_drawn')!r}",
    )

    # —— ⑮ 右键：不进拖拽（位置不动、不是在拖），菜单动作真能开面板 ——
    before = backend._window_local
    req.unlink(missing_ok=True)
    backend._dragging = False
    backend._right_mouse_down(FakeMouseEvent(center, backend._window_local), view=None)
    backend.linger(0.05)
    after = backend._window_local
    _check(
        "⑮右键不进拖拽路径（窗口不动、没在拖）",
        (not backend._dragging) and abs(before.x - after.x) < 0.5 and abs(before.y - after.y) < 0.5,
        f"dragging={backend._dragging} {before.x:.1f}→{after.x:.1f}",
    )
    menu = backend._build_menu()
    titles = [str(menu.itemAtIndex_(i).title()) for i in range(menu.numberOfItems())]
    _check(
        "⑯右键菜单只剩「打开控制面板」一条（用户明确不要菜单里的显示额度；设置已并进面板）",
        titles == ["打开控制面板"],
        f"菜单条目={titles}",
    )
    backend._open_panel()
    backend.linger(0.05)
    _check("⑰菜单「打开控制面板」⇒ 请求到面板", req.exists(), f"{req.name} exists={req.exists()}")

    # —— ⑱~㉑ 气泡：两行、每行放得下、5 秒、淡化（@researcher 那个「看不出错、只是没效果」的坑）——
    settings_store.save({"click_action": "badge"}, Path(os.environ["XIAOCC_SETTINGS_FILE"]))
    backend._reload_settings(force=True)
    backend._hide_badge()
    tap()
    backend.linger(0.06)
    probe = backend.probe()
    drawn = str(probe.get("badge_drawn") or "")
    lines = [part.strip() for part in drawn.split("/") if part.strip()]
    width = backend._bubble_text_width()
    _check(
        "⑱单击 ⇒ 弹的是两行对话气泡（不是一条字幕），且每行都放得下（不许出现半句话）",
        len(lines) == 2 and all(backend._text_fits(line, width) for line in lines),
        f"badge_drawn={drawn!r} 每行宽上限={width:.0f}px",
    )
    # 5 秒 + 最后一段淡化：把截止时刻拨到淡化窗口里，指纹必须**逐帧变**（只带文字的话它不变
    # ⇒ _paint 直接 return ⇒ 气泡卡在第一帧透明度、5 秒后硬切，正是要避免的观感）
    backend._badge_until = time.monotonic() + 0.6 * appkit._BADGE_FADE_S
    alpha_a = backend.probe().get("badge_alpha")
    fp_a = backend._fingerprint()
    backend.linger(0.12)
    alpha_b = backend.probe().get("badge_alpha")
    fp_b = backend._fingerprint()
    _check(
        "⑲淡化中指纹逐帧在变（不变量就永远不会重画 ⇒ 淡化静默失效）",
        fp_a != fp_b and alpha_a != alpha_b and 0.0 < float(alpha_b or 0) < 1.0,
        f"alpha {alpha_a}→{alpha_b}，指纹 {'变了' if fp_a != fp_b else '没变'}",
    )
    # 淡完之后：气泡要从画面上、从状态里都退干净（否则指纹永远比安静态多一项、重绘不停）
    live_sig = backend._badge_signature()  # 此刻气泡还挂着（alpha>0）——退干净后它必须消失
    backend._badge_until = time.monotonic() - 0.01
    backend.linger(0.08)
    backend._expire_badge()
    idle_fp = backend._fingerprint()
    probe = backend.probe()
    # 指纹整条不能直接比两次：动画相位/光标热区这些项本来就会随时间变。要比的是**气泡那一项**
    # ——「还拿着气泡时的签名」必须已经不在里面，且它换成了空档（否则重绘不会停）。
    _check(
        "⑳淡完 ⇒ 气泡从画面与状态里都退干净（badge_drawn 空、指纹里那一项回到空档）",
        (not probe.get("badge_drawn"))
        and not backend._badge_lines
        and not backend._badge_active()
        and (live_sig not in idle_fp)
        and ("" in idle_fp),
        f"badge_drawn={probe.get('badge_drawn')!r} alpha={probe.get('badge_alpha')} "
        f"lines={backend._badge_lines!r} 气泡签名还在指纹里={live_sig in idle_fp}",
    )
    _check(
        "㉑气泡在屏上 5 秒（用户指定），最后 1.2 秒用来淡化",
        (appkit._BADGE_TTL_S, appkit._BADGE_FADE_S) == (5.0, 1.2),
        f"TTL={appkit._BADGE_TTL_S} 淡化={appkit._BADGE_FADE_S}",
    )

    # —— ㉒~㉔ 单击不许把桌宠收回去（用户 19:10 报的真 bug）——
    # 旧行为：`_mouse_up` 无条件 `end_drag()` ⇒ `dock.drop()` ⇒ 桌宠本来就在边上，
    # 「按下即松手」被判成「扔到边上」⇒ 立刻收成 12px 把手条，气泡还画在那条 12px 里。
    def click_here(hold: float = 0.04) -> None:
        """在**当前**窗口中心点一下（`click()` 用的是函数入口那个 center，窗口挪了就不准）。"""
        here = backend._window_local.center
        backend._mouse_down(event_for(here, backend._window_local))
        backend.linger(hold)
        backend._mouse_up(None)
        backend.linger(0.02)

    space = backend._space()
    anchor_file = Path(os.environ["XIAOCC_ANCHOR_FILE"])
    body = backend._body_in_screen()
    # ① 摆到右边缘**内侧一点点**（还在吸附距离内）—— 用户平时就是这么放的。
    # 两个必须做的准备，否则判据测的不是点击：`reason="drag"`（别的理由会被防漂移守卫当场
    # 回锚）+ 把它落成新家（否则下一秒的漂移自检照样回锚）+ 光标停在它身上（贴边靠悬停判定，
    # 光标钉在屏外的话下一拍就把贴着边的窗口收起来了）。
    near = wl.Rect(
        space.screen.x + space.screen.width - body.width - backend.snap_distance * 0.5,
        body.y,
        body.width,
        body.height,
    )
    backend._set_window_rect(near, reason="drag")
    backend._save_anchor_from_window()
    backend._dock.reset()  # 「用户把它摆到边上、还没松手贴边」的那一刻
    cursor[0] = wl.Point(near.center.x, near.center.y)
    backend.linger(0.25)
    backend._hide_badge()
    anchor_before = anchor_file.read_bytes() if anchor_file.exists() else b""
    click_here()
    backend.linger(0.25)
    now_local = backend._window_local
    _check(
        "㉒桌宠停在屏幕边内侧时单击 ⇒ **不收成把手条**（宽度不变、没落边）",
        abs(now_local.width - near.width) < 1.0 and backend._dock.state == "floating",
        f"宽 {near.width:.0f}→{now_local.width:.0f} dock={backend._dock.state}",
    )
    anchor_after = anchor_file.read_bytes() if anchor_file.exists() else b""
    _check(
        "㉓单击也不许动锚点（点击不是搬家：不落边、不写锚点）",
        anchor_before == anchor_after,
        f"锚点文件 {'没动' if anchor_before == anchor_after else '被改了'}",
    )
    # ② 真的收成把手条（12px）时点一下 ⇒ 展开成完整角色 + 气泡在完整窗口里，且不回缩
    backend._dock.edge = wl.Edge.RIGHT
    backend._dock.docked = True
    backend._dock.collapsed = True
    backend._dock.armed = False
    backend._set_window_rect(
        wl.collapsed_rect(wl.Edge.RIGHT, space.screen, backend._body_in_screen()),
        reason="collapse",
    )
    backend.linger(0.2)
    strip_w = backend._window_local.width
    backend._hide_badge()
    click_here()
    backend.linger(0.25)
    expanded = backend._window_local.width
    _check(
        "㉔收起成把手条时单击 ⇒ 展开成完整角色 + 气泡（否则「单击显示额度」在最常见的状态下等于没做）",
        strip_w < 40.0 and expanded > 100.0 and bool(backend._badge_lines),
        f"把手条 {strip_w:.0f}px → 点击后 {expanded:.0f}px 气泡={backend._badge_lines}",
    )

    # —— ㉕ 按压边界必须能机器切段：`按下` / `按下收尾` 两行在 **INFO 级**真的产出 ——
    # 以前边界只有 debug 级的收尾行 ⇒ INFO 采集永远看不到，解析器只能靠坐标猜（同一份日志
    # 被数成 227/245/249、68 段 ≤4px 只能人眼对，根因都是这个）；而「位移=」原本只活在
    # tap 分支里 ⇒ 出行的样本按构造成全 ≤3px（幸存者偏差）。这条判据用**只收 INFO 的
    # handler** 抓，谁把它们降回 debug 这条就红 —— 那是刻意的。
    grabbed: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # 别的模块的 msg 里可能带 `%`，格式化会炸 ⇒ 兜住，只有我们要的那几行的形状要紧
            try:
                grabbed.append(record.getMessage())
            except (TypeError, ValueError):
                grabbed.append(str(record.msg))

    grab_handler = _Grab(level=logging.INFO)
    xiaocc_log = logging.getLogger("xiaocc")
    old_level = xiaocc_log.level
    xiaocc_log.setLevel(logging.INFO)  # 与部署一致（INFO；--verbose 才 DEBUG）
    xiaocc_log.addHandler(grab_handler)
    try:
        click_here()
        target = backend._window_local.center
        backend._mouse_down(event_for(target, backend._window_local))
        backend._mouse_dragged(
            event_for(wl.Point(target.x + 40.0, target.y), backend._window_local)
        )
        backend.linger(0.05)
        backend._mouse_up(None)
        backend.linger(0.2)
    finally:
        xiaocc_log.removeHandler(grab_handler)
        xiaocc_log.setLevel(old_level)
    joined = "\n".join(grabbed)
    _check(
        "㉕按压边界两行在 INFO 级都产出（tap / drag 两支都有，判据量 `位移=` 也在拖动那支）",
        "按下：屏幕=" in joined
        and "按下收尾：判定=tap" in joined
        and "按下收尾：判定=drag" in joined
        and "拖拽结束：落边=" in joined,
        f"抓到 {len(grabbed)} 行 · "
        + " | ".join(r for r in grabbed if r.startswith(("按下", "拖拽结束")))[:160],
    )



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
        check_tap_opens_panel(backend, number)
        backend.render(Render(event=StatusEvent(source="demo", state=State.IDLE), character=character))
        backend.linger(0.2)
        check_click_gestures(backend, number, cursor)
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
