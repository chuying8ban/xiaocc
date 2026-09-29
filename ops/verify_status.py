#!/usr/bin/env python3
"""门禁留痕的**消费侧**规则（生产侧在 scripts/verify_log.py；策略不写进记录器）。

读 `~/Library/Logs/xiaocc/verify.log`（一行一 JSON）与 `verify/<gate>.json`（最新态），
按**分档**新鲜度给出"这条绿能不能引用"，而不是一律"上次绿就当绿"。

分档理由（实测）：`drag_inject` 只能在「未锁 + 空闲 ≥5s」跑，锁屏的机器可能几天拿不到
新记录 ⇒ 给它一个统一的 24h 有效期，任何"过期不许发布"的消费方都会永久卡住，最后
被关掉（今晚已经出现过一次"守不住就被关掉"的形状）。所以：
  * 随时能跑的门禁：默认 24h 有效。
  * drag_inject 等"环境受限"型：不设硬过期；`rc=2 + blocker 含锁` 记「已知陈旧（环境受限）」，
    与「未跑」严格区分；它上一次真判定的年龄单独报出来。

退出码：0 = 每条有据（新鲜绿 / 环境受限陈旧 / 陈旧但明示）；1 = 存在需要人看的项
（未跑、超期、force 强跑）。**这个脚本只报告，不替谁拦发布** —— 判据是给人读的。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

FRESH_H_DEFAULT = 24.0           # 随时能跑的门禁
FRESH_H_STALE_NOTE = 72.0        # 环境受限型超过这个年龄要在报告里显式标"陈旧"
ENV_LIMITED = {"verify_drag_inject"}   # 需要「未锁 + 空闲」窗口才能跑的门禁


def repo_gates() -> list[str]:
    """仓库里现有的门禁脚本名 = 发布时"该有哪些门禁"的清单。

    默认只列"有记录的门禁"会把**从没跑过的**藏起来（看不见 ⇒ 容易被当成不存在），
    所以默认清单取"脚本存在 ∪ 有记录"，让 `未跑` 显式上屏。
    """
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    # verify_log 是**记录器本身**（门禁往里写），不是门禁 —— 列进去会多出一条永远"未跑"的噪音。
    return sorted(p.stem for p in scripts.glob("verify_*.py") if p.stem != "verify_log")


def logdir() -> Path:
    env = os.environ.get("XIAOCC_VERIFY_LOGDIR")
    return Path(env) if env else Path.home() / "Library/Logs/xiaocc"


def load_records(d: Path) -> dict[str, dict]:
    """最新态：verify/<gate>.json 优先；没有就取 verify.log 里该 gate 的最后一行。"""
    recs: dict[str, dict] = {}
    log = d / "verify.log"
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("gate"):
                recs[r["gate"]] = r          # 后写覆盖先写
    for p in sorted((d / "verify").glob("*.json")) if (d / "verify").is_dir() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(r, dict):
            r.setdefault("gate", p.stem)
            recs[r["gate"]] = r
    return recs


def age_h(rec: dict, now: float | None = None) -> float | None:
    at = rec.get("at")
    if isinstance(at, (int, float)):
        return ((now or time.time()) - at) / 3600.0
    if isinstance(at, str):
        try:
            dt = datetime.fromisoformat(at)      # py3.11+ 原生吃 "…Z"
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return ((now or time.time()) - dt.timestamp()) / 3600.0
        except ValueError:
            return None
    return None


def classify(gate: str, rec: dict | None, now: float | None = None) -> tuple[str, str]:
    """→ (状态, 说明)。状态是给人读的词，不假装成布尔。"""
    if rec is None:
        return "未跑", "没有记录（盘上没记录 = 未知，不是绿）"
    if rec.get("force"):
        return "强制跑", f"force=true ⇒ 不当真绿（rc={rec.get('rc')}）"
    rc = rec.get("rc")
    a = age_h(rec, now)
    age_s = "年龄未知" if a is None else f"{a:.1f}h 前"
    blocker = str(rec.get("blocker") or "")
    if gate in ENV_LIMITED and rc == 2:
        return "环境受限（非未跑）", f"跑了但被环境挡下：{blocker or '未写明原因'}（{age_s}）"
    if a is not None and a > (FRESH_H_STALE_NOTE if gate in ENV_LIMITED else FRESH_H_DEFAULT):
        word = "陈旧（环境受限型，无硬过期）" if gate in ENV_LIMITED else "超期"
        return word, f"最后记录 {age_s}，rc={rc}"
    ok = "绿" if rc == 0 else ("红" if rc == 1 else f"rc={rc}")
    # 证据必须自带"对着哪棵树跑的"：dirty=True 的绿是"对着未提交工作树跑的绿"，
    # 谁把它当发布绿灯就是引用错了对象（下一批提交后要重跑才有"对着提交的绿"）。
    tree = rec.get("rev") or "?"
    if rec.get("dirty"):
        tree += "+未提交"
    return f"新鲜·{ok}", f"{age_s}，rc={rc}，树={tree}" + (f"，blocker={blocker}" if blocker else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="门禁留痕的消费侧视图（只报告，不拦发布）")
    ap.add_argument("--gates", nargs="*", default=None, help="只看这几支门禁")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    d = logdir()
    recs = load_records(d)
    gates = args.gates or sorted(set(recs) | set(repo_gates()) or set(recs))
    now = time.time()
    rows = [(g, *classify(g, recs.get(g), now)) for g in gates]

    if args.json:
        print(json.dumps({"logdir": str(d), "rows": [
            {"gate": g, "state": s, "note": n} for g, s, n in rows]}, ensure_ascii=False))
    else:
        print(f"留痕目录: {d}（{len(recs)} 支门禁有记录）")
        if not rows:
            print("  没有任何门禁记录 ⇒ 全部读作「未跑（未知）」，不是绿")
            return 1
        for g, s, n in rows:
            print(f"  {g:<24} {s:<26} {n}")
    need_eye = [r for r in rows if r[1] in ("未跑", "超期", "强制跑") or r[1].startswith("陈旧（环境受限型")]
    return 1 if need_eye else 0


if __name__ == "__main__":
    sys.exit(main())
