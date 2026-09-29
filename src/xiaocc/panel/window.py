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
from .render import build_payload, device_wait_remaining, render_html, write_panel

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

    #: 首帧那份 payload 里 CPU 是不是空的（空就补一帧，**不管是因为什么空**）
    first_paint: dict[str, bool] = {"cpu_missing": False}

    def render_page_now(theme_now: str, *, device_wait: bool = True) -> str:
        payload = build_payload(
            quota_path=quota_path,
            probe_path=probe_path,
            theme=theme_now,
            settings_path=settings_path,
            device_wait=device_wait,
        )
        first_paint["cpu_missing"] = bool(payload.get("device", {}).get("cpu_missing"))
        html = render_html(payload)
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
    #
    # 注意：**脚本消息不注册在 Controller 上**，而是交给下面那个专用桥 _Bridge —— 为什么见
    # _Bridge 的注释（2026-09-29 实测：多角色的 Controller 收不到消息，专用对象一注册就通）。
    class Controller(  # type: ignore[misc]
        AppKit.NSObject,
        protocols=[objc.protocolNamed("WKScriptMessageHandler")],  # type: ignore[call-arg]
    ):
        def initWithTheme_(self, initial_theme):
            # pyobjc 的 initWith… 惯用法必须重绑 self（同 ops/motion_positive_control.py 那条误报）
            self = objc.super(Controller, self).init()  # noqa: PLW0642
            if self is None:
                return None
            self._theme = initial_theme
            self._window = None
            self._web = None
            #: 页面→宿主那条消息链的专用桥（见 _Bridge）
            self._bridge = None
            self._last_request = 0.0
            self._seen_request()
            return self

        def render_now(self, *, device_wait: bool = True) -> str:
            return render_page_now(self._theme, device_wait=device_wait)

        def fillDevice_(self, _timer) -> None:
            """设备那格来补数：重渲染一次页面（此时基线够了，``get()`` 不会再阻塞）。"""
            if self._web is not None:
                self._web.loadHTMLString_baseURL_(self.render_now(), None)

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
                # **页面传来的对象是 NSDictionary，不是 Python dict**（PyObjC 那层桥不过度转换），
                # `isinstance(body, dict)` 直接把它挡在门外 ⇒ 面板里每个按钮都"点了没反应"。
                # 2026-09-29 与「handler 不能用 Controller 自己」一起构成了这个 bug 的全部病因。
                try:
                    body = dict(body)
                except (TypeError, ValueError):
                    log.warning("页面消息解析不了：%r", body)
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
            elif action == "control":
                self._control_(str(body.get("cmd") or ""))
            elif action == "open":
                url = str(body.get("url") or "")
                if url.startswith(("http://", "https://")):
                    AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(url))

        def _control_(self, cmd: str) -> None:
            # 名字**必须带尾下划线**：pyobjc 把 NSObject 子类的每个方法都当 ObjC 选择器，
            # `_control(self, cmd)`（无尾下划线）会被当成 0 参数选择器 ⇒ 调用即
            # `BadPrototypeError`，实测把整块面板打崩（消息桥那一条也一起没了）。
            """面板里的「重启小cc / 退出小cc」（用户 2026-09-29）。

            面板是**独立进程**，桌宠被 bootout 不会带走它 ⇒ 「退出」成功后自己也要收窗
            （否则屏幕上留一个「桌宠未运行」的孤窗，看起来像没退干净）。dry-run 是门禁用的，
            只记账不动手，所以那时也不收窗。
            """
            from .. import control

            if cmd not in control.ACTIONS:
                log.warning("控制：不认识的命令 %r", cmd)
                return
            ok, detail = control.perform(cmd)
            log.info("面板：%s ⇒ %s（%s）", cmd, "成功" if ok else "失败", detail)
            if ok and cmd == "quit" and not control.dry_run():
                # 给页面 0.6s 把「已发出」写出来，再收窗（用 lambda 收进 self：类体里的名字
                # 在方法里**不是**闭包变量，直接引 `_quit_self` 会 NameError）
                # 留 2 秒再收窗：那 2 秒里页面上正写着「回来：下次开机自动回来，或 ops/xiaoccctl start」
                # —— 当场收窗会把这句话一起吞掉，用户就只剩「窗口和桌宠都没了」
                AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                    2.0, False, lambda _t: self._close()
                )

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
            """每 0.5s：桌宠那侧有没有新请求？有就按请求里写的动作办。

            动作：``close`` = 桌宠要退出/重启了，面板一起收（**否则用户点了「退出小cc」会留个孤窗**）；
            其余（含老格式只有 ``at`` 的）= 刷新 + 抬到前面。
            """
            if self._request_is_new():
                if self._request_action() == "close":
                    log.info("收到收窗请求（桌宠退出/重启）⇒ 面板跟着收")
                    self._close()
                    return
                self._reload()
                self._raise()

        def _request_action(self) -> str:
            """请求文件里的动作词。读不到/没有这个键 ⇒ ``open``（老格式等价语义）。"""
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return "open"
            if not isinstance(payload, dict):
                return "open"
            return str(payload.get("action") or "open")

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
            web = WebKit.WKWebView.alloc().initWithFrame_configuration_(rect, config)
            # 注册在 **web 自己的** configuration 上（创建之后），handler 是**专用桥**
            # （不是 Controller，理由见 _Bridge 的注释）。
            if self._bridge is None:
                self._bridge = _Bridge.alloc().initWithController_(self)
            web.configuration().userContentController().addScriptMessageHandler_name_(
                self._bridge, "xiaocc"
            )
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
            # 首帧**不等设备采样**（CPU 要 1s 的 tick 窗口，在这儿等就是空窗口挂一秒、
            # 而且每次打开面板都要再付一次）。那一格先写「采集中」，基线够了再渲染一遍填真数。
            web.loadHTMLString_baseURL_(self.render_now(device_wait=False), None)
            # 补帧的判据是「**这一帧的 CPU 是空的**」，不是「等一会儿就能有」：计数器恰好在窗口
            # 那一刻卡住时（@coder 那条残留边界）只有按前者才会补，否则那一屏永久停在「未取到」。
            fill_delay = device_wait_remaining()
            if fill_delay > 0 or first_paint["cpu_missing"]:
                AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    max(fill_delay, 0.2) + 0.2, self, "fillDevice:", None, False
                )

            AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                REQUEST_POLL_S, self, "tick:", None, True
            )
            window.makeKeyAndOrderFront_(None)

    class _Bridge(  # type: ignore[misc]
        AppKit.NSObject,
        protocols=[objc.protocolNamed("WKScriptMessageHandler")],
    ):
        """页面 → 宿主那条消息链的**专用桥**（就是它去当 messageHandler，不是 Controller）。

        2026-09-29 实测（真窗口 + evaluateJavaScript 真点 + 三种对象对照）：
        同一个 Controller 既当 NSWindow 委托、又当 WKWebView 导航委托、又是 NSTimer 的 target
        时，它当 messageHandler **一条消息都收不到** —— 注册在创建前那份 config 上不行、
        注册在 `web.configuration()` 上不行、换个名字再注册也不行；而随便一个只干这一件事的
        NSObject 子类（函数内定义的、甚至不声明协议的）**注册完立刻就能收到**。
        所以用一个专用对象接，收到后直接调 Controller 的 Python 方法转发（同进程，纯 Python 调用）。
        """

        def initWithController_(self, controller):
            # pyobjc 的 initWith… 惯用法必须重绑 self
            self = objc.super(_Bridge, self).init()  # noqa: PLW0642
            if self is None:
                return None
            self._controller = controller
            return self

        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            log.info("页面消息：%s", message.body())
            self._controller.userContentController_didReceiveScriptMessage_(ucc, message)

    app = AppKit.NSApplication.sharedApplication()
    # Accessory：有窗口、不进 Dock（桌宠是常驻小东西，不该多占一个 Dock 位）
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    controller = Controller.alloc().initWithTheme_(theme)
    controller._setup()
    app.activateIgnoringOtherApps_(True)

    from PyObjCTools import AppHelper

    AppHelper.runEventLoop()
    return 0
