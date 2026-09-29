"""``python -m xiaocc.panel`` / ``xiaocc panel`` 的入口。

三种用法各对应一个真实场景：

* 无参 —— 用户在桌宠上点了入口 / 命令行想看面板 ⇒ 开窗口；
* ``--request`` —— 桌宠进程调它「把面板打开或抬到前面」：已有面板只写请求文件（不拉起第二个），
  没有就继续往下走、当场变成面板进程；
* ``--dump`` —— 只渲染不显窗口（管道、截图、测试、想在浏览器里打开）⇒ 打路径，退出码 0。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .paths import (
    CHILD_ENV,
    DEFAULT_PANEL_HTML,
    PAGES,
    PANEL_REQUEST,
    read_json,
    request_open,
    running_panel,
)
from .render import write_panel


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiaocc panel", description="小cc 控制面板：额度 / 本机账本 / 桌宠现状"
    )
    parser.add_argument("--theme", choices=("night", "paper"), default="night", help="皮肤")
    parser.add_argument(
        "--page", choices=PAGES, default=None, help="翻到哪一页（默认：跟着请求文件，没有就 panel）"
    )
    parser.add_argument("--request", action="store_true", help="只请求打开/抬到前面（桌宠入口用）")
    parser.add_argument(
        "--dump", type=Path, metavar="FILE", help="只渲染到这个文件，不起窗口"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_PANEL_HTML, help=f"窗口那份 HTML 落在哪（默认 {DEFAULT_PANEL_HTML}）"
    )
    parser.add_argument("--quota", type=Path, default=None, help="quota.json 路径（默认取标准位置）")
    parser.add_argument("--probe", type=Path, default=None, help="probe.json 路径（默认取标准位置）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv if argv is not None else sys.argv[1:])

    quota_kwargs = {}
    if args.quota is not None:
        quota_kwargs["quota_path"] = args.quota
    if args.probe is not None:
        quota_kwargs["probe_path"] = args.probe

    if args.page is None:
        wanted = read_json(PANEL_REQUEST).get("page")
        args.page = wanted if wanted in PAGES else "panel"

    if args.dump is not None:
        try:
            path = write_panel(args.dump, theme=args.theme, page=args.page, **quota_kwargs)
        except OSError as exc:
            print(f"渲染失败：{exc}", file=sys.stderr)
            return 1
        print(path)
        return 0

    if args.request and os.environ.get(CHILD_ENV) != "1":
        # 只有「谁都没在开」时才走到这里拉一个；被 request_open 拉起来的子进程带着
        # CHILD_ENV 标记，直接往下走开自己的窗口 —— 不然它会再拉一个孙子（实测 3 个窗口）。
        result = request_open(theme=args.theme, page=args.page)
        if result == "raised":
            pid = running_panel()
            print(f"面板已经开着（pid {pid}），已请求它刷新并抬到前面")
            return 0
        if result == "failed":
            print("既没能请求到面板、也没能拉起它", file=sys.stderr)
            return 1
        # "spawned" ⇒ 子进程已经在开窗了，自己收工
        return 0

    try:
        from .window import open_panel
    except ImportError as exc:  # pragma: no cover - 只有缺 pyobjc 才会走到
        print(f"打不开窗口（缺 AppKit/WebKit？）：{exc}", file=sys.stderr)
        print(f"想要不显窗口的那份页面：xiaocc panel --dump {args.out}", file=sys.stderr)
        return 2
    return open_panel(theme=args.theme, page=args.page, out_path=args.out, **quota_kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
