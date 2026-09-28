"""小cc 的状态协议。

这是整个项目唯一的硬契约：**状态源(source) → 引擎(engine) → 角色(character) → 显示层(backend)**
四层之间只交换 :class:`StatusEvent`。任何新功能都不应该绕过它。

为什么要有协议：
  大肥鱼那类桌宠把自己焊死在某个宿主（DSH 插件）上，换个宿主就废了。
  小cc 反过来——宿主只是「状态源」的一种，谁都能写：
  守护进程、Hermes、DSH、CI、下载任务、番茄钟、手机上的一条推送。

协议规则：
  * ``state`` 只有 7 个取值，角色包必须为每个取值给出表现，不许静默兜底成「未知」。
  * 每个状态有 TTL（见 :data:`STATE_TTL`），过期即视为不新鲜；这是防止「任务已经死了，
    桌宠还在转圈」的唯一手段——必须有，且必须由协议层而不是角色层来管。
  * 进度只允许来自真实数字，没有 ``step``/``total`` 就显示阶段名，不许编百分比。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

__all__ = [
    "State",
    "STATE_PRIORITY",
    "STATE_TTL",
    "StatusEvent",
    "pick",
    "PROTOCOL_VERSION",
]

PROTOCOL_VERSION = 1


class State(str, Enum):
    """桌宠能表现的全部状态。新增状态属于破坏性改动，必须升 PROTOCOL_VERSION。"""

    OFFLINE = "offline"   # 状态源消失 / 宿主没启动
    IDLE = "idle"         # 活着，没事干
    THINKING = "thinking"  # 模型在推理、在等结果
    WORKING = "working"    # 在执行具体动作（改文件、跑命令、下载）
    WAITING = "waiting"    # 卡在需要人确认的地方
    DONE = "done"          # 一轮任务刚刚结束
    ERROR = "error"        # 出错了，需要人看

    def __str__(self) -> str:  # 让 f"{state}" 干净地输出状态名
        return self.value


#: 多个状态源同时报告时，谁说了算。数字越大越优先。
#: error > waiting > working > thinking > done > idle：谁更「需要人看见」谁上。
STATE_PRIORITY: Mapping[State, int] = {
    State.OFFLINE: 0,
    State.DONE: 1,
    State.IDLE: 2,
    State.THINKING: 3,
    State.WORKING: 4,
    State.WAITING: 5,
    State.ERROR: 6,
}

#: 事件保鲜期（秒）。None = 不过期（idle/offline 要一直有效，否则桌宠会自己消失）。
STATE_TTL: Mapping[State, float | None] = {
    State.THINKING: 90.0,
    State.WORKING: 45.0,
    State.WAITING: 600.0,
    State.DONE: 12.0,
    State.ERROR: 90.0,
    State.IDLE: None,
    State.OFFLINE: None,
}


@dataclass(frozen=True)
class StatusEvent:
    """一条状态报告。不可变——引擎不做就地修改，避免多源共享时的串味。"""

    source: str
    state: State
    at: float = field(default_factory=time.time)
    detail: str = ""
    project: str = ""
    step: int | None = None
    total: int | None = None
    #: 宿主给的原始载荷，调试用；不参与渲染决策。
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # 允许 source 写成 "file:/tmp/x.json" 这类描述串，统一裁成短名
        if not self.source:
            raise ValueError("StatusEvent.source 不能为空")
        if isinstance(self.state, str):  # 从 JSON 直接构造时的兜底
            object.__setattr__(self, "state", State(self.state))
        if self.step is not None and self.step < 0:
            raise ValueError("step 不能为负")
        if self.total is not None and self.total < 0:
            raise ValueError("total 不能为负")

    # —— 时间 ——
    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.at)

    def is_stale(self, now: float | None = None) -> bool:
        ttl = STATE_TTL.get(self.state)
        return ttl is not None and self.age(now) > ttl

    # —— 展示 ——
    @property
    def progress_text(self) -> str | None:
        """只有拿到真实数字才给进度，否则 None —— 让上层改用阶段名。"""
        if self.step is None or self.total is None or self.total <= 0:
            return None
        return f"{self.step}/{self.total}"

    def summary(self) -> str:
        head = f"[{self.state}]"
        bits = [b for b in (self.project, self.detail, self.progress_text) if b]
        return " ".join([head, *bits]) if bits else head

    # —— 序列化：这是第三方状态源（其它语言也行）唯一需要实现的格式 ——
    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "source": self.source,
            "state": self.state.value,
            "at": self.at,
        }
        for key in ("detail", "project", "step", "total"):
            value = getattr(self, key)
            if value not in (None, ""):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "StatusEvent":
        """解析状态源输出。字段缺失用默认值，非法状态名直接报错（不猜）。"""
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("状态必须是 JSON 对象")
        try:
            state = State(data["state"])
        except KeyError as exc:
            raise ValueError("缺少 state 字段") from exc
        except ValueError as exc:
            valid = ", ".join(s.value for s in State)
            raise ValueError(f"未知状态 {data.get('state')!r}，合法取值：{valid}") from exc
        return cls(
            source=str(data.get("source") or "unknown"),
            state=state,
            at=float(data.get("at") or time.time()),
            detail=str(data.get("detail") or ""),
            project=str(data.get("project") or ""),
            step=data.get("step"),
            total=data.get("total"),
            raw=data,
        )


def pick(events: Iterable[StatusEvent], now: float | None = None) -> StatusEvent | None:
    """多源合并：丢掉过期的，取优先级最高的那条。

    返回 None 表示「所有源都没有话说」——引擎会据此显示 offline，
    而不是自作聪明地保留上一条，那样会撒谎。
    """
    now = time.time() if now is None else now
    fresh = [e for e in events if not e.is_stale(now)]
    if not fresh:
        return None
    return max(fresh, key=lambda e: (STATE_PRIORITY[e.state], e.at))
