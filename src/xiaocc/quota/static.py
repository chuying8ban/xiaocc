"""拿不到余额的那几家：只报「未知」+ 控制台直达链接。

**为什么单独一个模块**：这几条最容易在实现里被「补一个 0」——面板上 ¥0.00 和
「额度在别人控制台里」是同一类假数。这里连 :class:`QuotaItem` 都不给，``items=[]``
让 UI 只能显示「未知」，物理上做不到印 0。
"""

from __future__ import annotations

from .base import STATE_UNKNOWN, QuotaContext, ServiceQuota, now_iso


class ConsoleOnlyAdapter:
    """形态和别的适配器一致，但永远返回 unknown（本机没有合法取数路径）。"""

    kind = "unknown"

    def __init__(self, service_id: str, name: str, console: str, why: str) -> None:
        self.id = service_id
        self.name = name
        self.console = console
        self._why = why

    def fetch(self, ctx: QuotaContext) -> ServiceQuota:
        return ServiceQuota(
            id=self.id,
            name=self.name,
            kind=self.kind,
            state=STATE_UNKNOWN,
            items=[],
            console=self.console,
            source="控制台",
            fetched_at=now_iso(),
            detail=self._why,
        )


def default_consoles() -> list[ConsoleOnlyAdapter]:
    """@researcher 核实过的、本机没有合法取数路径的那几家。

    （千问云 Token Plan 曾经也在这里，现在有自己的适配器了 —— 它走官方 CLI，
    见 :mod:`xiaocc.quota.qianwen`。）
    """
    return [
        ConsoleOnlyAdapter(
            "dashscope",
            "百炼 DashScope",
            "https://bailian.console.aliyun.com/",
            "per-key 无余额接口（/usage、/quota、/user/balance 等 7 条路径实测 404）；"
            "账户级余额需 BSS AK/SK 签名，且账户总额度≠百炼套餐余量",
        ),
        ConsoleOnlyAdapter(
            "nous",
            "Nous Portal",
            "https://portal.nousresearch.com/",
            "无公开额度接口，本机 credential_pool.nous 为空 ⇒ 需登录后看 Credits",
        ),
    ]
