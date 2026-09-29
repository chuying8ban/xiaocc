"""引擎测试：坏源隔离、增量重画、无源时的老实话。

offline 只表示没有任何源；源在线但无活动是 idle。
"""

from __future__ import annotations

from xiaocc.characters import load_character
from xiaocc.engine import Engine
from xiaocc.protocol import STATE_TTL, State, StatusEvent
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
    engine, _clock = _engine([ScriptedSource([None])])
    frame = engine.tick()
    assert frame is not None and frame.state is State.OFFLINE
    assert "没有任何状态源在线" in frame.event.detail


def test_engine_reports_offline_when_every_source_is_silent():
    """所有源都沉默（``poll()`` 返回 None）→ offline，detail 就是「没有任何状态源在线」。

    和下面那条「事件过期 → idle」的区别在于 events 是否为空：这里每个源都没交东西，
    引擎手里真的一条事件都没有，所以 offline 不冤枉谁。沉默也不等于故障，不许进 health()。
    """
    engine, _clock = _engine(
        [ScriptedSource([None]), ScriptedSource([None]), ScriptedSource([None])]
    )
    frame = engine.tick()
    assert frame is not None and frame.state is State.OFFLINE
    assert frame.event.detail == "没有任何状态源在线", "没故障时不该套「状态源故障：」前缀"
    assert engine.health() == {}


def test_engine_reports_offline_with_reason_when_only_source_explodes():
    """唯一的源抛异常 → 仍走 offline，但 detail 要说「状态源故障：」而不是笼统报离线。

    故障源这一轮被引擎隔离掉（continue），一条事件都没进 events，所以和「全沉默」同属
    events 为空那一支；而「事件过期」的源是**正常返回**了事件的，走 idle，不能混。
    """
    engine, _clock = _engine([BoomSource()])
    frame = engine.tick()
    assert frame is not None and frame.state is State.OFFLINE
    assert frame.event.detail.startswith("状态源故障：")
    assert "boom" in frame.event.detail and "源炸了" in frame.event.detail
    assert "boom" in engine.health()


def test_engine_reports_idle_when_source_online_but_event_stale():
    """源在线、只是它报的事件过了保鲜期 → idle（``源在线，当前无活动``），**不是** offline。

    为什么和「全沉默」不一样：这里源每轮都好好返回事件，events 非空，只是 age 超过 TTL
    被 ``pick()`` 判过期丢掉。源活着却扣「离线」帽子是撒谎——用户看到的是「小cc 没连上
    任何源」，正是「每轮任务结束闪一下离线脸」的病根。idle 的语义「在线但没在忙」才对得上。
    """
    now = 1000.0  # FakeClock 的默认起点
    ttl = STATE_TTL[State.DONE]
    assert ttl is not None, "done 必须有 TTL，否则这条用例的前提就不成立"
    # 把事件放到 TTL 之前 1 秒，确保它一定被判过期
    stale = StatusEvent(source="a", state=State.DONE, at=now - ttl - 1, detail="刚跑完")
    assert stale.is_stale(now)
    engine, _clock = _engine([ScriptedSource([stale])])
    frame = engine.tick()
    assert frame is not None
    assert frame.state is State.IDLE
    assert frame.state is not State.OFFLINE, "源在线就不许报离线"
    assert frame.event.detail == "源在线，当前无活动"


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
