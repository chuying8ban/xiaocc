#!/bin/zsh
# 小cc CPU 看门狗 —— 与屏幕状态无关的**运行时**保护（@lead 定：优先级高于门禁美化）
#
# 为什么不能只靠 `arm` 的门禁：门禁是**一次性**部署检查，而且判据②在屏幕睡着/锁屏时根本判读不了。
# 「人不在机器前、面板偷偷烧一整天」只能靠周期性采样来防。
#
# 谁拉起它：LaunchAgent `ai.hermes.xiaocc.watchdog`（StartInterval=300，每 5 分钟一次，常驻加载）
# 它做什么：量一次面板的**稳态** CPU（窗口默认 10s，两次 `ps -o time=` 做差，与屏幕状态无关），
#           **连续 3 次 > 8%** ⇒ 用 xiaoccctl stop 把面板停掉 + 写日志 + 落一份留痕文件。
# 面板没在跑：清零连续计数、静默退出（看门狗自己每次只跑几秒，不常驻）。
#
# 阈值可用环境变量覆盖（**只给测试用**，部署值写在下面）：
#   XIAOCC_WD_THRESHOLD=5 XIAOCC_WD_STREAK=2 XIAOCC_WD_WINDOW=3 ops/xiaocc_watchdog.sh
#
# 文件：
#   ~/Library/Logs/xiaocc/watchdog.log      每次采样的结论（>200KB 覆盖式轮转成 .1）
#   ~/Library/Logs/xiaocc/watchdog.json     当前状态：连续次数、最近几次采样、阈值
#   ~/Library/Logs/xiaocc/watchdog-stop.json 只在「真的动手停掉面板」时写，留痕用
# 回滚：`launchctl bootout gui/$(id -u)/ai.hermes.xiaocc.watchdog`

emulate -L zsh
set -u

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
LABEL="ai.hermes.xiaocc"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/ai.hermes.xiaocc.watchdog.plist"
LOGDIR="$HOME/Library/Logs/xiaocc"
WLOG="$LOGDIR/watchdog.log"
STATE="$LOGDIR/watchdog.json"
MARK="$LOGDIR/watchdog-stop.json"
PY="$PROJECT_DIR/.venv/bin/python"
CTL="$SCRIPT_DIR/xiaoccctl"

THRESHOLD="${XIAOCC_WD_THRESHOLD:-8}"
STREAK_LIMIT="${XIAOCC_WD_STREAK:-3}"
WINDOW="${XIAOCC_WD_WINDOW:-10}"

mkdir -p "$LOGDIR" 2>/dev/null
if [[ -f "$WLOG" ]] && [[ $(wc -c <"$WLOG" 2>/dev/null || print 0) -gt 200000 ]]; then
  mv -f "$WLOG" "$WLOG.1"
fi

say() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$WLOG"; }

_pid() { launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3; exit}'; }

# 稳态 CPU（百分比字符串）：两次 ps -o time= 做差；与屏幕状态无关，这也是它能兜底的原因。
_cpu_pct() {  # _cpu_pct <pid> <秒>
  local pid="$1" secs="${2:-10}" a b
  a=$(ps -o time= -p "$pid" 2>/dev/null | tr -d ' ')
  [[ -n "$a" ]] || { print "nan"; return 0 }
  sleep "$secs"
  b=$(ps -o time= -p "$pid" 2>/dev/null | tr -d ' ')
  "$PY" - "$a" "$b" "$secs" <<'PYEOF' 2>/dev/null || print "nan"
import sys
def secs(t):
    p = [float(x) for x in t.split(':')]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]
try:
    a, b, n = secs(sys.argv[1]), secs(sys.argv[2]), float(sys.argv[3])
except Exception:
    print("nan"); raise SystemExit(0)
print(f"{100 * (b - a) / n:.1f}")
PYEOF
}

_write_state() {  # _write_state <streak> <cpu> <空串|停掉的原因>
  "$PY" - "$STATE" "$LABEL" "${1:-0}" "${2:-nan}" "${3:-}" "$THRESHOLD" "$STREAK_LIMIT" "$WINDOW" "$(_pid)" <<'PYEOF' 2>/dev/null
import json, sys, time, pathlib
state, label, streak, cpu, note, thr, limit, window, pid = sys.argv[1:10]
path = pathlib.Path(state)
try:
    doc = json.loads(path.read_text())
except Exception:
    doc = {}
doc.update({
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "label": label,
    "streak": int(streak),
    "cpu": cpu,
    "threshold_pct": float(thr),
    "streak_limit": int(limit),
    "window_s": float(window),
    "pid": int(pid) if pid.strip().isdigit() else None,
    "note": note.strip() or None,
})
doc["samples"] = (doc.get("samples") or [])[-9:] + [{"ts": doc["ts"], "cpu": cpu, "streak": doc["streak"], "pid": doc["pid"]}]
# 原子写：同目录唯一临时名 + os.replace（直写在中途被杀/并发时留半截 JSON）
import os as _os, tempfile as _tf
_fd, _tn = _tf.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
with _os.fdopen(_fd, "w") as _fh:
    _fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
_os.replace(_tn, path)
PYEOF
}

PID="$(_pid)"
if [[ -z "$PID" ]]; then
  # 日志卫生：面板被人有意停着（常态）时不要每 5 分钟写一行噪音 —— 只在「上一轮还在跑 / 还有连续计数」时记一笔
  PREV=$("$PY" -c "import json,sys;d=json.load(open(sys.argv[1])) if __import__('os').path.exists(sys.argv[1]) else {};print(d.get('pid') or 0)" "$STATE" 2>/dev/null || print 0)
  PREV_STREAK=$("$PY" -c "import json,sys;import os;d=json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {};print(int(d.get('streak') or 0))" "$STATE" 2>/dev/null || print 0)
  [[ "$PREV" != "0" || "$PREV_STREAK" != "0" ]] && say "面板没在跑 ⇒ 计数清零（看门狗什么都不做）"
  _write_state 0 "nan" "面板没在跑，计数清零"
  exit 0
fi

CPU="$(_cpu_pct "$PID" "$WINDOW")"
STREAK=0
if [[ -f "$STATE" ]]; then
  STREAK=$("$PY" -c "import json,sys;print(int(json.load(open(sys.argv[1])).get('streak') or 0))" "$STATE" 2>/dev/null || print 0)
fi
if [[ "$CPU" == "nan" ]]; then
  say "量不到 CPU（pid=$PID 可能刚退出）⇒ 本轮不计"
  _write_state "$STREAK" "nan" "量不到 CPU"
  exit 0
fi

OVER=$("$PY" -c "print(1 if float('${CPU}') > float('${THRESHOLD}') else 0)" 2>/dev/null || print 0)
if (( OVER == 1 )); then
  STREAK=$(( STREAK + 1 ))
  say "采样：pid=$PID CPU=${CPU}% 超阈值(${THRESHOLD}%) ⇒ 连续第 ${STREAK}/${STREAK_LIMIT} 次"
else
  [[ $STREAK -gt 0 ]] && say "采样：pid=$PID CPU=${CPU}% 回到阈值内 ⇒ 连续计数清零（原 ${STREAK} 次）"
  STREAK=0
  _write_state 0 "$CPU" ""
  exit 0
fi

_write_state "$STREAK" "$CPU" ""

if (( STREAK >= STREAK_LIMIT )); then
  say "连续 ${STREAK} 次超 ${THRESHOLD}% ⇒ 停掉面板（@lead 定的运行时保护）"
  "$PY" - "$MARK" "$PID" "$CPU" "$STREAK" "$THRESHOLD" "$WINDOW" <<'PYEOF' 2>/dev/null
import json, sys, time, pathlib
mark, pid, cpu, streak, thr, window = sys.argv[1:7]
pathlib.Path(mark).write_text(json.dumps({
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "action": "watchdog-stop",
    "reason": f"连续 {streak} 次 CPU > {thr}%（每次 {window}s 稳态窗口）",
    "last_cpu_pct": cpu,
    "pid": int(pid) if pid.strip().isdigit() else None,
    "rearm": "修好后 ops/xiaoccctl arm；看门狗不自动重装面板",
}, ensure_ascii=False, indent=2) + "\n")
PYEOF
  if [[ -x "$CTL" ]]; then
    "$CTL" stop >>"$WLOG" 2>&1
  else
    launchctl bootout "$DOMAIN/$LABEL" >>"$WLOG" 2>&1
  fi
  say "已停（留痕 $MARK）"
  _write_state "$STREAK" "$CPU" "看门狗已停掉面板"
  exit 0
fi

exit 0
