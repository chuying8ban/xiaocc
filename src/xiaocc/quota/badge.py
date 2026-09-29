"""额度条那行字（单击小cc 时贴在角色上的那句）。

单独成模块是因为这行字**必须和面板同一套口径**：面板说「15 分钟一拍、最多滞后 ≈¥0.5」，
额度条就不能写「实时」；面板陈旧时不印旧数字，额度条也不许印。两处各写一套措辞，
迟早会出现「面板说陈旧、额度条还在报数」这种自相矛盾。

纯函数，输入就是 :func:`xiaocc.quota.store.load` 的那两份东西，所以能用一份假报告把
四种情形（正常 / 陈旧 / 全无接口 / 没采过）全测掉，不用起窗口。
"""

from __future__ import annotations

from typing import Any

#: 单位 → 符号（认不出就用原单位，绝不猜）
_SYMBOLS = {"CNY": "¥", "RMB": "¥", "USD": "$", "US$": "$"}

#: 超过这个年龄就说「陈旧」，不再印数字（与采集器 30 分钟的陈旧阈值同源）
STALE_AGE_S = 30 * 60.0


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "刚刚"
    if seconds < 90:
        return f"{max(1, round(seconds))} 秒前"
    if seconds < 5400:
        return f"{round(seconds / 60)} 分钟前"
    hours = seconds / 3600.0
    return f"{hours:.0f} 小时前" if hours >= 2 else "1 小时前"


def _amount(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def badge_candidates(report: dict[str, Any] | None, meta: dict[str, Any] | None) -> list[str]:
    """额度条可用的写法，**信息量从多到少**排好；调用方按窗口宽度挑第一个放得下的。

    为什么要多档：桌宠窗口只有 160px 宽，「DeepSeek ¥75.00 · 8 分钟前」放不下时会截成
    「DeepSeek ¥75.00 · 8 分…」—— 那还不如主动退到「¥75.00 · 8 分钟前」。所以量宽度这件事
    交给调用方（它在 AppKit 那边），这里只负责把候选和优先级给对。
    """
    meta = meta or {}
    if not meta.get("exists") or not report:
        line = "额度未采集 · 点开面板看详情"
        return [line]
    age = meta.get("age_s")
    if meta.get("stale") or (isinstance(age, (int, float)) and age > STALE_AGE_S):
        line = f"余额数据陈旧 · {_ago(age)}未更新"
        return [line]
    services = report.get("services") or []
    for service in services:
        if service.get("state") != "ok":
            continue
        items = service.get("items") or []
        if not items:
            continue
        first = items[0]
        unit = str(first.get("unit") or "")
        symbol = _SYMBOLS.get(unit.upper(), unit)
        amount = f"{symbol}{_amount(first.get('value'))}"
        name = str(service.get("name") or "余额")
        full = f"{name} {amount} · {_ago(age)}"
        return [full, f"{amount} · {_ago(age)}", amount]
    line = "没有可自动获取的余额 · 去控制台看"
    return [line]


def badge_text(report: dict[str, Any] | None, meta: dict[str, Any] | None) -> str:
    """额度条该写什么。取不到就是取不到，**绝不补 0**。"""
    return badge_candidates(report, meta)[0]
