"""扩展点发现：状态源 / 显示层 / 角色包。

两级查找，顺序固定：
  1. 已安装包的 entry point（``importlib.metadata``）—— 第三方扩展靠这个，**不用改核心代码**
  2. 内置表（BUILTIN_*）—— 源码直跑、没装包时也能用

一个扩展加载失败只影响它自己，``warnings`` 里会写明原因，不静默吞掉。
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import warnings
from collections.abc import Callable
from typing import Any

from .sources.base import StatusSource
from .sources.command import CommandSource
from .sources.file import FileSource
from .sources.hermes import HermesSource

__all__ = [
    "BACKENDS",
    "SOURCES",
    "available_backends",
    "available_sources",
    "load_backend",
    "load_source",
    "parse_source_spec",
]

_BUILTIN_SOURCES: dict[str, Callable[..., StatusSource]] = {
    "file": FileSource,
    "command": CommandSource,
    "hermes": HermesSource,
}

#: 显示层延迟导入：console 零依赖，AppKit 需要 pyobjc
_BUILTIN_BACKENDS: dict[str, str] = {
    "console": "xiaocc.backends.console:ConsoleBackend",
    "terminal": "xiaocc.backends.console:ConsoleBackend",  # 别名，喊得顺口
    "appkit": "xiaocc.backends.appkit:AppKitBackend",
}

#: 给 ``xiaocc backends`` 用的人话说明（内置显示层不容易从 docstring 里抠出一句）
_BACKEND_NOTES: dict[str, str] = {
    "console": "终端输出，用于调试、SSH 和 CI",
    "terminal": "console 的别名（无头环境里喊 terminal 更顺口）",
    "appkit": "macOS 原生窗口：透明无边框 + 置顶 + 贴边隐藏 + 点击穿透",
}

SOURCES = "xiaocc.sources"
BACKENDS = "xiaocc.backends"
CHARACTERS = "xiaocc.characters"


def _entry_points(group: str) -> dict[str, md.EntryPoint]:
    try:
        return {ep.name: ep for ep in md.entry_points(group=group)}
    # 元数据是外部文件（别的包写的），坏法穷举不完；失败就退回内置表并 warn。
    except Exception as exc:  # noqa: BLE001
        warnings.warn(f"读取 entry point {group} 失败：{exc}", stacklevel=2)
        return {}


def available_sources() -> dict[str, str]:
    """名字 → 说明，给 ``xiaocc sources`` 用。"""
    out: dict[str, str] = {}
    for name, factory in _BUILTIN_SOURCES.items():
        desc = getattr(factory, "description", "")
        if not desc:
            doc = (factory.__doc__ or "").strip().splitlines()
            desc = doc[0] if doc else ""
        out[name] = desc
    for name, ep in _entry_points(SOURCES).items():
        if name not in out:
            out[name] = f"第三方状态源（{ep.value}）"
    return out


def available_backends() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, target in _BUILTIN_BACKENDS.items():
        out[name] = _backend_note(name, target)
    for name, ep in _entry_points(BACKENDS).items():
        if name not in out:
            out[name] = f"第三方显示层（{ep.value}）"
    return out


def _backend_note(name: str, target: str) -> str:
    """内置显示层的人话说明；依赖没装的也照列，但把原因写在后面。"""
    note = _BACKEND_NOTES.get(name)
    if note is None:
        return f"内置显示层（{target}）"
    if name == "appkit":
        try:
            importlib.import_module("objc")
        except ImportError:
            return note + "  ← 需要 pip install 'xiaocc[macos]'"
    return note


def load_source(name: str, *args: Any, **kwargs: Any) -> StatusSource:
    """按名字造一个状态源。第三方源的构造参数原样透传（``名字:参数`` → 第一个位置参数）。"""
    factory = _BUILTIN_SOURCES.get(name)
    if factory is None:
        ep = _entry_points(SOURCES).get(name)
        if ep is None:
            known = ", ".join(sorted(available_sources()))
            raise KeyError(f"没有名为 {name!r} 的状态源。可用：{known}")
        factory = ep.load()
    return factory(*args, **kwargs)


def load_backend(name: str, **kwargs: Any) -> Any:
    target = _BUILTIN_BACKENDS.get(name)
    if target is None:
        ep = _entry_points(BACKENDS).get(name)
        if ep is None:
            known = ", ".join(sorted(available_backends()))
            raise KeyError(f"没有名为 {name!r} 的显示层。可用：{known}")
        obj = ep.load()
    else:
        module_name, _, attr = target.partition(":")
        module = __import__(module_name, fromlist=[attr])
        obj = getattr(module, attr)
    return obj(**kwargs) if kwargs else obj


def parse_source_spec(spec: str) -> StatusSource:
    """把命令行上的 ``hermes`` / ``file:/tmp/a.json`` / ``command:foo --json`` 解析成状态源。"""
    name, sep, arg = spec.partition(":")
    if not sep:
        # 不带参数的源允许省略冒号（hermes 会自动挑最新的 profile）
        if name == "hermes":
            return HermesSource()
        try:
            # 第三方源同样允许省略冒号：走它自己的默认参数
            return load_source(name)
        except KeyError as exc:
            raise ValueError(
                f"状态源要写成 '名字:参数'，收到 {spec!r}（例如 file:~/.xiaocc/status.json）"
            ) from exc
    if name == "file":
        return FileSource(arg)
    if name == "command":
        return CommandSource(arg)
    if name == "hermes":
        return HermesSource(profile=arg or None)
    return load_source(name, *([arg] if arg else []))
