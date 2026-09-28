"""显示层接口。

显示层只做三件事：把 :class:`~xiaocc.engine.Render` 画出来、动起来、随状态换颜色。
它**不**读状态源、不判断业务，这样换平台（AppKit / Web / Windows 原生）不用重写逻辑。

给 GUI 显示层的两个钩子（终端显示层用不上，保持默认即可）：

``interval``
    引擎节拍（秒）。CLI 在构造后写入。GUI 显示层可以据此决定「一轮之间阻塞多久去跑
    自己的事件循环」——动画才不会和引擎轮询抢时间片。
``self_paced``
    置 True 表示**这个显示层自己吃掉节拍**（在自己的事件循环里等），CLI 就不再额外
    ``sleep``。不置的话，GUI 会在「渲染 0.2 秒 + 睡 0.25 秒」之间来回，动画一顿一顿。
"""

from __future__ import annotations

import abc
import time

from ..engine import Render

__all__ = ["Backend"]


class Backend(abc.ABC):
    #: 短名，对应 ``xiaocc run --backend <name>``
    name: str = "unnamed"

    #: 引擎节拍（秒），由 CLI 写入 —— 见模块文档。
    interval: float = 0.25

    #: 显示层是否自己消化节拍（GUI 显示层置 True）—— 见模块文档。
    self_paced: bool = False

    @abc.abstractmethod
    def render(self, frame: Render) -> None:
        """呈现一帧。引擎只在状态变化时调用。"""

    def linger(self, seconds: float) -> None:
        """收尾前把最后一帧多留一会儿（截图 / 肉眼验收用）。默认就是睡一觉。

        GUI 显示层应当覆写成「继续跑事件循环 N 秒」——只是 ``sleep`` 的话，
        窗口会被系统标成无响应，截出来的图也可能是半张。
        """
        if seconds > 0:
            time.sleep(seconds)

    def close(self) -> None:
        """收尾（关窗、落日志）。"""
