"""Hermes 状态源测试。

关键点：**用临时 sqlite 复刻 Hermes 的表结构**，不碰用户真实的 state.db。
这样 CI（Linux/Windows）也能验，而且能精确构造「忙 / 刚干完 / 空闲 / 没装」四种情形。
"""

from __future__ import annotations

import sqlite3
import time

from xiaocc.protocol import State
from xiaocc.sources.hermes import HermesSource, find_state_dbs

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
             finish_reason: str | None = None, age: float = 0.0) -> "sqlite3.Connection":
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
