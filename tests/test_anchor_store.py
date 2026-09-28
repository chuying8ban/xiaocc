"""锚点落盘存储的测试：纯 IO、纯标准库，Linux CI 上照样能跑。

守的契约只有一条：**坏了就返回 None / False，绝不抛异常**。
读不到锚点，桌宠大不了回到默认角落；写不进锚点，拖动流程也不该被炸掉。
所以这里每一条「坏情况」都只断言返回值，绝不用 pytest.raises。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xiaocc.backends import anchor_store
from xiaocc.backends.anchor_store import ENV_OVERRIDE, anchor_path, load_anchor, save_anchor


def _dump(path: Path, payload: object) -> Path:
    """按 JSON 写一个「内容可控」的锚点文件，返回路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _dump_raw(path: Path, text: str) -> Path:
    """原样写字节（用来造坏 JSON）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# —— 正常往返 ————————————————————————————————————————————————————————————


@pytest.mark.parametrize(("x", "y"), [(1324.0, 96.0), (120.5, 300.25)])
def test_写进去再读回来坐标一致(tmp_path: Path, x: float, y: float) -> None:
    """小数必须原样保住：坐标差半个像素，运行时就会误判「有东西在偷偷挪窗口」。"""
    target = tmp_path / "anchor.json"
    assert save_anchor(x, y, target) is True
    assert load_anchor(target) == (x, y)


def test_整数坐标也能读回来(tmp_path: Path) -> None:
    """手写/旧版本文件里坐标是 int 也得认，读出来统一是 float。"""
    target = _dump(tmp_path / "anchor.json", {"x": 3, "y": 4})
    loaded = load_anchor(target)
    assert loaded is not None
    assert tuple(float(v) for v in loaded) == (3.0, 4.0)


def test_多余字段不影响读(tmp_path: Path) -> None:
    """文件里带的 saved_at / note 是给人看的，读的时候直接忽略。"""
    target = _dump(tmp_path / "anchor.json", {"x": 1, "y": 2, "saved_at": 1, "note": "x"})
    assert load_anchor(target) == (1.0, 2.0)


# —— 坏输入一律 None，不抛异常 ——————————————————————————————————————————


def test_文件不存在时返回None(tmp_path: Path) -> None:
    """首次启动就没有锚点文件，这是常态而不是错误。"""
    assert load_anchor(tmp_path / "还没写呢.json") is None


def test_坏JSON返回None(tmp_path: Path) -> None:
    """写到一半被杀留下的半个文件，读不出来就算了，别炸。"""
    target = _dump_raw(tmp_path / "anchor.json", "{坏JSON")
    assert load_anchor(target) is None


@pytest.mark.parametrize("payload", [{"x": 1}, {"y": 1}, {}])
def test_缺字段返回None(tmp_path: Path, payload: dict) -> None:
    """x / y 少一个都算坏文件：宁可回默认角，也不要用半个坐标去摆窗口。"""
    target = _dump(tmp_path / "anchor.json", payload)
    assert load_anchor(target) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"x": "1", "y": 2},
        {"x": True, "y": 2},
        {"x": 1, "y": "2"},
        {"x": 1, "y": None},
    ],
)
def test_类型不对返回None(tmp_path: Path, payload: dict) -> None:
    """字符串坐标、bool（int 的子类，但不是坐标）、null 一律不认。"""
    target = _dump(tmp_path / "anchor.json", payload)
    assert load_anchor(target) is None


# —— 写盘的副作用要干净 ——————————————————————————————————————————————————


def test_save会建出上层目录(tmp_path: Path) -> None:
    """~/.xiaocc/ 可能压根不存在，第一次保存就得自己把目录建出来。"""
    target = tmp_path / "深" / "一层" / "anchor.json"
    assert not target.parent.exists()
    assert save_anchor(12.0, 34.0, target) is True
    assert target.is_file()
    assert load_anchor(target) == (12.0, 34.0)


def test_save结束后同目录不留临时文件(tmp_path: Path) -> None:
    """原子写靠临时文件 + os.replace，成功之后同目录只该剩目标文件一个。"""
    target = tmp_path / "anchor.json"
    assert save_anchor(1324.0, 96.0, target) is True
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_写失败返回False不抛异常(tmp_path: Path) -> None:
    """目标位置已经是个目录（非空），replace 必然失败 —— 只能安静返回 False。"""
    target = tmp_path / "anchor.json"
    target.mkdir()
    (target / "占位.txt").write_text("别覆盖我", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())

    assert save_anchor(1324.0, 96.0, target) is False

    assert target.is_dir(), "失败就是失败，不该把目录覆盖成文件"
    assert (target / "占位.txt").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before, "失败后不许留下临时文件"


# —— 环境变量覆盖 ————————————————————————————————————————————————————————


def test_环境变量能覆盖默认路径(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """XIAOCC_ANCHOR_FILE 指哪儿就读哪儿，绝不顺手去碰 ~/.xiaocc/。"""
    wanted = tmp_path / "env" / "anchor.json"
    home_decoy = tmp_path / "假的家目录" / "anchor.json"
    _dump(home_decoy, {"x": 999.0, "y": 888.0})
    # 把「默认家目录路径」换成看得见摸得着的诱饵：真被读了就会读出 999/888。
    monkeypatch.setattr(anchor_store, "DEFAULT_ANCHOR_PATH", home_decoy)
    monkeypatch.setenv(ENV_OVERRIDE, str(wanted))

    assert anchor_path() == wanted
    assert not wanted.exists()
    assert load_anchor() is None, "环境变量指向的文件不存在时，不许回退去读默认路径"
    assert load_anchor(home_decoy) == (999.0, 888.0), "诱饵文件不该被动过"
