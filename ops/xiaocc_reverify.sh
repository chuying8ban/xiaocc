#!/bin/zsh
# 「屏亮补验」一次性任务（@lead 定：做，但做成一次性，不是周期任务，别和看门狗叠噪音）
#
# 解决什么：`arm --skip-motion` 是在屏幕睡着、判据②物理上判不了时的**临时放行**，会留一个欠账标记
# （~/Library/Logs/xiaocc/skip-motion.json）。本脚本等屏幕醒着时把**完整门禁**补跑一次：
#   gate 通过   ⇒ 写 reverify-ok.json（含 外部像素 changed/max_diff_pct 的实测数字）+ 删掉欠账标记
#   gate 不通过 ⇒ 写 reverify-fail.json + **把面板停回去**（不达标不留）
#   gate 判不了（又睡了）⇒ 只记一笔，欠账保留、面板照常跑（不误停）
# 跑完就结束；job 是 RunAtLoad 的一次性 job，不会被周期性拉起（要再补验得重新 bootstrap）。
#
# 可调（环境变量，plist 里可覆盖）：
#   XIAOCC_RV_WAIT_MAX   最多等多久屏幕醒（秒，默认 7200 = 2 小时）
#   XIAOCC_RV_POLL       轮询间隔（秒，默认 10）
#   XIAOCC_RV_SECS       gate 的测量窗口（秒，默认 60）

emulate -L zsh
set -u

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
CTL="$SCRIPT_DIR/xiaoccctl"
PY="${XIAOCC_RV_PY:-$PROJECT_DIR/.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3          # 沙箱/别处跑时的兜底
LOGDIR="${XIAOCC_RV_LOGDIR:-$HOME/Library/Logs/xiaocc}"   # 测试接缝：沙箱里跑不碰真文件
RVLOG="$LOGDIR/reverify.log"
DEBT="$LOGDIR/skip-motion.json"
OKMARK="$LOGDIR/reverify-ok.json"
FAILMARK="$LOGDIR/reverify-fail.json"
WAIT_MAX="${XIAOCC_RV_WAIT_MAX:-7200}"
POLL="${XIAOCC_RV_POLL:-10}"
SECS="${XIAOCC_RV_SECS:-60}"

mkdir -p "$LOGDIR"
if [[ -f "$RVLOG" ]] && [[ $(wc -c <"$RVLOG" 2>/dev/null || print 0) -gt 100000 ]]; then
  mv -f "$RVLOG" "$RVLOG.1" 2>/dev/null || true
fi
say() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$RVLOG"; }

# 屏幕状态：直接问窗口服务器（比跑一遍 doctor 便宜得多 —— 这个循环可能转两个小时）。
# 锁屏但屏亮也算「醒着」：实测此时像素判据可用。XIAOCC_RV_FORCE_AWAKE 是**测试接缝**，
# 用来在屏幕睡着的机器上验证「通过/不通过/判不了」三条分支（生产不用它）。
_awake() {
  [[ "${XIAOCC_RV_FORCE_AWAKE:-0}" == "1" ]] && { print "醒着"; return 0; }
  "$PY" - <<'PYEOF' 2>/dev/null || print "未知"
import Quartz
print("睡着" if Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()) else "醒着")
PYEOF
}

say "补验任务启动：等屏幕亮（最多 ${WAIT_MAX}s，每 ${POLL}s 一眼，首次=$(_awake)）；欠账标记 $([[ -f "$DEBT" ]] && print '在' || print '不在')"

waited=0
verdict=""
disp=$(_awake)
while (( waited < WAIT_MAX )); do
  if [[ "$disp" == "醒着" ]]; then
    # 2) 跑完整 gate（不用 --skip-motion：就是要那条外部像素判据）
    out=$("$CTL" gate "$SECS" 2>&1)
    rc=$?
    motion=$(print -r -- "$out" | grep -o '连续帧差异 [0-9.]*%' | tail -1)
    cpu=$(print -r -- "$out" | grep -o 'A) CPU：[0-9.]*%' | tail -1)
    say "屏幕亮着 ⇒ gate rc=$rc ${cpu} ${motion}"
    if (( rc != 5 )); then
      verdict="$rc"
      break
    fi
    # rc=5 = 判不了（恰好又睡了／像素抓不到）：**继续等**，别一轮判不了就整趟放弃
    # （踩过：屏幕闪一下醒、等脚本走到 gate 时又睡了 → 整趟白跑，得手动重新 bootstrap）
    say "gate 判不了（rc=5，屏幕又睡了？）⇒ 接着等屏幕亮再试"
  fi
  sleep "$POLL"
  waited=$(( waited + POLL ))
  disp=$(_awake)
done
if [[ -z "$verdict" ]]; then
  say "等满 ${WAIT_MAX}s 仍没拿到真裁决（当前：${disp:-未知}）⇒ 本轮不补验；欠账保留、面板不动"
  exit 0
fi

case "$verdict" in
  0)
    "$PY" - "$OKMARK" "$DEBT" "$cpu" "$motion" <<'PYEOF'
import json, os, sys, time
ok, debt, cpu, motion = sys.argv[1:5]
json.dump({"ts": time.strftime("%FT%T%z"), "action": "reverify-ok", "gate": "pass",
           "cpu": cpu, "motion": motion, "debt_cleared": os.path.exists(debt)},
          open(ok, "w"), ensure_ascii=False, indent=2)
if os.path.exists(debt):
    os.remove(debt)
PYEOF
    say "★ 补验通过 ⇒ 欠账标记已清（$OKMARK）"
    ;;
  4)
    "$PY" - "$FAILMARK" "$cpu" "$motion" <<'PYEOF'
import json, sys, time
json.dump({"ts": time.strftime("%FT%T%z"), "action": "reverify-fail", "gate": "fail",
           "cpu": sys.argv[2], "motion": sys.argv[3],
           "rearm": "修好后 ops/xiaoccctl arm"},
          open(sys.argv[1], "w"), ensure_ascii=False, indent=2)
PYEOF
    say "✗ 补验不通过 ⇒ 把面板停回去（欠账标记保留，留痕 $FAILMARK）"
    "$CTL" stop >>"$RVLOG" 2>&1
    ;;
  *)
    say "补验判不了（gate rc=$rc，多半是屏幕又睡了）⇒ 欠账保留、面板照常跑；屏幕醒时重新 bootstrap 本任务即可"
    ;;
esac
exit 0
