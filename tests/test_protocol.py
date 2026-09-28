"""协议层测试：优先级、TTL、序列化。协议是全项目的契约，这里必须最严。"""

from __future__ import annotations

import json
import time

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


# —— ``at`` 的护栏 ——
# age() 和 STATE_TTL 全建在 ``at`` 上：单位写错（毫秒）或写成 NaN，age 就会算成 0
# 甚至让所有比较失效，事件从此**永不过期**——一个 error 会永远挂在桌面上。
def _at_json(at: object) -> str:
    """造一条只有 ``at`` 可疑的载荷：state 合法，这样失败原因只可能来自 at。"""
    return json.dumps({"state": "error", "at": at}, ensure_ascii=False)


def test_from_json_defaults_at_to_now():
    """没写 ``at`` 就取当前时间。

    为什么：缺省若落成 0，事件一出生就算过期，桌宠会当场变 offline。
    """
    event = StatusEvent.from_json('{"state": "working", "detail": "编译中"}')
    assert abs(event.at - time.time()) < 1.0
    assert not event.is_stale()  # 刚落地必须是新鲜的，TTL 从这一刻开始算


def test_from_json_rejects_millisecond_timestamp():
    """毫秒是最常见的写法事故，必须当场抓住。

    为什么：``time.time() * 1000`` 超前几万年，age() 恒为 0，STATE_TTL 形同不存在。
    """
    with pytest.raises(ValueError, match="超前"):
        StatusEvent.from_json(_at_json(time.time() * 1000))


def test_from_json_allows_small_clock_skew():
    """略微超前属于机器间正常的时钟偏差，不能一刀切。

    为什么：源可能跑在另一台机器/容器上，几十秒偏差是常态，误杀好源比放过更糟。
    """
    given = time.time() + 30
    event = StatusEvent.from_json(_at_json(given))
    assert abs(event.at - given) < 0.01  # 原样收下，不做四舍五入或改写


@pytest.mark.parametrize("bad", [float("nan"), "nan"])
def test_from_json_rejects_non_finite_at(bad):
    """非有限数一律拒收。

    为什么：NaN 参与任何比较都是 False，``age > ttl`` 永远不成立 = 永不过期；
    字符串 ``"nan"`` 同理不是秒级时间戳，不能靠 float() 硬转蒙混过去。
    """
    with pytest.raises(ValueError, match="秒级时间戳"):
        StatusEvent.from_json(_at_json(bad))


def test_from_json_rejects_at_beyond_skew_budget():
    """边界：61 秒已经超出给时钟偏差留的 60 秒余量。

    为什么：护栏是「>60 秒即不合法」，这条钉住边界不会被悄悄放宽。
    """
    with pytest.raises(ValueError, match="超前"):
        StatusEvent.from_json(_at_json(time.time() + 61))
