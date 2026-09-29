"""控制面板的窗口宿主：WKWebView 装在 NSPanel 里，宿主与页面用 messageHandler 通信。

为什么要一个宿主进程而不是把页面丢给浏览器：面板上要有**真能按的按钮**（刷新采集、
跳控制台、切皮肤）。浏览器打开的话这些按钮就只能干看着 —— 空壳子是明确不被接受的。

进程模型：面板是**独立进程**（``xiaocc panel``），不是一个常驻服务，也不住在桌宠的事件
循环里。原因有两条：

* 桌宠空闲 CPU 有 <5% 的硬线，网页渲染不该挤进它的循环；
* 页面崩了/卡了不该把桌宠带走。

桌宠那侧的入口只做一件事：写一个 request 文件（见 :func:`request_open`）——面板每 0.5s
看一眼，比自己的启动时间新就刷新并抬到前面；没有活着的面板就顺带把自己变成面板进程。
这样「点桌宠」在「面板开/没开」两种情况下都有反应，也不会开出一堆窗口。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .. import settings as settings_store
from ..quota import DEFAULT_QUOTA_PATH
from ..quota import refresh as refresh_quota
from .paths import (
    DEFAULT_PANEL_HTML,
    DEFAULT_PROBE_PATH,
    PANEL_REQUEST,
    PANEL_STATE,
    REQUEST_POLL_S,
    release_spawn_lock,
)
from .render import build_payload, render_html, write_panel

log = logging.getLogger("xiaocc.panel")

WINDOW_SIZE = (560.0, 660.0)
WINDOW_MIN = (420.0, 420.0)
#: 页面量出来多高，窗口就多高（上限）—— WKWebView 的字体度量跟浏览器不完全一样，
#: 写死高度会让底部在窗口里被裁掉一截（实测：浏览器里 533px 的页，窗口里要 700+）。
WINDOW_MAX_H = 980.0



# —— 窗口本体 ————————————————————————————————————————————————————————

def _load_kit():  # pragma: no cover - 需要窗口服务器
    import AppKit
    import objc
    import WebKit

    return AppKit, WebKit, objc


def open_panel(
    *,
    theme: str = "night",
    settings_path: Path | None = None,
    quota_path: Path | None = None,
    probe_path: Path | None = None,
    out_path: Path | None = None,
    request_path: Path | None = None,
) -> int:  # pragma: no cover - 需要窗口服务器
    """起窗口、跑事件循环，窗口关掉才返回（退出码 0）。

    一个进程一个窗口、**一页** —— 设置就长在额度页上（用户 2026-09-29 的指令：设置和控制面板
    是同一个东西），所以也没有「换页要不要开新窗口」那个坑了。
    否则「一次点击开三个窗口」那个坑（子生孙）会以另一种形状回来。
    """
    AppKit, WebKit, objc = _load_kit()

    quota_path = Path(quota_path or DEFAULT_QUOTA_PATH)
    probe_path = Path(probe_path or DEFAULT_PROBE_PATH)
    out_path = Path(out_path or DEFAULT_PANEL_HTML)
    request_path = Path(request_path or PANEL_REQUEST)
    settings_path = Path(settings_path) if settings_path else settings_store.settings_path()

    def render_page_now(theme_now: str) -> str:
        html = render_html(
            build_payload(
                quota_path=quota_path,
                probe_path=probe_path,
                theme=theme_now,
                settings_path=settings_path,
            )
        )
        try:
            write_panel(
                out_path,
                quota_path=quota_path,
                probe_path=probe_path,
                theme=theme_now,
                settings_path=settings_path,
            )
        except OSError:
            pass
        return html

    # 控制器必须是 NSObject 子类（要同时当 WKScriptMessageHandler、NSTimer 的 target、窗口 delegate）
    class Controller(AppKit.NSObject):  # type: ignore[misc]
        def initWithTheme_(self, initial_theme):
            # pyobjc 的 initWith… 惯用法必须重绑 self（同 ops/motion_positive_control.py 那条误报）
            self = objc.super(Controller, self).init()  # noqa: PLW0642
            if self is None:
                return None
            self._theme = initial_theme
            self._window = None
            self._web = None
            self._last_request = 0.0
            self._seen_request()
            return self

        def render_now(self) -> str:
            return render_page_now(self._theme)

        # —— 页面 → 宿主 —
        def webView_didFinishNavigation_(self, webview, _navigation):
            """页面加载完：滚动归零 + 把窗口调到内容高度。

            两个坑都踩过：①不归零时窗口一开就停在页面中段（顶栏和主数字都在视野外），看上去像渲染坏了；
            ②高度**量 documentElement.scrollHeight 是错的** —— 内容比视口短时，根元素的 scrollHeight
            按规范返回视口高度，所以窗口只能长不能缩（短页面底下永远空一大片）。量 ``.win`` 的
            实际高度才对，两个方向都能自适应。字体度量在 WKWebView 里与浏览器不同，写死高度会裁掉页脚。
            """

            def done(value, _error):
                try:
                    height = float(value)
                except (TypeError, ValueError):
                    return
                self._fit_height(height)

            webview.evaluateJavaScript_completionHandler_(
                "window.scrollTo(0, 0);"
                " Math.ceil((document.querySelector('.win') || document.body)"
                " .getBoundingClientRect().height)", done
            )

        def _fit_height(self, height: float) -> None:
            if self._window is None or height <= 0:
                return
            target = max(WINDOW_MIN[1], min(WINDOW_MAX_H, height))
            frame = self._window.frame()
            if abs(frame.size.height - target) < 1.0:
                return
            self._window.setContentSize_(AppKit.NSMakeSize(frame.size.width, target))

        def userContentController_didReceiveScriptMessage_(self, _ucc, message):
            body = message.body()
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except ValueError:
                    body = {"action": body}
            if not isinstance(body, dict):
                return
            action = body.get("action")
            if action == "refresh":
                self._refresh_async()
            elif action == "theme":
                value = body.get("value")
                if value in ("night", "paper"):
                    self._theme = value
                    self._write_state()
            elif action == "save":
                # 设置页点选项 → 落盘 → 重渲染（页面自己就是「当前的被选中」那份证据）
                self._save_settings(body)
            elif action == "close":
                self._close()
            elif action == "open":
                url = str(body.get("url") or "")
                if url.startswith(("http://", "https://")):
                    AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(url))

        def _save_settings(self, body: dict) -> None:
            updates = {k: body[k] for k in ("click_action",) if k in body}
            try:
                result = settings_store.save(updates, settings_path)
                log.info("设置已保存：%s", {k: result.get(k) for k in settings_store.KNOWN_KEYS})
            except OSError as exc:
                log.warning("设置写盘失败：%s", exc)
            self._reload()

        def windowWillClose_(self, _note):
            self._close()

        def tick_(self, _timer):
            """每 0.5s：桌宠那侧有没有新请求（点了桌宠）？有就刷新 + 抬到前面。"""
            if self._request_is_new():
                self._reload()
                self._raise()

        # —— 内部 —
        def _seen_request(self) -> float:
            try:
                return request_path.stat().st_mtime
            except OSError:
                return 0.0

        def _request_is_new(self) -> bool:
            """桌宠那侧写了新请求（点了桌宠，或右键菜单「打开控制面板」）？有就刷新 + 抬到前面。"""
            mtime = self._seen_request()
            if mtime <= self._last_request + 1e-6:
                return False
            self._last_request = mtime
            return True

        def _write_state(self) -> None:
            try:
                PANEL_STATE.parent.mkdir(parents=True, exist_ok=True)
                PANEL_STATE.write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "at": time.time(),
                            "theme": self._theme,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                os.chmod(PANEL_STATE, 0o600)
            except OSError:
                pass
            release_spawn_lock()  # 状态文件已就位 ⇒ 别人看得到「已有面板」，可以让别人抢了

        def _reload(self) -> None:
            if self._web is not None:
                self._web.loadHTMLString_baseURL_(self.render_now(), None)

        def _refresh_async(self) -> None:
            """刷新采集：网络那一刀放后台线程，别把窗口冻住。"""
            import threading

            from PyObjCTools import AppHelper

            def work() -> None:
                try:
                    refresh_quota(quota_path)
                except Exception:  # noqa: BLE001,S110 - 采集失败只该让页面显示「陈旧」
                    pass
                AppHelper.callAfter(self._reload)

            threading.Thread(target=work, daemon=True).start()

        def _raise(self) -> None:
            """抬到前面。**被收进 Dock 的也要拉回来** —— 实测用户会把挡事的窗口点小化，
            这时 makeKeyAndOrderFront 不动它（isVisible=False / isMiniaturized=True），
            再点桌宠就好像没反应。"""
            if self._window is None:
                return
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            if self._window.isMiniaturized():
                self._window.deminiaturize_(None)
            self._window.makeKeyAndOrderFront_(None)
            self._window.orderFrontRegardless()

        def _close(self) -> None:
            try:
                PANEL_STATE.unlink()
            except OSError:
                pass
            release_spawn_lock()
            AppKit.NSApp.terminate_(None)

        def _setup(self) -> None:
            rect = AppKit.NSMakeRect(0.0, 0.0, *WINDOW_SIZE)
            style = (
                AppKit.NSWindowStyleMaskTitled
                | AppKit.NSWindowStyleMaskClosable
                | AppKit.NSWindowStyleMaskMiniaturizable
                | AppKit.NSWindowStyleMaskResizable
            )
            # 用 NSWindow 而不是 NSPanel：面板是 Accessory 进程（不进 Dock），
            # NSPanel 在应用非活跃时会被 hidesOnDeactivate 收走 —— 实测窗口根本不出现
            # （进程活着、状态文件写了、窗口服务器里没有它）。NSWindow + orderFrontRegardless
            # 是实测能出来的一条路（/tmp/wk_probe.py 的最小复现）。
            window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, style, AppKit.NSBackingStoreBuffered, False
            )
            window.setTitle_("小cc · 控制面板")
            window.setReleasedWhenClosed_(False)
            window.setHidesOnDeactivate_(False)
            window.setMinSize_(AppKit.NSMakeSize(*WINDOW_MIN))
            window.setDelegate_(self)
            window.center()

            config = WebKit.WKWebViewConfiguration.alloc().init()
            try:  # 留着右键「检查元素」，以后调页面用得上
                config.preferences().setValue_forKey_(True, "developerExtrasEnabled")
            except Exception:  # noqa: BLE001,S110 - 私有键，失败无所谓
                pass
            config.userContentController().addScriptMessageHandler_name_(self, "xiaocc")

            web = WebKit.WKWebView.alloc().initWithFrame_configuration_(rect, config)
            web.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
            )
            web.setNavigationDelegate_(self)
            window.setContentView_(web)

            self._window = window
            self._web = web
            self._write_state()
            # 先把窗口摆上屏再灌页面：页面渲染失败也别让窗口"根本没出现"
            window.orderFrontRegardless()
            web.loadHTMLString_baseURL_(self.render_now(), None)

            AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                REQUEST_POLL_S, self, "tick:", None, True
            )
            window.makeKeyAndOrderFront_(None)

    app = AppKit.NSApplication.sharedApplication()
    # Accessory：有窗口、不进 Dock（桌宠是常驻小东西，不该多占一个 Dock 位）
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    controller = Controller.alloc().initWithTheme_(theme)
    controller._setup()
    app.activateIgnoringOtherApps_(True)

    from PyObjCTools import AppHelper

    AppHelper.runEventLoop()
    return 0
