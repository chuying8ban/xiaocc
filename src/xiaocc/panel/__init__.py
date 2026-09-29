"""控制面板：把额度与本机账本渲染成一页，用 WKWebView 打开。

用法（桌宠那侧的入口也走同一个命令）：

* ``xiaocc panel``           —— 打开面板（窗口）
* ``xiaocc panel --request`` —— 「把面板打开/抬到前面」：已经有面板就请求它刷新，
                                没有就顺手拉一个（桌宠点击走这条）
* ``xiaocc panel --dump F``  —— 只渲染到文件，不起窗口（无头、可截图、可测试）

分层：:mod:`.render` 是纯函数（读文件 + 拼 HTML，无头可测）；:mod:`.window` 才碰 AppKit，
只在真要开窗口时导入 —— 所以 ``import xiaocc.panel`` 在没有窗口服务器的地方也能用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .render import (
    PAYLOAD_PLACEHOLDER,
    PET_FIELDS,
    build_payload,
    render_html,
    write_panel,
)

#: 需要窗口服务器才 import 得到的东西（AppKit/WebKit）在 :mod:`.window` 里，惰性取
__all__ = [
    "PAYLOAD_PLACEHOLDER",
    "PET_FIELDS",
    "build_payload",
    "open_panel",
    "render_html",
    "request_open",
    "running_panel",
    "write_panel",
]


def open_panel(**kwargs: Any) -> int:
    """起窗口并跑事件循环（窗口关掉才返回）。需要 AppKit/WebKit，故在此惰性导入。"""
    from .window import open_panel as _open

    return _open(**kwargs)


def request_open(**kwargs: Any) -> str:
    """「打开/抬到前面」：桌宠的入口只调它 —— 不 import AppKit、不阻塞。"""
    from .paths import request_open as _request

    return _request(**kwargs)


def running_panel() -> int | None:
    """活着的面板进程 pid（没有就是 ``None``）。"""
    from .paths import running_panel as _running

    return _running()


def dump(out: Path, **kwargs: Any) -> Path:
    """只渲染到文件（不起窗口）。"""
    return write_panel(Path(out), **kwargs)
