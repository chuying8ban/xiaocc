"""控制入口（退出 / 重启）的单测：**只验映射与卫生缝，绝不碰真 launchd 作业**。

红线：这里跑的每一条都必须落在沙箱里 —— `XIAOCC_CTL_OVERRIDE` 指替身脚本、
`XIAOCC_CTL_DRY_RUN=1` 连替身都不跑。真作业（`ai.hermes.xiaocc`）只允许在被测代码之外被碰。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from xiaocc import control


def test_quit_maps_to_stop_because_only_bootout_stops_it() -> None:
    """`quit` 必须是 `xiaoccctl stop`：直接 kill 进程会被 launchd 当非正常退出再拉起来。"""
    assert control.ctl_argv("quit")[-1] == "stop"
    assert control.ctl_argv("restart")[-1] == "restart"
    assert control.ctl_argv("quit")[:2] == ["/bin/zsh", control.ctl_path()]
    assert control.ACTIONS == ("restart", "quit")


def test_unknown_action_raises() -> None:
    with pytest.raises(ValueError, match="未知动作"):
        control.ctl_argv("nuke")


def test_override_dry_run_and_mark_are_a_clean_seam(tmp_path, monkeypatch) -> None:
    calls = tmp_path / "calls.txt"
    stub = tmp_path / "stub.sh"
    stub.write_text('#!/bin/zsh\nprint -- "$@" >> "' + str(calls) + '"\n', encoding="utf-8")
    mark = tmp_path / "mark.txt"
    monkeypatch.setenv("XIAOCC_CTL_MARK", str(mark))
    monkeypatch.setenv("XIAOCC_CTL_OVERRIDE", str(stub))

    monkeypatch.setenv("XIAOCC_CTL_DRY_RUN", "1")
    ok, detail = control.perform("quit")
    assert ok and detail == "dry-run"
    assert not calls.exists(), "dry-run 连替身都不许跑"
    assert "dry-run" in mark.read_text(encoding="utf-8")

    mark.unlink()
    monkeypatch.delenv("XIAOCC_CTL_DRY_RUN")
    ok, detail = control.perform("restart")
    assert ok, detail
    assert "restart" in calls.read_text(encoding="utf-8")
    assert "restart" in mark.read_text(encoding="utf-8")
