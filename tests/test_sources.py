"""状态源测试：file / command 两个万能胶，以及机械故障如何被引擎隔离。

故障口径（源自己不再造 ERROR/OFFLINE 事件）：
  * 机械故障——命令不存在 / 起不来 / 超时 / 退出码非零 / stdout 不是合法状态 JSON；
    文件不存在 / 读不了 / 内容不合法——``poll()`` **抛异常**，交给引擎隔离：
    捕获 → 按消息去重记一条 warning → 记进 ``health()`` → 不产生事件；
    只有没有任何源说话时才由引擎兜底呈现 ``offline``，并把原因写进 ``detail``。
  * ``ERROR`` 状态只留给**工作流自报**：载荷/文件里写着 ``state == "error"`` 时
    照常返回 ERROR 事件（那是被监控方真出了问题，桌宠该演出来）。
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from xiaocc.characters import load_character
from xiaocc.engine import Engine
from xiaocc.protocol import State
from xiaocc.sources.command import CommandSource
from xiaocc.sources.file import FileSource

ENGINE_LOG = "xiaocc.engine"


def _script(tmp_path, body: str) -> str:
    """写个临时脚本再调用它——避免在命令行里和引号搏斗。"""
    path = tmp_path / "helper.py"
    path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {path}"


def _engine_warnings(caplog) -> list[logging.LogRecord]:
    """只数引擎自己写的那几条 warning（去重就是靠它，别的日志混进来不算）。"""
    return [
        record
        for record in caplog.records
        if record.name == ENGINE_LOG and record.levelno == logging.WARNING
    ]


# —— file ——
def test_file_source_reads_status(tmp_path):
    path = tmp_path / "status.json"
    source = FileSource(path)
    source.touch(state="working", detail="编译中", step=2, total=5)
    event = source.poll()
    assert event is not None
    assert event.state is State.WORKING
    assert (event.detail, event.step, event.total) == ("编译中", 2, 5)


def test_file_source_missing_file_raises(tmp_path):
    """文件不存在是机械故障：抛出去让引擎隔离，而不是自己演一张 offline 脸。"""
    with pytest.raises(FileNotFoundError, match="没有这个文件"):
        FileSource(tmp_path / "nope.json").poll()


def test_file_source_unreadable_path_raises(tmp_path):
    """读不出来（这里拿目录当文件）同样是机械故障，原样抛 OSError。"""
    with pytest.raises(OSError):
        FileSource(tmp_path).poll()


def test_file_source_bad_json_raises(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    with pytest.raises(ValueError, match="内容不合法"):
        FileSource(path).poll()


def test_file_source_unknown_state_raises(tmp_path):
    """未知状态属于「内容不合法」，也是机械故障：不猜、不演。"""
    path = tmp_path / "s.json"
    path.write_text('{"state": "跳个舞"}', encoding="utf-8")
    with pytest.raises(ValueError, match="内容不合法"):
        FileSource(path).poll()


def test_file_source_workflow_error_is_an_event(tmp_path):
    """载荷自报 state=error 是工作流的错，桌宠要演出来——这条不许被顺手改掉。"""
    path = tmp_path / "s.json"
    path.write_text('{"state": "error", "detail": "构建炸了"}', encoding="utf-8")
    event = FileSource(path).poll()
    assert event is not None
    assert (event.state, event.detail) == (State.ERROR, "构建炸了")


def test_file_source_keeps_raising_while_broken(tmp_path):
    """去重是引擎的活儿：源每轮都得老实抛，不能自己咽下去只报一次。"""
    path = tmp_path / "s.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    source = FileSource(path)
    for _ in range(3):
        with pytest.raises(ValueError):
            source.poll()


def test_file_source_recovers_once_content_is_fixed(tmp_path):
    """坏一轮之后要能自己爬起来：故障时清掉 mtime，下一轮才会重读文件。"""
    path = tmp_path / "s.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    source = FileSource(path)
    with pytest.raises(ValueError):
        source.poll()
    source.touch(state="working", detail="编译中")
    event = source.poll()
    assert event is not None and event.state is State.WORKING


def test_file_source_keeps_same_state_alive(tmp_path):
    """文件没被改过 ≠ 状态过期：脚本写一次状态，桌宠就得一直演着。"""
    source = FileSource(tmp_path / "s.json")
    source.touch(state="working", detail="下载中")
    first = source.poll()
    assert first is not None
    second = source.poll()
    assert second is not None and second.state is State.WORKING
    assert not second.is_stale(), "同一个状态不该因为文件没改就判过期"


def test_file_source_empty_file_is_silence(tmp_path):
    """空文件是「本轮无话可说」，不是故障：返回 None，别惊动引擎。"""
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


def test_command_source_missing_binary_raises():
    with pytest.raises(FileNotFoundError, match="命令不存在"):
        CommandSource("xiaocc-绝对不存在的命令 --json").poll()


def test_command_source_unrunnable_binary_raises(tmp_path):
    """起不来（这里拿目录当可执行文件）也是机械故障，抛 OSError。"""
    with pytest.raises(OSError, match="命令起不来"):
        CommandSource(str(tmp_path)).poll()


def test_command_source_timeout_raises(tmp_path):
    cmd = _script(tmp_path, "import time; time.sleep(30)")
    with pytest.raises(subprocess.TimeoutExpired):
        CommandSource(cmd, timeout=0.2).poll()


def test_command_source_nonzero_exit_raises(tmp_path):
    """退出码非零 + stderr 的原因要一起抛出去，好在 detail 里给用户看。"""
    cmd = _script(tmp_path, "import sys; sys.stderr.write('炸了\\n'); sys.exit(3)")
    with pytest.raises(RuntimeError, match="退出码 3") as excinfo:
        CommandSource(cmd).poll()
    assert "炸了" in str(excinfo.value)


def test_command_source_bad_json_raises(tmp_path):
    cmd = _script(tmp_path, "print('这不是 json')")
    with pytest.raises(ValueError, match="输出不合法"):
        CommandSource(cmd).poll()


def test_command_source_workflow_error_is_an_event(tmp_path):
    """同上：载荷自报 state=error 要照常演出 ERROR，不能当成机械故障咽掉。"""
    cmd = _script(tmp_path, 'print(\'{"state": "error", "detail": "测试挂了"}\')')
    event = CommandSource(cmd).poll()
    assert event is not None
    assert (event.state, event.detail) == (State.ERROR, "测试挂了")


def test_command_source_keeps_raising_while_broken(tmp_path):
    source = CommandSource(_script(tmp_path, "import sys; sys.exit(3)"))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            source.poll()


def test_command_source_empty_string_rejected():
    with pytest.raises(ValueError):
        CommandSource("   ")


# —— 端到端：坏源交给引擎隔离 ——
def test_engine_broken_source_never_steals_the_screen(tmp_path, caplog):
    """一个永远坏的源 + 一个好源：好源照常上屏，坏源只留痕（health）不进画面。"""
    good = FileSource(tmp_path / "good.json", source_name="好文件")
    good.touch(state="working", detail="编译中", step=2, total=5)
    bad = FileSource(tmp_path / "missing.json", source_name="坏文件")
    engine = Engine([bad, good], load_character())

    with caplog.at_level(logging.WARNING, logger=ENGINE_LOG):
        frame = engine.tick()
        assert engine.tick() is None, "内容没变就不该重画"
        assert engine.tick() is None

    assert frame is not None
    assert frame.state is State.WORKING
    assert frame.caption == "编译中 · 2/5"
    assert frame.event.source == good.label, "抢画面的必须是好源，不是坏源的兜底事件"

    health = engine.health()
    assert set(health) == {"坏文件"}
    assert "FileNotFoundError" in health["坏文件"] and "没有这个文件" in health["坏文件"]


def test_engine_lone_broken_source_shows_offline_with_reason(tmp_path, caplog):
    """只有坏源时，用户看到的那一屏是 offline，且 detail 里写着到底哪儿坏了。"""
    bad = CommandSource("xiaocc-绝对不存在的命令 --json", source_name="坏命令")
    engine = Engine([bad], load_character())

    with caplog.at_level(logging.WARNING, logger=ENGINE_LOG):
        frame = engine.tick()

    assert frame is not None
    assert frame.state is State.OFFLINE
    detail = frame.event.detail
    assert "状态源故障" in detail
    assert "坏命令" in detail and "命令不存在" in detail
    assert engine.health() == {"坏命令": "FileNotFoundError: 命令不存在：xiaocc-绝对不存在的命令"}


def test_engine_does_not_spam_log_for_same_reason(tmp_path, caplog):
    """同一个原因连着坏三轮：health 一条、日志一条，不刷屏。"""
    bad = FileSource(tmp_path / "missing.json", source_name="坏文件")
    engine = Engine([bad], load_character())

    with caplog.at_level(logging.WARNING, logger=ENGINE_LOG):
        for _ in range(3):
            engine.tick()

    assert len(_engine_warnings(caplog)) == 1
    assert len(engine.health()) == 1


def test_engine_logs_again_when_reason_changes(tmp_path, caplog):
    """去重是按消息去重，不是「记一次就闭嘴」：换了原因得再说一遍。"""
    path = tmp_path / "s.json"
    source = FileSource(path, source_name="文件源")
    engine = Engine([source], load_character())

    with caplog.at_level(logging.WARNING, logger=ENGINE_LOG):
        engine.tick()  # 文件不存在
        engine.tick()  # 同一个原因：不刷
        path.write_text("{ 这不是 json", encoding="utf-8")
        engine.tick()  # 原因变了：再说一条

    warnings = _engine_warnings(caplog)
    assert len(warnings) == 2
    assert "没有这个文件" in warnings[0].getMessage()
    assert "内容不合法" in warnings[1].getMessage()


def test_engine_clears_health_once_source_recovers(tmp_path):
    """源自己好了就别再挂在 health 上：offline 兜底也要让位给真实状态。"""
    source = FileSource(tmp_path / "s.json", source_name="文件源")
    engine = Engine([source], load_character())

    first = engine.tick()
    assert first is not None and first.state is State.OFFLINE
    assert "文件源" in engine.health()

    source.touch(state="working", detail="编译中")
    second = engine.tick()
    assert second is not None and second.state is State.WORKING
    assert engine.health() == {}
