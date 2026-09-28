#!/bin/zsh
# 小cc macOS 显示层启动器
#
# 谁启动它：LaunchAgent ~/Library/LaunchAgents/ai.hermes.xiaocc.plist
#           （ProgramArguments = /bin/zsh <本脚本> top-right 15，RunAtLoad，
#            KeepAlive.SuccessfulExit=false 只在异常退出时拉起）。
#           参数：$1=停靠点（默认 top-right），$2=帧率（默认 15 —— 待机 12~15 帧是 @lead 批准的设计参数，
#           部署路径实测 15 帧 4.9% / 30 帧 9.0%，圈速与配置自洽、不是被节流）。
# 如何停止：launchctl bootout gui/$(id -u)/ai.hermes.xiaocc
#           bootout 之后 launchd 不再托管，也不会自动重启。
# 日志去向：stdout / stderr 由 plist 的 StandardOutPath / StandardErrorPath
#           捕获到 ~/Library/Logs/xiaocc/panel.log 与 panel.err.log，
#           因此本脚本内部不写任何日志、不做重试。
# 休眠断言：宠物本身绝不持有 sleep assertion（IOPMAssertion / caffeinate 等），
#           该职责由独立的 guard 进程负责。
#
# 用 exec 直接替换本进程，launchd 监视的 PID 就是宠物进程本身，没有 wrapper 父进程。

emulate -L zsh

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"

PATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin"
export PATH
cd "$PROJECT_DIR" || exit 78

ENTRY="$PROJECT_DIR/.venv/bin/xiaocc"
if [[ ! -x "$ENTRY" ]]; then
  print -u2 -- "xiaocc: venv 入口不存在：$ENTRY"
  exit 78
fi

CORNER="${1:-top-right}"
FPS="${2:-15}"          # @lead 允许待机 12~15 帧；15 帧在部署路径上实测 4.9%（30 帧是 9.0%）

exec "$ENTRY" run --source hermes -b appkit --backend-opt "at=$CORNER" --backend-opt "fps=$FPS"
