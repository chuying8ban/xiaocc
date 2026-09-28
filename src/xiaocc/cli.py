"""命令行入口。

    xiaocc sources                    # 有哪些状态源可用
    xiaocc backends                   # 有哪些显示层可用
    xiaocc run --source hermes        # 跑起来（默认 console）
    xiaocc run --source 'file:~/.xiaocc/status.json' --backend appkit
    xiaocc character validate ./my-character
    xiaocc where                      # 关键路径，排障先看这个

``--once`` 只跑一轮，供脚本和 CI 用。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

from . import __version__, characters, registry
from .engine import Engine

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiaocc", description="小cc —— 可扩展桌面状态伴侣")
    parser.add_argument("--version", action="version", version=f"xiaocc {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="打开调试日志（状态源报错会打出来）"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("sources", help="列出可用状态源")
    sub.add_parser("backends", help="列出可用显示层")
    sub.add_parser("where", help="打印关键路径")

    run = sub.add_parser("run", help="启动小cc")
    run.add_argument(
        "--source",
        "-s",
        action="append",
        default=[],
        metavar="名字[:参数]",
        help="可以给多个；默认 hermes",
    )
    run.add_argument("--backend", "-b", default="console", help="显示层，默认 console")
    run.add_argument("--character", "-c", default=None, help="角色包目录，默认内置")
    run.add_argument("--once", action="store_true", help="只跑一轮就退出")

    char = sub.add_parser("character", help="角色包操作")
    char_sub = char.add_subparsers(dest="char_command", required=True)
    validate = char_sub.add_parser("validate", help="校验角色包")
    validate.add_argument("path", nargs="?", default=None, help="角色包目录，默认内置角色")
    char_sub.add_parser("list", help="列出可用角色包")

    return parser


def _cmd_sources() -> int:
    print("可用状态源：")
    for name, desc in sorted(registry.available_sources().items()):
        print(f"  {name:<10} {desc}")
    print("\n用法：xiaocc run --source '名字:参数'   （多个 --source 会按优先级合并）")
    return 0


def _cmd_backends() -> int:
    print("可用显示层：")
    for name, desc in sorted(registry.available_backends().items()):
        print(f"  {name:<10} {desc}")
    return 0


def _cmd_where() -> int:
    print(f"内置角色目录：{characters.builtin_character_dir()}")
    print(f"用户角色目录：{Path.home() / '.xiaocc' / 'characters'}")
    print("用户状态文件：%s" % (Path.home() / ".xiaocc" / "status.json"))
    dbs = __import__("xiaocc.sources.hermes", fromlist=["find_state_dbs"]).find_state_dbs()
    print("检测到的 Hermes state.db：")
    for db in dbs or ["  (无)"]:
        print(f"  {db}" if db != "(无)" else db)
    return 0


def _cmd_character(path: str | None) -> int:
    character = characters.load_character(path)
    for warning in character.warnings:
        print(f"警告：{warning}", file=sys.stderr)
    width, height = character.canvas
    print(f"角色包 OK：{character.name} ({character.id}) v{character.version}")
    print(f"  作者 {character.author or '-'} · 许可 {character.license or '-'} · 画布 {width}x{height}")
    print(f"  状态覆盖 {len(character.states)}/7：")
    for state, spec in character.states.items():
        print(f"    {state.value:<9} {spec.motion:<8} {spec.accent:<8} {spec.caption}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    specs = args.source or ["hermes"]
    try:
        sources = [registry.parse_source_spec(spec) for spec in specs]
    except (KeyError, ValueError) as exc:
        print(f"状态源配置有问题：{exc}", file=sys.stderr)
        return 2

    try:
        character = characters.load_character(args.character)
    except characters.CharacterError as exc:
        print(f"角色包加载失败：{exc}", file=sys.stderr)
        return 2
    for warning in character.warnings:
        print(f"警告：{warning}", file=sys.stderr)

    try:
        backend = registry.load_backend(args.backend)
    except (KeyError, ImportError) as exc:
        print(f"显示层加载失败：{exc}", file=sys.stderr)
        hint = "（appkit 显示层需要先装：pip install 'xiaocc[macos]'）"
        print(hint, file=sys.stderr)
        return 2
    if isinstance(backend, type):
        backend = backend()

    engine = Engine(sources, character)
    stopping = False

    def _stop(signum, _frame):  # noqa: ANN001 - signal 处理器签名固定
        nonlocal stopping
        stopping = True
        print(f"\n收到信号 {signum}，准备退出…", file=sys.stderr)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _stop)
        except ValueError:  # 非主线程，忽略
            pass

    logging.info("小cc 启动：源=%s 角色=%s 节拍=%.2fs", specs, character.name, engine.interval)
    try:
        while not stopping:
            frame = engine.tick()
            if frame is not None:
                backend.render(frame)
            if args.once:
                break
            time.sleep(engine.interval)
    finally:
        backend.close()
        engine.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.command == "sources":
        return _cmd_sources()
    if args.command == "backends":
        return _cmd_backends()
    if args.command == "where":
        return _cmd_where()
    if args.command == "character":
        if args.char_command == "validate":
            return _cmd_character(args.path)
        if args.char_command == "list":
            found = characters.available_characters()
            if not found:
                print("没有找到角色包")
                return 1
            for path in found:
                try:
                    loaded = characters.load_character(path)
                    print(f"{loaded.name:<10} {loaded.id:<12} {path}")
                except characters.CharacterError as exc:
                    print(f"{'(损坏)':<10} {'-':<12} {path}  ← {exc}")
            return 0
    if args.command == "run":
        return _cmd_run(args)
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
