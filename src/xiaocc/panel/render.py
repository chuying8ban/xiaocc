"""把 quota.json + 桌宠自证据渲染成控制面板那一页（纯函数，不碰 AppKit）。

分两层是为了可测：:func:`build_payload` 只读文件、:func:`render_html` 只做字符串替换 ——
所以「面板会不会打出 ¥0.00」「陈旧会不会显示旧数字」这类判据能在无头测试里断言，
不必起窗口。窗口那一层见 :mod:`xiaocc.panel.window`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..quota import DEFAULT_QUOTA_PATH
from ..quota.store import load as load_quota
from .paths import DEFAULT_PROBE_PATH

#: 页面的宿主标记：渲染时被替换成 ``window.__XIAOCC__ = {...}`` 的 JSON
PAYLOAD_PLACEHOLDER = "__XIAOCC_PAYLOAD__"

#: 两个页面共用一份皮肤（两处各写一份 CSS 迟早会长歪），渲染时内联进来。
#: 内联而不是 <link>：WKWebView 用 loadHTMLString 灌 HTML 时没有 baseURL，相对路径取不到文件。
STYLE_PLACEHOLDER = "__XIAOCC_STYLE__"
STYLE_PATH = Path(__file__).with_name("style.css")

#: 页名 → 模板。``panel`` 是额度/账本/桌宠现状那一页，``settings`` 是设置页。
PAGE_TEMPLATES = {
    "panel": Path(__file__).with_name("panel.html"),
    "settings": Path(__file__).with_name("settings.html"),
}
PAGES = tuple(PAGE_TEMPLATES)

TEMPLATE_PATH = PAGE_TEMPLATES["panel"]

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
    "fps",
    "at",
)


def _pet_snapshot(probe_path: Path) -> dict[str, Any]:
    """读桌宠自证据的**白名单子集**。文件不在/坏了 ⇒ 返回 ``{"alive": False}``，不抛。"""
    try:
        raw = json.loads(Path(probe_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"alive": False}
    if not isinstance(raw, dict):
        return {"alive": False}
    return {key: raw[key] for key in PET_FIELDS if key in raw}


def build_settings_payload(
    *,
    quota_path: Path = DEFAULT_QUOTA_PATH,
    probe_path: Path = DEFAULT_PROBE_PATH,
    theme: str = "night",
    settings_path: Path | None = None,
) -> dict[str, Any]:
    """设置页的数据：当前设置 + 一份「数据来源」小字（时效与口径和面板同源）。"""
    from .. import settings as settings_store
    from ..quota.badge import badge_text
    from ..quota.base import now_iso as _now_iso

    report, meta = load_quota(Path(quota_path))
    described = settings_store.describe(settings_path)
    return {
        "theme": theme if theme in ("night", "paper") else "night",
        "rendered_at": _now_iso(),
        "schema": described.get("schema", 1),
        "click_action": described.get("click_action"),
        "settings_path": str(settings_path or settings_store.settings_path()),
        "problems": described.get("problems") or [],
        "quota_meta": meta,
        "source_line": badge_text(report, meta),
        "pet": _pet_snapshot(Path(probe_path)),
    }


def build_payload(
    *,
    quota_path: Path = DEFAULT_QUOTA_PATH,
    probe_path: Path = DEFAULT_PROBE_PATH,
    theme: str = "night",
    now_iso: str | None = None,
) -> dict[str, Any]:
    """组装页面数据。**取不到就是取不到** —— 这里不补 0、不编数。"""
    report, meta = load_quota(Path(quota_path))
    from ..quota.base import now_iso as _now_iso

    return {
        "theme": theme if theme in ("night", "paper") else "night",
        "rendered_at": now_iso or _now_iso(),
        "quota": {"meta": meta, "report": report or {}},
        "pet": _pet_snapshot(Path(probe_path)),
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


def render_page(
    page: str = "panel",
    *,
    quota_path: Path = DEFAULT_QUOTA_PATH,
    probe_path: Path = DEFAULT_PROBE_PATH,
    theme: str = "night",
    settings_path: Path | None = None,
) -> str:
    """按页名渲染。面板与设置页共用皮肤，各自的模板 + 各自的 payload。"""
    if page not in PAGE_TEMPLATES:
        raise ValueError(f"没有这一页：{page!r}（有 {', '.join(PAGES)}）")
    if page == "settings":
        payload = build_settings_payload(
            quota_path=quota_path, probe_path=probe_path, theme=theme, settings_path=settings_path
        )
    else:
        payload = build_payload(quota_path=quota_path, probe_path=probe_path, theme=theme)
    return render_html(payload, template=PAGE_TEMPLATES[page])


def write_page(out: Path, page: str = "panel", **kwargs: Any) -> Path:
    """渲染某一页并原子落盘（0600：页面里带账户金额）。"""

    html = render_page(page, **kwargs)
    return _atomic_write(Path(out), html)


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
    page: str = "panel",
) -> Path:
    """渲染并落盘（0600：页面里带账户金额）。返回写好的路径。"""
    return write_page(
        Path(out), page, quota_path=quota_path, probe_path=probe_path, theme=theme
    )
