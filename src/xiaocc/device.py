"""设备状态采集（CPU / 内存 / 磁盘 / 电池 / 开机时长）。

用户 2026-09-29 要求：小cc 要能看设备状态，右键显示。

设计约束（跟额度采集器同一套口径）：

* **纯本地、零网络、不要 sudo**；
* **取不到就写「未取到」**，绝不补 0 或编数字（每一项各自 try，坏一项不拖累其余）；
* **不进 UI 循环**：只在「右键菜单要弹」和「面板要渲染」时按需采一次，带 TTL 缓存
  （空闲 <5% CPU 那条线不能被吃掉）；
* 可测：所有解析都是纯函数（:func:`parse_vm_stat` / :func:`parse_pmset_batt`），
  真机输出直接进单测。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: 采样缓存：菜单连着弹、面板跟着渲染时不用反复起子进程
TTL_S = 2.0

#: `sysctl`/`vm_stat`/`pmset` 的超时——卡住也不能把菜单挂住
_CMD_TIMEOUT_S = 2.0

#: CPU 利用率是两个累计 tick 的差商：窗口太短算出来就是噪声（面板一打开就渲染时尤其）
#: ⇒ 窗口短于这个值就**不报 CPU**（写「未取到」），别拿噪声当真值
_MIN_CPU_WINDOW_S = 0.5

_GB = 1024.0**3

# —— Mach：CPU 累计 tick（一次读的是累计值，两次相隔的差才是利用率）——

_HOST_CPU_LOAD_INFO = 3
_CPU_STATE_MAX = 4


class _HostCpuLoadInfo(ctypes.Structure):
    _fields_ = [("cpu_ticks", ctypes.c_uint32 * _CPU_STATE_MAX)]


def _libc() -> ctypes.CDLL:
    path = ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib"
    lib = ctypes.CDLL(path, use_errno=True)
    lib.mach_host_self.restype = ctypes.c_uint32
    lib.host_statistics.argtypes = [
        ctypes.c_uint32,  # host
        ctypes.c_int,  # flavor
        ctypes.c_void_p,  # out
        ctypes.POINTER(ctypes.c_uint32),  # out count
    ]
    lib.host_statistics.restype = ctypes.c_int
    lib.sysctlbyname.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    lib.sysctlbyname.restype = ctypes.c_int
    return lib


def cpu_ticks() -> tuple[int, int] | None:
    """累计 ``(忙, 总)`` tick——差商就是这段时间的利用率。失败返回 None（不抛）。"""
    try:
        lib = _libc()
        info = _HostCpuLoadInfo()
        count = ctypes.c_uint32(_CPU_STATE_MAX)
        rc = lib.host_statistics(
            lib.mach_host_self(), _HOST_CPU_LOAD_INFO, ctypes.byref(info), ctypes.byref(count)
        )
        if rc != 0:
            return None
        ticks = list(info.cpu_ticks)
        busy = ticks[0] + ticks[1] + ticks[3]  # user + system + nice
        return busy, sum(ticks)
    except Exception:  # 采不到就走「未取到」，绝不让菜单跟着出事
        log.debug("CPU tick 读不到", exc_info=True)
        return None


def sysctl_uint64(name: str) -> int | None:
    """``sysctlbyname`` 读一个 64 位整数（``hw.memsize`` 这类）。"""
    try:
        lib = _libc()
        out = ctypes.c_uint64(0)
        size = ctypes.c_size_t(8)
        rc = lib.sysctlbyname(name.encode(), ctypes.byref(out), ctypes.byref(size), None, 0)
        return int(out.value) if rc == 0 else None
    except Exception:
        log.debug("sysctl %s 读不到", name, exc_info=True)
        return None


def _run(cmd: list[str]) -> str | None:
    try:
        done = subprocess.run(  # 命令是写死的
            cmd, capture_output=True, text=True, timeout=_CMD_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("跑 %s 失败", cmd, exc_info=True)
        return None
    return done.stdout or ""


# —— 纯解析（真机输出进单测）——

_VM_FIELDS = {
    "Pages free": "free",
    "Pages active": "active",
    "Pages inactive": "inactive",
    "Pages speculative": "speculative",
    "Pages wired down": "wired",
    "Pages occupied by compressor": "compressed",
    #: 活动监视器「内存已用」的主力项（老 macOS 的 vm_stat 没有这一行 ⇒ 由 used_bytes_from_vm 兜底）
    "Anonymous pages": "anonymous",
}
_PAGE_RE = re.compile(r"^(.+?):\s+(\d+)\.?$", re.MULTILINE)


def parse_vm_stat(text: str) -> dict[str, int]:
    """``vm_stat`` → ``{字段: 页数}``（键按 MACOS 的写法归一）。"""
    out: dict[str, int] = {}
    page_size = 4096
    match = re.search(r"page size of (\d+) bytes", text)
    if match:
        page_size = int(match.group(1))
    for name, count in _PAGE_RE.findall(text):
        key = _VM_FIELDS.get(name.strip())
        if key:
            out[key] = int(count)
    if out:
        out["page_size"] = page_size
    return out


def used_bytes_from_vm(pages: dict[str, int]) -> int | None:
    """已用内存 = （**匿名页** + 常驻页 + 压缩器页）× 页大小 —— 与活动监视器「内存已用」同口径。

    **2026-09-29 真机实测（24 GB / 16 KiB 页），三种口径在同一时刻差得不是"几十 MB"**：

    ======================  =========  ==================================
    口径                    读数       谁能对上
    ======================  =========  ==================================
    active+wired+压缩器      **13.8 GB**  （旧实现）谁也对不上
    匿名+常驻+压缩器（本式）  **16.0 GB**  活动监视器「内存已用」
    总内存−free−speculative  **20.4 GB**  `top` 的 ``PhysMem used``
    ======================  =========  ==================================

    用户会在活动监视器里对同一个数，所以取中间那个口径；旧实现少报约 2.3 GB（相对 15%）——
    在 24 GB 机器上就是「面板说 60%、活动监视器说 68%」，属于"看着合理其实错了"的那类假数。
    `匿名页` 缺失（更老的 macOS）时才退回 active 口径，宁可口径偏移也别整格显示「未取到」。
    """
    if not pages:
        return None
    page = pages.get("page_size", 4096)
    key = "anonymous" if pages.get("anonymous") else "active"
    used = pages.get(key, 0) + pages.get("wired", 0) + pages.get("compressed", 0)
    return used * page


_BATT_RE = re.compile(r"(\d+)%;\s*([^;]+);\s*([^\n]*)")
_STATE_WORDS = {
    "discharging": "用电中",
    "charging": "充电中",
    "charged": "已充满",
    "AC attached": "接电源",
    "finishing charge": "收尾充电",
}
_REST_RE = re.compile(r"^(\d+:\d+)\s*remaining")
_REST_WORDS = {
    "not charging": "未充电",
    "AC attached": "接着电源",
}


def parse_pmset_batt(text: str) -> tuple[int, str, str] | None:
    """``pmset -g batt`` → ``(电量%, 状态, 剩余)``；没有电池返回 None。

    真机一行长这样（2026-09-29 本机实录）：

        -InternalBattery-0 (id=27132003)\t90%; AC attached; not charging present: true

    第三段尾巴上的 ``present: true`` 是 pmset 自己拼上去的，不是剩余时间 ⇒ 先剥掉。
    """
    if "InternalBattery" not in text:
        return None
    match = _BATT_RE.search(text)
    if not match:
        return None
    state = _STATE_WORDS.get(match.group(2).strip(), match.group(2).strip())
    rest = re.sub(r"\s*present:\s*(true|false)", "", match.group(3)).strip()
    if rest_match := _REST_RE.match(rest):
        rest = f"剩 {rest_match.group(1)}"
    else:
        rest = _REST_WORDS.get(rest, rest) if rest else "剩余时间未取到"
    return int(match.group(1)), state, rest


def parse_boottime(text: str) -> float | None:
    """``sysctl -n kern.boottime``（``{ sec = 1755…, usec = … }``）→ 开机时刻。"""
    match = re.search(r"sec\s*=\s*(\d+)", text)
    return float(match.group(1)) if match else None


def human_duration(seconds: float) -> str:
    """秒 → 「3 天 4 小时」/「12 分钟」。"""
    seconds = max(0.0, seconds)
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"


def human_bytes(value: float) -> str:
    """字节 → 「9.4 GB」/「512 MB」。"""
    for unit, scale in (("GB", _GB), ("MB", 1024.0**2), ("KB", 1024.0)):
        if value >= scale:
            return f"{value / scale:.1f} {unit}"
    return f"{int(value)} B"


@dataclass(frozen=True)
class Device:
    """一次设备快照。**每个字段都可能为 None**（取不到就如实说，不补 0）。"""

    taken_at: float
    cpu_percent: float | None = None
    load: tuple[float, float, float] | None = None
    cpu_count: int | None = None
    mem_used: int | None = None
    mem_total: int | None = None
    disk_used: int | None = None
    disk_total: int | None = None
    battery: tuple[int, str, str] | None = None
    uptime_s: float | None = None

    #: 菜单 / 面板共用的中文行（取不到的项写「未取到」）
    def lines(self) -> list[tuple[str, str]]:
        """``[(标题, 值)]`` —— 顺序即展示顺序。"""
        rows: list[tuple[str, str]] = []
        cpu = "未取到" if self.cpu_percent is None else f"{self.cpu_percent:.0f}%"
        if self.load:
            cpu += f" · 负载 {' / '.join(f'{x:.2f}' for x in self.load)}"
        if self.cpu_count:
            cpu += f"（{self.cpu_count} 核）"
        rows.append(("CPU", cpu))
        if self.mem_used is not None and self.mem_total:
            pct = self.mem_used / self.mem_total * 100.0
            rows.append(
                (
                    "内存",
                    f"{human_bytes(self.mem_used)} / {human_bytes(self.mem_total)}（{pct:.0f}%）",
                )
            )
        else:
            rows.append(("内存", "未取到"))
        if self.disk_used is not None and self.disk_total:
            pct = self.disk_used / self.disk_total * 100.0
            rows.append(
                (
                    "磁盘",
                    f"{human_bytes(self.disk_used)} / {human_bytes(self.disk_total)}（{pct:.0f}%）",
                )
            )
        else:
            rows.append(("磁盘", "未取到"))
        if self.battery:
            pct, state, rest = self.battery
            rows.append(("电池", f"{pct}% {state} · {rest}"))
        if self.uptime_s is not None:
            rows.append(("已开机", human_duration(self.uptime_s)))
        return rows


def snapshot(prev_ticks: tuple[int, int] | None = None) -> Device:
    """采一次。``prev_ticks`` 给了就算这段区间的 CPU 利用率，否则 CPU 写未取到。"""
    now = time.time()
    cpu_ratio = None
    if prev_ticks is not None:
        ticks = cpu_ticks()
        if ticks is not None:
            busy0, total0 = prev_ticks
            busy1, total1 = ticks
            delta = total1 - total0
            if delta > 0:
                cpu_ratio = max(0.0, min(100.0, (busy1 - busy0) / delta * 100.0))
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    disk = None
    try:
        disk = shutil.disk_usage("/")
    except OSError:
        log.debug("磁盘用量读不到", exc_info=True)
    batt = None
    out = _run(["pmset", "-g", "batt"])
    if out:
        batt = parse_pmset_batt(out)
    boot = None
    out = _run(["sysctl", "-n", "kern.boottime"])
    if out:
        boot = parse_boottime(out)
    mem_total = sysctl_uint64("hw.memsize")
    mem_used = None
    out = _run(["vm_stat"])
    if out:
        mem_used = used_bytes_from_vm(parse_vm_stat(out))
    return Device(
        taken_at=now,
        cpu_percent=cpu_ratio,
        load=(load[0], load[1], load[2]) if load else None,
        cpu_count=os.cpu_count(),
        mem_used=mem_used,
        mem_total=mem_total,
        disk_used=disk.used if disk else None,
        disk_total=disk.total if disk else None,
        battery=batt,
        uptime_s=(now - boot) if boot else None,
    )


class Sampler:
    """带 TTL 缓存的采集器：**只在菜单要弹/面板要渲染时被问一次**。

    CPU 利用率靠两次累计 tick 的差 ⇒ 第一次问到就返回 None（写「未取到」），
    之后每次问到给的都是「距上次采样这段」的利用率。构造时就抓一次基线，
    所以第一次右键（通常离启动好几秒）已经能给出真值。
    """

    def __init__(self, ttl: float = TTL_S) -> None:
        self._ttl = ttl
        self._prev_ticks = cpu_ticks()
        self._prev_at = time.monotonic()
        self._cache: Device | None = None

    def get(self, *, fresh: bool = False) -> Device:
        now = time.monotonic()
        if not fresh and self._cache is not None and now - self._cache.taken_at < self._ttl:
            return self._cache
        window = now - self._prev_at
        if window < _MIN_CPU_WINDOW_S:
            if self._cache is not None:  # 问得太挤：沿用上次的数
                return self._cache
            # 还没出过 CPU 读数、窗口又太短（面板一打开就渲染、桌宠刚启动就右键）：
            # **等够这半秒再采一次**（一次性），别让 CPU 那格永远是「未取到」。
            time.sleep(_MIN_CPU_WINDOW_S - window)
            now = time.monotonic()
            window = now - self._prev_at
        dev = snapshot(self._prev_ticks if window >= _MIN_CPU_WINDOW_S else None)
        self._prev_ticks = cpu_ticks()
        self._prev_at = now
        self._cache = dev
        return dev
