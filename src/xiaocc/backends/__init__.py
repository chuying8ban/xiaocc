"""内置显示层。第三方显示层请声明 entry point ``xiaocc.backends``。"""

from __future__ import annotations

from .base import Backend
from .console import ConsoleBackend

__all__ = ["Backend", "ConsoleBackend"]
