"""每轮任务结束后的 12~20 秒，桌面会不会闪一次「没有任何状态源在线」？

这是撰稿人 + 研究员各自用真库复现出来的 bug：`_DONE_WINDOW = 20.0` 比协议里
`STATE_TTL[State.DONE] = 12.0` 宽 8 秒，而 done 事件当时用的是 `at=last_ts`（末条消息时间），
于是末条消息年龄落在 12~20 秒时，源老实报 done、`pick()` 却按 TTL 判它过期丢掉，
引擎兜底合成 offline。

本脚本**拷真实 state.db** 到临时文件，改末条消息的时间戳，走真 `HermesSource` + 真 `Engine`
（含真角色包），把「源返回什么 / 屏幕显示什么」逐年龄打出来。不是读代码推断，是跑出来的。

    .venv/bin/python scripts/verify_done_window.py            # 默认扫 5/13/18/25
    .venv/bin/python scripts/verify_done_window.py --ages 5,13,18,20,21,30
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from xiaocc import characters
from xiaocc.engine import Engine
from xiaocc.sources.hermes import HermesSource, find_state_dbs


#: 判据（不写死边界秒数，免得窗口一动就要改测试）：
#:   ① 源说了什么，屏幕就得演什么 —— 源报 done 而屏幕是 offline，就是这次这个 bug；
#:   ② 源还在说话时，屏幕**永远**不许是 offline（offline 的含义是「没有任何状态源在线」）。
def verdict(source_state: str, screen: str) -> bool:
    if screen == "offline":
        return source_state == "(沉默)"
    if source_state == "done":
        return screen == "done"
    return True


def _copy_db() -> Path:
    dbs = find_state_dbs()
    if not dbs:
        raise SystemExit("本机没找到 Hermes state.db，这个脚本要在有真实库的机器上跑")
    tmp = Path(tempfile.mkdtemp(prefix="cc-done-window-")) / "state.db"
    shutil.copy2(dbs[0], tmp)
    print(f"真库：{dbs[0]}\n临时副本：{tmp}")
    return tmp


def _shift_last_message(db: Path, age: float) -> None:
    """把「最近活动」的那个会话与它最后一条消息的时间戳改到 now - age。"""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT id FROM sessions WHERE archived = 0 OR archived IS NULL"
            " ORDER BY last_activity_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise SystemExit("临时库里没有可用会话")
        session_id = row[0]
        # 只改「最近活动」那一条消息的时间：拷来的真库里可能有我在跑的这一轮，
        # 留着别的消息会让源报 working，测不出 done 窗口。
        conn.execute("UPDATE messages SET role = 'assistant', tool_name = NULL,"
                     " finish_reason = NULL, timestamp = ? WHERE session_id = ?",
                     (time.time() - age, session_id))
        conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                     (time.time() - age, session_id))
        # 清掉 turn lease：有活跃租约时源会直接报 working（正在跑这一轮），
        # 那是另一条分支；本脚本要测的是「末条消息多旧算 done」。
        conn.execute("DELETE FROM session_turn_leases")
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ages", default="5,13,18,25", help="末条消息年龄（秒），逗号分隔")
    ap.add_argument("--character", default=None, help="角色包目录，默认内置")
    args = ap.parse_args()

    db = _copy_db()
    character = characters.load_character(args.character)
    failures = []
    print(f"\n{'末条消息年龄':<12}{'源返回':<12}{'屏幕显示':<12}判定")
    for age in (float(a) for a in args.ages.split(",")):
        _shift_last_message(db, age)
        source = HermesSource(db=db)
        engine = Engine([source], character)
        event = source.poll()
        frame = engine.tick()
        source_state = event.state.value if event is not None else "(沉默)"
        screen = frame.state.value
        detail = frame.event.detail or ""
        ok = verdict(source_state, screen)
        mark = "✅" if ok else "❌"
        if not ok:
            failures.append(f"age={age}s 屏幕={screen}（{detail}）")
        print(f"{age:<12g}{source_state:<12}{screen:<12}{mark} {detail}")

    if failures:
        print("\n不通过：" + "；".join(failures))
        return 1
    print("\n全部通过：窗口内演 done、窗口外不再演 done，全程没有假的 offline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
