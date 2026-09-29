"""`python -m xiaocc.quota show|refresh`。

先不动 `src/xiaocc/cli.py`（那文件现在有别人未提交的改动，撞车代价比省一条命令大）；
等它落地再把这两条挂成 `xiaocc quota show|refresh`。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from . import DEFAULT_QUOTA_PATH, load, refresh
from .base import STATE_OK

_MARK = {STATE_OK: "  ", "stale": "陈旧", "unknown": "未知", "error": "出错"}


def _ts() -> str:
    """launchd 把这支作业的 stdout 收进 `quota.out.log`，而那一行**原来没有时间戳** ⇒
    「它跑没跑」只能靠 quota.json 的 mtime 反推（实测踩过：看到一段 40 分钟的空档，
    却判不出是真没跑，还是 StartInterval + Background 的作业在锁屏/睡眠时被系统延后）。
    每拍一行都带时间戳，这件事就不用猜了。"""
    return time.strftime("%m-%d %H:%M:%S")


def _fmt_items(service: dict[str, Any]) -> str:
    items = service.get("items") or []
    if not items:
        return "未知（去官网看余额）" if service.get("state") == "unknown" else "—"
    return " ｜ ".join(f"{i['label']} {i['value']}{(' ' + i['unit']) if i['unit'] else ''}" for i in items)


def _fmt_money(value: Any, known: bool) -> str:
    """金额口径：不知道就写「未知」，**不许**把 None 打成 0.00。"""
    if not known or value is None:
        return "金额未知"
    return f"${value:,.2f}"


def _print_ledger(books: dict[str, Any]) -> None:
    days = books.get("window_days", 30)
    for key, label in (("default_profile", "默认 profile"), ("all_profiles", "全部 profile")):
        section = books.get(key)
        if not section:
            print(f"  本机账本（{label}，{days} 天）: 读不到")
            continue
        unpriced = section.get("unpriced_calls") or 0
        note = f"（另有 {unpriced:,} 次未计价）" if unpriced else ""
        print(
            f"  本机账本（{label}，{days} 天）: {section['calls']:,} 次 / "
            f"{_fmt_money(section['cost_usd'], section['cost_known'])}{note}"
            f" ｜ 输入 {section['input_tokens']:,} tok · 输出 {section['output_tokens']:,} tok"
        )


def _show(path: Path, as_json: bool) -> int:
    report, meta = load(path)
    if report is None:
        print(f"还没有额度文件：{path}（先跑 `python -m xiaocc.quota refresh`）")
        return 1
    if as_json:
        print(json.dumps({"meta": meta, "report": report}, ensure_ascii=False, indent=2))
        return 0
    note = " **已陈旧，采集器可能挂了**" if meta.get("stale") else ""
    print(f"== 额度（更新于 {report.get('updated_at', '?')}，{meta['age_s']}s 前）{note} ==")
    for service in report.get("services", []):
        mark = _MARK.get(service.get("state"), service.get("state"))
        print(f"  {service['name']:<18} {mark:<4} {_fmt_items(service)}")
        if service.get("detail") and service.get("state") != STATE_OK:
            print(f"      ↳ {service['detail']}")
    _print_ledger(report.get("ledger") or {})
    print("  （「剩余」只有 DeepSeek 是官方数；其余需登录各自控制台，「本机账本」是本机估算，不是账单）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m xiaocc.quota", description="小cc 额度采集")
    parser.add_argument("command", choices=("show", "refresh"), nargs="?", default="show")
    parser.add_argument("--file", type=Path, default=DEFAULT_QUOTA_PATH, help="quota.json 路径")
    parser.add_argument("--json", action="store_true", help="原样打 JSON")
    args = parser.parse_args(argv)

    if args.command == "refresh":
        report, saved = refresh(args.file)
        print(f"{_ts()} 采集完成：{len(report['services'])} 个服务；写入 {args.file} = {saved}")
        if not saved:
            print(f"{_ts()} （写盘失败：额度文件写不进去不该影响桌宠，这里只报告）")
        return 0 if saved else 1
    return _show(args.file, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
