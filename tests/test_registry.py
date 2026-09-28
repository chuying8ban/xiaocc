"""扩展点发现：第三方状态源怎么从命令行的名字变成实例。

这一层是「可扩展」的兑现处——它坏了，第三方包写得再对也启动不了。
"""

from __future__ import annotations

import pytest

from xiaocc import registry
from xiaocc.protocol import State, StatusEvent
from xiaocc.sources.base import StatusSource
from xiaocc.sources.command import CommandSource
from xiaocc.sources.file import FileSource
from xiaocc.sources.hermes import HermesSource


class _DemoSource(StatusSource):
    """假装是第三方包里的状态源：构造参数用于验证「名字:参数」透传。"""

    name = "demo"
    description = "测试用"
    interval = 1.0

    def __init__(self, minutes: float = 25.0) -> None:
        self.minutes = float(minutes)

    def poll(self):
        return StatusEvent(source=self.name, state=State.WORKING, detail=f"{self.minutes:g} 分钟")


class _FakeEntryPoint:
    """像 importlib.metadata.EntryPoint 那样够用：registry 只用 .value 和 .load()。"""

    def __init__(self, value: str, factory) -> None:
        self.value = value
        self._factory = factory

    def load(self):
        return self._factory


@pytest.fixture
def demo_installed(monkeypatch):
    """把 demo 状态源伪装成「已安装的第三方包」。"""
    fake = _FakeEntryPoint("demo_pkg:DemoSource", _DemoSource)

    def _entry_points(group: str):
        return {_DemoSource.name: fake} if group == registry.SOURCES else {}

    monkeypatch.setattr(registry, "_entry_points", _entry_points)
    return _DemoSource


def test_builtin_specs_unchanged(tmp_path):
    assert isinstance(registry.parse_source_spec("hermes"), HermesSource)
    assert isinstance(registry.parse_source_spec("hermes:writer"), HermesSource)
    file_source = registry.parse_source_spec(f"file:{tmp_path / 's.json'}")
    assert isinstance(file_source, FileSource)
    assert isinstance(registry.parse_source_spec("command:echo hi"), CommandSource)


def test_third_party_source_appears_in_list(demo_installed):
    assert "demo" in registry.available_sources()


def test_third_party_source_without_argument(demo_installed):
    """--source demo —— 省略冒号走默认参数，和内置 hermes 一样。"""
    source = registry.parse_source_spec("demo")
    assert isinstance(source, _DemoSource)
    assert source.minutes == 25.0


def test_third_party_source_argument_is_passed(demo_installed):
    """--source demo:7 —— 冒号后面那一段原样交给构造函数（位置参数）。"""
    source = registry.parse_source_spec("demo:7")
    assert isinstance(source, _DemoSource)
    assert source.minutes == 7.0


def test_third_party_source_with_empty_argument_uses_default(demo_installed):
    source = registry.parse_source_spec("demo:")
    assert isinstance(source, _DemoSource)
    assert source.minutes == 25.0


def test_unknown_source_keeps_the_helpful_message(demo_installed):
    with pytest.raises(ValueError, match="名字:参数"):
        registry.parse_source_spec("没有这个源")


def test_load_source_lists_what_exists(demo_installed):
    with pytest.raises(KeyError) as exc:
        registry.load_source("没有这个源")
    assert "hermes" in str(exc.value) and "demo" in str(exc.value)
