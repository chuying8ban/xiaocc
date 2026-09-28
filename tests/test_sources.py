"""状态源测试：file / command 两个万能胶，以及它们出错时的行为。"""

from __future__ import annotations

import sys

import pytest

from xiaocc.protocol import State
from xiaocc.sources.command import CommandSource
from xiaocc.sources.file import FileSource


def _script(tmp_path, body: str) -> str:
    """写个临时脚本再调用它——避免在命令行里和引号搏斗。"""
    path = tmp_path / "helper.py"
    path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {path}"


# —— file ——
def test_file_source_reads_status(tmp_path):
    path = tmp_path / "status.json"
    source = FileSource(path)
    source.touch(state="working", detail="编译中", step=2, total=5)
    event = source.poll()
    assert event is not None
    assert event.state is State.WORKING
    assert (event.detail, event.step, event.total) == ("编译中", 2, 5)


def test_file_source_missing_file_is_offline_not_crash(tmp_path):
    event = FileSource(tmp_path / "nope.json").poll()
    assert event is not None and event.state is State.OFFLINE


def test_file_source_keeps_same_state_alive(tmp_path):
    """文件没被改过 ≠ 状态过期：脚本写一次状态，桌宠就得一直演着。"""
    source = FileSource(tmp_path / "s.json")
    source.touch(state="working", detail="下载中")
    first = source.poll()
    assert first is not None
    second = source.poll()
    assert second is not None and second.state is State.WORKING
    assert not second.is_stale(), "同一个状态不该因为文件没改就判过期"


def test_file_source_reports_bad_json_once(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    source = FileSource(path)
    first = source.poll()
    assert first is not None and first.state is State.ERROR
    assert source.poll() is None, "同一个坏文件不该每秒刷一条错误"


def test_file_source_rejects_unknown_state(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"state": "跳个舞"}', encoding="utf-8")
    event = FileSource(path).poll()
    assert event is not None and event.state is State.ERROR


def test_file_source_empty_file_is_silence(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("   \n", encoding="utf-8")
    assert FileSource(path).poll() is None


# —— command ——
def test_command_source_reads_stdout_json(tmp_path):
    cmd = _script(tmp_path, 'print(\'{"state": "done", "detail": "好了"}\')')
    event = CommandSource(cmd).poll()
    assert event is not None
    assert (event.state, event.detail) == (State.DONE, "好了")


def test_command_source_empty_output_means_silence(tmp_path):
    assert CommandSource(_script(tmp_path, "pass")).poll() is None


def test_command_source_nonzero_exit_is_error(tmp_path):
    cmd = _script(tmp_path, "import sys; sys.stderr.write('炸了\\n'); sys.exit(3)")
    event = CommandSource(cmd).poll()
    assert event is not None and event.state is State.ERROR
    assert "3" in event.detail and "炸了" in event.detail


def test_command_source_repeats_error_only_once(tmp_path):
    source = CommandSource(_script(tmp_path, "import sys; sys.exit(3)"))
    assert source.poll() is not None
    assert source.poll() is None


def test_command_source_missing_binary_is_error():
    event = CommandSource("xiaocc-绝对不存在的命令 --json").poll()
    assert event is not None and event.state is State.ERROR
    assert "不存在" in event.detail


def test_command_source_empty_string_rejected():
    with pytest.raises(ValueError):
        CommandSource("   ")
