"""状态源基类。

写一个新状态源 = 继承 :class:`StatusSource`，实现 ``poll()``：
每一轮返回**当下**的状态（重复同一条没问题，引擎负责去重），
或者返回 None 表示「这一轮没话说」。
"""

from __future__ import annotations

import abc

from ..protocol import StatusEvent

__all__ = ["StatusSource"]


class StatusSource(abc.ABC):
    """所有状态源的共同接口。

    子类必须能安全地被反复 ``poll()``：引擎默认每秒轮一次，
    抛异常会被引擎隔离并记日志（``xiaocc run -v`` 可见），不会带崩桌宠。
    """

    #: 短名，出现在日志和 ``xiaocc sources`` 列表里
    name: str = "unnamed"
    #: 建议轮询间隔（秒）。引擎取所有源里最小的那个作为节拍。
    interval: float = 1.0
    #: 人类可读的说明，``xiaocc sources`` 会打印
    description: str = ""

    @abc.abstractmethod
    def poll(self) -> StatusEvent | None:
        """返回**当下**的状态。

        重复返回同一个状态是完全正常的（比如 file 源的文件没被改过），引擎会去重。
        返回 None 表示这一轮无话可说——注意它和「报 offline」不是一回事：
        None 什么都不改变，offline 会真的把桌宠切成离线脸。
        """

    def close(self) -> None:
        """释放资源。基类默认什么都不做。"""
