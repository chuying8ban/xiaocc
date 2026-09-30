"""额度条那行字（单击小cc 时贴在角色上的那句）。

单独成模块是因为这行字**必须和面板同一套口径**：面板说「15 分钟一拍、最多滞后 ≈¥0.5」，
额度条就不能写「实时」；面板陈旧时不印旧数字，额度条也不许印。两处各写一套措辞，
迟早会出现「面板说陈旧、额度条还在报数」这种自相矛盾。

纯函数，输入就是 :func:`xiaocc.quota.store.load` 的那两份东西，所以能用一份假报告把
四种情形（正常 / 陈旧 / 全无接口 / 没采过）全测掉，不用起窗口。
"""

from __future__ import annotations

from typing import Any

#: 档位枚举与别名**只从 `settings` 引**（面板的 labels、桌宠的点击、CLI 都引同一份）：
#: 在这里再抄一份就是第三处副本，加档时必漏一处（2026-09-29 数出来的那面墙）。
#: 别名仍要认 `caption`：旧配置里的值、宽度笔都会拿它来量。
from ..settings import CLICK_ACTION_ALIASES as _ACTION_ALIASES
from ..settings import CLICK_ACTIONS as ACTIONS  # noqa: F401 - 转出去给宽度笔/CLI 用

#: 单位 → 符号（认不出就用原单位，绝不猜）
_SYMBOLS = {"CNY": "¥", "RMB": "¥", "USD": "$", "US$": "$"}


def _money(value: Any, unit: str) -> str:
    """金额 + 单位。**认得出货币才把符号前置**，认不出就后置成「1200.00 Credits」。

    认不出还硬拼会写出 `Credits1200.00` 这种既没空格也没符号的怪字——DeepSeek 的
    CNY→¥ 让这条一直看不出来，千问云 Token Plan 一登录它就是主家，字面 bug 立刻现形。
    """
    symbol = _SYMBOLS.get(unit.upper()) if unit else None
    amount = _amount(value)
    return f"{symbol}{amount}" if symbol else f"{amount} {unit}".strip()

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
        amount = _money(first.get("value"), str(first.get("unit") or ""))
        name = str(service.get("name") or "余额")
        full = f"{name} {amount} · {_ago(age)}"
        return [full, f"{amount} · {_ago(age)}", amount]
    line = "没有可自动获取的余额 · 去官网看余额"
    return [line]


def badge_text(report: dict[str, Any] | None, meta: dict[str, Any] | None) -> str:
    """额度条该写什么。取不到就是取不到，**绝不补 0**。"""
    return badge_candidates(report, meta)[0]


def _pack_candidates(parts: list[str], per_line: int = 2) -> list[list[str]]:
    """若干「部件」→ 两行以内的**候选组**，从信息量最多退化到最少。

    为什么用"前缀退化"而不是手写每一条：多写一条就多一处会和窗口宽度脱节的硬编码，
    而调用方本来就是"按实测宽度挑第一个放得下的"。所以这里只保证三件事：最多两行、
    第一个候选信息最全、最后一个候选一定短到放得下。
    """
    parts = [p for p in parts if p]
    out: list[list[str]] = []
    for n in range(len(parts), 0, -1):
        sub = parts[:n]
        lines = [" · ".join(sub[i : i + per_line]) for i in range(0, len(sub), per_line)]
        if 1 <= len(lines) <= 2:
            out.append(lines)
    return out or [[]]


def _pct(part: Any, whole: Any) -> str | None:
    if part is None or not whole:
        return None
    return f"{part / whole * 100.0:.0f}%"


def _device_parts(device: Any, *, pending: bool = False) -> list[str]:
    """设备状态 → 可拼的部件（取不到的项写「未取到」，**绝不补 0**）。

    ``pending=True`` = **基线还没攒够**（CPU 那个 1 秒窗口），不是"采不到"：这两种空必须分开写
    ——把「等一秒就有」写成「未取到」才是让用户以为坏了的假数（面板那格写的就是「采集中」，
    三处出口的词表得是同一套）。
    """
    if device is None:
        return []
    cpu = getattr(device, "cpu_percent", None)
    mem = _pct(getattr(device, "mem_used", None), getattr(device, "mem_total", None))
    disk = _pct(getattr(device, "disk_used", None), getattr(device, "disk_total", None))
    battery = getattr(device, "battery", None)
    if cpu is None:
        cpu_part = "CPU 采集中" if pending else "CPU 未取到"
    else:
        cpu_part = f"CPU {cpu:.0f}%"
    parts = [
        cpu_part,
        "内存 未取到" if mem is None else f"内存 {mem}",
        "磁盘 未取到" if disk is None else f"磁盘 {disk}",
    ]
    if battery:  # 台式机没有电池：这一项直接不出现，而不是写「未取到 0」
        parts.append(f"电池 {int(battery[0])}%")
    return parts


def device_bubble_candidates(device: Any, *, pending: bool = False) -> list[list[str]]:
    """设备状态那两行（CPU/内存 在上、磁盘/电池 在下），按每行 148px 排。"""
    return _pack_candidates(_device_parts(device, pending=pending))


def bubble_candidates(
    action: str,
    *,
    report: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    device: Any = None,
    device_pending: bool = False,
) -> list[list[str]]:
    """单击小cc 时气泡该写什么 —— **三档字面都从这里出，调用方只按宽度挑**。

    ==========  ==========================================================
    ``badge``   额度（DeepSeek ¥72.22 · 9 分钟前）
    ``device``  电脑状态（CPU 16% · 内存 60% / 磁盘 22% · 电池 90%）
    ``all``     两样都要：一行额度、一行设备
    ``none``    不显示
    ==========  ==========================================================

    `caption` 当 `device` 的别名继续接受（用户 2026-09-29 把「状态文案」重定义成电脑状态，
    旧配置里那个词要还能用）。**额度与设备的陈旧/取不到规矩同源**：取不到就是取不到，
    不补 0、不印旧数。
    """
    action = _ACTION_ALIASES.get(action, action)
    if action == "none":
        return []
    device_cands = device_bubble_candidates(device, pending=device_pending)
    if action == "device":
        return device_cands
    quota_cands = badge_bubble_candidates(report, meta)
    if action != "all":
        return quota_cands
    # 「全部」：额度一行 + 设备一行（那个形态）。额度那行取**紧凑形**（金额 · 时效），
    # 找不到紧凑形就用它自己的首选整句；设备那行只取**首行**（CPU · 内存）——两行是气泡的硬上限，
    # 把设备那档的整组（可能两行）拼进来就成了三行，超出的部分真机上会被裁掉。
    quota_line = " · ".join(quota_cands[-2] if len(quota_cands) >= 2 else quota_cands[0])
    device_line = device_cands[0][0] if device_cands and device_cands[0] else ""
    combined = [[quota_line, device_line], [device_line], [quota_line]]
    return [c for c in combined if c and c[0]]


def badge_bubble_candidates(
    report: dict[str, Any] | None, meta: dict[str, Any] | None
) -> list[list[str]]:
    """**对话气泡**用的候选：每条候选是"最多两行的分组"，信息量从多到少排好。

    为什么不能沿用单行那套：气泡里那句要分两行画，窗口只有 160px 宽，**按"单行放不放得下"
    挑出来的句子到了两行版式里会变成"半句话"**（第一行撑满、第二行只剩两个字）。所以这里
    直接把"拆好行的候选"给调用方，由它按气泡每行可用宽度去挑；挑不到就退到更短的一条，
    最后一定是"放得下且读得完"的那条。

    口径与 :func:`badge_candidates` 完全同源（同一份报告、同一套陈旧/取不到的判据），
    两处措辞必须一致——面板说陈旧、气泡还在报数，就是自相矛盾。
    """
    meta = meta or {}
    if not meta.get("exists") or not report:
        return [["额度未采集", "点开面板看详情"]]
    age = meta.get("age_s")
    if meta.get("stale") or (isinstance(age, (int, float)) and age > STALE_AGE_S):
        # 两行的候选都要带「未更新」这个「别信它」的字：只写 `_ago(age)`（"60 分钟前"）会把
        # 提醒丢掉 —— 那半句话存在的意义就是别让人把陈旧数字当真数（逐行挑出来的）。
        return [
            ["余额数据陈旧", f"{_ago(age)}未更新"],
            ["余额数据陈旧", "未更新"],
            ["余额数据陈旧"],
        ]
    services = report.get("services") or []
    for service in services:
        if service.get("state") != "ok":
            continue
        items = service.get("items") or []
        if not items:
            continue
        first = items[0]
        amount = _money(first.get("value"), str(first.get("unit") or ""))
        name = str(service.get("name") or "余额")
        ago = _ago(age)
        return [
            [name, f"{amount} · {ago}"],
            [name, amount],
            [amount, ago],
            [amount],
        ]
    # **不许**把这句话拆成「没有可自动获取的 / 余额」—— 这段代码存在的理由就是
    # 「到了两行版式里会变成半句话」，它自己第一个犯（拿真 quota.json 跑出来的）。
    # 第一行给结论、第二行给动作；最后那条是保底（160px 下短句还能整句读出来）。
    return [
        ["没有可自动获取的余额", "去官网看余额"],
        ["没有可自动获取的余额"],
        ["去官网看余额"],
    ]
