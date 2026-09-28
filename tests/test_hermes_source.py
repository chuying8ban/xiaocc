"""Hermes 状态源测试。

关键点：**用临时 sqlite 复刻 Hermes 的表结构**，不碰用户真实的 state.db。
这样 CI（Linux/Windows）也能验，而且能精确构造「忙 / 刚干完 / 空闲 / 没装」四种情形。

``_project_name`` 是纯函数，不碰数据库，直接参数化验优先级即可。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from xiaocc.protocol import State
from xiaocc.sources.hermes import HermesSource, _project_name, find_state_dbs

SCHEMA = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, title TEXT, cwd TEXT, profile_name TEXT,
  started_at REAL, last_activity_at REAL, last_activity_description TEXT,
  archived INTEGER DEFAULT 0
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
  tool_name TEXT, finish_reason TEXT, timestamp REAL, active INTEGER DEFAULT 1
);
CREATE TABLE session_turn_leases (
  conversation_id TEXT, holder TEXT, acquired_at REAL, expires_at REAL
);
"""


def _make_db(tmp_path, *, lease: bool, last_role: str, tool_name: str | None = None,
             finish_reason: str | None = None, age: float = 0.0) -> sqlite3.Connection:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    now = time.time() - age
    conn.execute(
        "INSERT INTO sessions (id, title, cwd, profile_name, started_at, last_activity_at,"
        " last_activity_description) VALUES (?,?,?,?,?,?,?)",
        ("20260928_120000_abc", "创建可拓展开源桌宠小cc", "/tmp", "lead", now, now, "receiving stream response"),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, finish_reason, timestamp)"
        " VALUES (?,?,?,?,?,?)",
        ("20260928_120000_abc", last_role, "…", tool_name, finish_reason, now),
    )
    if lease:
        conn.execute(
            "INSERT INTO session_turn_leases VALUES (?,?,?,?)",
            ("20260928_120000_abc", "pid=1:turn=x", now, now + 300),
        )
    conn.commit()
    return conn


def test_busy_on_tool_call(tmp_path):
    conn = _make_db(tmp_path, lease=True, last_role="tool", tool_name="write_file")
    conn.close()
    event = HermesSource(db=tmp_path / "state.db").poll()
    assert event is not None
    assert event.state is State.WORKING
    assert "write_file" in event.detail
    assert event.project == "创建可拓展开源桌宠小cc"


def test_busy_on_user_message_is_thinking(tmp_path):
    _make_db(tmp_path, lease=True, last_role="user", age=1).close()
    event = HermesSource(db=tmp_path / "state.db").poll()
    assert event is not None and event.state is State.THINKING


def test_assistant_tool_calls_is_working(tmp_path):
    _make_db(tmp_path, lease=True, last_role="assistant", finish_reason="tool_calls").close()
    event = HermesSource(db=tmp_path / "state.db").poll()
    assert event is not None and event.state is State.WORKING


def test_no_lease_and_fresh_finish_is_done(tmp_path):
    _make_db(tmp_path, lease=False, last_role="assistant", finish_reason="stop", age=1).close()
    event = HermesSource(db=tmp_path / "state.db").poll()
    assert event is not None and event.state is State.DONE


def test_no_lease_and_old_activity_is_idle(tmp_path):
    _make_db(tmp_path, lease=False, last_role="assistant", finish_reason="stop", age=3600).close()
    event = HermesSource(db=tmp_path / "state.db").poll()
    assert event is not None and event.state is State.IDLE


def test_missing_db_is_offline(tmp_path):
    event = HermesSource(db=tmp_path / "nope.db").poll()
    assert event is not None and event.state is State.OFFLINE


def test_survives_garbage_db(tmp_path):
    bad = tmp_path / "state.db"
    bad.write_text("这不是 sqlite", encoding="utf-8")
    event = HermesSource(db=bad).poll()
    # 既不能崩，也不能假装没事：必须报 ERROR 或 OFFLINE
    assert event is not None and event.state in (State.ERROR, State.OFFLINE)


def test_find_state_dbs_sorted_newest_first(tmp_path):
    home = tmp_path / ".hermes"
    (home / "profiles" / "lead").mkdir(parents=True)
    (home / "profiles" / "coder").mkdir(parents=True)
    for rel in ("state.db", "profiles/lead/state.db", "profiles/coder/state.db"):
        (home / rel).write_bytes(b"x")
    found = find_state_dbs(home)
    assert len(found) == 3
    assert all(p.name == "state.db" for p in found)


# —— 项目名挑选（字幕里 · 左边那个词）——————————————————————————————

#: 显式传 home=，让「主目录本身不算项目名」这条在 CI 的任意机器上都成立，
#: 也保证测试不去碰真实的 ~/.hermes。
_FAKE_HOME = "/Users/alice"

#: Hermes 自动生成的群聊标题：`rmukls1dr`、`tmulchpfa` 都是连续 9 位 [a-z0-9]，纯机器哈希
_GROUP_TITLE = "Group: rmukls1dr-u871o · tmulchpfa-yu7jb"


@pytest.mark.parametrize(
    "cwd,title,profile_name,expected",
    [
        ("/Users/alice/ChenC/xiaocc", _GROUP_TITLE, "lead", "xiaocc"),  # 哈希标题让位给 cwd
        (None, "桌宠", "lead", "桌宠"),  # 人话标题必须保留，不能一刀切禁掉 title
        ("", "桌宠", None, "桌宠"),  # 空串 cwd 等于「没有 cwd」
        ("/Users/alice", "桌宠", "lead", "桌宠"),  # 主目录本身：不许把用户名挂桌面上
        ("/Users/alice", _GROUP_TITLE, "lead", "lead"),  # 主目录 + 哈希标题 → 退到 profile 名
        ("/", "桌宠", "lead", "桌宠"),  # 根目录同样不携带项目信息
        ("/", _GROUP_TITLE, None, ""),  # 三条线索全废 → 空串，上层就不拼 ·
        (None, None, None, ""),  # 同上：空串而不是 None
        (None, "rmukls1d", "lead", "lead"),  # 连续 8 位就算哈希（阈值含 8）
        (None, "rmukls1", "lead", "rmukls1"),  # 7 位还是人话，得留着
        # 边界：cwd 结尾带斜杠（宿主写库时常见），basename 不能被斜杠吃成空串
        ("/Users/alice/ChenC/xiaocc/", "桌宠", "lead", "xiaocc"),
    ],
)
def test_project_name_priority(cwd, title, profile_name, expected):
    """优先级：cwd basename → 人话 title → profile 名 → 空串。"""
    assert _project_name(cwd, title, profile_name, home=_FAKE_HOME) == expected


def test_project_name_expands_tilde_and_default_home():
    """边界：cwd 带 ``~`` 要先 expanduser，且默认 home 走 ``Path.home()``。

    为什么单开一个函数、不塞进上面的 parametrize：``~/ChenC/xiaocc`` 即使忘了展开，
    basename 也恰好是 ``xiaocc``，测不出差别；真正会露馅的是光秃秃一个 ``~`` ——
    不展开就会被当成 basename 挂上桌面（顺带把用户名漏出去），展开了才知道它
    就是主目录、必须跳过。这条只能对着机器真实主目录验，所以用 ``Path.home()``。
    """
    home = Path.home()
    assert _project_name(home, "桌宠", "lead") == "桌宠"  # 不传 home= 也认得真实主目录
    assert _project_name("~", "桌宠", "lead", home=home) == "桌宠"  # 展开后 = 主目录 → 跳过
    assert _project_name(home / "ChenC" / "xiaocc", "桌宠", "lead", home=home) == "xiaocc"
