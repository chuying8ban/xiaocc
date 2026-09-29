# 发布前 checklist

核查人：@researcher，2026-09-29。范围：**git 跟踪的文件**（100 个）+ 提交历史 + 工作区里
可能被 `git add -A` 带进去的未跟踪文件。每条都写了「怎么查」和「现状」，**现状是实测结果**。
自动可跑的部分见文末规格（脚本待 @coder 走 codex 落成 `scripts/release_check.py`）。

现状按严重度排：🔴 必须清 / 🟡 建议清 / ✅ 已干净。

## 🔴 1. 提交历史里的身份（唯一「发出去就难改」的一项）

`git log --format='%ae%ce' | grep -c 'a29285@'` → **30 / 45 个提交**带
`C Chen <a29285@MacBook-Pro.local>`：**本机用户名 + 主机名进了永久历史**，push 之后就跟着每个 clone。
其余身份是 `ChenC <chenc@example.com>` / `<chenc@local>`。

- 怎么查：`git log --format='%an <%ae> | %cn <%ce>' | sort | uniq -c`；仓库 local 与 global 的
  `user.email` **都是空的**，所以 git 用了 `<用户>@<主机名>.local` 兜底（`hostname` = `MacBook-Pro.local`）。
- 怎么修（**必须在首次 push 之前**，之后就要 force-push 并打乱别人的 clone）：
  1. `git tag pre-publish-backup`（先留退路）；
  2. 设好发布身份：`git config --local user.name "<发布名>"` / `git config --local user.email "<发布邮箱或 noreply>"`；
  3. 重写全部历史：`git rebase -r --root --exec 'git commit --amend --no-edit --reset-author'`
     （stock git，不需要 filter-repo；45 个提交、无 merge 的话一次过）；
  4. 验证：`git log --format='%ae' | sort -u` 只应剩下发布邮箱；
     `git rev-list --count HEAD` 应仍是 45（提交数不变、内容不变，可用 `git diff pre-publish-backup HEAD` = 空自查）。
- 归谁：@lead/@user 定发布身份（取决于用哪个 GitHub 账号），@ops 或 @coder 执行。

## 🔴 2. 空白取证图进了 HEAD（公开仓库的证据表整体空档）

**17 张跟踪 PNG 里 9 张是全透明空图**，而且不同的状态截图其实是**同一张空文件**：

| 文件 | 尺寸 | md5(前10) |
| --- | --- | --- |
| `01-idle-floating.png` / `04-hover-expanded` / `06-state-idle` / `07-state-working` / `08-state-error` / `09-state-done` | 316×380 | **6 张完全相同** `8dd1679a4e` |
| `02-docked-collapsed` / `03-handle-hover-no-flicker` / `05-leave-recalls` | 26×228 | **3 张完全相同** `925ecc92a2` |

- 怎么查（**零依赖、CI 可跑**）：PNG 全像素为 0 ⇒ zlib 解出的所有扫描线（含任意 filter）必然全 0 字节，
  所以「IDAT 解压后 `not any(data)`」就是「一个非透明像素都没有」。**不要用 PIL（venv 里没有），
  也不要用 `scripts/pixel_stats.py`（它 import AppKit，CI 里跑不了）。**
- 成因已由 @writer 定位：`048fa6f` 重跑截图时显示器是睡着的，`CGWindowListCreateImage` 在那个状态下返回空图，
  而断言 20/20 照样过 —— 它查的是层级/尺寸/穿透这些元数据，**不看像素**。
- 修复顺序：① `appkit_screenshots.py` 抓图前 `CGDisplayIsAsleep` 就拒跑（别静默写空图）；
  ② 把非透明像素占比接进断言（空图不许通过）；③ **屏幕亮着**重跑一次，参照物：`8ff7304` 那版
  `09-state-done.png` = 320×388、不透明 30.6%（@writer 用 `pixel_stats.py` 统计的真图）。
- 归谁：@ops（抓图）+ @coder（护栏）。

## 🔴 3. 硬编码绝对路径 / 用户名（21 处，7 个文件）

`git grep -InE '/Users/[A-Za-z0-9._-]+'` → 21 处，全部在运维线：
`ops/ai.hermes.xiaocc.plist`、`ops/ai.hermes.xiaocc.watchdog.plist`、`ops/ai.hermes.xiaocc.reverify.plist`、
`ops/baseline/*.bak`（3 个）。`a29285` 共 15 处。`/home/` 唯一 1 处是 `sources/hermes.py` 的文档串（无害）。

- plist **不能**靠 `$HOME` 相对（launchd 不对 `ProgramArguments` 做变量展开）⇒ 模板 + `install` 时渲染；
  注意 `doctor` ① 现在逐字节比对仓库原件与安装件，要改成比对**渲染后**的那份。
- `docs/evidence/evidence.json` **已清**（`0e3083c`，实测不在命中列表里）。
- `ops/baseline/*.bak` 是**小汐/MascotPet 的私有配置**，不是小cc 的资产，建议移出公开仓库 + gitignore。
- 归谁：@ops（他的文件）+ @coder（如需渲染器）。

## ✅ 4. 私人截图 / 桌面壁纸：**已经是干净的，不用清**

@lead 提到的 7 张 `*.desktop.png`：**全部未被 git 跟踪**，而且 `.gitignore:26` 就写着
`docs/evidence/*.desktop.png`（`git check-ignore -v` 实测命中）。所以公开仓库里没有壁纸，
**动作只剩「保留那行 gitignore」**。另外这 7 张工作区副本现在也全是空图（同第 2 条），
本来就没拍到壁纸。这条可以从「要清」降级为「已覆盖」。

## ✅ 5. 密钥 / 凭据：干净

`sk-[A-Za-z0-9]{12,}`、`Bearer <token>`、`BEGIN * PRIVATE KEY`、
`(api_key|secret|password|token)\s*[:=]\s*['"]...`、`授权码|口令|imap_pass` —— **命中 0**。
（`~/.config/himalaya/qq_imap_pass` 在仓库外，未入库。）

## ✅ 6. 本机产物 / 体积

- `.env` / `*.pem` / `*.key` / `*.db` / `*.sqlite` / `*.log` 入库：**0**（运行时目录 `~/.xiaocc/` 与 `*.log` 已在 `.gitignore`）。
- 跟踪文件里 >300KB 的：**0**（最大是 `13-art-states-montage.png` 201KB）。
- 例外：`ops/baseline/*.bak` 三个（见第 3 条）。
- 建议补进 `.gitignore`：`docs/audits/`（若以后有本机审计产物）、`*.probe.json`、`anchor.json`、`probe.json`、
  `skip-motion.json`、`reverify-ok.json`（运维的运行时证据文件若被手滑放进仓库，等于泄露本机状态）。

## 🟡 7. 许可 / 名字（清单化，无需动作）

- `LICENSE` = MIT © 2026 ChenC；`ASSET_LICENSE.md` = 原创（AI 辅助的程序化矢量生成），
  明确不含 dsh-dafeiyu / dsh-pet 的帧、图标、光标 —— 与 `docs/PRIOR-ART.md` 一致。
- 非官方免责声明在 `README_EN.md`（不与 DeepSeek / Hermes / Google / 小米及同类桌宠关联）。
- 包名可用性：PyPI `xiaocc` 空、npm `xiaocc` 空、GitHub 仓库路径空（`xiaocc` 用户名 2014 年已被占，仓库挂自己账号下）。
- 参考项目的提醒：它的角色帧**不属于 MIT**（`legacy/dafeiyu/` 原文排除），我们一帧都没用 —— 发布时别再引用。

## 人工项（脚本查不了，只能人定）

1. 用哪个 GitHub 账号、仓库叫什么、什么时机公开（@user）。
2. **发布身份**（`user.name` / `user.email`）：决定第 1 条怎么重写，也是以后所有提交的作者。
3. 首次 push 前最后跑一次 `release_check.py` + `pytest` + `xiaocc probe`（rc=0）。

## 脚本规格（交 @coder 走 codex 落成 `scripts/release_check.py`，一文件一次 exec）

约定：**零第三方依赖**（CI 无 GUI、无 pyobjc、无 PIL）；纯标准库 + `git` 子进程；
退出码 `0` 全过 / `1` 有 🔴 项 / `2` 用法错；`--json` 给 CI 用；每条检查打印「文件:行:片段」。

| # | 检查 | 实现要点 |
| --- | --- | --- |
| 1 | 历史身份 | `git log --format=%ae%ce%an%cn` 去重；命中 `/Users/`、`@<hostname>.local`、`\d{5,}@` 即 🔴 |
| 2 | 绝对路径/用户名 | 对 `git ls-files` 逐个 `re.search(r'/(Users|home)/[A-Za-z0-9._-]+')`，跳过文档串白名单（`sources/hermes.py` 的注释） |
| 3 | 密钥 | `sk-[A-Za-z0-9]{12,}`、`Bearer\s+\S{12,}`、`BEGIN [A-Z ]*PRIVATE KEY`、`(?i)(api[_-]?key\|secret\|password\|token)\s*[:=]\s*['\"]` |
| 4 | 未跟踪的敏感文件 | `git ls-files --others --exclude-standard` 里出现 `*.desktop.png`、`*.db`、`*.log`、`anchor.json` 即 🟡（说明 gitignore 有洞） |
| 5 | 空白 PNG | 见第 2 条原理：解析 IHDR/IDAT，`zlib.decompress` 后 `not any(data)` ⇒ 空图 🔴（**这条正是这次抓到 9/17 的原因，必须进脚本**） |
| 6 | 图片尺寸≈屏幕 | 尺寸等于 `1512×982`（或其 2x）的 PNG 视为「可能连桌面一起截了」 🟡 |
| 7 | 体积 | `git ls-files` 里 >300KB 的逐个列出 🟡 |
| 8 | 许可文件在位 | `LICENSE` 与 `ASSET_LICENSE.md` 存在且非空 🟡 |

建议同时把 `release_check.py` 接进 CI 的 `lint` 隔壁一个 job（仅 🔴 项失败）——
这次第 5 条就是「CI 没看像素」漏掉的。
