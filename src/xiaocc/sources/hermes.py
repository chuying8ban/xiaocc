"""Hermes 状态源：直接读 Hermes 自己的 state.db（只读），把真实的 Agent 活动演出来。

这是小cc 的第一个「真宿主」接入。和大肥鱼的关键区别：大肥鱼是 DSH 插件，
生命周期和显示层都被宿主绑死；小cc 只是**读一眼**宿主的状态，
宿主删了、关了、换了，小cc 照样活着（顶多显示 idle）。

数据来源（全部只读，不会影响 Hermes 运行）：
  * ``sessions``             —— 会话标题/工作目录/最后活动描述 = 桌宠上的「项目名 + 阶段」
  * ``messages``             —— 最新一条消息的角色与工具名 = 「在思考」还是「在干活」
  * ``session_turn_leases``  —— 有租约 = 这轮任务正在跑（最可靠的「忙不忙」信号）

探测链：租约 → 最新消息 → 会话元数据，任何一步拿不到就退到更保守的状态，
并且**宁可报 idle 也不报假进度**。
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from ..protocol import State, StatusEvent
from .base import StatusSource

__all__ = ["HermesSource", "find_state_dbs"]

#: 最后一条消息多久之内算「刚干完」（展示 done 动作然后自己回 idle）
_DONE_WINDOW = 20.0

#: 标题里出现这么长的一串小写字母/数字，就认定是机器生成的会话标识（哈希）
_HASH_RUN = re.compile(r"[a-z0-9]{8,}")

#: 群聊会话的标题前缀（Hermes 写成 ``Group: <会话标识>``）：大小写不敏感、容忍前后空白
_GROUP_TITLE = re.compile(r"^\s*group\s*:", re.IGNORECASE)

#: 群聊在字幕上占的那个词：群聊本身就是答案，不再往后回落
_GROUP_LABEL = "群聊"

#: 系统临时目录：和主目录/根目录一样，不说明「在哪个项目里干活」
_TEMP_DIRS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp")


def _project_name(
    cwd: str | os.PathLike[str] | None,
    title: str | None,
    profile_name: str | None,
    *,
    home: str | os.PathLike[str] | None = None,
) -> str:
    """给桌宠字幕挑一个「人类看得懂」的项目名，按优先级取第一个可用的。

    优先级（主管定死的契约）：

    1. ``cwd`` 的 basename —— 会话的工作目录最能说明「在哪个项目里干活」。
       取 basename 前先 ``Path(cwd).expanduser()`` 规整；``cwd`` 为空、
       或规整后等于下面这些「不携带项目信息」的目录时跳过：
       用户主目录本身（``/Users/xxx``、``/home/xxx``）、根目录 ``/``、
       以及系统临时目录（``/tmp`` 等，会话挂在临时目录里说明它不属于任何项目）。
    2. ``title`` 以 ``Group:`` 开头（大小写不敏感、容忍前后空白）→ 返回 ``群聊``，
       并且**不再往后回落**：群聊本身就是答案。前缀后面那串
       ``rmukls1dr-u871o · tmulchpfa-yu7jb`` 是机器生成的会话标识，
       挂到桌面上对人没有任何意义，只会把桌面弄脏。
    3. 其余 ``title`` —— 但只有「人话」标题才用。
       判定规则：标题里只要出现**连续 8 个及以上**的 ``[a-z0-9]``
       （小写字母或数字，例如 ``rmukls1dr``、``1a2b3c4d5e``），
       就认为这是 Hermes 自动生成的会话标识（哈希串），**整条标题作废**，
       继续往下找。
       为什么不要哈希标题：字幕是直接挂在桌面上的，一串哈希没人看得懂；
       而人类自己起的标题（``桌宠``、``cupk 论坛``）恰好不含这种长串，必须保留。
    4. 都没有 → 返回 ``""``，让上层（``protocol.py`` 拼 ``·`` 分隔符的地方）
       自然不显示分隔符，而不是挂一个空的分隔符在字幕上。

    ``profile_name`` **不参与取名**，只是留在签名上（调用方照旧传）：它是机器人名
    （``ops``/``coder``/``lead``），说的是「哪个机器人在干活」，不是「在哪个项目里
    干活」，拿它顶替项目名会把两件事混为一谈 —— 群聊会话没有 cwd、标题又全是哈希，
    桌面上就会凭空冒出一个 ``ops``。宁可空着。
    """
    if cwd:
        directory = Path(cwd).expanduser()
        home_dir = Path(home).expanduser() if home else Path.home()
        boring = {home_dir, Path(directory.anchor), Path(tempfile.gettempdir())}
        boring.update(Path(item) for item in _TEMP_DIRS)
        if directory.name and directory not in boring:
            return directory.name

    text = (title or "").strip()
    if _GROUP_TITLE.match(text):
        return _GROUP_LABEL
    if text and not _HASH_RUN.search(text):
        return text

    return ""


def find_state_dbs(home: str | os.PathLike[str] | None = None) -> list[Path]:
    """列出本机所有 Hermes profile 的 state.db，最新的排前面。"""
    root = Path(home).expanduser() if home else Path.home() / ".hermes"
    found: list[tuple[float, Path]] = []
    for candidate in [root / "state.db", *sorted((root / "profiles").glob("*/state.db"))]:
        try:
            found.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    return [path for _, path in sorted(found, key=lambda item: item[0], reverse=True)]


class HermesSource(StatusSource):
    name = "hermes"
    interval = 1.0
    description = "读取 Hermes state.db，把真实 Agent 活动映射成状态（默认自动挑最新 profile）"

    def __init__(
        self,
        db: str | os.PathLike[str] | None = None,
        *,
        profile: str | None = None,
        home: str | os.PathLike[str] | None = None,
        stale_after: float = 120.0,
    ) -> None:
        self._home = Path(home).expanduser() if home else Path.home() / ".hermes"
        self._explicit = Path(db).expanduser() if db else None
        self._profile = profile
        self.stale_after = stale_after
        self._db_path: Path | None = None
        self._conn: sqlite3.Connection | None = None

    @property
    def label(self) -> str:
        return f"hermes:{self._db_path.name if self._db_path else '(未连接)'}"

    # —— 连接 ——
    def _connect(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        path = self._explicit
        if path is None:
            if self._profile:
                path = self._home / "profiles" / self._profile / "state.db"
            else:
                dbs = find_state_dbs(self._home)
                path = dbs[0] if dbs else None
        if path is None or not path.exists():
            return None
        # 只读 + 不建 WAL：绝不干扰运行中的 Hermes
        uri = f"file:{path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return None
        self._db_path = path
        self._conn = conn
        return conn

    def _reset(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self._db_path = None

    def close(self) -> None:
        self._reset()

    # —— 主逻辑 ——
    def poll(self) -> StatusEvent | None:
        conn = self._connect()
        if conn is None:
            return StatusEvent(
                source=self.label,
                state=State.OFFLINE,
                detail="没找到 Hermes 的 state.db（Hermes 没装或没跑过）",
            )
        try:
            return self._poll_once(conn)
        except sqlite3.Error as exc:
            self._reset()  # db 被换过/锁住：断开，下一轮重连
            return StatusEvent(source=self.label, state=State.ERROR, detail=f"读取 state.db 失败：{exc}")

    def _poll_once(self, conn: sqlite3.Connection) -> StatusEvent | None:
        now = time.time()
        lease = conn.execute(
            """
            SELECT conversation_id, holder, expires_at FROM session_turn_leases
            WHERE expires_at > ? ORDER BY acquired_at DESC LIMIT 1
            """,
            (now,),
        ).fetchone()

        session_id = lease["conversation_id"] if lease else self._newest_session_id(conn, now)
        if not session_id:
            return StatusEvent(source=self.label, state=State.IDLE, detail="Hermes 空闲")

        row = conn.execute(
            """
            SELECT s.title, s.cwd, s.profile_name, s.last_activity_at, s.last_activity_description,
                   m.role, m.tool_name, m.finish_reason, m.timestamp
            FROM sessions s
            LEFT JOIN messages m ON m.id = (
                SELECT id FROM messages WHERE session_id = s.id ORDER BY id DESC LIMIT 1
            )
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return StatusEvent(source=self.label, state=State.IDLE, detail="Hermes 空闲")

        project = _project_name(row["cwd"], row["title"], row["profile_name"])
        last_ts = row["timestamp"] or row["last_activity_at"] or 0.0
        age = max(0.0, now - float(last_ts))
        stage = (row["last_activity_description"] or "").strip()

        if lease:
            state, detail = self._busy_state(row, stage)
            return StatusEvent(
                source=self.label, state=state, detail=detail, project=project, at=now
            )

        # 没租约：刚干完就亮一下 done，否则 idle
        if age <= _DONE_WINDOW and row["role"] in ("assistant", "tool"):
            return StatusEvent(
                source=self.label,
                state=State.DONE,
                detail=stage or "这轮做完了",
                project=project,
                at=last_ts,
            )
        if float(last_ts) and (now - float(last_ts)) <= self.stale_after:
            return StatusEvent(
                source=self.label, state=State.IDLE, detail=stage, project=project, at=now
            )
        return StatusEvent(source=self.label, state=State.IDLE, detail="Hermes 空闲", at=now)

    @staticmethod
    def _busy_state(row: sqlite3.Row, stage: str) -> tuple[State, str]:
        role = row["role"] or ""
        if role == "tool":
            # 工具名是具体的、「receiving stream response」是宿主内部黑话，优先前者
            tool = row["tool_name"] or ""
            return State.WORKING, (f"正在执行 {tool}" if tool else (stage or "正在执行工具"))
        if role == "assistant":
            if (row["finish_reason"] or "") == "tool_calls":
                return State.WORKING, "准备调用工具"
            return State.THINKING, "正在思考"
        if role == "user":
            return State.THINKING, "刚收到消息，开始处理"
        return State.THINKING, stage or "运行中"

    def _newest_session_id(self, conn: sqlite3.Connection, now: float) -> str | None:
        row = conn.execute(
            """
            SELECT id FROM sessions
            WHERE COALESCE(archived, 0) = 0
            ORDER BY COALESCE(last_activity_at, started_at, 0) DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return row["id"]
