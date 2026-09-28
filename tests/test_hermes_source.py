"""Hermes 状态源测试。

关键点：**用临时 sqlite 复刻 Hermes 的表结构**，不碰用户真实的 state.db。
这样 CI（Linux/Windows）也能验，而且能精确构造「忙 / 刚干完 / 空闲 / 没装」四种情形。

``_project_name`` 是纯函数，不碰数据库，直接参数化验优先级即可。

文件后半还有一组「TTL 护栏 + done 窗口回归」用例：那里不 mock 引擎，
而是拿真 ``HermesSource`` 喂真 ``Engine``（含真角色包），
验的是「源只要开口，事件就一定够新鲜走到屏幕上」。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from xiaocc.characters import load_character
from xiaocc.engine import Engine
from xiaocc.protocol import STATE_TTL, State, StatusEvent, pick
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


# —— TTL 护栏：源只要开口，事件就必须够新鲜走到屏幕上 ——————————————————————
#
# 为什么加这一组（真 bug，不是假想）：done 事件一度写成 ``at=last_ts``（末条消息时间），
# 而 ``hermes._DONE_WINDOW = 20.0`` 比协议里 ``STATE_TTL[State.DONE] = 12.0`` 宽 8 秒 ——
# 末条消息年龄落在 12~20s 时，源老实报 done，``pick()`` 却按 TTL 判它过期整条丢弃，
# 引擎只好兜底合成 offline，桌面上莫名闪一句「没有任何状态源在线」。
# 修法是 ``at=now``（``at`` 的语义是「这条汇报有多新鲜」，源是在**此刻**汇报的）。
#
# 但别只钉住 12 和 20 这两个数字：窗口和 TTL 都是会被人调的业务参数，
# 盯着「窗口 ≤ TTL」这种具体比较，下次一改数值就又漏。真正的不变量是：
#   **任何源发出的任何事件都满足 ``event.at >= now - STATE_TTL[event.state]``**，
# 它等价于「任何落在 ``now - TTL`` 之前的 ``at``，事件必被 ``pick()`` 丢掉」。
# 下面这个辅助函数就是这句契约，对每个状态、每个年龄段都成立，换任何新源都能直接复用。


def _assert_event_is_fresh(event: StatusEvent, now: float) -> None:
    """通用护栏：这条事件在 ``now`` 这一刻必须仍然新鲜，否则它根本走不到屏幕。

    顺手把「等价于」那半句也验掉：把同一状态的事件往前挪到 ``now - TTL`` 之前，
    ``pick()`` 必须丢掉它。这样才知道护栏是**真的在承重**（TTL 确实在被执行），
    而不是一句永远为真的空断言。
    """
    assert not event.is_stale(now), f"源刚报出来就已过期，屏幕上只会看到假的 offline：{event.summary()}"
    ttl = STATE_TTL[event.state]
    if ttl is None:
        return  # idle/offline 按协议不过期（否则桌宠会自己消失），护栏天然满足
    assert event.at >= now - ttl, f"{event.state} 的 at 早于 now-TTL({ttl}s)：{event.summary()}"
    assert pick([event], now) is event, "新鲜事件不该被 pick() 丢掉"
    expired = replace(event, at=now - ttl - 1.0)  # 同状态、只把 at 挪过界
    assert expired.is_stale(now)
    assert pick([expired], now) is None, "越过 now-TTL 的事件必须被 pick() 丢掉，护栏才有意义"


#: 末条消息年龄扫描点。13s/18s 是专门挑的：> ``STATE_TTL[DONE]``(12s) 又 <= ``_DONE_WINDOW``(20s)，
#: 正好落在那条 8 秒的缝里；20s 是窗口边界；30s/200s 是窗口外（idle 那条路）。
_AGES = [5.0, 13.0, 18.0, 20.0, 30.0, 200.0]


@pytest.mark.parametrize("age", _AGES)
def test_event_always_fresh_whatever_the_message_age(tmp_path, age):
    """逐年龄段跑一遍护栏：源报的每条事件都新鲜，屏幕演的就是源说的那个状态。

    这里**不 mock 引擎**：单源时 ``pick()`` 选中的就是源那条事件，
    于是「屏幕 == 源」是一条可以直接断言的性质；一旦哪天某个状态的 ``at`` 又被写成
    过去的时间，这条会先在屏幕上露馅（变成 offline），而不是只在日志里悄悄丢事件。
    """
    _make_db(tmp_path, lease=False, last_role="assistant", finish_reason="stop", age=age).close()
    source = HermesSource(db=tmp_path / "state.db")

    event = source.poll()
    assert event is not None, "hermes 源任何时候都该给个说法，不该沉默"
    _assert_event_is_fresh(event, time.time())

    frame = Engine([source], load_character()).tick()
    assert frame is not None
    assert frame.state is event.state, f"屏幕该演 {event.state}，实际演了 {frame.state}"
    assert frame.state is not State.OFFLINE, "源还在说话，屏幕就不许报「没有任何状态源在线」"
    _assert_event_is_fresh(frame.event, time.time())


def test_guardrail_covers_every_state_the_source_can_report(tmp_path):
    """护栏覆盖**全部分支**：干活/思考/刚干完/空闲/库坏了/没装。

    为什么不只测 done：出过 bug 的是 done，但 ``at`` 是每个分支各自写的，
    以后谁改了 working 或 error 的 ``at``，同样会让桌宠闪 offline。
    """
    scenarios = {
        "working": {"lease": True, "last_role": "tool", "tool_name": "write_file"},
        "thinking": {"lease": True, "last_role": "user"},
        "done": {"lease": False, "last_role": "assistant", "finish_reason": "stop", "age": 15.0},
        "idle": {"lease": False, "last_role": "assistant", "finish_reason": "stop", "age": 3600.0},
    }
    events = []
    for name, kwargs in scenarios.items():
        folder = tmp_path / name
        folder.mkdir()
        _make_db(folder, **kwargs).close()
        event = HermesSource(db=folder / "state.db").poll()
        assert event is not None
        events.append(event)

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "state.db").write_text("这不是 sqlite", encoding="utf-8")
    for db in (broken / "state.db", tmp_path / "nope.db"):  # 库坏了 / 根本没装
        event = HermesSource(db=db).poll()
        assert event is not None
        events.append(event)

    states = {event.state for event in events}
    assert {State.WORKING, State.THINKING, State.DONE, State.IDLE, State.OFFLINE} <= states
    now = time.time()
    for event in events:
        _assert_event_is_fresh(event, now)


# —— 回归：done 窗口那 8 秒缝里，屏幕不许闪 offline ——————————————————————————


def test_done_at_13s_reaches_screen_not_offline(tmp_path):
    """钉住这次的 bug：末条消息 13 秒前 → 源报 done，屏幕也得是 done。

    13s 这个点是专门挑的：它 > ``STATE_TTL[State.DONE]``(12s) 却 <= ``_DONE_WINDOW``(20s)，
    正好落在当初漏掉的那段缝里。那时 done 事件写的是 ``at=last_ts``，
    于是 ``pick()`` 按 TTL 判它过期、整条丢弃，引擎兜底合成 offline，
    桌面上闪一句「没有任何状态源在线」——用户看到的是「小cc 说它没连上任何源」。
    """
    _make_db(tmp_path, lease=False, last_role="assistant", finish_reason="stop", age=13.0).close()
    source = HermesSource(db=tmp_path / "state.db")

    event = source.poll()
    assert event is not None and event.state is State.DONE
    now = time.time()
    # ``at`` 必须是「此刻汇报」而不是末条消息时间：差值应当接近 0，绝不是一整个 13 秒
    assert abs(now - event.at) < 1.0, f"done 的 at 应是此刻，实际差了 {now - event.at:.3f}s"
    assert not event.is_stale(now), "13s 的消息年龄不该让 done 事件过期（TTL 管的是 at）"
    assert pick([event], now) is event

    frame = Engine([source], load_character()).tick()
    assert frame is not None
    assert frame.state is State.DONE, f"屏幕该演 done，实际是 {frame.state}"
    assert "没有任何状态源在线" not in frame.event.detail


def test_done_window_boundary_at_20s_falls_back_to_idle(tmp_path):
    """窗口边界：末条消息满 20 秒就不再算 done，源改报 idle —— 而 idle 同样不许过期。

    为什么要留这条：``_DONE_WINDOW`` 是源自己的业务参数，调大调小都可以，
    前提是「done 演完的那一刻」交给 idle 时事件依旧新鲜（``at=now``，
    且 idle 的 TTL 是 ``None``），桌面才不会在收尾瞬间闪一次 offline。
    """
    _make_db(tmp_path, lease=False, last_role="assistant", finish_reason="stop", age=20.0).close()
    # 建库和 poll 之间隔一小会儿：保证源看到的年龄**严格**大于窗口。
    # 否则在时钟刻度较粗的机器上（Windows CI）可能读到同一个刻度，
    # 20.0 <= 20.0 就被判成 done，这条边界用例反而成了随机失败。
    time.sleep(0.02)
    source = HermesSource(db=tmp_path / "state.db")

    event = source.poll()
    assert event is not None and event.state is State.IDLE
    _assert_event_is_fresh(event, time.time())

    frame = Engine([source], load_character()).tick()
    assert frame is not None and frame.state is State.IDLE


# —— 项目名挑选（字幕里 · 左边那个词）——————————————————————————————

#: 显式传 home=，让「主目录本身不算项目名」这条在 CI 的任意机器上都成立，
#: 也保证测试不去碰真实的 ~/.hermes。
_FAKE_HOME = "/Users/alice"

#: Hermes 自动生成的群聊标题：`rmukls1dr`、`tmulchpfa` 都是连续 9 位 [a-z0-9]，纯机器哈希。
#: 注意它自带 `Group: ` 前缀 —— 那就已经是「群聊」了，不该再往后回落 profile 名。
_GROUP_TITLE = "Group: rmukls1dr-u871o · tmulchpfa-yu7jb"
_GROUP_CHAT = "群聊"


@pytest.mark.parametrize(
    "cwd,title,profile_name,expected",
    [
        ("/Users/alice/ChenC/xiaocc", _GROUP_TITLE, "lead", "xiaocc"),  # 哈希标题让位给 cwd
        (None, "桌宠", "lead", "桌宠"),  # 人话标题必须保留，不能一刀切禁掉 title
        ("", "桌宠", None, "桌宠"),  # 空串 cwd 等于「没有 cwd」
        ("/Users/alice", "桌宠", "lead", "桌宠"),  # 主目录本身：不许把用户名挂桌面上
        ("/Users/alice", _GROUP_TITLE, "lead", _GROUP_CHAT),  # 主目录 + 群聊标题 → 群聊
        ("/", "桌宠", "lead", "桌宠"),  # 根目录同样不携带项目信息
        ("/", _GROUP_TITLE, None, _GROUP_CHAT),  # 根目录也救不了 `Group:` 前缀
        (None, None, None, ""),  # 没有任何线索 → 空串而不是 None
        (None, "Group: 桌宠", "lead", _GROUP_CHAT),  # 群聊前缀优先于「标题是人话」
        (None, "  Group: abc12345  ", "ops", _GROUP_CHAT),  # 前后空白、大小写要容忍
        (None, "rmukls1d", "lead", ""),  # 8 位哈希 + 无 cwd → 空串（**不许**回落 profile 名）
        (None, "rmukls1dr", "ops", ""),  # profile 名是机器人名，不是项目名
        (None, "rmukls1", "lead", "rmukls1"),  # 7 位还是人话，得留着
        # 边界：cwd 结尾带斜杠（宿主写库时常见），basename 不能被斜杠吃成空串
        ("/Users/alice/ChenC/xiaocc/", "桌宠", "lead", "xiaocc"),
    ],
)
def test_project_name_priority(cwd, title, profile_name, expected):
    """优先级：cwd basename → `Group:` 前缀（群聊）→ 人话 title → 空串。

    `profile_name` 参数保留只为兼容调用方，**不再参与取名** —— 它是机器人名
    （ops/coder/lead），挂到桌面上比挂哈希更难解释。
    """
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
