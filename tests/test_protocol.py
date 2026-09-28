"""协议层测试：优先级、TTL、序列化。协议是全项目的契约，这里必须最严。"""

from __future__ import annotations

import json

import pytest

from xiaocc.protocol import PROTOCOL_VERSION, State, StatusEvent, pick


def test_all_states_have_priority_and_ttl():
    from xiaocc.protocol import STATE_PRIORITY, STATE_TTL

    for state in State:
        assert state in STATE_PRIORITY, f"{state} 没定优先级"
        assert state in STATE_TTL, f"{state} 没定 TTL"


def test_pick_prefers_higher_priority():
    now = 1000.0
    idle = StatusEvent(source="a", state=State.IDLE, at=now)
    working = StatusEvent(source="b", state=State.WORKING, at=now)
    error = StatusEvent(source="c", state=State.ERROR, at=now)
    assert pick([idle, working, error], now).state is State.ERROR
    assert pick([idle, working], now).state is State.WORKING


def test_pick_drops_stale_events():
    now = 1000.0
    stale_error = StatusEvent(source="a", state=State.ERROR, at=now - 10_000)
    fresh_idle = StatusEvent(source="b", state=State.IDLE, at=now)
    assert pick([stale_error, fresh_idle], now).state is State.IDLE
    # 全都过期 → None，而不是硬撑着演上一条（那会撒谎）
    assert pick([stale_error], now) is None


def test_idle_and_offline_never_expire():
    now = 1_000_000.0
    old_idle = StatusEvent(source="a", state=State.IDLE, at=0.0)
    assert not old_idle.is_stale(now)
    assert pick([old_idle], now) is not None


def test_progress_text_only_with_real_numbers():
    assert StatusEvent(source="a", state=State.WORKING, step=2, total=5).progress_text == "2/5"
    assert StatusEvent(source="a", state=State.WORKING, step=2).progress_text is None
    assert StatusEvent(source="a", state=State.WORKING, total=0).progress_text is None


def test_roundtrip_json():
    event = StatusEvent(
        source="file:/tmp/s.json",
        state=State.WAITING,
        detail="要不要继续？",
        project="小cc",
        step=1,
        total=3,
    )
    payload = json.loads(event.to_json())
    assert payload["protocol"] == PROTOCOL_VERSION
    assert payload["state"] == "waiting"
    back = StatusEvent.from_json(event.to_json())
    assert (back.source, back.state, back.detail, back.project, back.step, back.total) == (
        event.source,
        event.state,
        event.detail,
        event.project,
        event.step,
        event.total,
    )


def test_from_json_rejects_unknown_state():
    with pytest.raises(ValueError, match="未知状态"):
        StatusEvent.from_json('{"state": "dancing"}')


def test_from_json_rejects_missing_state():
    with pytest.raises(ValueError, match="缺少 state"):
        StatusEvent.from_json('{"detail": "没状态"}')
