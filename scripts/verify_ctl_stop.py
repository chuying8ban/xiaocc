"""「退出小cc」的**收尾回归**：守卫必须跟着走，且退出不等于「永久禁用」。

为什么要有这一支（2026-09-29 真机实测）：`xiaocc stop` = `launchctl bootout` 主作业，而睡眠闸守卫
（`ai.hermes.mascot-pet.keepawake`）挂在 `KeepAlive.OtherJobEnabled` 上 —— 实测 **bootout 主作业
不会让守卫退**（照旧持有 `PreventUserIdleSystemSleep`）。真机后果：用户点了「退出」，pid 76657 的
断言又连续持有了 4h22m、跨过一次真退出 ⇒ **以为关了，机器还被按着不睡**。修法是 `cmd_stop` 里
显式幂等 bootout 守卫。

判据（全部用**可抛标签**跑，绝不碰真作业；`ai.hermes.xiaocc*` 一律不出现）：
  ① 只 bootout 主作业（修改前的行为）⇒ 守卫**仍在**、断言**仍被持有**  ← 反向控制：证明那行是承重的
  ② 再 bootout 守卫（现在的行为）⇒ 守卫不可见、`pgrep` 空、**断言回落到基线**
  ③ 两个可抛标签都不在 `launchctl print-disabled` 里  ← "退出 ≠ 永久禁用"（下次开机能回来）
  ④ `ops/xiaoccctl` 的 `cmd_stop` 里确实有对 `$GUARD_LABEL` 的 bootout（防被谁顺手删掉）
  ⑤ 跑完不留残留（可抛作业全清、plist 删除、断言回基线）

局限（说清楚，别当成"真机验证"）：这支守的是**原语 + 脚本文本**；真作业那一枪（真停 5 秒、
真重拉）由 @ops 的真机往返负责，因为 `LABEL` / `PLIST_DST` 目前没有沙箱缝，脚本没法在沙箱里跑
完整的 `cmd_stop`。

用法::

    .venv/bin/python scripts/verify_ctl_stop.py
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENTS = Path.home() / "Library" / "LaunchAgents"
DOMAIN = f"gui/{os.getuid()}"

PET = "ai.hermes.probe.ctlstop.pet"
GUARD = "ai.hermes.probe.ctlstop.guard"
REAL_LABELS = ("ai.hermes.xiaocc", "ai.hermes.mascot-pet.keepawake")

failures: list[str] = []
checks: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{' · ' + detail if detail else ''}")
    checks.append(name)
    if not ok:
        failures.append(name)


def run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def loaded(label: str) -> bool:
    return run("launchctl", "print", f"{DOMAIN}/{label}").returncode == 0


def disabled_labels() -> str:
    return run("launchctl", "print-disabled", DOMAIN).stdout


def assertion_holders() -> set[int]:
    """正在持有 `PreventUserIdleSystemSleep` 的 **pid 集合**（守卫"按着机器不让睡"的外部判据）。

    为什么会话里不能用 `pmset` 汇总块那个数字：它的 `PreventUserIdleSystemSleep 1` **不是断言条数**
    —— 实测同一次 dump 的明细里有 5 条不同 pid 的断言、汇总却写 1（`grep -c` 数汇总行也一样错）。
    所以判据按**明细行的 pid** 收：我们的可抛 caffeinate 在不在这个集合里，是唯一稳妥的问法。
    """
    out = run("pmset", "-g", "assertions").stdout
    return {
        int(pid)
        for pid in re.findall(
            r"^\s+pid (\d+)\([^)]*\): .*PreventUserIdleSystemSleep", out, re.MULTILINE
        )
    }


def job_pid(label: str) -> int | None:
    out = run("launchctl", "print", f"{DOMAIN}/{label}").stdout
    match = re.search(r"^\s*pid = (\d+)", out, re.MULTILINE)
    return int(match.group(1)) if match else None


def write_plist(label: str, argv: list[str], *, keepalive_pet: str | None = None) -> Path:
    path = AGENTS / f"{label}.plist"
    body: dict = {"Label": label, "ProgramArguments": argv, "RunAtLoad": True}
    if keepalive_pet:
        body["KeepAlive"] = {"OtherJobEnabled": {keepalive_pet: True}}
    path.write_bytes(plistlib.dumps(body))
    return path


def unload(label: str) -> None:
    run("launchctl", "bootout", f"{DOMAIN}/{label}")
    run("launchctl", "remove", f"{DOMAIN}/{label}")
    path = AGENTS / f"{label}.plist"
    if path.exists():
        path.unlink()


def main() -> int:
    for label in (PET, GUARD):
        assert label not in REAL_LABELS, "可抛标签不许和真标签同名"
    for label in (PET, GUARD):
        unload(label)

    print(f"-- 开始前的 PreventUserIdleSystemSleep 持有者：{len(assertion_holders())} 个 --")
    try:
        paths = [
            write_plist(PET, ["/bin/sleep", "120"]),
            # 守卫的替身：真去持有一条「禁止空闲睡眠」断言（不然量不到外部后果）
            write_plist(GUARD, ["/usr/bin/caffeinate", "-i", "-s", "-t", "120"], keepalive_pet=PET),
        ]
        for path in paths:
            run("launchctl", "bootstrap", DOMAIN, str(path))
        time.sleep(1.5)
        gpid = job_pid(GUARD)
        _check(
            "前置：两个可抛作业都起来了，且守卫自己的 pid 持有一条断言",
            loaded(PET) and gpid is not None and gpid in assertion_holders(),
            f"pet={loaded(PET)} guard_pid={gpid} 持有断言={gpid in assertion_holders() if gpid else None}",
        )

        # ① 反向控制：只 bootout 主作业（= 修之前 cmd_stop 的行为）
        run("launchctl", "bootout", f"{DOMAIN}/{PET}")
        time.sleep(2.0)
        _check(
            "①只 bootout 主作业 ⇒ 守卫仍在、断言仍被持有（证明显式收尾是承重的）",
            loaded(GUARD) and gpid is not None and gpid in assertion_holders(),
            f"guard_loaded={loaded(GUARD)} guard_pid={gpid} 仍持有={gpid in assertion_holders()}",
        )

        # ② 现在 cmd_stop 的行为：再 bootout 守卫
        run("launchctl", "bootout", f"{DOMAIN}/{GUARD}")
        for _ in range(16):
            if not loaded(GUARD):
                break
            time.sleep(0.25)
        time.sleep(0.5)
        after = assertion_holders()
        _check("②bootout 守卫后：label 不可见", not loaded(GUARD), f"loaded={loaded(GUARD)}")
        # 注意：判"进程没了"必须按 pid —— 替身的命令行是 `caffeinate -i -s -t 120`，里面**没有**
        # 标签字符串，`pgrep -f <label>` 在它还活着时也返回空（那种判据永远不会红）。
        gone = gpid is not None and run("ps", "-p", str(gpid)).returncode != 0
        _check("②bootout 守卫后：那个进程没了（按 pid 查）", gone, f"pid={gpid} 还在={not gone}")
        _check(
            "②bootout 守卫后：那条断言被释放（机器可以睡了）",
            gpid not in after,
            f"仍持有={gpid in after}，全场持有者 {len(after)} 个",
        )

        # ③ 「退出 ≠ 永久禁用」：下次开机要能回来
        dis = disabled_labels()
        _check(
            "③两个可抛标签都不在 print-disabled 里（退出≠永久禁用）",
            PET not in dis and GUARD not in dis,
            "print-disabled 无命中" if PET not in dis and GUARD not in dis else dis.strip()[:120],
        )

        # ④ 脚本文本：cmd_stop 必须显式收尾守卫
        script = (REPO / "ops" / "xiaoccctl").read_text(encoding="utf-8")
        stop_body = script.split("cmd_stop()", 1)[-1].split("\n}", 1)[0]
        _check(
            "④`cmd_stop` 里确实 bootout 了 $GUARD_LABEL（防这行被删）",
            "bootout" in stop_body and "$GUARD_LABEL" in stop_body,
            f"守卫收尾出现 {stop_body.count('$GUARD_LABEL')} 次",
        )
    finally:
        for label in (PET, GUARD):
            unload(label)
        time.sleep(0.5)

    # ⑤ 残留
    leftovers = [p.name for p in AGENTS.glob("ai.hermes.probe.ctlstop.*")]
    _check("⑤跑完无残留（plist 与作业都清了）", not leftovers, f"leftovers={leftovers}")
    _check("⑤那个 pid 不再持有断言", gpid not in assertion_holders(), f"gpid={gpid}")

    print()
    if failures:
        print(f"结果：FAIL（{len(failures)} 项）—— " + "；".join(failures))
        return 1
    print(f"结果：PASS（{len(checks)}/{len(checks)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
