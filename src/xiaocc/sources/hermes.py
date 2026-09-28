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
import sqlite3
import time
from pathlib import Path

from ..protocol import State, StatusEvent
from .base import StatusSource

__all__ = ["HermesSource", "find_state_dbs"]

#: 最后一条消息多久之内算「刚干完」（展示 done 动作然后自己回 idle）
_DONE_WINDOW = 20.0


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

        project = (row["title"] or row["cwd"] or "").strip()
        project = Path(project).name if "/" in project else project
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
