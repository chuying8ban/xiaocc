"""控制面板：无头那半（渲染 + 进程间那点事）的回归。

窗口那半（WKWebView）没法在无头 CI 里验 —— 那部分靠实拍截图 + 桌宠侧的处理函数回归
（``scripts/verify_drag_mouse.py`` 判据⑦⑧）。这里守的是两件真出过事的东西：

1. **一次点击只能开出 1 个窗口**。第一版一次点击开出了 3 个：① ``request_open`` 拉起的子进程
   又走了一遍 ``request_open``（子生孙）；② 两个调用方同时看不到 ``panel.json`` 而各开一个。
   现在前者靠 ``CHILD_ENV`` 标记、后者靠 O_EXCL 抢锁。
2. **取不到就写「未取到」，绝不补 0**（陈旧/缺失时也不许把旧数字当真数显示）。
"""

from __future__ import annotations

import json
import time

import pytest

from xiaocc import panel
from xiaocc.panel import paths, render


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把面板的三个文件指到临时目录（绝不碰用户真实的 ~/.xiaocc/）。"""
    monkeypatch.setattr(paths, "PANEL_STATE", tmp_path / "panel.json")
    monkeypatch.setattr(paths, "PANEL_REQUEST", tmp_path / "panel.request")
    monkeypatch.setattr(paths, "SPAWN_LOCK", tmp_path / "panel.spawn")
    return tmp_path


def _spawn_counter(monkeypatch):
    calls: list[list[str]] = []

    class _Fake:
        def __init__(self, argv, **_kwargs):
            calls.append(argv)

    monkeypatch.setattr(paths.subprocess, "Popen", _Fake)
    return calls


def test_请求文件总是被写下(sandbox, monkeypatch):
    _spawn_counter(monkeypatch)
    assert paths.request_open() == "spawned"
    assert json.loads(paths.PANEL_REQUEST.read_text())["at"] > 0


def test_只有一个人能拿到拉起锁(sandbox):
    assert paths.take_spawn_lock() is True
    assert paths.take_spawn_lock() is False, "锁被抢了两次 —— 会开出第二个窗口"
    paths.release_spawn_lock()
    assert paths.take_spawn_lock() is True


def test_面板已经开着就不再拉起(sandbox, monkeypatch):
    calls = _spawn_counter(monkeypatch)
    paths.PANEL_STATE.write_text(json.dumps({"pid": 1, "at": time.time()}) + "\n")
    # pid 1（launchd）在本机一定活着 —— 用它代替「已有一个面板在跑」
    result = paths.request_open()
    assert result in ("raised",), f"已有面板却又拉了进程：{result}"
    if result == "raised" and calls:
        pytest.fail(f"已有活着的面板，却还是拉了 {len(calls)} 个进程")


def test_第二次调用不许开第二个窗口(sandbox, monkeypatch):
    """复现第一版那个 bug：锁没被尊重时一次点击会开出 N 个面板。"""
    calls = _spawn_counter(monkeypatch)
    assert paths.request_open() == "spawned"
    assert paths.request_open() == "raised"
    assert len(calls) == 1, f"拉了 {len(calls)} 个进程（应该只有 1 个）"


def test_陈锁会过期(sandbox, monkeypatch):
    calls = _spawn_counter(monkeypatch)
    assert paths.take_spawn_lock() is True
    stale = time.time() - (paths.SPAWN_LOCK_TTL_S + 5)
    import os

    os.utime(paths.SPAWN_LOCK, (stale, stale))
    assert paths.request_open() == "spawned", "陈锁没过期 —— 面板死了就再也没法被拉起来"
    assert len(calls) == 1


def test_device_block_says_collecting_when_the_baseline_is_too_young() -> None:
    """两种空必须分开写：**「等一秒就有」**（采集中…）不是「未取到」。

    用户点开面板看到「未取到」会以为坏了；而这半秒的等待是我们自己的 CPU 采样窗口造成的。
    """
    from xiaocc.device import Device
    from xiaocc.panel.render import _device_block

    young = _device_block(
        Device(taken_at=0.0, cpu_percent=None, mem_used=1, mem_total=2), wait_remaining=0.6
    )
    assert young["pending"] is True
    assert dict(young["rows"])["CPU"] == "采集中…"

    stale = _device_block(Device(taken_at=0.0, cpu_percent=None), wait_remaining=0.0)
    assert stale["pending"] is False
    assert dict(stale["rows"])["CPU"] == "未取到"

    real = _device_block(Device(taken_at=0.0, cpu_percent=12.0), wait_remaining=0.6)
    assert real["pending"] is False and dict(real["rows"])["CPU"].startswith("12%")


def test_没有数据时不编数字(tmp_path):
    """quota.json 不存在（或坏掉）⇒ 页面必须写「还没有采集数据」，不许出现 ¥0.00。"""
    payload = render.build_payload(
        quota_path=tmp_path / "nope.json", probe_path=tmp_path / "nope-probe.json"
    )
    html = render.render_html(payload)
    assert "还没有采集数据" in html
    assert payload["quota"]["report"] == {}
    assert payload["pet"] == {"alive": False}


def test_陈旧标记会传到页面(tmp_path):
    quota = tmp_path / "quota.json"
    quota.write_text(json.dumps({"schema": 1, "services": []}))
    old = time.time() - 3600 * 6
    import os

    os.utime(quota, (old, old))
    payload = render.build_payload(quota_path=quota, probe_path=tmp_path / "p.json")
    meta = payload["quota"]["meta"]
    assert meta["exists"] is True and meta["stale"] is True, meta
    html = render.render_html(payload)
    assert "下面的数字可能是旧的" in html  # 页面上有明确交代，不是静默显示旧数


def test_探针白名单不许整份进页面(tmp_path):
    """probe.json 是诊断文件：只准白名单字段进网页（别把整份塞进去）。"""
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"pid": 42, "state": "idle", "内部秘密": "别外传"}))
    assert render._pet_snapshot(probe) == {"pid": 42, "state": "idle"}


def test_面板模块可以无头导入():
    """render/paths 不许在 import 时就依赖 AppKit（无头测试/CI 要能跑）。"""
    assert callable(panel.build_payload)
    assert callable(panel.request_open)
