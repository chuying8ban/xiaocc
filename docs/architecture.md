# 架构

## 一句话

```
sources/ ──StatusEvent──▶ engine ──Render──▶ backends/
                              ▲
                              │
                        characters（决定长什么样）
```

四层，三条边界，每条边界上只有一个数据结构。任何一层都可以被换掉，另外三层不需要知道。

## 为什么这么切

桌宠类项目最常见的死法是「三件事焊死」：状态获取、形象表现、窗口绘制写在同一个文件里。
表现是：换宿主就废、换形象要改代码、换平台得重写，最后没人敢动。

所以我们把**契约**定在三个真正稳定的地方：

| 问题 | 谁回答 | 契约 |
| --- | --- | --- |
| 现在在干什么？ | 状态源 | `StatusSource.poll() -> StatusEvent \| None` |
| 长什么样、怎么动？ | 角色包 | `character.json` 的 7 个状态表现 |
| 画在哪？ | 显示层 | `Backend.render(Render)` |

## 状态源（sources）

- 每轮 `poll()` 返回**当下**的状态即可，重复同一条没问题（引擎按内容去重）。
- 返回 `None` 表示「这一轮没话说」——注意它和 `offline` 不是一回事：
  `None` 什么都不改变，`offline` 会真的把桌宠切成离线脸。
- 允许抛异常。引擎会隔离它、记日志（`xiaocc run -v`）、并把它列进 `Engine.health()`，
  但**不会**让故障源抢走屏幕——屏幕上永远是其它源的真实状态。
- TTL 由状态本身决定（见 `protocol.STATE_TTL`），过期的状态自动退档，不退档就会
  「五分钟前就干完了还在转圈」，那是骗人。

## 角色包（characters）

一个目录 + `character.json`，必须为**全部 7 个状态**给出表现（`motion` + `accent`），
缺一个就 `CharacterError` —— 不许静默降级成空白。画布尺寸、调色板、
锚点都在清单里，所以同一套代码能跑任意角色。

角色包是数据，不是代码：第三方不必改核心就能发一个自己的小cc。

## 显示层（backends）

显示层只做三件事：画出来、动起来、随状态换色。它不读状态源、不判断业务，
所以换平台（macOS 原生 / Windows 原生 / Web / 终端）不用重写逻辑。

`console` 显示层不是玩具：它是无头环境下的验收手段，也是 CI 里端到端断言的抓手。

## 扩展一个状态源（完整例子）

```python
# my_pkg/pomodoro.py
from xiaocc.protocol import State, StatusEvent
from xiaocc.sources.base import StatusSource


class PomodoroSource(StatusSource):
    name = "pomodoro"
    description = "番茄钟：专注 25 分钟，休息 5 分钟"
    interval = 1.0

    def poll(self):
        ...
        return StatusEvent(source=self.name, state=State.WORKING, detail="专注中")
```

```toml
# my_pkg 的 pyproject.toml
[project.entry-points."xiaocc.sources"]
pomodoro = "my_pkg.pomodoro:PomodoroSource"
```

装完这个包，`xiaocc sources` 里就会多一项，核心代码一行没改。

## 目录

```
src/xiaocc/
  protocol.py     状态定义、优先级、TTL —— 全项目唯一契约
  engine.py       轮询、去重、故障隔离
  registry.py     扩展点发现（entry points + 内置表）
  characters.py   角色包加载与校验
  sources/        base / hermes / file / command
  backends/       base / console（+ 平台显示层）
  characters/     内置角色包
tests/            全部用例，含用临时 sqlite 复刻 Hermes 表结构的源测试
docs/sources.md   状态源开发指南（三分钟接一个新状态源）
docs/backends.md  显示层开发指南（契约、可复用的纯几何层、踩过的坑）
docs/PRIOR-ART.md 命名可用性与「参考 / 雷同」边界
docs/design/      形象稿与资产说明
docs/evidence/    真窗口截图与断言明细
```
