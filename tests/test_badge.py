"""额度条那行字：四种情形 + 「绝不补 0」+ 候选优先级。

面板和额度条必须同一套口径，所以这里不只测「正常能出数」，还测「取不到时说的话」——
那几句是用户唯一能看到的解释，写错就等于把「没采到」说成「余额 0」。
"""

from __future__ import annotations

from xiaocc.quota.badge import (
    STALE_AGE_S,
    badge_bubble_candidates,
    badge_candidates,
    badge_text,
)


def report(*services: dict) -> dict:
    return {"schema": 1, "services": list(services)}


def service(name: str, state: str, *items: dict) -> dict:
    return {"name": name, "state": state, "items": list(items)}


def ok_meta(age_s: float) -> dict:
    return {"exists": True, "age_s": age_s, "stale": False, "path": "/tmp/quota.json"}


def test_fresh_balance_reads_with_age() -> None:
    rep = report(service("DeepSeek", "ok", {"label": "可用余额", "value": 75.0, "unit": "CNY"}))
    assert badge_text(rep, ok_meta(480.0)) == "DeepSeek ¥75.00 · 8 分钟前"


def test_candidates_go_from_chatty_to_short() -> None:
    rep = report(service("DeepSeek", "ok", {"value": 75.0, "unit": "CNY"}))
    candidates = badge_candidates(rep, ok_meta(480.0))
    assert candidates[0] == "DeepSeek ¥75.00 · 8 分钟前"
    assert candidates[-1] == "¥75.00"
    assert all(len(candidates[i]) >= len(candidates[i + 1]) for i in range(len(candidates) - 1))


def test_stale_hides_the_number() -> None:
    rep = report(service("DeepSeek", "ok", {"value": 75.0, "unit": "CNY"}))
    text = badge_text(rep, {"exists": True, "age_s": STALE_AGE_S + 60, "stale": True, "path": "x"})
    assert "75" not in text, "陈旧时不许把旧数字当余额印出来"
    assert "陈旧" in text


def test_not_collected_yet() -> None:
    assert badge_text(None, {"exists": False, "age_s": None, "stale": True}) == "额度未采集 · 点开面板看详情"
    assert badge_text(report(), {"exists": True, "age_s": 1.0}) == "没有可自动获取的余额 · 去控制台看"


def test_unknown_services_do_not_fabricate_zero() -> None:
    rep = report(service("千问云", "unknown"), service("百炼", "error", {"value": None}))
    text = badge_text(rep, ok_meta(60.0))
    assert "0.00" not in text and "¥0" not in text
    assert text == "没有可自动获取的余额 · 去控制台看"


def test_first_ok_service_wins_and_units_are_mapped() -> None:
    rep = report(
        service("千问云", "unknown"),
        service("DeepSeek", "ok", {"value": 12.5, "unit": "CNY"}),
        service("OpenAI", "ok", {"value": 3.0, "unit": "USD"}),
    )
    assert badge_text(rep, ok_meta(30.0)).startswith("DeepSeek ¥12.50")


def test_unknown_unit_goes_after_the_number() -> None:
    """认不出货币就**后置**：`credits5.00` 既没空格也没符号，是字面 bug（@writer 抓的）。

    千问云 Token Plan 的 `Credits` 一登录就是主家，DeepSeek 的 CNY→¥ 一直把这条盖着。
    """
    rep = report(service("某家", "ok", {"value": 5.0, "unit": "credits"}))
    assert badge_text(rep, ok_meta(30.0)).startswith("某家 5.00 credits")


def test_known_currency_still_uses_the_symbol() -> None:
    rep = report(service("DeepSeek", "ok", {"value": 5.0, "unit": "CNY"}))
    assert badge_text(rep, ok_meta(30.0)).startswith("DeepSeek ¥5.00")


def test_credits_bubble_lines_stay_readable() -> None:
    """@writer 量的字面（11pt 真字体、气泡每行上限 153px）：`Credits1200.00 · 9 分钟前`
    拆两行后每行都读得完，第一行不许断在半句话中间。"""
    rep = report(service("千问云 Token Plan", "ok", {"value": 1200.0, "unit": "Credits"}))
    lines = badge_bubble_candidates(rep, ok_meta(540.0))[0]
    assert len(lines) == 2
    assert lines[0] == "千问云 Token Plan"
    assert lines[1] == "1200.00 Credits · 9 分钟前"
