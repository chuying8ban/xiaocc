# State source guide: add one in three minutes

[中文](sources.md) | English

A state source is the only part of this project that answers "what is happening right now". You write
one class, declare one entry point, and the source shows up in `xiaocc sources`. Nothing in the core
gets edited.

Decide which kind you need:

| Your situation | Use | Code to write |
| --- | --- | --- |
| a script can write a JSON file | `file:` | none |
| a command or script can print JSON | `command:` | none |
| you need to read an API, a database, a socket, or make a judgement | write your own source | three minutes |
| read live Hermes agent activity | `hermes` (built in) | none |

---

**Everything below assumes you ran `source .venv/bin/activate`.** If you did not, read `xiaocc` as
`.venv/bin/xiaocc`.

## Thirty seconds: two sources that need no code

```bash
# 1) any script that can write this file can drive the pet
echo '{"state":"working","detail":"编译中","step":2,"total":5}' > ~/.xiaocc/status.json
xiaocc run --source 'file:~/.xiaocc/status.json' -b terminal --once
```

```
( >  < ) 小cc [working]  编译中 · 2/5
```

```bash
# 2) the command's stdout is the state
xiaocc run --source 'command:mytool status --json' -b terminal --once
```

`file:` means the file's contents are the current state. A file nobody has touched since is not a
stale state, so to call it a day have the script write `{"state":"idle"}`. A deleted file reports
`offline` instead of crashing. An empty stdout from `command:` means "nothing to say this round",
which is not an error.

**A broken source is not a state.** An unreadable file, a missing command, a timeout, a non-zero exit
code, stdout that is not valid JSON: those are mechanical failures. The reason goes to the log
(`xiaocc -v`) and to `Engine.health()`, and it does not change the pet's face. One broken source has no
business covering up the healthy ones. If nothing else is talking either, the failure shows up as
`offline` with the reason in `detail`. The `error` state is reserved for the workflow itself reporting
a failure, which in practice means a payload containing `state=error`.

```
# measured on this machine: a broken command as the only source on screen
# (with another source talking, this line never appears)
$ xiaocc run --source 'command:没有这个命令' -b terminal --once
WARNING xiaocc.engine: 状态源 command 出错：FileNotFoundError: 命令不存在：没有这个命令
( -  - )z 小cc [offline]  状态源故障：command FileNotFoundError: 命令不存在：没有这个命令
```

A source handles a failure by **raising**, not by returning an `ERROR` event. The engine isolates it,
dedupes by message so one failure writes one log line, and falls back to `offline` when nothing else is
talking. A source does not need to remember what it already reported.

---

## Three minutes: write a real source

### 1. One file, and that is the whole implementation

```python
# xiaocc_pomodoro.py
import time

from xiaocc.protocol import State, StatusEvent
from xiaocc.sources.base import StatusSource


class PomodoroSource(StatusSource):
    name = "pomodoro"                                   # shows up in xiaocc sources and in logs
    description = "番茄钟：专注 N 分钟，然后休息 5 分钟"  # one line for humans
    interval = 1.0                                      # suggested poll interval, seconds

    def __init__(self, minutes: float = 25.0) -> None:  # the "name:argument" from the CLI lands here
        self.minutes = float(minutes)
        self.started = time.time()

    def poll(self):
        elapsed = time.time() - self.started
        if elapsed < self.minutes * 60:
            return StatusEvent(source=self.name, state=State.WORKING, project="pomodoro",
                               detail=f"专注中（还剩 {self.minutes * 60 - elapsed:.0f}s）")
        return StatusEvent(source=self.name, state=State.DONE, detail="这一轮结束了，起来走走")
```

### 2. One entry point

```toml
# in your package's pyproject.toml
[project.entry-points."xiaocc.sources"]
pomodoro = "xiaocc_pomodoro:PomodoroSource"
```

The group name has to be exactly `xiaocc.sources`. Get it wrong and nothing complains; your source
simply never appears in `xiaocc sources`, because a failed extension lookup only emits a warning (see
`registry.py`).

### 3. Install it and run

```bash
cd /path/to/xiaocc && .venv/bin/pip install -e ../xiaocc_pomodoro --no-deps
```

There is a complete runnable version in `examples/pomodoro-source/`. Copy it and change the insides.

```bash
.venv/bin/pip install -e examples/pomodoro-source --no-deps

$ xiaocc sources
可用状态源：
  command    执行一条命令，解析 stdout 上的 JSON
  file       读取一个 JSON 状态文件（任何脚本都能写）
  hermes     读取 Hermes state.db，把真实 Agent 活动映射成状态（默认自动挑最新 profile）
  pomodoro   第三方状态源（xiaocc_pomodoro:PomodoroSource）      ← it found itself

$ xiaocc run --source pomodoro -b terminal --once
( >  < ) 小cc [working]  pomodoro · 专注中（还剩 1500s）

$ xiaocc run --source 'pomodoro:0.02' -b terminal --once
( >  < ) 小cc [working]  pomodoro · 专注中（还剩 1s）
```

These are real runs, pasted from the machine this was written on. The CLI and the bundled sources
print in Chinese, and the example above keeps Chinese runtime strings, which is why the output is
Chinese. The row a source gets in `xiaocc sources`, its captions, and its log lines are all yours to
write in whatever language you like. Third-party sources are equals of the built-ins on the command
line: the colon and its argument are optional (`--source pomodoro` uses your defaults), and one
argument is passed through (`--source pomodoro:50`).

---

## How arguments travel

Whatever follows the colon is handed to your constructor as the **first positional argument**. Split
multiple values yourself, and keep the CLI syntax boring:

```python
def __init__(self, spec: str = "25") -> None:      # --source 'pomodoro:25,bg'
    minutes, _, tail = spec.partition(",")
```

Give the argument a default so the source also runs without a colon.

---

## The contract

| Rule | What it means |
| --- | --- |
| `poll()` returns the **current** state | Returning the same event again is fine; the engine dedupes by content and will not redraw |
| `None` is not `offline` | `None` = nothing to say this round, changing nothing; `offline` = actually switch the pet to the offline face |
| Seven states, no more | `idle/thinking/working/waiting/done/error/offline`; anything else raises instead of being guessed |
| Progress comes from real numbers | No `step`/`total`, no percentage. Do not invent one |
| TTL and priority are not yours to set | They come from `protocol.STATE_TTL` and `STATE_PRIORITY` (`working` 45s, `error`/`thinking` 90s, `idle`/`offline` never expire) |
| `interval` sets the clock | The engine uses the **smallest** interval among all sources, floor 0.25s; `file` is 0.75s, `command` is 2s |
| A raised exception is isolated | The engine logs it (`xiaocc -v`) and lists it in `Engine.health()`; the other sources keep drawing |
| A mechanical failure never reaches the screen | Unreadable file, missing or timed-out command, non-zero exit code, invalid JSON → log and `health()` only; it shows as `offline` with the reason in `detail` when nothing else is talking |
| `close()` is optional | Clean up on exit (close connections, flush state); the base class does nothing |

Keep three things apart:

- **A raised exception** means the source itself is broken. The engine isolates it and someone else
  draws the pet.
- **A mechanical failure** (unreadable file, missing or timed-out command, non-zero exit, output that
  is not JSON) also stays off the screen; the reason goes to the log and `health()`, and it only
  surfaces as `offline` when no other source is talking.
- **An `ERROR` event** means the workflow itself failed, which you report as `state=error` in the
  payload. `error` outranks everything and will cover the other sources, so send it only when a human
  has to see it.

---

## Debugging

```bash
xiaocc sources                 # did my package install? is the name right?
xiaocc where                   # key paths: character dirs, status file, detected databases
xiaocc -v run --source my_src  # -v goes before run, otherwise source errors stay silent
xiaocc run --source my_src -b terminal --once   # one frame, which is what scripts and CI want
```

Log from inside `poll()` with `logging.getLogger("xiaocc.sources.<name>")`, and do not `print`. The
`console` backend writes one line per frame to stdout, and mixed output makes it impossible to tell a
state from a debug line.

---

## Mistakes people make

| Mistake | What you see | What to do |
| --- | --- | --- |
| Entry point group misspelled (`xiaocc.source`, `xiaocc_sources`) | It installs, and it never shows up in `xiaocc sources`, with no error | Copy `xiaocc.sources` exactly, and run `xiaocc sources` right after installing |
| Heavy work inside `poll()` (network calls, directory scans) | That one source drags the clock down and the pet freezes | `poll()` should read a value somebody else already prepared; push the work to a thread, or use `file:` plus a scheduled script |
| Using a stale timestamp as `at` | The TTL judges the event expired and it drops the moment it arrives | Build the event with the current time (`FileSource` explicitly replaces `at` with `time.time()` for this reason) |
| Writing `command:` JSON straight into the shell | The shell eats the double quotes, so the output is not valid JSON and the failure only appears in `xiaocc -v` | Put the output in a script file instead of fighting quote rules |
| A broken source logging every round | The log becomes noise | One failure writes one line: the engine dedupes by message (`Engine.tick()`), so a source does not need to remember what it already said |
| Leaving a permanently broken source on the command line | Its mechanical failure stays in the log and the screen is unaffected | To make it visible, report `state=error` in the payload. That is the only way to take over the screen |
| `--source name` complains about needing `name:argument` | Your constructor argument has no default | Give it a default, or write `name:argument` |
| Mutating shared state inside `poll()` | Merging several sources behaves strangely | `StatusEvent` is frozen on purpose; sources should not write into each other's data |

---

## Shipping your source to other people

1. Publish it as its own package rather than filing it into this repo, and declare
   `[project.entry-points."xiaocc.sources"]`.
2. Write a real sentence for `description`. It is what appears in the `xiaocc sources` listing.
3. Bring tests: the happy path of `poll()`, "a bad payload is reported once", and "an exception is
   isolated". Copy the shape from `tests/test_sources.py`.
4. To land in this repo's `examples/`, follow `examples/pomodoro-source/`: one file, no dependencies,
   and comments that say what the example demonstrates.
