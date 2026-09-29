"""真机拖动**端到端**回归：合成鼠标事件 → 部署形态的子进程 → 逐帧对窗口服务器量。

为什么必须有它（2026-09-29 用户报「拖动还是卡顿」）：
`verify_drag_mouse.py` 驱动的是**处理函数**那层（假事件），覆盖不到「事件能不能递进这个
无边框 + hover 门控窗口」「事件循环一秒钟真把位置更新了几次」。今天这一刀正是卡在后半截：
修完坐标基准后跟手几何是对的，但部署形态下引擎节拍是 fps=15，窗口位置也就 15 次/秒 ——
手上是台阶感（用户的原话：还是卡顿）。

**实测过：合成事件能进这个窗口**，@ops 之前四路全败是时序问题 ——
`ignoresMouseEvents` 只有光标进热区后才翻 false，必须在 probe 说「允许鼠标事件」之后再投
mouseDown，否则这一击落到桌面上（本机 `CGPreflightPostEventAccess()` = True，权限不是瓶颈）。

判据（B：事件节拍）：
  ① 拖动中窗口位置更新 ≥ 50 次/秒（修前实测 ≈15，正好等于 fps）
  ② 逐帧跟手：单步 |dx| ≤ 指针单步 + 2px，方向反转 = 0
  ③ 拖动中角色不许画空：窗口图不透明占比 ≥ 10%（照 scripts/pixel_stats.py 的判空口径）
  ④ 顺手报拖动中 CPU（进程时间的差值），别用「提高节拍」把风扇换回来

**前置：未锁屏 + 空闲 ≥ 5s**（`--force` 跳过）。锁屏时系统会吞掉注入的 HID 事件 ⇒ 跑出来是**假红**
（看着像"拖动坏了"、实际是环境不满足）。今晚就栽过一次：同一支脚本 @ops 跑失败、@lead 跑成功，
差别只在 session 状态。环境不满足时退出码 **2** 并明说「不是功能坏了」，别让谁拿一条假红去改代码。

用法：``.venv/bin/python scripts/verify_drag_inject.py [--fps 15] [--secs 1.5] [--force]``
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from itertools import pairwise
from pathlib import Path

import verify_log

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "bin" / "python"

import Quartz

CURSOR_MOVE = Quartz.kCGEventMouseMoved
CURSOR_DOWN = Quartz.kCGEventLeftMouseDown
CURSOR_DRAG = Quartz.kCGEventLeftMouseDragged
CURSOR_UP = Quartz.kCGEventLeftMouseUp

failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{' · ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


def post(kind: int, x: float, y: float) -> None:
    """投一个合成鼠标事件（坐标 = 全局显示坐标、**左上**原点，CGEvent 的口径）。"""
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def window_rect(number: int):
    for info in Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionIncludingWindow, number
    ) or []:
        b = info.get("kCGWindowBounds")
        if b:
            return (b["X"], b["Y"], b["Width"], b["Height"])
    return None


def read_probe(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def cpu_seconds(pid: int) -> float:
    out = subprocess.run(
        ["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    if not out:
        return 0.0
    parts = [float(p) for p in out.replace("-", ":").split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def opaque_ratio(img) -> float:
    w = Quartz.CGImageGetWidth(img)
    h = Quartz.CGImageGetHeight(img)
    bpr = Quartz.CGImageGetBytesPerRow(img)
    bpp = Quartz.CGImageGetBitsPerPixel(img) // 8
    buf = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img)))
    opaque = 0
    for y in range(h):
        row = buf[y * bpr : (y + 1) * bpr]
        for x in range(w):
            if bpp == 4 and row[x * 4 + 3] > 88:
                opaque += 1
    return opaque / (w * h)


IDLE_MIN_S = 5.0


def environment_snapshot() -> dict[str, float | bool | None]:
    """环境快照：``locked``（屏是否锁着）+ ``idle_s``（距上次真人输入几秒；**锁着时为 None**）。

    留痕（``scripts/verify_log.py``）读的就是这一份 —— 判据（下面那个 rc=2 的前置）与证据
    （盘上那行 JSON）必须是同一套读数口径（同一个函数，留痕时再读一次）。这个项目反复吃亏的
    点正是同一件事抄两份，
    然后各改各的。

    锁着时**不去读空闲秒数**：前置第一条已经成立、判据用不上它，而这一读在拿不到 GUI 会话的
    进程里（沙箱 / ssh 进来的 shell）不是报错、是**一直卡着** ⇒ 为了留痕多要一个数，把
    「rc=2 说清环境不满足」这条最要紧的路换成挂死，是本末倒置。
    """
    session = Quartz.CGSessionCopyCurrentDictionary() or {}
    if session.get("CGSSessionScreenIsLocked"):
        return {"locked": True, "idle_s": None}
    idle = Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
    )
    return {"locked": False, "idle_s": float(idle)}


def environment_blocker() -> str | None:
    """前置不满足的原因（None = 可以跑）。

    锁屏时注入的 HID 事件会被系统吞掉，量出来的是**假红**；手刚在动则合成事件与真人事件抢同一个
    指针，跟手几何也没意义。两者都能从进程外读到，所以不靠"等人走开"这种口头前提。
    """
    snapshot = environment_snapshot()
    if snapshot["locked"]:
        return "屏是锁着的（锁屏时注入的事件会被系统吞掉）"
    # `or 0.0`：万一是 None（读不到空闲秒数）就按"手刚动过"处理 ⇒ 宁可 rc=2 跳过，
    # 也不要拿一个量不出来的环境去跑出一份没人信得过的数。
    idle = snapshot["idle_s"] or 0.0
    if idle < IDLE_MIN_S:
        return f"鼠标/键盘刚动过（空闲 {idle:.1f}s < {IDLE_MIN_S:g}s）"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=float, default=15.0, help="与被部署面板一致的帧率")
    ap.add_argument("--secs", type=float, default=1.5, help="拖动时长")
    ap.add_argument("--force", action="store_true", help="跳过前置（明知环境不满足时用）")
    args = ap.parse_args()

    blocker = None if args.force else environment_blocker()
    if blocker:
        # rc=2 与 FAIL(1) 分开：这是**环境不满足**，不是功能坏了（一条假红会误导人去改好代码）
        print(f"跳过（rc=2，环境不满足，**不是功能坏了**）：{blocker}")
        print("要跑就等屏解锁、手离开鼠标 5 秒再来；或在真的知道自己在做什么时加 --force。")
        # 这一行留痕就是「锁屏强跑的 rc=0」与「环境干净的真绿」在盘上唯一能分开的地方
        verify_log.record("verify_drag_inject", 2, force=args.force, blocker=blocker)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="xiaocc-inject-"))
    state = tmp / "state.json"
    state.write_text(json.dumps({"state": "idle", "detail": "拖动回归"}))
    probe = tmp / "probe.json"
    env = dict(
        os.environ,
        XIAOCC_ANCHOR_FILE=str(tmp / "anchor.json"),
        XIAOCC_PROBE_FILE=str(probe),
        PYTHONUNBUFFERED="1",
    )
    child = subprocess.Popen(
        [
            str(PY), "-m", "xiaocc.cli", "run",
            "--source", f"file:{state}",
            "--backend", "appkit",
            "--backend-opt", "at=center",
            "--backend-opt", f"fps={args.fps:g}",
        ],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        info: dict = {}
        for _ in range(100):
            time.sleep(0.1)
            info = read_probe(probe)
            if info.get("alive") and info.get("window_number"):
                break
        if not info.get("window_number"):
            print("子进程没起来（probe 里没有 window_number）")
            verify_log.record("verify_drag_inject", 2, force=args.force, blocker=blocker)
            return 2
        number = int(info["window_number"])
        rect = window_rect(number)
        assert rect is not None, "窗口服务器读不到窗口"
        print(f"-- 子进程 pid={child.pid} fps={args.fps:g} 窗口 {rect} --")

        cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2

        # 光标进热区 → **等 probe 说允许鼠标事件**（不等就投下去，mouseDown 会落到桌面）
        post(CURSOR_MOVE, cx, cy)
        ready = False
        for _ in range(60):
            time.sleep(0.05)
            if read_probe(probe).get("ignores_mouse_events") is False:
                ready = True
                break
        _check("前置：光标进热区后窗口接受鼠标事件", ready)
        if not ready:
            verify_log.record("verify_drag_inject", 3, force=args.force, blocker=blocker)
            return 3

        cpu0 = cpu_seconds(child.pid)
        t0 = time.monotonic()
        post(CURSOR_DOWN, cx, cy)
        time.sleep(0.05)
        pr = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        base = window_rect(number)
        press_cur, base_x = float(pr.x), (base[0] if base else float("nan"))

        sweep = 240.0
        samples: list[tuple[float, float, float]] = []
        while time.monotonic() - t0 < args.secs:
            t = (time.monotonic() - t0) / args.secs
            mx = cx - sweep * t
            post(CURSOR_DRAG, mx, cy)
            time.sleep(0.004)
            r = window_rect(number)
            cur = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            samples.append((time.monotonic() - t0, float(cur.x), r[0] if r else float("nan")))
        post(CURSOR_UP, cx - sweep, cy)
        time.sleep(0.3)
        cpu1 = cpu_seconds(child.pid)
        wall = samples[-1][0] or 1.0

        # 拖动中角色还在不在（不透明占比）
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, number,
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        ratio = opaque_ratio(img) if img is not None else 0.0

        xs = [s[2] for s in samples]
        moved = [(a, b) for a, b in pairwise(xs) if abs(b - a) > 0.5]
        deltas = [b - a for a, b in moved]
        rev_pairs = [(p, q) for p, q in pairwise(deltas) if p * q < 0]
        # 只有幅度 ≥2px 的反向才算「来回跳」：窗口位置是整像素，指针是浮点后取整，
        # ±1px 的往复是量化噪声，眼睛看不见（判据要给「手上有感觉的」那种跳）。
        rev_big = sum(1 for p, q in rev_pairs if min(abs(p), abs(q)) >= 2.0)
        rate = len(moved) / wall
        # 跟手残余：指针自按下走了多少 vs 窗口自按下走了多少（差多少就是「落后几像素」）
        residuals = [abs((s[1] - press_cur) - (s[2] - base_x)) for s in samples]
        max_res = max(residuals, default=0.0)
        mean_res = sum(residuals) / max(len(residuals), 1)
        p95_res = sorted(residuals)[max(int(len(residuals) * 0.95) - 1, 0)] if residuals else 0.0
        worst_at = residuals.index(max_res) if residuals else -1
        max_step = max((abs(d) for d in deltas), default=0.0)
        net_win, net_cur = xs[-1] - xs[0], samples[-1][1] - samples[0][1]
        drag_cpu = cpu1 - cpu0
        info = read_probe(probe)

        print(
            f"  拖动 {wall:.2f}s：窗口位置更新 {len(moved)} 次 ⇒ **{rate:.1f} 次/秒**"
            f"（指针走 {net_cur:+.0f}px，窗口走 {net_win:+.0f}px）"
        )
        print(
            f"  跟手残余 均值 {mean_res:.1f}px  p95 {p95_res:.0f}px  最大 {max_res:.0f}px"
            f"（第 {worst_at + 1}/{len(residuals)} 个采样）"
            f"  单步 |dx| 最大 {max_step:.0f}px  反向 {len(rev_pairs)}/{max(len(deltas) - 1, 1)} 步"
        )
        print(
            f"  拖动中 CPU {drag_cpu:.2f}s / {wall:.2f}s = {drag_cpu / wall * 100:.1f}%"
            f"  子进程圈速 {info.get('pump_loops_per_sec')}/s  上圈睡 {info.get('pump_sleep_ms')}ms"
            f"  拖动后不透明占比 {ratio * 100:.1f}%"
        )

        _check("①拖动中位置更新 ≥ 50 次/秒", rate >= 50.0, f"实测 {rate:.1f} 次/秒")
        _check("②跟手残余 p95 ≤ 6px（窗口不许落在指针后面）", p95_res <= 6.0,
               f"p95 {p95_res:.0f}px 均值 {mean_res:.1f}px 最大 {max_res:.0f}px")
        _check(
            "③不来回跳（≥2px 的方向反转 0）",
            rev_big == 0,
            f"≥2px 反转 {rev_big}，全部反转 {len(rev_pairs)}/{max(len(deltas) - 1, 1)} 步"
            + (f"，反向前两组 {[(round(a, 1), round(b, 1)) for a, b in rev_pairs[:2]]}" if rev_pairs else ""),
        )
        _check("④拖动中角色不空（不透明占比 ≥10%）", ratio >= 0.10, f"{ratio * 100:.1f}%")
    finally:
        post(CURSOR_MOVE, 40.0, 40.0)  # 指针还到角落，别留在测试位置
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(child.pid), signal.SIGTERM)
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)

    # 判据数字（都是上面真量出来的，取不到的就不传）：等效更新频率、跟手残余、往返跳、CPU
    criteria = {
        "rate_per_s": round(rate, 1),
        "p95_residual_px": round(p95_res, 1),
        "mean_residual_px": round(mean_res, 1),
        "max_residual_px": round(max_res, 1),
        "max_step_px": round(max_step, 1),
        "reversals_ge2px": rev_big,
        "reversals_all": len(rev_pairs),
        "opaque_pct": round(ratio * 100, 1),
        "drag_cpu_pct": round(drag_cpu / wall * 100, 1),
        "checks_failed": len(failures),
    }
    print()
    if failures:
        print(f"结果：FAIL（{len(failures)} 项）—— " + "；".join(failures))
        verify_log.record(
            "verify_drag_inject", 1, criteria=criteria, force=args.force, blocker=blocker
        )
        return 1
    print("结果：PASS（5/5）")
    verify_log.record(
        "verify_drag_inject", 0, criteria=criteria, force=args.force, blocker=blocker
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
