"""小cc 的「退出 / 重启」 —— **唯一入口**（用户 2026-09-29 要求加这两个功能）。

为什么是「把事交给 ``ops/xiaoccctl``」而不是在 Python 里直接敲 ``launchctl``：

* 桌宠由 **launchd** 管（label ``ai.hermes.xiaocc``，``KeepAlive=SuccessfulExit:false``）⇒
  **只有 ``bootout`` 才停得住它**：直接 kill 进程会被 launchd 当成「非正常退出」再拉起来，
  看起来就是「点了退出它自己又活了」。重启要 ``bootout`` + ``bootstrap`` 两步。
* 这两步脚本里已经踩过一遍（plist 同步、幂等、留痕 ``xiaoccctl.log``、`status` 退出码语义），
  所以这里只做一件事：**把事情交给它**，绝不在第二处再实现一遍 launchd 语义。

两条卫生红线（门禁/沙箱用 —— 回归脚本**绝不允许**碰真作业）：

* ``XIAOCC_CTL_OVERRIDE=<脚本>`` —— 换成替身脚本执行（门禁断言「确实调了、参数对」）；
* ``XIAOCC_CTL_DRY_RUN=1`` —— 只记账不动手（面板门禁要验点击链路，但不能真把桌宠停掉）；
* ``XIAOCC_CTL_MARK=<文件>`` —— 每次动作追加一行 ``<时刻> <动作> <命令行>``（留痕/自证据）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("xiaocc.control")

#: 对外只认这两个动作
ACTIONS = ("restart", "quit")

#: 动作 → ``xiaoccctl`` 的子命令。``quit`` 对应 ``stop``：**bootout 才停得住**（见文件头）。
SUBCOMMAND = {"restart": "restart", "quit": "stop"}

#: 仓库根（``src/xiaocc/control.py`` → 上溯三层）
REPO = Path(__file__).resolve().parents[2]

_TIMEOUT_S = 30.0


def ctl_path() -> str:
    """``xiaoccctl`` 脚本路径（``XIAOCC_CTL_OVERRIDE`` 可换成替身）。"""
    return os.environ.get("XIAOCC_CTL_OVERRIDE") or str(REPO / "ops" / "xiaoccctl")


def ctl_argv(action: str) -> list[str]:
    """动作 → 命令行。**纯函数**，单测直接断言映射（别真去跑脚本）。"""
    if action not in ACTIONS:
        raise ValueError(f"未知动作 {action!r}（只认 {ACTIONS}）")
    return ["/bin/zsh", ctl_path(), SUBCOMMAND[action]]


def dry_run() -> bool:
    return os.environ.get("XIAOCC_CTL_DRY_RUN") == "1"


def _mark(action: str, argv: list[str], note: str) -> None:
    path = os.environ.get("XIAOCC_CTL_MARK")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f} {action} {' '.join(argv)} [{note}]\n")
    except OSError as exc:  # 留痕失败不能把「退出」这种功能带崩
        log.warning("控制留痕写不进去：%s", exc)


def perform(action: str, *, timeout: float = _TIMEOUT_S) -> tuple[bool, str]:
    """执行并**等它结束**（面板 / CLI 用这条：自己不是被停的那个进程）。

    返回 ``(成功?, 说明)``。失败**只报不抛** —— 面板按钮不能因为脚本不在就崩。
    """
    argv = ctl_argv(action)
    if dry_run():
        _mark(action, argv, "dry-run")
        log.info("控制：%s（dry-run，只记账不执行）", action)
        return True, "dry-run"
    _mark(action, argv, "run")
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("控制：%s 执行不了：%s", action, exc)
        return False, f"{type(exc).__name__}: {exc}"
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    detail = f"rc={done.returncode}" + (f" · {tail[-1]}" if tail else "")
    log.info("控制：%s ⇒ %s", action, detail)
    return done.returncode == 0, detail


def perform_detached(action: str) -> tuple[bool, str]:
    """派一个**独立会话**去执行，立刻返回 —— 给「自己就是要被停掉的那个进程」用。

    桌宠右键菜单走这条：``xiaoccctl`` 会 ``bootout`` 掉它自己这个作业（我们会被 SIGTERM），
    所以脚本必须在新会话里跑（我们死了它也照跑），而且**不要**自己在半路 ``os._exit``——
    让 SIGTERM 走 cli 的收尾（写 ``probe.alive=false``、关窗、退栈）比"先自杀"干净。
    """
    argv = ctl_argv(action)
    if dry_run():
        _mark(action, argv, "dry-run")
        log.info("控制：%s（dry-run，只记账不执行）", action)
        return True, "dry-run"
    _mark(action, argv, "detached")
    kwargs: dict[str, Any] = {}
    if sys.platform == "darwin":
        kwargs["start_new_session"] = True  # 别跟桌宠共享信号组/控制终端
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError as exc:
        log.warning("控制：%s 派不出去：%s", action, exc)
        return False, f"{type(exc).__name__}: {exc}"
    log.info("控制：%s 已派出（独立会话，随后本进程会被 bootout 收走）", action)
    return True, "detached"
