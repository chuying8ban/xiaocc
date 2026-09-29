"""小cc 的设置：**唯一的**放法就是 ``~/.xiaocc/settings.json``（0600 + 原子写）。

为什么要有这个模块，而不是各写各的：桌宠、面板、（以后的设置窗口）都要读同一份设置。
谁留一份自己的副本，就会出现「面板里改了、桌宠还照旧的看」——那是可复现的投诉，不是洁癖。

规矩（和 quota.json / anchor.json 同一套）：

* 文件不存在 ⇒ 用默认值，**不报错、不自动创建**（读一次缺文件不该产生副作用）；
* 值非法 ⇒ **回落默认值**并记进 :func:`describe`（拼错的 key 不许静默变成「没配置」）；
* 写盘 ⇒ ``mkstemp`` + ``os.replace`` 原子替换 + 0600（里面有用户偏好，虽然不含密钥）。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: 单击小cc 时做什么。``badge`` 显示额度条、``device`` 显示**电脑状态**（CPU/内存/磁盘/电池）、
#: ``all`` 两样都要（一行额度 + 一行设备）、``none`` 什么都不显示。
#: ``caption``（旧的「状态文案」）**继续认**、读入时映射成 ``device``：用户 2026-09-29 把这个词
#: 重定义成「电脑的状态」，而这台机器的 settings.json 里此刻正写着 ``caption`` —— 直接摘掉枚举
#: 会让它回落默认，用户会以为自己的设置被吃了。
CLICK_ACTIONS = ("badge", "device", "all", "none")

#: 旧枚举名 → 新枚举名（只在**读入规范化**里用；写盘只写新名）
CLICK_ACTION_ALIASES = {"caption": "device"}

#: 单击动作的中文名（面板/菜单共用一份，别在两处各写一份）
CLICK_ACTION_LABELS = {
    "badge": "额度",
    "device": "设备状态",
    "all": "额度+设备",
    "none": "不显示",
}

DEFAULTS: dict[str, Any] = {
    "schema": 1,
    "click_action": "badge",
}

#: 允许写盘的键（别的键一律忽略，避免把拼错的键落进文件里再也看不出来）
KNOWN_KEYS = ("click_action",)

ENV_OVERRIDE = "XIAOCC_SETTINGS_FILE"


def settings_path() -> Path:
    """设置文件路径：环境变量 ``XIAOCC_SETTINGS_FILE`` 优先，否则 ``~/.xiaocc/settings.json``。"""
    override = os.environ.get(ENV_OVERRIDE)
    return Path(override) if override else Path.home() / ".xiaocc" / "settings.json"


def normalize(raw: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """把任意输入收敛成合法设置，返回 ``(设置, 被回落的原因)``。纯函数，好测。"""
    out = dict(DEFAULTS)
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        return out, ["设置不是一份 JSON 对象 ⇒ 全部用默认值"]
    for key in KNOWN_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "click_action":
            value = CLICK_ACTION_ALIASES.get(value, value)
            if value in CLICK_ACTIONS:
                out[key] = value
            else:
                problems.append(f"click_action={value!r} 不认识 ⇒ 回落默认 {DEFAULTS['click_action']!r}")
    unknown = sorted(set(raw) - set(KNOWN_KEYS) - {"schema"})
    if unknown:
        problems.append("忽略不认识的键：" + ", ".join(unknown))
    return out, problems


def load(path: Path | None = None) -> dict[str, Any]:
    """读设置。文件不在/坏了都返回默认值，**永不抛**（桌宠每拍都要读它）。"""
    target = Path(path) if path is not None else settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    value, _ = normalize(raw)
    return value


def describe(path: Path | None = None) -> dict[str, Any]:
    """:func:`load` 的带诊断版本：给 ``xiaoccctl doctor`` / 面板用。"""
    target = Path(path) if path is not None else settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"path": str(target), "exists": False, "problems": [], **DEFAULTS}
    except (OSError, ValueError) as exc:
        return {"path": str(target), "exists": True, "problems": [f"读不出来：{exc}"], **DEFAULTS}
    value, problems = normalize(raw)
    return {"path": str(target), "exists": True, "problems": problems, **value}


def save(updates: Mapping[str, Any], path: Path | None = None) -> dict[str, Any]:
    """把 ``updates`` 并进现有设置并原子落盘。返回落盘后的设置（含 ``problems``）。"""
    target = Path(path) if path is not None else settings_path()
    current = load(target)
    merged = dict(current)
    merged.update({k: v for k, v in updates.items() if k in KNOWN_KEYS})
    value, problems = normalize(merged)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    value["path"] = str(target)
    value["problems"] = problems
    return value
