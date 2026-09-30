#!/usr/bin/env python3
"""把 `ops/templates/*.plist.in` 渲染成这台机器上的真 plist —— 渲染逻辑只有这一份。

为什么必须渲染、不能在 plist 里写 `$HOME`：launchd **不**对 `ProgramArguments` 做变量展开
（`scripts/release_check.py` 判据 #2 的注释里是同一条结论），所以仓库里只能存带占位符的模板，
安装那一刻替换成绝对路径。

谁调它（三处共用，谁都不许在 shell 里再实现一遍替换）：
  * `ops/install.sh`  —— `--dest DIR` 渲染全部模板；`--stdout` 给 `--dry-run` 打印。
  * `ops/xiaoccctl`   —— `start` 用 `--out` 渲染面板那一份；`doctor` ① 用 `--diff` 比对
                         「渲染结果 ↔ 已安装件」（模板化之后不能再拿仓库原件去比了）。

刻意**不读** `REPO_ROOT` / `PYTHON` / `LOG_DIR` 环境变量（只读 `HOME` 来算默认值）：
本脚本必须是「参数的纯函数」，否则同名环境变量在别处被设过，`install` 渲染出的东西和
`doctor` ① 渲染出的东西就会不一致，报出来的「漂移」是假的。要覆盖就走命令行参数。

占位符四个，按需使用；渲染完还留着 `@大写@` 说明模板写错了 —— 直接报错，不产出坏 plist。
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from pathlib import Path

TEMPLATE_SUFFIX = ".in"
#: 渲染完还剩这种记号 = 模板里有本脚本不认识的占位符（`@name` 这种小写记号不在内，不会误伤）。
LEFTOVER_RE = re.compile(r"@[A-Z][A-Z0-9_]*@")
#: launchd 拒绝组/全局可写的 plist，所以别把权限交给 umask 碰运气。
PLIST_MODE = 0o644
#: `--diff` 最多打多少行差异：doctor ① 只要「哪几行漂了」，不需要整份 plist。
DIFF_LINE_LIMIT = 20


def default_repo_root() -> Path:
    """本文件在 `<repo>/ops/templates/render.py` ⇒ 往上三级就是仓库根（clone 到哪都对）。"""
    return Path(__file__).resolve().parents[2]


def resolve_values(args: argparse.Namespace) -> dict[str, str]:
    """算出四个占位符的值；命令行给了就用命令行的，没给就用这台机器上的默认。"""
    home = args.home or os.environ.get("HOME") or str(Path.home())
    repo_root = args.repo_root or str(default_repo_root())
    return {
        "@REPO_ROOT@": repo_root,
        "@PYTHON@": args.python or str(Path(repo_root) / ".venv" / "bin" / "python"),
        "@LOG_DIR@": args.log_dir or str(Path(home) / "Library" / "Logs" / "xiaocc"),
        "@HOME@": home,
    }


def render(template: Path, values: dict[str, str]) -> bytes:
    """读模板 → 替换占位符 → 返回字节。除占位符外一个字节都不动（中文注释、缩进、制表符照旧）。"""
    text = template.read_bytes().decode("utf-8")
    for key, value in values.items():
        text = text.replace(key, value)
    leftover = sorted(set(LEFTOVER_RE.findall(text)))
    if leftover:
        raise SystemExit(f"render: {template} 里有不认识的占位符：{'、'.join(leftover)}")
    return text.encode("utf-8")


def write_atomic(target: Path, data: bytes) -> None:
    """先写同目录临时文件再 `os.replace`：launchd 不会读到半份 plist。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".render-tmp")
    tmp.write_bytes(data)
    tmp.chmod(PLIST_MODE)
    os.replace(tmp, target)


def report_diff(label: str, target: Path, data: bytes) -> bool:
    """`--diff` 用：一致返回 True；不一致打印差在哪并返回 False（不写任何文件）。"""
    if not target.is_file():
        print(f"✗ {label}：目标不存在（{target}）")
        return False
    current = target.read_bytes()
    if current == data:
        print(f"✓ {label}：与 {target} 逐字节一致")
        return True
    print(f"✗ {label}：与 {target} 不一致")
    lines = list(
        difflib.unified_diff(
            current.decode("utf-8", errors="replace").splitlines(),
            data.decode("utf-8", errors="replace").splitlines(),
            fromfile=str(target),
            tofile=f"{label}（渲染结果）",
            lineterm="",
        )
    )
    for line in lines[:DIFF_LINE_LIMIT]:
        print(f"    {line}")
    if len(lines) > DIFF_LINE_LIMIT:
        print(f"    …（还有 {len(lines) - DIFF_LINE_LIMIT} 行差异）")
    return False


def output_name(template: Path) -> str:
    """`ai.hermes.xiaocc.plist.in` → `ai.hermes.xiaocc.plist`。"""
    return template.name.removesuffix(TEMPLATE_SUFFIX)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="render.py",
        description="渲染 LaunchAgent plist 模板（占位符 → 本机绝对路径）。",
    )
    parser.add_argument("templates", nargs="+", type=Path, metavar="TEMPLATE.in")
    parser.add_argument("--repo-root", metavar="DIR", help="仓库根目录（默认：本脚本往上三级）")
    parser.add_argument("--python", metavar="PATH", help="解释器（默认：<repo-root>/.venv/bin/python）")
    parser.add_argument("--log-dir", metavar="DIR", help="日志目录（默认：$HOME/Library/Logs/xiaocc）")
    parser.add_argument("--home", metavar="DIR", help="主目录（默认：$HOME）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--stdout",
        action="store_true",
        help="只把渲染结果打到 stdout，不写文件（模板只能给一个）",
    )
    mode.add_argument("--dest", metavar="DIR", help="写进这个目录，文件名去掉 .in")
    mode.add_argument("--out", metavar="FILE", help="写成这个文件（模板只能给一个）")
    parser.add_argument(
        "--diff",
        action="store_true",
        help="配合 --dest/--out：只比对不写；有差异退出 1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.stdout or args.out) and len(args.templates) != 1:
        print("render: --stdout / --out 一次只能渲染一个模板", file=sys.stderr)
        return 2
    values = resolve_values(args)

    if args.stdout:
        sys.stdout.buffer.write(render(args.templates[0], values))
        return 0

    targets: list[tuple[Path, Path]] = []
    for template in args.templates:
        if args.out:
            targets.append((template, Path(args.out)))
        else:
            targets.append((template, Path(args.dest) / output_name(template)))

    ok = True
    for template, target in targets:
        if not template.is_file():
            print(f"✗ 模板不存在：{template}", file=sys.stderr)
            ok = False
            continue
        data = render(template, values)
        label = output_name(template)
        if args.diff:
            ok = report_diff(label, target, data) and ok
        elif target.is_file() and target.read_bytes() == data:
            print(f"= {label}：已一致，不动 {target}")
        else:
            write_atomic(target, data)
            print(f"+ {label} → {target}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
