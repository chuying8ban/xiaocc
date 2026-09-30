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
  ⑨「退出小cc」第一下只进入待确认（不发送）、第二下才真到控制入口（留痕）
  ⑩dry-run 下面板自己不收窗（门禁才能继续跑；真跑时会收）
  ①「设备状态」⇒ 沙箱 settings.json 的 click_action 变 device
  ② 页面上的选中态跟着走（.on 落在 device 上，不是只看文件）
  ③ 点回「额度」⇒ 变回 badge（双向都验，免得只对一次）
  ④ 每个按钮点一遍都不出 JS 错（空壳子/半死按钮在这条上现形）

卫生：settings.json / 面板状态文件 / 额度文件全部走临时目录，**绝不碰用户真实文件**。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

import verify_log

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
# 「退出/重启」那个门禁**只走 dry-run + 留痕**：这里验的是「页面上点两下 ⇒ 桥 ⇒ Python 控制入口」，
# 绝不允许真把用户屏上的桌宠 bootout 掉（那样门禁自己会把它杀掉）。
CTL_MARK = SANDBOX / "ctl.mark"
os.environ["XIAOCC_CTL_DRY_RUN"] = "1"
os.environ["XIAOCC_CTL_MARK"] = str(CTL_MARK)

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
#: ⑫ 正文横向溢出探测：**逐元素比 scrollWidth 与 clientWidth**（只看横向）。
#: 为什么用「有自己文字节点的元素」：整页扫描会把容器算进去、报出一堆与裁切无关的量。
#: 只量横向：竖向差值会被 line-height / 字形盒基线差咬出 2–4px 的假溢出（`<h1>`、大数字都报过）。
OVERFLOW = (
    "(() => {"
    " const out = [];"
    " for (const el of document.querySelectorAll('body *')) {"
    "  const tag = el.tagName.toLowerCase();"
    "  if (tag === 'script' || tag === 'style') continue;"
    "  const own = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());"
    "  if (!own) continue;"
    "  const over = el.scrollWidth - el.clientWidth;"
    "  if (over > 2) out.push({tag: tag, cls: String(el.className || ''), over: over,"
    "    text: (el.textContent || '').trim().slice(0, 60)});"
    " }"
    " return JSON.stringify(out);"
    "})()"
)
#: ⑬ 的反向控制：往页面上塞一行**故意放不下**的文字，探针必须当场报出来。
#: 没有这一条，⑫ 的绿就只是"这次恰好没量到"——今晚反复讲的"仪表不反向控制就不知道守不守得住"。
CANARY_ADD = (
    "(() => {"
    " const d = document.createElement('div');"
    " d.id = 'overflow-canary';"
    " d.style.cssText = 'width:60px;white-space:nowrap;overflow:hidden';"
    " d.textContent = '诱饵：这一行故意放不下';"
    " document.body.appendChild(d);"
    " return d.id;"
    "})()"
)
CANARY_DEL = (
    "(() => { const d = document.getElementById('overflow-canary');"
    " if (d) d.remove(); return 'removed'; })()"
)

#: ⑫ 的白名单：**初始必须为空**，加一条就得写清理由（形如 `("per-key", "为什么这句允许被裁")`）。
#: 空着不是为了好看：要么它会变成噪音工厂然后被人关掉，要么变成静默豁免 —— 两个失败模式今晚都见过。
OVERFLOW_ALLOW: tuple[tuple[str, str], ...] = ()

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

    def step8(_t=None) -> None:
        """「退出小cc」两下确认：「删除类/改变状态」的点按先弹确认（用户定的规矩）。"""
        js(
            "(() => { const b = document.getElementById('btn-quit'); b.click();"
            " return b.textContent; })()",
            sink,
            "arm",
        )
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.4, False, step9)

    def step9(_t=None) -> None:
        armed = str(sink.get("arm", (None, None))[0] or "")
        check(
            "⑨「退出小cc」第一下只进入待确认（不发送，3 秒自动撤回）",
            armed == "再点一次确认" and not CTL_MARK.exists(),
            f"按钮={armed} 留痕={CTL_MARK.exists()}",
        )
        js("document.getElementById('btn-quit').click(); 'ok'", sink, "fire")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.6, False, step10)

    def step10(_t=None) -> None:
        mark = CTL_MARK.read_text(encoding="utf-8") if CTL_MARK.exists() else ""
        check(
            "⑩第二下才真到控制入口（dry-run 留痕：xiaoccctl stop）",
            "quit" in mark and "xiaoccctl" in mark and " stop" in mark,
            f"留痕={mark.strip()[:70]}",
        )
        check(
            "⑪dry-run 下面板自己不收窗（门禁继续；真跑时退出会收）",
            panel_web()[1] is not None,
            f"webview={panel_web()[1] is not None}",
        )
        js(OVERFLOW, sink, "overflow")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.6, False, step11)

    def step11(_t=None) -> None:
        """⑫面板**正文**不许横向被裁（量到过两处：千问 hint 68px、百炼 11px）。

        口径三条，都是别处吃过亏换来的：**只看横向**（竖向会被 line-height/基线差咬出 2–4px 假溢出）；
        **量 JS 跑完之后的 DOM**（今天两次栽在同一层：剥 `<style>` 扫模板会漏掉 JS 渲染出来的文本）；
        **白名单必须写理由且初始为空**。
        """
        raw = sink.get("overflow", (None, None))
        value, error = (raw if isinstance(raw, tuple) else (None, None))
        caught: list[dict] = []
        if isinstance(value, str) and value:
            with contextlib.suppress(json.JSONDecodeError):
                caught = json.loads(value)
        allowed = [
            item
            for item in caught
            if any(pattern in str(item.get("text") or "") for pattern, _reason in OVERFLOW_ALLOW)
        ]
        unknown = [item for item in caught if item not in allowed]
        check(
            "⑫面板正文没有横向被裁的文字（阈值 >2px；白名单为空）",
            error is None and not unknown,
            "无横向溢出" if not unknown else "; ".join(
                f"{item.get('tag')}.{item.get('cls')} 超{item.get('over')}px：{item.get('text')}"
                for item in unknown[:3]
            ),
        )
        js(CANARY_ADD, sink, "canary_add")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.4, False, step12)

    def step12(_t=None) -> None:
        """⑬ 反向控制：塞一行故意被裁的文字，探针必须看得见它（否则 ⑫ 的绿不算数）。"""
        js(OVERFLOW, sink, "overflow2")
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.4, False, step13)

    def step13(_t=None) -> None:
        raw = sink.get("overflow2", (None, None))
        value, error = (raw if isinstance(raw, tuple) else (None, None))
        caught: list[dict] = []
        if isinstance(value, str) and value:
            with contextlib.suppress(json.JSONDecodeError):
                caught = json.loads(value)
        canary_seen = any("诱饵" in str(item.get("text") or "") for item in caught)
        check(
            "⑬反向控制：故意塞一行被裁文字 ⇒ 探针当场报出来（证明 ⑫ 守得住）",
            error is None and canary_seen,
            "诱饵被抓到" if canary_seen else f"诱饵没被抓到（探针失效）caught={caught[:2]}",
        )
        js(CANARY_DEL, sink, "canary_del")
        failed = [name for name, ok, _ in RESULTS if not ok]
        print(f"\n结果：{'PASS' if not failed else 'FAIL'}（{len(RESULTS) - len(failed)}/{len(RESULTS)}）")
        print(f"沙箱 {SANDBOX}")
        sys.stdout.flush()
        verify_log.record(
            "verify_panel_click",
            1 if failed else 0,
            criteria={"passed": len(RESULTS) - len(failed), "checks": len(RESULTS)},
        )
        os._exit(1 if failed else 0)

    def step7(_t=None) -> None:
        refresh_err = sink.get("refresh", (None, None))[1]
        theme_value = sink.get("theme", (None, None))[0]
        check(
            "⑧顶栏「刷新」「主题」也活着（不是只有中间那排能按）",
            refresh_err is None and theme_value in ("night", "paper"),
            f"刷新错误={refresh_err} 主题={theme_value}",
        )
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.2, False, step8)

    # 2.6s 才开跑：补帧（~1.2s）已经落地、页面不再重渲染，后面的点击判据不会跟它抢
    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(2.6, False, step0)
    open_panel(
        theme="night",
        settings_path=SETTINGS,
        quota_path=QUOTA,
        out_path=SANDBOX / "panel.html",
        request_path=SANDBOX / "panel_request.json",
    )
    # 走到这里说明 open_panel 自己回来了（面板被关掉之类）⇒ step10 那次 os._exit 没发生，
    # 这一趟也得留痕，否则盘上留着的是**上一次**的绿。checks=0 的绿一眼就能认出来。
    rc = 0 if all(ok for _, ok, _ in RESULTS) else 1
    verify_log.record(
        "verify_panel_click",
        rc,
        criteria={"passed": sum(1 for _, ok, _ in RESULTS if ok), "checks": len(RESULTS)},
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
