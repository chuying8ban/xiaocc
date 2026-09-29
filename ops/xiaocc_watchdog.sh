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
# 全部可被环境变量接管 —— 给自己留一条**沙箱测试缝**：测「停面板」那条分支时不该动真面板、
# 不该往真留痕里写，也不该搅真 streak。
WLOG="${XIAOCC_WD_LOG:-$LOGDIR/watchdog.log}"
STATE="${XIAOCC_WD_STATE:-$LOGDIR/watchdog.json}"
MARK="${XIAOCC_WD_MARK:-$LOGDIR/watchdog-stop.json}"
PROBE="${XIAOCC_WD_PROBE:-$HOME/.xiaocc/probe.json}"
PY="$PROJECT_DIR/.venv/bin/python"
CTL="${XIAOCC_WD_CTL:-$SCRIPT_DIR/xiaoccctl}"
#: 变量名**不能叫 MATCH**：zsh 里 ``$MATCH`` 是 ``[[ s =~ re ]]`` 的语义变量（存匹配到的那截），
#: 本脚本后面的 ``[[ "$QAGE" =~ ^[0-9]+$ ]]`` 会把它覆盖成匹配到的数字（实测被吃成 "67"），
#: 于是「屏上有几只」变成 ``pgrep -f 67`` —— 静默匹配一堆无关进程。同类危险的还有 MBEGIN/MEND/status/pipestatus。
INSTANCE_PATTERN="${XIAOCC_WD_MATCH:-([.]venv/bin/xiaocc|-m xiaocc[.]cli) run}"
PANEL_ERR="${XIAOCC_WD_PANEL_ERR:-$LOGDIR/panel.err.log}"
PANEL_LOG="${XIAOCC_WD_PANEL_LOG:-$LOGDIR/panel.log}"
PANEL_CAP="${XIAOCC_WD_PANEL_CAP:-200000}"
QUOTA="${XIAOCC_WD_QUOTA_FILE:-$HOME/.xiaocc/quota.json}"
QUOTA_FLAG="${XIAOCC_WD_QUOTA_FLAG:-$LOGDIR/quota-stale.flag}"
#: 45 分钟 = 采集器连漏三拍（15 分钟/拍）；采集器自己的保鲜期是 30 分钟
QUOTA_MAX_AGE="${XIAOCC_WD_QUOTA_MAX:-2700}"

THRESHOLD="${XIAOCC_WD_THRESHOLD:-8}"
STREAK_LIMIT="${XIAOCC_WD_STREAK:-3}"
WINDOW="${XIAOCC_WD_WINDOW:-10}"

mkdir -p "$LOGDIR" 2>/dev/null
if [[ -f "$WLOG" ]] && [[ $(wc -c <"$WLOG" 2>/dev/null || print 0) -gt 200000 ]]; then
  mv -f "$WLOG" "$WLOG.1"   # 本脚本自己的日志：每次 say() 都是新 fd，mv 安全
fi

# 面板日志轮转：**不能 mv**。launchd 的 StandardOutPath/StandardErrorPath 是 O_APPEND 打开的
# （可抛标签实测：就地截断后新写入落在文件开头、无 NUL 空洞），而面板进程一直持有那个 fd ——
# mv 只搬名字，app 之后会继续写进 .1，活日志永远停在轮转那一刻。所以：先 cp 到 .1，再就地 : > 截断。
# 代价：cp 与截断之间写进的几行会丢（面板日志是诊断材料，接受）。
for _p in "$PANEL_ERR" "$PANEL_LOG" "$LOGDIR/quota.out.log" "$LOGDIR/quota.err.log"; do
  [[ -f "$_p" ]] || continue
  _sz=$(wc -c <"$_p" 2>/dev/null | tr -d ' ')
  if [[ -n "${_sz:-}" ]] && (( _sz > PANEL_CAP )); then
    if cp -f "$_p" "$_p.1" 2>/dev/null; then
      : >"$_p"
      say "面板日志轮转：$(basename "$_p") → $(basename "$_p").1（${_sz} 字节）"
    fi
  fi
done

say() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$WLOG"; }

_pid() { launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3; exit}'; }

# 前置①：屏上必须**只有一个**实例。手工 `--linger` 起的臂对 launchctl 完全隐形（实测：
# 一个臂白烧 3.5h / 9.6%，看门狗一行都没记），所以这里认 pgrep 的实数；不是 1 就整轮不计，
# 免得把别人的 CPU 记到受管实例头上、或者反过来为别人的白烧停掉用户的面板。
_instances() {  # 模式走参数，函数体不引用任何全局名（少一个被覆盖的机会）
  { pgrep -f "$1" 2>/dev/null || true; } | grep -c .
}

# 前置②：拖动中一律不计。B 把拖拽圈速抬到 ~250 圈/s，按住那一两秒 CPU 会冲到 14~17%。
# 判据用探针的 `dragging` 字段（`anchor_state="drag"` 亦可）—— 用户**按住不动**也是合法拖动，
# 所以不能拿「多久没收到拖动事件」当超时判据。
_dragging() {
  "$PY" - "$PROBE" <<'PYEOF' 2>/dev/null || print "unknown"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("unknown"); raise SystemExit(0)
print("yes" if (d.get("dragging") is True or d.get("anchor_state") == "drag") else "no")
PYEOF
}

# 面板自己报的状态（idle / thinking / working …）：display 解释不了的 CPU 波动多半在这条轴上，
# 所以每个样本都带着它，事后能按 display × state 交叉看，而不是拿两种状态下的小样本互相打脸。
_state_of_panel() {
  "$PY" - "$PROBE" <<'PYEOF' 2>/dev/null || print "?"
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("state") or "?")
except Exception:
    print("?")
PYEOF
}

# 每次采样都记显示状态：屏睡/锁屏与屏醒未锁的 CPU 读数**不可比**（同一条面板在两种状态下的
# 读数能差 1~2 个百分点），留痕里必须能分辨，否则以后没人说得清某个数是在哪种状态下取的。
_disp() {
  # 口径必须和 ops/xiaoccctl 的 _display_state 逐字一致（睡着+锁屏 / 睡着 / 锁屏 / 醒着）——
  # 两个写者用不同词表，事后没人能把看门狗样本和门禁留痕放在一张表里比。
  "$PY" - <<'PYEOF' 2>/dev/null || print "unknown"
import Quartz
asleep = bool(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))
try:
    locked = bool((Quartz.CGSessionCopyCurrentDictionary() or {}).get("CGSSessionScreenIsLocked"))
except Exception:
    locked = False
if asleep and locked:
    print("睡着+锁屏")
elif asleep:
    print("睡着")
elif locked:
    print("锁屏")
else:
    print("醒着")
PYEOF
}

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

_write_state() {  # _write_state <streak> <cpu> <空串|停掉的原因> [显示状态]
  "$PY" - "$STATE" "$LABEL" "${1:-0}" "${2:-nan}" "${3:-}" "$THRESHOLD" "$STREAK_LIMIT" "$WINDOW" "$(_pid)" "${4:-}" "$(_state_of_panel)" <<'PYEOF' 2>/dev/null
import json, sys, time, pathlib
state, label, streak, cpu, note, thr, limit, window, pid, disp, pstate = sys.argv[1:12]
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
    "display": disp.strip() or None,
    "panel_state": pstate.strip() or None,
    "note": note.strip() or None,
})
doc["samples"] = (doc.get("samples") or [])[-9:] + [
    {
        "ts": doc["ts"],
        "cpu": cpu,
        "streak": doc["streak"],
        "pid": doc["pid"],
        "display": disp.strip() or None,
        "panel_state": pstate.strip() or None,
    }
]
# 原子写：同目录唯一临时名 + os.replace（直写在中途被杀/并发时留半截 JSON）
import os as _os, tempfile as _tf
_fd, _tn = _tf.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
with _os.fdopen(_fd, "w") as _fh:
    _fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
_os.replace(_tn, path)
PYEOF
}

# —— 额度采集的健康检查（与面板相互独立，所以放在面板的两个前置**之前**）——
# 只在状态切换时写一行日志：陈旧 → 记一次 + 落 flag；恢复新鲜 → 删 flag + 记一行。
# 为什么值得放在这里：采集器是独立作业，它挂了不会有任何进程消失、也不会有非零退出被谁看见，
# 唯一的症状就是 quota.json 停止变新 —— 而它正好就是面板拿来显示「剩余」的那份文件。
if [[ -f "$QUOTA" ]]; then
  QAGE=$("$PY" -c "import os,sys,time;print(int(time.time()-os.path.getmtime(sys.argv[1])))" "$QUOTA" 2>/dev/null || print -1)
  if [[ "$QAGE" =~ ^[0-9]+$ ]] && (( QAGE > QUOTA_MAX_AGE )); then
    if [[ ! -f "$QUOTA_FLAG" ]]; then
      say "额度文件已陈旧 ${QAGE}s（>${QUOTA_MAX_AGE}s ⇒ 采集器连漏三拍）⇒ 面板只会显示「陈旧」；查 ai.hermes.xiaocc.quota 作业与 $LOGDIR/quota.err.log"
      printf '%s\n' "$QAGE" >"$QUOTA_FLAG" 2>/dev/null
    fi
  elif [[ -f "$QUOTA_FLAG" ]]; then
    rm -f "$QUOTA_FLAG" 2>/dev/null
    say "额度恢复新鲜（${QAGE}s 前）⇒ 陈旧标记清除"
  fi
fi

PID="$(_pid)"
DISP="$(_disp)"
STREAK=0
if [[ -f "$STATE" ]]; then
  STREAK=$("$PY" -c "import json,sys;print(int(json.load(open(sys.argv[1])).get('streak') or 0))" "$STATE" 2>/dev/null || print 0)
fi
if [[ -z "$PID" ]]; then
  # 日志卫生：面板被人有意停着（常态）时不要每 5 分钟写一行噪音 —— 只在「上一轮还在跑 / 还有连续计数」时记一笔
  PREV=$("$PY" -c "import json,sys;d=json.load(open(sys.argv[1])) if __import__('os').path.exists(sys.argv[1]) else {};print(d.get('pid') or 0)" "$STATE" 2>/dev/null || print 0)
  [[ "$PREV" != "0" || "$STREAK" != "0" ]] && say "面板没在跑 ⇒ 计数清零（看门狗什么都不做）"
  _write_state 0 "nan" "面板没在跑，计数清零" "$DISP"
  exit 0
fi

# 前置①：只认**单实例**（pgrep 实数）。不是 1 ⇒ 整轮不计：既不为别人的白烧停掉用户的面板，
# 也不把别人的 CPU 记到受管实例头上。（手工 --linger 的臂对 launchctl 隐形，只有 pgrep 看得见。）
INSTANCES="$(_instances "$INSTANCE_PATTERN")"
if [[ "$INSTANCES" != "1" ]]; then
  PREV_NOTE=$("$PY" -c "import json,os,sys;p=sys.argv[1];print((json.load(open(p)).get('note') or '') if os.path.exists(p) else '')" "$STATE" 2>/dev/null || print "")
  [[ "$PREV_NOTE" != *实例* ]] && say "屏上有 ${INSTANCES} 个实例（受管 pid=$PID）⇒ 本轮不计（只认单实例）"
  _write_state "$STREAK" "nan" "屏上 ${INSTANCES} 个实例，本轮不计" "$DISP"
  exit 0
fi

# 前置②：拖动中不计 —— 拖拽态 CPU 14~17% 是设计带宽，不是白烧。按笔记只在状态切换时写一行。
if [[ "$(_dragging)" == "yes" ]]; then
  PREV_NOTE=$("$PY" -c "import json,os,sys;p=sys.argv[1];print((json.load(open(p)).get('note') or '') if os.path.exists(p) else '')" "$STATE" 2>/dev/null || print "")
  [[ "$PREV_NOTE" != *拖动中* ]] && say "拖动中（探针 dragging=true）⇒ 本轮不计（拖拽态 14~17% 属设计带宽）"
  _write_state "$STREAK" "nan" "拖动中，本轮不计" "$DISP"
  exit 0
fi

CPU="$(_cpu_pct "$PID" "$WINDOW")"
if [[ "$CPU" == "nan" ]]; then
  say "量不到 CPU（pid=$PID 可能刚退出）⇒ 本轮不计"
  _write_state "$STREAK" "nan" "量不到 CPU" "$DISP"
  exit 0
fi

OVER=$("$PY" -c "print(1 if float('${CPU}') > float('${THRESHOLD}') else 0)" 2>/dev/null || print 0)
if (( OVER == 1 )); then
  STREAK=$(( STREAK + 1 ))
  say "采样：pid=$PID CPU=${CPU}% 超阈值(${THRESHOLD}%) ⇒ 连续第 ${STREAK}/${STREAK_LIMIT} 次（显示=$DISP）"
else
  [[ $STREAK -gt 0 ]] && say "采样：pid=$PID CPU=${CPU}% 回到阈值内 ⇒ 连续计数清零（原 ${STREAK} 次，显示=$DISP）"
  STREAK=0
  _write_state 0 "$CPU" "" "$DISP"
  exit 0
fi

_write_state "$STREAK" "$CPU" "" "$DISP"

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
