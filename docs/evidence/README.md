# 取证截图（显示层）

全部由脚本在**真窗口**上抓下来的，不是设计图、也不是手绘示意。两个产出脚本：

```bash
.venv/bin/python scripts/appkit_screenshots.py                # 贴边/悬停/穿透/7 状态 → 01~09
.venv/bin/python scripts/verify_window_position.py --capture  # 初始停靠点 9 例 → 11
.venv/bin/python scripts/art_montage.py                       # 7 状态对照图 → 13
```

（`appkit_screenshots.py` 默认还会多写一张 `<名字>.desktop.png` = 连桌面一起截，
用来证明角色真的浮在桌面之上；那几张被 `.gitignore` 排除，见文末。）

## 文件对应什么

| 文件 | 证明什么 |
| --- | --- |
| `01-idle-floating.png` | 待机时窗口是**透明无边框**的：图里没有矩形背板、没有阴影、没有标题栏 |
| `02-docked-collapsed.png` | 贴边收起后只剩一条把手条，且贴着屏幕边缘 |
| `03-handle-hover-no-flicker.png` | 鼠标移到把手条上时不抖动（连续两帧位置一致） |
| `04-hover-expanded.png` | 悬停展开后的完整窗口 |
| `05-leave-recalls.png` | 鼠标离开后重新收起 |
| `06~09-state-*.png` | 四个代表性状态用对了角色包里的 `assets/<状态>.svg`（idle / working / error / done） |
| `11-at-*.png` | `--backend-opt at=...` 各方位真开窗口抓的图；`11-err-*.png` 是三种错误提示的退出码 |
| `11-at-bottom-left.png` | 走真实 CLI（`xiaocc run --source hermes -b appkit`）起的窗口：状态取自当时活着的 Hermes 会话（底部那行是它的会话标签），可当「CLI 真跑」的证据 |
| `13-art-states-montage.png` | 7 个状态的对照图（原尺寸 + 60px + 32px）：证明 7 档一眼能区分、缩到 60px 仍认得出来 |
| `evidence.json` | 上面每一步的断言明细（层级 / 透明度 / 穿透 / 尺寸 / 视图是否跟窗口） |

## 为什么 `*_*.desktop.png` 不入库

那几张是连桌面一起截的（用来证明「角色浮在真实桌面之上、背后没有方框」），
会把你桌面上其它窗口的内容一起带进仓库，所以 `.gitignore` 排除，只在本地留着。
入库的都是**只含窗口本体**、带透明通道的图。

## 坐标的一个坑

`CGWindowListCopyWindowInfo` 报的 `kCGWindowBounds` 是**内容包围盒**（会裁掉四周全透明的留白），
比我们设给 `NSWindow` 的布局矩形小 1~4px。校验窗口位置时拿它做等值比较会误判成「位置不对」，
`scripts/verify_window_position.py` 因此只做 ±4px 容差校验，权威值取进程日志里的布局矩形。
