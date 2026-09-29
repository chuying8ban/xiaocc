"""`quota.json` 的读写。

规矩抄看门狗那一套（今天已经踩过一遍）：
- **原子写**：`mkstemp` 同目录 + `os.replace`，避免半截 JSON 被面板读到；
- **0600**：这里可能有账户金额轨迹，不该别人可读；
- **失败吞掉**：额度文件写不进去绝不许影响桌宠（返回 False，调用方记一行日志就行）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

#: 超过这个时间没刷新就当陈旧（采集器是 15min 一拍，两拍没动就是它出问题了）
STALE_AFTER_S = 30 * 60


def save(report: dict[str, Any], path: Path) -> bool:
    """原子写 + 0600。任何失败返回 False，不抛。"""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001 - 诊断文件写不进去不该影响任何人
        return False
    return True


def load(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """读回 (报告, 元信息)。元信息里带 ``age_s`` / ``stale``，文件不在时报告为 None。"""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None, {"exists": False, "age_s": None, "stale": True, "path": str(path)}
    meta = {
        "exists": True,
        "age_s": max(0.0, round(time.time() - stat.st_mtime, 1)),
        "stale": (time.time() - stat.st_mtime) > STALE_AFTER_S,
        "path": str(path),
    }
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, {**meta, "stale": True, "corrupt": True}
    if not isinstance(report, dict):
        return None, {**meta, "stale": True, "corrupt": True}
    return report, meta
