"""每帧那笔钱花在哪个 draw 调用上 —— 先量再拆，别猜。

做法：进程内起真窗口（appkit），把 `_draw_view` 里的每个 `_draw_*` 调用套上计时器
（在实例上打补丁，不动源码），跑几秒后按「调用次数 × 单次耗时」排序。
用法：XIAOCC_ANCHOR_FILE=/tmp/x.json XIAOCC_PROBE_FILE=/tmp/p.json .venv/bin/python scripts/draw_phases.py [秒]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("XIAOCC_ANCHOR_FILE", "/tmp/xiaocc_draw_phases_anchor.json")
os.environ.setdefault("XIAOCC_PROBE_FILE", "/tmp/xiaocc_draw_phases_probe.json")

from xiaocc import characters  # noqa: E402
from xiaocc.backends import appkit as ak  # noqa: E402
from xiaocc.engine import Render  # noqa: E402
from xiaocc.protocol import State, StatusEvent  # noqa: E402

WATCHED = ("_draw_rig", "_draw_face", "_draw_art", "_draw_caption", "_draw_handle", "_apply_pose")


def install_timers(backend):
    stats: dict[str, list[float]] = {name: [0.0, 0.0] for name in WATCHED}  # [秒, 次数]

    def wrap(name, fn):
        def timed(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                s = stats[name]
                s[0] += time.perf_counter() - t0
                s[1] += 1

        return timed

    for name in WATCHED:
        fn = getattr(backend, name, None)
        if fn is not None:
            setattr(backend, name, wrap(name, fn))
    return stats


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    character = characters.load_character()  # 内置角色包
    backend = ak.AppKitBackend()
    stats = install_timers(backend)

    event = StatusEvent(source="draw_phases", state=State.WORKING, detail="正在执行 patch",
                        project="xiaocc")
    backend.render(Render(event=event, character=character))
    backend.linger(seconds)

    t0 = time.perf_counter()
    try:
        backend._pump(seconds)  # noqa: SLF001 —— 直接跑主循环，量的是真实绘制成本
    finally:
        backend.close()
    wall = time.perf_counter() - t0

    print(f"墙钟 {wall:.2f}s")
    print(f"{'调用':<14}{'次数':>8}{'总计ms':>10}{'单次µs':>10}{'占墙钟':>9}")
    for name, (total, count) in sorted(stats.items(), key=lambda kv: -kv[1][0]):
        if count == 0:
            continue
        print(f"{name:<14}{int(count):>8}{total * 1000:>10.1f}{total / count * 1e6:>10.1f}"
              f"{total / wall * 100:>8.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
