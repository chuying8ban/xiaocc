"""显示层接口。

显示层只做三件事：把 :class:`~xiaocc.engine.Render` 画出来、动起来、随状态换颜色。
它**不**读状态源、不判断业务，这样换平台（AppKit / Web / Windows 原生）不用重写逻辑。

给 GUI 显示层的三个钩子（终端显示层用不上，保持默认即可）：

``interval``
    引擎节拍（秒）。CLI 在构造后写入。GUI 显示层可以据此决定「一轮之间阻塞多久去跑
    自己的事件循环」——动画才不会和引擎轮询抢时间片。
``self_paced``
    置 True 表示**这个显示层自己吃掉节拍**（在自己的事件循环里等），CLI 就不再额外
    ``sleep``。不置的话，GUI 会在「渲染 0.2 秒 + 睡 0.25 秒」之间来回，动画一顿一顿。
``idle``
    「这一轮没有新帧」时 CLI 调的钩子，默认睡一个 ``interval``。``self_paced`` 的
    显示层必须覆写成「继续跑事件循环 interval 秒」。

``self_paced`` 的语义（写错会烧满一个核）
    这个标志决定**谁负责把时间走掉**：

    * ``self_paced = True``：显示层自己拥有事件循环、自己消化节拍（``interval``），
      ``cli.py`` 因此**不再**在它后面补 ``time.sleep()``。
    * 既然 ``cli.py`` 不睡，显示层就**必须**在 :meth:`Backend.idle`（这一轮没有新帧）
      和 :meth:`Backend.render`（有新帧）里都把这段时间走掉。少一边，主循环就成了
      没有节拍的空转 —— 实测 appkit 显示层 5 秒空转 **732768 圈**（其中 732767 圈
      ``engine.tick()`` 返回 ``None``）、CPU **99.8%**，一个核吃满；同样的循环下
      console 显示层（``self_paced = False``）是 0%，差别就在这个标志。
    * ``self_paced = False``（默认，例如 console）：节拍由 ``cli.py`` 的
      ``time.sleep(interval)`` 负责，``idle()`` 不会被调用，保持默认实现即可。
    * 为什么要有 ``idle()`` 而不是让显示层自己在 ``render()`` 里睡：``engine.tick()``
      返回 ``None`` 时**没有帧**可画，但 GUI 层仍然要继续跑动画和事件处理 ——
      这段时间总得有个地方待。
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

    def idle(self) -> None:
        """本轮没有新帧：把到下一个节拍的时间走掉。

        默认实现就是 ``time.sleep(self.interval)``；``self_paced = True`` 的显示层
        （GUI）应该覆写它，用这段时间继续跑自己的事件循环与动画 —— 关键是**不能立刻
        返回**，否则 ``cli.py`` 的主循环会在「没有新帧」的那一轮里空转烧满一个核。
        """
        time.sleep(max(0.0, self.interval))

    def linger(self, seconds: float) -> None:
        """收尾前把最后一帧多留一会儿（截图 / 肉眼验收用）。默认就是睡一觉。

        GUI 显示层应当覆写成「继续跑事件循环 N 秒」——只是 ``sleep`` 的话，
        窗口会被系统标成无响应，截出来的图也可能是半张。
        """
        if seconds > 0:
            time.sleep(seconds)

    def close(self) -> None:
        """收尾（关窗、落日志）。"""
