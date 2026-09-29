"""「退出小cc」的**收尾回归**：守卫必须跟着走，且退出不等于「永久禁用」。

两条路径，都要跑：

**A（真路径）**：用 `xiaoccctl` 的沙箱缝（`XIAOCC_LABEL` / `XIAOCC_GUARD_LABEL` /
`XIAOCC_STATE` / `XIAOCC_CTL_LOG` / `XIAOCC_PLIST[_SRC]`）把**完整的 `cmd_stop`** 跑在两个
可抛作业上 —— 断言：可抛作业双双卸载、守卫进程按 pid 消失、**它那条
`PreventUserIdleSystemSleep` 断言被释放**、沙箱留痕写着「睡眠闸守卫已随停」、沙箱
`state.json` 被写而**真 `state.json` 的 mtime 不动**、**真宠物 pid 没变**、可抛标签都不在
`print-disabled` 里。

**B（原语 + 反向控制）**：不碰脚本，直接用可抛标签复现平台语义 —— 只 `bootout` 主作业
（= 修之前 `cmd_stop` 的行为）⇒ 守卫**仍加载、仍持有断言** ⇒ 证明 A 里代那句守卫收尾是
**承重**的，不是装饰；同时也是"哪天 macOS 改了 `KeepAlive.OtherJobEnabled` 行为"的探测器。

为什么要有这一支（2026-09-29 真机实测）：`xiaocc stop` 原本只 `bootout` 主作业，而睡眠闸
守卫挂在 `KeepAlive.OtherJobEnabled` 上 —— 实测 **bootout 主作业不会让守卫退**。真机后果：
用户点了「退出」，pid 76657 的断言又连续持有 4h22m、跨过一次真退出 ⇒ **以为关了，机器还被
按着不睡**（守卫心跳日志 22:04 AC 90% → 23:04 BATT 64%，那两拍宠物根本不在）。

局限：`_window_json` / `panel.log` 这些没有缝的路径仍是真路径上的只读/旁路动作（不会改真
作业）；真作业那一枪（真停 5 秒、真重拉、锚点不变）由真机往返负责。

用法::

    .venv/bin/python scripts/verify_ctl_stop.py
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOMAIN = f"gui/{os.getuid()}"
REAL_PET = "ai.hermes.xiaocc"
REAL_GUARD = "ai.hermes.mascot-pet.keepawake"
REAL_STATE = Path.home() / "Library" / "Logs" / "xiaocc" / "state.json"

PET = "ai.hermes.tmp.ctlstop.pet"
GUARD = "ai.hermes.tmp.ctlstop.guard"
TOUCHED = (PET, GUARD)

SANDBOX = Path(os.environ.get("TMPDIR", "/tmp")) / "xiaocc_ctlstop_sandbox"

failures: list[str] = []
checks: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{' · ' + detail if detail else ''}")
    checks.append(name)
    if not ok:
        failures.append(name)


def run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False, env=env)


def loaded(label: str) -> bool:
    return run("launchctl", "print", f"{DOMAIN}/{label}").returncode == 0


def job_pid(label: str) -> int | None:
    out = run("launchctl", "print", f"{DOMAIN}/{label}").stdout
    match = re.search(r"^\s*pid = (\d+)", out, re.MULTILINE)
    return int(match.group(1)) if match else None


def alive(pid: int | None) -> bool:
    return pid is not None and run("ps", "-p", str(pid)).returncode == 0


def disabled_labels() -> str:
    return run("launchctl", "print-disabled", DOMAIN).stdout


def assertion_holders() -> set[int]:
    """正在持有 `PreventUserIdleSystemSleep` 的 **pid 集合**（守卫"按着机器不让睡"的外部判据）。

    不能用 `pmset -g assertions` 汇总块里那个数字：它的 `PreventUserIdleSystemSleep 1`
    **不是断言条数** —— 实测同一次 dump 的明细里有 5 个不同 pid 的持有者（`grep -c` 数汇总行
    同样错）。所以判据按明细行的 pid 收：我们的可抛 caffeinate 在不在这个集合里，是唯一稳妥的问法。
    """
    out = run("pmset", "-g", "assertions").stdout
    return {
        int(pid)
        for pid in re.findall(r"^\s+pid (\d+)\([^)]*\): .*PreventUserIdleSystemSleep", out, re.MULTILINE)
    }


def write_plist(label: str, argv: list[str], path: Path, *, keepalive_pet: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body: dict = {"Label": label, "ProgramArguments": argv, "RunAtLoad": True}
    if keepalive_pet:
        body["KeepAlive"] = {"OtherJobEnabled": {keepalive_pet: True}}
    path.write_bytes(plistlib.dumps(body))
    return path


def unload(label: str) -> None:
    run("launchctl", "bootout", f"{DOMAIN}/{label}")
    run("launchctl", "remove", f"{DOMAIN}/{label}")


def bootstrap(*paths: Path) -> None:
    for path in paths:
        run("launchctl", "bootstrap", DOMAIN, str(path))


def sandbox_pair(root: Path, seconds: str) -> tuple[Path, Path]:
    """建一对可抛作业的 plist：主作业（/bin/sleep）+ 守卫替身（caffeinate，真持有断言）。"""
    if root.exists():
        shutil.rmtree(root)
    pet = write_plist(PET, ["/bin/sleep", seconds], root / "pet.plist")
    guard = write_plist(
        GUARD, ["/usr/bin/caffeinate", "-i", "-s", "-t", seconds], root / "guard.plist", keepalive_pet=PET
    )
    for label in TOUCHED:
        unload(label)
    bootstrap(pet, guard)
    time.sleep(1.5)
    return pet, guard


# ── A：真路径（完整 `cmd_stop`，沙箱缝隔离） ──────────────────────────────────────


def path_a_real_stop() -> None:
    print("-- A：完整 `cmd_stop` 跑在可抛标签上（沙箱缝）--")
    sb = SANDBOX / "A"
    pet_plist, _ = sandbox_pair(sb, "300")
    state, ctl_log = sb / "state.json", sb / "ctl.log"

    real_pid_before = job_pid(REAL_PET)
    real_state_before = REAL_STATE.stat().st_mtime_ns if REAL_STATE.exists() else None
    gpid = job_pid(GUARD)
    _check(
        "A0 前置：可抛宠物+守卫都起来了，且守卫自己的 pid 持有一条断言",
        loaded(PET) and gpid is not None and gpid in assertion_holders(),
        f"pet={loaded(PET)} guard_pid={gpid} 持有断言={gpid in assertion_holders() if gpid else None}",
    )

    env = {
        **os.environ,
        "XIAOCC_LABEL": PET,
        "XIAOCC_GUARD_LABEL": GUARD,
        "XIAOCC_STATE": str(state),
        "XIAOCC_CTL_LOG": str(ctl_log),
        "XIAOCC_PLIST": str(sb / "dst.plist"),
        "XIAOCC_PLIST_SRC": str(pet_plist),
    }
    res = run("/bin/zsh", str(REPO / "ops" / "xiaoccctl"), "stop", env=env)
    _check(
        "A1 `cmd_stop`（真路径）rc=0 且打印「已停止」",
        res.returncode == 0 and "已停止" in res.stdout,
        f"rc={res.returncode} stdout={res.stdout.strip()[:60]!r} stderr={res.stderr.strip()[:60]!r}",
    )
    _check("A2 可抛宠物与守卫都不再加载", not loaded(PET) and not loaded(GUARD), f"pet={loaded(PET)} guard={loaded(GUARD)}")
    _check("A3 守卫进程真的没了（按 pid 查，不是扫 label 字符串）", not alive(gpid), f"pid={gpid} 还在={alive(gpid)}")
    _check(
        "A4 守卫那条断言被释放（机器可以睡了）",
        gpid is not None and gpid not in assertion_holders(),
        f"仍持有={gpid in assertion_holders() if gpid else None}，全场持有者 {len(assertion_holders())} 个",
    )
    log_text = ctl_log.read_text(encoding="utf-8") if ctl_log.exists() else ""
    guard_lines = [ln for ln in log_text.splitlines() if "守卫" in ln]
    _check(
        "A5 沙箱留痕写着「睡眠闸守卫已随停」",
        "睡眠闸守卫已随停" in log_text,
        guard_lines[-1][:90] if guard_lines else "无守卫行",
    )
    real_state_after = REAL_STATE.stat().st_mtime_ns if REAL_STATE.exists() else None
    _check(
        "A6 沙箱 state.json 被写、真 state.json 一个字节没动",
        state.exists() and real_state_after == real_state_before,
        f"沙箱 state={state.exists()} 真 state mtime 变了={real_state_after != real_state_before}",
    )
    _check(
        "A7 真宠物没被动过（仍加载、pid 不变）",
        loaded(REAL_PET) and job_pid(REAL_PET) == real_pid_before,
        f"loaded={loaded(REAL_PET)} pid {real_pid_before}→{job_pid(REAL_PET)}",
    )
    dis = disabled_labels()
    _check(
        "A8 两个可抛标签都不在 print-disabled（退出≠永久禁用，下次开机能回来）",
        PET not in dis and GUARD not in dis,
        "print-disabled 无命中" if PET not in dis and GUARD not in dis else dis.strip()[:120],
    )


# ── B：原语 + 反向控制（不碰脚本，探平台语义） ──────────────────────────────────


def path_b_primitives() -> None:
    print("-- B：原语与反向控制（只 bootout 主作业 ⇒ 守卫不退？）--")
    sandbox_pair(SANDBOX / "B", "120")
    gpid = job_pid(GUARD)

    run("launchctl", "bootout", f"{DOMAIN}/{PET}")
    time.sleep(2.0)
    _check(
        "B1 反向控制：只 bootout 主作业 ⇒ 守卫仍加载、仍持有断言（证明显式收尾是承重的）",
        loaded(GUARD) and gpid is not None and gpid in assertion_holders(),
        f"guard_loaded={loaded(GUARD)} guard_pid={gpid} 仍持有={gpid in assertion_holders() if gpid else None}",
    )
    run("launchctl", "bootout", f"{DOMAIN}/{GUARD}")
    for _ in range(16):
        if not loaded(GUARD):
            break
        time.sleep(0.25)
    time.sleep(0.5)
    _check(
        "B2 再 bootout 守卫（= 现在 cmd_stop 的行为）⇒ 它那条断言被释放",
        gpid is not None and gpid not in assertion_holders(),
        f"仍持有={gpid in assertion_holders() if gpid else None}，全场持有者 {len(assertion_holders())} 个",
    )


def path_d_script_text() -> None:
    print("-- D：脚本文本（防那行被顺手删）--")
    script = (REPO / "ops" / "xiaoccctl").read_text(encoding="utf-8")
    stop_body = script.split("cmd_stop()", 1)[-1].split("\n}", 1)[0]
    _check(
        "D1 `cmd_stop` 里确实 bootout 了 $GUARD_LABEL",
        "bootout" in stop_body and "$GUARD_LABEL" in stop_body,
        f"守卫收尾出现 {stop_body.count('$GUARD_LABEL')} 次",
    )


def main() -> int:
    for label in TOUCHED:
        assert label not in (REAL_PET, REAL_GUARD), "可抛标签不许和真标签同名"
    assert SANDBOX.name == "xiaocc_ctlstop_sandbox", f"沙箱目录名字不对：{SANDBOX}"

    print(f"-- 开始前的 PreventUserIdleSystemSleep 持有者：{len(assertion_holders())} 个 --")
    # overrides 库（`print-disabled`）快照：**只有 `launchctl enable/disable` 会往里写记录**，
    # 而这条库**没有"取消"动词**（`launchctl` 只有 enable/disable/print-disabled，库文件
    # /var/db/com.apple.xpc.launchd/disabled.<uid>.plist 归 root，非 root 改不了）
    # ⇒ 谁要是在回归里手滑调一次 enable/disable 留个可抛标签的记录，就永久留在系统里。
    # 所以这支笔自己**只读**它，并断言前后逐字不变（顺带也是"别把判据放宽成前缀/子串"的护栏：
    # 库里本来就可能躺着别人留下的 `ai.hermes.probe.*` 之类记录，前缀匹配会被咬）。
    dis_before = disabled_labels()
    try:
        path_a_real_stop()
        path_b_primitives()
        path_d_script_text()
    finally:
        for label in TOUCHED:
            unload(label)
        time.sleep(0.5)
        shutil.rmtree(SANDBOX, ignore_errors=True)

    print("-- ⑤ 收尾 --")
    _check("⑤跑完无残留：可抛作业都不在", not loaded(PET) and not loaded(GUARD))
    _check("⑤跑完无残留：沙箱目录已删", not SANDBOX.exists(), str(SANDBOX))
    dis_after = disabled_labels()
    added = sorted(set(re.findall(r'"([^"]+)" =>', dis_after)) - set(re.findall(r'"([^"]+)" =>', dis_before)))
    _check(
        "⑤回归**没往 overrides 库写任何记录**（print-disabled 前后逐字不变）",
        dis_after == dis_before,
        f"新增记录={added}" if added else "逐字一致",
    )
    _check(
        "⑤真作业仍在（回归没碰到它）",
        loaded(REAL_PET) and loaded(REAL_GUARD),
        f"pet={loaded(REAL_PET)} guard={loaded(REAL_GUARD)}",
    )

    print()
    if failures:
        print(f"结果：FAIL（{len(failures)} 项）—— " + "；".join(failures))
        return 1
    print(f"结果：PASS（{len(checks)}/{len(checks)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
