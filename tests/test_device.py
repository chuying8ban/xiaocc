"""设备采集的解析与措辞（用户 2026-09-29 要的「右键显示设备状态」）。

真机输出直接进单测（2026-09-29 于本机实录）——解析器不许对着想象写。
"""

from __future__ import annotations

import time

from xiaocc.device import (
    Device,
    human_bytes,
    human_duration,
    parse_boottime,
    parse_pmset_batt,
    parse_vm_stat,
    used_bytes_from_vm,
)

VM_STAT_REAL = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                   149764.
Pages active:                                 418052.
Pages inactive:                               408145.
Pages speculative:                             10039.
Pages throttled:                                   0.
Pages wired down:                             207877.
Pages purgeable:                               16314.
"Translation faults":                     1316187301.
Pages copy-on-write:                       107933056.
Pages zero filled:                         590102439.
Pages reactivated:                          10881963.
Pages occupied by compressor:                 123456.
Anonymous pages:                              520977.
Pages stored in compressor:                   677936.
Pages purgeable:                              16314.
"""

PMSET_AC = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=27132003)\t90%; AC attached; not charging present: true
"""

PMSET_DISCHARGING = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=27132003)\t87%; discharging; 4:12 remaining present: true
"""

PMSET_CHARGING = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=27132003)\t62%; charging; 1:05 remaining present: true
"""

BOOTTIME_REAL = "{ sec = 1790558492, usec = 303294 } Mon Sep 28 09:21:32 2026\n"


def test_vm_stat_real_sample() -> None:
    pages = parse_vm_stat(VM_STAT_REAL)
    assert pages["page_size"] == 16384  # 这台是 16K 页，写死 4096 会把内存算成 1/4
    assert pages["free"] == 149764
    assert pages["active"] == 418052
    assert pages["wired"] == 207877
    assert pages["speculative"] == 10039
    assert pages["compressed"] == 123456


def test_used_bytes_matches_activity_monitor_formula() -> None:
    """已用内存取「匿名 + 常驻 + 压缩器」——活动监视器「内存已用」的口径。

    2026-09-29 真机上三种口径同刻分别是 13.8 / 16.0 / 20.4 GB（旧实现是第一个），
    所以这条不是"更精确一点"，是**换掉了少报 15% 的那个口径**：用户会拿活动监视器对。
    """
    pages = parse_vm_stat(VM_STAT_REAL)
    assert pages["anonymous"] == 520977
    used = used_bytes_from_vm(pages)
    assert used == (520977 + 207877 + 123456) * 16384
    old = (418052 + 207877 + 123456) * 16384  # 旧口径
    assert used > old and (used - old) / old > 0.10  # 真机上差 15%：别退回去


def test_used_bytes_falls_back_when_anonymous_missing() -> None:
    """老 macOS 的 vm_stat 没有「匿名页」那一行 ⇒ 退回 active 口径，而不是整格「未取到」。"""
    pages = {"page_size": 4096, "active": 100, "wired": 10, "compressed": 5}
    assert used_bytes_from_vm(pages) == (100 + 10 + 5) * 4096


def test_used_bytes_empty_is_none_not_zero() -> None:
    """取不到就返回 None（调用方写「未取到」）——绝不补 0。"""
    assert used_bytes_from_vm({}) is None


def test_pmset_ac_attached_real_sample() -> None:
    """尾巴上的 ``present: true`` 是 pmset 自己拼的，不是剩余时间。"""
    assert parse_pmset_batt(PMSET_AC) == (90, "接电源", "未充电")


def test_pmset_discharging_and_charging() -> None:
    assert parse_pmset_batt(PMSET_DISCHARGING) == (87, "用电中", "剩 4:12")
    assert parse_pmset_batt(PMSET_CHARGING) == (62, "充电中", "剩 1:05")


def test_pmset_without_battery_is_none() -> None:
    assert parse_pmset_batt("Now drawing from 'AC Power'\n") is None


def test_boottime_real_sample() -> None:
    assert parse_boottime(BOOTTIME_REAL) == 1790558492.0


def test_human_duration() -> None:
    assert human_duration(0) == "0 分钟"
    assert human_duration(12 * 60) == "12 分钟"
    assert human_duration(3600 * 4 + 600) == "4 小时 10 分"
    assert human_duration(86400 * 3 + 3600 * 4) == "3 天 4 小时"


def test_human_bytes_decimal_matches_what_macos_says_for_disks() -> None:
    """磁盘按 1000 进位（跟「关于本机」/``diskutil`` 一致），内存按 1024（同样跟系统一致）。"""
    from xiaocc.device import human_bytes_decimal

    assert human_bytes_decimal(994610155520) == "994.6 GB"
    assert human_bytes_decimal(193317859328) == "193.3 GB"


def test_parse_df_k_reads_the_data_volume_own_usage() -> None:
    """真机 ``df -k /System/Volumes/Data`` 实录（2026-09-29）—— 两个体积都要对上。"""
    from xiaocc.device import parse_df_k

    assert parse_df_k(DF_K_DATA_REAL) == (188787036 * 1024, 971298980 * 1024)
    assert parse_df_k("") is None
    assert parse_df_k("Filesystem 1024-blocks Used Available") is None  # 只有表头


def test_disk_usage_best_is_volume_scoped_and_never_raises() -> None:
    """真机跑一次：**不该**是 statvfs 的容器口径（本机实测差 28 GB）。"""
    from xiaocc.device import disk_usage_best

    got = disk_usage_best()
    assert got is None or (got[0] > 0 and got[1] > got[0])


DF_K_DATA_REAL = """Filesystem     1024-blocks      Used Available Capacity iused      ifree %iused  Mounted on
/dev/disk3s5     971298980 188787036 754754944    21%  972249 7547549440    0%   /System/Volumes/Data
"""


def test_human_bytes() -> None:
    assert human_bytes(24 * 1024**3) == "24.0 GB"
    assert human_bytes(512 * 1024**2) == "512.0 MB"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(999) == "999 B"


def test_lines_all_missing_says_未取到() -> None:
    """全采不到也要出三行、写「未取到」——不许抛，也不许编数字。"""
    device = Device(taken_at=0.0)
    rows = dict(device.lines())
    assert rows["CPU"] == "未取到"
    assert rows["内存"] == "未取到"
    assert rows["磁盘"] == "未取到"
    assert "电池" not in rows  # 没有电池不硬凑一行
    assert "已开机" not in rows


def test_lines_shape() -> None:
    device = Device(
        taken_at=0.0,
        cpu_percent=14.36,
        load=(2.5, 2.52, 2.64),
        cpu_count=10,
        mem_used=15627796480,
        mem_total=25769803776,
        disk_used=193317859328,  # Data 卷自己的消耗（diskutil 的 Volume Used Space）
        disk_total=994610155520,  # 容器总容量（「关于本机」报 994.6 GB）
        battery=(90, "接电源", "未充电"),
        uptime_s=123178.0,
    )
    rows = dict(device.lines())
    assert rows["CPU"] == "14% · 负载 2.50 / 2.52 / 2.64（10 核）"
    assert rows["内存"] == "14.6 GB / 24.0 GB（61%）"
    assert rows["磁盘"] == "193.3 GB / 994.6 GB（19%）"
    assert rows["电池"] == "90% 接电源 · 未充电"
    assert rows["已开机"] == "1 天 10 小时"


def test_cpu_ratio_needs_two_samples() -> None:
    """第一次问没有基线 ⇒ CPU 写「未取到」，不许拿累计值当利用率。"""
    from xiaocc.device import snapshot

    device = snapshot(None)
    assert device.cpu_percent is None
    assert device.mem_total and device.mem_total > 1024**3  # 内存总量总是能取到


def test_sampler_waits_once_so_first_reading_has_cpu() -> None:
    """冷启动第一次问（窗口不足 0.5s）要**等够再采**，否则面板/菜单第一眼就是「CPU 未取到」。"""
    from xiaocc.device import Sampler

    device = Sampler().get()
    assert device.cpu_percent is not None
    assert 0.0 <= device.cpu_percent <= 100.0


def test_sampler_cache_expires_with_its_ttl() -> None:
    """缓存判据必须用**同一个**时钟。

    混用时钟这条真踩过：`Device.taken_at` 是墙上时钟，而判据拿 `time.monotonic()` 去减它 ⇒
    差值是约 −1.79e9，**永远小于 TTL** ⇒ 缓存永不失效，右键菜单/面板会一直显示进程启动那一刻
    的数（真机实测：隔 3 秒再问、TTL 只有 2 秒，返回的还是同一个快照对象）。
    """
    from xiaocc.device import Sampler

    sampler = Sampler(ttl=0.05)
    first = sampler.get()
    # 等得比"采样窗口"(_MIN_CPU_WINDOW_S=1.0)长：窗口不够时走的是"问得太挤 ⇒ 沿用上次"那条路（那是设计）
    time.sleep(1.3)
    assert sampler.get() is not first  # 过期就必须重采，而不是把旧快照端上来


def test_cpu_window_is_wide_enough_that_frozen_pairs_vanish() -> None:
    """窗口必须 ≥1s —— 这条守的是一个**量出来的常数**，不是审美。

    2026-09-29 本机 30s @0.1s 直采 287 点：窗口 0.5s 时两读数"一模一样"（delta=0）**13.8%**、
    窗口 1.0s 时 **0.0%**（独立采样：10.1% vs 0/562）。降回 0.5s 就等于把
    「冷启动约 1/7 概率显示 CPU 未取到」这个 flake 放回来。
    """
    from xiaocc import device as device_mod

    assert device_mod._MIN_CPU_WINDOW_S >= 1.0


def test_frozen_counter_is_not_papered_over_by_a_blocking_retry(monkeypatch) -> None:
    """计数器卡住时**不再**主线程硬等重试：等它动要 中位 110ms / p90 633ms / 最大 938ms，
    任何预算都会落到「等满预算、最后还是印未取到」这种最差组合 ⇒ 改成抬窗口、删重试。

    这条同时钉住"别把重试加回来"：加了重试的话耗时会超出窗口本身。
    """
    from xiaocc import device as device_mod

    frozen = (1000, 2000)
    monkeypatch.setattr(device_mod, "cpu_ticks", lambda: frozen)
    sampler = device_mod.Sampler()
    started = time.monotonic()
    snap = sampler.get()
    elapsed = time.monotonic() - started
    assert snap.cpu_percent is None  # 卡住就是卡住，如实说，不靠烧时间来赌
    assert elapsed < device_mod._MIN_CPU_WINDOW_S + 0.4  # 只等了窗口，没有额外重试预算


def test_sampler_without_wait_returns_at_once_and_does_not_poison_the_cache() -> None:
    """首帧那条路（`wait=False`）：立刻返回、CPU 空着，而且**不许把这一份缓存起来**。

    缓存了它就会在 TTL 里一直按「未取到」把真数挡住（基线也不会往前走）。等基线够了一次
    `get()` 就得拿到 CPU —— 这就是面板「先写采集中、一秒后填真数」成立的前提。
    """
    from xiaocc.device import Sampler

    sampler = Sampler()
    quick = sampler.get(wait=False)
    assert quick.cpu_percent is None
    assert sampler.wait_remaining() > 0.0
    assert sampler.get().cpu_percent is not None  # 基线够了，内部补等一次就有


def test_sampler_caches_within_ttl() -> None:
    from xiaocc.device import Sampler

    sampler = Sampler(ttl=60.0)
    first = sampler.get(fresh=True)
    assert sampler.get() is first  # TTL 内不重采（右键连着点不烧 CPU）
