#!/usr/bin/env python3
"""对外发布前**重拍** `docs/design/` 那 10 张界面实拍 —— 拍的是**现版本**，不是旧版。

为什么要有它：`docs/design/` 里那批图是界面还没定稿时拍的，里面好几张展示的是**已经删掉**的
旧版画面（菜单少了退出/重启、气泡还是老措辞、面板还是老版式）。文档配旧图 = 对外说了假话，
比不配图更糟。重拍要能**再跑一遍**（不是某次手工 mktemp 的产物），所以写成脚本。

## 复用，不新造第三套
* 抓图与驱动：`scripts/appkit_screenshots.py` 的 `Driver` / `capture_window` /
  `require_awake_display` / `opaque_pixel_pct` —— 直接 import（区域抓图 `capture_desktop_region`
  **故意不用**：见红线 2）。
* 替身数据：`scripts/make_shot_fixtures.py` —— 子进程调它，**不抄它的逻辑**
  （抄一份就会跟真报告漂移，漂完截图展示的就是个不存在的界面）。
* 判空图：`scripts/pixel_stats.py` —— 收尾时对 10 张成品跑一遍，非透明占比打进证据。

## 卫生红线（三条，都踩过坑）
1. **绝不碰在跑的真桌宠**：全程只是一份沙箱 backend（`Driver` 起的那个进程内的窗口），
   锚点/自证据/设置/额度/面板请求五个缝全指到临时目录；菜单里的「重启/退出」再套一层
   `XIAOCC_CTL_OVERRIDE` 指向 `exit 0` 的替身脚本 ⇒ 就算合成事件误选中菜单项也**动不了真作业**。
2. **一个桌面像素都不许进图**：这台机器桌面上压着**全屏的 Hermes 聊天窗**（里面是我们自己的
   讨论、日志与 `/Users/…` 绝对路径），"连着桌面一起截"就等于把别人的会话发进公开仓库。
   所以 11 张**全部走窗口级抓图**（`CGWindowListCreateImage(IncludingWindow, id)` 只含那扇窗
   自己的内容）：8 张纯窗口图（表里 `how=window`），3 张需要「窗口在屏上的相对位置」的
   （菜单、把手条两张）用**两/多个窗口级抓图合成到一块中性底色**上（`how=window-composite`）。
   合成件不冒充真桌面：记录里写死 `canvas: 中性底色`、给出每个源窗口的矩形与像素尺寸。
   ⚠️ 早期版本用过 `capture_desktop_region` + 自己铺块衬底挡背景 —— **已废弃**：衬底和取景框
   差 34px 就会从边缘漏出真桌面，而且「不拍桌面」本来就不需要衬底。
3. **空图/半截图不算拍成，且每条记录都要当场过闸**：`how` 由抓图那条代码路径自己盖
   （缺 `how` 直接判红，不许缺省推断）；几何闸 = 源窗口矩形 ⊆ 取景框（纯窗口图则是
   「PNG 像素尺寸 == 窗口矩形 × 缩放」）；像素闸 = 合成图 `bright_px == 0 且 colors ≤ 4`
   （中性底色上的任何**亮**像素都只能是别人的东西漏进来了）、纯窗口图则跑 `pixel_stats.py`
   判非空（非透明占比 ≥ 2%）。三条闸 + 红线自查都在 `_record()` 里**当场**做、**当场**落账。

## 11 张是什么
| 文件名 | 拍什么 | how |
| --- | --- | --- |
| `气泡-额度.png` | 单击档位 `badge` | window |
| `气泡-设备状态.png` | 单击档位 `device` | window |
| `气泡-额度+设备.png` | 单击档位 `all` | window |
| `气泡-陈旧.png` | 报告陈旧（文件 mtime 老 + `state=stale`） | window |
| `气泡-未采集.png` | 额度文件不存在 ⇒ 「额度未采集」 | window |
| `气泡-满.png` | 气泡满亮度那一帧 | window |
| `气泡-淡化中.png` | TTL 末段 1.2s 线性淡化里 ≈1/3 满亮度那一帧 | window |
| `气泡-退干净.png` | 淡完、窗口里没有气泡那一帧 | window |
| `右键菜单.png` | 桌宠窗口 + 菜单窗口（独立窗口）合成 | window-composite |
| `贴边把手条-收起.png` | 贴到右边缘后收成的 12px 把手条本身 | window-composite |
| `贴边把手条-展开后.png` | 单击把手条之后展开的样子（贴右边缘） | window-composite |

「收起」与「展开后」是两张：文件名不许承诺图里没有的东西 —— 一张图里放不下「收起态」和
「展开态」两种画面（同一扇窗的两个状态），所以分开拍。

淡化那三帧**连拍**（每 0.1s 一张、共 6.6s），再按每帧的**气泡带实测 alpha** 选帧 ——
单次卡时间赌一帧的下场是：抓早了是满亮度、抓晚了气泡没了，而图看着都「挺正常」。

用法::

    .venv/bin/python scripts/shoot_design_shots.py            # 写 docs/design/*.png
    .venv/bin/python scripts/shoot_design_shots.py --dry-run  # 只打印计划与取景框，不落盘

退出码：0 = 11 张全出且每条记录三道闸全过；1 = 有张没拍成/闸没过；2 = 屏睡，拒跑（一张不落盘）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: 假家目录：`make_shot_fixtures.py` 自己的默认值（`/tmp` 下一个固定名）。
#: **不**放 `$HOME` 下 —— 放 `$HOME` 就是把真实用户名烤进图/留档（那条红线的原话）。
DEMO_HERMES = Path("/tmp/xiaocc-demo-hermes")

#: 沙箱：锚点 / 自证据 / 设置 / 额度 / 面板请求五个缝全在这儿。
#: 不设的话，跑一次重拍就把用户真实的 `~/.xiaocc/anchor.json` 改成脚本里的固定坐标
#: （这个坑 appkit_screenshots.py 的注释里已经记过一次）。
SANDBOX = Path(tempfile.mkdtemp(prefix="xiaocc-design-shots-"))
FIXTURES = SANDBOX / "fixtures"
QUOTA_COPY = SANDBOX / "quota.json"
SETTINGS = SANDBOX / "settings.json"
_CTL_STUB = SANDBOX / "ctl-stub.sh"
_CTL_STUB.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
_CTL_STUB.chmod(0o755)

os.environ["XIAOCC_ANCHOR_FILE"] = str(SANDBOX / "anchor.json")
os.environ["XIAOCC_PROBE_FILE"] = str(SANDBOX / "probe.json")
os.environ["XIAOCC_SETTINGS_FILE"] = str(SETTINGS)
os.environ["XIAOCC_QUOTA_FILE"] = str(QUOTA_COPY)
os.environ["XIAOCC_PANEL_STATE"] = str(SANDBOX / "panel_state.json")
os.environ["XIAOCC_PANEL_REQUEST"] = str(SANDBOX / "panel_request.json")
#: 菜单里的「重启/退出」：替身脚本 + dry-run 双保险 —— 合成事件万一选中了菜单项也停不了真作业
os.environ["XIAOCC_CTL_OVERRIDE"] = str(_CTL_STUB)
os.environ["XIAOCC_CTL_DRY_RUN"] = "1"
os.environ["XIAOCC_CTL_MARK"] = str(SANDBOX / "ctl.mark")

# 环境变量设好**之后**才 import 抓图器（它内部用 setdefault，我们这份优先）
from appkit_screenshots import (
    EMPTY_OPAQUE_PCT,
    Driver,
    _load_quartz,
    _rel,
    capture_window,
    require_awake_display,
)

from xiaocc import settings as settings_store
from xiaocc.backends import window_layout as wl
from xiaocc.backends.appkit import AppKitBackend
from xiaocc.characters import load_character
from xiaocc.engine import Render
from xiaocc.protocol import State, StatusEvent

#: 沙箱桌宠初始位置：**屏幕左侧偏中的纯色区**（这台机器桌面被全屏 Hermes 占着，
#: 右边那条没有文字的版面才是干净的取景框 —— 见模块 docstring 红线 2）。
START_AT = "1080,320"
#: 气泡带占窗口高度的比例（用来在连拍里定位「哪一横条在变」）。气泡画在窗口的文案带上，
#: 只占顶部一小段，所以上下各取 30% 比一比就够认出它在哪一头。
BAND_FRACTION = 0.30
#: 连拍参数：0.1s 一张 × 6.6s —— 取样要**密**，否则「淡化中」只能在 0.25s 的格子里挑，
#: 一档就是 ±0.21 alpha（1.2s 淡化里 0.25s 走了五分之一）⇒「取深一点」根本做不到。
BURST_INTERVAL_S = 0.1
BURST_SECONDS = 6.6
#: 「淡化中」那一帧要挑多深：**满亮度的 1/3**（≈ 淡化的最后 0.4s）。
#: 为什么不是 1/2：非透明占比的口径是「alpha > 0.35」，半透明那帧整枚气泡都还算「不透明」
#: ⇒ 和「满」那帧的数字几乎一样（35.1% vs 34.3%），拿它当「证明淡化存在」太弱。
#: 1/3 时气泡像素的 alpha 掉到阈值以下，占比与画面上都能一眼看出差别，文字仍读得出来。
FADE_MID_FRACTION = 1.0 / 3.0
#: 取景框四周留白（合成件用）
PAD = 34.0
#: 「额度报告」这份替身的文件年龄（秒）。**0 = 跟已审过的那批字面一致**（气泡印「1 秒前」）。
#: 真报告的年龄由 15 分钟一拍的采集器决定，所以想让存档图更像真机可以把它调到 300~600
#: （气泡会印「6 分钟前」）；代价是整批气泡的字面都变，得重新过一遍眼。
NORMAL_AGE_S = 0.0
#: 合成件的中性底色（很暗的蓝灰）。**不是**桌面、**不是**壁纸：记录里写死 `canvas` 就是它。
#: 亮度上限的理由见 :data:`BRIGHT_LIMIT` —— 底越暗，「框里出现亮像素 = 漏了别人的东西」越灵敏。
CANVAS_RGB = (0.10, 0.11, 0.15)
#: 亮像素上限：单通道超过它就算「漏了别人的东西进图」
BRIGHT_LIMIT = 0.30
#: 合成件里底色最多允许 32 级量化出几个色 —— 纯色底应该是 1；>4 说明底上还有别的东西
COLORS_LIMIT = 4
#: 每张图的 `how`：**由抓图那条代码路径自己盖**，`_record()` 里缺它直接判红。
HOW_WINDOW = "window"  # 纯窗口级抓图（CGWindowListCreateImage IncludingWindow）
HOW_COMPOSITE = "window-composite"  # 两/多个窗口级抓图合成到中性底色上
HOW_VALUES = (HOW_WINDOW, HOW_COMPOSITE)
#: 11 张成品（顺序即报告顺序）；`docs/design/` 里就该是这些名字
SHOT_NAMES = (
    "气泡-额度", "气泡-设备状态", "气泡-额度+设备", "气泡-陈旧", "气泡-未采集",
    "气泡-满", "气泡-淡化中", "气泡-退干净",
    "右键菜单", "贴边把手条-收起", "贴边把手条-展开后",
)

_CocoaPoint = namedtuple("_CocoaPoint", "x y")

failures: list[str] = []
records: list[dict] = []
#: 成品目录与证据文件（main 里设）。证据**边拍边落盘**：万一卡在某个模态循环里被强杀，
#: 已经拍到的那几张仍然有账可查（第一版就是这么卡死的，那样连一份账都留不下来）。
OUT_DIR: Path | None = None
EVIDENCE_PATH: Path | None = None


def evidence_payload() -> dict:
    """证据 JSON 的全文（边拍边落盘、收尾也用它）。**只写相对仓库根/文件名的路径**。

    绝对路径会把本机用户名与目录结构带进留档（`appkit_screenshots._rel()` 的注释里记过这坑）。
    """
    return {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "out": _rel(OUT_DIR) if OUT_DIR else None,
        "note": "替身数据（scripts/make_shot_fixtures.py），非作者真实数据；"
                "11 张全部是**窗口级抓图**（how=window / window-composite），"
                "合成件的背景是脚本铺的中性底色（canvas），**不是桌面** —— 桌面上压着别人的窗口",
        "records": records,
        "selfcheck": {"redline": redline_scan(), "shots": len(records)},
        "failures": failures,
    }


def write_evidence() -> None:
    """把当前账落盘。**每拍一张都会调它**（见 `Shooter._record`）—— 卡死的代价不许是「一份账都没有」。"""
    if EVIDENCE_PATH is None:
        return
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(evidence_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _needle_walk(value: Any, needle: str, path: str = "") -> list[str]:
    """在一个字段值里找 needle，返回它出现在哪几个子路径（**只报位置，不回显命中内容**）。"""
    if isinstance(value, str):
        return [path or "."] if needle and needle in value else []
    if isinstance(value, dict):
        return [hit for key, item in value.items() for hit in _needle_walk(item, needle, f"{path}.{key}")]
    if isinstance(value, (list, tuple)):
        return [
            hit
            for index, item in enumerate(value)
            for hit in _needle_walk(item, needle, f"{path}[{index}]")
        ]
    return []


def redline_scan() -> list[str]:
    """公开仓库红线的自查：**落盘产物的字段值**里不许出现真家目录路径 / 本机用户名。

    三条都踩过，写在这儿免得再改回去：

    1. needle 用 ``str(Path.home())``（**真绝对路径**），不用 `/Users/` 这个前缀 —— 前缀会命中
       一切"提到 /Users 的说明文字"；
    2. **只扫产物的字段值**，不扫脚本源码、也不扫自查诊断（`failures` / `gates` / `note`）——
       早期版本把脚本自己一起扫，而 needle 字面又写在脚本里 ⇒ 这条判据**永远红**，等于没查；
    3. needle 由 ``Path.home()`` **算出来**，不以字面落进任何被扫的地方；命中只报**位置**
       （`records[3].probe.quota_file` 这种），不回显路径本身。
    """
    needle = str(Path.home())
    hits: list[str] = []
    for index, record in enumerate(records):
        for key, value in record.items():
            if key in ("note", "failures", "gates"):
                continue
            hits.extend(f"records[{index}].{key}{where.lstrip('.')}" for where in _needle_walk(value, needle))
    return hits


def say(message: str) -> None:
    print(message, flush=True)


class _FakeMouseEvent:
    """假鼠标事件：backend 只要 ``locationInWindow()``（Cocoa 窗口坐标，原点左下）。

    只有这一个接口的壳就够（照 `scripts/verify_drag_mouse.py` 的 `FakeMouseEvent` 抄）——
    那边 import 会**直接改写**沙箱环境变量（它用的是赋值不是 setdefault），所以不能 import，
    只能抄这 6 行。
    """

    def __init__(self, screen: wl.Point, origin: wl.Rect) -> None:
        self._local = (screen.x - origin.x, origin.height - (screen.y - origin.y))

    def locationInWindow(self):
        return _CocoaPoint(*self._local)


def event_for(mouse: wl.Point, rect: wl.Rect) -> _FakeMouseEvent:
    return _FakeMouseEvent(mouse, rect)


# —— 图像：按字节读，不按像素读 ——
# `pixel_stats.py` 那套 `colorAtX_y_` 每像素一次调用，10 万像素级还行、百万像素级（桌面区域
# 抓图）要几十秒。脚本内部要**每帧**量一次 alpha（连拍 27 帧），所以走 CGDataProvider 的原始字节。


def read_rgba(quartz, path: Path) -> tuple[int, int, int, int, bytes]:
    url = quartz.CFURLCreateWithFileSystemPath(None, str(path), quartz.kCFURLPOSIXPathStyle, False)
    src = quartz.CGImageSourceCreateWithURL(url, None)
    image = quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    width = int(quartz.CGImageGetWidth(image))
    height = int(quartz.CGImageGetHeight(image))
    stride = int(quartz.CGImageGetBytesPerRow(image))
    depth = int(quartz.CGImageGetBitsPerPixel(image)) // 8
    data = bytes(quartz.CGDataProviderCopyData(quartz.CGImageGetDataProvider(image)))
    return width, height, stride, depth, data


def band_alpha(rgba: tuple[int, int, int, int, bytes], y0: float, y1: float) -> float:
    """某一横条里 alpha 的均值（含全透明像素）—— 气泡淡化时它单调掉到 0。"""
    width, height, stride, depth, data = rgba
    if depth != 4:
        return float("nan")
    lo = max(0, int(y0 * height))
    hi = min(height, int(y1 * height))
    if hi <= lo:
        return float("nan")
    total = 0
    for y in range(lo, hi):
        row = data[y * stride : (y + 1) * stride]
        for x in range(width):
            total += row[x * 4 + 3]
    return total / ((hi - lo) * width * 255.0)


def fast_opaque_pct(rgba: tuple[int, int, int, int, bytes], alpha_cut: int = 88) -> float:
    """非透明像素占比（百分数）—— 口径与 `pixel_stats.py` / `appkit_screenshots.py` 同源。"""
    width, height, stride, depth, data = rgba
    if depth != 4:
        return float("nan")
    opaque = 0
    for y in range(height):
        row = data[y * stride : (y + 1) * stride]
        for x in range(width):
            if row[x * 4 + 3] > alpha_cut:
                opaque += 1
    return opaque / (width * height) * 100.0


def window_rect(quartz, number: int):
    for info in quartz.CGWindowListCopyWindowInfo(
        quartz.kCGWindowListOptionIncludingWindow, number
    ) or []:
        bounds = info.get("kCGWindowBounds")
        if bounds:
            return (bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"])
    return None


def union_padded(rects, pad: float, bounds) -> tuple[float, float, float, float]:
    """若干矩形的并集 + 四周留白，再夹进 ``bounds``（屏幕外没有像素可取）。"""
    x0 = min(r[0] for r in rects) - pad
    y0 = min(r[1] for r in rects) - pad
    x1 = max(r[0] + r[2] for r in rects) + pad
    y1 = max(r[1] + r[3] for r in rects) + pad
    x0, y0 = max(x0, float(bounds[0])), max(y0, float(bounds[1]))
    x1 = min(x1, float(bounds[0] + bounds[2]))
    y1 = min(y1, float(bounds[1] + bounds[3]))
    return (x0, y0, x1 - x0, y1 - y0)


def frame_cleanliness(rgba: tuple[int, int, int, int, bytes], region, keep_rects) -> dict:
    """取景框里**除主体之外**还剩什么像素 —— 「图里不许出现别人的东西」的机器判据。

    衬底是两层很暗的蓝灰渐变，最大单通道 :data:`BACKDROP_BOTTOM` 的 0.224 <
    :data:`BRIGHT_LIMIT`；所以「把桌宠与菜单按窗口服务器的真实矩形挖掉之后还有亮像素」
    = 有别人的窗口（聊天文字、别的桌宠、鼠标指针…）漏进了这张要进公开仓库的图。

    返回 ``{"outside_px", "bright_px", "brightest", "colors"}`` —— 亮像素数为 0 才算干净。
    """
    width, height, stride, depth, data = rgba
    if depth != 4:
        return {"outside_px": 0, "bright_px": 0, "brightest": 0.0, "colors": 0}
    scale_x = width / region[2]
    scale_y = height / region[3]

    def px_rect(rect):  # CG 全局坐标 → 本图的像素范围
        x0 = int((rect[0] - region[0]) * scale_x) - 2
        y0 = int((rect[1] - region[1]) * scale_y) - 2
        x1 = int((rect[0] + rect[2] - region[0]) * scale_x) + 2
        y1 = int((rect[1] + rect[3] - region[1]) * scale_y) + 2
        return max(0, x0), max(0, y0), min(width, x1), min(height, y1)

    keep = [px_rect(r) for r in keep_rects]
    outside = bright = 0
    brightest = 0.0
    colors: set[tuple[int, int, int]] = set()
    for y in range(height):
        spans = sorted((x0, x1) for x0, y0, x1, y1 in keep if y0 <= y < y1)
        scan: list[tuple[int, int]] = []
        cursor = 0
        for x0, x1 in spans:  # 挖掉主体那几列，剩下的一段段看
            if x0 > cursor:
                scan.append((cursor, x0))
            cursor = max(cursor, x1)
        if cursor < width:
            scan.append((cursor, width))
        row = data[y * stride : (y + 1) * stride]
        for start, stop in scan:
            for x in range(start, stop):
                if row[x * 4 + 3] < 8:
                    continue
                outside += 1
                red, green, blue = row[x * 4], row[x * 4 + 1], row[x * 4 + 2]
                level = max(red, green, blue) / 255.0
                brightest = max(brightest, level)
                if level > BRIGHT_LIMIT:
                    bright += 1
                else:
                    colors.add((red >> 5, green >> 5, blue >> 5))
    return {
        "outside_px": outside,
        "bright_px": bright,
        "brightest": round(brightest, 3),
        "colors": len(colors),
    }


# —— 驱动 ——


class Shooter(Driver):
    """`appkit_screenshots.Driver` 的抓图方式换成本次的交付口径：

    * 文件名就是交付名（中文），直接写进 `--out`；
    * **每条记录都带 `how`**（由抓图那一步自己盖：:data:`HOW_WINDOW` / :data:`HOW_COMPOSITE`），
      缺 `how` 直接判红 —— 按字段分派的判据最容易出的错就是「字段缺了 = 默认绿」；
    * **当场过闸 + 当场落账**：几何 / 像素 / 红线三项都在 :meth:`_record` 里做完写进记录，
      并立刻 `write_evidence()`（一次中断最多让最后一张没账，不是整批没账）。

    拖拽、贴边、光标注入这些**照用父类**（不重写一条并行路径）。
    """

    def __init__(self, *args, out: Path, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.final_out = out

    # —— 三道闸 ——

    def _geometry_gate(self, how: str, path: Path, rgba, region, sources) -> str:
        """几何闸：图与「窗口服务器说的那个矩形」对得上吗。返回 `"ok"` 或失败原因。

        * `window`：PNG 的像素尺寸必须 == 窗口矩形 × 缩放（对不上说明抓的不是这扇窗 / 缩放变了）；
        * `window-composite`：每个源窗口的矩形都必须**完整落在取景框里**（少了就是半截窗口），
          且画布像素尺寸 == 取景框 × 缩放。
        """
        if how == HOW_WINDOW:
            if not sources or not sources[0].get("rect"):
                return "窗口服务器里读不到这扇窗的矩形 ⇒ 退不出「图 == 窗口」这条几何关系"
            rect = sources[0]["rect"]
            scale = rgba[0] / rect[2]
            want = (round(rect[2] * scale), round(rect[3] * scale))
            if (rgba[0], rgba[1]) != want:
                return f"窗口图 {rgba[0]}×{rgba[1]}px 与窗口矩形 {rect} × 缩放 {scale:g} 不符（应为 {want}）"
            return "ok"
        if how == HOW_COMPOSITE:
            for source in sources:
                rect = source["rect"]
                if not (
                    rect[0] >= region[0] - 0.5
                    and rect[1] >= region[1] - 0.5
                    and rect[0] + rect[2] <= region[0] + region[2] + 0.5
                    and rect[1] + rect[3] <= region[1] + region[3] + 0.5
                ):
                    return f"{source['what']} 窗口 {rect} 没被取景框 {region} 完整包住"
            scale = rgba[0] / region[2]
            want = (round(region[2] * scale), round(region[3] * scale))
            if (rgba[0], rgba[1]) != want:
                return f"画布 {rgba[0]}×{rgba[1]}px 与取景框 {region} × 缩放 {scale:g} 不符（应为 {want}）"
            return "ok"
        return f"未知 how={how!r}，没法判几何"

    def _pixel_gate(self, how: str, rgba, region, sources, opaque_pct: float) -> tuple[str, dict | None]:
        """像素闸：`window` 判非空（非透明占比 ≥ 2%）；`window-composite` 判**底色上不许有亮像素**。

        为什么合成件不看「非透明占比」：底色是铺满的 ⇒ 那个数恒等于 100%，什么也证明不了。
        为什么不看 `outside_px`：那是**扫描区面积**（干净样本上就有 77,088），拿它当判据会
        把好图判红。判据只有两条：`bright_px == 0`（暗底色之外不许有亮东西）
        且 `colors ≤ ` :data:`COLORS_LIMIT`（底本色 + 抗锯齿的极少几种）。
        """
        if how == HOW_WINDOW:
            if opaque_pct < EMPTY_OPAQUE_PCT:
                return f"非透明占比 {opaque_pct}% < {EMPTY_OPAQUE_PCT}% ⇒ 判成空图", None
            return "ok", None
        if how == HOW_COMPOSITE:
            clean = frame_cleanliness(rgba, region, [s["rect"] for s in sources])
            if clean["bright_px"]:
                return (
                    (
                        f"底色之外有 {clean['bright_px']} 个亮像素（最亮 {clean['brightest']}）"
                        "⇒ 主体之外还有别的东西"
                    ),
                    clean,
                )
            if clean["colors"] > COLORS_LIMIT:
                return (
                    f"底色区出现 {clean['colors']} 种色（上限 {COLORS_LIMIT}）⇒ 底上不止底色",
                    clean,
                )
            return "ok", clean
        return f"未知 how={how!r}，没法判像素", None

    def _record(
        self,
        name: str,
        note: str,
        path: Path,
        rgba,
        probe: dict,
        *,
        how: str = "",
        region=None,
        sources: list[dict] | None = None,
        **extra,
    ) -> dict:
        width, height = rgba[0], rgba[1]
        sources = sources or []
        opaque_pct = round(fast_opaque_pct(rgba), 2)
        gates: dict[str, str] = {}
        if how not in HOW_VALUES:
            gates["how"] = f"记录里缺/不认识 how={how!r}（只认 {list(HOW_VALUES)}）"
        else:
            gates["how"] = "ok"
        if gates["how"] == "ok":
            gates["geometry"] = self._geometry_gate(how, path, rgba, region, sources)
            verdict, clean = self._pixel_gate(how, rgba, region, sources, opaque_pct)
            gates["pixels"] = verdict
        else:
            clean = None
        record = {
            "file": f"{name}.png",
            "note": note,
            "how": how,
            "size_px": [width, height],
            "opaque_pct_fast": opaque_pct,
            "region": [round(v, 1) for v in region] if region else None,
            "sources": sources,
            "gates": gates,
            "cleanliness": clean,
            "probe": {
                key: probe.get(key)
                for key in (
                    "window_number",
                    "ns_frame",
                    "dock",
                    "edge",
                    "level",
                    "badge_drawn",
                    "badge_alpha",
                    "click_action",
                    "view_size",
                )
            },
            "at": time.strftime("%H:%M:%S"),
            **extra,
        }
        record["failures"] = [f"{k}：{v}" for k, v in gates.items() if v != "ok"] + list(
            extra.pop("failures", []) or []
        )
        records.append(record)
        say(
            f"  ✔ {record['file']:<26} {how:<16} {width}×{height}px  非透明 {opaque_pct:.1f}%"
            f"  闸={','.join(k for k, v in gates.items() if v != 'ok') or '全过'}"
            + (f"  {extra.get('extra_note')}" if extra.get("extra_note") else "")
        )
        if record["failures"]:
            failures.extend(f"{record['file']} ⇒ {item}" for item in record["failures"])
        write_evidence()  # 边拍边落账：中断的代价不许是「一份账都没有」
        return record

    def shoot_window(self, name: str, note: str) -> dict:
        """窗口本体（保留透明通道）—— 气泡那 8 张都用它。`how=window`。"""
        require_awake_display(self.quartz)
        probe = self.backend.probe()
        path = self.final_out / f"{name}.png"
        if not capture_window(self.quartz, probe["window_number"], path):
            raise RuntimeError(f"抓窗口失败：{name}")
        rgba = read_rgba(self.quartz, path)
        info = window_info(self.quartz, probe["window_number"])
        source = {
            "what": "pet",
            "window": probe["window_number"],
            "rect": list(info[0]) if info else None,
            "image_px": [rgba[0], rgba[1]],
            "layer": info[1] if info else None,
        }
        return self._record(
            name, note, path, rgba, probe, how=HOW_WINDOW, sources=[source] if info else []
        )

    def shoot_composite(
        self,
        name: str,
        note: str,
        region,
        sources_window: list[tuple[str, int]],
        *,
        extra: dict | None = None,
    ) -> dict:
        """**窗口级抓图 → 合成到中性底色**（需要「窗口在屏上的相对位置」的 3 张用它）。

        `sources_window` = `[(这是什么, 窗口号), …]`，顺序即叠放顺序。这里做三件事：
        ① 每扇窗单独抓一张窗口级 PNG（内容不含任何别的窗口）；② 按窗口服务器给的真实矩形贴到
        一块纯色画布上；③ 当场过几何/像素闸（见 :meth:`_geometry_gate` / :meth:`_pixel_gate`）。

        抓不到某扇窗的分窗口图就**直接报错**（不当成"背景露出来"勉强交图）：菜单是独立窗口
        （层 101），窗口级抓图实测拿得到；拿不到就说明这条路在这台机器上不成立，得人来看。
        """
        require_awake_display(self.quartz)
        probe = self.backend.probe()
        scratch = SANDBOX / "parts"
        scratch.mkdir(parents=True, exist_ok=True)
        parts = []
        sources = []
        for what, number in sources_window:
            part_path = scratch / f"{name}-{what}.png"
            image = capture_window_image(self.quartz, number, part_path)
            if image is None:
                raise RuntimeError(f"窗口级抓图失败（{what} #{number}）：{name}")
            info = window_info(self.quartz, number)
            if info is None:
                raise RuntimeError(f"窗口服务器里读不到 {what} #{number}：{name}")
            rect, layer, title = info
            scale = self.quartz.CGImageGetWidth(image) / rect[2]
            parts.append((rect, image, scale))
            sources.append(
                {
                    "what": what,
                    "window": number,
                    "rect": [round(v, 1) for v in rect],
                    "image_px": [
                        int(self.quartz.CGImageGetWidth(image)),
                        int(self.quartz.CGImageGetHeight(image)),
                    ],
                    "layer": layer,
                    "title": title,
                }
            )
        path = self.final_out / f"{name}.png"
        if not compose_windows(self.quartz, region, parts, path):
            raise RuntimeError(f"合成失败：{name}")
        rgba = read_rgba(self.quartz, path)
        return self._record(
            name, note, path, rgba, probe,
            how=HOW_COMPOSITE, region=region, sources=sources,
            canvas="中性底色（脚本铺的，非桌面）", **(extra or {}),
        )


def click_pet(shooter: Shooter, point: wl.Point, *, linger_after: float = 0.18) -> None:
    """走**真机同一条**按下/松手路径点一下桌宠（`_mouse_down` → `_mouse_up` → click_action）。

    必须 0.08s 内松手：`from_mouse=True` 的拖拽兜底过了 `_DRAG_BUTTON_UP_GRACE` 就会自己
    `end_drag()`，那一下 `_mouse_up` 会因为「没在拖」直接返回 ⇒ **点击被兜底吃掉、气泡不出来**
    （`verify_drag_mouse.py` 的 tap() 也是这个节奏）。
    """
    backend = shooter.backend
    backend._last_click_at = 0.0
    backend._mouse_down(event_for(point, backend._window_local))
    backend.linger(0.03)
    backend._mouse_up(None)
    backend.linger(linger_after)


def set_click_action(shooter: Shooter, action: str) -> None:
    """把档位写进**沙箱 settings.json**，等桌宠自己重读（每 5s 一读）。

    故意不直接改 `backend._settings`：这张图要证明的就是「设置文件说了算」——
    图是从文件那一路读出来的才作数。
    """
    settings_store.save({"click_action": action}, SETTINGS)
    shooter.backend.linger(0.35)  # 桌宠 5s 一读，给它一拍
    shooter.backend._reload_settings(force=True)  # 再推一把，免得正好卡在 4.9s 上
    got = shooter.backend._click_action()
    if got != action:
        raise RuntimeError(f"设置没生效：想要 {action}，桌宠读到 {got}")


def hide_bubble(shooter: Shooter) -> None:
    shooter.backend._hide_badge()
    shooter.backend.linger(0.3)


def deploy_quota(report: dict | None, *, age_s: float = 0.0, mtime_path: Path | None = None) -> None:
    """把一份额度报告放进沙箱。``age_s`` 只改**文件 mtime**（陈旧判据看的就是它，不是报告里的 at）。"""
    if report is None:
        QUOTA_COPY.unlink(missing_ok=True)
        return
    QUOTA_COPY.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if age_s:
        stamp = time.time() - age_s
        os.utime(QUOTA_COPY, (stamp, stamp))


def run_final_checks() -> None:
    """收尾：11 张成品跑一遍 `pixel_stats.py`（官方判空图口径）+ 红线自查。

    单独成函数是因为它**可能在 worker 线程里被调**：菜单关不掉时的兜底路径要「先把账做完再硬退出」
    （不然进程一死，连一份账都没有）。
    """
    if OUT_DIR is None:
        return
    pngs = [OUT_DIR / f"{name}.png" for name in SHOT_NAMES]
    missing = [p.name for p in pngs if not p.exists()]
    if missing:
        failures.append("没产出：" + "、".join(missing))
    say("\n— pixel_stats.py（官方判空图口径）—")
    stats = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pixel_stats.py"),
         *[str(p) for p in pngs if p.exists()]],
        capture_output=True, text=True, check=False,
    )
    say(stats.stdout.strip())
    stat_lines = {
        line.split(":", 1)[0].strip(): line for line in stats.stdout.splitlines() if ":" in line
    }
    for record in records:
        line = stat_lines.get(record["file"]) or stat_lines.get(record["file"].split(".")[0])
        if line:
            record["pixel_stats"] = line
            if "不透明" in line:
                pct = float(line.split("（")[1].split("%")[0])
                record["opaque_pct"] = pct
                if pct < EMPTY_OPAQUE_PCT:
                    failures.append(f"{record['file']} 非透明占比 {pct}% ⇒ 判成空图")
    hits = redline_scan()
    if hits:
        # 只报**位置**（不回显命中内容），所以这条判据不会自己把自己弄红
        failures.append("红线自查：落盘产物的字段值里有本机家目录路径 ⇒ " + "；".join(hits))
    write_evidence()


def main() -> int:
    parser = argparse.ArgumentParser(description="重拍 docs/design 的 10 张界面实拍")
    parser.add_argument("--out", default=str(ROOT / "docs" / "design"), help="成品 PNG 落哪")
    parser.add_argument("--evidence", default=None, help="证据 JSON（默认 $HOME/.xiaocc-shots/）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    parser.add_argument("--keep-sandbox", action="store_true", help="保留沙箱目录（排障用）")
    args = parser.parse_args()

    out = Path(args.out)
    evidence_path = (
        Path(args.evidence)
        if args.evidence
        else Path.home() / ".xiaocc-shots" / "design-shots.evidence.json"
    )
    global OUT_DIR, EVIDENCE_PATH
    OUT_DIR, EVIDENCE_PATH = out, evidence_path

    quartz = _load_quartz()
    require_awake_display(quartz)  # 屏睡时抓出来是全透明图 ⇒ 早拒早干净
    say(f"沙箱 {SANDBOX}")
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)

    # —— 替身数据：调仓库里现成的那支脚本，不抄它的逻辑 ——
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_shot_fixtures.py"),
         "--out", str(FIXTURES), "--hermes-dir", str(DEMO_HERMES)],
        capture_output=True, text=True, check=False,
    )
    if done.returncode != 0:
        say(f"替身脚本失败：\n{done.stdout}\n{done.stderr}")
        return 1
    normal_report = json.loads((FIXTURES / "quota.json").read_text(encoding="utf-8"))
    deploy_quota(normal_report)

    character = load_character(None)
    holder: dict = {}
    backend = AppKitBackend(cursor=lambda: holder["driver"].cursor[0], at=START_AT)
    backend.interval = 1.0 / 30.0  # 正常由 CLI 写入；脚本里手动对齐（照 appkit_screenshots）

    main_space = backend._space()
    shooter = Shooter(
        backend, character, SANDBOX / "scratch", quartz, main_space,
        main_height=main_space.origin_y + main_space.height, out=out,
    )
    holder["driver"] = shooter  # 光标来源那个 lambda 靠它解环（Driver 自带的 cursor 列表）
    display = quartz.CGDisplayBounds(quartz.CGMainDisplayID())
    display_rect = (display.origin.x, display.origin.y, display.size.width, display.size.height)

    plan_bubbles = [
        ("气泡-额度", "badge", "单击档位=额度（settings.json 里写的 badge）"),
        ("气泡-设备状态", "device", "单击档位=设备状态（device）"),
        ("气泡-额度+设备", "all", "单击档位=额度+设备（all，部署默认）"),
    ]

    try:
        backend.render(Render(event=StatusEvent(source="demo", state=State.IDLE), character=character))
        backend.linger(1.4)  # 开窗口 + 画第一帧 + 让设备采样器的 1s CPU 窗口攒够
        pet_rect = window_rect(quartz, backend.probe()["window_number"])
        if pet_rect is None:
            say("窗口服务器读不到沙箱窗口，放弃")
            return 1
        center = wl.Point(pet_rect[0] + pet_rect[2] / 2.0, pet_rect[1] + pet_rect[3] / 2.0)
        say(f"沙箱桌宠窗口 {tuple(round(v) for v in pet_rect)}（真桌宠另在别处，本脚本不碰它）")

        if args.dry_run:
            say("--dry-run：不落盘，只报告计划")
            for name, action, note in plan_bubbles:
                say(f"  会拍 {name}.png · {note}")
            say("  会拍 气泡-陈旧 / 气泡-未采集 / 气泡-满 / 气泡-淡化中 / 气泡-退干净")
            say("  会拍 右键菜单.png · 贴边把手条-收起.png · 贴边把手条-展开后.png（都是窗口级抓图 + 中性底色合成）")
            return 0

        # ——————— 1~3：三档气泡 ———————
        say("— 三档气泡 —")
        for name, action, note in plan_bubbles:
            set_click_action(shooter, action)
            click_pet(shooter, center)
            record = shooter.shoot_window(name, note)
            if not record["probe"]["badge_drawn"]:
                failures.append(f"{name}：气泡没画上去")
            hide_bubble(shooter)

        # ——————— 4：陈旧（文件 mtime 老 + 报告里 state=stale）———————
        say("— 陈旧 / 未采集 —")
        stale_report = json.loads(json.dumps(normal_report))
        for service in stale_report.get("services", []):
            if service.get("state") == "ok":
                service["state"] = "stale"
                service["detail"] = "上次拿到了、这次刷不出来"
        stale_report["updated_at"] = "2026-09-30T00:00:00+08:00"
        deploy_quota(stale_report, age_s=65 * 60.0)  # 65 分钟：>30min 陈旧线，且读作「1 小时前」
        set_click_action(shooter, "badge")
        click_pet(shooter, center)
        record = shooter.shoot_window("气泡-陈旧", "额度报告 state=stale + 文件 65 分钟没更新 ⇒ 印「陈旧」不印旧数字")
        if "陈旧" not in (record["probe"]["badge_drawn"] or ""):
            failures.append("气泡-陈旧：气泡里没有「陈旧」")
        hide_bubble(shooter)

        # ——————— 5：未采集（额度文件不存在）———————
        deploy_quota(None)
        click_pet(shooter, center)
        record = shooter.shoot_window("气泡-未采集", "额度文件不存在 ⇒ 「额度未采集」（取不到就是取不到，不补 0）")
        if "未采集" not in (record["probe"]["badge_drawn"] or ""):
            failures.append("气泡-未采集：气泡里没有「未采集」")
        hide_bubble(shooter)

        # ——————— 6~8：淡化三帧（连拍 + 按实测 alpha 选帧）———————
        # 连拍要**密**（0.1s）：1.2s 的线性淡化里，0.25s 一档就是 ±0.21 alpha ——
        # 那样「淡化中」只能在「几乎没淡」和「淡没了」之间二选一，选不出「取深一点」。
        say(f"— 淡化三帧（连拍 {BURST_SECONDS}s / {BURST_INTERVAL_S}s 一张）—")
        deploy_quota(normal_report, age_s=NORMAL_AGE_S)
        set_click_action(shooter, "all")
        burst_dir = SANDBOX / "burst"
        burst_dir.mkdir(parents=True, exist_ok=True)
        # 参考帧：**气泡没画上去**的同一扇窗 —— 用来定位气泡在哪一横条（自校准，不写死几何）
        hide_bubble(shooter)
        ref_path = burst_dir / "ref.png"
        capture_window(quartz, backend.probe()["window_number"], ref_path)
        click_pet(shooter, center, linger_after=0.0)
        backend.linger(0.06)  # 先把点击后那一帧画上屏，别让第 1 帧是「气泡还没画」
        frames: list[dict] = []
        t0 = time.monotonic()
        index = 0
        while time.monotonic() - t0 < BURST_SECONDS:
            index += 1
            path = burst_dir / f"frame-{index:03d}.png"
            probe = backend.probe()
            if not capture_window(quartz, probe["window_number"], path):
                break
            rgba = read_rgba(quartz, path)
            frames.append(
                {
                    "path": path,
                    "t": round(time.monotonic() - t0, 3),
                    "alpha": probe.get("badge_alpha"),
                    "drawn": probe.get("badge_drawn"),
                    # 上下两条各量一次：气泡在哪一条里，事后按「哪条摆得最大」自动认出来
                    "top": band_alpha(rgba, 0.0, BAND_FRACTION),
                    "bot": band_alpha(rgba, 1 - BAND_FRACTION, 1.0),
                }
            )
            backend.linger(max(0.0, BURST_INTERVAL_S - (time.monotonic() - t0 - (frames[-1]["t"]))))
        say(f"  连拍 {len(frames)} 帧：气泡 alpha {frames[0]['alpha']} → {frames[-1]['alpha']}")
        ref_rgba = read_rgba(quartz, ref_path)
        say(
            f"  无气泡参考帧：上段 {band_alpha(ref_rgba, 0.0, BAND_FRACTION):.3f}"
            f" / 下段 {band_alpha(ref_rgba, 1 - BAND_FRACTION, 1.0):.3f}"
        )
        swing = {
            "上": max(f["top"] for f in frames) - min(f["top"] for f in frames),
            "下": max(f["bot"] for f in frames) - min(f["bot"] for f in frames),
        }
        band = (0.0, BAND_FRACTION) if swing["上"] >= swing["下"] else (1 - BAND_FRACTION, 1.0)
        key = "top" if band[0] == 0.0 else "bot"
        say(f"  气泡带定位：上段摆幅 {swing['上']:.3f} / 下段摆幅 {swing['下']:.3f} ⇒ 取 {key} 段")
        for frame in frames:
            frame["band_alpha"] = frame[key]
        peak = max(f["band_alpha"] for f in frames)
        full = min((f for f in frames if f["band_alpha"] >= 0.9 * peak), key=lambda f: f["t"])
        mid = min(
            (f for f in frames if f["t"] > BURST_SECONDS - 2.0),
            key=lambda f: abs(f["band_alpha"] - FADE_MID_FRACTION * peak),
        )
        gone = next(
            (f for f in frames if f["band_alpha"] <= 0.02 * peak and not (f["drawn"] or "")),
            None,
        )
        if gone is None:
            failures.append("气泡-退干净：连拍里没有「气泡不在了」的那一帧")
            gone = frames[-1]
        say(
            f"  选帧：满 t={full['t']}s band={full['band_alpha']:.3f} · "
            f"淡化中 t={mid['t']}s band={mid['band_alpha']:.3f}（目标 {FADE_MID_FRACTION * peak:.3f}）· "
            f"退干净 t={gone['t']}s band={gone['band_alpha']:.3f}"
        )
        if mid["band_alpha"] > 0.5 * peak:
            failures.append(
                f"气泡-淡化中：挑到的帧太亮（band={mid['band_alpha']:.3f}，峰值 {peak:.3f}）"
            )
        for name, frame, note in (
            ("气泡-满", full, "气泡满亮度那一帧（TTL 5s 里的前 3.8s）"),
            ("气泡-淡化中", mid, f"最后 1.2s 线性淡化里取到 ≈{FADE_MID_FRACTION:.2f} 满亮度的那一帧（明显变淡、字仍读得出）"),
            ("气泡-退干净", gone, "淡完了：窗口里没有气泡，只剩角色"),
        ):
            path = out / f"{name}.png"
            shutil.copyfile(frame["path"], path)
            frame_probe = backend.probe()
            frame_number = frame_probe["window_number"]
            frame_info = window_info(quartz, frame_number)
            frame_rgba = read_rgba(quartz, path)
            # 这三帧也是**窗口级抓图**（连拍那一路就是 `capture_window`）⇒ how 由这条路盖
            record = shooter._record(
                name, note, path, frame_rgba, frame_probe,
                how=HOW_WINDOW,
                sources=[
                    {
                        "what": "pet",
                        "window": frame_number,
                        "rect": list(frame_info[0]) if frame_info else None,
                        "image_px": [frame_rgba[0], frame_rgba[1]],
                        "layer": frame_info[1] if frame_info else None,
                    }
                ],
                burst_t=frame["t"], burst_alpha=frame["alpha"], burst_drawn=frame["drawn"],
                band_alpha=round(frame["band_alpha"], 3),
                band=[round(band[0], 2), round(band[1], 2)], band_key=key,
                peak_band_alpha=round(peak, 3), burst_frames=len(frames),
            )
            if name == "气泡-退干净" and record["opaque_pct_fast"] < 2.0:
                failures.append("气泡-退干净：几乎是空图")
        hide_bubble(shooter)

        # ——————— 9~10：贴边把手条（收起 / 展开后 各一张）———————
        say("— 贴边把手条（收起 → 单击展开）—")
        for record in shoot_handle_strip(shooter, quartz, display_rect, backend, character):
            if record.get("failures"):
                failures.extend(record.pop("failures"))

        # ——————— 11：右键菜单（**放最后**）———————
        # 为什么最后：菜单是模态窗口，主线程要等它关掉才出得来。万一两条收菜单的路都没成，
        # 兜底是「把账做完 → 硬退出」（见 shoot_menu 的 worker），放最后才不会耽误别的张。
        say("— 右键菜单 —")
        set_click_action(shooter, "all")
        # 贴边那两张把桌宠留在右边缘了 ⇒ 先拖回原位再弹菜单（同一扇窗，不留两个位置说不清）
        shooter.drag_to(pet_rect[0], pet_rect[1])
        backend.end_drag()
        backend.linger(0.5)
        now = window_rect(quartz, backend.probe()["window_number"]) or pet_rect
        menu_center = wl.Point(now[0] + now[2] / 2.0, now[1] + now[3] / 2.0)
        say(f"  菜单点击点 {tuple(round(v) for v in (now or (0, 0, 0, 0)))} 中心 {menu_center}")
        record = shoot_menu(shooter, menu_center, quartz, display_rect, backend)
        if record.get("failures"):
            failures.extend(record.pop("failures"))
    finally:
        with suppress(Exception):
            backend.close()
        # 合成事件会把系统指针挪走 ⇒ 还到一个不碍事的纯色位（别留在测试点，
        # 也别落在真桌宠那条上 —— 那会让它展开）
        with suppress(Exception):
            post_mouse(quartz, quartz.kCGEventMouseMoved, 1150.0, 900.0, quartz.kCGMouseButtonLeft)

    # ——————— 收尾：官方判空图口径（pixel_stats.py）+ 红线自查 ———————
    run_final_checks()
    say(f"\n证据 JSON {EVIDENCE_PATH}")

    if not args.keep_sandbox:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    if failures:
        say(f"结果：FAIL（{len(failures)} 项）—— " + "；".join(failures))
        return 1
    say(f"结果：PASS（{len(records)}/10 张）")
    return 0


# —— 右键菜单 ——————————————————————————————————————————————————————————————————————


def post_mouse(quartz, kind: int, x: float, y: float, button: int) -> None:
    """投一个合成鼠标事件（坐标 = 全局显示坐标、左上原点，CGEvent 的口径）。"""
    event = quartz.CGEventCreateMouseEvent(None, kind, (float(x), float(y)), button)
    quartz.CGEventPost(quartz.kCGHIDEventTap, event)


def post_key(quartz, keycode: int) -> None:
    for down in (True, False):
        event = quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)


# —— 窗口级抓图 → 合成 ————————————————————————————————————————————————————————————
# 为什么不再拍桌面：这台机器的桌面被**全屏的 Hermes 聊天窗**占着（聊天区里的文字一直铺到
# x≈1450），`capture_desktop_region` 不管怎么圈都会拍到别人的会话与 `/Users/…` 路径 —— 而图
# 要进公开仓库。窗口级抓图（`kCGWindowListOptionIncludingWindow`）拿的是**那一扇窗自己的内容**，
# 不受遮挡影响、也不可能夹带别人的像素；需要「窗口在屏上的相对位置」时（菜单、把手条），
# 就把**窗与菜单分别抓下来、按它们真实的全局矩形贴到一块中性底色上**（CoreGraphics 合成，
# 不引 PIL）。合成件里除了我们这两三个窗口，只剩我们自己铺的底色 —— 这一点由像素闸
# （:func:`frame_cleanliness`：底色之外不许有亮像素、色数 ≤ :data:`COLORS_LIMIT`）当场验。


def capture_window_image(quartz, number: int, path: Path):
    """窗口级抓一张 PNG，再读回成 ``CGImage``（合成要用）。读不回来返回 ``None``。"""
    if not capture_window(quartz, number, path):
        return None
    url = quartz.CFURLCreateWithFileSystemPath(None, str(path), quartz.kCFURLPOSIXPathStyle, False)
    provider = quartz.CGDataProviderCreateWithURL(url)
    if provider is None:
        return None
    return quartz.CGImageCreateWithPNGDataProvider(
        provider, None, False, quartz.kCGRenderingIntentDefault
    )


def compose_windows(quartz, region, parts, path: Path) -> bool:
    """把若干「窗口级抓图」按各自的全局矩形贴到一块纯色画布上，写成 PNG。

    ``parts`` = ``[(rect, image, scale), …]``，顺序即叠放顺序（后来的压在上面 —— 菜单在桌宠之上）。
    画布尺寸 = 取景框 × 缩放；位图上下文原点在左下，所以 y 要翻一次（CG 画图时图像顶边
    落在目标矩形顶边，翻的是"这个矩形在画布上的位置"）。
    """
    scale = parts[0][2]
    width = round(region[2] * scale)
    height = round(region[3] * scale)
    space = quartz.CGColorSpaceCreateDeviceRGB()
    ctx = quartz.CGBitmapContextCreate(
        None, width, height, 8, width * 4, space, quartz.kCGImageAlphaPremultipliedLast
    )
    if ctx is None:
        return False
    # PyObjC 只桥了一部分 CGContext 方法名（`ctx.setFillColor_` 就不存在）⇒ 一律用模块级 C 函数
    quartz.CGContextSetFillColorWithColor(ctx, quartz.CGColorCreateGenericRGB(*CANVAS_RGB, 1.0))
    quartz.CGContextFillRect(ctx, quartz.CGRectMake(0.0, 0.0, float(width), float(height)))
    for rect, image, part_scale in parts:
        scale_x, scale_y = part_scale, part_scale
        left = (rect[0] - region[0]) * scale_x
        bottom = (region[1] + region[3] - (rect[1] + rect[3])) * scale_y
        quartz.CGContextDrawImage(
            ctx, quartz.CGRectMake(left, bottom, rect[2] * scale_x, rect[3] * scale_y), image
        )
    image = quartz.CGBitmapContextCreateImage(ctx)
    url = quartz.CFURLCreateWithFileSystemPath(None, str(path), quartz.kCFURLPOSIXPathStyle, False)
    dest = quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    if dest is None:
        return False
    quartz.CGImageDestinationAddImage(dest, image, None)
    return bool(quartz.CGImageDestinationFinalize(dest))


def window_info(quartz, number: int):
    """窗口服务器里那扇窗的 ``(矩形, 层级, 名字)``；没有就 ``None``。"""
    for info in quartz.CGWindowListCopyWindowInfo(
        quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGNullWindowID
    ) or []:
        if int(info.get("kCGWindowNumber", 0)) != number:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        if not bounds.get("Width", 0):
            return None
        return (
            (bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"]),
            int(info.get("kCGWindowLayer", 0)),
            str(info.get("kCGWindowName") or ""),
        )
    return None


def menu_window(quartz, pet_number: int):
    """菜单窗口的 ``(number, rect)``：本进程里**层级 ≥ 100** 的那种窗口（NSMenu 是独立窗口）。

    早期版本只按「不是桌宠的那扇窗」认，够用；但合成要**窗口号**才能单独抓它，所以这里
    连层级一起判 —— 层级是菜单的硬特征（实测 101）。
    """
    mine = os.getpid()
    for info in quartz.CGWindowListCopyWindowInfo(
        quartz.kCGWindowListOptionOnScreenOnly, quartz.kCGNullWindowID
    ) or []:
        if int(info.get("kCGWindowOwnerPID", 0)) != mine:
            continue
        number = int(info.get("kCGWindowNumber", 0))
        if number == pet_number or int(info.get("kCGWindowLayer", 0)) < 100:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        if bounds.get("Width", 0) > 0:
            return number, (bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"])
    return None


def dismiss_menu(menu, quartz, pet_number, pet_rect, state: dict) -> None:
    """关掉菜单。**这一步第一版翻过车**（进程挂死、菜单留在屏上），所以这里写清楚「凭什么」。

    实测结论（`probe_menu_dismiss.py` 逐变体跑出来的，不是猜的）：

    * `menu.cancelTracking()` 在 **worker 线程**里调用**无效** —— AppKit 这个调用只在主线程
      算数，而主线程这时正卡在 `popUpMenuPositioningItem…` 的模态跟踪循环里出不来；
    * `menu.performActionForItemAtIndex_(0)` **不关菜单**（工作者/主线程两条路都试过）；
    * ✅ **把 `cancelTracking` 派给主线程**（`performSelectorOnMainThread`）—— 模态循环的
      runloop 会把这个 perform 抽走执行 ⇒ 菜单窗口立刻从窗口列表里消失；
    * ✅ 兜底：往**本进程**的事件队列塞一个左键 `mouseDown`（落点在自己窗口左上角，必在菜单框外）。
      注意是**本进程队列**，不是 HID —— 合成 HID Escape 会投给当前活跃 app（用户那个聊天窗），
      替别人的界面按 Esc 不是我们该干的事。

    每一步都**回读窗口列表**确认真没了，才敢写下 `dismiss`；最后仍然在屏上就记
    ``still_open``（调用方据此判红，并走硬退出兜底）。
    """
    from Foundation import NSNumber

    menu.performSelectorOnMainThread_withObject_waitUntilDone_(b"cancelTracking", None, False)
    for _ in range(30):  # 最多等 3s
        time.sleep(0.1)
        if menu_window(quartz, pet_number) is None:
            state["dismiss"] = "主线程 cancelTracking（performSelectorOnMainThread）"
            return
    from AppKit import NSApp, NSEvent

    key = getattr(NSEvent, "NSEventTypeLeftMouseDown", 1)
    number = int((pet_rect or (0, 0, 0, 0))[0])  # 事件挂在哪个窗口号上无所谓，落点在自己窗内即可
    event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        key, (4.0, 4.0), 0, 0.0, number, None, 1, 1, 1.0
    )
    NSApp.postEvent_atStart_(event, True)
    for _ in range(30):
        time.sleep(0.1)
        if menu_window(quartz, pet_number) is None:
            state["dismiss"] = "本进程队列 mouseDown（postEvent，落点在自己窗口左上角）"
            return
    state["dismiss"] = "两条路都没关掉"
    state["still_open"] = True
    _ = NSNumber  # 保留 import 的意图：performSelector 传参得是 NSObject


def dock_region(rect, screen, *, left: float = 140.0, pad: float = 34.0):
    """贴边件的取景框：**右沿顶到屏幕右沿**（"贴在边上"这件事得看得见），左侧留 `left` 宽空底。

    屏幕外没有像素可取，所以右边自然收在屏幕沿上。
    """
    x0 = max(screen.x, rect[0] - left)
    x1 = min(screen.right, rect[0] + rect[2])
    y0 = max(screen.y, rect[1] - pad)
    y1 = min(screen.bottom, rect[1] + rect[3] + pad)
    return (x0, y0, x1 - x0, y1 - y0)


def shoot_menu(shooter: Shooter, center: wl.Point, quartz, display_rect, backend) -> dict:
    """右键 ⇒ 菜单：**菜单是独立窗口**（实测层 101）⇒ 桌宠窗口与菜单窗口**各做一次窗口级抓图**，
    再按两者的真实全局矩形合成到中性底色上 —— 桌面一个像素都不进图（模块 docstring 红线 2）。

    四个坑都记在这儿：

    1. `popUpMenuPositioningItem_atLocation_inView_` 会**阻塞到菜单关闭** ⇒ 主线程进去就出不来，
       「等菜单上屏 → 抓两张窗口图 → 合成 → 关菜单」必须交给 worker 线程；
    2. 合成要**窗口号**才能单独抓菜单；菜单的宽高跟设备行的字数有关（这台机器内存/磁盘那两行
       很长），所以矩形一律拿窗口服务器的真值，不眼估；
    3. 关菜单见 :func:`dismiss_menu`：实测只有「把 `cancelTracking` 派给主线程」与「往本进程
       队列塞一个 mouseDown」管用，worker 里直接 `cancelTracking` 会**挂死**（早期版本就死在这儿）；
    4. **合成点击要先"热"一下**：窗口不是 key 的时候，第一发合成点击会被丢掉 —— 实测不先发一个
       `MouseMoved` 并等一拍，`_right_mouse_down` **一次都不会被调用**。

    先走**合成右键事件**（真事件那条路）；它要是没弹出来，退回**直接调 `_right_mouse_down`**
    （同一条代码路径：`_build_menu` → `popUp…`），证据里标 `fallback_direct_call=true`
    （两种绿不许长得一样）。
    """
    pet_number = backend.probe()["window_number"]
    pet_rect = window_rect(quartz, pet_number)
    shooter.cursor[0] = wl.Point(center.x, center.y)  # 光标进热区：非 key 窗口靠这个翻
    backend.linger(0.4)  # `ignoresMouseEvents`，不翻 false 的话合成事件根本进不来
    say(f"  光标进热区：ignores_mouse_events={backend.probe()['ignores_mouse_events']}")

    menu = backend._build_menu()  # 自己留一份引用：worker 收菜单要用同一个对象
    backend._build_menu = lambda: menu  # 同一份代码，只是记住它
    # 合成右键**到底有没有投递到**这一层：投递了但菜单窗口没上来，与根本没投递，是两回事
    delivered: list[float] = []
    original_right_down = backend._right_mouse_down

    def counted_right_down(event, view=None):
        delivered.append(time.monotonic())
        return original_right_down(event, view)

    backend._right_mouse_down = counted_right_down
    state: dict[str, Any] = {"menu": None, "region": None, "record": None, "dismiss": None,
                             "fallback_direct_call": False, "still_open": False, "done": False}

    def worker(wait_s: float) -> None:
        """等菜单上屏 → 抓两张窗口图 → 合成 → 关菜单。只在 worker 线程里跑。"""
        try:
            deadline = time.monotonic() + wait_s
            found = None
            while found is None and time.monotonic() < deadline:
                found = menu_window(quartz, pet_number)
                time.sleep(0.03)
            if found is None:
                return
            number, rect = found
            state["menu"] = {"window": number, "rect": [round(v, 1) for v in rect]}
            region = union_padded([pet_rect, rect], PAD, display_rect)
            state["region"] = [round(v, 1) for v in region]
            try:
                state["record"] = shooter.shoot_composite(
                    "右键菜单",
                    "右键桌宠弹出的菜单（设备行 + 分隔线 + 打开控制面板 + 重启/退出小cc）："
                    "桌宠窗口与菜单窗口各做一次窗口级抓图，再按各自矩形合成到中性底色上",
                    region,
                    [("pet", pet_number), ("menu", number)],
                    extra={
                        "menu_rect": [round(v, 1) for v in rect],
                        "fallback_direct_call": state["fallback_direct_call"],
                    },
                )
            finally:
                # **无论抓图那一步成没成，都必须收菜单**：菜单留着不收 = 主线程永远出不来、
                # 屏上挂着别人的界面（第一版就是这么挂死的）。
                dismiss_menu(menu, quartz, pet_number, pet_rect, state)
            if state.get("still_open"):
                # 两条收菜单的路都没关掉它 ⇒ 主线程永远出不来。硬退出的代价必须只是"收尾快一点"：
                # 先把 11 张的账做完再死（进程一死，菜单一定从屏上消失，且不留残留进程）。
                say("  ✘ 菜单还挂在屏上 ⇒ 先把账做完，再硬退出（不留菜单、不留进程）")
                run_final_checks()
                os._exit(1)
        finally:
            state["done"] = True

    def pump_until_done(limit_s: float) -> None:
        deadline = time.monotonic() + limit_s
        while not state["done"] and time.monotonic() < deadline:
            backend.linger(0.2)  # ← 主线程卡在这一句里（模态菜单）；菜单一关就回来

    # 先「热」一下窗口（见上面第 4 条），再合成右键
    post_mouse(quartz, quartz.kCGEventMouseMoved, center.x, center.y, quartz.kCGMouseButtonLeft)
    backend.linger(0.5)
    state["done"] = False
    thread = threading.Thread(target=worker, args=(6.0,), daemon=True)
    thread.start()
    post_mouse(quartz, quartz.kCGEventRightMouseDown, center.x, center.y, quartz.kCGMouseButtonRight)
    post_mouse(quartz, quartz.kCGEventRightMouseUp, center.x, center.y, quartz.kCGMouseButtonRight)
    pump_until_done(20.0)
    thread.join(timeout=3.0)
    state["right_click_delivered"] = bool(delivered)
    say(f"  合成右键投递 {len(delivered)} 次 · 菜单窗口 {'找到了' if state['menu'] else '没找到'}")

    if state["menu"] is None:
        say("  合成右键没弹出菜单 ⇒ 退回直接调 _right_mouse_down（同一条代码路径）")
        state["fallback_direct_call"] = True
        state["done"] = False
        thread = threading.Thread(target=worker, args=(6.0,), daemon=True)
        thread.start()
        original_right_down(event_for(center, backend._window_local), backend._view)
        pump_until_done(20.0)
        thread.join(timeout=3.0)

    failures_here: list[str] = []
    if state["menu"] is None:
        failures_here.append("窗口服务器里一直读不到菜单窗口（合成右键与直接调用都没弹出来）")
    if state["record"] is None:
        failures_here.append("没抓到图")
    if state.get("still_open"):
        failures_here.append("菜单没关掉（worker 已硬退出；这行只会出现在收尾账里）")
    titles = [str(menu.itemAtIndex_(i).title()) for i in range(menu.numberOfItems())]
    expect_actions = ["打开控制面板", "重启小cc", "退出小cc"]
    if [t for t in titles if t in expect_actions] != expect_actions:
        failures_here.append(f"动作条目不对 ⇒ {titles}")
    if not any(t.startswith("CPU") for t in titles):
        failures_here.append(f"没有设备行 ⇒ {titles}")

    record = state["record"]
    if record is None:
        records.append({"file": "右键菜单.png", "how": HOW_COMPOSITE, "note": "没抓到",
                        "failures": failures_here})
        failures.extend(f"右键菜单.png ⇒ {item}" for item in failures_here)
        write_evidence()
        return {"failures": failures_here}
    # 收菜单是在 `_record()` 之后才发生的 ⇒ 这几项事后补齐，别当成"拍的时候就知道"
    record["dismiss"] = state["dismiss"]
    record["menu_items"] = titles
    record["menu_items_count"] = len(titles)
    record["fallback_direct_call"] = state["fallback_direct_call"]
    record["right_click_delivered"] = state.get("right_click_delivered", False)
    record["failures"] = list(record.get("failures") or []) + failures_here
    if failures_here:
        failures.extend(f"右键菜单.png ⇒ {item}" for item in failures_here)
    write_evidence()
    return record


# —— 贴边把手条 ————————————————————————————————————————————————————————————————


def shoot_handle_strip(shooter: Shooter, quartz, display_rect, backend, character) -> list[dict]:
    """拖到右边缘收成把手条 → **单击**它展开。两个状态**各出一张**。

    为什么拆两张：「收起态」和「展开态」是同一扇窗的两个画面，一张图放不下；`-单击展开` 这个
    名字又承诺了图里没有的东西（那版图里只有展开后的角色）。所以：

    * `贴边把手条-收起.png` —— 把手条本体（12px 宽，右侧顶到屏幕沿）；
    * `贴边把手条-展开后.png` —— 单击之后展开的样子（贴右边缘 + 气泡）。

    两张都是**窗口级抓图 + 中性底色合成**（不拍桌面）。贴边几何由父类
    `drag_to_right_edge()` 决定，不另写一套。
    """
    screen = shooter.space.screen
    shooter.drag_to_right_edge()
    backend.linger(0.5)
    probe = backend.probe()
    strip = window_rect(quartz, probe["window_number"])
    say(
        f"  收成把手条：dock={probe['dock']} 窗口={tuple(round(v) for v in (strip or (0, 0, 0, 0)))}"
    )
    out_records: list[dict] = []
    if probe["dock"] != "collapsed" or strip is None:
        failures.append(f"贴边把手条：没收起（dock={probe['dock']}，edge={probe.get('edge')}）")
        return out_records

    # ① 收起态：把手条本体。左留一截空底、右边顶到屏幕沿（"贴在边上"得看得见）
    region_strip = dock_region(strip, screen)
    out_records.append(
        shooter.shoot_composite(
            "贴边把手条-收起",
            "拖到右边缘后收成的 12px 把手条本体（收起态）：窗口级抓图 + 中性底色合成",
            region_strip,
            [("pet", probe["window_number"])],
            extra={
                "dock": probe["dock"],
                "edge": probe.get("edge"),
                "extra_note": f"（把手条 {tuple(round(v) for v in strip)}，右沿顶到屏幕 {screen.right:g}）",
            },
        )
    )

    # ② 展开后：单击把手条 —— 真机同一条按下/松手路径（按下那一瞬间 _expand_from_edge()）
    strip_center = wl.Point(strip[0] + strip[2] / 2.0, strip[1] + strip[3] / 2.0)
    shooter.cursor[0] = wl.Point(strip_center.x, strip_center.y)
    backend.linger(0.4)
    click_pet(shooter, strip_center, linger_after=0.3)
    expanded_probe = backend.probe()
    expanded = window_rect(quartz, expanded_probe["window_number"])
    say(
        f"  单击后：dock={expanded_probe['dock']} 窗口={tuple(round(v) for v in (expanded or (0, 0, 0, 0)))}"
        f" 气泡={expanded_probe['badge_drawn']!r}"
    )
    state_fail: list[str] = []
    if expanded_probe["dock"] == "collapsed":
        state_fail.append("单击后没展开（还是一条 12px）")
    if not expanded_probe["badge_drawn"]:
        state_fail.append("展开后没有气泡")
    region_expand = dock_region(expanded or strip, screen)
    out_records.append(
        shooter.shoot_composite(
            "贴边把手条-展开后",
            "单击把手条之后展开的样子（贴右边缘 + 气泡）：收起时那条的位置也在画面里",
            region_expand,
            [("pet", expanded_probe["window_number"])],
            extra={
                "dock": expanded_probe["dock"],
                "strip_rect": [round(v, 1) for v in strip],
                "expanded_rect": [round(v, 1) for v in (expanded or strip)],
                "failures": state_fail,
                "extra_note": f"（收起时条={tuple(round(v) for v in strip)}，展开后={tuple(round(v) for v in (expanded or strip))}）",
            },
        )
    )
    return out_records


if __name__ == "__main__":
    raise SystemExit(main())
