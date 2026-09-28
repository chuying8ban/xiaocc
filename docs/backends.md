# 显示层（backend）开发指南

显示层是「把一帧画出来」的那一层。它的输入只有一个不可变对象
[`Render`](../src/xiaocc/engine.py)，输出是屏幕上的像素。**它不读状态源、不判断业务、
不决定角色长什么样** —— 这三条守住了，换平台就不用重写逻辑。

## 1. 契约

```python
from xiaocc.backends.base import Backend

class MyBackend(Backend):
    name = "mybackend"        # xiaocc run --backend mybackend
    interval = 0.25           # CLI 会覆盖成引擎节拍
    self_paced = False        # True = 本层自己消化节拍（GUI 应当置 True）

    def render(self, frame: Render) -> None: ...   # 必须实现（引擎只在状态变化时调用）
    def linger(self, seconds: float) -> None: ...  # 可选：截图/肉眼验收时多留一会儿
    def close(self) -> None: ...                   # 可选：收尾
```

`Render` 给出的东西（就这些，够画了）：

| 字段 | 含义 |
| --- | --- |
| `frame.state` | 7 个状态之一（`idle/thinking/working/waiting/done/error/offline`） |
| `frame.caption` | 状态卡主文案：`项目 · 详情 · 进度`（进度只在有真实数字时才出现） |
| `frame.character` | 角色包：`canvas` / `palette` / `states[state].{motion, accent, caption}` |
| `frame.event` | 原始状态事件（`source` / `detail` / `step` / `total`），调试才用 |

## 2. 平台无关的部分直接复用 `window_layout`

贴边、命中、动作曲线是**纯几何**，不依赖任何图形库，已经在
[`backends/window_layout.py`](../src/xiaocc/backends/window_layout.py) 里实现并有单元测试：

```python
from xiaocc.backends import window_layout as wl

body  = wl.body_rect_of_window(window_rect, canvas=character.canvas, scale=1.0)
edge  = wl.choose_edge(body, screen)                 # 够近才吸附，太远返回 NONE
strip = wl.collapsed_rect(edge, screen, body)         # 收起后的把手条
full  = wl.docked_rect(edge, screen, size, center=strip.center.y)  # 展开后必须盖住把手条
pose  = wl.pose_for(character.spec(state).motion, t)  # 动作在第 t 秒的姿态
```

`wl.Dock` 是收起/展开的状态机（`drop()` / `update()` / `drag_started()`），
把「鼠标事件」翻译成 `DockAction.NONE|COLLAPSE|EXPAND` 就行。

**坐标约定**：`window_layout` 用「屏幕左上角为原点、y 向下」（跟 Windows / Web / CGWindow 一致）。
AppKit 用左下原点，所以在 AppKit 显示层里翻了两次：一次是窗口矩形（`:meth:`_Space.to_ns_rect`），
一次是视图内矩形（`_local()`）。**别把屏幕坐标喂给视图绘制** —— 画到视图外面去了以后，
屏幕上看只是「画面停住不动」，很难debug（本项目踩过，见第 5 节）。

## 3. 造型从哪来：角色包优先，程序化骨架兜底

显示层**不含**任何角色私有形状。取图的顺序（AppKit 显示层已实现，其它平台照抄即可）：

1. `character.json` 的 `assets.base` / `assets.<state>` 指向的图片（PNG / SVG 都行）
2. 角色包目录里自动发现的 `assets/<state>.png|.svg`
3. 都没有 → 通用程序化骨架：只用 `palette` + `canvas` + `motion` 画「光环 + 球体 + 表情」

第 3 条存在的意义是：第三方作者写了个新角色包、只填了配色，也能立刻看到东西，
而不是一片空白。选定形象后，把它做成 per-state 图丢进角色包即可换脸，**显示层不用改**。

## 4. 后端选项（--backend-opt）

```bash
xiaocc run -b appkit --backend-opt at=bottom-left --backend-opt scale=0.8 --backend-opt fps=30
```

这是**通用**机制，不是 appkit 专属：任何显示层只要在自己的 `__init__` 里声明具名关键字参数，
就能从命令行收到对应的选项，**不用改 CLI**（CLI 只负责把 `键=值` 收成 dict，再 `**opts`
透传给构造函数）。值会按 `int → float → true/false → 字符串` 的顺序自动转型，所以 `fps=30`
到显示层手里是数字 `30`、`scale=0.8` 是 `0.8`，转不了的当字符串（`at=bottom-left`）。
给了显示层不认识的键，CLI 会人话报错并 `exit 2`，不会静默忽略。

`appkit` 目前支持三个：

| 选项 | 取值 | 默认 |
| --- | --- | --- |
| `at` | `top-right` / `top-left` / `bottom-right` / `bottom-left` / `center`，或 `x,y`（屏幕左上角坐标，例如 `120,300`） | `top-right`（右上角） |
| `scale` | 缩放倍率 | 角色包的 `default_scale` |
| `fps` | 动画帧率（夹在 1–60） | `30` |

`at` 存在的现实理由：同一台机器上可能同时跑着别的桌宠（用户也可能自己开两个不同角色），
两只都默认挤在右上角就会叠成一坨、互相挡住 —— 所以初始位置必须能配。取值写错时在**加载阶段**
就报错、并把合法取值一次列全（悄悄跑到屏幕外的话，用户看不见角色，只会以为程序挂了）。
位置留白的具体数值由 `window_layout.ANCHOR_MARGIN` / `ANCHOR_TOP_OFFSET` 一处说了算，
显示层里不要再抄一份常量。

## 5. 窗口位置语义：拖动＝搬家，其余位移＝bug

两种位移必须分开对待，否则用户拖好的位置会被自己的代码弹回去，而真正该管的漂移却没人发现：

| 位移 | 语义 | 处理 |
| --- | --- | --- |
| 用户拖动后松手（且没贴边） | **有意搬家** | 写成锚点（内存 + 磁盘一起换）→ 下次启动还停在那儿 |
| 贴边收起 / 悬停展开 | 临时态 | 改窗口，**不动**锚点（否则「家」会永久变成屏幕边上） |
| 其它任何位移 | bug | 回锚 + 日志留一行 warning |

* 锚点文件：`~/.xiaocc/anchor.json`（`XIAOCC_ANCHOR_FILE` 可覆盖 —— **测试脚本必须覆盖它**，
  否则自测拖一下就把用户真实的锚点改了，下次启动桌宠落在屏幕中央）。
* 起始位置优先级：命令行 `--backend-opt at=…` > 磁盘锚点 > 默认右上角。传了 `at=` 时
  **不读也不写**磁盘那份（运维说了算）。
* 所有改窗口位置的地方都走 `_set_window_rect(rect, reason=…)`，`reason` 取
  `drag` / `collapse` / `expand` / `anchor` / `restore`。裸调 `setFrame` 会让窗口悄悄漂走
  而日志里什么都没有 —— 运维实测过：启动时 `1324,96`，跑到 3 分钟变成稳定的 `1225,100`
  （左偏 99px）且自己弹不回来，他在机器外无从判断那是交互还是卡住。
* 自检每秒一次（`_DRIFT_CHECK_PERIOD`，用**墙钟**判周期，不是「这一圈干了多少活」的时间 ——
  一圈里大部分时间在 sleep，按干活时间攒要好十几秒才查一次）：非拖动、非贴边态却不在锚点上
  → 回锚 + `检测到窗口漂移（非拖动）(x,y) → 回到锚点 (x,y)`。
* 运维/doctor 可 grep 的三种行：`位置变化 [<reason>] (x,y) → (x,y)`、
  `锚点已更新（用户拖动）: (x,y)`、`检测到窗口漂移（非拖动）…`；
  `probe()` 里读 `anchor` / `anchor_ok` / `anchor_state`（`anchor` / `drag` / `collapsed` / `drifted`）。
  **`anchor_ok` 只在「没人碰它却漂在别处」时为 `False`**，拖拽/贴边这些正常交互态都是 `True`，
  免得 doctor 把交互态误报成故障。

### 5.1 自证据（`~/.xiaocc/probe.json`）与门槛

显示层每秒把自己的一份快照落盘（原子写，`XIAOCC_PROBE_FILE` 可覆盖），**`xiaocc probe`** 读它并按
退出码下判据（0 通过 / 1 没过 / 2 没有文件）。为什么非要落盘：**CPU 低有两种可能** —— 真的省，
或者被节流了（动画其实在卡）。`ps` 只给得出前者，「圈速 ≈ fps」只有进程内知道。

门槛是**两件事一起过**：CPU < 5% **且** 圈速在 fps 的 0.7~1.3 倍之间。实测反例（同一份代码、
同一源、`fps=30`）：

| 启动方式 | 屏幕 | CPU | 圈速 |
| --- | --- | --- | --- |
| 前台 `xiaocc run -b appkit` | 醒 | 4.2% | ≈30/s（1.0x） |
| launchd + `ProcessType=Interactive` | 醒 | 16~18% | 28~30/s（0.94x） |
| launchd 不带 `ProcessType` | 醒 | 8.2% | **11.5/s（0.38x，被节流）** |
| launchd 不带 `ProcessType` | 睡 | 4.6% | **5.9/s（0.20x，被节流）** |
| launchd + `ProcessType=Adaptive` | 醒 | 18.4% | 28/s（0.94x） |

「不带键」那条只省了 CPU 是因为**画面真卡了**，光看 `ps` 会把它当成通过 —— 这就是门槛要两个数的原因。

launchd 托管那份 15%+ 的 CPU 在哪：`sample` 抓栈是
`CA::Layer::display_if_needed` → `-[NSViewBackingLayer display]` → CoreGraphics 软件光栅化
（`RGBAf16_image_mark` 贴图 / `RGBAf16_shade_radial_RGB` 径向渐变 / `aa_render`、`aa_cubeto` 抗锯齿路径）。
**是每帧重画的代价**，不是「画得多」那一类（降 fps 只会变成「被节流」）。要真降就得少画：
把静态部分（角色图、光晕）先点阵化缓存再贴，别每帧重新走一遍路径+渐变的光栅化。

## 6. 踩过的坑（照抄容易，独立踩出来要花一晚上）

| 坑 | 现象 | 正确做法 |
| --- | --- | --- |
| 用窗口矩形判贴边 | 离边缘还有几十像素就自动收起 | 用**角色本体**矩形判（窗口带透明留白和文案带） |
| 收起后立刻允许悬停展开 | 拖到边缘松手时鼠标还压在把手条上 → 一收一展疯狂抖动 | 收起时清掉「上膛」标志，鼠标**先离开一次**才允许展开（`Dock.armed`） |
| 展开后的窗口没盖住把手条 | 展开瞬间鼠标就落到窗口外 → 立刻又收起 | 展开时用把手条的跨轴中心做锚点（有测试守这条不变量） |
| 视图坐标 vs 屏幕坐标混用 | 画面「停住」，抓图拿到的是上一帧（字节完全一样） | 绘制只吃窗口内坐标；屏幕坐标只用于放窗口 |
| 窗口缩放后视图尺寸没跟着变 | 收起/展开后内容错位 | 绘制时用视图实际高度换算 y，别用缓存的窗口矩形 |
| 描边色没显式设置 | 弧线（笑眼/撇嘴）沿用上一笔的描边色，淡成一道灰边 | 每段弧之前显式 `setStroke()` |
| GUI 显示层里再 `sleep` | 渲染 0.2 秒 + 睡 0.25 秒，动画一顿一顿 | 置 `self_paced = True`，用 `interval` 在自己的事件循环里等 |
| `self_paced = True` 却只在 `render()` 里走时间 | **一个核吃满**（实测 99.9%）：`engine.tick()` 只在状态变化时给帧，**绝大多数轮次返回 `None`**，那几轮既不 `render()` 也不睡眠 → 主循环空转（实测 5 秒 732768 圈 / 99.8%）。降 `fps` 完全无效，因为瓶颈不在每帧工作量上 | `self_paced` 的显示层必须实现 `idle()`，把「没有新帧」那一拍的时间也走掉（见 `Backend.idle`）；`idle()` 里**绝不能**直接返回 |
| 用 `nextEventMatchingMask` 当限速器 | 以为它会按 `untilDate` 阻塞 33 毫秒，实际立刻返回 `None` | 它在本进程（没有 `NSApp.run()`）**不阻塞**：帧预算的余量用显式 `time.sleep()` 睡掉 |
| 拿 `ps %cpu` 单次采样验收 | 分不清「启动那几秒烧的」和「一直在烧」 | 采两次 `ps -o time=` 算差值（或 `resource.getrusage` 前后做差）得到窗口内真实占用 |
| 直接用真实鼠标做回归测试 | 测试会劫持用户的鼠标，且结果不确定 | 光标来源可注入（`AppKitBackend(cursor=...)`），拖拽用多段平滑位移 |

## 7. 已经有的显示层

| 名字 | 平台 | 依赖 | 能力 |
| --- | --- | --- | --- |
| `console` / `terminal` | 终端 | 无 | 调试、SSH、CI 里的端到端断言 |
| `appkit` | macOS | `pyobjc-framework-Cocoa` | 透明无边框 + 置顶 + 贴边隐藏 + 点击穿透 + 拖拽 |

想要 Windows / Web / TUI 版本？`xiaocc.sources` 和 `xiaocc.backends` 都是 entry point 扩展点，
**不用改核心代码**（见 `pyproject.toml` 的 `[project.entry-points]`）：

```toml
[project.entry-points."xiaocc.backends"]
mybackend = "my_pkg.backend:MyBackend"
```

## 8. 取证脚本

`scripts/appkit_screenshots.py` 用注入的光标把「拖拽 → 贴边收起 → 悬停展开 → 鼠标离开再收起」
跑一遍，逐步截图并断言窗口的真实状态（层级、透明、穿透、尺寸、视图是否跟窗口一致）：

```bash
python scripts/appkit_screenshots.py                    # → docs/evidence/
python -m xiaocc.cli run --source hermes -b appkit --once --linger 4   # 真 CLI，窗口留 4 秒
```
