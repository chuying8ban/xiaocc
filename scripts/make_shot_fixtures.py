#!/usr/bin/env python3
"""给对外截图造**替身数据**：面板的额度区、本机账本、桌宠现状都得能脱离真数据渲染。

为什么需要它：`docs/design/` 那批实拍要进公开仓库，而面板「本机账本」一节读的是作者真账本。
没有替身就只能两条路——不拍，或者把真实的调用次数/金额/库路径拍出去（后者是泄露本机状态，
跟「运行时证据不入库」同一条规矩）。所以造替身，并让这套缝**可复现**（不是某次手工 mktemp）。

产出的数字**故意都是整的、好认的假数**（¥128.00 / 1,234 次 / $12.34），配上 README 里那句
「演示数据来自替身报告，非作者真实数据」，谁看都知道不是作者余额。

用法::

    .venv/bin/python scripts/make_shot_fixtures.py [--out /tmp/xiaocc-shots]

跑完会打印需要设的四个环境变量，直接照抄进 dump / 截图命令即可。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

#: 固定、像样的替身家目录。两条讲究：
#:   * **不用 mktemp 那种 `/var/folders/…/tmp.XXXX`** —— 万一哪天账本那行把路径显示出来，
#:     截图里就是一条随机临时路径、看着像坏了；
#:   * **也不放 `$HOME` 下** —— 今天它不上屏，但放 `$HOME` 就会把真实用户名烤进 payload/留档，
#:     哪天谁把它显示出来就晚了。放 `/tmp` 下一个可读的固定名，两头都不沾。
DEMO_HERMES = Path("/tmp/xiaocc-demo-hermes")

#: 账本表结构照 `quota/ledger.py` 的查询取（只造它读的那几列）
SCHEMA = """
create table session_model_usage (
  model text, api_call_count integer, input_tokens integer, output_tokens integer,
  estimated_cost_usd real, cost_status text, last_seen real
)
"""

#: (model, 调用次数, 输入 token, 输出 token, 金额, 成本状态) —— 全是整假数
DEMO_ROWS = {
    "state.db": [
        ("deepseek-flash", 1234, 4_560_000, 1_230_000, 9.87, "estimated"),
        ("deepseek-reasoner", 321, 780_000, 640_000, 2.47, None),
    ],
    "profiles/coder/state.db": [
        ("deepseek-flash", 246, 900_000, 260_000, 1.98, "estimated"),
    ],
}

DEMO_QUOTA = {
    "schema": 1,
    "updated_at": "2026-09-30T00:00:00+08:00",
    "services": [
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "kind": "balance",
            "state": "ok",
            "items": [
                {"label": "可用余额", "value": "128.00", "unit": "CNY"},
                {"label": "其中赠送", "value": "8.00", "unit": "CNY"},
                {"label": "其中充值", "value": "120.00", "unit": "CNY"},
            ],
            "console": "https://platform.deepseek.com/usage",
            "source": "https://api.deepseek.com/user/balance",
            "fetched_at": "2026-09-30T00:00:00+08:00",
            "detail": None,
        },
        {
            "id": "qwen-token-plan",
            "name": "千问云 Token Plan",
            "kind": "unknown",
            "state": "unknown",
            "items": [],
            "console": "https://bailian.console.aliyun.com/",
            "source": "",
            "fetched_at": "2026-09-30T00:00:00+08:00",
            "detail": "未装官方 CLI「qianwen」· 登录：qianwen auth login",
        },
        {
            "id": "dashscope",
            "name": "百炼 DashScope",
            "kind": "unknown",
            "state": "unknown",
            "items": [],
            "console": "https://bailian.console.aliyun.com/",
            "source": "",
            "fetched_at": "2026-09-30T00:00:00+08:00",
            "detail": "per-key 无余额接口（官方不提供）· 账户级余额需 BSS 签名",
        },
        {
            "id": "nous-portal",
            "name": "Nous Portal",
            "kind": "unknown",
            "state": "unknown",
            "items": [],
            "console": "https://portal.nousresearch.com/",
            "source": "",
            "fetched_at": "2026-09-30T00:00:00+08:00",
            "detail": "无公开额度接口；本机 credential_pool.nous 为空 ⇒ 需登录后看 Credits",
            # 备注：这条未溢出（实测横向 0px），保留原文
        },
    ],
}

#: 账本那节**不是手写的**：照 `quota/ledger.py` 的公开入口从假库里算出来（形状/字段/口径
#: 都跟真报告一致）。手写一份形状容易跟真报告漂移，漂了以后截图就展示了一个不存在的界面。
def demo_ledger(hermes_dir: Path) -> dict:
    from xiaocc.quota import base, ledger

    return ledger.snapshot(base.default_state_dbs(hermes_dir), window_days=30)


DEMO_SETTINGS = {"click_action": "all"}


def write_demo_db(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.execute(SCHEMA)
        con.execute("create index if not exists idx_last_seen on session_model_usage(last_seen)")
        con.executemany(
            "insert into session_model_usage values (?,?,?,?,?,?,?)",
            [(*row, time.time() - 3600) for row in rows],
        )
        con.commit()
    finally:
        con.close()


def demo_probe() -> dict:
    """桌宠现状那节的自证据：**只造白名单里那几个字段**（照 `render.PET_FIELDS`）。"""
    from xiaocc.panel.render import PET_FIELDS

    source = {
        "alive": True,
        "pid": 12345,
        "state": "idle",
        "art": "idle",
        "caption": "替身数据",
        "state_source": "file",
        "state_changed_ago_s": 42,
        "ns_frame": [1288.0, 180.0, 160.0, 194.0],
        "view_size": [160, 194],
        "dock": "none",
        "anchor": [1288.0, 180.0],
        "anchor_ok": True,
        "anchor_state": "saved",
        "pump_loops_per_sec": 61.4,
        "paints_per_sec": 15.0,
        "fps": 15,
        "click_action": "all",
        "at": "2026-09-30T00:00:00+08:00",
    }
    return {key: source[key] for key in PET_FIELDS if key in source}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/xiaocc-shots", help="替身 JSON 落哪儿")
    ap.add_argument("--hermes-dir", default=str(DEMO_HERMES), help="替身家目录（放假账本）")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    hermes = Path(args.hermes_dir)

    for rel, rows in DEMO_ROWS.items():
        write_demo_db(hermes / rel, rows)
    report = dict(DEMO_QUOTA)
    report["ledger"] = demo_ledger(hermes)
    (out / "quota.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "probe.json").write_text(
        json.dumps(demo_probe(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "settings.json").write_text(
        json.dumps(DEMO_SETTINGS, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("替身已就位：")
    print(f"  假账本        {hermes}/state.db + {hermes}/profiles/coder/state.db")
    print(f"  假额度报告    {out / 'quota.json'}")
    print(f"  假桌宠自证据  {out / 'probe.json'}")
    print(f"  假设置        {out / 'settings.json'}")
    print("\n截图/dump 时带这四个缝（一个都不能少，少一个就会漏真数据进图）：")
    print(f"  XIAOCC_HERMES_DIR={hermes}")
    print(f"  XIAOCC_QUOTA_FILE={out / 'quota.json'}")
    print(f"  XIAOCC_PROBE_FILE={out / 'probe.json'}")
    print(f"  XIAOCC_SETTINGS_FILE={out / 'settings.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
