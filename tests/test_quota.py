"""额度采集器的离线单测：不碰网络、不碰真密钥、不碰真的 ~/.hermes。

守的是几条会被「顺手写成假数」的规矩：
- 拿不到余额 ⇒ 未知，**永远不许**印 0.00；
- 账本里成本不可信的行 ⇒ 那个模型的金额报「未知」，且合计一起未知；
- 密钥只从指定路径读、且**绝不出现在报告里**；
- 写盘/单个采集器失败一律不许抛给调用方（桌宠只读文件，不能被诊断链路拖死）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import urllib.error
from pathlib import Path

import pytest

from xiaocc.quota import DEFAULT_QUOTA_PATH, base, collect, ledger, refresh
from xiaocc.quota import store as quota_store
from xiaocc.quota.badge import (
    badge_bubble_candidates,
    badge_text,
    bubble_candidates,
    device_bubble_candidates,
)
from xiaocc.quota.base import QuotaContext, read_env_file
from xiaocc.quota.deepseek import DeepSeekAdapter, fetch_balance
from xiaocc.quota.qianwen import QwenTokenPlanAdapter
from xiaocc.quota.static import default_consoles

BALANCE_OK = json.dumps(
    {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "82.22",
                "granted_balance": "0.00",
                "topped_up_balance": "82.22",
            }
        ],
    }
).encode()


class FakeResponse:
    def __init__(self, body: bytes = BALANCE_OK) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_ok(body: bytes = BALANCE_OK):
    def _open(request, timeout):
        _open.last_request = request
        return FakeResponse(body)

    _open.last_request = None
    return _open


def opener_raise(exc: BaseException):
    def _open(request, timeout):
        raise exc

    return _open


# —— 1. DeepSeek 适配器 ——————————————————————————————————————————————


def test_deepseek_ok_parses_balance():
    quota = fetch_balance("sk-test", opener=opener_ok())
    assert quota.state == "ok"
    assert next(i.label for i in quota.items) == "可用余额"
    assert quota.items[0].value == "82.22"
    assert quota.items[0].unit == "CNY"


def test_deepseek_without_key_is_unknown_not_zero():
    quota = fetch_balance("")
    assert quota.state == "unknown"
    assert quota.items == []  # 没有 items ⇒ 面板印不出 0.00
    assert "DEEPSEEK_API_KEY" in (quota.detail or "")


@pytest.mark.parametrize(
    "exc,marker",
    [
        (urllib.error.HTTPError("u", 401, "Unauthorized", None, None), "HTTP 401"),
        (urllib.error.HTTPError("u", 500, "Server Error", None, None), "HTTP 500"),
        (urllib.error.URLError("timed out"), "URLError"),
        (TimeoutError("timed out"), "TimeoutError"),
    ],
)
def test_deepseek_errors_are_reported_not_raised(exc, marker):
    quota = fetch_balance("sk-test", opener=opener_raise(exc))
    assert quota.state == "error"
    assert marker in (quota.detail or "")
    assert quota.items == []


def test_deepseek_bad_json_and_bad_shape():
    bad = fetch_balance("sk-test", opener=opener_ok(b"{not json"))
    assert bad.state == "error" and bad.items == []
    empty = fetch_balance("sk-test", opener=opener_ok(b'{"balance_infos": []}'))
    assert empty.state == "error" and empty.items == []


def test_deepseek_error_detail_never_carries_body():
    secret_body = b'{"error": "leak-me-sk-abc"}'
    quota = fetch_balance("sk-test", opener=opener_ok(secret_body + b" broken"))
    assert "leak-me-sk-abc" not in json.dumps(quota.to_dict())


# —— 2. .env 解析 ——————————————————————————————————————————————————


def test_read_env_file_handles_quotes_comments_and_blanks(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(
        "# 注释\n\nDEEPSEEK_API_KEY=sk-abc\nQUOTED=\"v 1\"\nSINGLE='v2'\nBAD LINE\nEMPTY=\n",
        encoding="utf-8",
    )
    env = read_env_file(path)
    assert env["DEEPSEEK_API_KEY"] == "sk-abc"
    assert env["QUOTED"] == "v 1"
    assert env["SINGLE"] == "v2"
    assert env["EMPTY"] == ""
    assert "BAD LINE" not in env


def test_read_env_file_missing_path_is_empty(tmp_path: Path):
    assert read_env_file(tmp_path / "nope.env") == {}


# —— 3. 账本 ————————————————————————————————————————————————————————


SCHEMA_SQL = """
create table session_model_usage (
  session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT,
  task TEXT, api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
  estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
  first_seen REAL, last_seen REAL)
"""


def make_db(path: Path, rows: list[tuple]) -> Path:
    con = sqlite3.connect(path)
    con.execute(SCHEMA_SQL)
    con.executemany("insert into session_model_usage values (" + ",".join("?" * 18) + ")", rows)
    con.commit()
    con.close()
    return path


def row(model: str, calls: int, cost: float, status, age_days: float = 1.0) -> tuple:
    seen = time.time() - age_days * 86400
    return ("sess", model, "", "", "", "", calls, 100 * calls, 20 * calls, 0, 0, 0,
            cost, 0.0, status, "t", seen, seen)


def test_ledger_separates_unpriced_rows_from_unknown_cost(tmp_path: Path):
    """「未计价的行」只做注脚；只有一行已计价都没有的模型才报「金额未知」。"""
    db = make_db(
        tmp_path / "state.db",
        [
            row("model-a", 3, 0.5, "estimated"),
            row("model-b", 2, 0.0, None),  # 全未计价 ⇒ 金额未知
            row("model-b", 1, 0.0, "unknown"),
        ],
    )
    scan = ledger.scan_db(db)
    by_model = {m["model"]: m for m in scan["models"]}
    assert by_model["model-a"]["cost_usd"] == 0.5 and by_model["model-a"]["cost_known"] is True
    assert by_model["model-a"]["unpriced_calls"] == 0
    assert by_model["model-b"]["cost_usd"] is None and by_model["model-b"]["cost_known"] is False
    assert by_model["model-b"]["unpriced_calls"] == 3
    assert scan["calls"] == 6
    # 合计只累加已计价的模型，未计价的行另报计数——**不许把有数的报成没数**
    assert scan["cost_usd"] == 0.5 and scan["cost_known"] is True
    assert scan["unpriced_calls"] == 3


def test_ledger_counts_amount_even_when_status_is_null(tmp_path: Path):
    """金额不该按 ``cost_status`` 过滤：默认库里 333 行是 ``NULL`` 状态但金额 > 0，
    按状态过滤会少报 13%（$38.60 vs $44.46）——这条就是那次少报的回归。"""
    db = make_db(
        tmp_path / "state.db",
        [row("m", 4, 1.5, None), row("m", 2, 0.25, "unknown")],
    )
    scan = ledger.scan_db(db)
    model = scan["models"][0]
    assert model["cost_usd"] == 1.75 and model["cost_known"] is True
    assert model["unpriced_calls"] == 0  # 两行都有金额 ⇒ 都不是「未计价」
    assert scan["cost_usd"] == 1.75


def test_ledger_only_zero_amount_rows_are_unknown(tmp_path: Path):
    """一行金额都没有的模型才报「未知」，未计价次数要落在它头上。"""
    db = make_db(tmp_path / "state.db", [row("m", 5, 0.0, None), row("m", 3, 0.0, "unknown")])
    scan = ledger.scan_db(db)
    model = scan["models"][0]
    assert model["cost_usd"] is None and model["cost_known"] is False
    assert model["unpriced_calls"] == 8
    assert scan["cost_usd"] is None and scan["unpriced_calls"] == 8


def test_ledger_one_model_with_mixed_pricing(tmp_path: Path):
    db = make_db(
        tmp_path / "state.db",
        [row("mixed", 4, 1.25, "estimated"), row("mixed", 2, 0.0, None)],
    )
    scan = ledger.scan_db(db)
    model = scan["models"][0]
    assert model["cost_usd"] == 1.25 and model["cost_known"] is True
    assert model["unpriced_calls"] == 2
    assert scan["cost_usd"] == 1.25


def test_ledger_window_and_missing_db(tmp_path: Path):
    db = make_db(tmp_path / "state.db", [row("old", 9, 9.0, "estimated", age_days=90)])
    assert ledger.scan_db(db)["calls"] == 0
    missing = ledger.scan_db(tmp_path / "nope.db")
    assert missing["calls"] == 0 and missing["readable"] is False


def test_ledger_snapshot_default_vs_all_profiles(tmp_path: Path):
    make_db(tmp_path / "state.db", [row("m", 4, 1.0, "estimated")])
    prof = tmp_path / "profiles" / "ops"
    prof.mkdir(parents=True)
    make_db(prof / "state.db", [row("m", 6, 2.0, "estimated")])
    books = ledger.snapshot([tmp_path / "state.db", prof / "state.db"])
    assert books["default_profile"]["calls"] == 4
    assert books["all_profiles"]["calls"] == 10
    assert books["all_profiles"]["cost_usd"] == 3.0


def test_ledger_snapshot_never_raises_on_garbage(tmp_path: Path):
    junk = tmp_path / "state.db"
    junk.write_text("not a database", encoding="utf-8")
    books = ledger.snapshot([junk])
    assert books["default_profile"]["readable"] is False
    assert books["all_profiles"]["calls"] == 0


# —— 4. 落盘 ————————————————————————————————————————————————————————


def test_store_atomic_write_0600_and_roundtrip(tmp_path: Path):
    target = tmp_path / "deep" / "quota.json"
    report = {"schema": 1, "updated_at": "now", "services": []}
    assert quota_store.save(report, target) is True
    assert target.read_text(encoding="utf-8") == json.dumps(report, ensure_ascii=False, indent=2)
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in target.parent.iterdir()] == ["quota.json"]  # 没有 .tmp 残留
    loaded, meta = quota_store.load(target)
    assert loaded == report and meta["stale"] is False and meta["age_s"] < 5


def test_store_marks_stale_and_survives_missing(tmp_path: Path):
    target = tmp_path / "quota.json"
    quota_store.save({"schema": 1}, target)
    old = time.time() - 3600
    os.utime(target, (old, old))
    _, meta = quota_store.load(target)
    assert meta["stale"] is True and meta["age_s"] > 300
    _, missing = quota_store.load(tmp_path / "nope.json")
    assert missing["exists"] is False and missing["stale"] is True


def test_store_save_failure_returns_false_without_raising(tmp_path: Path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        assert quota_store.save({"schema": 1}, ro / "quota.json") is False
    finally:
        os.chmod(ro, 0o700)


# —— 5. 整体：未知不许变 0、密钥不许外泄、单条失败不拖累别人 ——————————————


class BombAdapter:
    id, name, kind, console = "bomb", "炸的服务", "unknown", ""

    def fetch(self, ctx):
        raise RuntimeError("boom")


def test_collect_unknown_services_have_no_zero():
    report = collect(adapters=default_consoles(), ctx=QuotaContext(env={}, state_dbs=[]))
    for service in report["services"]:
        assert service["state"] == "unknown"
        assert service["items"] == []
        assert all(item["value"] != "0.00" for item in service["items"])
        assert service["console"].startswith("http")


def test_collect_survives_a_bomb_adapter():
    report = collect(adapters=[BombAdapter()], ctx=QuotaContext(env={}, state_dbs=[]))
    assert report["services"][0]["state"] == "error"
    assert "boom" in report["services"][0]["detail"]


def test_api_key_never_appears_in_report(tmp_path: Path):
    secret = "sk-test-xxxx-do-not-leak"
    ctx = QuotaContext(env={"DEEPSEEK_API_KEY": secret}, state_dbs=[])
    report = collect(adapters=[DeepSeekAdapter(opener=opener_ok())], ctx=ctx)
    assert report["services"][0]["state"] == "ok"
    assert secret not in json.dumps(report, ensure_ascii=False)


def test_refresh_writes_file_and_load_reads_it_back(tmp_path: Path):
    target = tmp_path / "quota.json"
    report, saved = refresh(target, adapters=[BombAdapter()], ctx=QuotaContext(env={}, state_dbs=[]))
    assert saved is True
    loaded, meta = quota_store.load(target)
    assert loaded is not None
    assert loaded["services"][0]["id"] == "bomb" and meta["stale"] is False
    assert report["schema"] == 1


# —— 6. 千问云 Token Plan（官方 CLI）————————————————————————————————


def fake_proc(stdout: str = "", stderr: str = "", rc: int = 0):
    return subprocess.CompletedProcess(args=["qianwen"], returncode=rc, stdout=stdout, stderr=stderr)


FREE_TIER_JSON = json.dumps(
    {
        "rows": [
            {
                "id": "qwen3.8-max",
                "model": "qwen3.8-max",
                "free_tier": {
                    "mode": "standard",
                    "quota": {"status": "active", "total": 1000000, "remaining": 734500, "unit": "tokens"},
                },
            },
            {
                "id": "qwen3.5-plus",
                "model": "qwen3.5-plus",
                "free_tier": {"mode": "Only", "quota": {"status": "expire", "total": 0, "remaining": 0, "unit": "tokens"}},
            },
        ],
        "total": 2,
        "hasQuota": True,
    }
)


def qwen_runner(stdout: str = FREE_TIER_JSON, rc: int = 0, *, sub_out: str = "", sub_rc: int = 0):
    """第一个调用回 free-tier，第二个回 subscription（顺序固定）。"""
    calls: list[list[str]] = []

    def _run(args, timeout):
        calls.append(list(args))
        if "subscription" in args:
            return fake_proc(sub_out, rc=sub_rc)
        return fake_proc(stdout, rc=rc)

    _run.calls = calls
    return _run


def test_qwen_parses_free_tier_and_credits(tmp_path: Path):
    runner = qwen_runner(sub_out=json.dumps({"remainingCredits": 1200, "totalCredits": 5000, "remainingDays": 21}))
    adapter = QwenTokenPlanAdapter(runner=runner, cred_dir=tmp_path / "nope")
    quota = adapter.fetch(QuotaContext(env={}, state_dbs=[]))
    assert quota.state == "ok"
    labels = {i.label: (i.value, i.unit) for i in quota.items}
    assert labels["qwen3.8-max 剩余"] == ("734,500", "tokens")
    assert labels["qwen3.8-max 总量"] == ("1,000,000", "tokens")
    assert labels["qwen3.5-plus 免费额度"] == ("已过期", "tokens")
    assert labels["Credits 剩余"] == ("1,200", "")
    assert labels["套餐剩余"] == ("21", "天")


def test_qwen_auth_required_is_stale_when_credentials_exist(tmp_path: Path):
    """凭据在但被拒 ⇒ ``stale``（这就是 ``STATE_STALE`` 的第一个产出者）。"""
    cred = tmp_path / ".qianwen"
    cred.mkdir()
    adapter = QwenTokenPlanAdapter(
        runner=qwen_runner("AUTH_REQUIRED: please login", rc=2), cred_dir=cred
    )
    quota = adapter.fetch(QuotaContext(env={}, state_dbs=[]))
    assert quota.state == "stale"
    assert quota.items == []  # 陈旧也不许编数
    assert "AUTH_REQUIRED" in (quota.detail or "")


def test_qwen_never_logged_in_is_unknown(tmp_path: Path):
    adapter = QwenTokenPlanAdapter(
        runner=qwen_runner("AUTH_REQUIRED", rc=2), cred_dir=tmp_path / "missing"
    )
    quota = adapter.fetch(QuotaContext(env={}, state_dbs=[]))
    assert quota.state == "unknown" and quota.items == []


def test_qwen_missing_binary_and_other_failures(tmp_path: Path):
    def boom(args, timeout):
        raise FileNotFoundError("qianwen")

    missing = QwenTokenPlanAdapter(runner=boom, cred_dir=tmp_path / "x")
    assert missing.fetch(QuotaContext(env={}, state_dbs=[])).state == "unknown"
    broken = QwenTokenPlanAdapter(
        runner=qwen_runner("totally not json"), cred_dir=tmp_path / "x"
    )
    assert broken.fetch(QuotaContext(env={}, state_dbs=[])).state == "error"


def test_qwen_credits_from_seat_tiers_addon_and_period(tmp_path: Path):
    """@researcher 从二进制里反解的四处兜底：顶层 / seatTiers / addonRemaining / period。"""
    payload = {
        "data": {
            "planName": "Token Plan",
            "status": "valid",
            "seatTiers": [
                {"seats": 2, "totalCredits": 1000, "remainingCredits": 400},
                {"seats": 1, "totalCredits": 500, "remainingCredits": 100},
            ],
            "addonRemaining": 250,
            "period": {"remainingDays": 9},
        }
    }
    adapter = QwenTokenPlanAdapter(
        runner=qwen_runner(sub_out=json.dumps(payload)), cred_dir=tmp_path / "x"
    )
    quota = adapter.fetch(QuotaContext(env={}, state_dbs=[]))
    labels = {i.label: (i.value, i.unit) for i in quota.items}
    assert labels["Token Plan 剩余"] == ("500", "")  # 两个坐席相加
    assert labels["Token Plan 总量"] == ("1,500", "")
    assert labels["共享用量包剩余"] == ("250", "")
    assert labels["套餐剩余"] == ("9", "天")


def test_qwen_total_zero_is_not_a_zero_balance(tmp_path: Path):
    """CLI 的边界语义：``totalCredits<=0`` 是「不限量/按量」，不许画成 0 余额。"""
    adapter = QwenTokenPlanAdapter(
        runner=qwen_runner(sub_out=json.dumps({"planName": "Token Plan", "totalCredits": 0, "remainingCredits": 0})),
        cred_dir=tmp_path / "x",
    )
    quota = adapter.fetch(QuotaContext(env={}, state_dbs=[]))
    labels = {i.label: i.value for i in quota.items}
    assert labels["Token Plan 额度"].startswith("按量/不限量")
    assert "Token Plan 剩余" not in labels


def test_qwen_exhaust_status_is_reported(tmp_path: Path):
    adapter = QwenTokenPlanAdapter(
        runner=qwen_runner(
            sub_out=json.dumps({"planName": "Token Plan", "status": "exhaust", "totalCredits": 100, "remainingCredits": 0})
        ),
        cred_dir=tmp_path / "x",
    )
    quota = adapter.fetch(QuotaContext(env={}, state_dbs=[]))
    assert any(i.value == "已用尽" for i in quota.items)


def test_qwen_subscription_help_text_is_not_data(tmp_path: Path):
    """``subscription`` 是命令组：光敲它打印帮助且 exit 0 —— 不能被当成数据。"""
    adapter = QwenTokenPlanAdapter(
        runner=qwen_runner(sub_out="Usage: qianwen subscription <status|orders>"), cred_dir=tmp_path / "x"
    )
    quota = adapter.fetch(QuotaContext(env={}, state_dbs=[]))
    assert quota.state == "ok"  # free-tier 那几条还在
    assert any(i.label.startswith("qwen3.8-max") for i in quota.items)
    assert "subscription" in (quota.detail or "")
    assert all("Credits" not in i.label for i in quota.items)


def test_qwen_subscription_uses_the_leaf_command(tmp_path: Path):
    runner = qwen_runner(sub_out=json.dumps({"remainingCredits": 5, "totalCredits": 10}))
    QwenTokenPlanAdapter(runner=runner, cred_dir=tmp_path / "x").fetch(
        QuotaContext(env={}, state_dbs=[])
    )
    sub_calls = [c for c in runner.calls if "subscription" in c]
    assert sub_calls and sub_calls[0][1:] == ["subscription", "status", "--plan", "token", "--format", "json"]


def test_qwen_subscription_failure_keeps_free_tier_items(tmp_path: Path):
    adapter = QwenTokenPlanAdapter(
        runner=qwen_runner(sub_out="boom", sub_rc=1), cred_dir=tmp_path / "x"
    )
    quota = adapter.fetch(QuotaContext(env={}, state_dbs=[]))
    assert quota.state == "ok"
    assert any(i.label.startswith("qwen3.8-max") for i in quota.items)
    assert "subscription" in (quota.detail or "")


# —— 7. 账本名单不许写死 ————————————————————————————————————————————


def test_default_state_dbs_globs_profiles_on_disk(tmp_path: Path):
    (tmp_path / "state.db").write_bytes(b"")
    for name in ("coder", "newbie"):
        d = tmp_path / "profiles" / name
        d.mkdir(parents=True)
        (d / "state.db").write_bytes(b"")
    (tmp_path / "profiles" / "empty").mkdir()  # 没有 state.db 的 profile 目录要被忽略
    dbs = base.default_state_dbs(tmp_path)
    assert [base.db_label(p) for p in dbs] == ["default", "coder", "newbie"]
    assert base.profile_dbs(tmp_path) == [tmp_path / "profiles/coder/state.db",
                                          tmp_path / "profiles/newbie/state.db"]


def test_snapshot_reports_db_count_from_disk(tmp_path: Path):
    (tmp_path / "state.db").write_bytes(b"")
    for name in ("a", "b", "c"):
        d = tmp_path / "profiles" / name
        d.mkdir(parents=True)
        make_db(d / "state.db", [row("m", 1, 0.1, "estimated")])
    books = ledger.snapshot(base.default_state_dbs(tmp_path))
    assert books["db_count"] == 4
    assert books["db_profiles"] == ["default", "a", "b", "c"]
    assert books["all_profiles"]["calls"] == 3


# —— 8. 对话气泡的候选（两行分组）————————————————————————————————


OK_REPORT = {
    "services": [
        {"id": "deepseek", "name": "DeepSeek", "state": "ok",
         "items": [{"label": "可用余额", "value": "75.00", "unit": "CNY"}]},
    ]
}


def test_bubble_candidates_are_two_line_groups_best_first():
    cands = badge_bubble_candidates(OK_REPORT, {"exists": True, "age_s": 480, "stale": False})
    assert cands[0] == ["DeepSeek", "¥75.00 · 8 分钟前"]
    assert all(1 <= len(c) <= 2 for c in cands)  # 气泡最多两行
    assert all(all(line.strip() for line in c) for c in cands)  # 没有半句空行
    assert len(cands[-1]) == 1  # 最后一定退到能放下的最短一条


def test_bubble_candidates_never_print_old_numbers_when_stale():
    cands = badge_bubble_candidates(OK_REPORT, {"exists": True, "age_s": 3600, "stale": True})
    flat = " ".join(" ".join(c) for c in cands)
    assert "陈旧" in flat and "75.00" not in flat  # 与面板同规矩：陈旧不印旧数字


def test_bubble_candidates_match_the_single_line_wording():
    """两处口径必须同源：气泡和单行条不许一个说陈旧、一个还在报数。"""
    stale = (OK_REPORT, {"exists": True, "age_s": 3600, "stale": True})
    assert "陈旧" in badge_text(*stale)
    assert "陈旧" in " ".join("".join(c) for c in badge_bubble_candidates(*stale))

    none_report = {"services": [{"id": "dashscope", "name": "百炼", "state": "unknown", "items": []}]}
    meta = {"exists": True, "age_s": 30, "stale": False}
    assert "去官网看余额" in badge_text(none_report, meta)
    assert "去官网看余额" in "".join("".join(c) for c in badge_bubble_candidates(none_report, meta))

    empty = (None, {"exists": False, "age_s": None, "stale": True})
    assert "未采集" in badge_text(*empty)
    assert "未采集" in "".join("".join(c) for c in badge_bubble_candidates(*empty))


def test_bubble_candidates_keep_the_amount_and_age():
    cands = badge_bubble_candidates(OK_REPORT, {"exists": True, "age_s": 480, "stale": False})
    flat = ["".join(c) for c in cands]
    assert any("¥75.00" in line for line in flat)
    assert any("分钟前" in line for line in flat)


# —— 9. 气泡三档（额度 / 设备 / 全部）———————————————————————————


class FakeDevice:
    """只带 device.Device 的那几个字段（纯函数不依赖 AppKit，也不起真采样）。"""

    def __init__(self, **kw):
        self.cpu_percent = kw.get("cpu_percent", 16.0)
        self.mem_used = kw.get("mem_used", 14_400_000_000)
        self.mem_total = kw.get("mem_total", 24_000_000_000)
        self.disk_used = kw.get("disk_used", 206_000_000_000)
        self.disk_total = kw.get("disk_total", 926_000_000_000)
        self.battery = kw.get("battery", (90, "接电源", "未充电"))


def test_device_candidates_two_lines_best_first():
    cands = device_bubble_candidates(FakeDevice())
    assert cands[0] == ["CPU 16% · 内存 60%", "磁盘 22% · 电池 90%"]
    assert all(1 <= len(c) <= 2 for c in cands)  # 气泡硬上限两行
    assert cands[-1] == ["CPU 16%"]


def test_device_candidates_without_battery_skip_the_part():
    """台式机没有电池：那一项直接不出现，而不是写「电池 未取到」。"""
    cands = device_bubble_candidates(FakeDevice(battery=None))
    flat = " ".join(" ".join(c) for c in cands)
    assert "电池" not in flat
    assert cands[0] == ["CPU 16% · 内存 60%", "磁盘 22%"]


def test_device_candidates_say_taken_failed_never_zero():
    """取不到就写「未取到」——不补 0（device.py 的规矩，气泡也得守）。"""
    cands = device_bubble_candidates(
        FakeDevice(cpu_percent=None, mem_used=None, mem_total=None, disk_used=None, disk_total=None,
                   battery=None)
    )
    flat = " ".join(" ".join(c) for c in cands)
    assert "未取到" in flat and "0%" not in flat


def test_action_enum_is_single_sourced_across_modules():
    """档位枚举/别名**只有一份**（`settings`），`quota.badge` 是引过来的、不是抄一份。

    照抄一份的代价：面板/桌宠/CLI 引不同的名字，加一档就得记得改好几处，漏一处就是
    「用户选了却不生效」或「把内部 token 印出来」。用 ``is`` 断言同一对象，抄一份就红。
    """
    from xiaocc import settings as S
    from xiaocc.quota import badge

    assert badge.ACTIONS is S.CLICK_ACTIONS
    assert badge._ACTION_ALIASES is S.CLICK_ACTION_ALIASES


def test_bubble_candidates_dispatch_and_caption_alias():
    dev = FakeDevice()
    meta = {"exists": True, "age_s": 480, "stale": False}
    assert bubble_candidates("none", report=OK_REPORT, meta=meta, device=dev) == []
    assert bubble_candidates("device", report=OK_REPORT, meta=meta, device=dev)[0][0].startswith("CPU")
    # 旧枚举名 `caption` 必须还能用（settings.json 里写着它的机器不能回落默认）
    assert bubble_candidates("caption", report=OK_REPORT, meta=meta, device=dev) == \
        bubble_candidates("device", report=OK_REPORT, meta=meta, device=dev)
    assert bubble_candidates("badge", report=OK_REPORT, meta=meta, device=dev)[0][0] == "DeepSeek"


def test_all_action_is_one_line_quota_plus_one_line_device():
    """「全部」= 额度一行 + 设备一行：**不许三行**（气泡只放得下两行）。"""
    cands = bubble_candidates(
        "all", report=OK_REPORT, meta={"exists": True, "age_s": 480, "stale": False}, device=FakeDevice()
    )
    assert cands[0] == ["¥75.00 · 8 分钟前", "CPU 16% · 内存 60%"]
    assert all(1 <= len(c) <= 2 for c in cands)


def test_all_action_survives_missing_device():
    cands = bubble_candidates(
        "all", report=OK_REPORT, meta={"exists": True, "age_s": 480, "stale": False}, device=None
    )
    assert all(c and c[0] for c in cands)
    assert all(1 <= len(c) <= 2 for c in cands)
    assert any("¥75.00" in "".join(c) for c in cands)


def test_snapshot_payload_has_no_dead_keys(tmp_path: Path):
    """交付面上只放**有人读**的字段：按模型明细不进 payload（@lead 拍板删）。

    用户 2026-09-29 的要求是「不要有功能不明的功能」——面板和 CLI 都不读 `models`，
    写进 quota.json 就只是没人用的死数据。要看明细用 `ledger.scan_db()`（内部仍给）。
    """
    make_db(tmp_path / "state.db", [row("m", 3, 0.5, "estimated")])
    prof = tmp_path / "profiles" / "a"
    prof.mkdir(parents=True)
    make_db(prof / "state.db", [row("m", 1, 0.25, "estimated")])
    books = ledger.snapshot(base.default_state_dbs(tmp_path))
    for section in (books["default_profile"], books["all_profiles"]):
        assert "models" not in section and "truncated" not in section
    assert books["default_profile"]["calls"] == 3
    assert books["all_profiles"]["calls"] == 4 and books["all_profiles"]["cost_usd"] == 0.75
    assert ledger.scan_db(prof / "state.db")["models"][0]["model"] == "m"  # 内部仍给明细
    assert ledger.snapshot([])["default_profile"]["calls"] == 0  # 一个库都没有也不炸


def test_default_quota_path_is_under_xiaocc():
    assert DEFAULT_QUOTA_PATH.name == "quota.json" and DEFAULT_QUOTA_PATH.parent.name == ".xiaocc"
