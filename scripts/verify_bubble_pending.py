"""验「刚启动就单击」这一路：气泡先写「CPU 采集中」，然后又自己补成真数。

为什么要单独守它（@ops 指出的）：真用户**够不到**这一路 —— 启动到首次交互最短 6s（全天 10 次样本），
而 CPU 窗口只有 1s；但**回归/部署脚本会在重启后 1 秒内点**（同一个采样窗口）⇒ 那一趟的字面也必须对：
「采集中」不是「未取到」（后者才是用户以为坏了的假数）。

判据：构造采样器后 1s 内单击 ⇒ ① 气泡里 CPU 那格是「采集中」，且其余四行当刻就是真数；
② 等基线攒够，还是那枚气泡（TTL 不重置）**自己换成真数**。

沙箱：临时 anchor/probe/settings + **额度副本**，跑完 close()，绝不碰用户 ~/.xiaocc。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import verify_log

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

SANDBOX = Path(tempfile.mkdtemp(prefix="xiaocc-pending-"))
COPY = Path(tempfile.gettempdir()) / "xiaocc-quota-copy.json"
shutil.copy(Path.home() / ".xiaocc" / "quota.json", COPY)
os.environ["XIAOCC_ANCHOR_FILE"] = str(SANDBOX / "anchor.json")
os.environ["XIAOCC_PROBE_FILE"] = str(SANDBOX / "probe.json")
os.environ["XIAOCC_SETTINGS_FILE"] = str(SANDBOX / "settings.json")
os.environ["XIAOCC_QUOTA_FILE"] = str(COPY)

from xiaocc.backends import window_layout as wl
from xiaocc.backends.appkit import AppKitBackend
from xiaocc.characters import load_character
from xiaocc.engine import Render
from xiaocc.protocol import State, StatusEvent


def main() -> int:
    cursor = [wl.Point(-1000.0, -1000.0)]
    backend = AppKitBackend(cursor=lambda: cursor[0])
    try:
        character = load_character()
        backend.render(
            Render(event=StatusEvent(source="demo", state=State.IDLE), character=character)
        )
        # 立刻切到「设备状态」档并点 —— 采样器构造到现在还不到 1s，CPU 窗口没攒够
        (SANDBOX / "settings.json").write_text(
            json.dumps({"schema": 1, "click_action": "device"}, ensure_ascii=False),
            encoding="utf-8",
        )
        backend._reload_settings(force=True)
        remaining = backend._device.wait_remaining()
        print(f"窗口还差 {remaining:.2f}s 才够 ⇒ pending={remaining > 0.0}")
        backend._do_click_action()
        first = backend._badge_text
        backend.linger(0.05)
        print(f"点击那一刻的气泡 = {first!r}")
        backend.linger(1.4)
        later = backend._badge_text
        print(f"1.4s 后的气泡   = {later!r}")
        ok = "采集中" in first and "%" in later and "采集中" not in later
        print(f"\n结果：{'PASS' if ok else 'FAIL'}")
        verify_log.record(
            "verify_bubble_pending",
            0 if ok else 1,
            criteria={"cpu_window_remaining_s": round(remaining, 2)},
        )
        return 0 if ok else 1
    finally:
        backend.close()
        print(f"沙箱 {SANDBOX} 已收")


if __name__ == "__main__":
    raise SystemExit(main())
