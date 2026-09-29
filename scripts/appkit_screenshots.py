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
    <out>/evidence.json        每一步的真实窗口状态 + 断言结果（含 opaque_pct）

两道护栏，都是为了不让**空图**冒充证据（HEAD 里 01~09 那批正是空图，断言却 20/20 全绿）：

1. **屏睡拒跑**：显示器睡着时 ``CGWindowListCreateImage`` 不报错、只交回一张整幅全透明的图，
   所以写 PNG 之前先问一句 ``CGDisplayIsAsleep``，睡着就 rc=2 退出，一张图都不落盘。
2. **像素验收**：每张窗口截图都数一遍非透明像素占比、写进 step 的 ``opaque_pct``，
   < 2% 判为空图 ⇒ 该步断言失败、脚本 rc=1（判据同 :mod:`scripts.pixel_stats`）。

退出码：0 = 全过；1 = 有断言失败；2 = 显示器睡着，拒跑；3 = 缺 PyObjC。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # 仓库根：证据文件里的路径一律相对它写
sys.path.insert(0, str(ROOT / "src"))


def _rel(path: Path) -> str:
    """证据文件里只写**相对仓库根**的路径。

    ``docs/evidence/evidence.json`` 是受版本控制、要进公开仓库的：写绝对路径会泄露本机用户名
    与目录结构（``/Users/<用户名>/...``），别人 clone 下来也对不上。截图被 ``--out`` 指到仓库外时
    （相对仓库根表示不了）退化成只写文件名，宁可不完整也不泄露。
    """
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return path.name

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


#: alpha 高于这个值才算「这个像素真的画出来了」—— 判据与 scripts/pixel_stats.py 一致。
OPAQUE_ALPHA = 0.35
#: 非透明像素占比低于这个百分数 ⇒ 判为空图。真窗口图实测 20%~30%（参照 8ff7304 那版的
#: 09-state-done.png = 30.6%），屏睡交回来的空图是 0.0% —— 2% 这条线两边都碰不着。
EMPTY_OPAQUE_PCT = 2.0


def require_awake_display(quartz) -> None:
    """写 PNG 前的闸门：主显示器睡着就拒跑（rc=2），一张 PNG 都不落盘。

    屏睡时 :func:`CGWindowListCreateImage` **不报错**，只交回一张整幅全透明的图；而断言查的
    是层级/尺寸/穿透这些 ``probe()`` 元数据账，压根不看像素 —— 于是一份假证据能 20/20 全绿地
    混进 HEAD。这里只挡「真要写 PNG」这条路：纯元数据断言不需要屏亮，不该被它拦下。
    """
    if quartz.CGDisplayIsAsleep(quartz.CGMainDisplayID()):
        print(
            "拒绝抓图：显示器睡着，抓出来是空图，拒绝生成假证据。\n"
            "  CGWindowListCreateImage 在屏睡时不报错，只交回整幅全透明的 PNG，\n"
            "  而元数据断言照样会全绿。请唤醒屏幕（碰一下鼠标/键盘）后重跑。",
            file=sys.stderr,
        )
        raise SystemExit(2)


def opaque_pixel_pct(path: Path) -> float | None:
    """数一张 PNG 里非透明像素的占比（百分数）；读不到或不是位图时返回 ``None``。

    ``probe()`` 的账对（art=done.svg、层级、尺寸都对）≠ 像素真的画出来了，所以每张窗口截图
    都得自己数一遍。判据照搬 :mod:`scripts.pixel_stats`：alpha > :data:`OPAQUE_ALPHA` 算不透明。
    """
    # PyObjC 懒加载，同 _load_quartz：本文件顶部 import 之后还有 sys.path/env 设置，
    # 提到模块顶部就吃 E402。
    from AppKit import NSBitmapImageRep, NSData

    data = NSData.dataWithContentsOfFile_(str(path))
    if data is None:
        return None
    rep = NSBitmapImageRep.imageRepWithData_(data)
    if rep is None:
        return None
    width, height = int(rep.pixelsWide()), int(rep.pixelsHigh())
    total = width * height
    if total <= 0:
        return None
    opaque = 0
    for y in range(height):
        for x in range(width):
            pixel = rep.colorAtX_y_(x, y)
            if pixel is not None and pixel.alphaComponent() > OPAQUE_ALPHA:
                opaque += 1
    return opaque / total * 100.0


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
        # 本脚本唯一写 PNG 的地方 → 屏睡闸门装在这里：睡着就 rc=2，半张图都不落盘。
        require_awake_display(self.quartz)
        self.index += 1
        info = self.backend.probe()
        stem = f"{self.index:02d}-{name}"
        window_png = self.out / f"{stem}.png"
        ok = capture_window(self.quartz, info["window_number"], window_png)
        pct = opaque_pixel_pct(window_png) if ok else None
        record = {
            "step": stem,
            "note": note,
            "window_png": _rel(window_png) if ok else None,
            "opaque_pct": round(pct, 3) if pct is not None else None,
            "probe": info,
        }
        if desktop:
            # 窗口在布局坐标里的位置 → CGWindowList 全局坐标（主屏左上原点）
            x, y, width, height = self.space.to_cg_rect(self.backend._window_local, self.main_height)
            pad = 26.0
            region = self.quartz.CGRectMake(x - pad, y - pad, width + pad * 2, height + pad * 2)
            desktop_png = self.out / f"{stem}.desktop.png"
            if capture_desktop_region(self.quartz, region, desktop_png):
                record["desktop_png"] = _rel(desktop_png)
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

    quartz = _load_quartz()
    # 屏睡就别开窗口了：早拒早干净 —— 不建输出目录、不渲染、一张 PNG 都不落盘。
    # （Driver.shot 里还有同一道闸门，管的是「跑到一半屏幕才睡过去」。）
    require_awake_display(quartz)
    out.mkdir(parents=True, exist_ok=True)
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

    # ⑥ 像素级验收：账对 ≠ 画出来了。逐张数窗口截图的非透明像素，空图不许蒙过去 ——
    #    这一条以前没有，所以 01~09 全是空图时照样报了 20/20 全绿。
    for record in driver.steps:
        pct = record["opaque_pct"]
        checks.append(
            driver.check(
                f"{record['step']} 不是空图（非透明像素 ≥ {EMPTY_OPAQUE_PCT:g}%）",
                pct is not None and pct >= EMPTY_OPAQUE_PCT,
                "读不到像素" if pct is None else f"opaque_pct={pct:.3f}%",
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
