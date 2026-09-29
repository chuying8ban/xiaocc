"""门禁判据②的正对照：开一个 30fps 自绘的透明小窗，证明「像素判据」抓得到「画面真在动」。

为什么要它：`ops/xiaoccctl gate` 的判据②要求**从进程外**看到画面在变。可屏幕睡着/锁屏时
根本抓不到别的进程的窗口（`CGWindowListCreateImage` 返回 None），拿小cc 本体做正对照的前提
（人在、屏亮、未锁）并不总是成立。所以用这个自制窗口当基准 —— 它必定在动，判据必须说「在动」。

用法（屏幕必须醒着且未锁屏，否则窗口不在屏上，抓图必然 None）：

    .venv/bin/python ops/motion_positive_control.py 20 &     # 后台开一个动 20 秒的窗口
    ops/xiaoccctl motion <pid>                               # 期望 changed=true

注意：这里画的就是「每帧重画整块画布」——和小cc 现在烧 CPU 的画法是同一类，
所以它同时也是「每帧重画真的很贵」这一说的现场演示。
"""
from __future__ import annotations

import sys

import objc
from AppKit import NSApplication, NSBackingStoreBuffered, NSBezierPath, NSColor, NSView, NSWindow
from Foundation import NSMakeRect, NSTimer
from Quartz import CGWindowLevelForKey, kCGFloatingWindowLevelKey

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
W, H = 120.0, 60.0


class SpinView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(SpinView, self).initWithFrame_(frame)  # noqa: PLW0642  pyobjc 惯用法，此规则对它是误报
        if self is not None:
            self.t = 0.0
        return self

    def drawRect_(self, _rect):
        self.t += 1.0
        v = (self.t % 40) / 40.0                       # 每帧整块重画 ⇒ 像素一定在变
        NSColor.colorWithCalibratedRed_green_blue_alpha_(v, 1.0 - v, 0.5, 1.0).setFill()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, W, H))
        NSBezierPath.fillRect_(NSMakeRect((self.t * 3) % (W - 20), 10, 20, 20))

    def tick_(self, _timer):
        self.setNeedsDisplay_(True)


def main() -> int:
    app = NSApplication.sharedApplication()
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((200.0, 200.0), (W, H)), 0, NSBackingStoreBuffered, False)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setLevel_(CGWindowLevelForKey(kCGFloatingWindowLevelKey))
    view = SpinView.alloc().initWithFrame_(((0.0, 0.0), (W, H)))
    win.setContentView_(view)
    win.orderFrontRegardless()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0 / 30.0, view, "tick:", None, True)
    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(SECONDS, False, lambda _t: app.stop_(None))
    import os
    print(f"对照窗口 pid={os.getpid()} 跑 {SECONDS:g} 秒", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
