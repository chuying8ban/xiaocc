"""引擎：把若干状态源合成一条给显示层的指令。

职责边界写清楚，免得以后长成一团：
  * 定时轮询所有源，**隔离**单个源的异常（坏了只记日志，不拖垮桌宠）
  * 用 ``protocol.pick`` 合并成「此刻该演什么」
  * 状态没变就不打扰显示层（省电，也省得动画一直重播）
  * 打印/上报真实原因，便于用户在外地凭日志诊断
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from .characters import Character
from .protocol import State, StatusEvent, pick
from .sources.base import StatusSource

__all__ = ["Engine", "Render"]

log = logging.getLogger("xiaocc.engine")


def _identity(event: StatusEvent) -> tuple:
    """一帧的「内容指纹」——故意排除时间戳。"""
    return (
        event.source,
        event.state,
        event.detail,
        event.project,
        event.step,
        event.total,
    )



@dataclass(frozen=True)
class Render:
    """显示层拿到的一帧：演什么状态、用哪个角色、旁边写什么字。"""

    event: StatusEvent
    character: Character

    @property
    def state(self) -> State:
        return self.event.state

    @property
    def caption(self) -> str:
        """状态卡主文案：优先真实进度，没有就退回阶段名。"""
        bits = [b for b in (self.event.detail,) if b]
        if self.event.project:
            bits.insert(0, self.event.project)
        if (progress := self.event.progress_text) is not None:
            bits.append(progress)
        return " · ".join(bits)


class Engine:
    def __init__(
        self,
        sources: Sequence[StatusSource],
        character: Character,
        *,
        min_interval: float = 0.25,
        clock=time.time,
    ) -> None:
        if not sources:
            raise ValueError("至少需要一个状态源")
        self.sources = list(sources)
        self.character = character
        self.clock = clock
        self._frame: Render | None = None
        self.interval = max(min_interval, min(s.interval for s in self.sources))
        self._errors: dict[str, str] = {}

    @property
    def frame(self) -> Render | None:
        """当前帧（还没跑过就是 None）。"""
        return self._frame

    def tick(self) -> Render | None:
        """轮询一轮。返回「需要重画」的帧，无变化返回 None。

        重复判断只看**内容**（源/状态/文案/进度），不看时间戳：
        否则像 file 这种「一直同一个状态」的源会每轮都触发重画。
        """
        now = self.clock()
        events: list[StatusEvent] = []
        for source in self.sources:
            key = getattr(source, "name", repr(source))
            try:
                event = source.poll()
            # 隔离**故意**宽：任何源抛任何异常都不许带崩桌宠；异常只进 health()
            # + 日志（按消息去重），不进画面。
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                if self._errors.get(key) != message:
                    self._errors[key] = message
                    log.warning("状态源 %s 出错：%s", key, message)
                continue
            self._errors.pop(key, None)
            if event is not None:
                events.append(event)

        chosen = pick(events, now)
        if chosen is None:
            detail = "没有任何状态源在线"
            if self._errors:
                # 全都没消息时，把故障原因说出来，而不是笼统地报「离线」
                detail = "状态源故障：" + "；".join(f"{k} {v}" for k, v in self._errors.items())
            chosen = StatusEvent(source="engine", state=State.OFFLINE, at=now, detail=detail)

        if self._frame is not None and _identity(self._frame.event) == _identity(chosen):
            # 内容没变：静默更新一下时间戳，但不打扰显示层
            self._frame = Render(event=chosen, character=self.character)
            return None
        self._frame = Render(event=chosen, character=self.character)
        return self._frame

    def health(self) -> dict[str, str]:
        """当前有故障的状态源（名字 → 原因）。``xiaocc run -v`` 会打日志。"""
        return dict(self._errors)


    def close(self) -> None:
        for source in self.sources:
            try:
                source.close()
            except Exception:  # 关闭阶段的异常不值得打断退出
                log.debug("关闭状态源失败", exc_info=True)
