"""内置状态源。第三方状态源请在自己的包里实现并声明 entry point ``xiaocc.sources``。"""

from __future__ import annotations

from .base import StatusSource
from .command import CommandSource
from .file import FileSource
from .hermes import HermesSource

__all__ = ["StatusSource", "CommandSource", "FileSource", "HermesSource"]
