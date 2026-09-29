"""千问云 Token Plan：额度不在 HTTP 接口里，**在官方 CLI 里**。

用户的主力模型（qwen3.8-max）走这个计划，所以这一栏值得单独做，而不是丢给"看控制台"：

- 官方 CLI（`@qianwenai/qianwen-cli`，bin=`qianwen`）的 `usage free-tier --format json`
  返回 ``{"rows": [{"id":..., "model":..., "free_tier": {"mode": "standard"|"Only",
  "quota": {"status": "expire"|..., "total": N, "remaining": N, "unit": "..."}}}, ...],
  "total": ..., "hasQuota": bool}``（字段名从 CLI 二进制里核出来的，不是猜的）。
- **不能用 Token Plan 的 sk- key 认证**：`qianwen auth login` 走浏览器 device flow，凭据落
  ``~/.qianwen/`` ⇒ 未登录时只能是「未知」；**凭据在但被拒**（`AUTH_REQUIRED`，exit 2）
  则报 ``stale``（"上次拿到了、这次刷不出来"）——这也是全仓第一个 `stale` 的产出者。
- 只做**只读查询**：该计划官方明确"仅限交互式/智能体工具，禁自动化批量调用"，采集器不碰
  chat/completions。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .base import (
    STATE_ERROR,
    STATE_OK,
    STATE_STALE,
    STATE_UNKNOWN,
    QuotaContext,
    QuotaItem,
    ServiceQuota,
    error_detail,
    now_iso,
)

BINARY = "qianwen"
CREDENTIAL_DIR = Path.home() / ".qianwen"
CONSOLE_URL = "https://platform.qianwenai.com/"
LOGIN_HINT = "跑一次 `qianwen auth login`（浏览器批准一次即可）"


def _run_default(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout, check=False
    )


def _extract_json(text: str) -> Any:
    """CLI 可能先打一行横幅；取第一段 ``{`` 到最后一个 ``}`` 再解析。"""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出里没有 JSON")
    return json.loads(text[start : end + 1])


def _num(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _free_tier_items(payload: Any) -> list[QuotaItem]:
    """把 ``rows[].free_tier.quota`` 拉平成可显示条目（认不出的行直接跳过）。"""
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    items: list[QuotaItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tier = row.get("free_tier")
        quota = tier.get("quota") if isinstance(tier, dict) else None
        if not isinstance(quota, dict):
            continue
        model = str(row.get("model") or row.get("id") or "?").split("/")[-1]
        unit = str(quota.get("unit") or "")
        if quota.get("status") == "expire":
            items.append(QuotaItem(f"{model} 免费额度", "已过期", unit))
            continue
        remaining, total = quota.get("remaining"), quota.get("total")
        if remaining is not None:
            items.append(QuotaItem(f"{model} 剩余", _num(remaining), unit))
        if total is not None:
            items.append(QuotaItem(f"{model} 总量", _num(total), unit))
    return items


def _credits_items(payload: Any) -> list[QuotaItem]:
    """``subscription`` 的 Credits（Token Plan）。

    只看 ``totalCredits`` / ``remainingCredits`` / ``remainingDays`` 这几个已核到名字的键；
    真实嵌套结构要等一次登录后的真输出才能钉死，所以这里**认不出就返回空**，绝不编数。
    """
    if not isinstance(payload, dict):
        return []
    items: list[QuotaItem] = []
    remaining = payload.get("remainingCredits", payload.get("remaining_credits"))
    total = payload.get("totalCredits", payload.get("total_credits"))
    days = payload.get("remainingDays", payload.get("remaining_days"))
    if remaining is not None:
        items.append(QuotaItem("Credits 剩余", _num(remaining), ""))
    if total is not None:
        items.append(QuotaItem("Credits 总量", _num(total), ""))
    if days is not None:
        items.append(QuotaItem("套餐剩余", _num(days), "天"))
    return items


class QwenTokenPlanAdapter:
    """shell 调官方 CLI。``runner`` 可注入 ⇒ 单测不需要装 CLI、更不需要登录。"""

    id = "qwen_token_plan"
    name = "千问云 Token Plan"
    kind = "balance"
    console = CONSOLE_URL

    def __init__(
        self,
        *,
        runner: Callable[[Sequence[str], float], subprocess.CompletedProcess] | None = None,
        binary: str = BINARY,
        cred_dir: Path = CREDENTIAL_DIR,
    ) -> None:
        self._runner = runner or _run_default
        self._binary = binary
        self._cred_dir = Path(cred_dir)

    def _result(self, state: str, items=None, detail=None, source="qianwen CLI") -> ServiceQuota:
        return ServiceQuota(
            id=self.id,
            name=self.name,
            kind=self.kind,
            state=state,
            items=items or [],
            console=self.console,
            source=source,
            fetched_at=now_iso(),
            detail=detail,
        )

    def _logged_in(self) -> bool:
        """凭据目录存在 = 登录过（用来区分「从没登录」与「登录了但被拒」）。"""
        try:
            return self._cred_dir.exists()
        except OSError:
            return False

    def fetch(self, ctx: QuotaContext) -> ServiceQuota:
        args = [self._binary, "usage", "free-tier", "--format", "json"]
        try:
            proc = self._runner(args, ctx.timeout_s)
        except FileNotFoundError:
            return self._result(
                STATE_UNKNOWN,
                detail=f"未安装官方 CLI `{self._binary}`；装好后 {LOGIN_HINT}",
            )
        except Exception as exc:  # noqa: BLE001 - 采集器不许抛
            return self._result(STATE_ERROR, detail=error_detail(exc, 120))

        out = (proc.stdout or "") + (proc.stderr or "")
        rc = getattr(proc, "returncode", 0)
        if "AUTH_REQUIRED" in out or rc == 2:
            if self._logged_in():
                return self._result(
                    STATE_STALE, detail=f"凭据被拒（AUTH_REQUIRED）—— 重新登录：{LOGIN_HINT}"
                )
            return self._result(STATE_UNKNOWN, detail=f"未登录：{LOGIN_HINT}")
        if rc != 0:
            return self._result(STATE_ERROR, detail=f"CLI 退出码 {rc}")

        try:
            items = _free_tier_items(_extract_json(proc.stdout or ""))
        except Exception as exc:  # noqa: BLE001 - 结构不认识就是认不出，别编数
            return self._result(STATE_ERROR, detail=f"认不出 free-tier 输出：{error_detail(exc, 80)}")

        # Credits（订阅）是加分项：拿不到不影响上面那几条
        extra: list[str] = []
        try:
            sub = self._runner([self._binary, "subscription", "--format", "json"], ctx.timeout_s)
            items += _credits_items(_extract_json(sub.stdout or ""))
            if getattr(sub, "returncode", 0) != 0:
                extra.append("subscription 查询失败")
        except Exception:  # noqa: BLE001 - 可选信息
            extra.append("subscription 未取到")

        if not items:
            return self._result(STATE_UNKNOWN, detail="CLI 没报出任何额度行（可能账号没开通）")
        return self._result(STATE_OK, items=items, detail="；".join(extra) or None)
