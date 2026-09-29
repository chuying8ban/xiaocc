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
ops/xiaoccctl doctor      # 12 项自检，全绿退出 0，有 ✗ 退出 4
ops/xiaoccctl gate [秒]   # 门槛① CPU<5% ② 外部「画面真在动」；退出码 0 过 / 4 不过 / 5 判不了
ops/xiaoccctl arm [秒] [--skip-motion]   # 装回去：start+暖机+gate，不过或判不了都自动停回来
ops/xiaoccctl motion <pid>               # 单跑判据②（连抓窗口位图比像素），对照/排查用
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

@lead 定的门槛：`doctor` 全绿 + **CPU < 5% 且 圈速 ≈ fps**（@researcher 补的第二条，别让「便宜」其实是「被节流了」）
+ 位置留痕就位。**现在门槛是命令，不是口头约定**：

```sh
ops/xiaoccctl arm          # start → 暖机 15s → 量 60s → 过则留着，不过则自动 stop（rc=4）
ops/xiaoccctl gate 30      # 只量已经在跑的那一份，别动它
```

`arm` 的实测（2026-09-29 02:41，部署路径）：CPU **14.7%**（门槛 5%）但自证据**合格**
（`fps=30 圈速=27.8/s 上一圈睡=30.0ms 状态=working`、快照 0.1s 新、`在锚点=True`）⇒ 判**不通过**并**自动停回去**（`arm rc=4`，job 卸载、无进程）。
这条区分很关键：**它没在被节流**（圈速 27.8 ≈ fps 30，上圈真睡了 30ms，不是忙等），烧的是**每帧绘制**的钱。

### 判据②：「画面真在动」怎么从外面判（@lead 定，2026-09-29 换的口径）

原口径是「圈速 ≈ fps」，@lead 指出它有盲区：**动画若交给合成器做，进程本就该睡着**，拿圈速判会冤枉一条更好的实现。
所以判据②改成外部可观测的像素变化：连抓 3 张该 pid **主窗口**的位图（间隔 0.7s，窗口按「屏幕范围内面积最大」挑），
连续两帧原始字节差异 > 0.05% 即「在动」。另留一条**反忙等护栏**：圈速 > 2×fps 也判不过（别用 CPU 数字掩盖空转）。

```sh
ops/xiaoccctl motion <pid>     # 单独跑像素判据（对照/排查用）
ops/xiaoccctl gate 30          # 判据① + 判据② 一起判，退出码：0 过 / 4 不过 / 5 判不了
```

**屏幕睡着时判据②可能判不了**（屏睡时 CoreAnimation 停画、别的进程窗口也可能不在屏上）：这时 gate 退出 **5**、
`arm` **停回去但不算故障**，并提示「屏幕醒着时重跑」。**但「锁屏」不等于判不了** —— 实测锁屏但屏亮时
面板窗口照旧被正常抓到（1.4 秒内 22.6~28.4% 字节在变），所以 gate 是**先抓、抓到什么算什么**，
只有「真抓不到」或「屏睡下的静止」才算不可判读（避免把屏睡停画误判成故障）。
只有明确写 `ops/xiaoccctl arm 60 --skip-motion` 才会「只验 CPU」放行，且会在 `xiaoccctl.log` 里留痕。
（判不了时宁可说「证据不全」、也不凭半个证据把面板装上桌面 —— 但别把「锁屏」当成判不了，见上。）

**已在部署路径上过闸（2026-09-29 03:1x）**：`arm 60` → `A) CPU 3.8% <5%` / `B) 画面在动（1.4 秒内 22.6% 字节在变）` /
自证据 `fps=15 圈速=13.9/s 关系=anchor 在锚点=True` ⇒ 通过，面板留在运行状态，`doctor` 12 项全绿。
帧率改由 plist 第 3 个参数给（`run-panel.sh <停靠点> <帧率>`，当前 `top-right 15`）：15 帧 3.8~4.9%、30 帧 9.0%、点阵化前 16.8%，圈速与配置自洽（不是被节流）。

判据②的**正对照**是 `ops/motion_positive_control.py`（自开一个 30fps 自绘窗口，必定在动），
测完用 `ops/xiaoccctl motion <它的 pid>` 必须得到 `changed=true`；**它要求屏幕醒着且未锁屏**，
锁屏时连这个对照窗都不在屏上（抓图 None ⇒ 判「不可判读」，不会误报成「静止」）。
已验的负对照：桌面层（1512×982，Window Server）连抓三次 ⇒ `changed=false`、差异 0.0%。

**顺带一条实测（别照抄「屏幕睡=数字低」）**：屏幕睡着+锁屏时，Interactive 这一路仍量到 **14.6%**
（它照画不误）；@coder 那张表里「不带键+屏幕睡 = 4.6%」是**被节流**那一臂的特例，不是通用规律。

**手工复核**（不信脚本时）：

```sh
ops/xiaoccctl start && sleep 15
PID=$(launchctl print "gui/$(id -u)/ai.hermes.xiaocc" | awk '$1=="pid"{print $3; exit}')
A=$(ps -o time= -p "$PID"); sleep 60; B=$(ps -o time= -p "$PID")   # time= 过一分钟会变 HH:MM:SS，两种都要会解析
.venv/bin/xiaocc probe                                            # 退出码即第二条判据：0 过 / 1 没过 / 2 没文件
```

⚠️ **必须量 launchd 启动的那一份，别量手工跑的**（同一份代码 4.2% vs 托管 15~17%，**不是代码差异，是 `ProcessType` 那一个键**）。
⚠️ **记录显示器醒睡**（`doctor` 第 ⑫ 项会打）：屏幕睡着时 CoreAnimation 停画，CPU 天然偏低，两臂不可比。

**`ProcessType` 的结论（我这份 plist 的一行，已按 A/B 结果定住，别再顺手改）**：
`Interactive` **保留** —— 换 `Adaptive` 是 16.6%，白换；整个删掉是 6.5% 但**圈速掉到 10.3/s（画面真卡了）**，
门禁第二条本来就不放行。要降 CPU 只能改绘制路径（静态图层点阵化缓存），不是改这个键。
理由与四臂数字已经写进 `ops/ai.hermes.xiaocc.plist` 的注释里（改 plist 记得 `cp` 同步安装份，否则 `doctor` ① 会报不一致）。

**跑任何自检/测试都要设 `XIAOCC_ANCHOR_FILE=<临时路径>`**：不设就会往真实锚点 `~/.xiaocc/anchor.json` 写假坐标，
面板一 arm 就落在屏幕中间（codex 自测已犯过一次，留了个 (635,334) 的假锚点）。

`doctor` 的第 ⑩ 项会读锚点文件跟实测窗口矩形对一下（Δ≤6px 算一致）并打出最近一行位移留痕 ——
**只提示、不判失败**：拖拽搬家、贴边收起都是合法态，别把交互误报成故障。

## 7. CPU 看门狗（运行时保护，@lead 定：优先级高于门禁美化）

门禁 `arm` 是**一次性**部署检查，而且判据②在屏幕睡着时判不了；「人不在机器前、面板偷偷烧一整天」只能靠周期性采样兜底。

* `ai.hermes.xiaocc.watchdog`（`StartInterval=300`，常驻加载）+ `ops/xiaocc_watchdog.sh`：
  每 5 分钟量一次面板的**稳态** CPU（`ps -o time=` 两次做差，默认 10s 窗口，**与屏幕状态无关**），
  **连续 3 次 > 8%** ⇒ `xiaoccctl stop` + `watchdog.log` + **`watchdog-stop.json` 留痕**（含最后一次 CPU、阈值、`rearm` 提示）。
  面板没在跑 ⇒ 连续计数清零、静默退出（脚本自己几秒就结束，不常驻）。**它不会自动把面板装回来**，修好要人 `arm`。
* 文件：`~/Library/Logs/xiaocc/watchdog.log`、`watchdog.json`（当前连续计数 + 最近 9 次采样）、`watchdog-stop.json`（只在真动手停时写）。
* **阈值可配置**：`ops/ai.hermes.xiaocc.watchdog.plist` 的 `EnvironmentVariables` ——
  `XIAOCC_WD_THRESHOLD`（默认 8，%）、`XIAOCC_WD_STREAK`（默认 3，连续次数）、`XIAOCC_WD_WINDOW`（默认 10，秒）。
  改完 `launchctl bootout` + `bootstrap` 重载，然后确认 launchd 真的把它交给了脚本：
  `launchctl print gui/$(id -u)/ai.hermes.xiaocc.watchdog | grep XIAOCC`
  （**注意**：手敲 `ops/xiaocc_watchdog.sh` 走的是你自己的 shell 环境，看不到 plist 的值 —— 要验 plist 就用 `launchctl kickstart -k` 跑那一拍，再读 `watchdog.json` 里的阈值字段。）
* **日志口径**（避免每 5 分钟一行噪音）：常态一拍**只更新 `watchdog.json`**；`watchdog.log` 只在四种情况写 ——
  越界、回到阈值内、真停掉面板、以及「面板刚从有到无」（面板被人有意停着时不重复刷屏）。
* 停用：`launchctl bootout gui/$(id -u)/ai.hermes.xiaocc.watchdog`。
* **怎么测（别等 15 分钟）**：`XIAOCC_WD_THRESHOLD=3 XIAOCC_WD_STREAK=1 XIAOCC_WD_WINDOW=4 ops/xiaocc_watchdog.sh`
  —— 实测该组合在面板 4.0% 时立即停掉面板并写出留痕文件；部署默认值（8%/3 次/10s）不会被 3~9% 的常态触发。

**测量前务必确认只有一个面板进程**：`pgrep -f "xiaocc run"` 应为 1。
实测踩到过三次叠在同一位置的实例（两个是 `--linger 400` 的 A/B 臂、ppid=1 的残留：owner 脚本退出后它们还在）——
残留会同时烧 CPU、盖住窗口、污染任何「按 pid 从外面量」的结论。跑 A/B 的脚本要在自己的 `finally` 里清掉自己的臂。

## 8. 状态年龄（doctor ⑬）与「屏亮补验」一次性任务

**doctor ⑬ 状态年龄**（@lead 采纳 @researcher 的判读矩阵）：读 `~/.xiaocc/probe.json` 的
`state_source/state_at/state_age_s/state_ttl_s`，**只提示、不改退出码**（提示混进门禁会让 `arm` 因为一句话就停面板）；
`state_ttl_s` 为 `null`（idle/offline 这类不过期的状态）**永不报警**；容差 **+5s**（写盘周期 1s + 一拍 ≤1s，贴线会误报）。

> **已知语义坑（第一次上真机就撞到，@coder 待改）**：实测 `state_age_s` 目前是「距显示层上次**状态变化**多久」，
> 不是「源这次报告有多旧」—— 同一只面板上我先后读到 **74.5s（报 ⚠️）** 和 **1.0s（在期内）**。
> 因为显示层缓存的是最后一帧的 `event`，而 `hermes` 源每拍都用 `at=now` 重新报。
> 所以**长工具调用期间（`working` 持续 > TTL+5s）这条会狼来了**。判据：只要面板仍显示该状态、没退成 `idle/offline`，
> 就说明源每拍都在刷新鲜度 ⇒ 不是卡死（真卡死的形态是 `pick()` 丢弃 + 引擎合成 `idle/offline`）。
> 建议改成「最新**轮询到**的事件的年龄」，或另开一个 `state_changed_ago_s`。

**「屏亮补验」一次性任务**（@lead 定：一次性，不是周期任务，别和看门狗叠噪音）：
`ai.hermes.xiaocc.reverify` + `ops/xiaocc_reverify.sh`。屏幕睡着时 `arm --skip-motion` 会写欠账标记
`~/Library/Logs/xiaocc/skip-motion.json`；本任务等**屏幕亮起**后补跑一次完整 `gate`：

* 通过 ⇒ 写 `reverify-ok.json`（含 `changed` / `max_diff_pct` 实测数字）+ **删欠账标记**（欠账才算清）；
* 不通过 ⇒ 写 `reverify-fail.json` + **把面板停回去**；
* 判不了（屏幕又睡了）⇒ 只记一笔，欠账保留、面板照常跑（不误停）。

装/再装：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.xiaocc.reverify.plist`
（`RunAtLoad`、无 `StartInterval` ⇒ 只跑一次；要再补验重新 bootstrap）。
可调：`XIAOCC_RV_WAIT_MAX`（默认 7200s）/`XIAOCC_RV_POLL`（10s）/`XIAOCC_RV_SECS`（60s）；
**测试接缝** `XIAOCC_RV_LOGDIR`（沙箱目录）+ `XIAOCC_RV_FORCE_AWAKE=1` + 桩 `xiaoccctl`
—— 三条分支（通过/不通过/判不了）都用桩在沙箱里逐条验过，含「真删掉欠账标记」那一条。
