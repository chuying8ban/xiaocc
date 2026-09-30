"""`scripts/release_check.py` 规格表 #1–#11 的反向控制（每条都得能被现造样本判红）。

本仓规矩是「不许只写不验」：一条判据如果没人证明过它会红，那它绿不绿就没有信息量。
这里在 `tmp_path` 里**现搭一个最小 git 仓库**（造样本 → 跑 → 看 rc / 看那一行 → 目录自己清掉），
所以既不碰真仓库、也不留垃圾。

同时守反面：干净仓库必须 `rc=0`。假红（守卫把自己或守卫的测试判红）比漏报更容易让门禁
被关掉 —— 判据 #2/#3 里那几条「占位用户名 / 讲规则的行」豁免，就是为这个加的，这里也钉住。
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import release_check

CLEAN_EMAIL = "t@example.com"
CLEAN_NAME = "T"

#: 一个「除了被测那一项之外全绿」的最小仓库。判据 #1–#11 之外还要过 ①–④：
#: 所以得有文档目录（① 需要）、入口 md（④ 需要）、13 支门禁脚本（③ 需要）。
BASE_FILES: dict[str, str | bytes] = {
    "README.md": "# t\n",
    "README_EN.md": "# t\n",
    "LICENSE": "MIT License\n",
    "ASSET_LICENSE.md": "原创（AI 辅助的程序化矢量生成）\n",
}


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, env=env
    )
    return done.stdout


def make_repo(
    tmp_path: Path,
    files: dict[str, str | bytes],
    *,
    commit: bool = True,
    author_env: dict[str, str] | None = None,
    remote: str | None = None,
) -> Path:
    """现造一个仓库。`docs/` 空目录也要建：判据① 缺了它会直接判红。"""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", CLEAN_EMAIL)
    git(root, "config", "user.name", CLEAN_NAME)
    (root / "docs").mkdir()
    scripts_dir = root / "scripts"
    scripts_dir.mkdir()
    for name in release_check.GATE_SCRIPTS:
        (scripts_dir / f"{name}.py").write_text("", encoding="utf-8")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    git(root, "add", "-A")
    if commit:
        env = dict(os.environ)
        if author_env:
            env.update(author_env)
        git(root, "commit", "-q", "-m", "init", env=env)
    if remote is not None:
        git(root, "remote", "add", "origin", remote)
    return root


def run_check(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = release_check.main(["--root", str(root)])
    return rc, capsys.readouterr().out


def make_png(width: int, height: int, *, blank: bool = True) -> bytes:
    """现造一张 PNG。`blank=True` 就是「一个非透明像素都没有」的空图。

    这正是本仓踩过的坑：屏睡着的 `CGWindowListCreateImage` 返回全透明图，
    而断言只看层级/尺寸这些元数据、不看像素，于是 9 张空图照样通过并进了 HEAD。
    所以全 0 的 IDAT 就是判据 #5 必须判红的那个样本。
    """
    rows = [b"\x00" + b"\x00" * (width * 4) for _ in range(height)]
    if not blank:
        rows[0] = b"\x00\xff\xff\x00\x00" + b"\x00" * (width * 4 - 4)
    raw = b"".join(rows)

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        release_check.PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ——————————————————————————————————————————————————————————————————————————————
# 正面控制：干净仓库必须绿。这条挂了说明新判据在造假红，比漏报更该先修。
# ——————————————————————————————————————————————————————————————————————————————


def test_clean_repo_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path, dict(BASE_FILES))
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "FAIL 0 条" in out


def test_bad_root_is_usage_error(tmp_path: Path) -> None:
    """退出码 2 = 用法错。argparse 的 `type=existing_dir` 走 `parser.error()`。"""
    with pytest.raises(SystemExit) as excinfo:
        release_check.main(["--root", str(tmp_path / "nope")])
    assert excinfo.value.code == 2


def test_json_output_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path, dict(BASE_FILES))
    rc = release_check.main(["--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rc"] == rc == 0
    assert payload["fail"] == 0
    assert any(rec["path"] == "<git remote>" for rec in payload["records"])


# ——————————————————————————————————————————————————————————————————————————————
# #1 历史身份
# ——————————————————————————————————————————————————————————————————————————————


def test_history_identity_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """样本 = 一次以 `<用户>@<主机名>.local` 为作者/提交者的提交。"""
    leaking = "dev@buildbox.local"
    root = make_repo(
        tmp_path,
        dict(BASE_FILES),
        author_env={
            "GIT_AUTHOR_EMAIL": leaking,
            "GIT_COMMITTER_EMAIL": leaking,
            "GIT_AUTHOR_NAME": "C Chen",
            "GIT_COMMITTER_NAME": "C Chen",
        },
    )
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert "FAIL <git 历史>:0  身份泄漏：" in out
    assert leaking in out


def test_history_identity_stays_green_for_a_plain_email(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """反向的反向：`chenc@example.com` 这种正常身份不许判红（清单里剩下的那些身份就是它）。"""
    root = make_repo(tmp_path, dict(BASE_FILES))
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "无本机身份" in out


# ——————————————————————————————————————————————————————————————————————————————
# #2 绝对路径 / 用户名
# ——————————————————————————————————————————————————————————————————————————————


def test_abs_path_in_published_file_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = dict(BASE_FILES)
    leaked = "/Users/" + "ciuser"
    files["ops/ai.hermes.xiaocc.plist"] = (
        "\t<string>" + leaked + "/ChenC/xiaocc/ops/run-panel.sh</string>\n"
    )
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert "FAIL ops/ai.hermes.xiaocc.plist:1  硬编码绝对路径：" + leaked in out


def test_placeholder_home_is_exempt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`/Users/alice` 是测试里的假主目录（有意为之）⇒ 豁免，但必须打印出来。"""
    files = dict(BASE_FILES)
    files["tests/test_hermes_source.py"] = '_FAKE_HOME = "/Users/alice"\n'
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "占位用户名，豁免：/Users/alice" in out


def test_abs_path_allowlisted_file_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """清单点名的白名单文件（`sources/hermes.py` 的文档串）不许判红。"""
    files = dict(BASE_FILES)
    files[release_check.ABS_PATH_ALLOWLIST[0]] = (
        '"""用户主目录本身（``/Users/xxx``、``/home/xxx``）。"""\n'
    )
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "文档串白名单" in out


# ——————————————————————————————————————————————————————————————————————————————
# #3 密钥
# ——————————————————————————————————————————————————————————————————————————————


@pytest.mark.parametrize(
    "line,label",
    [
        ("KEY = " + '"sk-' + "abcdefghijklmnop" + '"\n', "sk- 密钥"),
        ("headers = " + '{"Authorization": "Bearer ' + "abcdefghijklmnop" + '"}\n', "Bearer 令牌"),
        ("-----BEGIN " + "RSA PRIVATE KEY-----\n", "私钥文件头"),
        ("api_key" + " = " + '"' + "hunter2hunter2" + '"\n', "凭据赋值"),
    ],
)
def test_secret_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str, label: str
) -> None:
    files = dict(BASE_FILES)
    files["conf/settings.py"] = line
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert "FAIL conf/settings.py:1  疑似凭据" in out
    assert label in out


def test_rule_describing_line_is_exempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """守卫描述守卫自己的那些行（清单抄了模式、测试造了占位值）不许判红。"""
    files = dict(BASE_FILES)
    # 放 docs/ 外：docs/ 下的新文件会被判据④ 当孤儿件判红，那是另一条判据的事。
    files["conf/rules.md"] = "`Bearer <token>`、`secret = \"«redacted:sk-…»\"`\n"
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "讲规则 / 占位值的行，豁免" in out


# ——————————————————————————————————————————————————————————————————————————————
# #4 未跟踪的敏感文件（🟡）
# ——————————————————————————————————————————————————————————————————————————————


@pytest.mark.parametrize("name", ["probe.db", "watchdog.log", "anchor.json", "01-idle.desktop.png"])
def test_untracked_sensitive_is_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    root = make_repo(tmp_path, dict(BASE_FILES))
    (root / name).write_text("x", encoding="utf-8")
    rc, out = run_check(root, capsys)
    assert rc == 0, out  # 🟡 不影响退出码
    assert f"WARN {name}:0" in out


def test_untracked_ordinary_file_is_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path, dict(BASE_FILES))
    (root / "notes.txt").write_text("x", encoding="utf-8")
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "notes.txt" not in out


# ——————————————————————————————————————————————————————————————————————————————
# #5 空白 PNG（🔴）—— 这条正是这次抓到 9/17 张空图的原因
# ——————————————————————————————————————————————————————————————————————————————


def test_png_helpers_agree_on_a_blank_sample(tmp_path: Path) -> None:
    """先钉住工具本身：全零 IDAT 的图必须被认成空图，有像素的不能被认成空图。"""
    blank = tmp_path / "blank.png"
    blank.write_bytes(make_png(4, 4, blank=True))
    assert release_check.png_size(str(blank)) == (4, 4)
    assert release_check.png_pixels_are_blank(str(blank)) is True

    painted = tmp_path / "painted.png"
    painted.write_bytes(make_png(4, 4, blank=False))
    assert release_check.png_pixels_are_blank(str(painted)) is False


def test_blank_png_in_repo_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = dict(BASE_FILES)
    files["docs/evidence/06-state-idle.png"] = make_png(320, 388, blank=True)
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert "FAIL docs/evidence/06-state-idle.png:0  空白图：" in out


def test_real_evidence_png_is_not_blank() -> None:
    """正例：`docs/evidence/06-state-idle.png`（刚补拍过）必须判非空。"""
    real = Path(__file__).resolve().parent.parent / "docs" / "evidence" / "06-state-idle.png"
    if not real.is_file():
        pytest.skip("这张取证图不在仓库里")
    assert release_check.png_pixels_are_blank(str(real)) is False


def test_broken_png_is_red_not_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """解析不了的图**不许**静默放行 —— 那是「假绿」，比赛过还糟。"""
    files = dict(BASE_FILES)
    files["docs/evidence/broken.png"] = b"not a png at all\n"
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert "FAIL docs/evidence/broken.png:0  PNG 解析失败" in out


# ——————————————————————————————————————————————————————————————————————————————
# #6 图片尺寸 ≈ 屏幕（🟡）
# ——————————————————————————————————————————————————————————————————————————————


@pytest.mark.parametrize("size", [(1512, 982), (3024, 1964)])
def test_screen_sized_png_is_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], size: tuple[int, int]
) -> None:
    files = dict(BASE_FILES)
    files["assets/shot.png"] = make_png(*size, blank=False)
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert f"WARN assets/shot.png:0  尺寸 {size[0]}x{size[1]} = 整屏" in out


def test_normal_sized_png_is_quiet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = dict(BASE_FILES)
    files["assets/shot.png"] = make_png(320, 388, blank=False)
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "1 张 PNG，没有整屏尺寸的" in out


# ——————————————————————————————————————————————————————————————————————————————
# #7 体积（🟡）
# ——————————————————————————————————————————————————————————————————————————————


def test_oversized_file_is_warn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = dict(BASE_FILES)
    files["assets/big.bin"] = b"\0" * (release_check.SIZE_LIMIT_BYTES + 1)
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out  # 🟡 不影响退出码
    assert "WARN assets/big.bin:0" in out
    assert "300KB" in out


def test_at_the_limit_is_quiet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """正好 300KB 不算超 —— 边界只判 `>`，别把「刚好卡线」也报出来。"""
    files = dict(BASE_FILES)
    files["assets/edge.bin"] = b"\0" * release_check.SIZE_LIMIT_BYTES
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "WARN assets/edge.bin" not in out


# ——————————————————————————————————————————————————————————————————————————————
# #8 许可文件（🟡）
# ——————————————————————————————————————————————————————————————————————————————


def test_missing_license_is_warn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {name: text for name, text in BASE_FILES.items() if name != "LICENSE"}
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "WARN LICENSE:0  许可文件不存在" in out


def test_missing_asset_license_is_warn_and_also_reddens_rule_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """拿掉 `ASSET_LICENSE.md`：判据 #8 报 🟡，**同时**判据① 会因为「来源声明文件不存在」报 🔴。

    这条故意把耦合写下来 —— 免得以后有人看见 `WARN ASSET_LICENSE.md` 却以为退出码该是 0。
    """
    files = {name: text for name, text in BASE_FILES.items() if name != "ASSET_LICENSE.md"}
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert "WARN ASSET_LICENSE.md:0  许可文件不存在" in out
    assert "FAIL ASSET_LICENSE.md:0  来源声明文件不存在" in out


def test_empty_license_is_warn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = dict(BASE_FILES)
    files["LICENSE"] = "\n  \n"
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "WARN LICENSE:0  许可文件是空的" in out


# ——————————————————————————————————————————————————————————————————————————————
# #9 重写前置条件（🔴）
# ——————————————————————————————————————————————————————————————————————————————


def test_remote_without_origin_head_is_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    root = make_repo(tmp_path, dict(BASE_FILES), remote=str(bare))
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "WARN <git remote>:0" in out
    assert "git ls-remote" in out   # 让作者去远端核对有没有历史，别再默认「本地没 remote 就等于远端是空的」


def test_remote_with_origin_head_fast_forward_is_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """推过 + 有 origin/HEAD，但本地 HEAD 还是它的后代 ⇒ 🟡：远端有历史是事实，快进推送不需要 force-push。

    这条同时是假红守卫：只按「origin/HEAD 在」判红，任何 fetch 过的正常仓库都会永红。
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    root = make_repo(tmp_path, dict(BASE_FILES), remote=str(bare))
    git(root, "push", "-q", "origin", "HEAD:refs/heads/main")
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "WARN <git remote>:0" in out
    assert "本次是快进推送，不需要 force-push" in out
    assert "FAIL <git remote>" not in out


def test_remote_with_origin_head_diverged_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """推过之后改写已推送的提交（amend）⇒ origin/HEAD 不再是 HEAD 的祖先 ⇒ 🔴 推上去要 force-push。"""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    root = make_repo(tmp_path, dict(BASE_FILES), remote=str(bare))
    git(root, "push", "-q", "origin", "HEAD:refs/heads/main")
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(root, "commit", "-q", "--amend", "-m", "改写已推送的提交")
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert "FAIL <git remote>:0  本地历史已与远端分叉" in out
    assert "force-push" in out


# ——————————————————————————————————————————————————————————————————————————————
# #10 去人称（🔴）
# ——————————————————————————————————————————————————————————————————————————————

#: 样本里的真代号用拼接造：直接把「@」和代号连写，会把本测试文件自己判红（判据 #10 扫全仓）。
_LEAD = "@" + "lead"
_CODER = "@" + "coder"


def test_internal_handle_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = dict(BASE_FILES)
    files["ops/run-panel.sh"] = f"# 门槛：CPU < 5%（{_LEAD} 定的）\n"
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert f"FAIL ops/run-panel.sh:1  出现内部工作流代号：{_LEAD}" in out


def test_noreply_github_email_is_not_a_handle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`@users.noreply.github.com` 这种邮箱不带词边界时不许被判成代号（没有 `\b` 就会假红）。"""
    files = dict(BASE_FILES)
    files["conf/meta.py"] = 'AUTHOR = "158806394+chuying8ban@users.noreply.github.com"\n'
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "FAIL conf/meta.py" not in out


def test_rule_describing_handle_line_is_exempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """讲「去人称」规矩的行会点名代号，必须豁免并打印（不许静默放过）。"""
    files = dict(BASE_FILES)
    files["conf/rules.md"] = f"# 去人称：已发布文件不出现 {_LEAD} / {_CODER} 这类内部代号\n"
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "讲去人称规则的行，豁免" in out


def test_handle_scan_covers_src_not_just_docs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """范围必须是全仓（`git ls-files`）：src/ 下的代号同样要判红，不是只扫 docs/。"""
    files = dict(BASE_FILES)
    files["src/xiaocc/quota/badge.py"] = f"# 实测（{_CODER}）：四舍五入到 5%\n"
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert f"FAIL src/xiaocc/quota/badge.py:1  出现内部工作流代号：{_CODER}" in out


# ——————————————————————————————————————————————————————————————————————————————
# #11 点名提交可解析（🔴 .md / 🟡 scripts/*.py）
# ——————————————————————————————————————————————————————————————————————————————


def test_dead_commit_ref_in_docs_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """docs/ 下 `.md` 点名一个不存在的 7 位 SHA ⇒ 🔴，FAIL 并带出原句。"""
    files = dict(BASE_FILES)
    files["README.md"] = "# t\n\n[设计记录](docs/design/README.md)\n"
    files["docs/design/README.md"] = "# 设计\n\n旧版在 `0badc0d` 那次之后退役\n"
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert "FAIL docs/design/README.md:3  点名了不存在的提交 0badc0d：git cat-file -t 取不到" in out


def test_real_commit_ref_in_docs_is_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """现造一个真实提交，docs/ 下 `.md` 用它带语境词的短 SHA ⇒ rc == 0。"""
    files = dict(BASE_FILES)
    files["README.md"] = "# t\n\n[设计记录](docs/design/README.md)\n"
    files["docs/design/README.md"] = "# 设计\n"
    root = make_repo(tmp_path, files)
    # 短 SHA 前 7 位有可能全是数字（判据 #11 把纯数字串当非提交号跳过），
    # 那就再补一个提交，直到拿到一个含 a–f 字母、能被判据当提交号收下的短 SHA。
    short = ""
    for _ in range(8):
        git(root, "commit", "-q", "--allow-empty", "-m", "real commit")
        candidate = git(root, "rev-parse", "--short=7", "HEAD").strip()
        if any(ch in "abcdef" for ch in candidate):
            short = candidate
            break
    assert short, "造不出含字母的短 SHA（概率上几乎不可能）"
    (root / "docs/design/README.md").write_text(
        f"# 设计\n\n重拍参照 `{short}` 那版。\n", encoding="utf-8"
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "point at real commit")
    rc, out = run_check(root, capsys)
    assert rc == 0, out


def test_non_commit_hex_is_not_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """假 token 不许被报：纯数字 `1000000` 与 64 位 sha256 都要静默放行。"""
    files = dict(BASE_FILES)
    files["README.md"] = "# t\n\n[设计记录](docs/design/README.md)\n"
    sha256 = "e" * 64
    files["docs/design/README.md"] = (
        f"# 设计\n\n纯数字 `1000000` 与 sha256 `{sha256}` 都不算提交号。\n"
    )
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "1000000" not in out
    assert sha256 not in out


def test_dead_commit_ref_in_scripts_is_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """scripts/ 下 `.py` 的注释里点一个不存在的 7 位 SHA ⇒ 只 🟡、不影响退出码。"""
    files = dict(BASE_FILES)
    files["scripts/appkit_screenshots.py"] = "# 参照 0badc0d 那版\n"
    root = make_repo(tmp_path, files)
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "WARN scripts/appkit_screenshots.py:1  点名了不存在的提交 0badc0d：git cat-file -t 取不到" in out


# ——————————————————————————————————————————————————————————————————————————————
# #11 后半段：退役表「取回用的提交」——那个提交里要真取得到那张图，
# 且必须是**最后动过它**的提交（表下自己写的口径，判据只是把它变成能机器验的东西）。
# ——————————————————————————————————————————————————————————————————————————————


def _commit_all(root: Path, message: str) -> str:
    """把仓库现有改动提交掉，返回完整 SHA。"""
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


def _design_repo(tmp_path: Path, *, with_shot: bool) -> Path:
    """最小仓库 + 一个 docs/design/（要被判据④从 README 走到）。"""
    files: dict[str, str | bytes] = dict(BASE_FILES)
    files["README.md"] = "# t\n\n[设计记录](docs/design/README.md)\n"
    files["docs/design/README.md"] = "# 设计\n"
    if with_shot:
        files["docs/design/旧图.png"] = make_png(4, 4, blank=False)
    return make_repo(tmp_path, files)


def _write_retired_row(root: Path, pointer: str) -> None:
    """写成退役表：那张图已经不在工作树里，取回指针是 `pointer`。"""
    (root / "docs/design/README.md").write_text(
        "# 设计\n\n"
        "| 文件 | 内容 | 状态 | 取回用的提交 |\n"
        "| --- | --- | --- | --- |\n"
        f"| `旧图.png` | 早期界面 | 已退役（被新版取代） | `{pointer}` |\n",
        encoding="utf-8",
    )


def test_retired_pointer_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """指针指对了（那一版里取得到，且是最后动过它的提交）⇒ 绿。"""
    root = _design_repo(tmp_path, with_shot=True)
    keep = git(root, "rev-parse", "HEAD").strip()  # 建仓那次提交就是「留下这张图的那一版」
    git(root, "rm", "-q", "docs/design/旧图.png")
    _commit_all(root, "退役：图不留在工作树")
    _write_retired_row(root, keep[:7])
    _commit_all(root, "退役表")
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert f"旧图.png：{keep[:7]} 里取得到，且是最后动过它的提交" in out


def test_retired_pointer_at_older_commit_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """指到更早的提交：图照样 `git show` 得出来，但不是最后动过它的那一版 ⇒ 红。

    实仓就是这个形状：指到「退役理由」那个提交（它只是又往后走了一步），
    能解析、能取回，但表下那句「最后动过这张图的提交」是假的。
    """
    root = _design_repo(tmp_path, with_shot=True)
    early = git(root, "rev-parse", "HEAD").strip()  # 建仓那次提交留下的就是第一版截图
    (root / "docs/design/旧图.png").write_bytes(make_png(6, 6, blank=False))
    last = _commit_all(root, "重拍了一版")
    git(root, "rm", "-q", "docs/design/旧图.png")
    _commit_all(root, "退役：图不留在工作树")
    _write_retired_row(root, early[:7])
    _commit_all(root, "退役表")
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert (
        f"取回用的提交不是最后动过这张图的提交：口径要求 {last[:7]}，表里写的是 {early[:7]}" in out
    )


def test_retired_pointer_without_the_file_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """指到这张图还不存在的那一版 ⇒ 红：这一节承诺的是「一条命令取回」。"""
    root = _design_repo(tmp_path, with_shot=False)
    before = git(root, "rev-parse", "HEAD").strip()
    (root / "docs/design/旧图.png").write_bytes(make_png(4, 4, blank=False))
    _commit_all(root, "加图")
    git(root, "rm", "-q", "docs/design/旧图.png")
    _commit_all(root, "退役：图不留在工作树")
    _write_retired_row(root, before[:7])
    _commit_all(root, "退役表")
    rc, out = run_check(root, capsys)
    assert rc == 1
    assert (
        f"取回用的提交里没有这张图：git cat-file -t {before[:7]}:docs/design/旧图.png 取不到" in out
    )


def test_py_docstring_table_is_not_read_as_a_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """假红守卫：`.py` 模块 docstring 里的 `*.png` 表格（拍摄清单，名字相对输出目录）不是指针表。

    实仓踩到的就是这条：`scripts/shoot_design_shots.py` 的 docstring 里有一张 11 行的拍摄清单表，
    那些名字是写入 `docs/design/` 的，相对脚本目录当然找不到 —— 按指针核就是 11 条假红。
    """
    root = _design_repo(tmp_path, with_shot=False)
    (root / "scripts/shoot_demo.py").write_text(
        "# 拍摄清单\n\n| 文件名 | how |\n| --- | --- |\n| `没拍过.png` | window |\n",
        encoding="utf-8",
    )
    _commit_all(root, "拍摄清单")
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "没给「取回用的提交」" not in out


def test_live_image_row_is_not_read_as_a_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """假红守卫：活图索引行里也有提交号（那是「什么时候拍的」），不许被当成退役指针。"""
    root = _design_repo(tmp_path, with_shot=True)
    head = git(root, "rev-parse", "HEAD").strip()  # 图还在工作树里：这一行是活图索引
    (root / "docs/design/README.md").write_text(
        "# 设计\n\n"
        "| 文件 | 拍摄时间 |\n"
        "| --- | --- |\n"
        f"| `旧图.png` | 2026-09-30 00:30 · 工作树（`{head}` 之后） |\n",
        encoding="utf-8",
    )
    _commit_all(root, "活图索引")
    rc, out = run_check(root, capsys)
    assert rc == 0, out
    assert "没给「取回用的提交」" not in out
