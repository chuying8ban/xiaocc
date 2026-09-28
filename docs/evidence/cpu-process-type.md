# 面板 CPU 15% vs 4%：真凶是 plist 里的 `ProcessType=Interactive`

核查：@researcher，2026-09-29，1512×982 屏，`--source hermes`，`fps=30`，全部从进程外
用 `ps -o time=` 做差量（暖机后取窗口，单位 = 累计 CPU 秒 / 窗口秒）。

## 结论（实测）

| 启动方式 | 实测 CPU |
| --- | --- |
| 前台手工：`xiaocc run -b appkit`（默认右上角） | **3.6%** |
| 前台手工：`--backend-opt at=bottom-right`（= run-panel.sh 那一路） | **4.1%** |
| 前台 + 项目目录 cwd + 最小 PATH + stdout 重定向到日志文件 | **4.2%** |
| **launchd（LaunchAgent）+ `ProcessType=Interactive`** | **16.4%**（本人两轮：45s 窗口、20s 窗口各一次；@ops 独立测得 15.2 / 15.3%） |
| **launchd 不带 `ProcessType` 键** | **4.6% / 4.5%**（本人两轮，45s / 20s 窗口） |

`Interactive / 无键 = 3.57x ~ 3.64x`，两次独立复跑一致（`scripts/verify_process_type.py`）。

即：**@coder 的忙等修复是对的，@ops 的 15% 也是真的 —— 差异来自部署 plist 的一个键，不是代码。**
去掉那个键，同一份代码在 launchd 下就是 4.5%，与小汐同规格基准（4.6~5.0%）同档。

## 被排除的变量（逐个实测，不是推断）

- **窗口位置**：右上角 3.6% vs `bottom-right` 4.1%（1.16x）→ 不是位置/被遮挡问题。
- **cwd / PATH / stdout 去向**：把 run-panel.sh 的这三项（项目目录、最小 PATH、日志文件重定向）
  全搬进前台，仍是 4.2% → 不是环境差异。
- **`KeepAlive`、`RunAtLoad`、`LimitLoadToSessionType=Aqua`**：无键那一臂保留了这些键，仍是 4.6%
  → 不是 launchd 托管本身，就是 `ProcessType` 这个键。
- 状态源、fps、角色包、锚点路径两臂完全相同。

## 机制（推断，未 profile，别当结论用）

`ProcessType=Interactive` 是告诉系统「这个 job 是用户交互关键路径」：它关掉定时器合并
（timer coalescing）与 App Nap。忙等修掉之后，每秒 30 次唤醒 + 每帧一次 CoreAnimation flush
第一次被**按价全额收取**，于是同一份代码在两种启动方式下差 3.5 倍。
至于这 3.5 倍是「唤醒本身变贵」还是「合成/绘制被全额计费」，本次没做栈采样；
要坐实机制，对部署中的进程跑一次 `sample <pid> 5` 看栈直方图即可（不需要 sudo）。

## 建议（@ops 的 plist 一行，@coder 的代码一行）

1. `ProcessType=Interactive` → 先试 **`Adaptive`**（Apple 文档语义：由系统决定，前台全速、
   不在前台可被节流），实测不达标就**整个删掉这个键**（已实测 4.5%）。
   注意：不能靠降 `fps` 掩盖（已验证与帧率无关）。

   **⚠️ 本节后经全队复核被推翻（2026-09-29 晚），保留原文仅为留痕：**
   - `Adaptive` 白换（16.6% vs 16.8%）；**删键那一路的 4.5~4.6% 是「被节流」而不是「省电」**——
     @coder 复量到圈速 10.3/s（屏睡时 5.9/s），只有配置值的 0.2~0.34 倍，画面真卡了。
   - 所以 plist **保留 `Interactive`**（@ops 定，四臂数字已写进 plist 注释）；
     降 CPU 只能靠每帧少画：@coder 的静态层点阵化把部署路径从 16.8% 压到 9.0%（圈速不变），
     再以 `fps=15` 过闸（3.8%，圈速 13.9/s = 配置值）。
   - 另：本文「屏幕睡着时数字会偏低」说反了一半——@ops 实测 `Interactive` 那臂屏睡时仍 14.6%
     （照画不误），低的是被节流的那一臂。**两臂仍要记录显示状态，但别拿它解释差异。**
2. **验收要同时量两件事**，否则「便宜」可能只是「被节流了」：CPU < 5% **且** 事件循环圈速
   ≈ `fps`。`appkit.probe()` 里已经有这两个自证据（上一秒结算的圈速、上一圈真正睡了多少 ms），
   但它现在**只在进程内被 scripts/ 用**，ops 的 `doctor` 从外面读不到。
   建议把 `probe()` 的关键字段（圈速 / 圈数 / `anchor` / `anchor_ok` / `anchor_state`）
   落到一个机器可读文件（如 `~/.xiaocc/probe.json`）或加一个 `xiaocc probe` 子命令，
   这样「没忙等、也没被节流」变成可外部断言的判据，而不是靠 `ps` 一个数字。
3. 量 CPU 时**记录显示器/锁屏状态**：显示器睡着时 CoreAnimation 会停画，数字会偏低，
   两臂必须在同一显示状态下比（本轮全部在亮屏状态下取）。

## 复跑

```bash
python scripts/verify_process_type.py                 # 15s 暖机 + 45s 测量，两臂
python scripts/verify_process_type.py --seconds 20 --warmup 10
```

脚本用一次性 Label `ai.hermes.xiaocc.proctypeprobe-*`，跑完 `bootout` + 删 plist，
不碰部署中的 `ai.hermes.xiaocc`；锚点走 `XIAOCC_ANCHOR_FILE` 临时路径，
不污染 `~/.xiaocc/anchor.json`（@coder 踩过的那个坑）。
