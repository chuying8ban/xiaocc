"""命令行入口。

    xiaocc sources                    # 有哪些状态源可用
    xiaocc backends                   # 有哪些显示层可用
    xiaocc run --source hermes        # 跑起来（默认 console）
    xiaocc run --source 'file:~/.xiaocc/status.json' --backend appkit
    xiaocc run -b appkit --backend-opt at=bottom-left   # 给显示层传选项
    xiaocc character validate ./my-character
    xiaocc where                      # 关键路径，排障先看这个
    xiaocc probe                      # 面板是不是「省电且没被节流」（退出码即判据）

``--once`` 只跑一轮，供脚本和 CI 用。
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, characters, registry
from .backends.anchor_store import anchor_path
from .engine import Engine

__all__ = ["main"]

log = logging.getLogger("xiaocc.cli")


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
    sub.add_parser("probe", help="读显示层的自证据：面板是不是「省电且没被节流」")

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
    run.add_argument(
        "--backend-opt",
        action="append",
        default=[],
        metavar="键=值",
        help=(
            "传给显示层构造函数的选项，可以重复给多个（值自动转 int/float/bool），"
            "例如 --backend-opt at=bottom-left --backend-opt fps=30；"
            "各显示层支持哪些选项见 docs/backends.md"
        ),
    )
    run.add_argument("--character", "-c", default=None, help="角色包目录，默认内置")
    run.add_argument("--once", action="store_true", help="只跑一轮就退出")
    run.add_argument(
        "--linger",
        type=float,
        default=0.0,
        metavar="秒",
        help="渲染后让显示层再留 N 秒（截图 / 肉眼验收用；配合 --once 就是「打一枪看一眼」）",
    )

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


def _probe_file() -> Path:
    """自证据文件的实际路径（与显示层同口径：``XIAOCC_PROBE_FILE`` 优先）。"""
    override = os.environ.get("XIAOCC_PROBE_FILE")
    return Path(override).expanduser() if override else Path.home() / ".xiaocc" / "probe.json"


def _cmd_probe() -> int:
    """读显示层落盘的自证据，判断门槛过没过 —— **退出码就是判据**（doctor / CI 直接断言）。

    为什么非看两个数不可：**CPU 低有两种可能** —— 真的省，或者被节流了（画面其实在卡）。
    ``ps`` 只给得出前者；圈速只有进程内知道，所以「圈速 ≈ fps」这条必须从这份文件断言：
    圈速远低于 fps = 被节流，远高于 fps = 又退回忙等，两种都不算达标。
    """
    target = _probe_file()
    try:
        info = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"没有自证据文件：{target}")
        print("（面板还没启动过？先 `xiaoccctl start`，或 `xiaocc run -b appkit` 前台跑一份）")
        return 2
    except (OSError, ValueError) as exc:
        print(f"自证据文件读不了：{target}（{exc}）")
        return 2

    fps = float(info.get("fps") or 0.0)
    loops = info.get("pump_loops_per_sec")
    age = time.time() - float(info.get("at") or 0.0)
    period = float(info.get("probe_interval_s") or 1.0)
    stale_after = max(3.0, 3.0 * period)  # 快照本身一秒一写，三拍没更新就算过期

    print(f"文件：{target}")
    print(f"pid={info.get('pid')}  存活={info.get('alive')}  快照 {age:.1f}s 前"
          f"（超过 {stale_after:.0f}s 算过期）")
    print(f"fps={fps:g}  圈速={loops}/s  上一圈睡={info.get('pump_sleep_ms')}ms"
          f"  状态={info.get('state')}")
    print(f"锚点={info.get('anchor')}  关系={info.get('anchor_state')}  在锚点={info.get('anchor_ok')}")
    print(f"文案={info.get('caption_drawn')!r}")

    reasons: list[str] = []
    if not info.get("alive"):
        reasons.append("进程已不在（这份是上一次运行留下的快照）")
    if age > stale_after:
        reasons.append(f"快照过期 {age:.1f}s > {stale_after:.0f}s（面板卡死或没在跑）")
    if loops is None:
        reasons.append("快照里没有圈速字段（显示层太老？）")
    elif fps > 0 and not (0.7 * fps <= loops <= 1.3 * fps):
        why = "被节流，画面在卡" if loops < 0.7 * fps else "又退回忙等"
        reasons.append(f"圈速 {loops:.0f}/s 偏离 fps={fps:g} 超 ±30%（{why}）")
    if info.get("anchor_ok") is False:
        reasons.append(f"窗口漂在锚点外（{info.get('anchor_state')}）")

    if reasons:
        print("门槛：未通过 —— " + "；".join(reasons))
        return 1
    print("门槛：通过（快照新鲜、圈速≈fps、窗口在锚点上）")
    return 0


def _cmd_where() -> int:
    print(f"内置角色目录：{characters.builtin_character_dir()}")
    print(f"用户角色目录：{Path.home() / '.xiaocc' / 'characters'}")
    print("用户状态文件：%s" % (Path.home() / ".xiaocc" / "status.json"))
    # 运维脚本要能问到路径，别硬编码（环境变量覆盖后要显示实际那份）
    print(f"窗口锚点文件：{anchor_path()}")
    print(f"显示层自证据：{_probe_file()}")
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


def _coerce_backend_opt(text: str) -> Any:
    """``"30"`` → ``30``，``"0.8"`` → ``0.8``，``"true"/"false"`` → bool，其余原样当字符串。"""
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return text


def _parse_backend_opts(values: list[str]) -> dict[str, Any]:
    """把重复给的 ``--backend-opt 键=值`` 收成一个 dict，直接透传给显示层构造函数。

    这是**通用**机制：显示层想收什么选项，在自己的 ``__init__`` 里加个具名关键字参数就行，
    CLI 不用跟着改。写错格式（没有 ``=``、或者键是空串）抛 :class:`ValueError`，
    消息里带上正确写法 —— 命令行上的错要用人话说，不要甩栈。
    """
    opts: dict[str, Any] = {}
    for value in values:
        key, sep, raw = value.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(
                f"显示层选项写错了：{value!r}；要写成 键=值，例如 --backend-opt at=bottom-left"
            )
        opts[key] = _coerce_backend_opt(raw.strip())
    return opts


def _unknown_backend_opts(name: str, opts: dict[str, Any]) -> list[str]:
    """显示层拒收之后回头确认：到底是哪几个键它不认（只用来报错，不用来放行）。"""
    try:
        params = inspect.signature(registry.load_backend(name)).parameters
    except (KeyError, ImportError, TypeError, ValueError):  # 问不出来就把选项全列上
        return list(opts)
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return list(opts)  # 收 **kwargs 的显示层：报错另有原因，别赖选项
    return [key for key in opts if key not in params]


def _cmd_run(args: argparse.Namespace) -> int:
    specs = args.source or ["hermes"]
    try:
        sources = [registry.parse_source_spec(spec) for spec in specs]
    except (KeyError, ValueError) as exc:
        print(f"状态源配置有问题：{exc}", file=sys.stderr)
        return 2

    try:
        opts = _parse_backend_opts(args.backend_opt)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        character = characters.load_character(args.character)
    except characters.CharacterError as exc:
        print(f"角色包加载失败：{exc}", file=sys.stderr)
        return 2
    for warning in character.warnings:
        print(f"警告：{warning}", file=sys.stderr)

    try:
        backend = registry.load_backend(args.backend, **opts)
    except (KeyError, ImportError) as exc:
        print(f"显示层加载失败：{exc}", file=sys.stderr)
        hint = "（appkit 显示层需要先装：pip install 'xiaocc[macos]'）"
        print(hint, file=sys.stderr)
        return 2
    except TypeError:
        unknown = _unknown_backend_opts(args.backend, opts)
        listed = " ".join(f"{key}={opts[key]}" for key in unknown) or "(见下)"
        print(
            f"显示层 {args.backend} 不认识选项 {listed}；该显示层支持哪些选项见 docs/backends.md",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:  # 选项的**取值**不合法（例如 at=middle），显示层在加载时就挡下来了
        print(f"显示层 {args.backend} 选项有误：{exc}", file=sys.stderr)
        return 2
    if isinstance(backend, type):
        backend = backend()

    engine = Engine(sources, character)
    # GUI 显示层要用节拍跑自己的事件循环（见 Backend 文档）
    backend.interval = engine.interval
    stopping = False

    def _stop(signum, _frame):  # signal 处理器签名固定
        nonlocal stopping
        stopping = True
        print(f"\n收到信号 {signum}，准备退出…", file=sys.stderr)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _stop)
        except ValueError:  # 非主线程，忽略
            pass

    log.info("小cc 启动：源=%s 角色=%s 节拍=%.2fs", specs, character.name, engine.interval)
    try:
        while not stopping:
            frame = engine.tick()
            if frame is not None:
                backend.render(frame)
            if args.once:
                break
            # 这一轮的时间**必须**被走掉，哪怕 tick() 没吐出新帧：引擎只在状态变化时给帧，
            # 而状态大部分时间是不变的。少一边走时间，主循环就成了没有节拍的空转 ——
            # 实测 appkit 显示层 5 秒跑了 732768 圈（其中 732767 圈 tick() 返回 None，
            # 既不 render 也不睡），CPU 99.8%、一个核吃满；同样的循环下 console 是 0%。
            # 所以两条路都得走：self_paced 的显示层（GUI）自己拥有事件循环，由 idle() 去
            # 消化节拍（再补一次 sleep 会让动画一顿一顿）；其余显示层由这里 sleep。
            if backend.self_paced:
                backend.idle()
            else:
                time.sleep(engine.interval)
    finally:
        backend.linger(args.linger)
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
    if args.command == "probe":
        return _cmd_probe()
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
