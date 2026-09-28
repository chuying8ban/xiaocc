"""角色包校验测试：注入式扩展的第一道闸门——坏角色包必须报错，不许静默降级。"""

from __future__ import annotations

import json

import pytest

from xiaocc.characters import (
    CharacterError,
    builtin_character_dir,
    load_character,
    parse_character,
)
from xiaocc.protocol import State


def _valid() -> dict:
    return json.loads((builtin_character_dir() / "character.json").read_text(encoding="utf-8"))


def test_builtin_character_loads_and_covers_every_state():
    character = load_character()
    assert set(character.states) == set(State), "内置角色必须覆盖全部状态"
    assert character.canvas[0] > 0 and character.canvas[1] > 0
    assert not character.warnings, f"内置角色不该有警告：{character.warnings}"


@pytest.mark.parametrize("state", list(State))
def test_each_state_must_be_declared(state: State):
    data = _valid()
    del data["states"][state.value]
    with pytest.raises(CharacterError, match=state.value):
        parse_character(data)


def test_unknown_motion_only_warns():
    data = _valid()
    data["states"]["idle"]["motion"] = "breakdance"
    character = parse_character(data)
    assert any("breakdance" in w for w in character.warnings)


def test_bad_accent_is_rejected():
    data = _valid()
    data["states"]["error"]["accent"] = "red"
    with pytest.raises(CharacterError, match="accent"):
        parse_character(data)


def test_bad_canvas_is_rejected():
    data = _valid()
    data["canvas"] = {"width": 0, "height": 240}
    with pytest.raises(CharacterError, match="canvas"):
        parse_character(data)


def test_missing_file_reports_path(tmp_path):
    with pytest.raises(CharacterError, match="找不到角色清单"):
        load_character(tmp_path / "nope")
