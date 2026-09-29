"""七支真窗口门禁的**统一运行时留痕**：把「最近一次绿没绿」变成盘上的证据。

为什么非有不可（2026-09-30）：门禁的结论只活在某个终端的 stdout 里，窗口一关就再没人能回答
「昨晚到底绿没绿」。更要命的是**看着绿不等于绿**，所以盘上必须能把两种绿分开：

* ``verify_drag_tracking.py`` 曾经全绿而真机是红的 —— 它 5 个调用点清一色
  ``backend.move_window_to()``，鼠标事件那条路径一次都没被驱动过 ⇒ 留痕里要留下**判据数字**
  （更新频率、p95 残余、往返跳次数…），不是一句 PASS；
* ``verify_drag_inject.py`` 带 ``--force`` 可跳过前置，而**锁屏时注入的 HID 事件会被系统吞掉**
  ⇒ 锁屏强跑同样给 rc=0，将来会和「环境干净时的真绿」长得一字不差 ⇒ 每条记录都带 ``force``
  与 ``env{locked, idle_s}``，让"强跑出来的绿"自己招认。

留痕（一律 0600，与 ``xiaoccctl.log`` 同目录同族）::

    ~/Library/Logs/xiaocc/verify.log           一行 JSON **追加**（历史账：O_APPEND，单次写入单行）
    ~/Library/Logs/xiaocc/verify/<gate>.json   该门禁的**最新态**（mkstemp + os.replace 原子替换）

门禁里只加两三行::

    import verify_log
    verify_log.record("verify_ctl_stop", rc, criteria={"passed": 12, "checks": 12})

**记录器绝不改变门禁判词**：任何异常都自己吞掉（best-effort），只往 stderr 打一行警告，返回
最新态路径或 None —— 绝不 raise、绝不 ``sys.exit``。留痕写不进去不该让一支门禁变红，
更不该让它变绿。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

LOG_DIR = Path.home() / "Library" / "Logs" / "xiaocc"
#: 测试接缝（照 ``ops/xiaocc_reverify.sh`` 的 ``XIAOCC_RV_LOGDIR`` 的路子）：沙箱里跑不碰真账
LOG_DIR_ENV = "XIAOCC_VERIFY_LOGDIR"
LOG_NAME = "verify.log"
LATEST_DIRNAME = "verify"
#: 读环境快照的超时（秒）。``CGEventSourceSecondsSinceLastEventType(HID)`` 在**拿不到 GUI 会话**
#: 的进程里不是报错、而是**永久阻塞**（本机沙箱里实测 >4s 不返回；ssh 进来的 shell 同理）⇒
#: 这一读必须能被掐断，否则"留痕"会变成"七支门禁一起卡死"。
ENV_TIMEOUT_S = 2.0


def _log_dir() -> Path:
    override = os.environ.get(LOG_DIR_ENV)
    return Path(override).expanduser() if override else LOG_DIR


def _warn(message: str) -> None:
    """留痕自己的毛病只许在 **stderr** 留一行 —— stdout 是门禁判词的地盘，一个字都不许碰。"""
    with contextlib.suppress(Exception):
        print(f"verify_log：{message}（不影响门禁判词）", file=sys.stderr)


def _now() -> str:
    """带时区的 ISO、秒精度：要和同一目录里 ``xiaoccctl.log`` 的时间对得上，不要毫秒。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _finite(value: Any) -> Any:
    """能进 JSON 的值：bool / int / 有限 float / 短字符串；其余（含 ``nan``、``inf``）丢掉。

    宁可少一个字段也不放 ``nan`` 进去 —— ``json.dumps`` 默认会把它照写成 ``NaN``，那不是合法
    JSON，**一行坏账能让整份历史读不回来**。
    """
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 3) if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:200]
    return None


def _as_int(rc: Any) -> int | None:
    try:
        return int(rc)
    except (TypeError, ValueError):
        return None


def _criteria(criteria: Mapping[str, Any] | None) -> dict[str, Any]:
    """门禁自己的判据数字（取不到的字段就不传，**绝不编数字**）。"""
    if not isinstance(criteria, Mapping):
        return {}
    cleaned = {}
    for key, value in criteria.items():
        kept = _finite(value)
        if kept is not None:
            cleaned[str(key)] = kept
    return cleaned


def _environment() -> dict[str, Any]:
    """``{locked, idle_s}`` —— **复用** ``verify_drag_inject.environment_snapshot()``。

    不在这里另抄一份「怎么读锁屏/空闲」的语义：判据（rc=2 那个前置）与证据（这行留痕）必须是
    同一套读数口径（同一个函数；留痕时再读一次，因为 ``record()`` 收不到门禁手里那一份），
    否则迟早出现「日志说没锁屏、门禁说锁着」这种自己打自己的账。
    读不到（非 macOS / 没装 Quartz / 卡住被掐断）就写 ``None``，明说"这一项没量到"，
    **不编一个 False 进去** —— 编出来的 ``locked=False`` 正好能把一次锁屏强跑洗成"环境干净的真绿"。
    """
    try:
        import verify_drag_inject  # 同目录 import：跑门禁时 sys.path[0] 就是 scripts/
    except Exception as exc:  # noqa: BLE001 - 证据缺一项，不该把门禁带倒
        _warn(f"环境快照没读到（locked/idle_s 记 null）：{type(exc).__name__}: {exc}")
        return {"locked": None, "idle_s": None}

    box: dict[str, Any] = {}

    def read() -> None:
        try:
            box["snapshot"] = verify_drag_inject.environment_snapshot()
        except BaseException as exc:  # noqa: BLE001 - 结果交回主线程处理
            box["error"] = exc

    # daemon：万一它卡在 mach 调用里，也不许拦着门禁进程退出
    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(ENV_TIMEOUT_S)
    snapshot = box.get("snapshot")
    if worker.is_alive():
        _warn(f"环境快照 {ENV_TIMEOUT_S:g}s 没读回来（这个进程多半拿不到 GUI 会话）⇒ 记 null")
        return {"locked": None, "idle_s": None}
    if not isinstance(snapshot, Mapping):
        error = box.get("error")
        _warn(f"环境快照没读到（locked/idle_s 记 null）：{type(error).__name__}: {error}")
        return {"locked": None, "idle_s": None}
    try:
        return {"locked": bool(snapshot["locked"]), "idle_s": _finite(snapshot["idle_s"])}
    except Exception as exc:  # noqa: BLE001 - 证据缺一项，不该把门禁带倒
        _warn(f"环境快照没读到（locked/idle_s 记 null）：{type(exc).__name__}: {exc}")
        return {"locked": None, "idle_s": None}


def _git() -> dict[str, Any]:
    """``rev`` + ``dirty``：这行绿是**哪份代码**跑出来的。

    没有版本，"上周那次绿"就永远对不上是哪棵树绿的；而 ``dirty=True`` 的绿只证明**当时那棵
    没提交的树**是绿的 —— 这正是"脚本全绿而真机是红的"最爱藏身的地方。
    """

    def git_out(*argv: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *argv],
                cwd=str(REPO),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    status = git_out("status", "--porcelain")
    return {
        "rev": git_out("rev-parse", "--short", "HEAD"),
        "dirty": None if status is None else bool(status),
    }


def _safe_name(gate: str) -> str:
    """门禁名要当文件名用：只留 ``[A-Za-z0-9._-]``，别让谁拿 ``../`` 把最新态写到别处去。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", gate)[:80] or "unknown"


def _append_line(path: Path, line: str) -> None:
    """一行 JSON **追加**（O_APPEND + 单次 write）：两支门禁并发跑也不会互相截断或穿插。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(path, 0o600)  # umask 可能把新文件建成 0644；也可能躺着别人早先建的旧账
        # 写满循环：普通文件上单次 ``os.write`` 几乎不会短写，但上一句注释自己说了
        # 「一行坏账能让整份历史读不回来」——那就配一个循环，别让注释比代码硬。
        view = memoryview(line.encode("utf-8"))
        while view:
            written = os.write(fd, view)
            if written <= 0:  # 写不动了（磁盘满/被信号打断）：宁可这行残缺，也不许死循环
                break
            view = view[written:]
    finally:
        os.close(fd)


def _write_latest(path: Path, payload: str) -> None:
    """最新态：``mkstemp`` + ``os.replace`` 原子替换 —— 读者永远看不到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def record(
    gate: str,
    rc: int,
    criteria: Mapping[str, Any] | None = None,
    force: bool = False,
    blocker: str | None = None,
) -> Path | None:
    """给 ``gate`` 记一次运行结果。成功返回最新态文件路径，任何失败返回 ``None``（绝不抛）。

    ``rc`` 是门禁自己的退出码（0 绿 / 1 FAIL / 2 环境不满足…），``blocker`` 是前置不满足的
    原因，``force`` 是这次有没有 ``--force`` 跳过前置 —— 后两个字段存在的唯一理由就是：
    **让"锁屏强跑出来的 rc=0"和"环境干净的真绿"在盘上不可能是同一行**。
    """
    try:
        entry = {
            "at": _now(),
            "gate": str(gate),
            "rc": _as_int(rc),
            # 和 ``criteria`` 里的字符串同一个上限（``_finite`` 的 200 字）：
            # 同一份记录里两条口径不一致，读的人就得记两套规则。
            "blocker": None if blocker is None else str(blocker)[:200],
            "force": bool(force),
            "env": _environment(),
            **_git(),
            "pid": os.getpid(),
            "criteria": _criteria(criteria),
        }
        root = _log_dir()
        _append_line(root / LOG_NAME, json.dumps(entry, ensure_ascii=False) + "\n")
        latest = root / LATEST_DIRNAME / f"{_safe_name(str(gate))}.json"
        _write_latest(latest, json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
        return latest
    except Exception as exc:  # noqa: BLE001 - 记录器绝不允许把门禁带倒
        _warn(f"{gate}: 留痕没写成 {type(exc).__name__}: {exc}")
        return None
