"""留痕记录器（``scripts/verify_log.py``）自己的门禁：**账本不可信 ⇒ 门禁的绿就不可信**。

为什么要给"记录留痕的东西"写测试（2026-09-30）：这一天出过两次「看着绿不等于绿」——
``verify_drag_tracking.py`` 全绿而真机是红的（它 5 个调用点清一色 ``move_window_to()``，鼠标
事件那条路一次都没走），以及 ``verify_drag_inject.py`` 锁屏 + ``--force`` 强跑照样给 rc=0。
留痕存在的唯一理由就是把这两种绿和「环境干净的真绿」在盘上分开 ⇒ 这里守的正是**盘上分不分
得开**，不是函数返回了什么。

红线：一律写进 ``tmp_path``（走 ``XIAOCC_VERIFY_LOGDIR`` 那个缝），绝不往真的
``~/Library/Logs/xiaocc`` 里塞测试账。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_log

GATE = "verify_fake_gate"
LOCKED = "屏是锁着的（锁屏时注入的事件会被系统吞掉）"
FIELDS = ("at", "gate", "rc", "blocker", "force", "env", "rev", "dirty", "pid", "criteria")


def _no_constants(name: str) -> float:
    raise AssertionError(f"账里出现了非法 JSON 常量 {name}（一行坏账能让整份历史读不回来）")


@pytest.fixture(autouse=True)
def fast_env_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """把环境快照的掐断时间调短。

    没有 GUI 会话的机器（CI、沙箱、ssh 进来的 shell）上那次 HID 读数**不是报错而是一直卡着**，
    2 秒 × 十几次调用会把测试拖成半分钟；调到 0.2s 顺便也就把"卡住 ⇒ 记 null"这条路跑了一遍。
    """
    monkeypatch.setattr(verify_log, "ENV_TIMEOUT_S", 0.2)


@pytest.fixture
def logdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """留痕指到沙箱目录（记录器每次调用都重读这个环境变量）。"""
    root = tmp_path / "logs"
    monkeypatch.setenv(verify_log.LOG_DIR_ENV, str(root))
    return root


def history(root: Path) -> list[dict]:
    """``verify.log`` 逐行读回；``NaN`` / ``Infinity`` 当场判失败（那不是合法 JSON）。"""
    text = (root / verify_log.LOG_NAME).read_text(encoding="utf-8")
    return [json.loads(line, parse_constant=_no_constants) for line in text.splitlines() if line]


def latest(root: Path, gate: str = GATE) -> dict:
    path = root / verify_log.LATEST_DIRNAME / f"{gate}.json"
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_no_constants)


def test_skipped_run_is_distinguishable_from_a_real_green(logdir: Path) -> None:
    """(a) 「环境不满足的 rc=2 + blocker」与「真绿 rc=0」在盘上不许长得一样。"""
    verify_log.record(GATE, 2, blocker=LOCKED)
    verify_log.record(GATE, 0)
    skipped, green = history(logdir)
    assert skipped["rc"] == 2 and skipped["blocker"] == LOCKED and "锁" in skipped["blocker"]
    assert green["rc"] == 0 and green["blocker"] is None
    assert (skipped["rc"], skipped["blocker"]) != (green["rc"], green["blocker"])


def test_forced_green_confesses_that_it_was_forced(logdir: Path) -> None:
    """(b) ``--force`` 如实落盘：锁屏强跑出来的 rc=0 得能一眼认出是强跑的。"""
    verify_log.record(GATE, 0, force=True, blocker=LOCKED)
    verify_log.record(GATE, 0)
    forced, clean = history(logdir)
    assert forced["rc"] == 0 and forced["force"] is True
    assert clean["rc"] == 0 and clean["force"] is False and clean["blocker"] is None
    assert (forced["force"], forced["blocker"]) != (clean["force"], clean["blocker"]), (
        "两种 rc=0 必须在盘上分得开，否则将来「锁屏强跑的绿」和「真绿」一字不差"
    )


def test_entry_says_when_which_code_and_what_environment(logdir: Path) -> None:
    """(c) at / rev / dirty / pid / env{locked,idle_s} 一个都不能少。"""
    path = verify_log.record(GATE, 0, criteria={"passed": 12, "checks": 12})
    entry = history(logdir)[0]
    for field in FIELDS:
        assert field in entry, f"少字段 {field}"
    assert entry["gate"] == GATE
    assert entry["pid"] == os.getpid()
    assert entry["criteria"] == {"passed": 12, "checks": 12}
    # 缺版本的绿对不上是哪棵树绿的；dirty 的绿只证明当时那棵没提交的树是绿的
    assert entry["rev"] is None or isinstance(entry["rev"], str)
    assert entry["dirty"] in (True, False, None)
    stamp = datetime.fromisoformat(entry["at"])
    assert stamp.tzinfo is not None, "at 必须带时区"
    assert stamp.microsecond == 0, "at 是秒精度"
    assert path is not None and path.exists()


def test_environment_is_snapshot_not_a_fabricated_false(logdir: Path) -> None:
    """(c') env 两个键都在；读不到就是 null，**绝不编一个 ``locked=False``** 进去。"""
    verify_log.record(GATE, 0)
    env = history(logdir)[0]["env"]
    assert set(env) == {"locked", "idle_s"}
    assert env["locked"] in (True, False, None)
    assert env["idle_s"] is None or isinstance(env["idle_s"], float)


def test_unreadable_environment_records_null(logdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境读数失败 ⇒ ``locked/idle_s`` 记 null，而 force/blocker 照样得留下。"""
    monkeypatch.setitem(sys.modules, "verify_drag_inject", None)  # 让那次 import 直接失败
    verify_log.record(GATE, 0, force=True, blocker=LOCKED)
    entry = history(logdir)[0]
    assert entry["env"] == {"locked": None, "idle_s": None}
    assert entry["force"] is True and entry["blocker"] == LOCKED


def test_latest_state_is_replaced_while_history_appends(logdir: Path) -> None:
    """(d) ``verify/<gate>.json`` 是最新态（覆盖），``verify.log`` 是历史账（两条）。"""
    verify_log.record(GATE, 1, criteria={"passed": 9, "checks": 12})
    first = latest(logdir)
    verify_log.record(GATE, 0, criteria={"passed": 12, "checks": 12})
    second = latest(logdir)
    assert first["rc"] == 1 and second["rc"] == 0, "最新态必须被第二次调用覆盖"
    rows = history(logdir)
    assert len(rows) == 2, "verify.log 是追加语义，不是覆盖"
    assert [row["rc"] for row in rows] == [1, 0]
    assert second == rows[-1], "最新态就是最后一条"
    names = sorted(p.name for p in (logdir / verify_log.LATEST_DIRNAME).iterdir())
    assert names == [f"{GATE}.json"], f"不许留下 tmp 垃圾：{names}"


def test_log_files_are_0600(logdir: Path) -> None:
    """日志一律 0600（与同目录 ``state.json`` / ``watchdog.json`` 一个口径）。"""
    verify_log.record(GATE, 0)
    paths = (logdir / verify_log.LOG_NAME, logdir / verify_log.LATEST_DIRNAME / f"{GATE}.json")
    for path in paths:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"{path.name} 权限是 {oct(mode)}"


def test_existing_0644_log_gets_tightened_to_0600(logdir: Path) -> None:
    """早先被 umask 建成 0644 的旧账，追加一次就该收回到 0600。"""
    logdir.mkdir(parents=True)
    old = logdir / verify_log.LOG_NAME
    old.write_text("", encoding="utf-8")
    old.chmod(0o644)
    verify_log.record(GATE, 0)
    assert stat.S_IMODE(old.stat().st_mode) == 0o600


def test_unwritable_log_directory_does_not_break_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(e) 目录建不出来：不抛、返回 None —— 留痕写不进去不该让门禁变红（更不该让它变绿）。"""
    not_a_dir = tmp_path / "blocker"
    not_a_dir.write_text("这是个文件，在它底下 mkdir 必定失败", encoding="utf-8")
    monkeypatch.setenv(verify_log.LOG_DIR_ENV, str(not_a_dir / "logs"))
    assert verify_log.record(GATE, 0) is None


def test_mkdir_failure_is_swallowed(logdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(e') 换成 ``mkdir`` 当场失败（盘满）也一样：不抛、返回 None。"""

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert verify_log.record(GATE, 0) is None
    assert not (logdir / verify_log.LOG_NAME).exists()


def test_junk_criteria_never_poisons_the_line(logdir: Path) -> None:
    """``nan`` / ``inf`` / 随便一个对象都不许进账，但这一行仍要是合法 JSON。"""
    verify_log.record(
        GATE,
        "0",
        criteria={"rate": float("nan"), "inf": float("inf"), "obj": object(), "ok": 1.5},
    )
    entry = history(logdir)[0]  # history() 里的 parse_constant 就是这条断言本身
    assert entry["rc"] == 0 and entry["criteria"] == {"ok": 1.5}


def test_gate_name_cannot_escape_the_latest_directory(logdir: Path) -> None:
    """门禁名要当文件名用：``../`` 不许把最新态写到 ``verify/`` 外面去。"""
    path = verify_log.record("../../etc/xiaocc-escape", 0)
    assert path is not None
    assert path.parent == logdir / verify_log.LATEST_DIRNAME


def test_default_log_dir_sits_next_to_xiaoccctl_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认落在 ``~/Library/Logs/xiaocc`` —— 与 ``xiaoccctl.log`` 同目录同族，不另发明地方。"""
    monkeypatch.delenv(verify_log.LOG_DIR_ENV, raising=False)
    assert verify_log._log_dir() == Path.home() / "Library" / "Logs" / "xiaocc"


def test_environment_snapshot_is_not_reimplemented_in_the_recorder() -> None:
    """锁屏/空闲的语义只有一份（在 ``verify_drag_inject`` 里）—— 谁抄第二份这条就红。

    这个项目反复吃亏的点就是同一件事抄两份、然后各改各的；判据（rc=2 那个前置）和证据
    （盘上这行 JSON）必须走同一个读数函数。
    """
    source = (SCRIPTS / "verify_log.py").read_text(encoding="utf-8")
    assert "import Quartz" not in source, "记录器不该自己伸手去碰 Quartz"
    assert "Quartz.CG" not in source, "锁屏/空闲的读数只许有 verify_drag_inject 那一份"
    assert "environment_snapshot" in source, "必须复用 verify_drag_inject 的那一个读数"
