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
import json
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


def read_probe(path: Path, tries: int = 3, gap: float = 0.6) -> dict | None:
    """读显示层落盘的自证据（`XIAOCC_PROBE_FILE`）。

    为什么门槛必须同时看它：CPU 低有两种可能 —— 真的省，或者**被节流了**（动画其实在卡）。
    `pump_loops_per_sec` 是进程内结算的上一秒圈速，圈速 ≈ fps 才说明「画面照跑、只是不烧核」。
    """
    for _ in range(tries):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(gap)
            continue
        if data.get("pump_loops_per_sec"):
            return data
        time.sleep(gap)
    return None


def display_asleep() -> bool | None:
    """主显示器是不是睡着了 —— 屏幕一睡 CoreAnimation 就停画、数字会偏低，两臂必须在同一状态下取数。"""
    try:
        import Quartz
    except ImportError:  # pragma: no cover - 环境相关
        return None
    return bool(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))


def pid_of(label: str) -> int | None:
    out = subprocess.run(["launchctl", "print", f"gui/{UDID}/{label}"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            return int(line.split("=", 1)[1].strip())
    return None


def arm(name: str, process_type: str | None, seconds: float, warmup: float,
        source: str, fps: int) -> dict | None:
    if not ENTRY.is_file():
        sys.exit(f"找不到 venv 入口：{ENTRY}（先按 README 建 .venv）")
    label = f"{LABEL_PREFIX}-{'interactive' if process_type else 'plain'}"
    anchor = Path(tempfile.gettempdir()) / f"{label}-anchor.json"
    anchor.unlink(missing_ok=True)
    probe = Path(tempfile.gettempdir()) / f"{label}-probe.json"
    probe.unlink(missing_ok=True)
    plist: dict = {
        "Label": label,
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        "WorkingDirectory": str(REPO),
        "EnvironmentVariables": {"XIAOCC_ANCHOR_FILE": str(anchor),
                                 "XIAOCC_PROBE_FILE": str(probe),
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
        info = read_probe(probe) or {}
        loops = info.get("pump_loops_per_sec")
        slept = info.get("pump_sleep_ms")
        ratio = f"{loops / fps:.2f}x fps" if loops else "圈速读不到"
        print(f"{name:38s} ProcessType={process_type!s:12s} CPU={cpu:5.1f}%"
              f"  圈速={loops or '-'!s:>5s}/s（{ratio}）"
              f"  睡={slept if slept is not None else '-'}ms"
              f"  pid={pid}  累计 {t1}s → {t2}s")
        if loops is None:
            print(f"    ⚠️ {probe} 没读到圈速：这臂只能看 CPU，判不了「是不是被节流了」")
        return {"cpu": cpu, "loops": loops, "slept": slept}
    finally:
        subprocess.run(["launchctl", "bootout", f"gui/{UDID}/{label}"],
                       capture_output=True, text=True)
        time.sleep(2)
        path.unlink(missing_ok=True)
        anchor.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="ProcessType 对面板 CPU 的 A/B")
    ap.add_argument("--arm", choices=["both", "interactive", "plain", "adaptive", "all"], default="both")
    ap.add_argument("--seconds", type=float, default=45.0, help="测量窗口（默认 45s）")
    ap.add_argument("--warmup", type=float, default=15.0, help="暖机（默认 15s）")
    ap.add_argument("--source", default="hermes", help="状态源（默认 hermes）")
    ap.add_argument("--fps", type=int, default=30, help="面板帧率（默认 30）")
    args = ap.parse_args()

    asleep = display_asleep()
    state = {True: "主显示器已睡（CoreAnimation 会停画，数字偏低，两臂必须同状态取数）",
             False: "主显示器醒着", None: "读不到显示器状态（缺 Quartz）"}[asleep]
    print(f"仓库 {REPO}  源={args.source}  fps={args.fps}  测量窗口={args.seconds}s")
    print(f"显示器状态：{state}")
    results: dict[str, dict] = {}
    if args.arm in ("both", "interactive", "all"):
        v = arm("launchd + ProcessType=Interactive", "Interactive",
                args.seconds, args.warmup, args.source, args.fps)
        if v is not None:
            results["interactive"] = v
    if args.arm in ("both", "plain", "all"):
        v = arm("launchd 不带 ProcessType 键", None,
                args.seconds, args.warmup, args.source, args.fps)
        if v is not None:
            results["plain"] = v
    if args.arm in ("adaptive", "all"):
        # Adaptive 的语义正是桌宠想要的：前台全速、后台/无人时可节流
        v = arm("launchd + ProcessType=Adaptive", "Adaptive",
                args.seconds, args.warmup, args.source, args.fps)
        if v is not None:
            results["adaptive"] = v

    if results:
        base_key = "plain" if "plain" in results else next(iter(results))
        base = results[base_key]
        print("\n各臂对照（判据是**两件事一起过**：CPU < 5% 且 圈速 ≈ fps ——")
        print("只看 CPU 会把「卡成幻灯片但很省」判成通过，只看圈速会把忙等放过去）：")
        for key, val in results.items():
            loops = val["loops"]
            cpu_ok = val["cpu"] < 5.0
            rate_ok = loops is not None and 0.7 * args.fps <= loops <= 1.3 * args.fps
            why = []
            if not cpu_ok:
                why.append(f"CPU {val['cpu']:.1f}%")
            if not rate_ok:
                why.append("圈速读不到" if loops is None
                           else f"圈速 {loops:.0f}/s（{'被节流' if loops < 0.7 * args.fps else '忙等'}）")
            ratio = f"{val['cpu'] / base['cpu']:.2f}x vs {base_key}"
            print(f"  {key:12s} CPU={val['cpu']:5.1f}%  圈速={loops or '-'!s:>5s}/s"
                  f"  {ratio:22s}  {'✅ 过' if (cpu_ok and rate_ok) else '❌ ' + '；'.join(why)}")
    leftovers = sorted(p.name for p in AGENTS_DIR.glob(f"{LABEL_PREFIX}*"))
    print("残留 plist：", leftovers or "无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
