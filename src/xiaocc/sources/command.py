"""命令状态源：跑一条命令，读它 stdout 上的 JSON。

给「不想装 Python 也不想让对方读文件」的场合用：任何语言的脚本或一行 shell 都行。

    xiaocc watch --source 'command:cat ~/.cache/build-status.json'
    xiaocc watch --source 'command:mytool status --json'

约定（违反就抛异常交给引擎隔离，不猜、不自己造画面）：
  * 退出码 0 且 stdout 有 JSON 对象  → 该状态（载荷自报 state=error 也照演，那是工作流的错）
  * 退出码 0 且 stdout 为空          → 这条源本轮无话可说（None）
  * 命令跑不起来 / 超时 / 退出码非 0 / stdout 不是合法状态 JSON → 机械故障，抛异常
"""

from __future__ import annotations

import json
import shlex
import subprocess

from ..protocol import StatusEvent
from .base import StatusSource

__all__ = ["CommandSource"]


class CommandSource(StatusSource):
    """执行一条命令，把 stdout 上的 JSON 当作状态。

    机械故障（命令本身坏了：不存在、起不来、超时、退出码非零、输出不是合法状态
    JSON）一律**抛异常**，绝不自己造 ``ERROR`` 事件；``ERROR`` 只留给工作流自报
    （载荷里 ``state == "error"``，那是被监控方真出了问题，桌宠该演出来）。

    为什么要分这条线：``ERROR`` 是画面上的一种状态，观众看到的是桌宠在演；而
    「命令坏了」是这条源自己的健康问题，把它演出来只会让用户看见一张来历不明的
    哭脸，分不清是构建炸了还是脚本路径写错了。异常抛出去之后引擎会接手处理：
    捕获 → 按消息内容去重后 ``log.warning`` → 记进 ``health()``（``run -v`` 可查）
    → **不产生事件**；只有在没有任何源说话时才用 ``offline`` 兜底，并把故障原因
    写进 detail。也就是说，「不进画面、但要留痕、还要可诊断」这套语义引擎已经
    实现好了，源这边自己造事件反而把它绕过去了。
    """

    name = "command"
    interval = 2.0
    description = "执行一条命令，解析 stdout 上的 JSON"

    def __init__(
        self,
        command: str,
        *,
        timeout: float = 5.0,
        source_name: str | None = None,
    ) -> None:
        self.command = command
        self.timeout = timeout
        self._argv = shlex.split(command)
        if not self._argv:
            raise ValueError("命令不能为空")
        if source_name:
            self.name = source_name

    @property
    def label(self) -> str:
        return f"command:{self.command}"

    def poll(self) -> StatusEvent | None:
        try:
            proc = subprocess.run(
                self._argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"命令不存在：{self._argv[0]}") from exc
        except OSError as exc:
            # 权限不够、不是可执行文件之类：起不来就是起不来，别让引擎猜
            raise OSError(f"命令起不来：{self.command}（{exc}）") from exc
        except subprocess.TimeoutExpired:
            # 原样抛：异常消息里已经带了命令和超时秒数，够定位了
            raise

        if proc.returncode != 0:
            stderr_lines = [
                line.strip()
                for line in (proc.stderr or proc.stdout or "").splitlines()
                if line.strip()
            ]
            hint = " / ".join(stderr_lines[:3])
            if len(hint) > 120:  # detail 要拼进一行给用户看，别把整段 traceback 塞进去
                hint = hint[:117] + "…"
            reason = f"命令退出码 {proc.returncode}：{self.command}"
            if hint:
                reason += f"（{hint}）"
            raise RuntimeError(reason)

        text = (proc.stdout or "").strip()
        if not text:
            return None
        try:
            event = StatusEvent.from_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"命令输出不合法：{exc}") from exc

        return StatusEvent(
            source=self.label,
            state=event.state,
            at=event.at,
            detail=event.detail,
            project=event.project,
            step=event.step,
            total=event.total,
            raw=event.raw,
        )
