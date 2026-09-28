"""示例：给小cc 接一个番茄钟状态源。

这是 docs/sources.md 里「三分钟接一个新状态源」的可运行版本，
故意只用一个文件、零依赖：照抄改改就能变成你自己的源。

    .venv/bin/pip install -e examples/pomodoro-source
    xiaocc run --source pomodoro:50 -b terminal
"""

from __future__ import annotations

import time

from xiaocc.protocol import State, StatusEvent
from xiaocc.sources.base import StatusSource


class PomodoroSource(StatusSource):
    name = "pomodoro"
    description = "番茄钟：专注 N 分钟，然后休息 5 分钟"
    interval = 1.0

    def __init__(self, minutes: float = 25.0) -> None:
        self.minutes = float(minutes)
        self.started = time.time()

    def poll(self) -> StatusEvent | None:
        elapsed = time.time() - self.started
        if elapsed < self.minutes * 60:
            return StatusEvent(
                source=self.name,
                state=State.WORKING,
                project="pomodoro",
                detail=f"专注中（还剩 {self.minutes * 60 - elapsed:.0f}s）",
            )
        return StatusEvent(source=self.name, state=State.DONE, detail="这一轮结束了，起来走走")

    def close(self) -> None:
        self.started = time.time()
