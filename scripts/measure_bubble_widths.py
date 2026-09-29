"""气泡字面的**真字体宽度回归**：逐条量「气泡里会写什么」，谁超宽就点出来。

为什么单独一支笔：气泡每行可用宽**真机是 132px**（窗口 160 − 左右内边距 6×2 − 文字内缩 8×2），
而这个数**从应用那条算法算**（`appkit.bubble_budget_for`），不在笔里另写一个——笔曾经自己写 148，
比应用宽 16px：133~148px 的候选被它判"放得下"，真机上其实选不上。字面是按"放得下"挑的——
一旦某条候选超宽，要么它永远选不上（白写），要么在 160px 窗口里被截成半句话。两者的共同点是
**肉眼在代码里看不出来**，必须拿真字体量。

量的是**所有档位、所有候选**（额度 / 设备 / 全部 / 不显示），数据取真的 `~/.xiaocc/quota.json`
和真的设备快照（取不到就是取不到，正好把「未取到」那些形态一起量掉）。

用法::

    .venv/bin/python scripts/measure_bubble_widths.py           # 打表
    .venv/bin/python scripts/measure_bubble_widths.py --strict   # 任一档没有放得下的候选就退出码 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from AppKit import NSFont, NSFontAttributeName, NSString
from Foundation import NSDictionary

from xiaocc import device as device_mod
from xiaocc.backends.appkit import bubble_budget_for
from xiaocc.characters import load_character
from xiaocc.quota import load
from xiaocc.quota.badge import ACTIONS, bubble_candidates

#: 气泡每行可用宽 —— **从应用自己的算法算**（窗口宽按角色推），别在笔里另写一个数：
#: 2026-09-29 @writer 抓到笔写 148、而应用画字还有 8px 内缩 ⇒ 真机只有 132，笔会把 133~148px
#: 的候选判成「放得下」。改内边距/内缩时两边**必然**一起动。
LINE_MAX_PT = bubble_budget_for(load_character())
FONT_SIZE = 11.0


def width(text: str) -> float:
    font = NSFont.systemFontOfSize_(FONT_SIZE)
    attrs = NSDictionary.dictionaryWithObject_forKey_(font, NSFontAttributeName)
    return float(NSString.stringWithString_(text).sizeWithAttributes_(attrs)[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="任一档没有放得下的候选就以 1 退出")
    ap.add_argument("--file", type=Path, default=None, help="quota.json 路径")
    args = ap.parse_args()

    report, meta = load(args.file) if args.file else load()
    snap = device_mod.Sampler().get()
    print(f"-- quota.json: exists={meta.get('exists')} age={meta.get('age_s')}s stale={meta.get('stale')} --")
    print(f"-- 设备快照: {[f'{k}={v}' for k, v in snap.lines()]} --")
    print(f"-- 每行可用宽 {LINE_MAX_PT:.0f}px（窗口宽 − 气泡左右内边距 6×2 − 文字内缩 8×2，同一处算法）--")

    failures: list[str] = []
    for action in (*ACTIONS, "caption"):
        cands = bubble_candidates(action, report=report, meta=meta, device=snap)
        print(f"\n== {action} ==  {len(cands)} 条候选")
        fits = False
        for idx, lines in enumerate(cands, 1):
            widths = [width(line) for line in lines]
            ok = all(w <= LINE_MAX_PT for w in widths) and 1 <= len(lines) <= 2
            fits = fits or ok
            mark = "✓" if ok else "✗"
            shown = " ／ ".join(f"「{line}」{w:.1f}" for line, w in zip(lines, widths))
            print(f"   {mark} #{idx}({len(lines)} 行) {shown}")
        if not cands:
            print("   （这一档不显示：没有候选）")
            continue
        if not fits:
            failures.append(action)
            print("   ✗ 这一档没有任何候选放得下 —— 真机上必然被截")

    print()
    if failures:
        print(f"结果：FAIL（{len(failures)} 档没得选）—— " + "、".join(failures))
        return 1
    print("结果：PASS（每一档都至少有一条候选放得下）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
