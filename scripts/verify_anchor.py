#!/usr/bin/env python
"""验证「位置语义」真的生效：拖动=搬家落盘、其余位移=回锚留痕、贴边不算搬家。

    .venv/bin/python scripts/verify_anchor.py

为什么要有这个脚本：光有代码和日志说明不了行为。运维实测过一个反例 ——
启动瞬间窗口在 ``1324,96``，跑到约 3 分钟后外部抓到稳定的 ``1225,100``（左偏 99px）
且自己弹不回来，而位置变化**不留痕**，他在机器外根本判断不了那是正常交互还是卡住。
所以这里把四种位置语义都跑一遍，每条都从**窗口服务器/磁盘/日志**这三个外部视角取证据：

1. 没锚点文件时：启动位置＝默认右上角（不读磁盘也得稳）
2. 拖动到中间：落盘 + 内存锚点都换 + ``anchor_state == "anchor"``
3. 重启（新实例、同一个锚点文件）：按上次拖到的位置启动
4. 命令行给了 ``at=``：听命令行的，**不覆写**用户那份锚点
5. 外部位移（绕过显示层直接改 window frame）：自检抓回来 + 日志留痕
6. 拖到边上贴边收起：**不算搬家**，锚点文件不许变

``XIAOCC_ANCHOR_FILE`` 把锚点指到临时目录，跑完不留脏数据（不会碰用户真实的 ``~/.xiaocc/``）。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xiaocc import characters
from xiaocc.backends import anchor_store
from xiaocc.backends import window_layout as wl
from xiaocc.backends.appkit import AppKitBackend
from xiaocc.engine import Render
from xiaocc.protocol import State, StatusEvent

SCREEN = (1512.0, 982.0)
WIN = (160.0, 194.0)
DEFAULT_TOP_RIGHT = (SCREEN[0] - WIN[0] - 28.0, 96.0)
FAR_CURSOR = wl.Point(15.0, SCREEN[1] - 15.0)  # 离桌宠很远，别让悬停/贴边掺进来


class _LogSpy(logging.Handler):
    """把显示层的日志行抓下来 —— 留痕本身也是被验证的行为。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())  # getMessage() 已经套好参数了

    def seen(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)


def _frame(state: State, detail: str) -> Render:
    character = characters.load_character(None)
    return Render(
        event=StatusEvent(source="probe:verify-anchor", state=state, detail=detail),
        character=character,
    )


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="xiaocc-anchor-"))
    os.environ["XIAOCC_ANCHOR_FILE"] = str(tmpdir / "anchor.json")

    spy = _LogSpy()
    logging.getLogger("xiaocc").addHandler(spy)
    logging.getLogger("xiaocc").setLevel(logging.INFO)

    backend = AppKitBackend(cursor=lambda: FAR_CURSOR)
    frame = _frame(State.WORKING, "正在执行 read_file")
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")

    # ① 没有锚点文件 → 默认右上角
    backend.render(frame)
    probe = backend.probe()
    at_default = tuple(round(v) for v in backend._window_local.size) == (160, 194) and (
        abs(probe["anchor"][0] - DEFAULT_TOP_RIGHT[0]) < 0.6
        and abs(probe["anchor"][1] - DEFAULT_TOP_RIGHT[1]) < 0.6
    )
    check(
        "① 无锚点文件 → 默认右上角",
        at_default and probe["anchor_state"] == "anchor" and probe["anchor_ok"] is True,
        f"锚点 {probe['anchor']} · 期望 {list(DEFAULT_TOP_RIGHT)} · {probe['anchor_state']}",
    )

    # ② 拖动到中间 → 落盘 + 锚点换新
    backend.start_drag()
    for step in range(1, 13):                       # 多段平滑位移，别一段大跳
        backend.move_window_to(
            backend._window_local.x + (600.0 - DEFAULT_TOP_RIGHT[0]) / 12,
            backend._window_local.y + (420.0 - DEFAULT_TOP_RIGHT[1]) / 12,
        )
        backend._pump(0.02)
    edge = backend.end_drag()
    backend._pump(0.3)
    saved = anchor_store.load_anchor()
    probe = backend.probe()
    check(
        "② 拖动 → 锚点落盘（内存+磁盘一起换）",
        edge is wl.Edge.NONE
        and saved is not None
        and abs(saved[0] - probe["anchor"][0]) < 1.0
        and abs(saved[1] - probe["anchor"][1]) < 1.0
        and probe["anchor_state"] == "anchor"
        and spy.seen("锚点已更新（用户拖动）"),
        f"落盘 {saved} · 内存锚点 {probe['anchor']} · {probe['anchor_state']} · 日志留痕="
        f"{spy.seen('锚点已更新（用户拖动）')}",
    )
    dragged_anchor = tuple(probe["anchor"])
    backend.close()

    # ③ 重启 → 按上次拖到的位置启动
    restarted = AppKitBackend(cursor=lambda: FAR_CURSOR)
    restarted.render(frame)
    probe2 = restarted.probe()
    check(
        "③ 重启后按上次拖动的位置启动",
        abs(probe2["anchor"][0] - dragged_anchor[0]) < 0.6
        and abs(probe2["anchor"][1] - dragged_anchor[1]) < 0.6
        and spy.seen("按上次拖动的锚点启动"),
        f"新实例锚点 {probe2['anchor']} · 上次 {list(dragged_anchor)}",
    )

    # ④ 外部位移（绕过显示层改 window frame）→ 自检抓回 + 留痕
    spy.lines.clear()
    space = restarted._space()
    bogus = wl.Rect(dragged_anchor[0] - 99.0, dragged_anchor[1] + 4.0, *WIN)
    restarted._window.setFrame_display_(space.to_ns_rect(bogus), True)   # 故意绕开收口点
    restarted._window_local = bogus                                      # 假装窗口「记账」也乱了
    moved_really = abs(restarted._window.frame().origin.x - space.to_ns_rect(bogus).origin.x) < 1.0
    if not moved_really:                     # 位移没落地的话，后面的断言会假通过
        check("④ 外部位移 → 回锚 + 日志留痕", False, "外部位移没生效（setFrame 没动窗口）")
    else:
        for _ in range(6):                   # 自检每秒一次：分片地推进，别指望一次长 pump
            restarted._pump(0.5)
        now_ns = restarted._window.frame().origin
        want_ns = space.to_ns_rect(restarted._anchor).origin
        back = abs(now_ns.x - want_ns.x) < 1.0 and abs(now_ns.y - want_ns.y) < 1.0
        check(
            "④ 外部位移 → 回锚 + 日志留痕",
            back and spy.seen("检测到窗口漂移（非拖动）"),
            f"外层看到窗口 {'已回锚' if back else f'仍漂在 {now_ns}'} · "
            f"漂移留痕={spy.seen('检测到窗口漂移（非拖动）')} · "
            f"日志最后一行={spy.lines[-1][:70] if spy.lines else '(无)'}",
        )
    restarted.close()

    # ⑤ 命令行 at= 优先，且不覆写用户那份锚点
    explicit = AppKitBackend(at="top-left", cursor=lambda: FAR_CURSOR)
    explicit.render(frame)
    probe4 = explicit.probe()
    still_saved = anchor_store.load_anchor()
    check(
        "⑤ at= 优先且不覆写磁盘锚点",
        abs(probe4["anchor"][0] - 28.0) < 0.6
        and abs(probe4["anchor"][1] - 96.0) < 0.6
        and still_saved is not None
        and abs(still_saved[0] - dragged_anchor[0]) < 1.0,
        f"本次锚点 {probe4['anchor']}（期望 [28,96]）· 磁盘那份仍是 {still_saved}",
    )

    # ⑥ 拖到边上贴边收起 → 不算搬家
    spy.lines.clear()
    explicit.start_drag()
    for step in range(1, 7):
        explicit.move_window_to(probe4["anchor"][0] - step * 4.0, probe4["anchor"][1] + 40.0)
        explicit._pump(0.02)
    edge2 = explicit.end_drag()
    explicit._pump(0.3)
    after = anchor_store.load_anchor()
    unchanged = (
        after is not None
        and still_saved is not None
        and abs(after[0] - still_saved[0]) < 0.5
        and abs(after[1] - still_saved[1]) < 0.5
    )
    check(
        "⑥ 贴边收起不算搬家（锚点文件不变）",
        edge2 is not wl.Edge.NONE and unchanged,
        f"落在 {edge2} · 磁盘锚点 {after} · 期望仍是 {still_saved}（未变={unchanged}）",
    )
    explicit.close()

    os.environ.pop("XIAOCC_ANCHOR_FILE", None)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"== {passed}/{len(results)} 通过 ==")
    print(f"（锚点临时目录 {tmpdir}，未触碰 ~/.xiaocc/）")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
