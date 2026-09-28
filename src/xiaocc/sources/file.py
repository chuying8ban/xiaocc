"""文件状态源：盯一个 JSON 文件。

这是「万能胶」——任何脚本只要能写一个 JSON 文件，就能驱动小cc：

    echo '{"state":"working","detail":"编译中","step":2,"total":5}' > ~/.xiaocc/status.json

文件被删或还没创建时报告 OFFLINE，而不是报错退出。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..protocol import State, StatusEvent
from .base import StatusSource

__all__ = ["FileSource"]


class FileSource(StatusSource):
    """盯一个 JSON 文件。

    这是「万能胶」——任何脚本只要能写一个 JSON 文件，就能驱动小cc::

        echo '{"state":"working","detail":"编译中","step":2,"total":5}' > ~/.xiaocc/status.json

    语义：**文件内容就是当前状态**，文件没被改过不代表状态过期（要收工就写
    ``{"state":"idle"}`` 或 ``offline``）。文件还不存在时报告 OFFLINE，而不是报错退出。
    """

    name = "file"
    interval = 0.75
    description = "读取一个 JSON 状态文件（任何脚本都能写）"

    def __init__(self, path: str | os.PathLike[str], *, source_name: str | None = None) -> None:
        self.path = Path(path).expanduser()
        self._mtime: float | None = None
        self._cached: StatusEvent | None = None
        self._warned: str | None = None
        if source_name:
            self.name = source_name

    @property
    def label(self) -> str:
        return f"file:{self.path}"

    def poll(self) -> StatusEvent | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._forget()
            return StatusEvent(
                source=self.label, state=State.OFFLINE, detail=f"没有这个文件：{self.path}"
            )
        except OSError as exc:
            return StatusEvent(source=self.label, state=State.ERROR, detail=str(exc))

        if self._mtime == stat.st_mtime:
            # 内容没动：把时间戳刷成现在，让「文件没被改过」不等于「状态过期」。
            # 文件状态源把文件内容当作当前状态——要收工就写 state=offline/idle。
            if self._cached is not None:
                self._cached = StatusEvent(
                    source=self._cached.source,
                    state=self._cached.state,
                    at=time.time(),
                    detail=self._cached.detail,
                    project=self._cached.project,
                    step=self._cached.step,
                    total=self._cached.total,
                    raw=self._cached.raw,
                )
            return self._cached
        self._mtime = stat.st_mtime

        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return StatusEvent(source=self.label, state=State.ERROR, detail=str(exc))
        if not text:
            self._cached = None
            return None

        try:
            event = StatusEvent.from_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            # 坏数据不静默吞掉，但也不刷屏：同一原因只喊一次
            message = f"{self.path} 内容不合法：{exc}"
            if self._warned != message:
                self._warned = message
                return StatusEvent(source=self.label, state=State.ERROR, detail=message)
            self._cached = None
            return None

        self._warned = None
        # 不用文件里的 at（可能是旧时间戳，TTL 会误判过期）；内容没变时由上面的
        # 分支负责刷新时间戳，所以这里用当前时间。
        self._cached = StatusEvent(
            source=self.label,
            state=event.state,
            at=time.time(),
            detail=event.detail,
            project=event.project,
            step=event.step,
            total=event.total,
            raw=event.raw,
        )
        return self._cached

    def _forget(self) -> None:
        self._mtime = None
        self._cached = None

    def touch(self, **payload: object) -> Path:
        """辅助方法（测试和脚本用）：原子写一份状态。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload.setdefault("state", State.IDLE.value)
        payload.setdefault("source", self.label)
        payload.setdefault("at", time.time())
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        return self.path
