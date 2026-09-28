"""小cc —— 住在桌面上的状态伴侣。

分层（每层之间只认 :mod:`xiaocc.protocol`）::

    状态源 sources/  →  引擎 engine.py  →  角色 characters.py  →  显示层 backends/

一句话：**谁都能驱动它，谁都能换掉它的皮，谁都能换掉它的窗口。**
"""

from __future__ import annotations

from .characters import Character, CharacterError, load_character
from .engine import Engine, Render
from .protocol import PROTOCOL_VERSION, State, StatusEvent, pick

__all__ = [
    "PROTOCOL_VERSION",
    "Character",
    "CharacterError",
    "Engine",
    "Render",
    "State",
    "StatusEvent",
    "__version__",
    "load_character",
    "pick",
]

__version__ = "0.1.0"
