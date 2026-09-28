# ops/ —— 小cc 在本机的运维层（macOS）

这一层只干一件事：让用户**不看着屏幕**也知道小cc 活着、能启停、能回滚。
面板本身的行为（贴边、动作、形象）在 `src/xiaocc/backends/`，不在这里。

## 1. 装在哪 / 文件在哪

| 东西 | 位置 | 谁维护 |
| --- | --- | --- |
| LaunchAgent（自启+守护） | `~/Library/LaunchAgents/ai.hermes.xiaocc.plist` ← 仓库原件 `ops/ai.hermes.xiaocc.plist` | 本目录，`xiaoccctl start` 会覆盖式同步 |
| 启动器 | `ops/run-panel.sh <角落>`，只做「找出 venv 入口 → `exec`」，不做日志、不重试 | 本目录 |
| 控制入口 | `ops/xiaoccctl {status\|start\|stop\|restart\|doctor\|logs [err]}` | 本目录 |
| 面板日志 | `~/Library/Logs/xiaocc/panel.log`、`panel.err.log`（各自 >200KB 覆盖式轮转成 `.1`） | launchd |
| 本脚本日志 | `~/Library/Logs/xiaocc/xiaoccctl.log` | `xiaoccctl` |
| **状态落盘** | `~/Library/Logs/xiaocc/state.json`（ts / 是否加载 / pid / 停靠点 / 窗口矩形 / last_error） | `xiaoccctl` |
| 睡眠闸守卫 | `~/Library/LaunchAgents/ai.hermes.mascot-pet.keepawake.plist`（原件 `ops/keepawake.plist`） | 见 §3 |

**人不在机器前时先看这两个文件**：`~/Library/Logs/xiaocc/state.json`（一行就能判断活没活）与 `ops/xiaoccctl doctor`（9 项自检，含「窗口是否真的在屏幕上」）。

## 2. 日常操作

```sh
ops/xiaoccctl status      # 运行中退出 0，未运行退出 1
ops/xiaoccctl start       # 幂等；顺带补起睡眠闸守卫
ops/xiaoccctl stop        # bootout；本来没跑也返回 0
ops/xiaoccctl restart
ops/xiaoccctl doctor      # 全绿退出 0，有 ✗ 退出 4
ops/xiaoccctl logs err    # 看 stderr 末尾 40 行
```

退出码：`0` 正常 / `1` 未运行 / `2` 用法错 / `3` 没装 / `4` 操作或自检失败。
**全程静默**：没有 osascript、没有弹窗，双击 `.command` 也不会跳窗。

## 3. 睡眠闸（为什么小cc 要管机器睡不睡）

这台机器 `pmset: sleep=1` —— 空闲 1 分钟就睡。旧桌宠小汐当年就是因为「她的 NSActivity 断言随进程死掉」
而在机器入睡后表现为「消失 + 极卡」，于是有了独立的守卫脚本 `~/.hermes/scripts/xiaoxi_sleep_guard.sh`：
只要被守护的 launchd job 还在，就持有 `caffeinate -i`（接电源一直持有；电池低于 15% 放行，回升到 17% 再持有）。
小cc 自己也**不持有任何断言**（`run-panel.sh` 里刻意没有），持有者是守卫，这样重启面板不会丢闸。

两个必须知道的机制（都实测过，写在这里免得后人再踩）：

* **守卫会自己退出**：它每轮 `launchctl print` 被守护的 label，看到不在就 `exit 0`（设计如此，
  「桌宠关掉 ⇒ 机器可以正常睡」）。小汐被 bootout 后它确实在 20 秒内退出了（`sleepguard.log` 有记录）。
* **`KeepAlive.OtherJobEnabled` 只管「启动」**：被依赖的 job 重新加载**不会**把守卫自动拉回来。
  所以 `xiaoccctl start` 每次都会检查守卫、不在就 `kickstart` 补起 —— 否则 `stop` 之后机器会
  在无人察觉时回到「空闲 1 分钟就睡」。

取代小汐时对这份 plist 只动了两处（Label 没改，避免牵连别处）：
`EnvironmentVariables.XIAOXI_PET_LABEL=ai.hermes.xiaocc`、`KeepAlive.OtherJobEnabled.ai.hermes.xiaocc=true`。

**一句话语义（别让下一个人以为 keepawake 坏了）**

* 小cc **在跑** ⇒ 守卫持有 `caffeinate -i` ⇒ 机器不因空闲而睡。
* 小cc **被 bootout**（`xiaoccctl stop`，或临时掐掉）⇒ 守卫在 ≤20 秒内**自己退出并释放断言** ⇒
  **机器恢复「空闲 1 分钟就睡」，这是设计，不是故障** —— 守卫存在的全部理由就是「桌宠在，机器就别睡」。
* 之后 `start` 时守卫**不会**被 launchd 自动拉回（见上一条机制），所以 `xiaoccctl start` 每次都会
  `kickstart` 它，别绕过脚本手工 `bootstrap` 面板。
* 实测留痕（2026-09-29）：`sleepguard.log` 里 `00:41:31 小汐（ai.hermes.xiaocc）已关掉 ⇒ 守卫退出，机器可以照常睡`——
  括号里是 label 实参，脚本里的字面文案还写着「小汐」（`~/.hermes/scripts/xiaoxi_sleep_guard.sh` 是用户资产，我没改）。
  判断「闸门在不在」永远看 `pmset -g assertions | grep PreventUserIdleSystemSleep` 和守卫日志，**不看脚本名字**。

## 4. 回滚：30 秒内恢复旧桌宠小汐

```sh
ops/xiaoccctl stop
launchctl bootout "gui/$(id -u)/ai.hermes.mascot-pet.keepawake"        # 停守卫（可选）
launchctl bootout "gui/$(id -u)/ai.hermes.xiaocc" 2>/dev/null          # 已停就忽略报错
cp ops/baseline/ai.hermes.mascot-pet.keepawake.plist.bak ~/Library/LaunchAgents/ai.hermes.mascot-pet.keepawake.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ai.hermes.mascot-pet.keepawake.plist
cp ops/baseline/ai.hermes.mascot-pet.plist.bak ~/Library/LaunchAgents/ai.hermes.mascot-pet.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ai.hermes.mascot-pet.plist
```

停用小汐时**没有删任何东西**：`~/mascot-pet/` 原样保留，plist 只是改名成
`ai.hermes.mascot-pet.plist.disabled`，`ai.hermes.keepawake.plist.disabled`（另一路 keepawake）没碰。

## 5. 取代小汐时留下的基线证据

`ops/baseline/` 是动手前的原件（三个 plist）与 `baseline.txt`（当时 `launchctl list` / 进程 / 断言快照）。
`ops/xiaoccctl uninstall` 不在计划里 —— 要拆就按 §4 反着来，一步可逆。

## 6. arm（装回去）的门槛，与 ops 的测量方法

@lead 定的门槛：`doctor` 全绿 + 启动后 60 秒稳态 `ps` 里该进程 **< 5%** + 位置留痕就位；不达标不许装回去。

**怎么量**（冷启动那十几秒不算，两次取样做差，窗口 ≥60 秒）：

```sh
ops/xiaoccctl start && sleep 15
PID=$(launchctl print "gui/$(id -u)/ai.hermes.xiaocc" | awk '$1=="pid"{print $3; exit}')
A=$(ps -o time= -p "$PID"); sleep 60; B=$(ps -o time= -p "$PID")   # time= 超过一分钟会变成 MM:SS.ss 以上格式，两种都要会解析
```

⚠️ **必须量 launchd 启动的那一份，别量手工跑的**。plist 里 `ProcessType=Interactive` 是让 33ms 定时器按点触发的
（动画不卡的原因），代价是每一拍的开销都要真付。2026-09-29 实测**同一份代码**：
手工前台实例 **4.2%**、launchd（Interactive）实例 **15.2%**（6×10s 分段恒定 15.0~15.6%），旧桌宠小汐同规格自报 **4.6~5%**。
所以「4.3% 通过」这类结论必须写清是哪种启动方式量的，否则不是同一个数。

**当前记录（2026-09-29）**：CPU 忙等已修（99.9% → 手工前台 4.2%），但 **launchd 路径 15.2% 未达门槛，
面板处于停止状态**（`42b97c8`、`ops/findings-2026-09-29.md`）；位置语义已独立复跑
`scripts/verify_anchor.py` **6/6**（含「外部位移 → 回锚 + 日志留痕」）。

**跑任何自检/测试都要设 `XIAOCC_ANCHOR_FILE=<临时路径>`**：不设就会往真实锚点 `~/.xiaocc/anchor.json` 写假坐标，
面板一 arm 就落在屏幕中间（codex 自测已犯过一次，留了个 (635,334) 的假锚点）。

`doctor` 的第 ⑩ 项会读锚点文件跟实测窗口矩形对一下（Δ≤6px 算一致）并打出最近一行位移留痕 ——
**只提示、不判失败**：拖拽搬家、贴边收起都是合法态，别把交互误报成故障。
