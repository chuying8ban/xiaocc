"""文件状态源：盯一个 JSON 文件。

这是「万能胶」——任何脚本只要能写一个 JSON 文件，就能驱动小cc：

    echo '{"state":"working","detail":"编译中","step":2,"total":5}' > ~/.xiaocc/status.json

文件被删、读不出来、内容不合法都属于**机械故障**：直接抛异常，交给引擎隔离
（按消息去重后写日志 + 记进 ``health()``），源自己不造 ERROR/OFFLINE 事件。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
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
    ``{"state":"idle"}`` 或 ``offline``）。

    故障分工——**机械故障不进画面**：文件不存在 / 读不出来 / 内容不合法（坏 JSON、
    缺字段、未知状态都算）时 :meth:`poll` 直接抛异常，由引擎负责隔离：捕获 → 按消息
    去重后 ``log.warning`` 一行 → 记进 ``health()`` → 不产生事件；没有任何源说话时才由
    引擎兜底呈现 ``offline`` 并把原因写进 ``detail``。源自己抢着报 offline/error 会污染
    「谁在说话」的判断。``ERROR`` 状态只留给**工作流自报**：文件内容里写着
    ``state == "error"`` 时照常返回 ERROR 事件。
    """

    name = "file"
    interval = 0.75
    description = "读取一个 JSON 状态文件（任何脚本都能写）"

    def __init__(self, path: str | os.PathLike[str], *, source_name: str | None = None) -> None:
        self.path = Path(path).expanduser()
        self._mtime: float | None = None
        self._cached: StatusEvent | None = None
        if source_name:
            self.name = source_name

    @property
    def label(self) -> str:
        return f"file:{self.path}"

    def poll(self) -> StatusEvent | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError as exc:
            self._forget()
            raise FileNotFoundError(f"没有这个文件：{self.path}") from exc
        except OSError:
            # 机械故障：原样抛出去（原因文字都在异常里），不自己造事件。
            # 顺手清掉 mtime，下一轮才会重试读取，而不是拿旧缓存糊弄过去。
            self._forget()
            raise

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
        except OSError:
            self._forget()
            raise
        if not text:
            self._cached = None
            return None

        try:
            event = StatusEvent.from_json(text)
        except ValueError as exc:
            # 坏 JSON / 缺字段 / 未知状态都是机械故障，抛给引擎去重记日志。
            self._forget()
            raise ValueError(f"{self.path} 内容不合法：{exc}") from exc

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
        # 临时名必须唯一（mkstemp）：file 源的用途就是让别人来写这个状态文件，
        # 对方若也按``<文件>.tmp``再改名的通行写法，就会跟这里抢同一个临时文件。
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
            Path(tmp_name).replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink()
            raise
        return self.path
