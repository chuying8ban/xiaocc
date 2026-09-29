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


def test_default_quota_path_is_under_xiaocc():
    assert DEFAULT_QUOTA_PATH.name == "quota.json" and DEFAULT_QUOTA_PATH.parent.name == ".xiaocc"
