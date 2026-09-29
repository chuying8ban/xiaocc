"""设置的读/写/回落（纯文件，不需要窗口服务器）。

为什么这些用例值得写：``click_action`` 是**用户手输**的枚举（面板写、也可能有人手改 JSON），
拼错的 key 一旦被静默当成「没配置」，用户看到的就是「我设了却不生效」——那是要花一小时
去查的投诉。所以「非法值必须回落 + 必须报出来」在这里钉死。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from xiaocc import settings as S


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    assert S.load(target) == S.DEFAULTS
    assert not target.exists(), "读一次缺文件不该产生副作用（不自动创建）"


def test_default_click_action_is_badge() -> None:
    assert S.DEFAULTS["click_action"] == "badge"


def test_invalid_value_falls_back_and_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"click_action": "baldge"}), encoding="utf-8")
    assert S.load(target)["click_action"] == "badge", "拼错的值必须回落，不许当成「没配置」"
    described = S.describe(target)
    assert described["problems"], "回落的原因必须能看见"
    assert "baldge" in described["problems"][0]


def test_broken_json_falls_back_without_raising(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{ 这不是 JSON", encoding="utf-8")
    assert S.load(target) == S.DEFAULTS
    assert S.describe(target)["problems"]


def test_save_writes_0600_atomically_and_only_known_keys(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "settings.json"
    result = S.save({"click_action": "none", "click_actions": "typo", "whatever": 1}, target)
    assert result["click_action"] == "none"
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"设置文件权限是 {oct(mode)}"
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == {"schema": 1, "click_action": "none"}, "只认识的键才落盘"
    assert not list(target.parent.glob("*.tmp")), "临时文件必须被 replace 掉"


def test_save_keeps_what_was_there(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    S.save({"click_action": "caption"}, target)
    S.save({"click_action": "badge"}, target)
    assert S.load(target)["click_action"] == "badge"


def test_save_rejects_invalid_and_still_writes_default(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    result = S.save({"click_action": "nope"}, target)
    assert result["click_action"] == "badge"
    assert S.load(target)["click_action"] == "badge"


def test_labels_cover_every_action() -> None:
    assert set(S.CLICK_ACTION_LABELS) == set(S.CLICK_ACTIONS)


def test_env_override_path(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "other.json"
    monkeypatch.setenv(S.ENV_OVERRIDE, str(override))
    assert S.settings_path() == override
