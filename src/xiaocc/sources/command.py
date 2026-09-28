"""命令状态源：跑一条命令，读它 stdout 上的 JSON。

给「不想装 Python 也不想让对方读文件」的场合用：任何语言的脚本或一行 shell 都行。

    xiaocc watch --source 'command:cat ~/.cache/build-status.json'
    xiaocc watch --source 'command:mytool status --json'

约定（违反即报 ERROR，不猜）：
  * 退出码 0 且 stdout 有 JSON 对象  → 该状态
  * 退出码 0 且 stdout 为空          → 这条源本轮无话可说（None）
  * 退出码非 0                       → ERROR，detail 带 stderr 首行
"""

from __future__ import annotations

import json
import shlex
import subprocess

from ..protocol import State, StatusEvent
from .base import StatusSource

__all__ = ["CommandSource"]


class CommandSource(StatusSource):
    name = "command"
    interval = 2.0
    description = "执行一条命令，解析 stdout 上的 JSON"

    def __init__(
        self,
        command: str,
        *,
        timeout: float = 5.0,
        source_name: str | None = None,
    ) -> None:
        self.command = command
        self.timeout = timeout
        self._argv = shlex.split(command)
        if not self._argv:
            raise ValueError("命令不能为空")
        self._last_error: str | None = None
        if source_name:
            self.name = source_name

    @property
    def label(self) -> str:
        return f"command:{self.command}"

    def poll(self) -> StatusEvent | None:
        try:
            proc = subprocess.run(
                self._argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError:
            return self._error("命令不存在：%s" % self._argv[0])
        except subprocess.TimeoutExpired:
            return self._error(f"命令超过 {self.timeout:g}s 没返回")

        if proc.returncode != 0:
            first = (proc.stderr or proc.stdout or "").strip().splitlines()
            return self._error(f"退出码 {proc.returncode}：{first[0] if first else '(无输出)'}")

        text = (proc.stdout or "").strip()
        if not text:
            self._last_error = None
            return None
        try:
            event = StatusEvent.from_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(f"stdout 不是合法状态 JSON：{exc}")

        self._last_error = None
        return StatusEvent(
            source=self.label,
            state=event.state,
            at=event.at,
            detail=event.detail,
            project=event.project,
            step=event.step,
            total=event.total,
            raw=event.raw,
        )

    def _error(self, detail: str) -> StatusEvent | None:
        # 同一个故障只报一次，避免命令永久坏掉时每秒刷一条 ERROR
        if self._last_error == detail:
            return None
        self._last_error = detail
        return StatusEvent(source=self.label, state=State.ERROR, detail=detail)
