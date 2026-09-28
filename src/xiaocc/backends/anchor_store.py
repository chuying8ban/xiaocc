"""窗口锚点的落盘存储（只存取，不碰任何 GUI 库）。

为什么要有这个文件
------------------
桌宠的窗口位置有两种完全不同的语义，必须分开对待：

1. **用户拖动** = 有意搬家。用户把桌宠拖到哪儿，就是他希望它待在那儿；
   下次启动必须还停在那个位置，绝不能自己弹回默认角落。
2. **除拖动外的任何位移** = bug。比如布局计算写错、多显示器切换后坐标算歪、
   某段代码顺手 setFrame 了一下 —— 这些都属于「不该动却动了」，必须回到锚点。
   （唯一的例外是贴边收起/展开这类明确的临时态，它结束后同样要能回到锚点。）

有了第 2 条，就需要一个「正确答案」存在磁盘上：这就是锚点。
运行时随时可以把窗口当前坐标和锚点比一比，不一致就说明有东西在偷偷挪窗口。

谁写它 / 谁读它
---------------
* **写**：显示层在用户拖动落定（drag 结束、窗口停稳）之后调用 :func:`save_anchor`。
  除此之外没有任何代码应该写这个文件。
* **读**：显示层启动时调用 :func:`load_anchor` 决定初始位置（读不到就用默认角）；
  运维的 doctor 也读它当参照，用来判断窗口现在是不是还在锚点上。

本模块只做纯 IO：读坏了、类型不对、写失败，一律返回 ``None`` / ``False``，
**绝不抛异常**，也**不导入任何 GUI 库**（这样在 Linux CI 上照样能测）。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_ANCHOR_PATH",
    "ENV_OVERRIDE",
    "anchor_path",
    "load_anchor",
    "save_anchor",
]

DEFAULT_ANCHOR_PATH = Path.home() / ".xiaocc" / "anchor.json"
ENV_OVERRIDE = "XIAOCC_ANCHOR_FILE"

_NOTE = "用户拖动后记下的锚点"


def anchor_path() -> Path:
    """锚点文件路径：环境变量 ``XIAOCC_ANCHOR_FILE`` 优先，否则 ``~/.xiaocc/anchor.json``。

    环境变量是给测试和临时排查用的（测试靠前者），空字符串视同未设置。
    """
    raw = os.environ.get(ENV_OVERRIDE, "")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return DEFAULT_ANCHOR_PATH


def _is_number(value: Any) -> bool:
    """只认 int / float，明确排除 bool（bool 是 int 的子类，但显然不是坐标）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def load_anchor(path: Path | None = None) -> tuple[float, float] | None:
    """读锚点，返回 ``(x, y)``。

    文件不存在、JSON 坏了、不是对象、``x``/``y`` 缺失或类型不对（含 bool、
    NaN/Inf），一律返回 ``None``，**绝不抛异常**。文件里的多余字段直接忽略。
    坐标给了小数就原样保留小数。
    """
    target = Path(path) if path is not None else anchor_path()
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 —— 契约是「读坏了一律当没锚点」，见模块 docstring
        return None

    if not isinstance(data, dict):
        return None

    x = data.get("x")
    y = data.get("y")
    if not _is_number(x) or not _is_number(y):
        return None

    return (float(x), float(y))


def save_anchor(x: float, y: float, path: Path | None = None) -> bool:
    """写锚点，成功返回 ``True``。

    目录不存在就建；**原子写**：先把内容写进同目录下的临时文件，再 ``os.replace``
    覆盖目标，避免进程被杀时留下半个文件。坐标非法、权限不够、磁盘满了等任何
    失败都返回 ``False``，**绝不抛异常**。
    """
    target = Path(path) if path is not None else anchor_path()

    if not _is_number(x) or not _is_number(y):
        return False

    payload = {
        "x": float(x),
        "y": float(y),
        "saved_at": int(time.time()),
        "note": _NOTE,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    tmp_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
        return True
    except Exception:  # noqa: BLE001 —— 契约是「写不进去返回 False」，日志由调用方留
        return False
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):  # 清临时文件失败不值得再抛：真正该管的是上面的返回值
                tmp_path.unlink()
