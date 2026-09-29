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

#: **叶子命令**：``subscription`` 是命令组，光敲它会打印帮助并 exit 0（不是数据）
SUBSCRIPTION_ARGS = ("subscription", "status", "--plan", "token", "--format", "json")

#: 认出计划对象用的键（顶层/嵌套都能命中）
_PLAN_KEYS = (
    "totalCredits", "total_credits", "remainingCredits", "remaining_credits",
    "seatTiers", "seat_tiers", "addonRemaining", "addon_remaining",
    "remainingDays", "remaining_days", "planName", "plan_name", "subscribed",
)


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


def _find_plan(node: Any, depth: int = 2) -> dict[str, Any] | None:
    """在返回体里找那个「计划对象」（顶层包一层 ``data``/``plan`` 也算）。"""
    if not isinstance(node, dict) or depth < 0:
        return None
    if any(key in node for key in _PLAN_KEYS):
        return node
    for value in node.values():
        if isinstance(value, dict):
            found = _find_plan(value, depth - 1)
            if found is not None:
                return found
        elif isinstance(value, list):
            for item in value:
                found = _find_plan(item, depth - 1)
                if found is not None:
                    return found
    return None


def _credits_items(payload: Any) -> list[QuotaItem]:
    """``subscription status --plan token`` 的 Credits。

    **坑（@researcher 先踩、我从 CLI 二进制独立核过）**：``subscription`` 是命令组不是叶子，
    光敲 ``subscription --format json`` 会**打印帮助并 exit 0**——拿到的不是数据。叶子是
    ``subscription status --plan token``。

    归一化后的计划对象形如 ``{subscribed, planName, status, totalCredits, remainingCredits,
    usedPct, resetDate, remainingDays, seatTiers[], addonRemaining, period{...}}``，但
    ``totalCredits``/``remainingCredits`` 在**顶层和 ``seatTiers[]`` 里都有**、
    ``remainingDays`` 在**顶层和 ``period`` 里都有**、共享用量包在 ``addonRemaining``——
    所以四个位置都要兜；认不出就返回空，绝不编数。
    """
    plan = _find_plan(payload)
    if plan is None:
        return []
    items: list[QuotaItem] = []
    label = str(plan.get("planName") or "Credits")
    if plan.get("status") == "exhaust":
        items.append(QuotaItem(f"{label} 状态", "已用尽"))
    if plan.get("subscribed") is False and not plan.get("remainingCredits"):
        items.append(QuotaItem(f"{label} 状态", "未订阅"))

    total, remaining, used = _credits_from_seats(plan)
    if total is None:
        total = plan.get("totalCredits", plan.get("total_credits"))
        remaining = plan.get("remainingCredits", plan.get("remaining_credits"))
        used = plan.get("usedPct", plan.get("used_pct"))
    if _is_positive(total):
        if remaining is not None:
            items.append(QuotaItem(f"{label} 剩余", _num(remaining), ""))
        items.append(QuotaItem(f"{label} 总量", _num(total), ""))
        if used is not None:
            items.append(QuotaItem("已用", f"{_num(used)}%", ""))
    elif total is not None or remaining is not None:
        # CLI 自己的边界语义：total<=0 表示「不限量/按量」，**不是**余额为 0
        items.append(QuotaItem(f"{label} 额度", "按量/不限量（CLI 未报总量）"))

    addon = plan.get("addonRemaining")
    if _is_positive(addon):
        items.append(QuotaItem("共享用量包剩余", _num(addon), ""))
    days = plan.get("remainingDays")
    period = plan.get("period")
    if days is None and isinstance(period, dict):
        days = period.get("remainingDays")
    if days is not None:
        items.append(QuotaItem("套餐剩余", _num(days), "天"))
    return items


def _credits_from_seats(plan: dict[str, Any]) -> tuple[Any, Any, Any]:
    """``seatTiers[]`` 汇总（每个坐席一项：``totalCredits``/``remainingCredits``/``seats``）。"""
    tiers = plan.get("seatTiers") or plan.get("seat_tiers")
    if not isinstance(tiers, list) or not tiers:
        return None, None, None
    total = remaining = 0
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        total += tier.get("totalCredits") or 0
        remaining += tier.get("remainingCredits") or 0
    return total, remaining, None


def _is_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


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
            sub = self._runner([self._binary, *SUBSCRIPTION_ARGS], ctx.timeout_s)
            items += _credits_items(_extract_json(sub.stdout or ""))
            if getattr(sub, "returncode", 0) != 0:
                extra.append("subscription 查询失败")
        except Exception:  # noqa: BLE001 - 可选信息
            extra.append("subscription 未取到（命令组要敲到叶子：subscription status --plan token）")

        if not items:
            return self._result(STATE_UNKNOWN, detail="CLI 没报出任何额度行（可能账号没开通）")
        return self._result(STATE_OK, items=items, detail="；".join(extra) or None)
