"""角色包：小cc「长什么样、怎么动」的唯一描述。

一个角色包 = 一个目录 + 一份 ``character.json``。里面必须为**全部 7 个状态**
给出表现，协议层不允许出现「这个状态角色不知道怎么办」。

字段说明（``character.json``）::

    {
      "id": "xiaocc",
      "name": "小cc",
      "version": "0.1.0",
      "author": "ChenC",
      "license": "MIT",
      "canvas": {"width": 220, "height": 240},
      "default_scale": 1.0,
      "palette": {"body": "#F3EFE4", "ink": "#2B2E3A", "accent": "#6FD3E8"},
      "states": {
        "idle":     {"motion": "float",       "accent": "#6FD3E8", "caption": "待命"},
        "thinking": {"motion": "ponder",      "accent": "#8FA7FF", "caption": "思考中"},
        "working":  {"motion": "busy",        "accent": "#FFC24B", "caption": "干活中"},
        "waiting":  {"motion": "beckon",      "accent": "#FF8A5B", "caption": "等你确认"},
        "done":     {"motion": "cheer",       "accent": "#5BD08A", "caption": "搞定"},
        "error":    {"motion": "wobble",      "accent": "#F2665E", "caption": "出错了"},
        "offline":  {"motion": "sleep",       "accent": "#9AA1B1", "caption": "离线"}
      },
      "assets": {"base": "assets/base.svg"}      // 可选；没有就由显示层程序化绘制
    }

``motion`` 是**给渲染层的动作名**，不是自由文本：显示层用 :data:`KNOWN_MOTIONS`
里的动作做动画，写错会告警（这样第三方角色不会因为拼错动作名而静默不动）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .protocol import State

__all__ = [
    "KNOWN_MOTIONS",
    "Character",
    "CharacterError",
    "StateSpec",
    "available_characters",
    "builtin_character_dir",
    "load_character",
]

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

#: 显示层保证能演的动作。第三方角色用别的动作名不算错，但会告警。
KNOWN_MOTIONS = frozenset(
    {
        "float",    # 待机漂浮/呼吸
        "ponder",   # 思考：转圈、眨眼、头顶冒泡
        "busy",     # 干活：快速小动作、忙碌
        "beckon",   # 招呼人：举手/闪光
        "cheer",    # 完成：跳一下、撒花
        "wobble",   # 出错：抖动、变红
        "sleep",    # 离线：闭眼、慢呼吸
    }
)


class CharacterError(ValueError):
    """角色包不合法。带上是哪个文件的哪个字段，方便第三方作者自己修。"""


@dataclass(frozen=True)
class StateSpec:
    motion: str
    accent: str = "#6FD3E8"
    caption: str = ""


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    version: str
    author: str
    license: str
    canvas: tuple[int, int]
    states: Mapping[State, StateSpec]
    palette: Mapping[str, str] = field(default_factory=dict)
    default_scale: float = 1.0
    assets: Mapping[str, str] = field(default_factory=dict)
    path: Path | None = None
    warnings: tuple[str, ...] = ()

    def spec(self, state: State) -> StateSpec:
        return self.states[state]

    def accent(self, state: State) -> str:
        return self.states[state].accent or self.palette.get("accent", "#6FD3E8")


def builtin_character_dir() -> Path:
    """内置角色包目录（packaging 后也在包内）。"""
    return Path(__file__).resolve().parent / "characters" / "xiaocc"


def available_characters(root: Path | None = None) -> list[Path]:
    """可用的角色包目录：内置 + ``~/.xiaocc/characters/*``。"""
    out: list[Path] = []
    for base in (builtin_character_dir().parent, Path.home() / ".xiaocc" / "characters"):
        if base and base.is_dir():
            out.extend(p for p in sorted(base.iterdir()) if (p / "character.json").is_file())
    return out


def load_character(target: str | Path | None = None) -> Character:
    """加载并校验角色包。``target`` 可以是目录，也可以是 character.json 本身。"""
    if target is None:
        path = builtin_character_dir()
    else:
        path = Path(target).expanduser()
    manifest_path = path / "character.json" if path.is_dir() else path
    if not manifest_path.is_file():
        raise CharacterError(f"找不到角色清单：{manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CharacterError(f"{manifest_path} 不是合法 JSON：{exc}") from exc
    return parse_character(data, base_dir=manifest_path.parent)


def parse_character(data: Mapping[str, Any], *, base_dir: Path | None = None) -> Character:
    """校验并构造 :class:`Character`。所有错误都带上字段名，不猜。"""
    if not isinstance(data, Mapping):
        raise CharacterError("角色清单顶层必须是 JSON 对象")

    def required(key: str) -> Any:
        if key not in data:
            raise CharacterError(f"角色清单缺少必填字段 {key!r}")
        return data[key]

    warnings_: list[str] = []
    canvas_raw = required("canvas")
    if not isinstance(canvas_raw, Mapping):
        raise CharacterError("canvas 必须是对象，形如 {\"width\": 220, \"height\": 240}")
    width, height = int(canvas_raw.get("width", 0)), int(canvas_raw.get("height", 0))
    if width <= 0 or height <= 0:
        raise CharacterError(f"canvas 尺寸必须为正整数，收到 {width}x{height}")

    states_raw = required("states")
    if not isinstance(states_raw, Mapping):
        raise CharacterError("states 必须是对象")
    states: dict[State, StateSpec] = {}
    for state in State:
        entry = states_raw.get(state.value)
        if entry is None:
            raise CharacterError(
                f"states 缺少 {state.value!r} —— 每个角色包必须覆盖全部 7 个状态，"
                "不允许「未知状态」这种兜底"
            )
        if not isinstance(entry, Mapping):
            raise CharacterError(f"states.{state.value} 必须是对象")
        motion = str(entry.get("motion") or "").strip()
        if not motion:
            raise CharacterError(f"states.{state.value}.motion 不能为空")
        if motion not in KNOWN_MOTIONS:
            warnings_.append(
                f"states.{state.value}.motion={motion!r} 不在内置动作集里，"
                f"显示层可能不支持（已知：{', '.join(sorted(KNOWN_MOTIONS))}）"
            )
        accent = str(entry.get("accent") or "").strip()
        if accent and not _HEX.match(accent):
            raise CharacterError(f"states.{state.value}.accent 必须是 #RGB 或 #RRGGBB，收到 {accent!r}")
        states[state] = StateSpec(motion=motion, accent=accent, caption=str(entry.get("caption") or ""))

    palette_raw = data.get("palette") or {}
    if not isinstance(palette_raw, Mapping):
        raise CharacterError("palette 必须是对象")
    palette = {}
    for key, value in palette_raw.items():
        if not _HEX.match(str(value)):
            raise CharacterError(f"palette.{key} 必须是 #RGB 或 #RRGGBB，收到 {value!r}")
        palette[str(key)] = str(value)

    assets_raw = data.get("assets") or {}
    if not isinstance(assets_raw, Mapping):
        raise CharacterError("assets 必须是对象")
    assets: dict[str, str] = {}
    for key, rel in assets_raw.items():
        assets[str(key)] = str(rel)
        if base_dir is not None and not (base_dir / str(rel)).is_file():
            warnings_.append(f"assets.{key} 指向的文件不存在：{base_dir / str(rel)}")

    scale = float(data.get("default_scale", 1.0))
    if scale <= 0:
        raise CharacterError(f"default_scale 必须为正数，收到 {scale}")

    return Character(
        id=str(required("id")),
        name=str(required("name")),
        version=str(data.get("version") or "0.0.0"),
        author=str(data.get("author") or ""),
        license=str(data.get("license") or ""),
        canvas=(width, height),
        states=states,
        palette=palette,
        default_scale=scale,
        assets=assets,
        path=base_dir,
        warnings=tuple(warnings_),
    )
