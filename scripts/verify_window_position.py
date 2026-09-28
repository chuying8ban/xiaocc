"""验证「初始停靠点」真的生效：从进程外抓窗口矩形 + 退出码，自己说话。

    .venv/bin/python scripts/verify_window_position.py [--capture]

为什么不用进程内的日志当唯一证据：日志是它自己报的，抓窗口是外部观察，
两者对上才算数。两个坐标源要清楚区别：

* 进程日志里的 `窗口就绪：160x194 @ Rect(...)` = 我们设置给 NSWindow 的布局矩形（权威）
* `CGWindowListCopyWindowInfo` 的 `kCGWindowBounds` = 窗口服务器看到的**内容包围盒**，
  会把四周全透明的留白裁掉（典型差 1~4px）。所以只做 ±4px 容差校验，
  拿它做等值比较会把自己误判成「位置不对」。

`--capture` 时顺带把每个方位的窗口抓成 PNG（透明通道保留），放在 docs/evidence/。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import Quartz

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python")
SCREEN_W, SCREEN_H = 1512.0, 982.0

#: 把锚点文件指到一个**不存在的**路径：这个脚本验的是 `at=`/默认角的算术，
#: 而用户拖动会把锚点落盘（见 scripts/verify_anchor.py），残留的锚点文件会让
#: 「默认右上角」那一例随机失败 —— 那不是回归，是测试自己没隔离。
ANCHOR_ENV = {
    **os.environ,
    "XIAOCC_ANCHOR_FILE": str(Path(tempfile.gettempdir()) / "xiaocc-verify-no-anchor.json"),
    # 自证据同理：这些用例会真启动面板，不隔离就会往用户真实的 ~/.xiaocc/probe.json 里写
    "XIAOCC_PROBE_FILE": str(Path(tempfile.gettempdir()) / "xiaocc-verify-probe.json"),
}
for _stale in (ANCHOR_ENV["XIAOCC_ANCHOR_FILE"], ANCHOR_ENV["XIAOCC_PROBE_FILE"]):
    Path(_stale).unlink(missing_ok=True)

#: (说明, 文件名用的 slug, 附加参数, 期望退出码, 期望布局左上角)
CASES: list[tuple[str, str, list[str], int, tuple[float, float] | None]] = [
    ("默认（不传选项）", "default-top-right", [], 0, (SCREEN_W - 160 - 28, 96.0)),
    ("at=top-left", "at-top-left", ["--backend-opt", "at=top-left"], 0, (28.0, 96.0)),
    ("at=bottom-left", "at-bottom-left", ["--backend-opt", "at=bottom-left"], 0,
     (28.0, SCREEN_H - 194 - 28)),
    ("at=bottom-right", "at-bottom-right", ["--backend-opt", "at=bottom-right"], 0,
     (SCREEN_W - 160 - 28, SCREEN_H - 194 - 28)),
    ("at=center", "at-center", ["--backend-opt", "at=center"], 0,
     ((SCREEN_W - 160) / 2, (SCREEN_H - 194) / 2)),
    ("at=120,300（绝对坐标）", "at-xy", ["--backend-opt", "at=120,300"], 0, (120.0, 300.0)),
    ("at=middle（非法取值）", "err-bad-value", ["--backend-opt", "at=middle"], 2, None),
    ("bogus=1（显示层不认识）", "err-unknown-opt", ["--backend-opt", "bogus=1"], 2, None),
    ("bottom-left（少了等号）", "err-bad-format", ["--backend-opt", "bottom-left"], 2, None),
]


def _sample_ours(pid: int, attempts: int = 3, gap: float = 0.35) -> list:
    """采样属于 pid 的窗口。

    连采两次并要求两次一致才采信：窗口刚上屏时窗口服务器偶尔会把它的坐标
    报成另一个 Space 的偏移值（实测见过 X=-301 / X=4440，而窗口本身没错），
    这种测量噪声不该被当成「位置不对」。
    """
    ours: list = []
    previous = None
    for _ in range(attempts):
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID)
        ours = [w for w in infos if w.get("kCGWindowOwnerPID") == pid]
        signature = [(w["kCGWindowNumber"], dict(w["kCGWindowBounds"])["X"],
                      dict(w["kCGWindowBounds"])["Y"]) for w in ours]
        if ours and signature == previous:
            return ours
        previous = signature
        time.sleep(gap)
    return ours


def _run_window_case(case, capture: bool):
    _label, slug, extra, want_code, want_xy = case
    cmd = [PY, "-m", "xiaocc.cli", "run", "--source", "hermes", "--backend", "appkit",
           "--once", "--linger", "3", *extra]

    if want_code != 0:                        # 错误路径：跑完拿退出码和提示语
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              timeout=60, check=False, env=ANCHOR_ENV)
        text = (proc.stderr or "") + (proc.stdout or "")
        lines = [line for line in text.strip().splitlines() if line.strip()]
        msg = next((line for line in lines if "选项" in line or "停靠点" in line), "")
        ok = proc.returncode == want_code and "Traceback" not in text
        return ok, f"exit={proc.returncode} · {msg.strip()[:96]}"

    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=ANCHOR_ENV)
    time.sleep(2.0)                           # 等窗口上屏
    ours = _sample_ours(proc.pid)
    # 抓图必须在进程还活着的时候做：窗口一销毁，CGWindowListCreateImage 只会返回 None
    shot_done = False
    if capture and ours:
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
            ours[0]["kCGWindowNumber"], Quartz.kCGWindowImageBoundsIgnoreFraming)
        if image is not None:
            dest_path = ROOT / "docs/evidence" / f"11-{slug}.png"
            url = Quartz.CFURLCreateWithFileSystemPath(None, str(dest_path),
                                                       Quartz.kCFURLPOSIXPathStyle, False)
            dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
            Quartz.CGImageDestinationAddImage(dest, image, None)
            shot_done = bool(Quartz.CGImageDestinationFinalize(dest))
    proc.terminate()
    out, _ = proc.communicate(timeout=15)

    match = re.search(r"窗口就绪：([\d.]+)x([\d.]+) @ Rect\(x=([-\d.]+), y=([-\d.]+)", out)
    if not match:
        return False, "日志里没有「AppKit 窗口就绪」那一行"
    x, y = float(match.group(3)), float(match.group(4))
    exact = abs(x - want_xy[0]) < 0.6 and abs(y - want_xy[1]) < 0.6

    seen = "没抓到窗口"
    if ours:
        bounds = dict(ours[0]["kCGWindowBounds"])
        seen = (f"窗口服务器看到 X={bounds['X']:.0f} Y={bounds['Y']:.0f}"
                f"（内容包围盒，允许缩小最多 4px）")
        # 两个坐标系都是「左上角为原点、y 向下」，不用换算
        if abs(bounds["X"] - x) > 4 or abs(bounds["Y"] - y) > 4:
            exact = False
            frames = ", ".join(
                f"id={w['kCGWindowNumber']}:X={dict(w['kCGWindowBounds'])['X']:.0f}"
                f"/Y={dict(w['kCGWindowBounds'])['Y']:.0f}"
                f"/layer={w.get('kCGWindowLayer')}" for w in ours)
            seen += f" · 抓到 {len(ours)} 个窗口 [{frames}]"
    if shot_done:
        seen += f" · 已抓图 11-{slug}.png"
    return exact, (f"布局 ({x:.0f},{y:.0f}) 期望 ({want_xy[0]:.0f},{want_xy[1]:.0f}) · {seen}")


def main() -> int:
    parser = argparse.ArgumentParser(description="验证显示层初始停靠点")
    parser.add_argument("--capture", action="store_true", help="顺带抓窗口截图到 docs/evidence/")
    args = parser.parse_args()

    print(f"== 初始停靠点验证（{len(CASES)} 例）==")
    results = []
    for case in CASES:
        ok, detail = _run_window_case(case, args.capture)
        results.append((case[0], detail, ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {case[0]}: {detail}")

    passed = sum(1 for _, _, ok in results if ok)
    print(f"== {passed}/{len(results)} 通过 ==")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
