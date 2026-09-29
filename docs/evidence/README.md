# 取证截图（显示层）

全部由脚本在**真窗口**上抓下来的，不是设计图、也不是手绘示意。两个产出脚本：

```bash
.venv/bin/python scripts/appkit_screenshots.py                # 贴边/悬停/穿透/7 状态 → 01~09
.venv/bin/python scripts/verify_window_position.py --capture  # 初始停靠点 9 例 → 11
.venv/bin/python scripts/art_montage.py                       # 7 状态对照图 → 13
.venv/bin/python scripts/desktop_mockup.py                    # README 首屏图 → docs/images/（见文末）
```

（`appkit_screenshots.py` 默认还会多写一张 `<名字>.desktop.png` = 连桌面一起截，
用来证明角色真的浮在桌面之上；那几张被 `.gitignore` 排除，见文末。）

> ⚠️ **入库的 `01~09` 这一批目前是空图，不能当证据用**（2026-09-29 发现）。`048fa6f` 那次
> 「重跑截图」是在**显示器睡着**（`CGDisplayIsAsleep=True`）时跑的，`CGWindowListCreateImage`
> 在这个状态下返回**全透明**的图：9 张窗口本体图 + 9 张 `.desktop.png` 的**非零字节都是 0**
> （把 `NSBitmapImageRep.bitmapData()` 整个扫一遍数出来的，不是抽样看几眼）。
> 断言 20/20 却照样通过，因为断言查的是窗口层级/尺寸/穿透这些**元数据**，压根不看像素。
>
> 在这个坑补上之前：① `appkit_screenshots.py` 抓图前应先看显示器状态，睡着就**拒跑并报错**，
> 别静默写空图；② 把「非透明像素占比」接进断言（`scripts/pixel_stats.py` 已有现成判据），
> 空图不许通过。屏亮时重跑一次即可恢复 —— 参照物是 `8ff7304` 那版：`09-state-done.png`
> 320×388、非零字节 166821/496640。

## 文件对应什么

| 文件 | 证明什么 |
| --- | --- |
| `01-idle-floating.png` | 待机时窗口是**透明无边框**的：图里没有矩形背板、没有阴影、没有标题栏 |
| `02-docked-collapsed.png` | 贴边收起后只剩一条把手条，且贴着屏幕边缘 |
| `03-handle-hover-no-flicker.png` | 鼠标移到把手条上时不抖动（连续两帧位置一致） |
| `04-hover-expanded.png` | 悬停展开后的完整窗口 |
| `05-leave-recalls.png` | 鼠标离开后重新收起 |
|| `06-state-idle.png`、`07-state-working.png`、`08-state-error.png`、`09-state-done.png` | 四个代表性状态用对了角色包里的 `assets/<状态>.svg`（idle / working / error / done） |
|| `11-default-top-right.png`、`11-at-top-left.png`、`11-at-bottom-left.png`、`11-at-bottom-right.png`、`11-at-center.png`、`11-at-xy.png` | `--backend-opt at=...` 各方位真开窗口抓的图 |
| `11-at-bottom-left.png` | 走真实 CLI（`xiaocc run --source hermes -b appkit`）起的窗口：状态取自当时活着的 Hermes 会话（底部那行是它的会话标签），可当「CLI 真跑」的证据 |
| `13-art-states-montage.png` | 7 个状态的对照图（原尺寸 + 60px + 32px）：证明 7 档一眼能区分、缩到 60px 仍认得出来 |
| `evidence.json` | 上面每一步的断言明细（层级 / 透明度 / 穿透 / 尺寸 / 视图是否跟窗口） |
| [cpu-process-type.md](cpu-process-type.md) | 另一个疑案的核查记录：面板 CPU 15% vs 4% 的真凶是 plist 里的 `ProcessType=Interactive`（进程外 `ps -o time=` 差量量的） |

## 为什么 `*_*.desktop.png` 不入库

那几张是连桌面一起截的（用来证明「角色浮在真实桌面之上、背后没有方框」），
会把你桌面上其它窗口的内容一起带进仓库，所以 `.gitignore` 排除，只在本地留着。
入库的都是**只含窗口本体**、带透明通道的图。

## README 首屏图是合成的：`docs/images/desktop-mockup.png`

`scripts/desktop_mockup.py` 把**只含窗口本体**的取证图贴到一块**由代码画出来的**背景上，
位置取真机锚点（1512×982 上 `x=1324, y=96`，即默认右上角）。合成里不含任何真实壁纸 ——
连桌面一起截的图不该进公开仓库，而透明底的窗口本体图直接贴进 README 又看不出它是浮在桌面上的。

脚本自带一次自检：**再渲染一遍不带窗口的版本，两版必须字节不同**，否则说明窗口压根没贴上去
（这个坑真踩过：`NSImage.drawInRect_` 在无 GUI 的进程里对懒加载的 PNG 静默不画 —— 背景画得出来、
图贴不上）。出图按 2x 像素落盘（412×400 点 → 824×800 像素），贴图与像素 1:1，不糊。

源图用哪张可以用 `--window` 指：默认是 `01-idle-floating.png`（待机、无字幕，不会串味）；
要显示字幕就换 `09-state-done.png` 那类 —— 但字幕里的项目名会跟着当时的取名规则变，
所以首屏图默认挑没有字幕的那张。

## 坐标的一个坑

`CGWindowListCopyWindowInfo` 报的 `kCGWindowBounds` 是**内容包围盒**（会裁掉四周全透明的留白），
比我们设给 `NSWindow` 的布局矩形小 1~4px。校验窗口位置时拿它做等值比较会误判成「位置不对」，
`scripts/verify_window_position.py` 因此只做 ±4px 容差校验，权威值取进程日志里的布局矩形。
