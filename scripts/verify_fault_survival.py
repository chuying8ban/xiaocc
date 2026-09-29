"""源坏了 / 文件没了 / 命令超时 —— 桌宠必须照样活着（⑧）。

机械故障（命令不存在、退出码非零、超时、文件没了、输出不是合法 JSON）不许进画面：
只留痕、只在没有任何源说话时以 offline 兜底、并把原因写进 detail。这里不是读代码下的结论，
而是**真的把每条坏源拉起来跑**，看进程还活着、画面还在动、退出干净。

    .venv/bin/python scripts/verify_fault_survival.py
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "bin" / "python"
failures: list[str] = []

# 每条：标签、--source 规格、观察秒数、detail 里该出现的关键字
CASES: list[tuple[str, str, float, str]] = [
    ("命令不存在", "command:no-such-cmd-xyz-12345", 4.0, "命令不存在"),
    ("退出码非零", "command:false", 4.0, "退出码"),
    ("命令超时", "command:sleep 30", 9.0, "TimeoutExpired"),
    ("文件没了", "file:/tmp/xiaocc-no-such-file.json", 4.0, "没有这个文件"),
    ("输出不是 JSON", "command:echo not-json-at-all", 4.0, "不合法"),
]


def run_case(label: str, spec: str, seconds: float, keyword: str) -> None:
    proc = subprocess.Popen(
        [str(PY), "-m", "xiaocc.cli", "run", "--source", spec, "--backend", "console"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(seconds)
    alive = proc.poll() is None
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    rc = proc.returncode
    frames = out.count("小cc") + out.count("( ")
    ok_alive = alive
    ok_offline = "离线" in out or "offline" in out
    ok_reason = keyword in out
    ok_exit = rc == 0
    ok_frames = frames >= 2
    detail = f"活着={ok_alive} 有离线态={ok_offline} 原因含「{keyword}」={ok_reason} " \
             f"帧数≈{frames} 退出码={rc}"
    print(f"  {'PASS' if all([ok_alive, ok_offline, ok_frames, ok_exit]) else 'FAIL'}  {label} · {detail}")
    if not all([ok_alive, ok_offline, ok_frames, ok_exit]):
        failures.append(label)
    if not ok_reason:
        print("        （原因关键字没出现，画面/日志实际内容见下）")
        print("        " + "\n        ".join(out.strip().splitlines()[-6:]))


def main() -> int:
    print("== 坏源下她还活着吗（真起进程跑）==")
    for label, spec, seconds, keyword in CASES:
        run_case(label, spec, seconds, keyword)
    print(f"\n结果：{'全部通过' if not failures else '失败 ' + '、'.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
