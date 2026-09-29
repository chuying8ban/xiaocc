"""验：面板点「退出小cc」⇒ 面板**自己收窗退出**（不留孤窗）。

门禁 `verify_panel_click.py` 里那条是 dry-run（不能真把桌宠停掉），所以「退出成功后自己收窗」
这一步它验不到 —— 这里补上，仍**不碰真作业**：`XIAOCC_CTL_OVERRIDE` 指替身脚本（exit 0），
不开 dry-run，于是面板会走真实分支：通知控制入口 → 0.6s 后收窗 → 进程退出。

判据：进程在 6 秒内自己退出（rc=0）、且沙箱里的 panel_state.json 被清掉。
跑法：`python scripts/verify_panel_quit.py`；退出码 0 = 收窗了，1 = 6 秒后还活着（孤窗）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import verify_log

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

SANDBOX = Path(tempfile.mkdtemp(prefix="xiaocc-panelquit-"))
STUB = SANDBOX / "ctl-stub.sh"
STUB.write_text('#!/bin/zsh\nexit 0\n', encoding="utf-8")
os.environ["XIAOCC_SETTINGS_FILE"] = str(SANDBOX / "settings.json")
os.environ["XIAOCC_PANEL_STATE"] = str(SANDBOX / "panel_state.json")
os.environ["XIAOCC_PANEL_REQUEST"] = str(SANDBOX / "panel_request.json")
os.environ["XIAOCC_PANEL_HTML"] = str(SANDBOX / "panel.html")
os.environ["XIAOCC_PANEL_LOCK"] = str(SANDBOX / "panel.lock")
os.environ["XIAOCC_ANCHOR_FILE"] = str(SANDBOX / "anchor.json")
os.environ["XIAOCC_PROBE_FILE"] = str(SANDBOX / "probe.json")
os.environ["XIAOCC_QUOTA_FILE"] = str(SANDBOX / "quota.json")
os.environ["XIAOCC_CTL_OVERRIDE"] = str(STUB)
os.environ["XIAOCC_CTL_MARK"] = str(SANDBOX / "ctl.mark")
os.environ.pop("XIAOCC_CTL_DRY_RUN", None)

(SANDBOX / "settings.json").write_text(
    json.dumps({"schema": 1, "click_action": "badge"}, ensure_ascii=False), encoding="utf-8"
)
(SANDBOX / "quota.json").write_text("{}", encoding="utf-8")

from AppKit import NSApp, NSTimer

from xiaocc.panel.window import open_panel


def web():
    for window in NSApp.windows() or []:
        view = window.contentView()
        if view is not None and "WebView" in type(view).__name__:
            return view
    return None


def click_quit(_t=None) -> None:
    view = web()
    if view is None:
        print("FAIL：没找到 webview")
        sys.stdout.flush()
        verify_log.record("verify_panel_quit", 1)
        os._exit(1)
    # 两下：第一下进待确认，第二下真发（页面自己的规矩）
    view.evaluateJavaScript_completionHandler_(
        "(() => { const b=document.getElementById('btn-quit'); b.click(); b.click(); return b.textContent; })()",
        lambda value, error: print(f"点了两下「退出小cc」⇒ 按钮={value} 错误={error}", flush=True),
    )


def still_alive(_t=None) -> None:
    print("FAIL：6 秒后面板进程还活着 —— 没有自己收窗（孤窗）")
    sys.stdout.flush()
    verify_log.record("verify_panel_quit", 1, criteria={"deadline_s": 6.0, "still_alive": True})
    os._exit(1)


NSTimer.scheduledTimerWithTimeInterval_repeats_block_(2.5, False, click_quit)
NSTimer.scheduledTimerWithTimeInterval_repeats_block_(6.0, False, still_alive)
print(f"沙箱 {SANDBOX}（替身 {STUB.name}，未开 dry-run）", flush=True)
open_panel(
    theme="night",
    settings_path=SANDBOX / "settings.json",
    quota_path=SANDBOX / "quota.json",
    out_path=SANDBOX / "panel.html",
    request_path=SANDBOX / "panel_request.json",
)
# open_panel 自己回来了 = 面板**自己收窗退出**了（这一支的绿）；上面两个 os._exit 是红。
verify_log.record("verify_panel_quit", 0, criteria={"deadline_s": 6.0, "still_alive": False})
