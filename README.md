# 小cc

[English](README_EN.md) | 中文

> 住在桌面上的**状态伴侣**。谁都能驱动它，谁都能换掉它的皮，谁都能换掉它的窗口。

小cc 是独立项目，与 DeepSeek、Hermes Agent、Google、小米以及任何其它桌宠项目均无关联；
7 个状态词取自工作流的通用语义，不取自任何同类项目（边界与先例核查见 [docs/PRIOR-ART.md](docs/PRIOR-ART.md)）。

小cc 是一个开源、可扩展的桌面宠物：它把「某个工作流正在干什么」变成一个看得见的
小生命。默认接 Hermes，但状态源是插件——你的构建脚本、下载任务、番茄钟、CI，
只要写出符合协议的一行 JSON，就能让它动起来。

```
      ★
     ╱
   ( >  < )   小cc [working]  xiaocc · 正在执行 pytest  (3/5)
```

## 和别的桌宠有什么不一样

大多数桌宠项目（包括本人之前的尝试）都长这样：**一个宿主 + 一套贴图 + 一个窗口**，
三者焊死在一起。换宿主就废，换形象要改代码，换平台得重写。

小cc 把这三件事拆成了三个可替换的层，层与层之间只交换一份状态协议：

| 层 | 干什么 | 怎么扩展 |
| --- | --- | --- |
| **状态源** `sources/` | 回答「现在在干什么」 | 实现 `StatusSource.poll()`，声明 entry point `xiaocc.sources` |
| **角色包** `characters/` | 回答「长什么样、怎么动」 | 一个目录 + `character.json`，7 个状态全给表现即可换皮 |
| **显示层** `backends/` | 回答「画在哪」 | 实现 `Backend.render()`，声明 entry point `xiaocc.backends` |

核心包**零第三方依赖**（纯标准库），因为它只负责状态，不碰图形；
图形依赖挂在 `xiaocc[macos]` 这样的可选分组里，不存在「装不上就整只废掉」。

## 现在就能跑

```bash
git clone <repo> && cd xiaocc
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

xiaocc sources                 # 有哪些状态源
xiaocc run --source hermes     # 读 Hermes 的真实 Agent 活动（默认终端显示层）
xiaocc run --source 'file:~/.xiaocc/status.json'
```

真实输出（本机实测，当时另一个会话正在执行工具）：

```
( >  < ) 小cc [working]  xiaocc · 正在执行 read_file
( >  < ) 小cc [working]  编译中 · 2/5
```

第二条来自下面这条 `file:` 源 —— 任何脚本都能驱动它，不需要写一行 Python：

```bash
echo '{"state":"working","detail":"编译中","step":2,"total":5}' > ~/.xiaocc/status.json
```

## 状态协议

7 个状态，优先级从低到高：

| 状态 | 含义 | 默认存活 |
| --- | --- | --- |
| `offline` | 状态源全都没消息 | 不过期 |
| `done` | 刚干完一轮 | 12s |
| `idle` | 在线但没在忙 | 不过期 |
| `thinking` | 收到输入，正在想 | 90s |
| `working` | 正在执行工具/命令 | 45s |
| `waiting` | 在等你确认 | 10min |
| `error` | 出错了 | 60s |

三条硬规矩：

1. **不编数据。** 宿主没给进度，就不显示百分比（`step`/`total` 缺省即不显示）。
2. **不猜。** 过期（超过存活时间）就退档，不把「五分钟前的 working」当成还在干活。
3. **不连坐。** 一个状态源抛异常只记日志（`xiaocc run -v` 可见），其它源照常上屏。

想动手扩展：接一个新状态源看 [docs/sources.md](docs/sources.md)（三分钟，含可运行示例），
写一个新显示层看 [docs/backends.md](docs/backends.md)。

## 路线图

- [x] 协议 + 引擎 + 状态源（`hermes` / `file` / `command`）+ 角色包校验 + CI
- [x] macOS 原生显示层（透明、无边框、置顶、贴边隐藏、点击穿透）
- [x] 角色包定稿：方向 B「胶囊机甲丸丸」7 状态矢量稿
- [ ] Windows 原生显示层 / Web 显示层
- [ ] `waiting` 状态的真实来源（工具审批、待确认消息）
- [ ] `docs/` 的英文翻译
- [ ] 单文件打包（不带 Python 也能跑）

## 许可

MIT —— 见 [LICENSE](LICENSE)。角色形象与美术资源同样以 MIT 发布（见 `ASSET_LICENSE.md`）。
