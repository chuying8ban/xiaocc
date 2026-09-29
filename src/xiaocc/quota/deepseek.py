"""DeepSeek：唯一一家有**真剩余**接口的服务。

`GET https://api.deepseek.com/user/balance` →
``{"is_available": true, "balance_infos": [{"currency": "CNY", "total_balance": "82.22",
"granted_balance": "0.00", "topped_up_balance": "82.22"}]}``

只依赖标准库 urllib（不引新依赖）；``opener`` 可注入，所以单测不需要网络，真调用由人跑。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import (
    STATE_ERROR,
    STATE_OK,
    STATE_UNKNOWN,
    QuotaContext,
    QuotaItem,
    ServiceQuota,
    error_detail,
    now_iso,
)

BALANCE_URL = "https://api.deepseek.com/user/balance"
CONSOLE_URL = "https://platform.deepseek.com/usage"
ENV_KEY = "DEEPSEEK_API_KEY"


def _opener_default(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_balance(
    api_key: str,
    *,
    opener=_opener_default,
    base_url: str = BALANCE_URL,
    timeout: float = 10.0,
) -> ServiceQuota:
    """取余额。任何失败都变成一条 ``state="error"`` 的结果，绝不抛。"""
    def result(state: str, items=None, detail=None, source=base_url) -> ServiceQuota:
        return ServiceQuota(
            id="deepseek",
            name="DeepSeek",
            kind="balance",
            state=state,
            items=items or [],
            console=CONSOLE_URL,
            source=source,
            fetched_at=now_iso(),
            detail=detail,
        )

    if not api_key:
        return result(STATE_UNKNOWN, detail="未配置 DEEPSEEK_API_KEY（见 ~/.hermes/.env）")

    request = urllib.request.Request(
        base_url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    )
    try:
        with opener(request, timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        return result(STATE_ERROR, detail=f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return result(STATE_ERROR, detail=error_detail(exc, 120))
    except Exception as exc:  # noqa: BLE001 - 采集器不许把异常抛给桌宠
        return result(STATE_ERROR, detail=error_detail(exc, 120))

    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
        infos = payload["balance_infos"]
    except Exception as exc:  # noqa: BLE001 - 坏 JSON / 结构不认识
        return result(STATE_ERROR, detail=error_detail(exc, 120) or "响应不是预期的 JSON 结构")

    if not isinstance(infos, list) or not infos:
        return result(STATE_ERROR, detail="响应里没有 balance_infos")

    items: list[QuotaItem] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        currency = str(info.get("currency") or "")
        total = info.get("total_balance")
        if total is None:
            continue
        items.append(QuotaItem("可用余额", str(total), currency))
        granted, topped = info.get("granted_balance"), info.get("topped_up_balance")
        if granted is not None:
            items.append(QuotaItem("其中赠送", str(granted), currency))
        if topped is not None:
            items.append(QuotaItem("其中充值", str(topped), currency))

    if not items:
        return result(STATE_ERROR, detail="响应里没有可显示的余额字段")

    available = payload.get("is_available")
    detail = None if available in (True, None) else f"接口报 is_available={available!r}"
    return result(STATE_OK, items=items, detail=detail)


class DeepSeekAdapter:
    id = "deepseek"
    name = "DeepSeek"
    kind = "balance"
    console = CONSOLE_URL

    def __init__(self, *, opener=_opener_default, base_url: str = BALANCE_URL) -> None:
        self._opener = opener
        self._base_url = base_url

    def fetch(self, ctx: QuotaContext) -> ServiceQuota:
        return fetch_balance(
            ctx.env.get(ENV_KEY, ""),
            opener=self._opener,
            base_url=self._base_url,
            timeout=ctx.timeout_s,
        )
