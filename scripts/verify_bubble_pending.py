"""验「刚启动就单击」这一路：气泡先写「CPU 采集中」，半秒后自己补成真数。

@ops 说的正是这一路：真用户够不到（启动→首次交互最短 6s），但**回归脚本会在重启后 1 秒内点**。
沙箱实例（临时 anchor/probe/settings + 真 quota 副本），跑完 close()，不碰用户 ~/.xiaocc。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path.home() / "ChenC/项目/xiaocc"
sys.path.insert(0, str(REPO / "src"))

SANDBOX = Path(tempfile.mkdtemp(prefix="xiaocc-pending-"))
shutil.copy(Path.home() / ".xiaocc/quota.json", Path("/tmp/xiaocc-quota-copy.json"))
os.environ["XIAOCC_ANCHOR_FILE"] = str(SANDBOX / "anchor.json")
os.environ["XIAOCC_PROBE_FILE"] = str(SANDBOX / "probe.json")
os.environ["XIAOCC_SETTINGS_FILE"] = str(SANDBOX / "settings.json")
os.environ["XIAOCC_QUOTA_FILE"] = "/tmp/xiaocc-quota-copy.json"

from xiaocc.backends.appkit import AppKitBackend  # noqa: E402
from xiaocc.backends import window_layout as wl  # noqa: E402
from xiaocc.characters import load_character  # noqa: E402
from xiaocc.engine import Render  # noqa: E402
from xiaocc.protocol import State, StatusEvent  # noqa: E402


def main() -> int:
    cursor = [wl.Point(-1000.0, -1000.0)]
    backend = AppKitBackend(cursor=lambda: cursor[0])
    try:
        character = load_character()
        backend.render(Render(event=StatusEvent(source="demo", state=State.IDLE), character=character))
        # 立刻点（构造采样器到现在还不到 1s ⇒ CPU 窗口没攒够）
        (SANDBOX / "settings.json").write_text(
            json.dumps({"schema": 1, "click_action": "device"}, ensure_ascii=False), encoding="utf-8"
        )
        backend._reload_settings(force=True)
        print("窗口还差 %.2fs 才够 ⇒ pending=%s" % (
            backend._device.wait_remaining(), backend._device.wait_remaining() > 0.0
        ))
        backend._do_click_action()
        first = backend._badge_text
        backend.linger(0.05)
        print("点击那一刻的气泡 =", repr(first))
        backend.linger(1.4)
        print("1.4s 后的气泡   =", repr(backend._badge_text))
        ok = ("采集中" in first) and ("%" in backend._badge_text) and ("采集中" not in backend._badge_text)
        print("\n结果：", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        backend.close()
        print(f"沙箱 {SANDBOX} 已收")


if __name__ == "__main__":
    raise SystemExit(main())
