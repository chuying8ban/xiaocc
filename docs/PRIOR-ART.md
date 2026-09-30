# 先例核查与「参考 / 雷同」边界

核查日期：2026-09-28（CST）。所有结论都来自下面的实测命令，不是印象。verified by the maintainer。

## 1. 名字可用性（实测）

| 名字空间 | 名称 | 结果 | 证据 |
| --- | --- | --- | --- |
| PyPI | `xiaocc` | **可用**（404 = 未注册） | `curl -o /dev/null -w '%{http_code}' https://pypi.org/pypi/xiaocc/json` → 404 |
| PyPI | `xiao-cc` / `xiaocc-pet` / `xiaocc-ai` / `xiaocc-desktop` / `xiaoccpet` / `xiao-cc-pet` / `xiao-c` / `xiaoc` / `cc-pet` | 全可用（均 404） | 同上，逐个替换包名 |
| npm | `xiaocc` | **可用** | `https://registry.npmjs.org/xiaocc` → 404 |
| npm | `dsh-dafeiyu` | 已被占用（参考项目的包） | `https://registry.npmjs.org/dsh-dafeiyu` → 200 |
| GitHub 用户名/组织 | `xiaocc` | **已被占用**（`XiaoCC`，2014-04-15 注册，175 个公开仓库） | `GET /users/xiaocc` → 200；`GET /orgs/xiaocc` → 404 |
| GitHub 仓库 | `QCYTSN/xiaocc`（示例路径） | 404，未被占用 | `GET /repos/QCYTSN/xiaocc` → 404 |

GitHub 搜 `xiaocc` 命中 22 个仓库、`xiao-cc` 命中 39 个，逐个看都是个人主页（`*.github.io`）、奶茶店小项目、以 `xiaocc` 为 user 名的 `aarch64 c compiler` 之类，**没有同类桌宠/状态伴侣产品**。

PyPI 的取名限制只有四条（<https://pypi.org/help/#project-name>）：与标准库模块冲突、与已有项目**过于相似可能混淆**、被管理员明令禁止、被他人注册但从未发布。上表只排除了「已被占用」和「有近似项目」，不构成对「过于相似」的绝对保证——但目前前缀 `xiaocc*` 下无任何已发布项目，风险低。

### 结论
仓库名 / PyPI 名 / npm 名统一用 **`xiaocc`**；中文对外名 **小cc**。

唯一真实的名字风险不在仓库名，而在 **「CC」这个缩写本身**：2026-09-18 Google 把它的 AI agent 产品正式叫作 **CC**（Coordinating Companion，TechCrunch 报道，<https://techcrunch.com/2026/09/18/>）；此外小米 CC 手机系列、`ccswitch.ai`、以及开发社区里 `cc` 长期是 Claude Code 的口语缩写。应对：README 首屏不要把项目品牌化地单独写成大写 "CC"，对外一律 `xiaocc` / 小cc。

## 2. 参考项目：`QCYTSN/dsh-dafeiyu`

实测（GitHub API + 原始文件，经镜像 `gh-proxy.com` / `ghproxy.net` 取，直连被墙）：

- 创建 2026-08-14，最近推送 2026-09-16；**362 stars / 28 forks**；语言 JavaScript；topics：`agent-companion`、`deepseek-harness`、`desktop-pet`、`dsh-plugin`、`windows`。
- 形态：**DSH（DeepSeek Harness）的插件**，npm 包 `dsh-dafeiyu@0.1.14`，`engines.node >=22.19`；附一个 Python/原生 helper（`runtime/` + `native/macos/` Swift）。
- 代码许可：**MIT，Copyright (c) 2026 QCYTSN**（`LICENSE`）。
- 状态协议（`src/protocol.js`）：`CompanionState` = `IDLE / THINKING / WORKING / WAITING / SUCCESS / ERROR / DISCONNECTED`（大写常量），消息信封 `{protocolVersion, kind, timestamp, ...}`，`PROTOCOL_VERSION = 1`，`kind` 有 `ready/hello/state/pulse/task/tasks/config/settings/ping/pong/closed/shutdown`。
- 动作集（`docs/PRODUCT_SCOPE.md`）：呼吸/眨眼/观察/扫地/走路/开心/生气/摸头/戳/尾巴/拖动/眩晕等 18 个。

## 3. 美术与素材许可：**不能碰的部分**

`ASSET_LICENSE.md` 原文（经镜像取回）划出三条相互独立的边界：

1. **`legacy/dafeiyu/`（也就是我们本地那只「大肥鱼」的角色帧）不属于 MIT**。原文：*"The previous BigFish frames remain archived under `legacy/dafeiyu/` under their original restricted terms"*、*"remain excluded from the MIT code license"*、*"Redistribution outside this repository requires permission from the respective rights holders"*。其中拖动帧由 `@Serendipity-wu02` 贡献并注明 *"Redistribution outside this repository requires permission"*。
   → **大肥鱼的角色帧一帧都不能进 `xiaocc`**，也不能作为描图/重绘底稿。
2. **它现在的角色帧也不是它的原创**：`assets/pet/` 由第三方项目 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 的绿幕动画转码而来，Copyright (c) 2026 PC2005-cloud，MIT + 上游 AI 生成提示词。→ 我们用它，等于「与参考项目同源」，直接违背「不能雷同」。
3. **`assets/cursor_*.cur` 是 Chromium 项目原样拷贝**，BSD-3-Clause（The Chromium Authors），并带 MD5 校验。→ 我们若要做同款拖拽光标，自己画，或同样引用 BSD-3 上游文件并保留声明，别从它仓库拷那两个文件。

另：它带免责声明 *"unofficial fan-made project and is not affiliated with or endorsed by DeepSeek"*，上游还牵出 `ds-local-pet`、`1190fasheqi/dafeiyu-pet` 两个仓。

## 4. 参考 vs 雷同：可做 / 不可做

**可参考（工程思想，不受版权保护，独立实现即可——这些是通用手法，任何桌宠都会做）**
- 透明、无边框、始终置顶、非激活面板；
- 「状态源 → 统一协议 → 可插拔显示层」的解耦思路；
- 贴边吸附 / 悬停展开 / 鼠标命中判定 / 鼠标不在角色上时整窗穿透；
- 动作的 enter / body / exit 三段式、动作优先级与可中断、reduced-motion 无障碍开关；
- 拖拽改变朝向、位置持久化。

**会变成雷同（要避开）**
1. **复制代码**。MIT 允许用但要保留原版权声明——这与「全新、不雷同」直接冲突。一行都不抄，重写实现（我们的纯几何层 `window_layout.py` 是零图形依赖的独立实现，这是我们的差异点，值得在 README 里讲）。
2. **协议字面**。它的状态词与我们 **5/7 重合**：我们 `OFFLINE/IDLE/THINKING/WORKING/WAITING/DONE/ERROR` vs 它 `IDLE/THINKING/WORKING/WAITING/SUCCESS/ERROR/DISCONNECTED`。我们用小写 wire 值、结构也不同（`StatusEvent` 逐行 JSON，不是 `{protocolVersion,kind,...}` 信封），**不算抄**；但评审会看出来。建议 README 里一句话说明「状态词取自宿主工作流的通用语义，不取自任何同类项目」，并保留我们已差异化的 `DONE` / `OFFLINE` 命名。
3. **文档与呈现**。README 结构、目录分工（`docs/ACCEPTANCE.md`、`docs/UPDATING.md`、`docs/PRODUCT_SCOPE.md`）、截图、状态文案（`src/status-copy.js` 那一套）都不照抄；它的取舍表尤其不要照抄——那是它针对「插件版」做的决策。
4. **包名与资源**：包名不重名（已核实）；美术资源 100% 本项目原创（含 AI 辅助生成，见下）。

## 5. 我们这边修掉的一处不实声明（已修）

`ASSET_LICENSE.md` 原先把全部美术资源概括成「本项目原创、由人逐笔绘制」（原文见 git 历史）。实际情况是：`docs/design/` 下的 A/B/C 三张方向稿与 7 个状态矢量稿，都是 **Codex CLI（`codex exec`）按一份书面几何规格逐文件程序化生成**，再人工校对坐标——原来那句话不成立，公开仓库里这种断言会被要求出示证据。参考项目的处理方式值得直接采用：**标注「AI 生成 / AI 辅助」+ 把生成规格与源文件一起入库**，并在测试或脚本里钉住来源。

现已按此改完，`ASSET_LICENSE.md` 的现行措辞是：

> 角色形象（胶囊机甲「丸丸」及 7 个状态的矢量稿）为**本项目原创**：
> 形象方向由项目设计稿确定，矢量稿由 AI 辅助的程序化生成产出，再经人工校对。
> 生成规格、状态对照表与源文件位置留档于 [design/丸丸-资产说明.md](design/丸丸-资产说明.md)。

生成规格的单独留档在 `docs/design/生成记录.md`（几何规格全文、7 状态对照、逐文件来源、生成工具与人工校对点）。**不要沿用「生成提示词、源文件与转换脚本留档于 `docs/design/`」这类说法**：`docs/design/` 里现在有规格与源文件，但生成是逐文件直出 SVG、没有中间格式，也就没有转换脚本——写上去同样不可核。
