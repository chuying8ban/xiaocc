"""控制面板：无头那半（渲染 + 进程间那点事）的回归。

窗口那半（WKWebView）没法在无头 CI 里验 —— 那部分靠实拍截图 + 桌宠侧的处理函数回归
（``scripts/verify_drag_mouse.py`` 判据⑦⑧）。这里守的是两件真出过事的东西：

1. **一次点击只能开出 1 个窗口**。第一版一次点击开出了 3 个：① ``request_open`` 拉起的子进程
   又走了一遍 ``request_open``（子生孙）；② 两个调用方同时看不到 ``panel.json`` 而各开一个。
   现在前者靠 ``CHILD_ENV`` 标记、后者靠 O_EXCL 抢锁。
2. **取不到就写「未取到」，绝不补 0**（陈旧/缺失时也不许把旧数字当真数显示）。
"""

from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path

import pytest

from xiaocc import panel
from xiaocc.panel import paths, render

SRC = Path(__file__).resolve().parent.parent / "src"


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


def test_rendered_page_has_no_markdown_in_what_the_user_reads(tmp_path) -> None:
    """渲染后的**正文**里不许有 Markdown（``**`` / 反引号）—— 静态 HTML 不跑 Markdown，会原样上屏。

    实拍：`两个按钮都要**点两下**` 就这么送到了用户眼前（2026-09-29 抓的）。
    必须**先剥掉 ``<style>``/``<script>`` 再找**（实测教训：不剥就是永久红 ——
    CSS/JS 注释里本来就有 ``**`` 和反引号），否则这条判据守不住东西还天天亮红灯。
    """
    payload = render.build_payload(
        quota_path=tmp_path / "nope.json", probe_path=tmp_path / "nope-probe.json"
    )
    page = render.render_html(payload)
    body = re.sub(r"<(style|script)\b.*?</\1>", "", page, flags=re.DOTALL | re.IGNORECASE)
    assert "**" not in body, "正文里有 Markdown 星号：用户看到的就是 `**` 本身"
    assert "`" not in body, "正文里有反引号：命令要写成 <code>…</code>"


#: 会**原样上屏**的关键字参数（面板是静态 HTML，不跑 Markdown；终端里 Markdown 也没好处）
_UI_KWARGS = {"hint", "detail", "note", "summary", "title"}
#: 同类意思的模块级常量（`LOGIN_HINT` 这种会拼进上面那些话里）
_UI_CONST_HINT = ("HINT", "DETAIL", "NOTE", "SUMMARY", "LABEL")


def _literal_texts(node):
    """取出表达式里的**字面量片段**（f-string 只取固定部分；变量值不在此列）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                yield value.value
    elif isinstance(node, ast.BinOp):  # 字符串拼接
        yield from _literal_texts(node.left)
        yield from _literal_texts(node.right)


def _markdown_hits(text: str) -> str:
    bad = [m for m in ("**", "`") if m in text]
    return "/".join(bad)


def test_user_visible_strings_in_code_carry_no_markdown() -> None:
    """代码里那些**会原样上屏**的字符串不许带 Markdown —— 用户看到的就是星号/反引号本身。

    实拍两处：面板注里的 `**点两下**`，和千问那条「未安装官方 CLI `qianwen`」（静态 HTML 不跑
    Markdown ⇒ 反引号原样送到眼前）。用 ``ast`` 只认「这些关键字参数里的字面量 + 同类模块常量」，
    所以 docstring 里的 ``反引号`` 不会误伤（实测教训：不剥注释/文档的扫描会永久红）。
    """
    offenders: list[str] = []
    for path in sorted((SRC / "xiaocc").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in _UI_KWARGS:
                        for text in _literal_texts(kw.value):
                            if (bad := _markdown_hits(text)):
                                offenders.append(f"{path.name}:{node.lineno} {kw.arg}=… 带 {bad}")
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if not any(any(k in n.upper() for k in _UI_CONST_HINT) for n in names):
                    continue
                for text in _literal_texts(node.value):
                    if (bad := _markdown_hits(text)):
                        offenders.append(f"{path.name}:{node.lineno} {names[0]}=… 带 {bad}")
    assert not offenders, "会原样上屏的字符串里有 Markdown：\n" + "\n".join(offenders)


def test_short_home_abbreviates_paths_in_page_data() -> None:
    """页面数据里的 `$HOME` 一律缩成 `~`（页脚与账本「库」行显示的是绝对路径）。

    这条是重拍实拍时抓出来的：那两行把 `/Users/<用户名>/…` 直接印在页面上，截图一进公开仓库
    就是真用户名。`~` 既短又不泄露，且只动显示用的页面数据（诊断日志仍要真路径）。
    """
    home = str(Path.home())
    assert render._short_home(f"{home}/.hermes/state.db") == "~/.hermes/state.db"
    # 递归：嵌套 dict / list 里的路径也要缩写；不是 home 开头的（如替身目录）原样保留
    payload = {
        "meta": {"path": f"{home}/.xiaocc/quota.json", "n": 3},
        "ledger": {"default_profile": {"path": f"{home}/.hermes/state.db"}, "dbs": [f"{home}/a", "/tmp/b"]},
    }
    got = render._short_home(payload)
    assert got["meta"] == {"path": "~/.xiaocc/quota.json", "n": 3}
    assert got["ledger"]["default_profile"]["path"] == "~/.hermes/state.db"
    assert got["ledger"]["dbs"] == ["~/a", "/tmp/b"]


def test_device_block_says_collecting_when_the_baseline_is_too_young() -> None:
    """两种空必须分开写：**「等一秒就有」**（采集中）不是「未取到」。

    用户点开面板看到「未取到」会以为坏了；而这半秒的等待是我们自己的 CPU 采样窗口造成的。
    """
    from xiaocc.device import Device
    from xiaocc.panel.render import _device_block

    young = _device_block(
        Device(taken_at=0.0, cpu_percent=None, mem_used=1, mem_total=2), wait_remaining=0.6
    )
    assert young["pending"] is True
    # 措辞**不带省略号**（唯一的紧候选 `CPU 采集中 · 内存 未取到` 128.6px /
    # 预算 132，加省略号 137.5px 直接超预算，而省略号一个字节的信息都没多给）
    assert dict(young["rows"])["CPU"] == "采集中"

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
