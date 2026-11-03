#!/usr/bin/env python3
"""发布协同工具：主模块 + 嵌套模块同 train 按序打 tag（matrix-tools#6，承接 bootstrap#2）。

单仓发版一条命令（--changelog 自动补版本节 → PR → 等合并 → 检查 → tag → Release）：
  python3 scripts/release_train.py pkg main=v0.16.0 --apply --changelog "新增 xxx（#N）"
嵌套同 train（嵌套小节自动写入根与子 CHANGELOG，父模块地板须已就位）：
  python3 scripts/release_train.py bootstrap main=v0.6.4 nacos=v0.3.4 obs=v0.2.9 --apply --changelog "..."
缺省 dry-run 只跑检查；--changelog 缺小节时 dry-run 只提示不落 PR。

--issue（可多次，#153）：把关联问题单写进版本节（「关联问题单：<repo>#<N>。」）。
**为什么要它**：回执的「变更内容」按 issue 号在**版本节**里定位本条修复；
版本节不点名 issue ⇒ 回执只能提示「该版本节未提到本 issue ⇒ 下方是该版本的
全部变更」。issue 号写在哪必须统一（回执读 CHANGELOG，Release 正文的
「合入记录」行它读不到）：
  python3 scripts/release_train.py matrix-tools main=v0.1.30 --apply \
      --changelog "修 xxx" --issue 153

检查面（全过才打 tag，任一失败原子中止）：
  a. ls-remote 同名 tag 查重（重推 = 代理/sumdb 集体中毒，.github#5）
  b. go mod tidy -diff 干净（地板指向已删 tag 时直接红）
  c. go build（GOWORK=off，外部用户视角）
  d. CHANGELOG 含本 tag（版本号必须来自即将发布的内容）
  e. gofmt + golangci-lint（本地可用时；与 org CI 同面——发完才发现红的事不再有）
  f. **依赖可解析**：被 tag 模块的每条 github.com/oekit/* require 必须**已发布**
     或**本 train 一起发版**——嵌套模块的 `replace ... => ../` 只对本仓自身构建
     有效（Go 忽略依赖里的 replace）⇒ 父仓版本没发布就发嵌套模块，下游消费方
     直接解析失败（matrix-tools#148）
  0. train 前自动 fetch + ff 拉齐 main（tag 永远打在远端 main 最新基线上，
     不再依赖"记得先 pull"——并行会话推过 main 也不落空）
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
sys.path.insert(0, str(__file__).rsplit("/", 1)[0])
from matrix import (MODULES, ORG, RemoteReadError, git_branch, ensure_clean_all,  # noqa: E402
                     remote_tag_exists, repo_dir, sh)
from inflight import read as inflight_read  # noqa: E402

TODAY = time.strftime("%Y-%m-%d")


def preflight_gate(repo_root, apply: bool) -> None:
    """发版现场门禁：工作树干净；main 与远端一致（落后自动 ff 拉齐、分叉即拒）；
    --apply 还要求 main 分支 + 未被并行会话登记占用。"""
    ensure_clean_all([repo_root], "release_train")
    if git_branch(repo_root) != "main":
        raise SystemExit(f"{repo_root.name}: 须在 main 上（当前 {git_branch(repo_root)}）")
    sh(["git", "fetch", "origin", "main"], repo_root)
    behind = sh(["git", "rev-list", "--count", "main..origin/main"], repo_root).stdout.strip()
    ahead = sh(["git", "rev-list", "--count", "origin/main..main"], repo_root).stdout.strip()
    if behind != "0":
        sh(["git", "pull", "--ff-only", "origin", "main"], repo_root)
        print(f"[sync] main 落后 {behind}，已 ff 拉齐")
    ahead = sh(["git", "rev-list", "--count", "origin/main..main"], repo_root).stdout.strip()
    if ahead != "0":
        raise SystemExit(f"{repo_root.name}: main 领先远端 {ahead} 个提交（未推）——先落 PR 推 main 再 train")
    if apply:
        cur = inflight_read(repo_root.name)
        if cur:
            raise SystemExit(f"{repo_root.name}: 在途登记被占用（pid {cur.get('pid')}「{cur.get('note', '')}」）"
                             f"——确认对方收尾后 inflight.py release {repo_root.name} 再发版")


def tag_exists_remote(repo: str, tag: str) -> bool:
    """远端是否已有同名 tag —— **读不到 ⇒ 抛 RemoteReadError**（#85）。

    此前是 `bool(stdout.strip())`：ls-remote 失败（SSH 掉线 / 网络抖动）时
    stdout 为空 ⇒ 返回 False ⇒ 「这个 tag 不存在」⇒ **查重门禁静默放行**。
    同一个确实存在的 tag，仅因读法不同就从「存在」翻成「不存在」（变异实测）。

    现在统一走 matrix.remote_tag_exists（canonical HTTPS → origin 兜底 →
    失败抛错）：「读到空」与「读不到」严格分开。
    """
    return remote_tag_exists(repo, tag)


def go_env():
    env = dict(os.environ)
    env["GOWORK"] = "off"
    return env


def check_repo(root, failures: list[str]) -> None:
    """仓级检查（每 train 一次）：gofmt + golangci-lint（本地可用时）。"""
    r = subprocess.run(["gofmt", "-l", "."], cwd=root, capture_output=True, text=True)
    files = [l for l in r.stdout.split() if "/vendor/" not in l and not l.startswith("vendor/")]
    if files:
        failures.append(f"gofmt 未格式化：{files[:5]}")
        return
    if not shutil.which("golangci-lint"):
        print("[check] golangci-lint 本地不可用，跳过（CI 兜底）")
        return
    r = subprocess.run(["golangci-lint", "run", "--timeout", "5m"],
                       cwd=root, capture_output=True, text=True, env=go_env())
    if r.returncode != 0:
        failures.append(f"golangci-lint 失败\n{(r.stdout + r.stderr)[-600:]}")


def _repo_root(module_dir: Path) -> Path:
    """从嵌套模块目录上溯到仓根（带 .git 的那层）。"""
    d = module_dir.resolve()
    while d != d.parent and not (d / ".git").exists():
        d = d.parent
    return d


def check_module(repo: str, name: str, module_dir, tag: str, failures: list[str]):
    """单模块前置检查（结果追加进 failures）。"""
    # a. ls-remote 同名 tag 查重（重推 = 代理/sumdb 缓存集体中毒，.github#5）
    #    读不到 ⇒ fail-closed：查重没做成就不许进打 tag 环节（#85）。
    try:
        exists = tag_exists_remote(repo, tag)
    except RemoteReadError as e:
        failures.append(f"{name}: 远端 tag 读不到，无法查重（读不到 ≠ 不存在）：{e}")
        return
    if exists:
        failures.append(f"{name}: tag {tag} 已存在于远端（重推会使已缓存消费方集体中毒）")
        return
    # b. tidy 干净（地板指向已删 tag 时 tidy 直接红）
    r = subprocess.run(["go", "mod", "tidy", "-diff"], cwd=module_dir,
                       capture_output=True, text=True, env=go_env())
    if r.returncode != 0:
        failures.append(f"{name}: go mod tidy -diff 非空\n{r.stdout[-600:]}{r.stderr[-200:]}")
        return
    # c. 构建
    r = subprocess.run(["go", "build", "./..."], cwd=module_dir,
                       capture_output=True, text=True, env=go_env())
    if r.returncode != 0:
        failures.append(f"{name}: go build 失败\n{r.stderr[-600:]}")
        return
    # d. CHANGELOG 核对（同名小节或行内出现 tag 名）
    #    嵌套模块的 tag 带**完整子路径**（Go 的子目录模块规则）：
    #    observability/tracing/instrumentation/kitexotel/v0.1.0 ⇒ 该 tag 名
    #    出现在**仓根** CHANGELOG 的版本节里（ensure_changelog 写的）。
    #    旧写法取 module_dir.parent/CHANGELOG.md（假定嵌套模块在仓根下一层）——
    #    多层嵌套时会指向一个不存在的目录 ⇒ 发版被凭空拦下。
    if name == "main":
        cl = module_dir / "CHANGELOG.md"
    else:
        cl = _repo_root(module_dir) / "CHANGELOG.md"
    if not cl.exists() or tag not in cl.read_text():
        failures.append(f"{name}: CHANGELOG.md 未包含 {tag}（版本号必须来自即将发布的内容）")


# ---------- 被 tag 模块的 oekit 依赖必须「下游可解析」（#148）----------

def parse_go_mod_requires(text: str) -> list[tuple[str, str]]:
    """从 go.mod 文本解析 require 列表（**只认 require**，不认 replace）。

    认两种写法：
      · 单行：`require github.com/oekit/pkg v0.31.1`
      · 块状：`require (` … `\tgithub.com/oekit/pkg v0.31.1 // indirect` … `)`

    ⚠️ **必须排除 replace 块**：Go 的 replace 块里也是「路径 版本/目标」的形状，
    混进来会把「被替换掉的依赖」当成真依赖 ⇒ 门禁读到的不是事实。
    """
    out: list[tuple[str, str]] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        # 去掉行尾注释（`// indirect` 等）——版本号在注释之前，不受影响
        line = line.split("//", 1)[0].strip()
        if not line:
            continue
        if in_block:
            if line.startswith(")"):
                in_block = False
                continue
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
            continue
        if not line.startswith("require"):
            continue
        rest = line[len("require"):].strip()
        if rest == "(":
            in_block = True
            continue
        if rest:
            parts = rest.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
    return out


def required_tag_form(module_path: str, version: str) -> tuple[str, str]:
    """require 的 module path → (目标仓, 目标 tag)。

    Go 的子目录模块规则：
      · 根模块     `github.com/oekit/<repo>`             ⇒ tag `<version>`
      · 子目录模块 `github.com/oekit/<repo>/<sub...>`     ⇒ tag `<sub...>/<version>`

    ⚠️ 子路径是**完整子路径**，不是最后一段：`logx/hlog` 的 tag 是 `hlog/v0.1.6`，
    `kitex/observability/tracing/instrumentation/kitexotel` 的 tag 是
    `observability/tracing/instrumentation/kitexotel/v0.7.4`。
    用「最后一段」当前缀 ⇒ 读不到 tag ⇒ 门禁把「读不到」当成「没发布」。

    ⚠️ 主版本后缀（`/v2`）不算子路径：根模块的主版本 tag 仍是 `v2.x.y`。
    """
    parts = module_path.split("/")
    if len(parts) < 3 or parts[0] != "github.com" or parts[1] != "oekit":
        raise ValueError(f"不是 oekit 模块路径：{module_path}")
    target_repo = parts[2]
    subs = parts[3:]
    if subs and re.fullmatch(r"v[2-9]\d*", subs[-1]):
        # 主版本模块：tag 就是版本本身（版本号自身已带 v2 前缀）
        return target_repo, version
    if not subs:
        return target_repo, version
    return target_repo, "/".join(subs) + "/" + version


def remote_read_failed(exc: Exception) -> bool:
    """★ **判定点**：远端 tag 读不到 ⇒ 该不该**判红**（fail closed）。

    读不到 ≠ 已发布（matrix-tools#85 口径）⇒ 默认恒判红。

    ⚠️ 这是**可注入的判定点**：`scripts/release_train_requires_selftest.py --mutate`
    会临时替换它，验证「fail open」这个最要命的失效模式**确实**被用例 6/13 抓到
    ⇒ 避免自测只「跑过」却从未验证过它能转红。
    """
    return True


def check_requires_resolvable(repo: str, name: str, module_dir, planned_tags, failures,
                               tag_exists=None) -> None:
    """被 tag 的模块，其 go.mod 里每条 `github.com/oekit/*` require 必须**下游可解析**。

    **为什么这条门禁是必需的（matrix-tools#148）**：

    嵌套模块对父仓有 `replace github.com/oekit/<父仓> => ../`（指向本地源码）
    ⇒ 本仓构建时 `go mod tidy -diff` / `go build` **全绿** ⇒ **看起来**没问题。
    但 Go **忽略依赖里的 replace 指令**（replace 只在主模块的 go.mod 生效）
    ⇒ 下游消费方解析这个嵌套模块时，会照着它的 go.mod 去模块代理取父仓的
    require 版本 ⇒ 该版本若尚未发布 ⇒ 消费方直接「unknown revision」解析失败。

    实测（2026-11-02，file:// 代理模拟，见 issue 正文）：嵌套模块 require 父仓
    v1.9.9（未发布）⇒ 消费方 `go mod tidy` 失败；把父仓 v1.0.0 发布进同一代理
    ⇒ 同样形态解析成功（对照组证明模拟本身有牙齿）。

    ⇒ 判据：每条 oekit require 的版本，要么**已在远端发布**，要么**在本次 train 内
    一起发版**；两者都不是 ⇒ 不许打这个 tag。

    ⚠️ 远端读不到 ⇒ **fail closed**（读不到 ≠ 没发布，#85 口径）。
    """
    if tag_exists is None:
        tag_exists = tag_exists_remote
    gomod = module_dir / "go.mod"
    if not gomod.exists():
        return
    try:
        text = gomod.read_text(encoding="utf-8")
    except OSError as e:  # noqa: BLE001 —— 读不到就报「判不了」，不猜
        failures.append(f"{name}: 读不到 go.mod（{e}）⇒ 依赖可解析性判不了")
        return
    for path, ver in parse_go_mod_requires(text):
        if not path.startswith("github.com/oekit/"):
            continue
        try:
            target_repo, tag = required_tag_form(path, ver)
        except ValueError as e:
            failures.append(f"{name}: {e}")
            continue
        if (target_repo, tag) in planned_tags:
            continue  # 本次 train 一起发 ⇒ 发完就有了
        try:
            ok = tag_exists(target_repo, tag)
        except RemoteReadError as e:
            if remote_read_failed(e):
                failures.append(f"{name}: 远端 tag 读不到（{e}）⇒ 无法判定 "
                                f"{target_repo}@{tag} 是否已发布 ⇒ 判不了，不许放行")
            continue
        if not ok:
            failures.append(
                f"{name}: 依赖 {path} {ver} 未发布（远端无 {target_repo}@{tag}）"
                f" ⇒ 下游消费方解析本模块时会去代理取 {target_repo}@{tag} 而取不到"
                f"（嵌套模块的 `replace ... => ../` 只对本仓自身构建有效，"
                f"Go 忽略依赖里的 replace）⇒ 请先发 {target_repo} {ver}，"
                f"或把它一起放进本次 train")


def gate_verdict(unresolved: list[str]) -> bool:
    """★ **判定点**：「本次 train 只有嵌套模块」时**是否阻断**。

    ⇒ `unresolved` 非空（确有未发布 / 判不了的 require）⇒ 阻断；
      全空（每条 require 都已发布）⇒ 放行。

    ⚠️ 这是**可注入的判定点**：`scripts/release_train_requires_selftest.py --mutate`
    会临时替换它，验证「一刀切守卫」（恒阻断）与「恒放行」两个失效方向
    **确实**分别被用例 11 / 12 / 13 抓到 ⇒ 避免自测只「跑过」却从未验证过它能转红。
    """
    return bool(unresolved)


def nested_only_gate(repo: str, specs, tag_exists=None) -> tuple[bool, str]:
    """「本次 train 只有嵌套模块、没有父仓 main=」⇒ **条件守卫**。

    ⇒ 返回 (是否放行, 提示/报错文本)。

    ★ **判据不是「有没有 main=」，而是 matrix-tools#148 那条**：
      「每条 oekit require 的版本，要么**已在远端发布**，要么**本次 train 一起发**」
      —— 它由 `check_requires_resolvable` 实现 ⇒ 本函数**复用它**，不另写一套更粗的。

    ⚠️ **为什么不能一刀切**（实测 2026-11-03）：
      一刀切守卫（「只有嵌套 ⇒ 拒绝」）会挡死「把新地板带进**已发布产物**」的唯一修法
      —— 8 个待发嵌套模块里 **7 个的 require 全部已发布**（只发嵌套**安全**），
      真正需要同 train 的只有 `timertask/redis`（require `timertask v0.2.7` 未发布）。

    ⚠️ **远端读不到 ⇒ fail closed**（读不到 ≠ 已发布，#85 口径）⇒ 判红。
    """
    if any(name == "main" for name, _d, _t in specs):
        return True, ""
    # ★ `only` 有**两个消费者**，取值必须**各取各的**（#214）：
    #   ① 文案（人读）⇒ 要「模块名=版本号」，多个用「、」分隔；
    #   ② **修法示例命令行**（人照抄）⇒ 必须是 `name=ver` 的 **spec 形态**，
    #      多个用**空格**分隔（spec 解析是 `spec.split("=", 1)` 逐个解析的）。
    # ⚠️ **旧实现有**两处**错**（实测 2026-11-03）：
    #   (a) `_t.split('/', 1)[1]` 对**多段**嵌套模块丢首段：
    #       tag `observability/tracing/instrumentation/kitexotel/v0.7.6`
    #       ⇒ `split('/', 1)[1]` = `tracing/instrumentation/kitexotel/v0.7.6`
    #       ⇒ 文案给出**不存在的模块名**（丢了 `observability/`）。
    #       ⇒ 正确取法 = `rsplit('/', 1)[-1]`（**只取版本号**，单段/多段都对）。
    #   (b) 旧实现**同一个串**同时当下标分隔（文案用「、」）和**命令行参数**用
    #       ⇒ 多模块时照抄示例命令得到 `nacos=v0.3.10、obs=v0.2.14`
    #       ⇒ **一个 token** ⇒ `ver` = `v0.3.10、obs=v0.2.14`
    #       ⇒ 被 spec 的版本号格式校验**直接拒掉**（退出码 2）⇒ 示例**不可用**。
    #       ⇒ 修法：**两个消费者各用一个串**（文案串 vs 命令行串）。
    only = "、".join(f"{name}={_t.rsplit('/', 1)[-1]}" for name, _d, _t in specs)
    only_specs = " ".join(f"{name}={_t.rsplit('/', 1)[-1]}" for name, _d, _t in specs)
    planned = {(repo, tag) for _n, _d, tag in specs}
    unresolved: list[str] = []
    for name, d, tag in specs:
        check_requires_resolvable(repo, name, d, planned, unresolved, tag_exists=tag_exists)
    if gate_verdict(unresolved):
        return False, (
            f"{repo}: 本次 train 只有嵌套模块（{only}），且有**未发布的 require** ⇒ 不许发。\n"
            f"  ⇒ 判据（matrix-tools#148）：每条 oekit require 要么**已发布**，"
            f"要么**本次 train 一起发**。\n"
            f"  ⇒ 未满足的 require：\n"
            + "".join(f"      - {u}\n" for u in unresolved)
            + f"  ⇒ 修法：把上面列出的仓一起放进本次 train（"
            f"例：python3 scripts/release_train.py {repo} main=vX.Y.Z {only_specs} "
            f"--apply --changelog \"...\"）\n"
            f"  ⇒ 若这些 require 其实**已发布**，那是远端读 tag 失败 ⇒ "
            f"**fail closed**（读不到 ≠ 已发布）⇒ 查网络/权限后重试。"
        )
    return True, (
        f"[note] 本次 train 只有嵌套模块（{only}），没有父模块 main=。\n"
        f"       ✔ 已核：这些模块的每条 oekit require **都已发布** ⇒ 只发嵌套**安全**"
        f"（判据同 matrix-tools#148）。\n"
        f"       ⚠️ 两个副作用（已就地修掉，不会漏出去）：版本节文案里的父模块版本号"
        f"为空 ⇒ 不再写成「跟随 {repo}  发版」；分支名不再退化成 `chore/rel-`。"
    )

# ---------- --changelog：版本节自动生成 + PR + 等合并 ----------

def wanted_sections(root, specs) -> list[tuple[Path, str]]:
    """每个 spec 需要的 (CHANGELOG 文件, 标题行前缀)。嵌套 spec 需根 + 子双份。"""
    out = []
    main_tag = next((t for n, _d, t in specs if n == "main"), None)
    for name, d, tag in specs:
        if name == "main":
            out.append((root / "CHANGELOG.md", f"## [{tag}]"))
        else:
            out.append((root / "CHANGELOG.md", f"## [{tag}]"))
            ver = tag.rsplit("/", 1)[-1]
            out.append((d / "CHANGELOG.md", f"## [{ver}]"))
    return out


def insert_top(path: Path, heading: str, bullet: str) -> bool:
    """版本节插到文件最顶（严格递减门禁要求新版本在最上）；已存在返回 False。

    ⚠️ **文件不存在 ⇒ 跳过，不创建**（实测 logx 仓形态）：
       logx 的四个嵌套模块（hlog / kitexlog / sink / hertzlog）**都没有**独立的
       `CHANGELOG.md` —— 嵌套版本节**只记在根 CHANGELOG**（形态 `## [hlog/v0.1.6]`）。
       旧实现直接 `path.read_text()` ⇒ 对不存在的路径抛
       `FileNotFoundError` ⇒ **logx 这种仓根本发不了版**（版本节 PR 落不下去）。
       ⇒ 现在：文件不存在 ⇒ 返回 False（跳过该面），**不新建空文件**
       （新建会造出「嵌套模块有独立 CHANGELOG」的假形态 ⇒ 与 logx 既有惯例不一致）。
    """
    if not path.exists():
        return False
    t = path.read_text()
    if any(l.startswith(heading) for l in t.splitlines()):
        return False
    block = f"{heading} — {TODAY}\n\n- {bullet}\n\n"
    m = re.search(r"(?m)^## \[", t)
    t2 = (t[:m.start()] + block + t[m.start():]) if m else t.rstrip() + "\n\n" + block
    path.write_text(t2)
    return True


def find_changelog_check() -> Path | None:
    """定位 org 共享的 changelog_check.py（找不到返回 None）。

    候选（按序）：
      1. 环境变量 ``CHANGELOG_CHECK`` 显式指定的路径；
      2. ``<matrix-tools 上级>/.github/scripts/changelog_check.py`` —— 矩阵工作区
         布局（org 的共享脚本仓叫 **.github**）；
      3. ``<matrix-tools>/scripts/changelog_check.py`` —— 本仓自带副本（若曾 vendored）。

    **为什么换了路径**（#113）：旧实现写的是
    ``<上级>/profile/scripts/changelog_check.py`` —— "profile" 是**很久以前的
    org 名**（矩阵改名后共享脚本仓是 .github）⇒ 该路径**恒不存在** ⇒
    ``if not chk.exists(): return`` ⇒ **预校验被静默跳过** ⇒ 发版本地放行、
    CI 才拦（本地预校验形同虚设）。
    """
    env = os.environ.get("CHANGELOG_CHECK")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    here = Path(__file__).resolve().parent.parent  # matrix-tools/
    for cand in (here.parent / ".github" / "scripts" / "changelog_check.py",
                 here / "scripts" / "changelog_check.py"):
        if cand.exists():
            return cand
    return None


def validate_changelog(path: Path) -> None:
    """本地跑 org 的 changelog_check.py（**缺失时明确告警**，不静默跳过）。

    必须在 CHANGELOG **所属仓的目录**里跑：changelog_check 用
    `git describe --exact-match --tags HEAD` 判断「HEAD 处于 tag」——在别的仓
    里跑会拿到**那个仓**的 tag。实测踩过：从 matrix-tools 目录校验 data 的
    CHANGELOG 时，matrix-tools 的 HEAD 恰好有 tag v0.1.1，于是误报
    「data 的 HEAD 处于 tag v0.1.1」，发版被凭空拦下。
    """
    chk = find_changelog_check()
    if chk is None:
        # **不静默跳过**（#113）：找不到就明确说出来，让「本地没拦」这件事可见。
        print("[changelog] 警告：未找到 org 的 changelog_check.py ⇒ 本地预校验跳过。"
              "CI 的 changelog 检查项仍会兜底，但本地放行、CI 才拦的老毛病依旧。"
              "（可用环境变量 CHANGELOG_CHECK 指向脚本路径）")
        return
    repo_root = path.parent
    while repo_root != repo_root.parent and not (repo_root / ".git").exists():
        repo_root = repo_root.parent
    r = subprocess.run([sys.executable, str(chk), str(path)],
                       capture_output=True, text=True, cwd=str(repo_root))
    if r.returncode != 0:
        raise SystemExit(f"CHANGELOG 门禁预校验失败（{path.name}）：\n{(r.stdout + r.stderr)[-400:]}")


def wait_pr_merged(repo: str, number: int, timeout: int = 900) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = sh(["gh", "pr", "view", str(number), "--repo", f"{ORG}/{repo}",
                 "--json", "state", "--jq", ".state"], None, check=False).stdout.strip()
        if st == "MERGED":
            print(f"[changelog] PR #{number} 已合并")
            return
        if st == "CLOSED":
            raise SystemExit(f"{repo}: CHANGELOG PR #{number} 被关闭，中止")
        time.sleep(15)
    raise SystemExit(f"{repo}: 等待 CHANGELOG PR #{number} 合并超时（CI 可能仍在跑）——稍后重跑 train")


def format_bullets(bullets: list[str], nested_self: bool, repo: str, main_tag: str,
                   issues: list[str] | None = None) -> str:
    """把 --changelog 的多条 bullet 拼成版本节正文（**不丢条目**，#114）。

    ``--changelog`` 可多次给出，每多一条就是一条 bullet ⇒ 版本节应当把它们
    **全部**写进去。旧实现只取 ``bullets[0]``，其余**静默丢弃** ⇒ 发版说明
    缺条目（且没有任何提示）。

    拼接形态：``"\\n- ".join(bullets)`` —— 因为版本节模板已经带了首个 ``- ``，
    拼出来的结果是::

        - 第一条
        - 第二条

    ``issues``（#153）：关联问题单。版本节里**点名 issue** 是回执能定位到
    「本条修复」的**前提**——回执读的是 CHANGELOG 版本节（Release 正文的
    「合入记录」行它读不到）。不点名 ⇒ 回执只能提示「该版本节未提到本 issue」。

    ⚠️ **main_tag 为空的措辞**（只发嵌套模块时）：旧实现无条件写
    ``f"跟随 {repo} {main_tag} 发版"`` ⇒ main_tag 为空时产出
    ``"跟随 registry  发版"``（**双空格**）⇒ 一句没人看得懂的文案进了版本节。
    ⇒ 现在空值时**不编造父模块版本号**，改说「随本次 train 的父模块一并发布」。
    """
    if nested_self or not bullets:
        if main_tag:
            body = f"跟随 {repo} {main_tag} 发版（release_train 嵌套 train）"
        else:
            # ★ 父模块本次不在 train 里 ⇒ 不写一个空版本号（否则出双空格文案）
            # ★★ 且**不得声称「父模块一并发布」**（#208）：只发嵌套时父仓本次
            #    并不发版 ⇒ 这句话进了**已发布产物**（Release 正文 + CHANGELOG）
            #    就是一条**假陈述** ⇒ 改为如实说明「父模块本次不发版」。
            body = "随本次 train 一并发布（只发嵌套模块，父模块本次不发版）"
        if issues:
            # ★ issue 号必须落在这条 **bullet** 里（#208）：回执（close_issue）读的是
            #   **嵌套 CHANGELOG** 的版本节（嵌套 tag 优先读 <sub>/CHANGELOG.md）⇒
            #   issue 号只写在下面的「关联问题单」行 ⇒ 定位器 entries=0
            #   ⇒ 回执降级成「提到本 issue 但没绑到具体条目」。
            body += "（" + "、".join(issues) + "）"
    else:
        body = "\n- ".join(bullets)
    if issues:
        body += "\n\n关联问题单：" + "、".join(issues) + "。"
    return body


def unbound_issues(section: str, issues: list[str] | None) -> list[str]:
    """版本节里**没有被绑到具体条目**的 issue 引用（#162）。

    回执的「变更内容」按 issue 号在**版本节**里定位本条修复
    （`gen_releases.locate_issue_entries`）⇒ 只有**绑到条目**（某条 bullet 及其续行
    里提到该号）才定位得到；只在叙述文字里提到（例如只出现在「关联问题单」行）
    ⇒ 回执只能提示「未绑定到具体条目」⇒ 读者读不到「这条到底改了什么」。

    ⇒ 发版时用它判「这条 issue 的回执会不会讲成别的修复」：**判不出 ⇒ 不许发版**
    （fail closed），而不是照发照关单。

    ⚠️ 判据用**回执同款**定位器（不另写一套）——两边的判定口径分裂过一次
    （#153 / #154 / #158 这一族都是同一个根：issue 号写在哪、两边读哪里的分裂）。
    """
    if not issues:
        return []
    from gen_releases import locate_issue_entries
    out: list[str] = []
    for ref in issues:
        m = re.fullmatch(r"([a-z0-9.-]+)#(\d+)", ref)
        if not m:
            out.append(ref)                # 形态不认识 ⇒ 判为未绑定（fail closed）
            continue
        r, n = m.group(1), m.group(2)
        try:
            loc = locate_issue_entries(section, r, n)
        except Exception:
            out.append(ref)                # 判不了 ⇒ 不许放行（fail closed）
            continue
        if not loc.entries:
            out.append(ref)
    return out


def issue_binding_msg(repo: str, tag: str, unbound: list[str], reason: str) -> str:
    """--issue 的引用在目标版本节里绑不到条目时的**可执行**提示（#162）。"""
    nums = [r.split("#", 1)[1] for r in unbound if "#" in r]
    sample = ("（#%s）" % nums[0]) if nums else "（#<N>）"
    return (
        f"[changelog] ❌ {repo} {tag} 的版本节**没有把 {'、'.join(unbound)} 绑到具体条目**"
        f"（{reason}）\n"
        f"    ⇒ 这些 issue 的回执会讲成「该版本的全部变更」或别的修复"
        f"（读者读不到本条）。\n"
        f"    ⇒ 修法：把 issue 号写进**对应的 bullet**（如「……{sample}」）——\n"
        f"      回执按号在版本节里**逐条目**定位，只在「关联问题单」行里点名**不算**。\n"
        f"    ⇒ 若该版本**已经发布**：按**发布事实**把条目补记进那个版本节\n"
        f"      （`git tag --contains <commit>` 可证修复确实在那个 tag 内；\n"
        f"      **已发布的 tag 快照本身不动**——本仓先例：PR #151 写回 v0.1.21）。"
    )


def check_new_section_binding(bullets: list[str], repo: str, main_tag: str,
                              issues: list[str] | None) -> None:
    """建节路径：--issue 绑不到条目 ⇒ 中止发版（#162）。

    ⚠️ 送检的正文要带上 "- " 前缀（insert_top 写进文件时就是 `- {bullet}`）——
    少了前缀，第一条就不是 bullet ⇒ 定位器看不到条目 ⇒ 误判成「没绑到」。
    """
    if not issues:
        return
    # ★ **逐面校验**（#208）：只发嵌套模块时，回执（close_issue）读的是
    #   **嵌套 CHANGELOG** 的版本节（`gen_releases.changelog_section`：嵌套 tag
    #   优先读 `<sub>/CHANGELOG.md`，文件不存在才回退根面）⇒ **只验根面** = 给
    #   「嵌套面没绑上」开了口子 ⇒ fail-closed 门禁给出的是**假保证**。
    for face, nested_self in (("根面", False), ("嵌套面", True)):
        body = "- " + format_bullets(bullets, nested_self, repo, main_tag, issues)
        bad = unbound_issues(body, issues)
        if bad:
            raise SystemExit(issue_binding_msg(
                repo, main_tag or "（本次 train 的嵌套模块）", bad,
                f"新建的{face}版本节里 issue 号只出现在「关联问题单」行、没绑到具体条目"))


def ensure_changelog(repo: str, root, specs, bullets: list[str], apply: bool,
                     issues: list[str] | None = None) -> None:
    """缺版本节时：生成（置顶）→ 预校验 → PR → 排程合并 → 等合并 → 拉齐。
    dry-run 只报告不落 PR。bullets 来自 --changelog（可多次，**全部写入**）。
    issues 来自 --issue（#153）：关联问题单写进版本节 ⇒ 回执定位得到本条修复。

    #162：**--issue 的引用绑不到条目 ⇒ 不许发版**（fail closed）。
    旧实现只打印一行提示就继续走 ⇒ 版本节已存在时（同一版里修了两条 issue、
    或版本节被别的 PR 先建出来）引用被**静默丢弃** ⇒ 回执定位不到本条修复。
    """
    wanted = wanted_sections(root, specs)
    todo = []
    for path, heading in wanted:
        t = path.read_text() if path.exists() else ""
        if not any(l.startswith(heading) for l in t.splitlines()):
            todo.append((path, heading))
    if not todo:
        if issues:
            from gen_releases import changelog_section
            # ★ 逐个 spec 查（#208）：嵌套 tag 的回执读的是**嵌套 CHANGELOG** 的
            #   版本节（`gen_releases.changelog_section`：先 <sub>/CHANGELOG.md，
            #   文件不存在才回退根面）⇒ 必须按**回执同款**读法逐面查。
            #   旧实现只查 `main_tag` 那一份 ⇒ 「只发嵌套」时 main_tag 为空 ⇒
            #   查的是**空面** ⇒ 永远判成「没绑上」⇒ 误杀重跑。
            bad_specs = []
            for name, _d, tag in specs:
                nested = None if name == "main" else name
                version = tag.rsplit("/", 1)[-1]
                sec = changelog_section(str(root), version, nested) or ""
                bad = unbound_issues(sec, issues)
                if bad:
                    bad_specs.append((tag, bad))
            if bad_specs:
                tag0, bad0 = bad_specs[0]
                raise SystemExit(issue_binding_msg(
                    repo, tag0, bad0,
                    "版本节已存在 ⇒ --issue 的关联问题单写不进去"))
            print(f"[changelog] {repo} 的版本节已存在（"
                  f"{'、'.join(t for _n, _d, t in specs)}），且 "
                  f"{'、'.join(issues)} 都能定位到条目 ⇒ 放行")
        return
    main_tag = next((t for n, _d, t in specs if n == "main"), "")

    # 建节路径也要判：**新写的版本节**必须能定位到这些 issue，否则回执讲成别的修复。
    # ⚠️ **dry-run 也要判**：预览时就该看见这个问题（否则「dry-run 绿、apply 才炸」
    # ⇒ 又变成一种静默）。只影响显式带 --issue 的发版（不带 --issue ⇒ issues 为空
    # ⇒ 本检查不触发 ⇒ 既有批量发版脚本不受影响）。
    check_new_section_binding(bullets, repo, main_tag, issues)
    if not apply:
        for path, heading in todo:
            print(f"[changelog] dry-run：{path.name} 缺 {heading}（--apply 时自动生成并落 PR）")
        return

    # 写入必须发生在 land_pr 的分支上（edit 回调）——先写后落 PR 会被干净树守卫拒绝
    def edit():
        changed: set[Path] = set()
        for path, heading in todo:
            nested_self = path != root / "CHANGELOG.md"
            bullet = format_bullets(bullets, nested_self, repo, main_tag, issues)
            if insert_top(path, heading, bullet):
                changed.add(path)
        for path in sorted(changed):
            validate_changelog(path)
        return [str(p.relative_to(root)) for p in sorted(changed)]

    from prkit import land_pr, merge_pr, open_pr
    # ★ 分支名**不能退化成 `chore/rel-`**（没有版本号 ⇒ 分支名无法区分批次）。
    #   只发嵌套模块时 main_tag 为空 ⇒ 用**本次 train 的 tag** 兜底（多模块则取全部）。
    if main_tag:
        rel_slug = main_tag
        rel_title_tag = main_tag
    else:
        rel_slug = "+".join(t for _n, _d, t in specs)
        rel_title_tag = rel_slug
    url = land_pr(root, repo, f"chore/rel-{rel_slug}",
                  f"chore: CHANGELOG 补 {rel_title_tag}（release_train --changelog）",
                  f"chore: CHANGELOG 补 {rel_title_tag}",
                  "release_train 自动生成（版本节置顶，过 changelog_check 预校验）。",
                  edit=edit, auto_merge=True)
    print(f"[changelog] PR: {url or '无变更'}")
    if not url:
        return
    m = re.search(r"/pull/(\d+)", url)
    if m:
        pr = open_pr(repo, f"chore/rel-{rel_slug}")
        if pr:  # 尚未自动合并则等待（auto-merge 等 CI）
            wait_pr_merged(repo, int(m.group(1)))
    sh(["git", "pull", "--ff-only", "origin", "main"], root)
    preflight_gate(root, apply=True)


def main():
    argv = sys.argv[1:]
    apply = "--apply" in argv
    bullets: list[str] = []
    issue_refs: list[tuple[str | None, str]] = []   # 关联问题单（#153）
    args: list[str] = []       # 位置参数（--changelog 的值不属于此处）
    i = 0
    while i < len(argv):
        if argv[i] == "--changelog":
            if i + 1 >= len(argv):
                print("--changelog 需要跟描述文本")
                sys.exit(2)
            bullets.append(argv[i + 1])
            i += 2
        elif argv[i] == "--issue":
            if i + 1 >= len(argv):
                print("--issue 需要跟 issue 号（如 153 或 matrix-tools#153）")
                sys.exit(2)
            val = argv[i + 1]
            m = re.fullmatch(r"(?:([a-z0-9.-]+)#)?(\d+)", val)
            if not m:
                print(f"--issue 格式应为 <N> 或 <repo>#<N>：{val!r}")
                sys.exit(2)
            issue_refs.append((m.group(1), m.group(2)))
            i += 2
        else:
            if not argv[i].startswith("--"):
                args.append(argv[i])
            i += 1
    if len(args) < 2 or any("=" not in a for a in args[1:]):
        print(__doc__)
        sys.exit(2)
    repo = args[0]
    if repo not in MODULES:
        print(f"未知模块仓 {repo}（MODULES 无此项）")
        sys.exit(2)
    # 关联问题单统一写成 `<repo>#<N>`：回执按这个形态定位版本节里的条目
    # （#153）。不带仓名前缀时按**本次发布的仓**补齐。
    issues = [f"{r or repo}#{n}" for r, n in issue_refs]

    root = repo_dir(repo)
    specs = []  # (名称, 模块目录, tag 名)，主模块在前
    for spec in args[1:]:
        name, ver = spec.split("=", 1)
        if not re.fullmatch(r"v\d+\.\d+\.\d+", ver):
            print(f"{spec}: 版本号须为 vX.Y.Z 全三段")
            sys.exit(2)
        if name == "main":
            specs.append(("main", root, ver))
        else:
            sub = root / name
            if not (sub / "go.mod").exists():
                print(f"{repo}/{name}: 子目录无 go.mod，不是嵌套模块")
                sys.exit(2)
            specs.append((name, sub, f"{name}/{ver}"))

    # ★ 只发嵌套模块（没有 main=）⇒ **条件守卫**：只在**确有未发布 require** 时拒绝。
    #
    #   ⚠️ **位置要紧**：在 preflight_gate 之前 —— 用法错误应当**先**于工作区检查报出，
    #   否则用户看到的是「须在 main 上（当前 ）」这类与真正问题无关的报错。
    #
    #   ★ **为什么不能一刀切**（实测 2026-11-03）：
    #   #195 的旧实现是**无条件**拒绝（「本次 train 只有嵌套模块 ⇒ 拒绝」），并写
    #   「若父仓本次无需发版（地板已就位），**不要**单独发嵌套模块」——
    #   **这句是反的**：地板已就位恰恰是「可以单独发嵌套模块」的**前提**
    #   （要修的正是「仓库树已抬地板、但**已发布产物**还带旧地板」那批）。
    #
    #   ⇒ **真判据不是「有没有 main=」，而是 #148 那条**：
    #     「每条 oekit require 的版本，要么**已在远端发布**，要么**本次 train 一起发**」——
    #     它**已在同一脚本里实现**（`check_requires_resolvable`）⇒ 守卫应当**复用它**，
    #     而不是另写一套更粗的（一刀切 = 误伤安全用例）。
    #   ⇒ **实测**：8 个待发嵌套模块里 **7 个的 require 全部已发布** ⇒ 只发嵌套**安全**；
    #     真正需要同 train 的只有 `timertask/redis`（require `timertask v0.2.7` 未发布）。
    #     一刀切守卫会把这 7 个安全用例全挡死 ⇒ **「把新地板带进已发布产物」的唯一修法被堵死**。
    ok_nested, nested_msg = nested_only_gate(repo, specs)
    if not ok_nested:
        raise SystemExit(nested_msg)          # 报错 ⇒ 只走这一条出口
    if nested_msg:
        print(nested_msg)                     # 放行 ⇒ 打提示

    preflight_gate(root, apply)

    print(f"release train: {repo}（{'apply' if apply else 'dry-run'}）")
    for name, d, tag in specs:
        print(f"  {tag}  <- {d}")

    if bullets:
        ensure_changelog(repo, root, specs, bullets, apply, issues)
    elif apply:
        # apply 但没给 --changelog 且缺小节 → check d 会拦；提前提示
        missing = [(p, h) for p, h in wanted_sections(root, specs)
                   if not any(l.startswith(h) for l in (p.read_text().splitlines() if p.exists() else []))]
        if missing:
            print(f"[hint] 缺 {len(missing)} 个版本节，可用 --changelog \"描述\" 自动生成")

    # 先全部检查，任一失败即中止（不产生任何 tag）
    failures: list[str] = []
    print("[check] gofmt + golangci-lint（仓级）")
    check_repo(root, failures)
    # 本次 train 会发布的 tag（**带仓名**）：同名的 vX.Y.Z 在别的仓可能是另一个版本
    planned_tags = {(repo, tag) for _n, _d, tag in specs}
    for name, d, tag in specs:
        print(f"[check] {tag}")
        check_module(repo, name, d, tag, failures)
        check_requires_resolvable(repo, name, d, planned_tags, failures)
    if failures:
        print(f"\n❌ {len(failures)} 项检查失败，未打任何 tag：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    if not apply:
        print("\n✓ 全部检查通过（dry-run，加 --apply 打 tag 并推送）")
        return

    # 按序打 annotated tag；先全部创建，再统一推送（推送失败可定位已建列表）。
    # 发版期间登记在途（防并行会话同仓抢发），收尾 finally 释放。
    import inflight
    inflight.claim(repo, "release_train " + " ".join(t for _, _, t in specs))
    created = []
    try:
        for name, d, tag in specs:
            msg = f"{repo} {tag}"
            subprocess.run(["git", "-C", str(d), "tag", "-a", tag, "-m", msg], check=True)
            created.append((name, d, tag))
            print(f"[tag] {tag}")
        for name, d, tag in created:
            subprocess.run(["git", "-C", str(d), "push", "origin", tag], check=True)
            print(f"[push] {tag}")
        create_releases(repo, root, created)
    finally:
        inflight.release(repo)
    print(f"\n✓ train 完成：{len(created)} 个 tag 已推送")


def create_releases(repo: str, root, created: list) -> None:
    """tag → GitHub Release 对账（.github#8）：notes=CHANGELOG 同版本小节 + 该 tag
    go.mod 的矩阵依赖地板。嵌套模块 tag 恒 --latest=false，根模块 tag 成为 latest。
    失败只告警不回滚（tag 已推送；gen_releases.py --apply 可幂等补齐）。"""
    import os
    import tempfile

    sys.path.insert(0, str(Path(__file__).parent))
    from gen_releases import build_notes  # noqa: E402

    for name, _d, tag in created:
        path = None
        try:
            notes = build_notes(str(root), tag, repo)
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write(notes)
                path = f.name
            args = ["release", "create", tag, "--repo", f"oekit/{repo}",
                    "--title", tag, "--notes-file", path, "--verify-tag"]
            if "/" in tag:  # 嵌套模块 tag 不抢根模块的 latest
                args.append("--latest=false")
            r = subprocess.run(["gh", *args], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[release] ⚠ {tag}: {(r.stderr or r.stdout).strip()[:200]}（gen_releases.py --apply 可补齐）")
            else:
                print(f"[release] {tag}")
        finally:
            if path:
                os.unlink(path)


if __name__ == "__main__":
    main()
