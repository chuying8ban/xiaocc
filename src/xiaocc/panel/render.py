"""把 quota.json + 桌宠自证据渲染成控制面板那一页（纯函数，不碰 AppKit）。

分两层是为了可测：:func:`build_payload` 只读文件、:func:`render_html` 只做字符串替换 ——
所以「面板会不会打出 ¥0.00」「陈旧会不会显示旧数字」这类判据能在无头测试里断言，
不必起窗口。窗口那一层见 :mod:`xiaocc.panel.window`。

**设置也在这页里**（用户 2026-09-29 的指令：设置和控制面板是同一个东西），所以 payload 里
带上了当前设置 + 可选项 + 「气泡里现在会写什么」的实时预览 —— 改完当场能看到效果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..quota import DEFAULT_QUOTA_PATH
from ..quota.store import load as load_quota
from .paths import DEFAULT_PROBE_PATH

#: 页面的宿主标记：渲染时被替换成 ``const DATA = {...}`` 的 JSON
PAYLOAD_PLACEHOLDER = "__XIAOCC_PAYLOAD__"

#: 皮肤（渲染时内联）。内联而不是 <link>：WKWebView 用 loadHTMLString 灌 HTML 时没有
#: baseURL，相对路径取不到文件。
STYLE_PLACEHOLDER = "__XIAOCC_STYLE__"
STYLE_PATH = Path(__file__).with_name("style.css")

TEMPLATE_PATH = Path(__file__).with_name("panel.html")

#: 桌宠自证据里允许进面板的字段（白名单：probe.json 是诊断文件，别整份塞进网页）
PET_FIELDS = (
    "alive",
    "pid",
    "state",
    "art",
    "caption",
    "state_source",
    "state_changed_ago_s",
    "ns_frame",
    "view_size",
    "dock",
    "anchor",
    "anchor_ok",
    "anchor_state",
    "pump_loops_per_sec",
    "paints_per_sec",
    "fps",
    "at",
    # 手势/气泡这几项：面板要把「单击现在会做什么、气泡正在写什么」显示出来，
    # 不然用户改了设置、页面里没有任何地方能确认它生效了。
    "click_action",
    "badge_drawn",
    "badge_alpha",
)


def _pet_snapshot(probe_path: Path) -> dict[str, Any]:
    """读桌宠自证据的**白名单子集**。文件不在/坏了 ⇒ 返回 ``{"alive": False}``，不抛。

    **白名单之外一个字段都不加**（连路径都不加）：probe.json 是诊断文件，页面上用到什么就
    只带什么 —— `tests/test_panel.py` 拿两个断言钉着这条。路径要显示的话，在页面里由
    payload 的别处带出来，别从这里夹带。
    """
    try:
        raw = json.loads(Path(probe_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"alive": False}
    if not isinstance(raw, dict):
        return {"alive": False}
    return {key: raw[key] for key in PET_FIELDS if key in raw}


def build_payload(
    *,
    quota_path: Path = DEFAULT_QUOTA_PATH,
    probe_path: Path = DEFAULT_PROBE_PATH,
    theme: str = "night",
    now_iso: str | None = None,
    settings_path: Path | None = None,
) -> dict[str, Any]:
    """组装页面数据。**取不到就是取不到** —— 这里不补 0、不编数。"""
    from .. import settings as settings_store
    from ..quota.badge import badge_text
    from ..quota.base import now_iso as _now_iso

    report, meta = load_quota(Path(quota_path))
    described = settings_store.describe(settings_path)
    return {
        "theme": theme if theme in ("night", "paper") else "night",
        "rendered_at": now_iso or _now_iso(),
        "quota": {"meta": meta, "report": report or {}},
        "pet": _pet_snapshot(Path(probe_path)),
        "settings": {
            "path": str(settings_path or settings_store.settings_path()),
            "schema": described.get("schema", 1),
            "click_action": described.get("click_action"),
            "actions": list(settings_store.CLICK_ACTIONS),
            "labels": dict(settings_store.CLICK_ACTION_LABELS),
            "problems": described.get("problems") or [],
            # 预览用**单行**那套口径（同一份报告、同一套陈旧判据）——面板说陈旧、气泡还在报数
            # 就是自相矛盾，所以预览与气泡必须同源
            "bubble_preview": badge_text(report, meta),
        },
    }


def render_html(payload: dict[str, Any], *, template: Path | None = None) -> str:
    """把数据塞进模板（皮肤也一起内联）。JSON 里的 ``</`` 会被转义，免得提前关掉 <script>。"""
    target = Path(template or TEMPLATE_PATH)
    text = target.read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    if PAYLOAD_PLACEHOLDER not in text:
        raise ValueError(f"模板里找不到注入点 {PAYLOAD_PLACEHOLDER}：{target}")
    if STYLE_PLACEHOLDER in text:
        text = text.replace(STYLE_PLACEHOLDER, STYLE_PATH.read_text(encoding="utf-8"))
    return text.replace(PAYLOAD_PLACEHOLDER, blob)


def _atomic_write(out: Path, html: str) -> Path:
    import os
    import tempfile

    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(html)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out


def write_panel(
    out: Path,
    *,
    quota_path: Path = DEFAULT_QUOTA_PATH,
    probe_path: Path = DEFAULT_PROBE_PATH,
    theme: str = "night",
    settings_path: Path | None = None,
) -> Path:
    """渲染并落盘（0600：页面里带账户金额）。返回写好的路径。"""
    html = render_html(
        build_payload(
            quota_path=quota_path,
            probe_path=probe_path,
            theme=theme,
            settings_path=settings_path,
        )
    )
    return _atomic_write(Path(out), html)
