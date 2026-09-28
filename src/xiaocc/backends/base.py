"""显示层接口。

显示层只做三件事：把 :class:`~xiaocc.engine.Render` 画出来、动起来、随状态换颜色。
它**不**读状态源、不判断业务，这样换平台（AppKit / Web / Windows 原生）不用重写逻辑。
"""

from __future__ import annotations

import abc

from ..engine import Render

__all__ = ["Backend"]


class Backend(abc.ABC):
    #: 短名，对应 ``xiaocc run --backend <name>``
    name: str = "unnamed"

    @abc.abstractmethod
    def render(self, frame: Render) -> None:
        """呈现一帧。引擎只在状态变化时调用。"""

    def close(self) -> None:
        """收尾（关窗、落日志）。"""
