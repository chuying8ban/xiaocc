#!/usr/bin/env python
"""小cc macOS 显示层的真窗口取证脚本 —— 跑一次，得到截图 + 一份可核对的 JSON。

它刻意**不用真鼠标**：光标来源由 :class:`AppKitBackend` 注入，所以拖拽、贴边、
悬停展开这几步都是确定性的，也不会劫持用户正在用的鼠标。拖拽用多段平滑位移模拟
（单段大跳会让「松手判定」看起来永远正确，掩盖真实抖动）。

    python scripts/appkit_screenshots.py                 # 输出到 docs/evidence/
    python scripts/appkit_screenshots.py --out /tmp/shots --states idle,working,error

输出：
    <out>/NN-<name>.png        窗口本体（CGWindowList，保留透明通道）
    <out>/NN-<name>.desktop.png 同位置连着桌面一起截（证明真的浮在桌面上）
    <out>/evidence.json        每一步的真实窗口状态 + 断言结果
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: 取证脚本会真的拖动桌宠 → 必须把锚点与自证据指到临时路径。
#: 不设的话，跑一次自测就把用户真实的 ~/.xiaocc/anchor.json 改成了脚本里的固定坐标，
#: 下次面板一启动桌宠就落在屏幕中央（这个坑踩过两次了，两个文件都疼）。
_TMP = Path(tempfile.gettempdir())
os.environ.setdefault("XIAOCC_ANCHOR_FILE", str(_TMP / "xiaocc-shots-anchor.json"))
os.environ.setdefault("XIAOCC_PROBE_FILE", str(_TMP / "xiaocc-shots-probe.json"))

from xiaocc import characters
from xiaocc.backends import window_layout as wl
from xiaocc.backends.appkit import AppKitBackend
from xiaocc.engine import Render
from xiaocc.protocol import State, StatusEvent

SCENARIOS = {
    "idle": (State.IDLE, "待命"),
    "thinking": (State.THINKING, "思考中 · 读状态源"),
    "working": (State.WORKING, "正在执行 read_file"),
    "waiting": (State.WAITING, "等你确认"),
    "done": (State.DONE, "搞定"),
    "error": (State.ERROR, "出错了：状态源超时"),
    "offline": (State.OFFLINE, "离线"),
}


def _load_quartz():
    try:
        import Quartz

        return Quartz
    except ImportError as exc:  # pragma: no cover - 取决于环境
        print(f"需要 PyObjC Quartz 才能截图：{exc}", file=sys.stderr)
        raise SystemExit(3) from exc


def capture_window(quartz, window_number: int, path: Path) -> bool:
    image = quartz.CGWindowListCreateImage(
        quartz.CGRectNull,
        quartz.kCGWindowListOptionIncludingWindow,
        window_number,
        quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if image is None:
        return False
    url = quartz.CFURLCreateWithFileSystemPath(
        None, str(path), quartz.kCFURLPOSIXPathStyle, False
    )
    dest = quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    if dest is None:
        return False
    quartz.CGImageDestinationAddImage(dest, image, None)
    return bool(quartz.CGImageDestinationFinalize(dest))


def capture_desktop_region(quartz, rect, path: Path) -> bool:
    """截屏幕的一块区域（含桌面背景）—— 证明窗口真的浮在桌面上。"""
    image = quartz.CGWindowListCreateImage(
        rect,
        quartz.kCGWindowListOptionOnScreenOnly,
        quartz.kCGNullWindowID,
        quartz.kCGWindowImageDefault,
    )
    if image is None:
        return False
    url = quartz.CFURLCreateWithFileSystemPath(
        None, str(path), quartz.kCFURLPOSIXPathStyle, False
    )
    dest = quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    if dest is None:
        return False
    quartz.CGImageDestinationAddImage(dest, image, None)
    return bool(quartz.CGImageDestinationFinalize(dest))


class Driver:
    def __init__(self, backend: AppKitBackend, character, out: Path, quartz, space, main_height: float):
        self.backend = backend
        self.character = character
        self.out = out
        self.quartz = quartz
        self.space = space
        self.main_height = main_height
        self.steps: list[dict] = []
        self.index = 0
        self.cursor = [wl.Point(-1000.0, -1000.0)]  # 初始鼠标在屏幕外 → 不干扰判定

    # —— 场景 ——
    def render_state(self, state: State, detail: str, seconds: float = 0.45) -> None:
        event = StatusEvent(source="demo", state=state, detail=detail, project="xiaocc")
        self.backend.render(Render(event=event, character=self.character))
        self.backend.linger(seconds)

    def shot(self, name: str, note: str = "", desktop: bool = True) -> dict:
        self.index += 1
        info = self.backend.probe()
        stem = f"{self.index:02d}-{name}"
        window_png = self.out / f"{stem}.png"
        ok = capture_window(self.quartz, info["window_number"], window_png)
        record = {
            "step": stem,
            "note": note,
            "window_png": str(window_png) if ok else None,
            "probe": info,
        }
        if desktop:
            # 窗口在布局坐标里的位置 → CGWindowList 全局坐标（主屏左上原点）
            x, y, width, height = self.space.to_cg_rect(self.backend._window_local, self.main_height)
            pad = 26.0
            region = self.quartz.CGRectMake(x - pad, y - pad, width + pad * 2, height + pad * 2)
            desktop_png = self.out / f"{stem}.desktop.png"
            if capture_desktop_region(self.quartz, region, desktop_png):
                record["desktop_png"] = str(desktop_png)
        self.steps.append(record)
        return record

    # —— 拖拽：走和真鼠标同一条状态迁移，位移用多段平滑曲线 ——
    def drag_to(
        self, target_x: float, target_y: float, steps: int = 24, grab: tuple[float, float] = (0.5, 0.5)
    ) -> None:
        """``grab`` 是「手抓在角色身上的哪个位置」（窗口内相对坐标）。

        它决定了松手时鼠标停在哪 —— 而鼠标停在哪，正是贴边收起的时序里最容易出问题的一点：
        贴着边缘那一侧抓起来推过去的，松手时鼠标**还在把手条上**。所以默认中点抓，
        贴边场景要用 :meth:`drag_to_right_edge` 里的偏边缘抓法来复现。
        """
        start = self.backend._window_local
        self.cursor[0] = wl.Point(start.x + start.width * grab[0], start.y + start.height * grab[1])
        self.backend.start_drag()  # 等价于鼠标按下（会把把手条先弹开）
        start = self.backend._window_local
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)  # 先慢后快再慢，像人手拖
            self.backend.move_window_to(
                start.x + (target_x - start.x) * ease, start.y + (target_y - start.y) * ease
            )
            self.backend.linger(0.02)
        self.cursor[0] = wl.Point(
            target_x + start.width * grab[0], target_y + start.height * grab[1]
        )

    def drag_to_right_edge(self) -> wl.Edge:
        screen = self.space.screen
        # 目标：角色本体右沿离屏幕 4px（必然落在吸附距离内），窗口本身贴着右边缘
        target_x = screen.right - wl.PAD - self.character.canvas[0] * self.backend._scale() - 4.0
        # 抓在角色右侧推过去 → 松手时鼠标正好压在把手条上，这才是会抖动的那种握法
        self.drag_to(max(screen.x, target_x), self.backend._window_local.y, grab=(0.97, 0.5))
        return self.backend.end_drag()

    def refloat(self) -> None:
        """拖回屏幕中间 → 自由漂浮（顺带验证「离开边缘就不再收起」）。"""
        screen = self.space.screen
        self.drag_to(screen.x + screen.width * 0.42, screen.y + screen.height * 0.34)
        self.backend.end_drag()
        self.cursor[0] = wl.Point(screen.center.x + 420.0, screen.bottom - 60.0)
        self.backend.linger(0.2)

    # —— 断言 ——
    def check(self, name: str, condition: bool, detail: str = "") -> dict:
        return {"check": name, "ok": bool(condition), "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description="小cc AppKit 显示层真窗口取证")
    parser.add_argument("--out", default=None, help="截图输出目录，默认 docs/evidence")
    parser.add_argument("--character", default=None, help="角色包目录，默认内置")
    parser.add_argument(
        "--states",
        default="idle,working,error,done",
        help="除了贴边流程外额外渲染的状态，逗号分隔",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    out = Path(args.out) if args.out else root / "docs" / "evidence"
    out.mkdir(parents=True, exist_ok=True)

    quartz = _load_quartz()
    character = characters.load_character(args.character)

    driver_holder: dict = {}
    backend = AppKitBackend(cursor=lambda: driver_holder["driver"].cursor[0])
    backend.interval = 0.2  # 正常由 CLI 写入；脚本里手动对齐

    driver = Driver(
        backend,
        character,
        out,
        quartz,
        backend._space(),
        main_height=backend._space().origin_y + backend._space().height,
    )
    driver_holder["driver"] = driver

    checks: list[dict] = []
    summary: dict = {
        "character": {"id": character.id, "name": character.name, "canvas": list(character.canvas)},
        "screen": list(backend._space().screen.size),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    def view_tracks_window(info: dict) -> bool:
        """视图尺寸必须跟着窗口变，否则所有绘制坐标都会错位。"""
        view = info["probe"].get("view_size")
        frame = info["probe"]["ns_frame"]
        return view is not None and abs(view[0] - frame[2]) < 0.6 and abs(view[1] - frame[3]) < 0.6

    # ① 自由漂浮的待机
    driver.render_state(State.IDLE, "待命")
    info = driver.shot("idle-floating", "自由漂浮 + 待机动画")
    checks += [
        driver.check("窗口不透明=false", info["probe"]["opaque"] is False),
        driver.check("窗口有阴影=false", info["probe"]["has_shadow"] is False),
        driver.check("置顶层级>0", info["probe"]["level"] > 0, f"level={info['probe']['level']}"),
        driver.check("窗口可见", info["probe"]["visible"] is True),
        driver.check("待机时不挂文案", not info["probe"]["caption_drawn"], info["probe"]["caption_drawn"]),
    ]

    # ② 点击穿透边界：透明四角必须让点击穿过去
    window = backend._window_local
    body = backend._body_in_screen()
    corner = wl.Point(window.x + 1.0, window.y + 1.0)
    driver.cursor[0] = corner
    backend.linger(0.12)
    corner_passthrough = backend.probe()["ignores_mouse_events"]
    driver.cursor[0] = body.center
    backend.linger(0.12)
    body_caught = backend.probe()["ignores_mouse_events"]
    checks += [
        driver.check("鼠标在透明角落 → 整窗穿透", corner_passthrough is True, f"corner={corner}"),
        driver.check("鼠标在角色身上 → 接管点击", body_caught is False, f"body={body.center}"),
    ]

    # ③ 拖到右边缘 → 松手收起
    edge = driver.drag_to_right_edge()
    backend.linger(0.2)
    info = driver.shot("docked-collapsed", "拖到右边缘松手 → 收成把手条")
    strip_flush = abs(info["probe"]["ns_frame"][2] - wl.HANDLE_THICKNESS) < 0.6
    checks += [
        driver.check("松手判定落在右边缘", edge is wl.Edge.RIGHT, str(edge)),
        driver.check("收起后是把手条厚度", strip_flush, f"width={info['probe']['ns_frame'][2]}"),
        driver.check("收起后视图跟着窗口缩", view_tracks_window(info),
                     str(info["probe"].get("view_size"))),
    ]

    # ③b 鼠标还压在把手条上：必须保持收起（防抖动的核心时序）
    driver.cursor[0] = backend._window_local.center
    backend.linger(0.35)
    still_collapsed = backend._dock.state == "collapsed"
    checks.append(
        driver.check("鼠标仍压在条上时保持收起（不抖动）", still_collapsed, backend._dock.state)
    )
    info = driver.shot("handle-hover-no-flicker", "松手后鼠标仍在条上：不许自动弹开", desktop=False)

    # ④ 悬停 → 展开；鼠标离开 → 重新收起
    strip = backend._window_local
    driver.cursor[0] = wl.Point(200.0, 400.0)  # 先把鼠标挪开，解除「刚松手」的防抖
    backend.linger(0.1)
    driver.cursor[0] = strip.center
    backend.linger(0.25)
    info_expanded = driver.shot("hover-expanded", "鼠标移到把手条 → 展开（贴在右边缘）")
    expanded_local = backend._window_local
    checks += [
        driver.check("悬停后变为展开", info_expanded["probe"]["dock"] == "expanded",
                     info_expanded["probe"]["dock"]),
        driver.check("展开后仍贴右边缘",
                     abs(expanded_local.right - backend._space().screen.right) < 0.6,
                     f"right={expanded_local.right}"),
        driver.check("展开后的窗口盖住把手条位置", expanded_local.contains(strip.center, wl.HOVER_GRACE)),
    ]

    driver.cursor[0] = wl.Point(180.0, 500.0)  # 鼠标离开
    backend.linger(0.25)
    info = driver.shot("leave-recalls", "鼠标离开 → 再次收起", desktop=False)
    checks.append(driver.check("鼠标离开后重新收起", info["probe"]["dock"] == "collapsed",
                              info["probe"]["dock"]))

    # ⑤ 其余状态（拖回屏幕中间自由漂浮，顺带验证「离开边缘就不再收起」）
    driver.refloat()
    checks.append(
        driver.check("拖回屏幕中间 → 不再收起", backend._dock.state == "floating",
                     backend._dock.state)
    )
    driver.cursor[0] = wl.Point(-1000.0, -1000.0)
    for name in [s.strip() for s in args.states.split(",") if s.strip()]:
        if name not in SCENARIOS:
            print(f"跳过未知状态 {name}", file=sys.stderr)
            continue
        state, detail = SCENARIOS[name]
        driver.render_state(state, detail, seconds=0.4)
        info = driver.shot(f"state-{name}", f"{state.value} 动作 {character.spec(state).motion}")
        checks.append(
            driver.check(
                f"{name} 用对了动作/配色",
                info["probe"]["state"] == state.value,
                f"state={info['probe']['state']} art={info['probe']['art']}",
            )
        )

    summary["checks"] = checks
    summary["steps"] = driver.steps
    summary["passed"] = sum(1 for c in checks if c["ok"])
    summary["failed"] = [c for c in checks if not c["ok"]]
    summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out / "evidence.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    backend.close()
    print(f"截图 {len(driver.steps)} 组 → {out}")
    for check in checks:
        print(f"  {'PASS' if check['ok'] else 'FAIL'}  {check['check']}  {check['detail']}")
    print(f"断言 {summary['passed']}/{len(checks)} 通过")
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
