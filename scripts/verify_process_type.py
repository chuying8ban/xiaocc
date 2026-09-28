#!/usr/bin/env python3
"""量「launchd 的 ProcessType 键」对面板 CPU 的影响（部署路径的真问题，不是代码问题）。

背景：同一份代码、同一个源、同一个 fps、同一个窗口位置，**前台手工启动 4.2%**，
而 ops 用 LaunchAgent 部署的那一份 **15.3%**。本脚本把两者的差异逐一排除后做 A/B：

  前台（workspace 默认）                 3.6%
  前台 + 项目目录 cwd + 最小 PATH + 日志文件   4.2%   → cwd / PATH / stdout 都不解释差异
  launchd + ProcessType=Interactive     16.4%   ← 真凶
  launchd 不带 ProcessType 键             4.6%   ← 去掉即回到前台水平
  （Interactive / 无键 = 3.57x，2026-09-29 实测，1512×982 屏、--source hermes、fps=30）

用法：
    python scripts/verify_process_type.py                # 两臂各 15s 暖机 + 45s 测量
    python scripts/verify_process_type.py --seconds 20    # 快速复跑
    python scripts/verify_process_type.py --arm plain     # 只跑一臂

安全：用一次性 Label `ai.hermes.xiaocc.proctypeprobe-*`，跑完 `launchctl bootout` + 删 plist，
不碰 `ai.hermes.xiaocc` 这个部署中的 job；锚点文件走 `XIAOCC_ANCHOR_FILE` 临时路径，
不会污染 `~/.xiaocc/anchor.json`。
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UDID = str(os.getuid())
LABEL_PREFIX = "ai.hermes.xiaocc.proctypeprobe"
AGENTS_DIR = Path.home() / "Library/LaunchAgents"
REPO = Path(__file__).resolve().parent.parent
ENTRY = REPO / ".venv/bin/xiaocc"


def cpu_seconds(pid: int) -> float | None:
    """取进程累计 CPU 时间（秒）。macOS `ps -o time=` 是 [[hh:]mm:]ss.ss。"""
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        return None
    total = 0.0
    for part in out.split(":"):
        total = total * 60 + float(part)
    return total


def pid_of(label: str) -> int | None:
    out = subprocess.run(["launchctl", "print", f"gui/{UDID}/{label}"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            return int(line.split("=", 1)[1].strip())
    return None


def arm(name: str, process_type: str | None, seconds: float, warmup: float,
        source: str, fps: int) -> float | None:
    if not ENTRY.is_file():
        sys.exit(f"找不到 venv 入口：{ENTRY}（先按 README 建 .venv）")
    label = f"{LABEL_PREFIX}-{'interactive' if process_type else 'plain'}"
    anchor = Path(tempfile.gettempdir()) / f"{label}-anchor.json"
    anchor.unlink(missing_ok=True)
    plist: dict = {
        "Label": label,
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        "WorkingDirectory": str(REPO),
        "EnvironmentVariables": {"XIAOCC_ANCHOR_FILE": str(anchor),
                                 "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "ProgramArguments": [str(ENTRY), "run", "--source", source, "-b", "appkit",
                             "--backend-opt", "at=bottom-right",
                             "--backend-opt", f"fps={fps}"],
        "StandardOutPath": str(Path(tempfile.gettempdir()) / f"{label}.log"),
        "StandardErrorPath": str(Path(tempfile.gettempdir()) / f"{label}.err.log"),
    }
    if process_type:
        # KeepAlive 与部署那份一致：只在异常退出时拉起
        plist["ProcessType"] = process_type
        plist["KeepAlive"] = {"SuccessfulExit": False}

    path = AGENTS_DIR / f"{label}.plist"
    path.write_bytes(plistlib.dumps(plist))
    boot = subprocess.run(["launchctl", "bootstrap", f"gui/{UDID}", str(path)],
                          capture_output=True, text=True)
    if boot.returncode != 0:
        path.unlink(missing_ok=True)
        print(f"{name}: bootstrap 失败 rc={boot.returncode} {boot.stderr.strip()}")
        return None
    try:
        time.sleep(warmup)                       # 冷启动不计
        pid = pid_of(label)
        if pid is None:
            print(f"{name}: job 起了但拿不到 pid")
            return None
        t1 = cpu_seconds(pid)
        time.sleep(seconds)
        t2 = cpu_seconds(pid)
        if t1 is None or t2 is None:
            print(f"{name}: 进程中途退出（看 {plist['StandardErrorPath']}）")
            return None
        cpu = (t2 - t1) / seconds * 100.0
        print(f"{name:38s} ProcessType={str(process_type):12s} CPU={cpu:5.1f}%"
              f"   pid={pid}  累计 {t1}s → {t2}s")
        return cpu
    finally:
        subprocess.run(["launchctl", "bootout", f"gui/{UDID}/{label}"],
                       capture_output=True, text=True)
        time.sleep(2)
        path.unlink(missing_ok=True)
        anchor.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="ProcessType 对面板 CPU 的 A/B")
    ap.add_argument("--arm", choices=["both", "interactive", "plain"], default="both")
    ap.add_argument("--seconds", type=float, default=45.0, help="测量窗口（默认 45s）")
    ap.add_argument("--warmup", type=float, default=15.0, help="暖机（默认 15s）")
    ap.add_argument("--source", default="hermes", help="状态源（默认 hermes）")
    ap.add_argument("--fps", type=int, default=30, help="面板帧率（默认 30）")
    args = ap.parse_args()

    print(f"仓库 {REPO}  源={args.source}  fps={args.fps}  测量窗口={args.seconds}s")
    results: dict[str, float] = {}
    if args.arm in ("both", "interactive"):
        v = arm("launchd + ProcessType=Interactive", "Interactive",
                args.seconds, args.warmup, args.source, args.fps)
        if v is not None:
            results["interactive"] = v
    if args.arm in ("both", "plain"):
        v = arm("launchd 不带 ProcessType 键", None,
                args.seconds, args.warmup, args.source, args.fps)
        if v is not None:
            results["plain"] = v

    if len(results) == 2:
        print(f"\nInteractive / 无键 = {results['interactive'] / results['plain']:.2f}x")
        ok = results["plain"] < 5.0
        print("门槛（无键那一路 < 5%，可比基准：小汐 4.6~5.0%）："
              + ("通过" if ok else f"未通过（{results['plain']:.1f}%）"))
    leftovers = sorted(p.name for p in AGENTS_DIR.glob(f"{LABEL_PREFIX}*"))
    print("残留 plist：", leftovers or "无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
