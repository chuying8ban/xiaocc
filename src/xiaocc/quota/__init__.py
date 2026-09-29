"""额度采集器：把「能拿到的真数」和「拿不到的」分开报，绝不合成一个假数。

用法（先这样，等 ops 的 cli.py 改动落地再接 `xiaocc quota` 子命令）::

    .venv/bin/python -m xiaocc.quota refresh     # 采集 + 落 ~/.xiaocc/quota.json
    .venv/bin/python -m xiaocc.quota show        # 读文件打表（不发网络请求）
    .venv/bin/python -m xiaocc.quota show --json # 原始 JSON

桌宠只读 `~/.xiaocc/quota.json`，**采集永远不会在 UI 循环里跑**（空闲 <5% 那条线不能被网络
轮询吃掉）；采集失败只让对应服务变 ``error``/``stale``，不影响别人、也不影响桌宠。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import ledger, store
from .base import (
    STATE_ERROR,
    QuotaContext,
    ServiceQuota,
    default_state_dbs,
    error_detail,
    hermes_env_file,
    now_iso,
    read_env_file,
)
from .deepseek import DeepSeekAdapter
from .qianwen import QwenTokenPlanAdapter
from .static import default_consoles

SCHEMA = 1

#: 采集结果落在哪。``XIAOCC_QUOTA_FILE`` 是**回归脚本的沙箱缝**：验证要拿一份假报告
#: 驱动桌宠的额度条，那条路径绝不能读用户真实的 ``~/.xiaocc/quota.json``
#: （同 anchor / probe / panel.request 那一套卫生红线）。
ENV_QUOTA_FILE = "XIAOCC_QUOTA_FILE"


def default_quota_path() -> Path:
    override = os.environ.get(ENV_QUOTA_FILE)
    return Path(override) if override else Path.home() / ".xiaocc" / "quota.json"


DEFAULT_QUOTA_PATH = default_quota_path()

__all__ = [
    "DEFAULT_QUOTA_PATH",
    "ENV_QUOTA_FILE",
    "SCHEMA",
    "collect",
    "default_adapters",
    "default_quota_path",
    "load",
    "refresh",
]


def default_adapters() -> list:
    """默认采集列表：DeepSeek（真余额）+ 千问云（官方 CLI）+ 两家只能看控制台的空态。"""
    return [DeepSeekAdapter(), QwenTokenPlanAdapter(), *default_consoles()]


def build_context(
    *,
    env: dict[str, str] | None = None,
    env_file: Path | None = None,
    state_dbs: Sequence[Path] | None = None,
    window_days: int = 30,
) -> QuotaContext:
    """默认从**默认 profile 的绝对路径**读密钥，不看 $HERMES_PROFILE / cwd。"""
    return QuotaContext(
        env=dict(env) if env is not None else read_env_file(env_file or hermes_env_file()),
        state_dbs=list(state_dbs) if state_dbs is not None else default_state_dbs(),
        window_days=window_days,
    )


def collect(*, adapters: Sequence | None = None, ctx: QuotaContext | None = None) -> dict[str, Any]:
    """采一版报告。**永不抛**：单条服务失败只标自己。"""
    context = ctx or build_context()
    services: list[dict[str, Any]] = []
    for adapter in adapters if adapters is not None else default_adapters():
        try:
            quota = adapter.fetch(context)
        except Exception as exc:  # noqa: BLE001 - 一个采集器炸了不许拖累别的
            quota = ServiceQuota(
                id=getattr(adapter, "id", "?"),
                name=getattr(adapter, "name", "?"),
                kind=getattr(adapter, "kind", "unknown"),
                state=STATE_ERROR,
                console=getattr(adapter, "console", ""),
                source="",
                fetched_at=now_iso(),
                detail=error_detail(exc),
            )
        services.append(quota.to_dict())

    try:
        books = ledger.snapshot(context.state_dbs, window_days=context.window_days)
    except Exception as exc:  # noqa: BLE001 - 账本读不动也不影响余额那几栏
        books = {"window_days": context.window_days, "generated_at": now_iso(),
                 "error": error_detail(exc), "default_profile": None, "all_profiles": None}
    return {"schema": SCHEMA, "updated_at": now_iso(), "services": services, "ledger": books}


def refresh(path: Path = DEFAULT_QUOTA_PATH, **kwargs) -> tuple[dict[str, Any], bool]:
    """采集 + 落盘，返回 (报告, 是否写成功)。"""
    report = collect(**kwargs)
    return report, store.save(report, path)


def load(path: Path = DEFAULT_QUOTA_PATH) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """给桌宠用的读口：报告 + 元信息（``stale`` 时 UI 该显示「陈旧」而不是旧数字）。"""
    return store.load(path)
