"""发布前检查：文档里的声明 ↔ 仓库事实，对不对得上？

这个脚本只回答一个问题：**「文档里的声明与仓库事实是否一致」**；
它**不回答「门禁是不是绿的」**——运行时是否通过由本机日志回答，日志不进仓库，
否则把某一次运行结果写进版本库，又是一次「承诺超出事实」。

三条判据，全部只读仓库、不跑被测程序：

① 措辞：仓库根 `docs/` 下递归所有 `.md` 与仓库根 `ASSET_LICENSE.md` 里，
   来源声明不许出现 BAD_WORDS（自绘 / 纯手绘 / 手工绘制）。
② 声明 ↔ 留档：`ASSET_LICENSE.md` 与 `docs/design/*.md` 的「声明行」上点到的路径，
   必须真的在仓库里（窄口径，不做全仓路径普查，边界见该函数注释）。
③ 护栏：文档里逐字点名的 13 支门禁脚本，必须还在 `scripts/` 下（防未来改名 / 删除）。

用法：

    python3 scripts/release_check.py              # root = 本脚本所在目录的上一级
    python3 scripts/release_check.py --root DIR   # 指定仓库根
    python3 scripts/release_check.py --help

退出码：0 = 全部 OK；1 = 至少一条 FAIL；2 = 用法错误（--root 不存在 / 未知参数）。
输出不用 ANSI 颜色，逐条 `OK|FAIL <相对路径>:<行号>  <说明>`，FAIL 的下一行抄原句。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# 判据①：来源声明里不许出现的措辞。这三个词都把「AI 辅助生成 + 人工校对」
# 说成了「人一笔笔画出来的」，属于承诺超出事实，所以按子串命中即 FAIL。
BAD_WORDS = ("自绘", "纯手绘", "手工绘制")

#: 行里同时出现这些**说规则**的用语 ⇒ 它是在讲"不许这么写"，不是在声称自己是这么来的
#: （实拍：`docs/RELEASE-CHECKLIST.md` 写"① 来源声明禁用措辞（自绘/纯手绘/手工绘制）"，
#:  朴素写法会把它自己判红 —— 守卫把描述守卫的文档判红，是最容易被关掉的那种假红）。
#: 只做**同一条行内**豁免，且要打印出来（不许静默放过）。
RULE_WORDS = ("禁用", "不许", "禁止", "别用", "不要用", "放回")

# 判据②：哪些算「声明行」。行里出现这些用语之一，就把该行的反引号 token 当作
# 被点名的路径；另外任何行只要有 Markdown 链接目标 ](target)，target 也算点名。
DECLARE_WORDS = (
    "留档于",
    "留档在",
    "存档于",
    "存放于",
    "记录在",
    "另见",
    "参见",
    "详见",
    "见",
    "取自",
)

# 判据③：文档里逐字点名的门禁脚本（scripts/<name>.py）。
# 这是一条**防未来改名 / 删除**的护栏：今天 13 支全在，正常情况下不该报出任何东西。
# 不要为了「跑出结果」去放宽或改造它——尤其不要改成「扫描 scripts/ 下所有 verify_*.py」，
# 那样一来脚本改名之后这里照样绿，护栏就废了。名单变了只应该是文档真的改了名字的时候。
GATE_SCRIPTS = (
    "verify_anchor",
    "verify_bubble_pending",
    "verify_ctl_stop",
    "verify_dock_cycle",
    "verify_done_window",
    "verify_drag_inject",
    "verify_drag_mouse",
    "verify_drag_tracking",
    "verify_fault_survival",
    "verify_panel_click",
    "verify_panel_quit",
    "verify_process_type",
    "verify_window_position",
)

BACKTICK_RE = re.compile(r"`([^`]+)`")
LINK_TARGET_RE = re.compile(r"\]\(([^)]*)\)")
# 本仓路径候选的字面形状：字母 / 数字 / 下划线 / 点 / 斜杠 / 连字符。
# re.UNICODE 让 \w 收下中文，所以 `docs/design/生成记录.md` 这种路径能过。
PATH_RE = re.compile(r"^[\w./-]+$", re.UNICODE)
PATH_SUFFIXES = (".md", ".svg", ".py")

DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Report:
    """收集并打印逐条结论。格式固定，方便直接贴进发布记录里看。"""

    def __init__(self) -> None:
        self.ok_count = 0
        self.fail_count = 0

    def section(self, title: str) -> None:
        print()
        print(f"== {title} ==")

    def ok(self, path: str, lineno: int, message: str) -> None:
        self.ok_count += 1
        print(f"OK   {path}:{lineno}  {message}")

    def fail(self, path: str, lineno: int, message: str, source: str | None = None) -> None:
        self.fail_count += 1
        print(f"FAIL {path}:{lineno}  {message}")
        # 有原句就抄原句，让 FAIL 自带证据；结构性缺失（文件 / 目录 / 脚本不存在）
        # 没有原文可抄，就不硬编一行假的出来。
        if source is not None:
            print(f"     | {source.strip()}")

    def summary(self) -> int:
        print()
        print("== 小结 ==")
        print(f"OK {self.ok_count} 条，FAIL {self.fail_count} 条")
        if self.fail_count:
            print("总判：不一致 —— 每条 FAIL 都是「文档这么说、仓库不是这样」")
            return 1
        print("总判：一致 —— 文档里的声明与仓库事实对得上")
        print("      （门禁是否跑绿不由本脚本回答：那要看本机日志，日志不进仓库）")
        return 0


class RepoIndex:
    """全仓文件清单，懒加载，只给判据②的第 3 种锚点用（前两种命中就不会走这儿）。"""

    def __init__(self, root: str) -> None:
        self.root = root
        self._paths: list[str] | None = None

    def paths(self) -> list[str]:
        if self._paths is None:
            self._paths = walk_repo_files(self.root)
        return self._paths


def rel(root: str, path: str) -> str:
    """相对仓库根、统一用 / 分隔，输出里就是这个形式。"""
    return os.path.relpath(path, root).replace(os.sep, "/")


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def walk_repo_files(root: str) -> list[str]:
    """列出仓库内所有文件的相对路径（跳过 .git），排序后返回，保证结论可复现。"""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for name in filenames:
            found.append(rel(root, os.path.join(dirpath, name)))
    return sorted(found)


def markdown_files_under(root: str, directory: str) -> list[str]:
    """递归收 directory 下所有 .md（判据①的范围就是这么定的），排序后返回。"""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, directory)):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            if name.endswith(".md"):
                found.append(os.path.join(dirpath, name))
    return found


def is_repo_path_candidate(token: str) -> bool:
    """这个 token 像不像「本仓的一个路径」。

    同时满足才算：strip 后非空；不含 `://`（外链不是仓库路径）；不含空白
    （`codex exec` 这种是命令，不是路径）；字面形状匹配 PATH_RE；
    含 `/`，或者转小写后以 .md / .svg / .py 结尾（否则 `idle`、`shell`、
    `dsh-pet` 这类普通词会被当成路径，全是误报）。
    """
    stripped = token.strip()
    if not stripped:
        return False
    if "://" in stripped:
        return False
    if re.search(r"\s", stripped):
        return False
    if not PATH_RE.match(stripped):
        return False
    if "/" in stripped:
        return True
    return stripped.lower().endswith(PATH_SUFFIXES)


def declared_tokens(line: str) -> list[str]:
    """从一行文档里取出「被点名的路径」候选，按出现顺序去重。"""
    tokens: list[str] = []
    if any(word in line for word in DECLARE_WORDS):
        tokens.extend(BACKTICK_RE.findall(line))
    # 链接目标不要求同句有声明用语：写成 [x](path) 本身就是在点名 path。
    tokens.extend(LINK_TARGET_RE.findall(line))
    kept: list[str] = []
    for token in tokens:
        stripped = token.strip()
        if stripped in kept or not is_repo_path_candidate(stripped):
            continue
        kept.append(stripped)
    return kept


def token_exists(root: str, md_path: str, token: str, index: RepoIndex) -> bool:
    """按三种锚点依次找这个 token，任一命中就算存在。"""
    if os.path.exists(os.path.join(root, token)):
        return True
    # 文档里常写同目录相对路径，例如 `docs/design/丸丸-资产说明.md` 里的「生成记录.md」。
    if os.path.exists(os.path.join(os.path.dirname(md_path), token)):
        return True
    suffix = "/" + token
    return any(path.endswith(suffix) for path in index.paths())


def check_wording(root: str, report: Report) -> None:
    """判据①：来源声明里的措辞。"""
    report.section("判据① 措辞：来源声明里不许出现「自绘 / 纯手绘 / 手工绘制」")

    targets: list[str] = []
    license_md = os.path.join(root, "ASSET_LICENSE.md")
    if os.path.isfile(license_md):
        targets.append(license_md)
    else:
        report.fail("ASSET_LICENSE.md", 0, "来源声明文件不存在")

    if os.path.isdir(os.path.join(root, "docs")):
        targets.extend(markdown_files_under(root, "docs"))
    else:
        report.fail("docs", 0, "文档目录不存在")

    for path in targets:
        lines = read_lines(path)
        hits: list[tuple[int, str, tuple[str, ...]]] = []
        for lineno, text in enumerate(lines, start=1):
            found = tuple(word for word in BAD_WORDS if word in text)
            if not found:
                continue
            if any(word in text for word in RULE_WORDS):
                # 讲规则的行：不是声明，豁免，但要留痕（看得见，才能判断豁免是否被滥用）
                report.ok(rel(root, path), lineno, "说规则的行，豁免：" + "、".join(found))
                continue
            hits.append((lineno, text, found))
        if not hits:
            report.ok(rel(root, path), 0, f"{len(lines)} 行，无禁用措辞")
            continue
        for lineno, text, found in hits:
            report.fail(
                rel(root, path),
                lineno,
                "来源声明出现禁用措辞：" + "、".join(found),
                text,
            )


def check_declared_paths(root: str, report: Report) -> None:
    """判据②：「声明 ↔ 留档」那条链。

    边界（很重要，别扩）：这里**只查「声明里说留档在某处」的路径是否真的存在**，
    不做全仓路径普查。范围文件只有 `ASSET_LICENSE.md` 与 `docs/design/*.md`；
    范围行只有「声明行」（含声明用语，或带 Markdown 链接目标）。
    因此下列东西天然不在范围内，也不该为了「多查一点」把它们捞进来：
      - 运行时状态路径，如 `~/.xiaocc/*.json`（`~` 与 `*` 过不了 PATH_RE，且根本不在仓库里）；
      - glob，如 `ops/baseline/*.bak`（`*` 过不了 PATH_RE）；
      - 树里别处存在的裸文件名，如 `LICENSE`、`character.json`、`dsh-pet`
        （不含 `/` 且不以 .md/.svg/.py 结尾，被候选规则挡掉）。
    特别地：**不要扫 `docs/PRIOR-ART.md`**——那里引用的是别的参考项目的文件，
    本仓当然没有，扫它只会产出一堆误报。
    """
    report.section("判据② 声明 ↔ 留档：文档点到的路径必须真的在仓库里")

    scope: list[str] = []
    license_md = os.path.join(root, "ASSET_LICENSE.md")
    if os.path.isfile(license_md):
        scope.append(license_md)
    design_dir = os.path.join(root, "docs", "design")
    if os.path.isdir(design_dir):
        names = sorted(name for name in os.listdir(design_dir) if name.endswith(".md"))
        scope.extend(os.path.join(design_dir, name) for name in names)

    index = RepoIndex(root)
    for path in scope:
        candidates: list[tuple[int, str, str]] = []
        for lineno, text in enumerate(read_lines(path), start=1):
            for token in declared_tokens(text):
                candidates.append((lineno, token, text))
        if not candidates:
            report.ok(rel(root, path), 0, "无声明路径")
            continue
        for lineno, token, text in candidates:
            if token_exists(root, path, token, index):
                report.ok(rel(root, path), lineno, f"{token} 存在")
            else:
                report.fail(
                    rel(root, path),
                    lineno,
                    f"{token} 不存在（声明说留档在这儿）",
                    text,
                )


# 判据④：`docs/` 下的文件必须「从入口可达」。
#
# 为什么必须是这个口径（两个反例都是实测撞出来的）：
#   * 「被任意文件引用一次」——`docs/quota.md` 当时只有两条**源码注释**提到它，判据绿，可读者一步也走不到；
#   * 「被某份 md 引用一次」——A↔B 两个互相引用的文件会一起判绿，整体其实是孤岛。
# 所以只认「从 README.md / README_EN.md 出发、顺着 md 之间的相对链接可达」：
#   * md 文件：要能顺着链接走到；
#   * 非 md 文件（图 / 原型 html）：要在可达 md 的正文里被点名（写文件名即可）。
# 范围刻意只到 `docs/`：`src/**/assets/**` 那些矢量是**随代码分发**的资产（由 character.json 引用），
# 本就不该进文档，扫它们只会得到几十条噪音然后把这条判据关掉。
ENTRY_DOCS = ("README.md", "README_EN.md")
LINK_RE = re.compile(r"\]\(([^)]+)\)")
SKIP_LINK_PREFIXES = ("http://", "https://", "#", "mailto:")


def md_links(text: str) -> list[str]:
    """正文里 `](target)` 形式的链接目标，去掉 #锚点 与外部 URL。"""
    found: list[str] = []
    for raw in LINK_RE.findall(text):
        target = raw.split("#", 1)[0].strip().strip("<>")
        if not target or target.startswith(SKIP_LINK_PREFIXES):
            continue
        found.append(target)
    return found


def reachable_markdown(root: str, report: Report) -> set[str]:
    """从入口 md 出发顺着相对链接走一圈，返回可达 md 的相对路径集合。"""
    queue: list[str] = []
    for entry in ENTRY_DOCS:
        if os.path.isfile(os.path.join(root, entry)):
            queue.append(entry)
        else:
            report.fail(entry, 0, "入口文档不存在（判据④没法从这儿出发）")
    reached: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in reached:
            continue
        path = os.path.join(root, current)
        if not os.path.isfile(path):
            continue
        reached.add(current)
        for target in md_links(read_text(path)):
            if not target.endswith(".md"):
                continue
            resolved = rel(root, os.path.normpath(os.path.join(os.path.dirname(path), target)))
            if resolved.startswith(".."):
                continue
            if os.path.isfile(os.path.join(root, resolved)):
                queue.append(resolved)
    return reached


def published_files(root: str) -> list[str]:
    """会被发布出去的文件清单 = git 跟踪的文件。

    为什么不用文件系统遍历：`docs/` 下有 `.DS_Store`、`docs/evidence/*.desktop.png` 这些
    **`.gitignore` 里排除、永远不会发布**的东西（连桌面一起截的图，曾把桌面内容带进仓库）。
    判据只该管"会被发布的那批"，用 `git ls-files` 一次拿到最准；git 不在就退回遍历文件系统。
    """
    try:
        done = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return walk_repo_files(root)
    return sorted(path for path in done.stdout.split("\0") if path)


def check_docs_reachable(root: str, report: Report) -> None:
    """判据④：docs/ 下每个文件都得有人能走到。"""
    report.section("判据④ docs/ 下的文件必须从入口可达（README.md / README_EN.md）")
    reached = reachable_markdown(root, report)
    if not reached:
        report.fail("README.md", 0, "入口文档一份都没读到，判据④无法判定")
        return
    mentions = "\n".join(read_text(os.path.join(root, name)) for name in sorted(reached))
    for path in published_files(root):
        if not path.startswith("docs/"):
            continue
        if path in reached:
            report.ok(path, 0, "可从入口顺着链接走到")
        elif os.path.basename(path) in mentions or path in mentions:
            report.ok(path, 0, "在可达文档里被点名")
        else:
            report.fail(path, 0, "没人引用：读者从 README 走不到它（补进 docs/design/README.md 的清单，或在文档里点名）")


def check_gate_scripts(root: str, report: Report) -> None:
    """判据③：文档逐字点名的门禁脚本还在不在 `scripts/`。"""
    report.section("判据③ 护栏：文档点名的 13 支门禁脚本必须还在 scripts/")
    for name in GATE_SCRIPTS:
        script = f"scripts/{name}.py"
        if os.path.isfile(os.path.join(root, script)):
            report.ok(script, 0, "门禁脚本在位")
        else:
            report.fail(script, 0, "文档点名的门禁脚本不存在（改名或被删？）")


def existing_dir(value: str) -> str:
    if not os.path.isdir(value):
        raise argparse.ArgumentTypeError(f"目录不存在：{value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_check.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        type=existing_dir,
        default=None,
        help="仓库根目录（默认：本脚本所在目录的上一级）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = os.path.abspath(args.root) if args.root else DEFAULT_ROOT
    if not os.path.isdir(root):
        print(f"release_check: 仓库根不存在：{root}", file=sys.stderr)
        return 2

    print(f"发布前检查 · root={root}")
    print("只核对「文档里的声明 ↔ 仓库事实」；门禁是否跑绿不在这里回答。")

    report = Report()
    check_wording(root, report)
    check_declared_paths(root, report)
    check_gate_scripts(root, report)
    check_docs_reachable(root, report)
    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
