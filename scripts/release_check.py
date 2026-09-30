"""发布前检查：文档里的声明 ↔ 仓库事实，对不对得上？

这个脚本只回答一个问题：**「文档里的声明与仓库事实是否一致」**；
它**不回答「门禁是不是绿的」**——运行时是否通过由本机日志回答，日志不进仓库，
否则把某一次运行结果写进版本库，又是一次「承诺超出事实」。

前四条判据查「文档声明 ↔ 仓库事实」，后面十一条是本文件模块 docstring 里那张规格表
#1–#11 的逐条落地（规格与实现同文件，避免指向仓库外的文档）。全部只读仓库、不跑被测程序：

① 措辞：仓库根 `docs/` 下递归所有 `.md` 与仓库根 `ASSET_LICENSE.md` 里，
   来源声明不许出现 BAD_WORDS（自绘 / 纯手绘 / 手工绘制）。
② 声明 ↔ 留档：`ASSET_LICENSE.md` 与 `docs/design/*.md` 的「声明行」上点到的路径，
   必须真的在仓库里（窄口径，不做全仓路径普查，边界见该函数注释）。
③ 护栏：文档里逐字点名的 13 支门禁脚本，必须还在 `scripts/` 下（防未来改名 / 删除）。
④ 可达性：`docs/` 下每个文件都得能从 `README.md` / `README_EN.md` 顺着链接走到。

规格表 #1–#11（红线 #1/#2/#3/#5/#9/#10/#11 判 🔴，其余 🟡 只提醒、不影响退出码）：

#1 历史身份：提交历史里不许出现本机用户名 / 主机名兜底邮箱（唯一「发出去就难改」的一项）。
#2 绝对路径 / 用户名：已发布文件里不许出现 `/Users/<名>`、`/home/<名>`。
#3 密钥：不许出现 sk- 密钥、Bearer 令牌、私钥文件头、凭据赋值。
#4 未跟踪的敏感文件（🟡）：未跟踪又没被 ignore 的 `.desktop.png` / `.db` / `.log` / `anchor.json`。
#5 空白 PNG（🔴）：自己解析 IHDR/IDAT + `zlib`，解压后全 0 ⇒ 一个非透明像素都没有。
#6 图片尺寸≈屏幕（🟡）／ #7 体积 >300KB（🟡）／ #8 许可文件在位（🟡）。
#9 重写前置条件（🔴/🟡）：有 remote 且 `origin/HEAD` 存在时，看**本地 HEAD 还是不是它的后代** ——
#   不是（历史已与远端分叉）⇒ 🔴 推上去必须 force-push；是（快进推送）⇒ 🟡，只留一句说明。
#   判的是**事实**：只按「origin/HEAD 在」判红，任何 fetch 过的正常仓库都会永红（恒红的门禁=没有门禁）。
#10 去人称（🔴）：已发布文件里不许出现内部工作流代号（lead / coder / ops / researcher / writer / 裸 user）。
#11 点名提交可解析（🔴 .md / 🟡 scripts/*.py）：已发布文件里逐字点名的提交必须能在本仓解析；
#   退役表「取回用的提交」那一格还必须在那个提交里真的取得到那张图，且是**最后动过它**的提交。

范围一律是**会被发布的文件**（`git ls-files`）：`docs/evidence/*.desktop.png` 在
`.gitignore` 里、永远不发布，把它报出来就是假红（文件系统遍历会连 `.DS_Store` 一起捞进来）。

零第三方依赖：纯标准库 + `git` 子进程 —— CI 没有 GUI、没有 pyobjc、也没有 PIL
（所以 PNG 自己解析，不碰 `scripts/pixel_stats.py`，它 import AppKit）。

用法：

    python3 scripts/release_check.py              # root = 本脚本所在目录的上一级
    python3 scripts/release_check.py --root DIR   # 指定仓库根
    python3 scripts/release_check.py --json       # 机读输出（给 CI 用）
    python3 scripts/release_check.py --help

退出码：0 = 全部 OK（🟡 WARN 不影响）；1 = 至少一条 🔴 FAIL；2 = 用法错误
（--root 不存在 / 未知参数）。输出不用 ANSI 颜色，逐条
`OK|WARN|FAIL <相对路径>:<行号>  <说明>`，FAIL 的下一行抄原句当证据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import zlib

# 判据①：来源声明里不许出现的措辞。这三个词都把「AI 辅助生成 + 人工校对」
# 说成了「人一笔笔画出来的」，属于承诺超出事实，所以按子串命中即 FAIL。
BAD_WORDS = ("自绘", "纯手绘", "手工绘制")

#: 行里同时出现这些**说规则**的用语 ⇒ 它是在讲"不许这么写"，不是在声称自己是这么来的
#: （实拍：写明「① 来源声明禁用措辞（自绘/纯手绘/手工绘制）」的那份发布清单，
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
    """收集并打印逐条结论。格式固定，方便直接贴进发布记录里看。

    三个级别就是清单里的记号：OK = ✅；WARN = 🟡（建议清，**不影响退出码**）；
    FAIL = 🔴（发布硬伤，退出码 1）。`quiet` 给 `--json` 用：只攒记录不打印。
    """

    def __init__(self, quiet: bool = False) -> None:
        self.ok_count = 0
        self.warn_count = 0
        self.fail_count = 0
        self.quiet = quiet
        self.records: list[dict[str, object]] = []

    def _remember(self, level: str, path: str, lineno: int, message: str,
                 source: str | None = None) -> None:
        record: dict[str, object] = {
            "level": level,
            "path": path,
            "line": lineno,
            "message": message,
        }
        if source is not None:
            record["source"] = source.strip()
        self.records.append(record)

    def section(self, title: str) -> None:
        if self.quiet:
            return
        print()
        print(f"== {title} ==")

    def ok(self, path: str, lineno: int, message: str) -> None:
        self.ok_count += 1
        self._remember("ok", path, lineno, message)
        if not self.quiet:
            print(f"OK   {path}:{lineno}  {message}")

    def warn(self, path: str, lineno: int, message: str, source: str | None = None) -> None:
        """🟡 建议清：列出来给人看，但**不**影响退出码。"""
        self.warn_count += 1
        self._remember("warn", path, lineno, message, source)
        if self.quiet:
            return
        print(f"WARN {path}:{lineno}  {message}")
        if source is not None:
            print(f"     | {source.strip()}")

    def fail(self, path: str, lineno: int, message: str, source: str | None = None) -> None:
        self.fail_count += 1
        self._remember("fail", path, lineno, message, source)
        if self.quiet:
            return
        print(f"FAIL {path}:{lineno}  {message}")
        # 有原句就抄原句，让 FAIL 自带证据；结构性缺失（文件 / 目录 / 脚本不存在）
        # 没有原文可抄，就不硬编一行假的出来。
        if source is not None:
            print(f"     | {source.strip()}")

    def summary(self) -> int:
        if not self.quiet:
            print()
            print("== 小结 ==")
            print(f"OK {self.ok_count} 条，WARN {self.warn_count} 条，FAIL {self.fail_count} 条")
            if self.fail_count:
                print("总判：有发布硬伤 —— 每条 FAIL 都要清掉，或明确写下「为什么可以豁免」")
            elif self.warn_count:
                print("总判：无硬伤；WARN 是建议清的项（不影响退出码）")
            else:
                print("总判：一致 —— 文档里的声明与仓库事实对得上")
                print("      （门禁是否跑绿不由本脚本回答：那要看本机日志，日志不进仓库）")
        return 1 if self.fail_count else 0

    def as_dict(self, root: str, rc: int) -> dict[str, object]:
        """`--json` 的形状：CI 只读 rc，人要排障时看 records。"""
        return {
            "root": root,
            "rc": rc,
            "ok": self.ok_count,
            "warn": self.warn_count,
            "fail": self.fail_count,
            "records": self.records,
        }


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


# ————————————————————————————————————————————————————————————————————————————
# 规格表 #1–#11（见本文件模块 docstring 末尾那张表）。
#
# 三条铁律（清单里写死的，别改）：
#   * 零第三方依赖 —— 纯标准库 + `git` 子进程。CI 没有 GUI、没有 pyobjc、没有 PIL，
#     所以 PNG 自己解析 IHDR / IDAT 再 `zlib.decompress`，绝不去 import 图片库。
#   * 范围一律取「会被发布的文件」= `git ls-files`。`docs/evidence/*.desktop.png` 在
#     `.gitignore` 里、永远不发布；用文件系统遍历会把它和 `.DS_Store` 一起报出来（假红）。
#   * 每条都打印「文件:行:片段」，FAIL 下一行抄原句当证据；豁免也要打印（不许静默放过）。
# ————————————————————————————————————————————————————————————————————————————

GIT_TIMEOUT = 60


def git_capture(root: str, *args: str) -> tuple[int, str]:
    """跑一条只读 git 命令，返回 (rc, stdout)。rc=127 表示 git 不在（CI 上不该发生）。"""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return done.returncode, done.stdout


def published_lines(root: str, relpath: str):
    """按行产出 (行号, 文本)；含 NUL 的二进制文件（PNG 等）直接跳过。

    不跳的话会在图片的随机字节上做正则 —— 那种命中既解释不清也没法修。
    """
    try:
        with open(os.path.join(root, relpath), "rb") as handle:
            data = handle.read()
    except OSError:
        return
    if b"\0" in data[:8192]:
        return
    yield from enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1)


# —— #1 历史身份 ——————————————————————————————————————————————————————————————
#: git 在 `user.email` 为空时用 `<用户名>@<主机名>.local` 兜底（例如 `dev@buildbox.local`）。
HOST_EMAIL_RE = re.compile(r"@[A-Za-z0-9._-]+\.local", re.IGNORECASE)
#: 5 位以上纯数字的邮箱名：工号 / 学号 / 手机号 + 兜底域名，同样把本机身份带出去。
DIGITS_AT_RE = re.compile(r"\d{5,}@")
HISTORY_PATH = "<git 历史>"


def check_history_identity(root: str, report: Report) -> None:
    """#1（🔴）提交历史里的身份 —— 唯一「发出去就难改」的一项。

    清单原文写的是 `git log --format=%ae%ce%an%cn`（四个值连成一行）；这里换成
    `%n` 分隔，信息一样，但每个身份独立成行 —— 去重不用猜边界，证据能抄成干净一行。
    """
    report.section("判据 #1 历史身份：提交里不许出现本机用户名 / 主机名")
    rc, out = git_capture(root, "log", "--format=%ae%n%ce%n%an%n%cn")
    if rc != 0:
        report.warn(HISTORY_PATH, 0, "读不到提交历史（还没有提交？），身份无法核对")
        return
    count_rc, count_out = git_capture(root, "rev-list", "--count", "HEAD")
    commits = count_out.strip() if count_rc == 0 else "?"
    identities = sorted({line.strip() for line in out.splitlines() if line.strip()})
    hits = 0
    for identity in identities:
        reasons: list[str] = []
        if "/Users/" in identity or "/home/" in identity:
            reasons.append("本机主目录路径")
        if HOST_EMAIL_RE.search(identity):
            reasons.append("本机主机名兜底邮箱 @<主机名>.local")
        if DIGITS_AT_RE.search(identity):
            reasons.append("5 位以上纯数字的邮箱名")
        if reasons:
            hits += 1
            report.fail(HISTORY_PATH, 0, "身份泄漏：" + "、".join(reasons), identity)
    if not hits:
        report.ok(HISTORY_PATH, 0, f"{commits} 个提交、{len(identities)} 个去重身份，无本机身份")


# —— #2 绝对路径 / 用户名 ————————————————————————————————————————————————————————
#: 清单 #2 的原式：`/(Users|home)/<名字>`。
ABS_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
#: 清单点名的白名单：`sources/hermes.py` 的文档串里写了 `/Users/xxx`、`/home/xxx`
#: 当例子 —— 那是解释规则的文字，不是泄漏。
ABS_PATH_ALLOWLIST = ("src/xiaocc/sources/hermes.py",)
#: 占位用户名：测试里显式传假主目录（`/Users/alice`）是有意为之（否则 CI 会去碰真 ~/.hermes）。
#: 报出来就是「守卫把守卫自己的测试判红」那种假红；豁免一律打印，看得见。
PLACEHOLDER_USERS = frozenset(
    {
        "alice",
        "bob",
        "someone",
        "placeholder",
        "example",
        "user",
        "username",
        "name",
        "you",
        "me",
        "xxx",
    }
)


def check_abs_paths(root: str, report: Report) -> None:
    """#2（🔴）已发布文件里的硬编码绝对路径 / 本机用户名。

    plist 特别注意：launchd **不**对 `ProgramArguments` 做变量展开，所以这里不能靠
    `$HOME` 相对；正确做法是模板 + `install` 时渲染（见清单第 3 条的「怎么修」）。
    """
    report.section("判据 #2 绝对路径 / 用户名：已发布文件里不许出现 /Users/<名>、/home/<名>")
    hits = 0
    exempt = 0
    scanned = 0
    for relpath in published_files(root):
        if relpath in ABS_PATH_ALLOWLIST:
            report.ok(relpath, 0, "文档串白名单（清单 #2 点名的豁免）")
            continue
        scanned += 1
        for lineno, text in published_lines(root, relpath):
            found = ABS_PATH_RE.findall(text)
            if not found:
                continue
            real = [p for p in found if p.rsplit("/", 1)[-1].lower() not in PLACEHOLDER_USERS]
            if real:
                hits += 1
                report.fail(
                    relpath, lineno, "硬编码绝对路径：" + "、".join(sorted(set(real))), text
                )
            if len(real) != len(found):
                exempt += 1
                kept = sorted({p for p in found if p not in real})
                report.ok(relpath, lineno, "占位用户名，豁免：" + "、".join(kept))
    if not hits:
        report.ok(
            "<已发布文件>", 0, f"{scanned} 个文件，无绝对路径 / 用户名（{exempt} 行豁免）"
        )


# —— #3 密钥 ————————————————————————————————————————————————————————————————————
#: 清单 #3 的四个式子，逐字来自规格表。
SECRET_PATTERNS = (
    ("sk- 密钥", re.compile(r"sk-[A-Za-z0-9]{12,}")),
    ("Bearer 令牌", re.compile(r"Bearer\s+\S{12,}")),
    ("私钥文件头", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("凭据赋值", re.compile(r"(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]")),
)
#: 命中行里有这些记号 ⇒ 它写的是**模式本身 / 占位符 / 明确脱敏的值**，不是真凭据。
#: 实测必需的豁免：发布清单里抄了四条模式（里面有 `Bearer <token>`）、
#: `tests/test_quota.py` 用 `secret = "«redacted:sk-…»"` 验证「凭据不会进报告」——
#: 两条都是守卫在描述守卫，判红就是假红。豁免一律打印。
SECRET_EXEMPT_MARKERS = RULE_WORDS + (
    "«redacted",
    "REDACTED",
    "redacted",
    "<token",
    "<your",
    "<secret",
    "<api",
    "YOUR_",
    "***",
    "…",
    "xxx",
    "占位",
    "脱敏",
)


def check_secrets(root: str, report: Report) -> None:
    """#3（🔴）密钥 / 令牌 / 凭据赋值。"""
    report.section("判据 #3 密钥：sk- 密钥 / Bearer 令牌 / 私钥头 / 凭据赋值")
    hits = 0
    exempt = 0
    scanned = 0
    for relpath in published_files(root):
        scanned += 1
        for lineno, text in published_lines(root, relpath):
            for label, pattern in SECRET_PATTERNS:
                match = pattern.search(text)
                if match is None:
                    continue
                if any(marker in text for marker in SECRET_EXEMPT_MARKERS):
                    exempt += 1
                    report.ok(relpath, lineno, f"讲规则 / 占位值的行，豁免：{label}")
                    continue
                hits += 1
                report.fail(
                    relpath, lineno, f"疑似凭据（{label}）：{match.group(0)[:60]}", text
                )
    if not hits:
        report.ok(
            "<已发布文件>",
            0,
            f"{scanned} 个文件，无 sk- / Bearer / 私钥头 / 凭据赋值（{exempt} 行豁免）",
        )


# —— #4 未跟踪的敏感文件 ————————————————————————————————————————————————————————
#: 清单 #4 的口径：未跟踪**且没被 ignore** ⇒ `.gitignore` 有洞，`git add -A` 就会入库。
UNTRACKED_SENSITIVE_SUFFIXES = (".desktop.png", ".db", ".log")
UNTRACKED_SENSITIVE_NAMES = ("anchor.json",)


def check_untracked_sensitive(root: str, report: Report) -> None:
    """#4（🟡）未跟踪的敏感文件。"""
    report.section("判据 #4 未跟踪的敏感文件（🟡）：出现在这里说明 .gitignore 有洞")
    rc, out = git_capture(root, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        report.warn("<git ls-files --others>", 0, "git 不可用，列不出未跟踪文件")
        return
    suspects: list[str] = []
    for path in (line for line in out.splitlines() if line.strip()):
        base = os.path.basename(path)
        if base in UNTRACKED_SENSITIVE_NAMES or path.endswith(UNTRACKED_SENSITIVE_SUFFIXES):
            suspects.append(path)
    for path in suspects:
        report.warn(path, 0, "未跟踪的敏感产物：一次 git add -A 就会跟着发布出去")
    if not suspects:
        report.ok(
            "<未跟踪文件>", 0, "没有未跟踪的 .desktop.png / .db / .log / anchor.json"
        )


# —— #5 空白 PNG ————————————————————————————————————————————————————————————————
#: 零依赖解析 PNG 需要的就这几样：签名 + 块结构 + zlib。
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_chunks(path: str):
    """逐个产出 PNG 块 (类型, 数据)；不是 PNG 或文件截断就抛 ValueError。"""
    with open(path, "rb") as handle:
        data = handle.read()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("PNG 签名不符")
    pos = len(PNG_SIGNATURE)
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        end = pos + 8 + length
        if end + 4 > len(data):
            raise ValueError(f"{ctype.decode('latin-1')} 块越界（文件截断？）")
        yield ctype, data[pos + 8 : end]
        pos = end + 4
        if ctype == b"IEND":
            return


def png_size(path: str) -> tuple[int, int]:
    """只读到 IHDR：返回 (宽, 高)。"""
    for ctype, payload in png_chunks(path):
        if ctype == b"IHDR":
            return int.from_bytes(payload[0:4], "big"), int.from_bytes(payload[4:8], "big")
    raise ValueError("没有 IHDR 块")


def png_idat_raw(path: str) -> bytes:
    """把 IDAT 拼起来解压，返回原始扫描线数据（含每行 filter 字节）。"""
    idat = b"".join(payload for ctype, payload in png_chunks(path) if ctype == b"IDAT")
    return zlib.decompress(idat)


def png_pixels_are_blank(path: str) -> bool:
    """#5 的原理（清单原文）：全像素为 0 ⇒ IDAT 解出的扫描线（含 filter 字节）必然全 0。

    所以「`zlib.decompress(IDAT)` 后 `not any(data)`」就等于「一个非透明像素都没有」，
    不需要 PIL，也不需要 `scripts/pixel_stats.py`（那个 import AppKit，CI 里跑不了）。
    """
    return not any(png_idat_raw(path))


def published_pngs(root: str) -> list[str]:
    return [path for path in published_files(root) if path.lower().endswith(".png")]


def check_blank_png(root: str, report: Report) -> None:
    """#5（🔴）空白取证图 —— 这条正是 17 张里 9 张空图漏进 HEAD 的原因。"""
    report.section("判据 #5 空白 PNG：IDAT 解压后全为 0 就是「一个非透明像素都没有」")
    pngs = published_pngs(root)
    if not pngs:
        report.ok("<已发布 PNG>", 0, "没有会被发布的 PNG")
        return
    for relpath in pngs:
        try:
            width, height = png_size(os.path.join(root, relpath))
            raw = png_idat_raw(os.path.join(root, relpath))
        except (OSError, ValueError, zlib.error) as exc:
            report.fail(relpath, 0, f"PNG 解析失败，无法确认非空：{exc}")
            continue
        if not any(raw):
            report.fail(
                relpath,
                0,
                f"空白图：{width}x{height}，IDAT 解压后 {len(raw)} 字节全为 0",
            )
        else:
            report.ok(relpath, 0, f"{width}x{height}，解压 {len(raw)} 字节，有非透明像素")


# —— #6 图片来源尺寸 ——————————————————————————————————————————————————————————————
#: 本机屏幕尺寸（`system_profiler` 实测 1512x982）：正好这个尺寸 = 很可能整屏截，把桌面带出去。
SCREEN_LIKE_SIZES = frozenset({(1512, 982), (3024, 1964)})


def check_png_screen_size(root: str, report: Report) -> None:
    """#6（🟡）图片尺寸 ≈ 屏幕。"""
    report.section("判据 #6 图片尺寸 ≈ 屏幕（🟡）：疑似连桌面一起截了")
    checked = 0
    suspects = 0
    for relpath in published_pngs(root):
        try:
            size = png_size(os.path.join(root, relpath))
        except (OSError, ValueError):
            continue  # 解析不了的由判据 #5 去报，别在这儿重复刷屏
        checked += 1
        if size in SCREEN_LIKE_SIZES:
            suspects += 1
            report.warn(relpath, 0, f"尺寸 {size[0]}x{size[1]} = 整屏，可能连桌面一起截了")
    if not suspects:
        report.ok("<已发布 PNG>", 0, f"{checked} 张 PNG，没有整屏尺寸的")


# —— #7 体积 ————————————————————————————————————————————————————————————————————
SIZE_LIMIT_BYTES = 300 * 1024


def check_file_sizes(root: str, report: Report) -> None:
    """#7（🟡）>300KB 的已发布文件逐个列出。"""
    report.section("判据 #7 体积（🟡）：>300KB 的已发布文件逐个列出")
    big: list[tuple[int, str]] = []
    for relpath in published_files(root):
        try:
            size = os.path.getsize(os.path.join(root, relpath))
        except OSError:
            continue
        if size > SIZE_LIMIT_BYTES:
            big.append((size, relpath))
    for size, relpath in sorted(big, reverse=True):
        report.warn(relpath, 0, f"{size} 字节（{size / 1024:.0f}KB）> 300KB，建议压缩或移出")
    if not big:
        report.ok("<已发布文件>", 0, "没有 >300KB 的文件")


# —— #8 许可文件 ————————————————————————————————————————————————————————————————
LICENSE_FILES = ("LICENSE", "ASSET_LICENSE.md")


def check_license_files(root: str, report: Report) -> None:
    """#8（🟡）许可文件在位且非空。"""
    report.section("判据 #8 许可文件在位（🟡）：LICENSE 与 ASSET_LICENSE.md 存在且非空")
    for name in LICENSE_FILES:
        full = os.path.join(root, name)
        if not os.path.isfile(full):
            report.warn(name, 0, "许可文件不存在")
        elif not read_text(full).strip():
            report.warn(name, 0, "许可文件是空的")
        else:
            report.ok(name, 0, f"{os.path.getsize(full)} 字节，非空")


# —— #9 重写前置条件 ————————————————————————————————————————————————————————————
def check_rewrite_precondition(root: str, report: Report) -> None:
    """#9（🔴/🟡）重写前置条件：远端有没有历史，以及**这次推送要不要 force-push**。

    `origin/HEAD` 在只说明「远端有历史」——那是不会消失的事实，任何 fetch 过的正常仓库都成立，
    拿它判红等于让门禁永红。真正决定要不要 force-push 的是**本地 HEAD 还是不是 `origin/HEAD` 的后代**
    （`git merge-base --is-ancestor` 的退出码：0 = 是 ⇒ 快进、不需要 force-push，🟡；
    1 = 不是 ⇒ 历史已分叉、推上去必须 force-push，🔴）。
    """
    report.section("判据 #9 重写前置条件：远端有没有历史 / 这次推送要不要 force-push")
    rc, out = git_capture(root, "remote", "-v")
    if rc == 127:
        report.warn("<git remote>", 0, "git 不可用，判断不了有没有 push 过")
        return
    if not out.strip():
        report.ok("<git remote>", 0, "本地没有 remote：但别默认远端没有历史，重写前先核对目标仓是否已存在")
        return
    rc_head, _ = git_capture(root, "rev-parse", "--verify", "origin/HEAD")
    if rc_head != 0:
        report.warn(
            "<git remote>",
            0,
            "有 remote 但取不到 origin/HEAD：证不了远端有没有历史，重写前用 `git ls-remote` 确认",
            out.strip(),
        )
        return
    rc_anc, _ = git_capture(root, "merge-base", "--is-ancestor", "origin/HEAD", "HEAD")
    if rc_anc == 0:
        report.warn(
            "<git remote>",
            0,
            "远端已有历史（origin/HEAD 在），但本地 HEAD 是它的后代：本次是快进推送，不需要 force-push",
            out.strip(),
        )
    elif rc_anc == 1:
        report.fail(
            "<git remote>",
            0,
            "本地历史已与远端分叉（origin/HEAD 不再是 HEAD 的祖先）：推上去要 force-push，先确认没人克隆过",
            out.strip(),
        )
    else:
        report.warn(
            "<git remote>",
            0,
            f"origin/HEAD 与 HEAD 的祖先关系判不了（git merge-base 退出码 {rc_anc}），重写前人工核一遍",
            out.strip(),
        )


# —— #10 去人称 ————————————————————————————————————————————————————————————————
#: 发布文件里不许出现的内部工作流代号。`\b` 必须带：`@users.noreply.github.com` 这种
#: 邮箱里的 `@users` 不带词边界时**不会**命中「user」这一项 —— 没有 `\b` 就会把邮箱判成代号（假红）。
HANDLE_RE = re.compile(r"@(?:lead|coder|ops|researcher|writer|user)\b")

#: 命中行里有这些记号 ⇒ 它是在**讲这条去人称规矩**（点名代号当反例），不是正文里真用了代号。
#: 守卫自己写这条规格、测试自己造样本都得写出这些代号；朴素写法会把它判红 ——
#: 同判据①③「守卫把守卫判红」那类假红，所以只做**同一条行内**豁免，且要打印（不许静默放过）。
HANDLE_RULE_MARKERS = (
    "Never write",
    "never write",
    "不出现",
    "去人称",
    "内部代号",
    "internal handles",
)


def check_internal_handles(root: str, report: Report) -> None:
    """#10（🔴）去人称：已发布文件里不许出现内部工作流代号。"""
    report.section("判据 #10 去人称：已发布文件里不许出现内部工作流代号")
    hits = 0
    exempt = 0
    scanned = 0
    for relpath in published_files(root):
        scanned += 1
        for lineno, text in published_lines(root, relpath):
            found = HANDLE_RE.findall(text)
            if not found:
                continue
            if any(word in text for word in RULE_WORDS) or any(
                marker in text for marker in HANDLE_RULE_MARKERS
            ):
                exempt += 1
                report.ok(
                    relpath,
                    lineno,
                    "讲去人称规则的行，豁免：" + "、".join(sorted(set(found))),
                )
                continue
            hits += 1
            report.fail(
                relpath, lineno, "出现内部工作流代号：" + "、".join(sorted(set(found))), text
            )
    if not hits:
        report.ok(
            "<已发布文件>", 0, f"{scanned} 个文件，无内部工作流代号（{exempt} 行豁免）"
        )


# —— #11 点名提交可解析 ——————————————————————————————————————————————————————————
#: 候选 token：7 位或 40 位小写十六进制，前后不许再贴着十六进制字符。
#: 前后夹断是必需的：不然 40 位 SHA 里会再切出中间 7 位、64 位 sha256 里也会切出
#: 一段 40 位当作独立 token，都是同一句里的重复假红。
COMMIT_REF_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{7}|[0-9a-f]{40})(?![0-9a-f])")

#: 怎么才算「点名了一个提交」：token 被反引号包住，或该行出现下面这些「讲提交」的词。
#: 为什么必须有这个语境判据：`1000000`、`1790702` 这种纯数字不是 SHA，
#: 而英文单词（如 defaced）、sha256 文件摘要也长得像十六进制——不卡语境就会把它们
#: 误判成「点名的提交」，全是假红。所以先按语境收口，再送去 git cat-file 验证。
REF_CONTEXT_WORDS = (
    "已修",
    "已并进",
    "之后",
    "那次",
    "那版",
    "参照",
    "取代",
    "提交",
    "commit",
    "rev",
    "见",
)


def in_commit_ref_context(line: str, token: str) -> bool:
    """这个 token 是否落在「点名一个提交」的语境里（反引号包裹，或同行有语境词）。"""
    for span in BACKTICK_RE.findall(line):
        if token in span:
            return True
    return any(word in line for word in REF_CONTEXT_WORDS)


#: 表格里「取回用的提交」那一格的形状：**整格**只有一个反引号包着的提交号。
#: 只认整格是必需的：状态列里也会出现反引号包着的提交号（那是退役理由的一部分，不是指针），
#: 按「行里所有提交号」去核就会连理由一起判红 —— 那是假红。
POINTER_CELL_RE = re.compile(r"^`([0-9a-f]{7,40})`$")


def retired_row_targets(
    relpath: str, text: str, known_basenames: frozenset[str]
) -> list[tuple[str, str, str | None]]:
    """表格行点到「工作树里已经不在的图」时，返回 [(名字, 仓库相对路径, 取回指针或 None)]。

    三个收口，每一个都是实测踩出来的假红：

    * 只认 `.md`：退役指针表是**文档**的承诺。`scripts/shoot_design_shots.py` 的模块
      docstring 里也有一张 `*.png` 表格（那是拍摄清单，名字相对**输出目录**），
      按指针去核就是把拍摄清单判红。
    * 只认**整个仓库里都找不到**的 basename：跨目录互相点名是常态
      （`docs/evidence/README.md` 点名 `docs/design/` 里的图），
      只看「相对本文件所在目录存在与否」会把它们误判成退役图。
    * 只认表格行，且活图行不算：活图索引里也写了提交号，但那是「什么时候拍的」，
      不是「去哪一版取回」。
    """
    if not relpath.endswith(".md") or not text.lstrip().startswith("|"):
        return []
    base = os.path.dirname(relpath)
    pointer: str | None = None
    for cell in text.strip().strip("|").split("|"):
        found = POINTER_CELL_RE.match(cell.strip())
        if found:
            pointer = found.group(1)
            break
    targets: list[tuple[str, str, str | None]] = []
    for name in BACKTICK_RE.findall(text):
        if not name.lower().endswith(".png"):
            continue
        if os.path.basename(name) in known_basenames:
            continue  # 还在版控里：这是活图索引行，不是退役指针
        path = os.path.normpath(os.path.join(base, name))
        targets.append((name, path, pointer))
    return targets


def check_retired_rows(
    root: str,
    report: Report,
    relpath: str,
    lineno: int,
    text: str,
    ref_types: dict[str, tuple[int, str]],
    known_basenames: frozenset[str],
) -> int:
    """退役表那一行的可核承诺：指针要取得到那张图，而且是**最后动过它**的提交。

    为什么按「最后动过」核、而不是只核「取得到」：任何后代提交里这张图都还在，
    所以随便指一个更晚的提交也能 `git show` 出来 —— 但拿回来的是**更晚那一版**，
    表下那句「那一版里最后动过这张图的提交」就成了假话。指到更早的提交同理。
    口径由文档自己写死在表下，判据只是把它变成机器能验的东西。
    """
    problems = 0
    for name, path, pointer in retired_row_targets(relpath, text, known_basenames):
        if pointer is None:
            report.fail(
                relpath,
                lineno,
                f"退役图 {name} 没给「取回用的提交」：这一节的口径是名字 + 退役原因 + 那一版里的提交",
                text,
            )
            problems += 1
            continue
        key = f"{pointer}:{path}"
        if key not in ref_types:
            ref_types[key] = git_capture(root, "cat-file", "-t", key)
        rc, out = ref_types[key]
        if rc == 127:
            report.warn("<git cat-file>", 0, "git 不可用，退役表的取回指针无法解析")
            return problems + 1
        if rc != 0:
            report.fail(
                relpath,
                lineno,
                f"取回用的提交里没有这张图：git cat-file -t {key} 取不到",
                text,
            )
            problems += 1
            continue
        if out.strip() != "blob":
            report.fail(
                relpath,
                lineno,
                f"{key} 不是文件：git cat-file -t 返回 {out.strip()}",
                text,
            )
            problems += 1
            continue
        rc_log, last = git_capture(
            root, "log", "-1", "--diff-filter=AMRC", "--format=%H", "HEAD", "--", path
        )
        last = last.strip()
        if rc_log != 0 or not last:
            report.fail(
                relpath,
                lineno,
                f"历史里找不到动过 {path} 的提交，证不了这个取回指针",
                text,
            )
            problems += 1
            continue
        if not last.startswith(pointer):
            report.fail(
                relpath,
                lineno,
                f"取回用的提交不是最后动过这张图的提交：口径要求 {last[:7]}，表里写的是 {pointer}",
                text,
            )
            problems += 1
            continue
        report.ok(relpath, lineno, f"{name}：{pointer} 里取得到，且是最后动过它的提交")
    return problems


def check_doc_commit_refs(root: str, report: Report) -> None:
    """#11（🔴 .md / 🟡 scripts/*.py）已发布文件里逐字点名的提交，必须能在本仓解析。

    只扫 `.md` 与 `.py`；`tests/` 下的 `.py` 直接跳过：测试故意现造死 SHA 当样本，
    扫它就是「守卫把守卫自己的测试判红」那类假红（同判据①、#10 已有的豁免口径）。
    同一个 token 的 `git cat-file -t` 结果缓存复用，仓库里点同一提交多处的只查一次。
    """
    report.section("判据 #11 点名提交可解析：点名的提交要能解析，退役表的取回指针还要取得到那张图")
    resolved_types: dict[str, tuple[int, str]] = {}
    published = published_files(root)
    #: 已发布文件的 basename：判「这张图还在不在版控里」，跨目录点名也认得出是活图。
    known_basenames = frozenset(os.path.basename(path) for path in published)
    hits = 0
    problems = 0
    for relpath in published:
        if not relpath.endswith((".md", ".py")):
            continue
        if relpath.startswith("tests/") and relpath.endswith(".py"):
            continue
        for lineno, text in published_lines(root, relpath):
            for match in COMMIT_REF_RE.finditer(text):
                token = match.group(1)
                if not any(ch in "abcdef" for ch in token):
                    continue  # 纯数字 7 位串不是提交号（1000000、1790702 这类）
                if not in_commit_ref_context(text, token):
                    continue  # 英文单词 / 文件摘要不落在点名提交的语境里，不算
                hits += 1
                if token not in resolved_types:
                    resolved_types[token] = git_capture(root, "cat-file", "-t", token)
                rc, out = resolved_types[token]
                if rc == 127:
                    report.warn("<git cat-file>", 0, "git 不可用，点名的提交无法解析")
                    return
                if rc != 0:
                    message = f"点名了不存在的提交 {token}：git cat-file -t 取不到"
                    if relpath.endswith(".md"):
                        report.fail(relpath, lineno, message, text)
                    else:
                        report.warn(relpath, lineno, message, text)
                    problems += 1
                    continue
                object_type = out.strip()
                if object_type == "commit":
                    continue
                report.warn(
                    relpath,
                    lineno,
                    f"点名的 {token} 不是提交对象：git cat-file -t 返回 {object_type}",
                    text,
                )
                problems += 1
            problems += check_retired_rows(
                root, report, relpath, lineno, text, resolved_types, known_basenames
            )
    if not problems:
        report.ok("<已发布文件>", 0, f"{hits} 处提交引用全部可解析（含退役表的取回指针）")


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
    parser.add_argument(
        "--json",
        action="store_true",
        help="按 JSON 输出（给 CI 用；不打印逐条人读结论）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = os.path.abspath(args.root) if args.root else DEFAULT_ROOT
    if not os.path.isdir(root):
        print(f"release_check: 仓库根不存在：{root}", file=sys.stderr)
        return 2

    report = Report(quiet=args.json)
    if not args.json:
        print(f"发布前检查 · root={root}")
        print("只核对「文档里的声明 ↔ 仓库事实」；门禁是否跑绿不在这里回答。")

    check_wording(root, report)
    check_declared_paths(root, report)
    check_gate_scripts(root, report)
    check_docs_reachable(root, report)
    # 规格表 #1–#11：顺序与清单一致，FAIL/WARN 都带位置与原句。
    check_history_identity(root, report)
    check_abs_paths(root, report)
    check_secrets(root, report)
    check_untracked_sensitive(root, report)
    check_blank_png(root, report)
    check_png_screen_size(root, report)
    check_file_sizes(root, report)
    check_license_files(root, report)
    check_rewrite_precondition(root, report)
    check_internal_handles(root, report)
    check_doc_commit_refs(root, report)

    rc = report.summary()
    if args.json:
        print(json.dumps(report.as_dict(root, rc), ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
