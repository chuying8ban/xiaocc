"""终端显示层。

不是玩具：它是**无头环境下的验收手段**。人在外面只有 SSH 时，
``xiaocc run --backend console`` 能证明状态源、引擎、角色三层都通了；
CI 里也用它对引擎做端到端断言。
"""

from __future__ import annotations

import sys
from typing import TextIO

from ..engine import Render
from ..protocol import State
from .base import Backend

__all__ = ["ConsoleBackend"]

#: 每个状态一张脸。故意做得克制——这是调试视图，不是角色的本体设计。
FACES: dict[State, str] = {
    State.IDLE: "( ·  · )",
    State.THINKING: "( ·  · )?",
    State.WORKING: "( >  < )",
    State.WAITING: "( o  o )!",
    State.DONE: "( ^  ^ )",
    State.ERROR: "( x  x )",
    State.OFFLINE: "( -  - )z",
}


class ConsoleBackend(Backend):
    name = "console"

    def __init__(self, stream: TextIO | None = None, *, show_face: bool = True) -> None:
        self.stream = stream or sys.stdout
        self.show_face = show_face
        self.frames = 0

    def render(self, frame: Render) -> None:
        self.frames += 1
        name = frame.character.name
        face = f"{FACES.get(frame.state, '( ?  ? )')} " if self.show_face else ""
        caption = frame.caption
        line = f"{face}{name} [{frame.state.value}]"
        print(f"{line}{'  ' + caption if caption else ''}", file=self.stream, flush=True)

    def close(self) -> None:
        print(f"-- {self.frames} 帧后退出 --", file=self.stream, flush=True)
