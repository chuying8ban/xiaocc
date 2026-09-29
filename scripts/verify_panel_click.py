"""面板按钮**真能按**吗？—— 真 WKWebView + 真 evaluateJavaScript 点击 + 落盘断言。

为什么非有不可（2026-09-29 用户报「面板里那三个按钮点不了」）：这个 bug 有两个病因，
**两个都不会让任何东西报错** —— 页面看着好好的、`postMessage` 不抛、Python 侧
`respondsToSelector` 也是 True、`window.webkit.messageHandlers.xiaocc` 还是个对象，
但消息一条都到不了 / 到了被静默丢掉。只有「真点一下、看设置有没有落盘」能抓到：

* ① messageHandler 注册在 Controller 自己身上 ⇒ WebKit 一条都不投递
     （对照：随便一个只干这一件事的 NSObject 子类一注册就通）⇒ 改用专用桥 ``_Bridge``；
* ② 页面传来的是 **NSDictionary**，``isinstance(body, dict)`` 直接挡掉、静默 return
     ⇒ 改成 ``dict(body)``。

判据（全部真窗口、真点击、真落盘）：
  ⓪设备卡：首帧那格「采集中」会**自己填成真数**（补帧落在 ~1.2s；不许停在「未取到」）
  ①「设备状态」⇒ 沙箱 settings.json 的 click_action 变 device
  ② 页面上的选中态跟着走（.on 落在 device 上，不是只看文件）
  ③ 点回「额度」⇒ 变回 badge（双向都验，免得只对一次）
  ④ 每个按钮点一遍都不出 JS 错（空壳子/半死按钮在这条上现形）

卫生：settings.json / 面板状态文件 / 额度文件全部走临时目录，**绝不碰用户真实文件**。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

SANDBOX = Path(tempfile.mkdtemp(prefix="xiaocc-panelclick-"))
SETTINGS = SANDBOX / "settings.json"
for key, name in (
    ("XIAOCC_SETTINGS_FILE", "settings.json"),
    ("XIAOCC_PANEL_STATE", "panel_state.json"),
    ("XIAOCC_PANEL_REQUEST", "panel_request.json"),
    ("XIAOCC_PANEL_HTML", "panel.html"),
    ("XIAOCC_PANEL_LOCK", "panel.lock"),
    ("XIAOCC_ANCHOR_FILE", "anchor.json"),
    ("XIAOCC_PROBE_FILE", "probe.json"),
):
    os.environ[key] = str(SANDBOX / name)
# 额度报告走副本：判据不该因为真报告被刷新而变
QUOTA = SANDBOX / "quota.json"
try:
    QUOTA.write_bytes((Path.home() / ".xiaocc" / "quota.json").read_bytes())
except OSError:
    QUOTA.write_text("{}")
os.environ["XIAOCC_QUOTA_FILE"] = str(QUOTA)

from AppKit import NSApp, NSTimer

from xiaocc.panel.window import open_panel

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name} · {detail}", flush=True)


def panel_web():
    for window in NSApp.windows() or []:
        view = window.contentView()
        if view is not None and "WebView" in type(view).__name__:
            return window, view
    return None, None


def js(script: str, sink: dict, key: str) -> None:
    """在当前页面跑一段 JS，把 (值, 错误) 塞进 sink[key]（异步）。"""
    _window, web = panel_web()
    if web is None:
        sink[key] = (None, "没有 webview")
        return

    def done(value, error):
        sink[key] = (value, error)

    web.evaluateJavaScript_completionHandler_(script, done)


SELECTED = (
    "Array.from(document.querySelectorAll('#click-actions button'))"
    ".map(b => b.dataset.act + (b.classList.contains('on') ? '*' : '')).join(' ')"
)
#: 点一下并回传「点之前 / 点之后」的选中态字符串 —— 页面当场有没有切过去，一眼可比
CLICK = (
    "(() => {"
    " const sel = () => Array.from(document.querySelectorAll('#click-actions button'))"
    "   .map(b => b.dataset.act + (b.classList.contains('on') ? '*' : '')).join(' ');"
    " const before = sel();"
    " const b = document.querySelector('#click-actions button[data-act=\"%s\"]');"
    " if(!b) return 'NO-BUTTON';"
    " b.click();"
    " return before + ' => ' + sel();"
    "})()"
)


def main() -> int:
    SETTINGS.write_text(json.dumps({"schema": 1, "click_action": "badge"}, ensure_ascii=False))
    sink: dict[str, tuple] = {}

    def read_settings() -> str:
        try:
            return json.loads(SETTINGS.read_text()).get("click_action")
        except (OSError, ValueError):
            return "读不到"

    def step0(_t=None) -> None:
        """先等补帧落地再看页面 —— 补帧是一次整页重渲染，正好落在 step1 那个时刻就会把
        ①的读取打断（这条门禁今天真的偶发红过一次 6/7：**判据没错、是它自己在跟重渲染抢**）。
        """
        js("document.getElementById('dev').innerText.replace(/\\n/g, ' | ')", sink, "dev0")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, False, step_dev)

    def step_dev(_t=None) -> None:
        text = str(sink.get("dev0", (None, None))[0] or "")
        check(
            "⑦设备卡：首帧写「采集中」→ 自己填成真数（不停在「未取到」）",
            "%" in text and "未取到" not in text,
            f"本机负载={text[:80]}",
        )
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.4, False, step1)

    def step1(_t=None) -> None:
        js(SELECTED, sink, "selected0")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.4, False, step2)

    def step2(_t=None) -> None:
        check(
            "①面板打开时：选中态在设置里那一项上",
            sink.get("selected0", (None,))[0] == "badge* device all none",
            f"页面={sink.get('selected0', (None, None))[0]} 设置={read_settings()}",
        )
        js(CLICK % "device", sink, "click1")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.9, False, step3)

    def step3(_t=None) -> None:
        click_result = str(sink.get("click1", (None, None))[0] or "")
        after = click_result.split(" => ")[-1]
        check(
            "②点「设备状态」⇒ 真落盘 + **页面当场**就切过去（不是空壳子）",
            read_settings() == "device" and after == "badge device* all none",
            f"JS={click_result} 设置={read_settings()}",
        )
        js(SELECTED, sink, "selected1")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.4, False, step4)

    def step4(_t=None) -> None:
        check(
            "③重渲染后选中态跟着走（页面上看得见，不是只有文件变）",
            sink.get("selected1", (None,))[0] == "badge device* all none",
            f"页面={sink.get('selected1', (None, None))[0]}",
        )
        js(CLICK % "badge", sink, "click2")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.9, False, step5)

    def step5(_t=None) -> None:
        click_result = str(sink.get("click2", (None, None))[0] or "")
        after = click_result.split(" => ")[-1]
        check(
            "④点回「额度」⇒ 双向都能改（页面当场切回 badge*）",
            read_settings() == "badge" and after == "badge* device all none",
            f"JS={click_result} 设置={read_settings()}",
        )
        js(CLICK % "all", sink, "click3")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.9, False, step6)

    def step6(_t=None) -> None:
        click_result = str(sink.get("click3", (None, None))[0] or "")
        after = click_result.split(" => ")[-1]
        check(
            "⑤「额度+设备」也点到（用户 2026-09-29 新要的那档，页面当场切过去）",
            read_settings() == "all" and after == "badge device all* none",
            f"JS={click_result} 设置={read_settings()}",
        )
        js(CLICK % "none", sink, "click4")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.9, False, step6b)

    def step6b(_t=None) -> None:
        click_result = str(sink.get("click4", (None, None))[0] or "")
        after = click_result.split(" => ")[-1]
        check(
            "⑥「不显示」也点到（四个按钮一个不落，页面当场切过去）",
            read_settings() == "none" and after == "badge device all none*",
            f"JS={click_result} 设置={read_settings()}",
        )
        # 顶栏三个按钮：刷新 / 主题 / 关闭 —— 只验「点了不报错」，别把面板关掉
        js("document.getElementById('btn-refresh').click(); 'ok'", sink, "refresh")
        js(
            "(() => { document.getElementById('btn-theme').click();"
            " return document.documentElement.dataset.theme; })()",
            sink,
            "theme",
        )
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.9, False, step7)

    def step7(_t=None) -> None:
        refresh_err = sink.get("refresh", (None, None))[1]
        theme_value = sink.get("theme", (None, None))[0]
        check(
            "⑧顶栏「刷新」「主题」也活着（不是只有中间那排能按）",
            refresh_err is None and theme_value in ("night", "paper"),
            f"刷新错误={refresh_err} 主题={theme_value}",
        )
        failed = [name for name, ok, _ in RESULTS if not ok]
        print(f"\n结果：{'PASS' if not failed else 'FAIL'}（{len(RESULTS) - len(failed)}/{len(RESULTS)}）")
        print(f"沙箱 {SANDBOX}")
        # 必须 os._exit：NSApp.terminate_ 直接结束进程、退出码恒 0 —— 门禁得能红
        sys.stdout.flush()
        os._exit(1 if failed else 0)

    # 2.6s 才开跑：补帧（~1.2s）已经落地、页面不再重渲染，后面的点击判据不会跟它抢
    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(2.6, False, step0)
    open_panel(
        theme="night",
        settings_path=SETTINGS,
        quota_path=QUOTA,
        out_path=SANDBOX / "panel.html",
        request_path=SANDBOX / "panel_request.json",
    )
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
