"""面板用到的**路径**与**进程间那点事**（不碰 AppKit，桌宠侧也要 import）。

桌宠上的入口只做一件事：:func:`request_open` —— 写一个 request 文件。面板进程每 0.5s
看一眼那个文件，比自己上次处理的新就刷新并把自己抬到前面；没有活着的面板就当场拉一个。
这样「点桌宠」在面板开着/没开两种情况下都有反应，也不会开出一堆窗口、不留后台空壳。

三个文件都落在 ``~/.xiaocc/``（和 probe / anchor / quota 同一处），都可用同名环境变量改到
临时目录 —— 回归脚本靠这个把状态写到 ``tempfile.mkdtemp()`` 里，绝不碰用户真实的那份。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


XIAOCC_DIR = Path.home() / ".xiaocc"

#: 桌宠自证据（默认值必须和 :mod:`xiaocc.backends.appkit` 一致）
DEFAULT_PROBE_PATH = _env_path("XIAOCC_PROBE_FILE", XIAOCC_DIR / "probe.json")

#: 面板进程自己的状态文件（pid/开始时间/皮肤）
PANEL_STATE = _env_path("XIAOCC_PANEL_STATE", XIAOCC_DIR / "panel.json")

#: 「把面板打开/抬到前面」的请求文件（桌宠写、面板读）
PANEL_REQUEST = _env_path("XIAOCC_PANEL_REQUEST", XIAOCC_DIR / "panel.request")

#: 页面在磁盘上的那份（窗口里用的是同一份 HTML；想在浏览器里看就直接打开它）
DEFAULT_PANEL_HTML = _env_path("XIAOCC_PANEL_HTML", XIAOCC_DIR / "panel.html")

#: 面板轮询请求文件的周期（秒）
REQUEST_POLL_S = 0.5

#: 「有人正在拉起面板」的互斥锁（O_EXCL），防止一次点击开出 N 个窗口
SPAWN_LOCK = _env_path("XIAOCC_PANEL_LOCK", XIAOCC_DIR / "panel.spawn")
SPAWN_LOCK_TTL_S = 20.0

#: 由 :func:`request_open` 拉起来的子进程带的标记：它不该再走一遍「要不要拉一个」
CHILD_ENV = "XIAOCC_PANEL_CHILD"


def read_json(path: Path) -> dict[str, Any]:
    """读一个 JSON 对象；不在/坏了都返回 ``{}``（面板与桌宠都不许因此报错）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, (int, str)):
        return False
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_panel() -> int | None:
    """活着的面板进程 pid，没有就是 ``None``（状态文件里记的 pid 已死 ⇒ 顺手清掉）。"""
    pid = read_json(PANEL_STATE).get("pid")
    if pid_alive(pid):
        return int(str(pid))
    try:
        PANEL_STATE.unlink()
    except OSError:
        pass
    return None


def take_spawn_lock() -> bool:
    """抢「我正在拉起面板」的锁。抢到 = 由我来拉；抢不到 = 别人正在拉，别开第二个。

    O_EXCL 是这里唯一靠谱的原子原语（先写 panel.json 再拉进程的话，两个调用方会同时看不到
    状态文件而各开一个窗口 —— 实测：一次点击开出 3 个面板窗口）。
    持锁方写好自己的 panel.json 后立刻放锁；进程中途死掉则靠 :data:`SPAWN_LOCK_TTL_S` 过期。
    """
    try:
        fd = os.open(SPAWN_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = time.time() - SPAWN_LOCK.stat().st_mtime
        except OSError:
            return False
        if age < SPAWN_LOCK_TTL_S:
            return False
        try:  # 陈锁：上一个拉起流程没放锁就死了
            SPAWN_LOCK.unlink()
        except OSError:
            return False
        return take_spawn_lock()
    except OSError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except OSError:
        pass
    return True


def release_spawn_lock() -> None:
    try:
        SPAWN_LOCK.unlink()
    except OSError:
        pass


#: 面板里可以翻的页（右键菜单「设置…」= 请求 settings 页，一个进程一个窗口，翻页不另开窗口）
PAGES = ("panel", "settings")


def request_open(
    *, theme: str | None = None, page: str = "panel", spawn: bool = True
) -> str:
    """请求「把面板打开/抬到前面」，`page=` 指定翻到哪一页。

    返回：

    * ``"raised"``  —— 已经有面板开着，已请求它刷新 + 抬到前面（并按需翻页）；
    * ``"spawned"`` —— 之前没有，当场拉了一个；
    * ``"failed"``  —— 请求文件写不进去，或拉进程失败。

    桌宠的入口只调它 —— 不 import AppKit、不阻塞、失败只记一行日志。
    """
    if page not in PAGES:
        page = "panel"
    try:
        PANEL_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        PANEL_REQUEST.write_text(
            json.dumps({"at": time.time(), "theme": theme, "page": page}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.chmod(PANEL_REQUEST, 0o600)
    except OSError:
        return "failed"
    if running_panel() is not None or not spawn:
        return "raised"
    if not take_spawn_lock():  # 别人正在拉，别开第二个窗口
        return "raised"
    kwargs: dict[str, Any] = {}
    if sys.platform == "darwin":
        kwargs["start_new_session"] = True  # 别跟桌宠共享控制终端/信号组
    env = dict(os.environ)
    env[CHILD_ENV] = "1"  # 子进程别再走一遍「要不要拉一个」（否则子生孙，一次点击开出 N 个窗口）
    try:
        subprocess.Popen(
            [sys.executable, "-m", "xiaocc.panel", "--request"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
    except OSError:
        release_spawn_lock()
        return "failed"
    return "spawned"
