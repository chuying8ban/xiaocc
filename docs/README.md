# 文档总览

按「你想干什么」排：

| 文档 | 讲什么 |
| --- | --- |
| [architecture.md](architecture.md) | 整体结构：状态源 → 后端 → 窗口/渲染，各层边界在哪 |
| [sources.md](sources.md) · [sources_EN.md](sources_EN.md) | 怎么写一个状态源（三分钟，含可运行示例） |
| [backends.md](backends.md) | 怎么写一个新显示层（后端） |
| [quota.md](quota.md) | 额度那几行的数据来源与口径：哪家有真接口、其余几家为什么只能写「无自动接口」、取不到为什么不补 0 |
| [design/README.md](design/README.md) | 形象与界面稿、实拍，以及每张图对应哪一版 |
| [evidence/README.md](evidence/README.md) | 真窗口抓的取证截图，各张证明什么；[cpu-process-type.md](evidence/cpu-process-type.md) 是其中一个具体疑案的核查 |
| [PRIOR-ART.md](PRIOR-ART.md) | 与先例、同类项目的边界核查（哪些判断是我们自己的） |
| [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md) | 发布前检查清单：哪些项已由 `scripts/release_check.py` 自动跑、哪些还没落 |

两条规矩：

1. **面板上只写「结论 + 一个动作」，依据论证放这里。** 例：某家额度为什么取不到，面板上写
   「无自动接口」，完整依据（探测过的路径、实测结果）在 [quota.md](quota.md) —— 那几行的宽度
   预算只有一行，把依据挤进去会被截成半句。
2. **图片进了 `docs/` 就至少要被引用一次。** 没人引用的图会变成孤儿（读者找不到、作者也不记得
   它对应哪一版）；设计归档用 [design/README.md](design/README.md) 里那张「文件 ↔ 版本 ↔ 状态」
   表挂住，新增图直接往表里加一行。
