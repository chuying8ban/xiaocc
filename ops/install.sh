#!/bin/zsh
# 小cc LaunchAgent 安装器 —— 把 ops/templates/*.plist.in 渲染成**这台机器**的真 plist
#
# 为什么必须有这一步：仓库里存的是模板，不是能直接 `cp` 的 plist。launchd **不**对
#   ProgramArguments 做变量展开（scripts/release_check.py 判据 #2 的注释里是同一条结论），
#   所以 `$HOME` 这种占位写在 plist 里没用 —— 只能在安装那一刻把 @REPO_ROOT@ / @PYTHON@ /
#   @LOG_DIR@ / @HOME@ 换成绝对路径。别人 clone 下来跑这一条，装上的就是他机器的那一份。
#
# 怎么敲：
#   ops/install.sh                        # 渲染到 ~/Library/LaunchAgents
#   ops/install.sh --dry-run              # 只把渲染结果打到 stdout，一个字节都不写
#   ops/install.sh --dest DIR             # 渲染到别处（自测 / 沙箱用）
#   ops/install.sh --force                # 已加载的 job 也覆盖
#   ops/install.sh --repo-root DIR --python PATH --log-dir DIR
#
# 取值优先级：命令行参数 > 同名环境变量（REPO_ROOT / PYTHON / LOG_DIR）> 下面探测出的默认值。
#   解析完会把四个值都打出来 —— 环境变量是容易被别的东西顺手设掉的，看得见才查得出来。
#
# 它**不碰** launchd：不 bootstrap、不 bootout、不 kickstart、不 enable/disable。
#   要不要重载由你定，脚本最后把该敲的命令打出来（重载 = 面板会闪一下，这个决定不该替用户做）。
#
# 退出码：0 全部就位 / 2 用法错 / 3 缺模板或找不到 python / 4 有 job 已加载需要 --force，或写入失败

emulate -L zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
TEMPLATE_DIR="$SCRIPT_DIR/templates"
RENDER="$TEMPLATE_DIR/render.py"

usage() {
  sed -n '2,24p' "$SCRIPT_DIR/${0:t}" | sed 's/^# \{0,1\}//'
}

# 空串 = 没给；兜底放在参数解析**之后**（--repo-root 一改，PYTHON 的默认值得跟着改）
REPO_ROOT="${REPO_ROOT:-}"
PYTHON="${PYTHON:-}"
LOG_DIR="${LOG_DIR:-}"
DEST=""
DRY_RUN=false
FORCE=false

while (( $# )); do
  case "$1" in
    --dest)      [[ $# -ge 2 ]] || { print -u2 "install: --dest 缺参数"; exit 2 }; DEST="$2"; shift 2 ;;
    --dest=*)    DEST="${1#*=}"; shift ;;
    --repo-root) [[ $# -ge 2 ]] || { print -u2 "install: --repo-root 缺参数"; exit 2 }; REPO_ROOT="$2"; shift 2 ;;
    --repo-root=*) REPO_ROOT="${1#*=}"; shift ;;
    --python)    [[ $# -ge 2 ]] || { print -u2 "install: --python 缺参数"; exit 2 }; PYTHON="$2"; shift 2 ;;
    --python=*)  PYTHON="${1#*=}"; shift ;;
    --log-dir)   [[ $# -ge 2 ]] || { print -u2 "install: --log-dir 缺参数"; exit 2 }; LOG_DIR="$2"; shift 2 ;;
    --log-dir=*) LOG_DIR="${1#*=}"; shift ;;
    --dry-run)   DRY_RUN=true; shift ;;
    --force)     FORCE=true; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           print -u2 "install: 不认的参数：$1（--help 看用法）"; exit 2 ;;
  esac
done

: ${REPO_ROOT:=${SCRIPT_DIR:h}}
: ${PYTHON:=$REPO_ROOT/.venv/bin/python}
: ${LOG_DIR:=$HOME/Library/Logs/xiaocc}
: ${DEST:=$HOME/Library/LaunchAgents}

[[ -f "$RENDER" ]] || { print -u2 "install: 渲染器不存在：$RENDER"; exit 3 }
typeset -a TEMPLATES
TEMPLATES=("$TEMPLATE_DIR"/*.plist.in(N))
(( ${#TEMPLATES} )) || { print -u2 "install: 没找到模板：$TEMPLATE_DIR/*.plist.in"; exit 3 }

# 跑渲染器用哪个 python：venv 那个在就用它（和 quota plist 里写的同一个），
# 不在就退回系统 python3 —— **新 clone 的机器还没建 venv 也得能装**，别把自己锁死。
RENDER_PY=""
if [[ -x "$PYTHON" ]]; then
  RENDER_PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  RENDER_PY="$(command -v python3)"
elif [[ -x /usr/bin/python3 ]]; then
  RENDER_PY=/usr/bin/python3
else
  print -u2 "install: 找不到能用的 python3（渲染器跑不起来）"; exit 3
fi

typeset -a RARGS
RARGS=(--repo-root "$REPO_ROOT" --python "$PYTHON" --log-dir "$LOG_DIR" --home "$HOME")

print "install: 渲染参数（渲染器 $RENDER_PY）"
print "  REPO_ROOT = $REPO_ROOT"
print "  PYTHON    = $PYTHON"
print "  LOG_DIR   = $LOG_DIR"
print "  DEST      = $DEST"
$DRY_RUN && print "  --dry-run：只打印，不写任何文件、不建任何目录"

if ! [[ -x "$PYTHON" ]]; then
  print -u2 "install: ⚠ PYTHON 不存在或不可执行：$PYTHON"
  print -u2 "           ai.hermes.xiaocc.quota 那个 job 起来会立刻失败。先建 venv"
  print -u2 "           （python3 -m venv .venv && .venv/bin/pip install -e '.[macos]'），"
  print -u2 "           或者 --python 指一个真的解释器。其余 3 个 job 不受影响。"
fi

_job_loaded() {  # 只读探测：launchctl print。本脚本绝不 bootstrap/bootout/kickstart。
  launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1
}

if ! $DRY_RUN; then
  mkdir -p "$DEST" || { print -u2 "install: 建不出目标目录：$DEST"; exit 4 }
  # LOG_DIR 也得在：launchd 不会替你建目录，StandardOutPath 打不开日志 job 就起不来
  [[ -d "$LOG_DIR" ]] || mkdir -p "$LOG_DIR" || { print -u2 "install: 建不出日志目录：$LOG_DIR"; exit 4 }
fi

typeset -i wrote=0 same=0 refused=0 failed=0
typeset -a RELOADED=()

for tpl in "${TEMPLATES[@]}"; do
  base="${tpl:t}"                 # ai.hermes.xiaocc.quota.plist.in
  label="${base%.plist.in}"       # ai.hermes.xiaocc.quota
  dst="$DEST/$label.plist"

  if $DRY_RUN; then
    print ""
    print -r -- "--- $base → $dst ---"
    "$RENDER_PY" "$RENDER" "${RARGS[@]}" --stdout "$tpl"
    continue
  fi

  # 先问「已经一样了吗」：一样就完全不碰那个文件（不刷 mtime、不惊动 launchd）
  if "$RENDER_PY" "$RENDER" "${RARGS[@]}" --out "$dst" --diff "$tpl" >/dev/null 2>&1; then
    print "  = $label.plist 已一致，不动"
    same+=1
    continue
  fi

  if _job_loaded "$label" && ! $FORCE; then
    print -u2 "  ✗ $label 已加载，且安装件与渲染结果不一致 —— 不给 --force 不覆盖"
    print -u2 "    （覆盖只改磁盘上那份，launchd 里跑的还是旧配置；要生效必须 bootout + bootstrap）"
    refused+=1
    continue
  fi

  if "$RENDER_PY" "$RENDER" "${RARGS[@]}" --out "$dst" "$tpl"; then
    wrote+=1
    RELOADED+=("$label")
  else
    print -u2 "  ✗ $label 写入失败：$dst"
    failed+=1
  fi
done

$DRY_RUN && exit 0

print ""
print "install: 写入 $wrote 份，已一致 $same 份，拒绝 $refused 份，失败 $failed 份 → $DEST"
if (( ${#RELOADED} )); then
  print "改了这些 job 的 plist，要让 launchd 认新配置得重载（本脚本不代劳）："
  for label in "${RELOADED[@]}"; do
    print "  launchctl bootout   gui/\$(id -u)/$label            # 没加载会报 not loaded，忽略"
    print "  launchctl bootstrap gui/\$(id -u) $DEST/$label.plist"
  done
fi
if (( refused )); then
  print -u2 "install: 有 $refused 个 job 已加载，被拦下了 —— 确认可以覆盖就加 --force"
  exit 4
fi
(( failed )) && exit 4
exit 0
