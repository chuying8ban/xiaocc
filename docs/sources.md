# 状态源（source）开发指南：三分钟接一个新状态源

状态源是整个项目里唯一回答「现在在干什么」的地方。写一个类 + 声明一行 entry point，
`xiaocc sources` 里就会多一项，**核心代码一行不用改**。

先看要接什么：

| 你的情况 | 用哪个 | 要写代码吗 |
| --- | --- | --- |
| 脚本能写出一个 JSON | `file:` | 不用 |
| 有个命令/脚本能输出 JSON | `command:` | 不用 |
| 要读 API、数据库、socket、做判断 | 自己写一个源 | 三分钟 |
| 读 Hermes 的真实 Agent 活动 | `hermes`（内置） | 不用 |

---

**下面假设你已经 `source .venv/bin/activate`**（没激活就把 `xiaocc` 读成 `.venv/bin/xiaocc`）。

## 三十秒：不写代码的两条万能胶

```bash
# 1) 任何脚本只要能写这个文件，就能驱动小cc
echo '{"state":"working","detail":"编译中","step":2,"total":5}' > ~/.xiaocc/status.json
xiaocc run --source 'file:~/.xiaocc/status.json' -b terminal --once
```

```
( >  < ) 小cc [working]  编译中 · 2/5
```

```bash
# 2) 命令的 stdout 就是状态；退出码非 0 记成 error
xiaocc run --source 'command:mytool status --json' -b terminal --once
```

`file:` 的语义是「文件内容 = 当前状态」：文件没被改过不代表状态过期，要收工就让脚本写
`{"state":"idle"}`；文件被删掉则是 `offline`，不是崩溃。`command:` 的 stdout 为空表示
「这轮无话可说」，不是错误。

---

## 三分钟：写一个真源

### 1. 一个文件（这就是全部代码）

```python
# xiaocc_pomodoro.py
import time

from xiaocc.protocol import State, StatusEvent
from xiaocc.sources.base import StatusSource


class PomodoroSource(StatusSource):
    name = "pomodoro"                                    # 出现在 xiaocc sources / 日志里
    description = "番茄钟：专注 N 分钟，然后休息 5 分钟"   # 给人看的一句话
    interval = 1.0                                       # 建议轮询间隔（秒）

    def __init__(self, minutes: float = 25.0) -> None:   # 命令行的「名字:参数」进这里
        self.minutes = float(minutes)
        self.started = time.time()

    def poll(self):
        elapsed = time.time() - self.started
        if elapsed < self.minutes * 60:
            return StatusEvent(source=self.name, state=State.WORKING, project="pomodoro",
                               detail=f"专注中（还剩 {self.minutes * 60 - elapsed:.0f}s）")
        return StatusEvent(source=self.name, state=State.DONE, detail="这一轮结束了，起来走走")
```

### 2. 一行 entry point

```toml
# 你这个包的 pyproject.toml
[project.entry-points."xiaocc.sources"]
pomodoro = "xiaocc_pomodoro:PomodoroSource"
```

注意组名必须一字不差是 `xiaocc.sources`。写错了**不会报错**，只是它永远不出现在
`xiaocc sources` 里（扩展点发现失败只发 warning，见 `registry.py`）。

### 3. 装上，跑

```bash
cd /path/to/xiaocc && .venv/bin/pip install -e ../xiaocc_pomodoro --no-deps
```

仓库里有一份可以直接跑的完整版：`examples/pomodoro-source/`（照抄改改就是你的源）。

```bash
.venv/bin/pip install -e examples/pomodoro-source --no-deps

$ xiaocc sources
可用状态源：
  command    执行一条命令，解析 stdout 上的 JSON
  file       读取一个 JSON 状态文件（任何脚本都能写）
  hermes     读取 Hermes state.db，把真实 Agent 活动映射成状态（默认自动挑最新 profile）
  pomodoro   第三方状态源（xiaocc_pomodoro:PomodoroSource）      ← 它自己冒出来了

$ xiaocc run --source pomodoro -b terminal --once
( >  < ) 小cc [working]  pomodoro · 专注中（还剩 1500s）

$ xiaocc run --source 'pomodoro:0.02' -b terminal --once
( >  < ) 小cc [working]  pomodoro · 专注中（还剩 1s）
```

以上是本机实测输出。第三方源和内置源在命令行里完全同权：能省冒号（`--source pomodoro`，
走默认参数），也能带一个参数（`--source pomodoro:50`）。

---

## 参数怎么传

`名字:参数` 里的那一段，原样作为**第一个位置参数**交给你的构造函数。多参数自己拆，
别把 CLI 语法搞复杂：

```python
def __init__(self, spec: str = "25") -> None:      # --source 'pomodoro:25,bg'
    minutes, _, tail = spec.partition(",")
```

把参数写成可选（有默认值），这样不带冒号也能跑。

---

## 契约（只有这几条，记住就够）

| 规则 | 说明 |
| --- | --- |
| `poll()` 每轮返回**当下** | 重复返回同一条没问题，引擎按内容去重，不会重复重画 |
| 返回 `None` ≠ 报 `offline` | `None` = 这轮无话可说（不改变任何东西）；`offline` = 真的把桌宠切成离线脸 |
| 只有 7 个状态 | `idle/thinking/working/waiting/done/error/offline`；给别的值直接报错，不猜 |
| 进度只来自真实数字 | 没给 `step`/`total` 就不显示百分比，别自己造 |
| TTL 和优先级不归你管 | 由 `protocol.STATE_TTL` / `STATE_PRIORITY` 决定（`working` 45s、`error`/`thinking` 90s、`idle`/`offline` 不过期） |
| `interval` 决定节拍 | 引擎取**所有源里最小的**，下限 0.25s；`file` 0.75s、`command` 2s |
| 抛异常会被隔离 | 引擎记日志（`xiaocc -v`）+ `Engine.health()`，其它源照常上屏 |
| `close()` 可选 | 退出时收尾（关连接、落盘），基类默认什么都不做 |

**「抛异常」和「返回 ERROR 事件」不是一回事**：抛异常 = 这个源瞎了，别人替你上屏；返回
`ERROR` 事件 = 你确实报了一个错误，而 `error` 优先级最高，它会盖住其它所有源。后者要慎用，
只有「必须让人看见」才这么报。

---

## 调试

```bash
xiaocc sources                 # 我装了没？名字对不对？
xiaocc where                   # 关键路径（角色目录、状态文件、检测到的 state.db）
xiaocc -v run --source my_src  # -v 要放在 run 前面，状态源报错才打出来
xiaocc run --source my_src -b terminal --once   # 只跑一帧，适合脚本和 CI
```

在 `poll()` 里用 `logging.getLogger("xiaocc.sources.<名字>")` 打日志，**别 print**：
`console` 显示层每帧就写在 stdout，混在一起分不清哪行是状态、哪行是调试。

---

## 踩过的坑

| 坑 | 现象 | 怎么做 |
| --- | --- | --- |
| entry point 组名写错（`xiaocc.source`、`xiaocc_sources`） | 装上了、`xiaocc sources` 里却没有，也不报错 | 照抄 `xiaocc.sources`；装完先跑 `xiaocc sources` 确认 |
| `poll()` 里干重活（网络请求、扫目录） | 节拍被这一个源拖慢，整只桌宠卡住 | `poll()` 只读「早就准备好的值」，重活放后台线程，或干脆用 `file:` + 定时脚本 |
| 用了数据里的旧时间戳做 `at` | TTL 误判成过期，状态刚报就退档 | 用当前时间构造事件（`FileSource` 就为此显式把 `at` 换成 `time.time()`） |
| 在 shell 里写 `command:` 的 JSON | 双引号被 shell 吃掉 → `stdout 不是合法状态 JSON` | 把输出逻辑写成脚本文件，别在命令行里跟引号搏斗 |
| 坏源每轮刷一条 ERROR | 日志/画面被刷屏 | 同一个故障只报一次（内置源都这么做），详见 `CommandSource._error` |
| 一个永远坏掉的源挂在命令行上 | 它以最高优先级抢镜，好源也看不见 | 不在乎的源就别挂；或者让它返回 `None` |
| `--source 名字` 报「要写成 '名字:参数'」 | 你的构造函数参数没有默认值 | 给参数加默认值，或者老实写 `名字:参数` |
| 在 `poll()` 里改共享状态 | 多源合并时行为诡异 | `StatusEvent` 是 frozen 的，源之间不要互相写对方的数据 |

---

## 想把自己的源发给别人用

1. 单独发一个包（别往主仓库塞），声明 `[project.entry-points."xiaocc.sources"]`；
2. `description` 写一句人话——它直接出现在 `xiaocc sources` 的列表里；
3. 带上测试：`poll()` 的正常路径 + 「坏数据只报一次」+ 「异常被隔离」，照 `tests/test_sources.py` 抄；
4. 想进主仓库的 `examples/`，就按 `examples/pomodoro-source/` 的规格来（单文件、零依赖、注释说清楚它在演示什么）。
