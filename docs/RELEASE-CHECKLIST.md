# 发布前 checklist

核查人：@researcher，2026-09-29。范围：**git 跟踪的文件**（100 个）+ 提交历史 + 工作区里
可能被 `git add -A` 带进去的未跟踪文件。每条都写了「怎么查」和「现状」，**现状是实测结果**。
自动可跑的部分见文末规格。已落地的是**三条窄判据**（`python3 scripts/release_check.py`，rc=0/1/2）：
措辞禁用词、声明 ↔ 留档路径存在、文档点名的门禁脚本仍在位 —— **文末表里 #1–#9 那九条尚未实现**，
别把它们当已经跑过。

现状按严重度排：🔴 必须清 / 🟡 建议清 / ✅ 已干净。

## 决策与进度（2026-09-29 晚更新）

- 第 1 条（历史身份）：@ops 决定**留到发布日做批处理**，理由是发布身份未定 + 重写要挑停手窗口。
  我先前写的「先止血」那段**站不住，更正如下**（别按错的理由去排期）：
  1. 重写用的是 `git rebase -r --root`，**覆盖全部提交** —— 所以「新增泄漏」并不会让重写变贵
     （50 个和 60 个提交都是几秒），设不设 local 身份**不影响重写成本**。设它的唯一作用：
     万一提前 push，少漏一点本机身份。**不是必须动作。**
  2. 真正要守的是一条铁律：**重写之前不许 push**。一旦 push，改历史就要 force-push，
     所有 clone 全废；本地没 push 时这个操作零成本。所以顺序是「重写 → 立刻 push」，中间不夹别的事。
     这条可以自动检查：`git remote -v` 为空 ⇒ 还安全；一旦有 remote 且已有推送 ⇒ 重写不再是零成本。
  3. **@ops 说的「挑停手窗口」可以缩小**：本地重写几秒完成，不需要一整天，只需要「重写那一刻
     没有别的进程在往这个仓库写提交」。要保的是**顺序**，不是时长。
  4. 现况提醒：本机身份提交数在长 —— 我今天早上扫是 **30/45**，刚才（写这份清单时）已是 **34/50**，
     其中包含我自己那次提交（`e1845ee` 的作者就是 `C Chen <a29285@MacBook-Pro.local>`）。
     每多一个只是让发布日的重写集变大，不影响成本，但**push 之前必须收敛**。
- 第 4 条（桌面壁纸）：**无动作**，保持 `.gitignore` 那行即可。
- 第 6 条：运行时产物已由 @ops `.gitignore` 掉（`728b297`）；`ops/baseline/*.bak` 暂留仓库（keepawake 的回滚凭据），
  发布日随批处理移出，并同步改 `ops/README.md` §4 的回滚路径。
- 第 2 条（空图）：@coder 已派 codex 做护栏（屏睡拒跑 rc=2、非透明像素 <2% 判空图、`opaque_pct` 进 evidence.json）。
  注意副作用是**有意的**：屏睡时截图套件会从「假通过」变成「红」。真像素要等屏亮重抓，和 @ops 的补验任务同拍。
- 第 3 条：`docs/evidence/evidence.json` 已清；plist 的模板化仍在 @ops/@coder 之间待定归属。

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

## 🟡 8. 固定临时名 / 原子写（@ops 的扫描结论需要修正一处）

@ops 报「`src/ops/scripts` 里剩下的两处写盘都是 `tempfile.mkstemp`，**没有别的固定名**」——
逐行扫下来**还剩一处固定名**：

- `src/xiaocc/sources/file.py:126`：`FileSource.touch()` 用 `self.path.with_suffix(suffix + ".tmp")`
  写临时文件再 `replace`，**名字是固定的 `<状态文件>.tmp`**。这正是面板刚踩的那一类（两个写者抢同一个临时名 →
  其中一个 `replace` 扑空 ENOENT）。
- 危害场景：file 源的整个用途就是**让别人来写那个状态文件**。生产者若也按常见约定写 `status.json.tmp` 再改名，
  就会和 `touch()` 抢同一个临时文件。影响面窄（`touch()` 目前只在 `tests/test_sources.py` 5 处被调用，
  是给测试/脚本用的辅助方法），但它会随公开仓库成为「原子写」的示例代码，建议一并换成 `mkstemp`
  （和 `anchor_store.py:118`、`appkit.py:481` 同款，3 行）。

另外三处**非原子写**（不是 bug，今天也没有并发写者，仅记录；改成原子写是三行的事）：
`scripts/appkit_screenshots.py:345` 写 `docs/evidence/evidence.json`（**这是入库的产物**，被中断会留下半截 JSON）、
`ops/xiaocc_watchdog.sh:90/133` 写停用留痕、`ops/xiaoccctl:238` 写 `state.json`。

## 人工项（脚本查不了，只能人定）

0. **发布范围：内部工作件要不要随仓库公开？**（2026-09-30 提出，等 @user 点头）
   已知两块：`ops/findings-2026-09-29.md`（会话里的运维发现记录）、`docs/RELEASE-CHECKLIST.md`
   与 [RELEASE-CHECKLIST 自身] 这类「给作者自己看的检查表」。**建议：不发布** —— 它们既不是
   用户文档也不是代码，公开出去只会让读者困惑「这是给谁看的」；但**先不删**（删除要 @user 拍板，
   按既有习惯先摆方案），落地方式两选：移出仓库 / 公开分支出剔除。
   这与「文档可达性」判据的关系：这几份**不进**可达性白名单，所以它们零引用的状态是**已知且允许**的，
   别让判据把"内部件"和"孤儿件"混成一个红。
   **当前基线（2026-09-30 实测）**：从 `README.md` / `README_EN.md` 出发，`docs/**` 全部可达，
   **唯一不可达的 md 就是 `ops/findings-2026-09-29.md`** —— 所以这条判据落地时应当**只有这一条豁免**；
   哪天豁免多出第二条，就是新孤儿，别顺手加白名单。

1. 用哪个 GitHub 账号、仓库叫什么、什么时机公开（@user）。
2. **发布身份**（`user.name` / `user.email`）：决定第 1 条怎么重写，也是以后所有提交的作者。
3. 首次 push 前最后跑一次 `release_check.py` + `pytest` + `xiaocc probe`（rc=0）。
   注意分工：`release_check.py` 只回答**「文档声明 ↔ 仓库事实」**（措辞/留档路径/门禁脚本在位），
   **不回答任何门禁是不是绿的** —— 那要看本机日志，而日志按第 6 条不进仓库 ⇒ 公开文档只能声明
   「有哪些门禁、各守什么判据」，不许声称「已经绿了」（否则声明又落在聊天记录上）。

## 脚本规格（#1–#9 **已落地**，`release_check.py` 现在 rc=1）

**当前状态（2026-09-30 凌晨实测）**：`python3 scripts/release_check.py` 报 **OK 179 / WARN 13 / FAIL 20 / rc=1**——
20 条 FAIL **全部**是下面 §1 与 §3 那两件已知事，不是新问题：

- **§1 历史身份 1 条**：`a29285@MacBook-Pro.local`（命中「`@<主机名>.local`」与「5 位以上纯数字邮箱名」）。
  修法是历史重写，而重写要先定**发布身份**（本文件 §2）⇒ **等 @user 拍板，别自己动 `filter-repo`**。
- **§3 硬编码绝对路径 19 条 / 8 个文件**：`/Users/a29285` 落在 `ops/*.plist`(4)、`ops/baseline/*.bak`(3)、
  `ops/keepawake.plist`。修法是 plist 模板化（安装时替换），不改运行时行为。
- #3 密钥 0 条（4 行豁免：抄规则的文档行与测试里的假凭据，命中一律打印）；
  **#5 空白 PNG 0 条**（51 张全有非透明像素，含刚补拍的 `docs/evidence/06-state-idle.png`）；#4/#6/#8/#9 全 OK。
- #7 有 **13 条 WARN**（`docs/design/*.png` 311KB~1.2MB，🟡 不进退出码）——里面 `设置页-实拍.png` 已是退役图，
  **暂不删**（删除按老规矩等 @user）；要腾体积就在同一批里一起做。

⇒ **读法**：这份 rc=1 是"待办的发布硬伤"，不是"仓库坏了"；上面两件落地后应自动回到 rc=0。

## 脚本规格（#1–#9 的实现要点）

**已落地**：`scripts/release_check.py` —— ① 来源声明禁用措辞（自绘/纯手绘/手工绘制）；② **只查
「声明 ↔ 留档」那条链**（`ASSET_LICENSE.md` + `docs/design/*.md` 声明行上的路径 token 与 Markdown
链接目标必须存在）；③ 文档逐字点名的 13 支门禁脚本必须在 `scripts/` 在位；④ **`docs/` 下的文件必须从入口可达**
（入口 = `README.md` / `README_EN.md`，路径 = md 之间的相对链接；md 要能走到、非 md 要在可达 md 里被点名。
口径刻意收窄：不认「被任意文件引用一次」（源码注释会救活孤岛），也不认「被某份 md 引用」（A↔B 互引会一起绿）；
候选只取 `git ls-files` 里**会被发布**的文件，`.DS_Store` 与 `docs/evidence/*.desktop.png` 这类不进判据）。
**判别力做过反例自证**
（放回「自绘」⇒ rc=1；把留档路径改成不存在的 ⇒ rc=1；改名一支门禁脚本 ⇒ rc=1）。
② 的已知边界：它判「点名的路径存在」，`docs/design/` 这种真目录不会被判红 ⇒ 抓不住「目录在、
承诺的产物不在」，要堵得再加一条"声明提到某类产物就必须在该目录里出现"。
④ 落地当天就抓出三处：设计清单与取证 README 里 `方向A-星屑小灵.svg / .png`、`06~09-state-*.png`
这类**缩写写法**（缩写在判据下就等于没点名）—— 表是它的白名单来源，表偷懒它就当场报，这正是设计意图。

**尚未实现（下面这张表的 #1–#9）**：

约定：**零第三方依赖**（CI 无 GUI、无 pyobjc、无 PIL）；纯标准库 + `git` 子进程；
退出码 `0` 全过 / `1` 有 🔴 项 / `2` 用法错；`--json` 给 CI 用；每条检查打印「文件:行:片段」。

| # | 检查 | 实现要点 |
| --- | --- | --- |
| 1 | 历史身份 | `git log --format=%ae%ce%an%cn` 去重；命中 `/Users/`、`@<hostname>.local`、`\d{5,}@` 即 🔴 |
| 2 | 绝对路径/用户名 | 对 `git ls-files` 逐个 `re.search(r'/(Users|home)/[A-Za-z0-9._-]+')`，跳过文档串白名单（`sources/hermes.py` 的注释） |
| 3 | 密钥 | `sk-[A-Za-z0-9]{12,}`、`Bearer\s+\S{12,}`、`BEGIN [A-Z ]*PRIVATE KEY`、`(?i)(api[_-]?key\|secret\|password\|token)\s*[:=]\s*['\"]` |
| 4 | 未跟踪的敏感文件 | `git ls-files --others --exclude-standard` 里出现 `*.desktop.png`、`*.db`、`*.log`、`anchor.json` 即 🟡（说明 gitignore 有洞） |
| 5 | 空白 PNG | 见第 2 条原理：解析 IHDR/IDAT，`zlib.decompress` 后 `not any(data)` ⇒ 空图 🔴。**这条正是抓到 9 张空图的原因，必须进脚本** —— 注：那起事故本身已修（2026-09-30 屏醒时重拍，非透明 0% → 20% 左右，`appkit_screenshots.py` 的「不是空图」断言 29/29 通过），但**判据仍未落** |
| 6 | 图片尺寸≈屏幕 | 尺寸等于 `1512×982`（或其 2x）的 PNG 视为「可能连桌面一起截了」 🟡 |
| 7 | 体积 | `git ls-files` 里 >300KB 的逐个列出 🟡 |
| 8 | 许可文件在位 | `LICENSE` 与 `ASSET_LICENSE.md` 存在且非空 🟡 |
| 9 | **重写前置条件** | `git remote -v` 为空 ⇒ 历史重写仍是零成本（从未 push）；一旦有 remote 且 `git rev-parse --verify origin/HEAD` 成功 ⇒ 重写要 force-push，报警 🔴 |

建议同时把 `release_check.py` 接进 CI 的 `lint` 隔壁一个 job（仅 🔴 项失败）——
这次第 5 条就是「CI 没看像素」漏掉的。
