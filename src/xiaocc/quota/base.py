"""额度采集的公共类型与口径常量。

三条铁律（都是从今天真机上量出来的，不是设计洁癖）：

1. **「剩余额度」和「已用」是两个口径，不许并成一栏**：只有 DeepSeek 有真余额接口，
   其余几家的额度只在自家控制台里 —— 拿不到就报「未知」，**永远不许印 ¥0.00**
   （把「不知道」画成 0 和把额度画错一样是假数）。
2. **密钥只从默认 profile 的绝对路径读**：网关跑在默认 profile，密钥在 `~/.hermes/.env`，
   而调用方（launchd / runner）的 cwd 与环境可能是另一个 profile ⇒ 按「当前 profile」取会取到空。
3. 采集失败只影响它自己那一条，整体**永不抛**（面板读文件，读不到就显示陈旧/未知）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

#: 单条服务的状态：真数 / 陈旧（上次拿到了、这次没刷新）/ 未知（本来就拿不到）/ 出错（这次去取失败了）
STATE_OK = "ok"
STATE_STALE = "stale"
STATE_UNKNOWN = "unknown"
STATE_ERROR = "error"

#: 默认 profile 的家目录 —— **写死绝对路径**，不看 $HERMES_PROFILE、不看 cwd
DEFAULT_HERMES_DIR = Path.home() / ".hermes"
DEFAULT_HERMES_ENV = DEFAULT_HERMES_DIR / ".env"

#: 沙箱缝：只给回归/截图用，指向**替身**家目录。默认值永远是真的那份。
#:
#: 为什么必须有：面板「本机账本」一节的数字来自真账本，而对外截图**绝不能把作者真实调用次数、
#: 金额、真库路径拍进公开仓库**（跟运行时证据不入库是同一条规矩）；缝设错则是把真数的替身
#: 拍了个假数、或者反过来把真数拍出去 ⇒ 代价不对称，所以只认这一个显式变量名。
HERMES_DIR_ENV = "XIAOCC_HERMES_DIR"


def hermes_dir() -> Path:
    """默认 profile 的家目录；只有 ``XIAOCC_HERMES_DIR`` 显式设了才换（回归/截图用替身）。"""
    raw = os.environ.get(HERMES_DIR_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_HERMES_DIR


def hermes_env_file() -> Path:
    """密钥文件：默认那份 `.env`；沙箱里跟着替身家目录走。"""
    return hermes_dir() / ".env"

#: 账本：默认库 + **每个** profile 自己的 state.db（@writer/@researcher 实测只读默认库会漏约 41%）
#:
#: **不要在代码里写死 profile 名单**（这里原来是写死的 5 个名字，面板那边还写死"全部 6 库"）：
#: 新建或改名一个 profile 就会**静默漏掉**它的账本，页面照样说"全部 6 库"、一个错都不报。
#: 名单只有一个来源——磁盘上真实存在的 ``profiles/*/state.db``。
PROFILES_DIRNAME = "profiles"

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def now_iso() -> str:
    """本地时区的 ISO 时间戳（面板要给人看，不是给机器解析）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def profile_dbs(root_dir: Path | None = None) -> list[Path]:
    """磁盘上真实存在的 profile 账本（按名字排序）——**不依赖任何写死的名单**。"""
    root = Path(root_dir or hermes_dir()) / PROFILES_DIRNAME
    try:
        return sorted(p / "state.db" for p in root.iterdir() if (p / "state.db").exists())
    except OSError:
        return []


def default_state_dbs(root_dir: Path | None = None) -> list[Path]:
    """默认库 + 扫出来的各 profile 库；**数量由磁盘决定**，面板别再写死"6 库"。"""
    root = root_dir or hermes_dir()
    return [Path(root) / "state.db", *profile_dbs(root)]


def db_label(path: Path) -> str:
    """账本的显示名：profile 库叫它自己的名字，默认库叫 ``default``（面板直接拿来用）。"""
    path = Path(path)
    return path.parent.name if path.parent.parent.name == PROFILES_DIRNAME else "default"


def read_env_file(path: Path) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 文件（`.env`）。

    只做解析，不校验、不打印值、不认识 `export`——`.env` 里没有那东西，猜错了反而会把
    值截断。引号（成对的单/双引号）会剥掉。
    """
    env: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return env
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


@dataclass(frozen=True)
class QuotaItem:
    """一条可显示的额度/用量。``value`` 是**已经格式化好的字符串**，单位单列。

    ``value`` 用字符串而不是 float：这个面板要显示「未知」，而 float 没有「未知」这一档
    （NaN 会被各处悄悄写成 0.0）。
    """

    label: str
    value: str
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value, "unit": self.unit}


@dataclass
class ServiceQuota:
    """一个服务的一条结果。``kind``：``balance``=官方剩余、``used``=本机账本、``unknown``=拿不到。"""

    id: str
    name: str
    kind: str
    state: str
    items: list[QuotaItem] = field(default_factory=list)
    console: str = ""
    source: str = ""
    fetched_at: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "state": self.state,
            "items": [item.to_dict() for item in self.items],
            "console": self.console,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "detail": self.detail,
        }


@dataclass
class QuotaContext:
    """采集上下文：密钥来源、时间、账本路径、统计窗口。测试全部从这一个口注入。"""

    env: dict[str, str] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now().astimezone())
    state_dbs: list[Path] = field(default_factory=list)
    window_days: int = 30
    timeout_s: float = 10.0


class Adapter(Protocol):
    """一个服务的采集器。``fetch`` 只许返回 :class:`ServiceQuota`，抛异常由调用方兜成 error。"""

    id: str
    name: str
    kind: str
    console: str

    def fetch(self, ctx: QuotaContext) -> ServiceQuota:  # pragma: no cover - 协议
        ...


def error_detail(exc: BaseException, limit: int = 200) -> str:
    """异常 → 给面板看的一行原因。**只取类型名和首行消息，绝不塞响应体/密钥。**"""
    msg = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {msg}"[:limit]
