# 取证截图

这里的 `*.png` 是 `scripts/appkit_screenshots.py` 真实跑一遍 macOS 显示层留下的
**窗口本体截图**（带透明通道，就是屏幕上那一层像素，不是设计稿）。

```bash
python scripts/appkit_screenshots.py          # 重新生成，顺带跑 20 条断言
```

| 文件 | 说明 |
| --- | --- |
| `01-idle-floating` | 自由漂浮的待机 |
| `02-docked-collapsed` | 拖到屏幕右边缘松手 → 收成一条 12×116 的把手 |
| `03-handle-hover-no-flicker` | 鼠标仍压在把手条上 → 保持收起（防抖动的关键时序） |
| `04-hover-expanded` | 鼠标移到把手条 → 展开，且仍贴着右边缘 |
| `05-leave-recalls` | 鼠标离开 → 重新收起 |
| `06..09-state-*` | 各状态的动作与状态色 |
| `evidence.json` | 每一步的真实窗口状态（层级/透明/穿透/尺寸/视图尺寸）+ 断言结果 |

`*.desktop.png`（连桌面一起截，用来看透明合成效果）**不入库**——里面会有作者桌面上
其它窗口的内容。本地跑脚本时会生成，已经被 `.gitignore` 挡掉。
