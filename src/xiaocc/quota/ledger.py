"""本机账本口径的「已用」—— 没有余额接口的那几家，只能报这个。

数据源是 Hermes 自己的 `state.db`（表 `session_model_usage`）。三个口径必须分清：

- **本机不是 1 个库，是 6 个**：默认库 + 5 个 profile，各有自己的 `session_model_usage`。
  只读默认库实测会漏掉约 41%（32,771 vs 61,559 次），所以两组数都要报。
- **「未计价的行」和「成本未知的模型」是两件事**，不能混成一个「未知」：真实库里大量行是
  `cost_status` 为 NULL 而 `estimated_cost_usd=0`（比如 title_generation 这类不单独计费的
  调用），那些行只该被记成「另有 N 次未计价」的注脚；只有一个模型**一行已计价都没有**时，
  它的金额才是「未知」。第一版把两者当成一回事，结果把 deepseek-flash 的 $43.36 也吞成了
  「金额未知」——**宁可少报也不能多报，但也不能把有数的东西报成没数**。
- **金额的判据是「`estimated_cost_usd` 有没有值」，不是 `cost_status` 是不是 `estimated`**
  （第二版又踩了这个）：默认库里有 333 行 / 约 4,500 次调用是
  `cost_status = NULL` 而金额 > 0，按状态过滤会**少报 13%**（$38.60 vs $44.46，六库少报
  $13.5）。所以：金额 = 全部行的 `estimated_cost_usd` 求和；「未计价」只算
  **既没有金额、状态也不可信**的行。
- 库是**只读**打开的，且一个库打不开只跳过它，不许影响别的库和别的服务。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import db_label, now_iso

#: 成本可信的状态；其余（None / "" / unknown）一律让那个模型的金额变成「未知」
KNOWN_COST_STATUS = frozenset({"estimated", "actual", "final", "official", "provider"})

#: 单库扫描的超时：账本可能正被网关写，等不到就跳过（诊断读不许阻塞别人）
_SQLITE_TIMEOUT_S = 2.0

_QUERY = """
select model,
       sum(coalesce(api_call_count, 0))            as calls,
       sum(coalesce(input_tokens, 0))              as input_tokens,
       sum(coalesce(output_tokens, 0))             as output_tokens,
       sum(coalesce(estimated_cost_usd, 0))        as known_cost,
       sum(case when estimated_cost_usd > 0 or cost_status in ({known}) then 1 else 0 end)
                                                   as priced_rows,
       sum(case when estimated_cost_usd > 0 or cost_status in ({known}) then 0
                else coalesce(api_call_count, 0) end) as unpriced_calls
  from session_model_usage
 where last_seen is null or last_seen >= ?
 group by model
"""


def _empty_scan(path: Path, note: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "readable": False,
        "note": note,
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "cost_known": False,
        "models": [],
    }


def scan_db(path: Path, *, window_days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """扫一个库；打不开/没表 ⇒ ``readable=False`` 的空结构（不抛）。"""
    path = Path(path)
    if not path.exists():
        return _empty_scan(path, "库不存在")
    cutoff = (now.timestamp() if now else datetime.now().astimezone().timestamp()) - window_days * 86400
    known = ",".join(f"'{s}'" for s in sorted(KNOWN_COST_STATUS))
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT_S)
    except sqlite3.Error as exc:
        return _empty_scan(path, f"打不开：{type(exc).__name__}")
    try:
        try:
            rows = con.execute(_QUERY.format(known=known), (cutoff,)).fetchall()
        except sqlite3.Error as exc:
            return _empty_scan(path, f"查不动：{type(exc).__name__}")
    finally:
        con.close()

    models: list[dict[str, Any]] = []
    for model, calls, in_tok, out_tok, known_cost, priced_rows, unpriced_calls in rows:
        models.append(
            {
                "model": model or "(未知模型)",
                "calls": int(calls or 0),
                "input_tokens": int(in_tok or 0),
                "output_tokens": int(out_tok or 0),
                # 金额判据是「有没有值」，不是「状态可不可信」；一行都没有金额的模型才是「未知」。
                # 未计价的行只做注脚：不吞掉、也不假装是 0。
                "cost_usd": round(float(known_cost or 0.0), 4) if priced_rows else None,
                "cost_known": bool(priced_rows),
                "unpriced_calls": int(unpriced_calls or 0),
            }
        )
    models.sort(key=lambda m: (-m["calls"], m["model"]))
    return {
        "path": str(path),
        "readable": True,
        "note": None,
        "calls": sum(m["calls"] for m in models),
        "input_tokens": sum(m["input_tokens"] for m in models),
        "output_tokens": sum(m["output_tokens"] for m in models),
        "cost_usd": _sum_cost(models),
        "cost_known": any(m["cost_known"] for m in models),
        "unpriced_calls": sum(m["unpriced_calls"] for m in models),
        "models": models,
    }


def _sum_cost(models: list[dict[str, Any]]) -> float | None:
    """合计金额 = 已计价模型的合计；**一个已计价的都没有才是「未知」**。

    未计价的行不参与求和，但要另外报 ``unpriced_calls`` —— 那是「还有多少调用没计入」的注脚，
    不是「金额为 0」。
    """
    priced = [m for m in models if m["cost_known"]]
    if not priced:
        return None
    return round(sum(m["cost_usd"] or 0.0 for m in priced), 4)


def _merge(scans: list[dict[str, Any]]) -> dict[str, Any]:
    """把多个库合成一份「全部 profile」口径（按模型合并）。"""
    bucket: dict[str, dict[str, Any]] = {}
    for scan in scans:
        for m in scan["models"]:
            acc = bucket.setdefault(
                m["model"],
                {"model": m["model"], "calls": 0, "input_tokens": 0, "output_tokens": 0,
                 "cost_usd": None, "cost_known": False, "unpriced_calls": 0, "_cost": 0.0},
            )
            acc["calls"] += m["calls"]
            acc["input_tokens"] += m["input_tokens"]
            acc["output_tokens"] += m["output_tokens"]
            acc["unpriced_calls"] += m["unpriced_calls"]
            if m["cost_known"]:
                acc["_cost"] += m["cost_usd"] or 0.0
                acc["cost_known"] = True
    models = []
    for acc in bucket.values():
        cost = round(acc.pop("_cost"), 4)
        acc["cost_usd"] = cost if acc["cost_known"] else None
        models.append(acc)
    models.sort(key=lambda m: (-m["calls"], m["model"]))
    return {
        "readable": any(s["readable"] for s in scans),
        "note": None,
        "calls": sum(m["calls"] for m in models),
        "input_tokens": sum(m["input_tokens"] for m in models),
        "output_tokens": sum(m["output_tokens"] for m in models),
        "cost_usd": _sum_cost(models),
        "cost_known": any(m["cost_known"] for m in models),
        "unpriced_calls": sum(m["unpriced_calls"] for m in models),
        "models": models,
        "dbs": [s["path"] for s in scans if s["readable"]],
    }


def snapshot(
    state_dbs: list[Path], *, window_days: int = 30, now: datetime | None = None, top: int = 12
) -> dict[str, Any]:
    """返回面板要的两组数：默认库一份、全部库合并一份。**永不抛。**"""
    scans = [scan_db(p, window_days=window_days, now=now) for p in state_dbs]
    default = scans[0] if scans else _empty_scan(Path("state.db"), "没有配置账本")
    allscope = _merge(scans)
    for section in (default, allscope):
        if len(section["models"]) > top:
            section["models"] = section["models"][:top]
            section["truncated"] = True
    return {
        "window_days": window_days,
        "generated_at": now_iso(),
        # 库的数量**由 payload 带出去**，页面别再写死"全部 6 库"（新建 profile 会静默漏）
        "db_count": len(state_dbs),
        "db_profiles": [db_label(p) for p in state_dbs],
        "default_profile": default,
        "all_profiles": allscope,
    }
