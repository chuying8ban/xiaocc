"""引擎测试：坏源隔离、增量重画、无源时的老实话。"""

from __future__ import annotations

from xiaocc.characters import load_character
from xiaocc.engine import Engine
from xiaocc.protocol import State, StatusEvent
from xiaocc.sources.base import StatusSource


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedSource(StatusSource):
    name = "scripted"
    interval = 1.0

    def __init__(self, events: list[StatusEvent | None]) -> None:
        self.events = events
        self.calls = 0

    def poll(self) -> StatusEvent | None:
        event = self.events[min(self.calls, len(self.events) - 1)]
        self.calls += 1
        return event


class BoomSource(StatusSource):
    name = "boom"
    interval = 1.0

    def poll(self) -> StatusEvent | None:
        raise RuntimeError("源炸了")


def _engine(sources) -> tuple[Engine, FakeClock]:
    clock = FakeClock()
    return Engine(sources, load_character(), clock=clock), clock


def test_engine_emits_only_on_change():
    clock = FakeClock()
    working = StatusEvent(source="a", state=State.WORKING, at=1000.0, detail="写文件")
    engine = Engine([ScriptedSource([working, working, working])], load_character(), clock=clock)
    assert engine.tick() is not None
    clock.advance(0.1)
    assert engine.tick() is None, "状态没变就不该重画"
    assert engine.frame is not None and engine.frame.state is State.WORKING


def test_engine_isolates_broken_source():
    good = ScriptedSource([StatusEvent(source="good", state=State.WORKING, at=1000.0)])
    engine, _ = _engine([BoomSource(), good])
    frame = engine.tick()
    # 坏源不许带崩整只桌宠，也不许抢走屏幕：好源的状态照常上屏
    assert frame is not None and frame.state is State.WORKING
    assert "boom" in engine.health(), "故障源要能被诊断到（xiaocc run -v 打日志）"


def test_engine_reports_offline_when_no_source_speaks():
    engine, clock = _engine([ScriptedSource([None])])
    frame = engine.tick()
    assert frame is not None and frame.state is State.OFFLINE
    assert "没有任何状态源在线" in frame.event.detail


def test_engine_needs_at_least_one_source():
    import pytest

    with pytest.raises(ValueError):
        Engine([], load_character())


def test_caption_uses_progress_when_real():
    engine, _ = _engine(
        [
            ScriptedSource(
                [
                    StatusEvent(
                        source="a",
                        state=State.WORKING,
                        at=1000.0,
                        detail="跑测试",
                        project="小cc",
                        step=3,
                        total=7,
                    )
                ]
            )
        ]
    )
    frame = engine.tick()
    assert frame is not None
    assert frame.caption == "小cc · 跑测试 · 3/7"


def test_close_swallows_source_errors():
    class BadClose(StatusSource):
        name = "bad-close"
        interval = 1.0

        def poll(self):
            return None

        def close(self):
            raise RuntimeError("关不掉")

    engine, _ = _engine([BadClose()])
    engine.close()  # 不该抛
