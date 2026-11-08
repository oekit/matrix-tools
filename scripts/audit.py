#!/usr/bin/env python3
"""全矩阵审计：module 行 / 残留引用 / README 门面 / AGENTS / CHANGELOG / 安装行版本 / 矩阵表新鲜度 / 站点。
用法：
  python3 scripts/audit.py               # 本地检出（MATRIX_BASE 或默认路径）
  MATRIX_BASE=repos python3 scripts/audit.py --remote-tags   # Actions 克隆布局
输出：stdout 报告；发现漂移时 exit 1（供 CI 判定）。"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
sys.path.insert(0, str(__file__).rsplit("/", 1)[0])
from matrix import (  # noqa: E402
    MODULES, ORG, SITE_REPO, STALE_RE, repo_dir, module_line, sibling_deps, latest_tag,
    GO_FLOOR_BASE_MAX, GO_FLOOR_BUSINESS_OK, GO_FLOOR_BASE_LAYER, GO_FLOOR_EXEMPT, DESC,
    RemoteReadError, REMOTE_READ_LOG, enable_tag_cache, prefetch_remote_tags, remote_tags,
)
from verify_site_copy import check_counts as site_check_counts, check_llms_coverage, check_ghost_roots, count_units  # noqa: E402
import gen_matrix_go  # noqa: E402  （只用它的退出码契约 EXIT_* / classify_check）
from verify_release_units import (check_llms_units, check_section_consistency,  # noqa: E402
                                  check_filesystem_oracle, derive_nested_versions,
                                  check_nested_version_correspondence)

BASE = os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", ".."))
BASE = os.path.abspath(BASE)
REMOTE_TAGS = "--remote-tags" in sys.argv
EXEMPT_QUICKUSE = {"pkg", "kitex-examples"}      # pkg 全文即用法；kitex-examples 有「## 运行」
EXEMPT_TABLE = set()                              # 矩阵总览表所有仓都必须有

issues: list[str] = []


# ---------- 进度与缓冲 ----------
#
# 为什么要有进度行（2026-10-25 实测）：本脚本的 stdout 在**重定向/管道**时是
# **块缓冲**（Python 默认），实测一次 48s 的审计写进日志文件时，
# **整个运行期间文件都是 0 字节**，只在进程退出那一刻一次性落盘。
# 于是「跑到一半被 kill / 超时」这种形态 ⇒ 日志里**一个字都没有**
# （实测后台任务「Re-run audit for final truth」900s 超时，输出日志只有
# 运行时那句 "Command timed out after 900 seconds"，审计自己的输出全丢）。
# 排查时既不知道跑到哪，也不知道当时在查什么。
#
# 修法两条：
#  1. stdout 改成**行缓冲** ⇒ 印一行落一行，进程被 kill 也留下已印的部分；
#  2. **进度行写 stderr**：stderr 默认行缓冲（Python 3.9+），且 CI 里
#     `audit.py > audit-report.txt` 收集的是 stdout ⇒ 进度进的是**日志**，
#     报告文件（会被贴进漂移 issue）保持干净，格式一个字不变。
try:                                    # 某些环境下 stdout 可能不是 TextIOWrapper
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):   # noqa: BLE001 - 拿不到就认了，不该因此崩
    pass


def progress(msg: str) -> None:
    """进度行写 stderr（实时落盘，不进报告文件）。"""
    print(f"[audit] {msg}", file=sys.stderr, flush=True)


def check(cond: bool, msg: str):
    if not cond:
        issues.append(msg)


# ---------- 退出码契约（CI 门禁按**退出码**判，不按报告文本判） ----------
#
#   0 = 通过（无漂移；报告里有「✓ 矩阵审计通过」）
#   1 = **有漂移**（issues 非空；报告里有 ❌ 行）
#   2 = **基础设施失败**（脚本崩了 / 门禁不可用）——**不是**「零漂移」
#
# 为什么必须把 2 单列（实测 2026-11-03）：Python 对**未捕获异常**的退出码
# 就是 **1**，而 1 在本脚本里表示「有漂移」⇒ 两者撞码 ⇒ CI 分不清
# 「真漂移」与「脚本崩了」。旧门禁更糟：audit.py 一崩，报告就是 0 字节 ⇒
# `grep -c '❌'` 得 0 ⇒ 「Fail on drift」步骤不触发 ⇒ **PR 门禁判成
# 零漂移全绿**（假绿）。⇒ 未捕获异常统一映射到 2，并打出定位信息。
def _audit_crash_handler(exc_type, exc, tb):
    """未捕获异常 ⇒ 基础设施失败（退出码 2），**不是**「零漂移」。

 崩溃前已积累的 issues 也要打出来：否则报告是空的 ⇒ 门禁虽然靠退出码
 判红了，但人看不出「在崩之前已经发现了什么」。
    """
    import traceback
    traceback.print_exception(exc_type, exc, tb)
    try:
        if issues:
            print(f"审计中断前已发现 {len(issues)} 项漂移：")
            for _i in issues:
                print(f"  ❌ {_i}")
        print(f"❌ 审计因异常中断（{exc_type.__name__}: {exc}）"
              f"——基础设施失败，不是「零漂移」")
    except Exception:  # noqa: BLE001 - 报告失败也不能吞掉退出码
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(2)


sys.excepthook = _audit_crash_handler


def gate_unavailable(what: str, e: BaseException) -> str:
    """门禁不可用 ⇒ **判红**（口径与 gen_matrix_go.classify_check 一致）。

    为什么（实测 2026-11-03）：这三道门禁的**异常路径**此前只 ⚠ 不判红
    ⇒ 该道门禁**整条消失**（读不到 = 没结论），而报告照样印
    「✓ 矩阵审计通过」——「判不了」被当成「没问题」（#79 的同一条纪律：
    判不了 ≠ 通过）。
    """
    return f"{what}不可用：{e}——该门禁**未核对**（判不了 ≠ 通过）"


def _read_remote(repo: str) -> str | None:
    """真正去读某仓的远端最新 tag（真读网络的那一步）。"""
    return latest_tag(repo, BASE, remote=REMOTE_TAGS)


# 读失败的仓：漂移**只记一次**。同一仓会被问四次（见 remote_latest 的说明），
# 记 4 遍同一条漂移只会让人以为有 4 个问题。
_remote_fail_recorded: set[str] = set()


def remote_latest(repo: str) -> str | None:
    """某仓**远端**最新 tag；读不到 ⇒ 记为漂移，**不**静默跳过（#79）。

    旧写法是 `lt = latest_tag(...); if lt: check(...)` —— 读失败时 latest_tag
    返回 None，整条检查**静默消失**，而报告照样印「✓ 矩阵审计通过」
    （#26 的形态：23/24 仓被跳过，报告仍显示通过）。

    现在：读不到 = 明确记一条漂移 ⇒ 该仓这一版**没被核对过**，不能算通过。

    **缓存**（2026-10-25）：本函数有**四个**调用点（README 安装行 / AGENTS 当前
    版本行 / 站点 versions.json / 嵌套模块父模块地板），而 `matrix.remote_tags()`
    没有缓存 ⇒ 24 仓 × 4 处 = **96 次** HTTPS 读，实测把审计拖到 7 分钟以上、
    后台任务 900s 超时。现在同一仓**只读一次**（96 → 24 次），且 24 仓并发预取；
    读失败也缓存（失败仓恰恰最慢），但**仍记一条漂移**（口径不变）。
    详见 matrix.py 的 enable_tag_cache。
    """
    try:
        return latest_tag(repo, BASE, remote=REMOTE_TAGS)
    except RemoteReadError as e:
        if repo not in _remote_fail_recorded:
            _remote_fail_recorded.add(repo)
            check(False, f"{e}（该仓版本事实未核对，不能当通过）")
        return None


def expected_table(repo: str) -> str:
    from gen_tables import render_table_for  # noqa: 延迟导入避免环
    return render_table_for(repo).strip()


# ---------- breakingIndex 完整性门禁（matrix-tools#31） ----------
#
# 落地顺序提示（matrix-tools#34）：本门禁比对的是「**当前生成器的输出** vs
# **已发布产物**」。改了生成器（gen_versions.py / gen_matrix_go.py）之后，
# 两侧只有一种组合能对上：**产物用新生成器产出并先落地，再合生成器**。
# 先合生成器 → 门禁报「漏收」；用旧生成器落产物再合 → 门禁报「多出」。
# 两种报错都不提顺序，排查成本高——所以把提示写进报错本身。
GEN_ORDER_HINT = ("｜若本次改的是生成器（gen_versions.py / gen_matrix_go.py），"
                   "落地顺序应为：先用**新**生成器产出并落地产物，再合生成器"
                   "（见 matrix-tools#34；或跑 scripts/land_generator_change.py）")
# 背景：versions.json 的 breakingIndex 靠人工从各模块 CHANGELOG 转抄，实测
# 24 个模块中 5 个模块 15 条 Breaking 记录未收录——registry 与 nns 的
# breakingIndex 甚至是**空数组**，而两仓的 v0.4.0 正是「SPI 包提升至模块根」
# 这一类下游必断的变更。下游照矩阵升版，构建直接红，且报错只说
# 「no required module provides package X」，完全不提真正原因（路径变了）。
#
# 门禁口径：**条目级全量比对**，不比对「有没有」这种粗粒度。
#   - CHANGELOG 标注的破坏性条目 → 必须在 versions.json 里逐条在位；
#   - versions.json 里的条目 → 必须在 CHANGELOG 里找得到（反向防手改漂移）。
# 全量比对（不按已发布 tag 过滤）是刻意的：按 tag 过滤会在「本地没 fetch 到
# 那些 tag」时把条目误判为未发布而跳过——那是**门禁假通过**的最典型形态。
# 本地 CHANGELOG 只会写已发布版本，多报最多是提示重跑生成器，漏报是断链。
def check_breaking_index(live: dict):
    from gen_versions import breaking_index, breaking_suspects  # noqa: 延迟导入
    indexed = {m["module"]: m.get("breakingIndex") or [] for m in live.get("modules", [])}
    for repo in MODULES:
        try:
            want = breaking_index(repo)
        except FileNotFoundError:
            continue
        have = indexed.get(repo)
        if have is None:
            check(False, f"{repo}: versions.json 缺该模块条目（跑 gen_versions.py）")
            continue
        have_keys = {(it.get("version"), it.get("line")) for it in have}
        want_keys = {(it["version"], it["line"]) for it in want}
        missing = want_keys - have_keys
        extra = have_keys - want_keys
        if missing:
            sample = sorted(missing)[:2]
            check(False, f"{repo}: breakingIndex 漏收 {len(missing)} 条 CHANGELOG 破坏性变更"
                         f"（跑 gen_versions.py）：{sample}{GEN_ORDER_HINT}")
        if extra:
            sample = sorted(extra)[:2]
            check(False, f"{repo}: breakingIndex 多出 {len(extra)} 条 CHANGELOG 里没有的条目"
                         f"（跑 gen_versions.py）：{sample}{GEN_ORDER_HINT}")

    # 疑似破坏性（机器判不了语义）：不阻塞，但必须显式提示，不能静默吞掉
    suspects = []
    for repo in MODULES:
        try:
            s = breaking_suspects(repo)
        except FileNotFoundError:
            continue
        if s:
            suspects.append(f"{repo}×{len(s)}")
    if suspects:
        print(f"  ⚠ 疑似破坏性变更未标注（`**refactor(...)**` / `### Changed` 段等，"
              f"机器判不了语义，请人工确认是否需进 breakingIndex）：{'; '.join(suspects)}")


# ---------- breakingIndex 独立 oracle（matrix-tools#31 复审补漏） ----------
#
# 为什么要再加一路 oracle：上面的双向门禁是「**解析器输出** vs **生成的
# versions.json**」——两边**共享同一套判断**。解析器一旦有盲区，两边一起漏，
# 门禁照样全绿。这正是 issue #31 抱怨的「门禁假通过」：矩阵说 registry/nns
# 无破坏性变更，而 CHANGELOG 白纸黑字写着 SPI 包提升至模块根。
#
# 复审实测到的盲区：CHANGELOG 里**顶格独立成行**的破坏性标记
# （`**BREAKING**: ...`，不带 `- ` bullet 前缀）——gormx v0.6.0 与 hertz v0.7.0
# 的主键类型泛型化正是这种写法，旧解析器整段漏收，两仓 breakingIndex 近乎空。
#
# oracle 的判据**刻意不复用解析器逻辑**：直接扫 CHANGELOG 原文，用肉眼可见的
# 三种标记形态找候选，再要求 breakingIndex 必须覆盖到。这样解析器改了盲区，
# oracle 仍站在旁边盯着——它验的是「原文里写着的破坏性，矩阵有没有如实告诉下游」。
ORACLE_WORD_RE = re.compile(r"(?i)breaking|不兼容|破坏性")
ORACLE_VERHEAD_RE = re.compile(r"^##\s*\[([^\]]+)\]")
ORACLE_SEMVER_RE = re.compile(r"v\d+\.\d+\.\d+")
# **已发布**版本头：只有 vX.Y.Z 形态的版本段，其内容才要求进 breakingIndex。
# `## [Unreleased]`（未发布）的内容**不在**矩阵里——矩阵描述的是已发布的版本线，
# 未发布的内容进了矩阵反而是**提前剧透**（下游会按一个还不存在的版本去装）。
# 生成器侧同口径：gen_versions._normalize_version 对 Unreleased 返回 None。
# 此前 oracle 只在「段标题」分支跳过非 semver，bullet/standalone 分支没跳 →
# hertz 的 [Unreleased] 段里一条 **BREAKING** 标记被判成「矩阵漏收」，
# 把全组织的 matrix-tools PR 门禁卡住（2026-10-20 实测）。
ORACLE_PUBLISHED_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")
# 归属校验用：**全限定**的 oekit 模块路径 `github.com/oekit/<name>`。
# 刻意不认裸 `<name>/`——裸形式无法区分「别仓」与「本仓的子包」。
ORACLE_OWNPATH_RE = re.compile(r"github\.com/oekit/([a-z0-9-]+)")


def _oracle_norm(s: str) -> str:
    """归一化：去 markdown 记号与空白，只留可比的文字内容。"""
    return re.sub(r"\s+", " ", re.sub(r"[*`#>\s]+", " ", s)).strip()


def _oracle_markers(text: str) -> list[tuple[str, str, str]]:
    """扫 CHANGELOG 原文 → [(形态, 版本头, 关键文本)]。独立于解析器。

    **只收已发布版本段（`## [vX.Y.Z]`）里的标记**——`## [Unreleased]` 的内容
    尚未发布，矩阵里本来就不该有（与生成器同口径，见 ORACLE_PUBLISHED_RE）。
    """
    out: list[tuple[str, str, str]] = []
    ver = ""
    published = False
    for raw in text.splitlines():
        m = ORACLE_VERHEAD_RE.match(raw)
        if m:
            ver = m.group(1).strip()
            published = bool(ORACLE_PUBLISHED_RE.match(ver))
            continue
        s = raw.strip()
        if not s:
            continue
        if not published:
            # 未发布段（含版本头之前的内容）：不要求进 breakingIndex
            continue
        # (a) `### Breaking` 段标题——该版本应当有对应条目
        if s.startswith("###"):
            title = s.lstrip("#").strip()
            if ORACLE_WORD_RE.search(title):
                out.append(("section", ver, title))
            continue
        # (b) 顶格独立成行的 `**BREAKING**: ...`——必须被逐条覆盖。
        # ★ 只认**破坏性词开头**的顶格粗体（`**BREAKING**` / `**不兼容**` /
        #   `**破坏性**`），不认任意 `**...**` 粗体——2026-11-07 实测误报：
        #   concurrency v0.27.0 的叙述标题「**`fanout.Option` 更名**：本仓
        #   AGENTS.md 写着……0.x 阶段 minor 允许 breaking……」是同一段变更的
        #   **正文复述**（bullet 条目已在 breakingIndex），旧规则因行内出现
        #   "breaking" 一词把它当独立标记 ⇒ 要求同一段内容被覆盖两次，
        #   而它的前 60 字符与条目不同 ⇒ 永远判未覆盖。
        if re.match(r"^\*\*(BREAKING|不兼容|破坏性)", s):
            out.append(("standalone", ver, _oracle_norm(s)))
            continue
        # (c) 条目行内含破坏性关键词——必须被逐条覆盖
        if s.startswith("- ") and ORACLE_WORD_RE.search(s):
            out.append(("bullet", ver, _oracle_norm(s[2:])))
    return out


def check_breaking_oracle():
    """独立 oracle：CHANGELOG 原文里的破坏性标记，必须在 breakingIndex 里被覆盖。"""
    from gen_versions import breaking_index  # noqa: 延迟导入

    all_suspect: list[str] = []
    for repo in MODULES:
        try:
            root = repo_dir(repo, BASE)
        except FileNotFoundError:
            continue
        if not root.exists():
            continue
        try:
            items = breaking_index(repo)
        except FileNotFoundError:
            continue
        keys = [_oracle_norm(it.get("line", "")) for it in items]
        vers = [it.get("version", "") for it in items]
        sources = [(root / "CHANGELOG.md", "")]
        # ⚠️ **必须扫到任意深度**（实测 2026-11-03）：旧写法 `*/go.mod` 只扫**一层**
        # ⇒ 深度 4 的 kitexotel（observability/tracing/instrumentation/kitexotel）
        # 的**独立 CHANGELOG** 既不在解析器（gen_versions 同样只扫一层）里、
        # 也不在本 oracle 里 ⇒ 两边**同款深度口径** ⇒ 共同失明
        # （违背本文件「判据不复用被检对象逻辑」的纪律）。
        # 前缀 = **相对仓根的完整子路径**（与嵌套 tag 前缀同形，如
        # `observability/tracing/instrumentation/kitexotel/v0.7.7`）。
        for gomod in sorted(root.glob("**/go.mod")):
            if gomod.parent == root:
                continue          # 仓根本身不是嵌套模块
            if ".git" in gomod.parts or "vendor" in gomod.parts:
                continue          # 依赖仓的 vendor 不是本仓的嵌套模块
            _sub = gomod.parent.relative_to(root).as_posix()
            sources.append((gomod.parent / "CHANGELOG.md", f"{_sub}/"))
        for path, _prefix in sources:
            if not path.exists():
                continue
            for kind, ver, val in _oracle_markers(path.read_text(encoding="utf-8")):
                if kind == "section":
                    tok = ORACLE_SEMVER_RE.search(ver or "")
                    if not tok:
                        continue
                    want = tok.group(0)
                    if not any(want in v for v in vers):
                        check(False, f"{repo}: CHANGELOG {want} 有 Breaking 段，"
                                     f"但 breakingIndex 该版本无任何条目（解析器可能漏收整段）")
                    continue
                k = val[:60]
                if not k:
                    continue
                if not any(k in kk or kk[:60] in k for kk in keys if kk):
                    check(False, f"{repo}: CHANGELOG 有破坏性标记但 breakingIndex 未覆盖"
                                 f"（{kind}，{ver}）：{val[:60]}")

        # (d) 归属校验（查**产物**而非输入，故与解析器逻辑独立）：
        # breakingIndex 里的每一条，若它**全限定**引用了**别仓**的模块路径、
        # 完全没有本仓的全限定路径，那它值得人工看一眼——有可能是把别仓的
        # 破坏性变更收进了本仓（信号 F 这类启发式最容易犯这个错）。
        #
        # ⚠️ **只能作提示，不能作阻塞门禁**（2026-10-20 实测定论）：
        # 「条目提到别仓路径」**不是**误归属的证据——迁移目标（logx v0.4.0
        # `Manager` 迁至 core/lifecycle）、壳来源（data 的 timertask 壳移除
        # 转发符号）、剥离对象（timertask 根包不再依赖 concurrency）、先例引用
        # 都会合法地提到别仓。实测收紧到「全限定路径」后仍有 4 条**合法条目**
        # 被误报。门禁要「少而致命」，误报多的门禁会被绕过——故降级为 ⚠ 提示。
        own_q = f"github.com/oekit/{repo}"
        suspect = []
        for it in items:
            line = it.get("line", "")
            others = set()
            for m in ORACLE_OWNPATH_RE.finditer(line):
                name = m.group(1) or ""
                # 「oekit」是组织名也是 BOM 仓名，`github.com/oekit/oekit/issues/N`
                # 是 issue 链接——不是「别仓路径」，排除掉
                if name and name != repo and name != ORG:
                    others.add(name)
            if not others:
                continue
            if own_q in line:
                continue
            suspect.append(f"{repo}→{sorted(others)[0]}")
        all_suspect.extend(suspect)
    if all_suspect:
        print(f"  ⚠ 归属存疑条目（仅提示，不阻塞）：条目提到别仓路径但未提到本仓路径，"
              f"请人工确认是否收错仓：{'; '.join(all_suspect[:8])}")


# ---------- Go 版本地板三层口径 + go.mod/CI 成对（matrix-tools#33） ----------
def _go_directive(d) -> tuple[int, int] | None:
    """读 go.mod 的 go 指令 → (major, minor)。"""
    p = d / "go.mod"
    if not p.exists():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^go\s+(\d+)\.(\d+)", line)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None


def _ci_go_version(d) -> str | None:
    """读各仓 ci.yml 的 go-version（形如 '1.24'）。"""
    p = d / ".github" / "workflows" / "ci.yml"
    if not p.exists():
        return None
    m = re.search(r"go-version:\s*'?([0-9.]+)'?", p.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def check_go_floor_pairing(repo: str, d):
    """Go 地板三层口径 + go.mod/CI 成对一致。

    负责人裁决（#33）：「基础包需要依赖低版本的 go1.24，业务包依赖 1.24 / 1.25 / 1.26 都行。」
    门禁**只放宽「地板值」，不放宽「成对」**——CI 必须用 go.mod 声明的版本构建：
    CI 用 1.26 构建、go.mod 声明 1.24，等于**最低支持版本从来没被验证过**，
    下游在 1.24 上装这个包可能到 CI 才第一次发现构建失败。
    """
    gm = _go_directive(d)
    if gm is None:
        return
    ci = _ci_go_version(d)
    if ci:
        parts = ci.split(".")
        if len(parts) >= 2 and all(x.isdigit() for x in parts[:2]):
            ci_m = (int(parts[0]), int(parts[1]))
            if ci_m != gm:
                check(False, f"{repo}: go.mod 声明 go {gm[0]}.{gm[1]}，CI 却用 {ci} 构建"
                             f"——最低支持版本未被验证（CI 版本须与 go.mod 成对）")
    if repo in GO_FLOOR_EXEMPT:
        return
    if repo in GO_FLOOR_BASE_LAYER:
        if gm > GO_FLOOR_BASE_MAX:
            check(False, f"{repo}: 基础层 Go 地板 {gm[0]}.{gm[1]} 高于 1.24"
                         f"（基础包地板抬高会传染全部下游）")
    elif gm not in GO_FLOOR_BUSINESS_OK:
        check(False, f"{repo}: 业务层 Go 地板 {gm[0]}.{gm[1]} 不在允许档位 "
                     f"（1.24 / 1.25 / 1.26）内")


# ---------- 远端事实预取（先读一遍，后面全是缓存命中） ----------
#
# 为什么放在最前面：本脚本后面有**四个**地方问同一仓的远端最新 tag
# （README 安装行 / AGENTS 当前版本行 / 站点 versions.json / 嵌套模块地板），
# gen_releases 的 Release 对账还要**每个仓的完整 tag 列表**；而
# `matrix.remote_tags()` 不带缓存 ⇒ 24 仓 × 4 处 = 96 次 HTTPS 读
# （实测 1.4–2.1s/次，拥塞时更久）⇒ 审计本身要跑 281s，后台任务直接
# 900s 超时（2026-10-25 实测）。这里一次性**并发**读掉 24 仓，之后全是命中。
#
# 缓存**默认关**（matrix.enable_tag_cache）——发布类脚本不能开：它们的产物
# （BOM / versions.json）必须反映**生成那一刻**的远端事实（#79）。
# 审计是**一次性**的核对，开着正合适。
#
# 读失败也缓存，但**仍记一条漂移**（matrix-tools#79 的口径不变）：
# 「读不到」永远不等于「没有 tag」。
enable_tag_cache()
if REMOTE_TAGS:
    progress(f"阶段 1/7 预取远端事实（{len(MODULES)} 仓，并发）…")
    prefetch_remote_tags(MODULES, BASE, on_fail=lambda repo, e: check(
        False, f"{e}（该仓版本事实未核对，不能当通过）"))
    progress("阶段 1/7 完成")

# ---------- 模块仓 ----------
for _i, repo in enumerate(MODULES, 1):
    progress(f"阶段 2/7 逐仓检查 {_i}/{len(MODULES)} {repo}")
    # ⚠️ **读不到仓 ⇒ 判红**（实测 2026-11-03）：旧写法让 FileNotFoundError
    # 冒泡 ⇒ 脚本崩在**第一仓** ⇒ 报告 0 字节 ⇒ CI 的 `grep -c '❌'` 得 0
    # ⇒ 「Fail on drift」步骤不触发 ⇒ **PR 门禁判成零漂移全绿**（假绿）。
    # 现在：读不到 = 该仓**未被核对** ⇒ 记一条漂移（口径与 #79 一致：
    # 「判不了」≠「没问题」），并继续跑后面的门禁。
    try:
        d = repo_dir(repo, BASE)
    except FileNotFoundError as e:
        check(False, f"{repo}: 仓检出失败（{e}）——该仓未被核对，不能当通过")
        continue
    # 0) Go 地板三层口径 + go.mod/CI 成对
    check_go_floor_pairing(repo, d)
    # 1) module 行
    ml = module_line(repo, BASE)
    check(ml == f"module github.com/oekit/{repo}", f"{repo}: module 行 = {ml!r}")
    # 2) 残留引用
    r = subprocess.run(["grep", "-rl", "--exclude-dir=.git",
                        "-e", "yi-nology", "-e", "openekit", "-e", "github.com/oekit/ekit-", "."],
                       cwd=d, capture_output=True, text=True)
    if r.stdout.strip():
        issues.append(f"{repo}: 残留旧引用 {r.stdout.strip().splitlines()[:3]}")
    # 3) AGENTS / CHANGELOG
    check((d / "AGENTS.md").exists(), f"{repo}: 缺 AGENTS.md")
    check((d / "CHANGELOG.md").exists(), f"{repo}: 缺 CHANGELOG.md")
    # 占位/畸形版本头门禁（2026-10-18 事故：批量写版时版本号替换失败，15 仓 36 处
    # 「## [- 描述」与 vv 双 v 笔误残留——已手工填实，此处防复发）
    cl_bad = [l for l in (d / "CHANGELOG.md").read_text().splitlines()
              if l.startswith("## [-") or re.match(r"## \[.*vv\d", l)]
    if cl_bad:
        check(False, f"{repo}: CHANGELOG 占位/畸形版本头 {len(cl_bad)} 处"
                     f"（如 {cl_bad[0][:48]!r}；填实为已发布 tag）")
    readme = d / "README.md"
    if not readme.exists():
        issues.append(f"{repo}: 缺 README")
        continue
    t = readme.read_text()
    # 4) README 门面
    check("🤖 AI Agent" in t and "AGENTS.md" in t, f"{repo}: README 缺 AI Agent 行")
    if repo not in EXEMPT_QUICKUSE:
        # README 重写后的统一模板（2026-10-15「README 重写」批次）：这个仓库做什么 + 快速使用
        check("## 这个仓库做什么" in t and "## 快速使用" in t,
              f"{repo}: README 缺这个仓库做什么/快速使用段")
    if repo not in EXEMPT_TABLE:
        check(t.count("## 矩阵总览") == 1,
              f"{repo}: 矩阵总览段重复（历史遗留旧段未清理，跑 gen_tables.py）")
        check(f"## 矩阵总览（{len(MODULES)} 活跃模块，自动生成）" in t,
              f"{repo}: 矩阵总览表过期或缺失（跑 gen_tables.py）")
        # 5) 表新鲜度：重新生成并与现状比对
        m = re.search(r"## 矩阵总览.*?(?=\n## |\Z)", t, flags=re.S)
        cur = m.group(0).strip() if m else ""
        want = expected_table(repo)
        # 只比对表格主体（标题行与正文），忽略尾部注释块
        cur_rows = "\n".join(l for l in cur.splitlines() if l.startswith("|"))
        want_rows = "\n".join(l for l in want.splitlines() if l.startswith("|"))
        check(cur_rows == want_rows, f"{repo}: 矩阵表内容漂移（跑 gen_tables.py）")
    # 6) 安装行版本
    m = re.search(r"go get github\.com/oekit/" + re.escape(repo) + r"@(v[0-9.]+)", t)
    if m:
        lt = remote_latest(repo)
        if lt:
            check(m.group(1) == lt, f"{repo}: 安装行 {m.group(1)} ≠ 最新 {lt}（跑 refresh_versions.py）")
    else:
        print(f"[warn] {repo}: README 无安装行（仅提示）")
    # 6b) AGENTS 当前版本行（与 README 安装行同一套对齐纪律）
    agents = d / "AGENTS.md"
    ma = re.search(r"当前版本：`(v[0-9.]+)`", agents.read_text()) if agents.exists() else None
    if ma:
        lt2 = remote_latest(repo)
        if lt2:
            check(ma.group(1) == lt2, f"{repo}: AGENTS 当前版本 {ma.group(1)} ≠ 最新 {lt2}（跑 refresh_versions.py）")
    # 6c) AGENTS.md 首行的 DESC 行必须是矩阵真相源 DESC 的**前缀**
    # 实测（2026-10-20）：data/core/pkg 三仓的这行是**陈旧快照**——DESC 更新后
    # 没人同步，AGENTS.md 里留着旧的能力描述；hertz 同类（AGENTS.md 还写着
    # 不存在的 `WithReadyAddr`）。AGENTS.md 是 AI Agent 判断「这个仓能做什么」的
    # 入口，与真相源不一致 = 组织里有两个事实源。
    # 允许截断（有的仓只写前半句），但截断点必须在 DESC 内——不许改写。
    if agents.exists():
        _head = agents.read_text().splitlines()[:8]
        _aline = next((l[2:].strip() for l in _head
                       if l.startswith("> ") and not l.startswith("> **")
                       and not l.startswith("> 当前版本")), None)
        _desc = (DESC.get(repo) or "").strip()
        if _aline and _desc and not _desc.startswith(_aline):
            issues.append(
                f"{repo}: AGENTS.md 首行不是 DESC 的前缀（跑 refresh_agents_desc.py）"
                f"——AGENTS.md 与矩阵真相源说法不一致")

# ---------- 门面壳同步（data 八壳 ← 源包导出符号） ----------
# 「源仓新增导出符号时本壳需同步」的跟版税此前靠人记（2026-10-16 曾漏
# pkg v0.6.0 的 BatchGetter），现为 audit 硬门禁。
from shell_sync import check_sync  # noqa: E402 延迟导入同目录脚本

for d in check_sync(BASE):
    issues.append(d)

# ---------- 站点仓 ----------
progress("阶段 3/7 站点仓与产物")
try:
    site = repo_dir(SITE_REPO, BASE)
    llms = site / "docs" / "public" / "llms.txt"
    check(llms.exists(), "site: 缺 docs/public/llms.txt")
    # 站点文案口径 + llms.txt 模块覆盖：**全站 .md/.txt 逐个数字核对**，
    # 不再只查 index.md 一个文件、也不再用子串匹配（"124 个独立发版" 里有
    # "24 个独立发版"，旧写法照样放行）。
    # 判据独立：期望值来自矩阵的模块登记表 + 文件系统里的真实 go.mod
    # （--repos-dir 给了就扫；CI 里 MATRIX_BASE=repos 天然有）。
    _site_counts = count_units(BASE if os.path.isdir(BASE) else None)
    issues += site_check_counts(site, _site_counts)
    issues += check_llms_coverage(site)
    # 发版单元全集（根 24 + 嵌套 7 + 伞仓 1 = 32）：verify_site_copy 只管**根模块**条目集合，
    # 嵌套模块的独立条目与 go get 安装行是另一件事——llms.txt 是 Agent 唯一会抓的
    # 机器可读索引，嵌套模块（零依赖隔离的载体）不给安装行 = 装不上。
    if llms.exists():
        _llms_text = llms.read_text(encoding="utf-8", errors="ignore")
        _repos = Path(BASE) if os.path.isdir(BASE) else None
        # 嵌套模块的「当前版本 + 对应父仓版本」——门禁**自己**的推导（tag 日期序），
        # 与生成器（commit 祖先关系）不同源，两者互为交叉验证。
        _nested_derived = derive_nested_versions(_repos)
        issues += check_filesystem_oracle(_repos)
        issues += check_llms_units(_llms_text)
        issues += check_section_consistency(_llms_text, _nested_derived)
        # llms.txt 条目里的**反引号标识符**必须在本仓源码里真实存在——
        # 实测抓到 hertz 条目写了 kitex 的 WithReadyAddr（照抄得到 undefined）。
        # 判据只读源码（客观事实），不与被检对象共享任何解析逻辑。
        # 做**非阻塞提示**：机器判不了「这是不是别仓的 API」，由人确认。
        try:
            from llms_api_check import check_llms_api_identifiers  # noqa: E402
            _api_warn = check_llms_api_identifiers(site, Path(BASE), list(MODULES))
            if _api_warn:
                print(f"  ⚠ llms.txt 反引号标识符在本仓源码里不存在（仅提示，不阻塞，"
                      f"请人工确认是否串味/已移除）：{len(_api_warn)} 条")
                for _w in _api_warn[:6]:
                    print(f"      · {_w}")
                if len(_api_warn) > 6:
                    print(f"      · …… 另 {len(_api_warn) - 6} 条同类")
        except Exception as e:  # noqa: BLE001
            # ⚠️ **不可用 ⇒ 判红**（实测 2026-11-03）：旧写法只 ⚠ ⇒ 这道门禁
            # **整条消失**（读不到 = 没结论），而报告照样印「✓ 矩阵审计通过」。
            issues.append(gate_unavailable("llms.txt 标识符判据", e))
        # oracle C：已发布 versions.json 的 nested 对应关系必须与推导一致
        if _nested_derived:
            _vf = site / "docs" / "public" / "versions.json"
            if not _vf.exists():
                issues.append("site: 缺 docs/public/versions.json（嵌套模块的版本对应关系在机器可读矩阵里）")
            else:
                try:
                    _pub = json.loads(_vf.read_text())
                    issues += check_nested_version_correspondence(_pub, _nested_derived, _repos)
                except Exception as e:  # noqa: BLE001
                    issues.append(f"site: versions.json 解析失败：{e}")
    if os.path.isdir(BASE):
        issues += check_ghost_roots(Path(BASE), _site_counts)
    r = subprocess.run(["grep", "-rl", "--exclude-dir=.git",
                        "--exclude-dir=dist", "--exclude-dir=cache",
                        "-e", "github.com/yi-nology", "-e", "github.com/openekit", "."],
                       cwd=site, capture_output=True, text=True)
    if r.stdout.strip():
        issues.append(f"site: 残留旧引用 {r.stdout.strip().splitlines()[:3]}")
    for g in ["getting-started", "logging", "errors", "caching", "http-service",
              "rpc-service", "observability", "migration", "ai-agent"]:
        check((site / "docs" / "guide" / f"{g}.md").exists(), f"site: 缺指南 {g}")
    # versions.json 新鲜度：这是站点对外发布的版本口径，陈旧比缺失更有害
    # （消费方照着站点选版本会装到旧版）。此前靠"发版后记得重跑"，
    # 2026-10-18 实测从 06:04 起累积 6/24 条陈旧（concurrency v0.14.0、
    # redisx v0.8.0 等），现为硬门禁。
    vfile = site / "docs" / "public" / "versions.json"
    if not vfile.exists():
        check(False, "site: 缺 docs/public/versions.json")
    else:
        live = json.loads(vfile.read_text())
        cur = {m["module"]: m.get("latest") for m in live.get("modules", [])}
        stale = []
        for repo in MODULES:
            lt = remote_latest(repo)
            if lt and cur.get(repo) != lt:
                stale.append(f"{repo}(站点 {cur.get(repo)} ≠ 最新 {lt})")
        check(not stale, f"site: versions.json 陈旧 {len(stale)} 项（跑 gen_versions.py）：{'; '.join(stale[:4])}")
        check_breaking_index(live)
except FileNotFoundError as e:
    issues.append(f"site: {e}")

# 独立 oracle 放在站点块之外：它验的是「CHANGELOG 原文 ↔ 解析器输出」，
# 与站点是否发布 versions.json 无关——站点文件缺失时，它照样要盯住解析器盲区。
def check_dead_paths():
    """独立门禁：**本仓已消失的路径必须被 breakingIndex 提及**。

    与 check_breaking_oracle 互补：那条问「CHANGELOG 里标了 breaking 的，矩阵收没收」；
    这条问「下游 import 会断的路径，矩阵告没告」。两者都独立于生成器的措辞判断，
    但查的是不同的漏——实测（2026-10-20）hertz v0.5.1 / bootstrap v0.5.0 的
    4 条死路径在 breakingIndex 里一条都没有，而矩阵对下游说「无破坏性变更」。
    """
    try:
        from verify_dead_paths import dead_paths as _dead, covered as _covered  # noqa: 延迟导入
    except Exception as e:  # noqa: BLE001
        issues.append(f"dead-paths: 无法加载独立门禁脚本（{e}）")
        return
    try:
        site_dir = repo_dir(SITE_REPO, BASE)
        data = json.loads((site_dir / "docs" / "public" / "versions.json").read_text(encoding="utf-8"))
        blobs = {m["module"]: "\n".join(e.get("line", "") for e in (m.get("breakingIndex") or []))
                 for m in data.get("modules", [])}
    except Exception as e:  # noqa: BLE001
        issues.append(f"dead-paths: 读不到站点 versions.json（{e}）")
        return
    miss: list[str] = []
    for repo in MODULES:
        blob = blobs.get(repo)
        if blob is None:
            continue
        try:
            if not repo_dir(repo, BASE).is_dir():
                continue
        except FileNotFoundError:
            continue
        for path, ver in _dead(repo, BASE):
            if not _covered(path, blob):
                miss.append(f"{repo}: {path}（{ver}）")
    check(not miss, "dead-paths: 有死路径未被 breakingIndex 提及（下游 import 会构建失败）："
                    + "; ".join(miss[:4]))


def check_version_line():
    """独立门禁：**同一版本线内，后发布的 tag 版本号必须更高**（发布序列倒挂）。

    与前两条互补：check_breaking_oracle 问「CHANGELOG 标了 breaking 的矩阵收没收」；
    check_dead_paths 问「下游 import 会断的路径矩阵告没告」；这条问的是**发布事实**
    ——已发布版本里有没有「版本号在时间线上回退」的旁支版本。

    实测（2026-10-23）：4 仓 5 个已发布版本命中（network v0.1.1 / kitex v0.3.9 /
    v0.3.10 / redisx v0.3.1 / middleware v0.5.1）。它们 tag 在远端、Release 在、
    proxy 上可解析 ⇒ **撤不掉**（重推 = 缓存中毒），只能登记 + 声明。

    已登记的 5 处只提示不阻塞（`scripts/version_line_known.json`）；
    **未登记的新倒挂 ⇒ 判红**；某仓版本事实读不到 ⇒ 也判红（判不了 ≠ 通过）。
    """
    try:
        from verify_version_line import scan, EXTRA_LINE_REPOS  # noqa: 延迟导入
    except Exception as e:  # noqa: BLE001
        issues.append(f"version-line: 无法加载独立门禁脚本（{e}）")
        return
    repos = list(MODULES) + [r for r in EXTRA_LINE_REPOS if r not in MODULES]
    try:
        res = scan(Path(BASE), repos)
    except Exception as e:  # noqa: BLE001
        issues.append(f"version-line: 门禁执行异常（{e}）")
        return
    for item in res["known"]:
        print(f"  ⚠ 已知旁支版本（已登记，tag 已发布撤不掉）：{item['repo']} {item['tag']}"
              f"（发布于 {item['date']}，晚于已发布的 {item['higher_tag']}）")
    check(not res["unknown"],
          "version-line: 有未登记的发布序列倒挂（同一版本线内后发布的 tag 版本号反而更低）："
          + "; ".join(f"{i['repo']} {i['tag']}" for i in res["unknown"][:6]))
    check(not res["unreadable"],
          "version-line: 有仓的版本事实读不到（判不了，不能当通过）："
          + "; ".join(res["unreadable"][:4]))


progress("阶段 4/7 独立门禁（breakingIndex oracle / 死路径 / 版本线）")
check_breaking_oracle()
check_dead_paths()
check_version_line()

# ---------- 架构图（生成器产物一致性，light+dark） ----------
try:
    from gen_diagram import render_svg, targets as diagram_targets  # noqa: 延迟导入
    tgts = diagram_targets()
    if not tgts:
        print("[warn] 架构图两处目标仓均未检出，跳过一致性检查")
    for label, p, theme in tgts:
        check(p.exists() and p.read_bytes() == render_svg(theme),
              f"{label}: 与生成器不一致（跑 gen_diagram.py）")
    home = repo_dir("oekit", BASE)
    ht = (home / "README.md").read_text()
    check("docs/assets/architecture.svg" in ht, "oekit 主仓: README 未嵌入架构图")
    check("architecture-dark.svg" in ht, "oekit 主仓: README 缺暗色架构图 source")
except SystemExit as e:
    issues.append(f"diagram: {e}")

# ---------- Go 注释内模块路径引用校验（matrix-tools#4） ----------
# 背景：registry/nns/kitex/workflow 包提升至模块根时发现多仓注释仍引用旧路径
# （registry/kitex、network/loadbalance 等）——文本漂移人工难发现，收进门禁。

def check_go_comment_paths():
    """扫描各仓 *.go 注释/文本中的 github.com/oekit/<name> 引用：
    <name> 不在 MODULES（已删除仓/旧路径）即报漂移。"""
    known = set(MODULES) | {"matrix-tools", "oekit", "oekit.github.io", "profile"}
    pat = re.compile(r"github\.com/oekit/([A-Za-z0-9_-]+)")
    for repo in MODULES:
        root = repo_dir(repo, BASE)
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != ".git"]
            for fn in files:
                if not fn.endswith(".go"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    text = open(path, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                for m in pat.finditer(text):
                    name = m.group(1)
                    if name not in known:
                        rel = os.path.relpath(path, BASE)
                        check(False, f"{repo}: 引用不存在的模块路径 "
                                     f"github.com/oekit/{name}（{rel}，注释或文本陈旧）")


check_go_comment_paths()

# ---------- 嵌套模块主模块地板门禁（matrix-tools#6，承接 bootstrap#2） ----------
# 背景：bootstrap v0.5.1 事故——嵌套依赖地板指向已随结构重置清空的旧 tag，
# tidy/直连直接失败；「主模块已发新版而嵌套地板滞后」的漂移此前也靠人记。

def parse_semver(v: str):
    m = re.match(r"v(\d+)\.(\d+)\.(\d+)$", v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def check_nested_module_floors():
    """嵌套模块 go.mod require 其父模块时，地板不得落后父模块最新 tag。"""
    for repo in MODULES:
        root = repo_dir(repo, BASE)
        for gomod in sorted(root.glob("**/go.mod")):
            if gomod.parent == root:
                continue          # 仓根本身不是嵌套模块
            # 相对路径（多层嵌套也对）：observability/tracing/instrumentation/kitexotel
            sub = str(gomod.parent.relative_to(root))
            text = gomod.read_text()
            for req in re.finditer(r"^(?:\t|require )(github\.com/oekit/(\S+)) (v\S+)$", text, re.M):
                dep_repo, ver = req.group(2), req.group(3)
                if dep_repo != repo:  # 只校验对父模块的 require
                    continue
                lt = remote_latest(repo)
                if REMOTE_READ_LOG.get(repo) == "failed":
                    # 读失败已在 remote_latest 记为漂移；这里不再报「无主模块 tag」——
                    # 那会把「读不到」说成「没有 tag」（正是 #79 要拆开的两件事）。
                    continue
                floor, cur = parse_semver(ver), parse_semver(lt) if lt else None
                if not floor:
                    check(False, f"{repo}/{sub}: 父模块依赖版本不可解析 {ver}")
                    continue
                if not cur:
                    check(False, f"{repo}/{sub}: 父模块无主模块 tag 可比对（地板 {ver}）")
                    continue
                if floor < cur:
                    check(False, f"{repo}/{sub}: 父模块地板 {ver} 落后最新 {lt}"
                                 f"（release_train.py 同 train 升版）")


check_nested_module_floors()

# ---------- 各仓自身 go.mod 的 oekit 地板 vs 远端最新 tag（oekit/oekit#102） ----------
# **根模块形态**：`github.com/oekit/<短名>`
#
# ⚠️ 与「嵌套模块形态」（`github.com/oekit/<仓>/<子路径>`）**刻意分两个判据**：
#   · 根模块形态 → 本判据（scan_repo_floors），比「各模块远端最新 tag」；
#   · 嵌套模块形态 → check_nested_floors_of_other_repo（#104 方向），
#     比「**该嵌套模块**的远端最新 tag」——两者的「最新」不是同一个东西。
# 合并成一个正则会让「最新」的定义自相矛盾。
_FLOOR_REQ_RE = re.compile(
    r"^(?:\t|require )(github\.com/oekit/([a-z0-9-]+)) (v\d+\.\d+\.\d+)( // indirect)?$",
    re.M)

# 嵌套模块形态（oekit/oekit#104）：`github.com/oekit/<仓>/<子路径>`
# ⚠️ 路径段允许 `-`（仓名带连字符）与 `.`（如 hertzlog），**不含** `/` 开头/结尾。
_NESTED_FLOOR_REQ_RE = re.compile(
    r"^(?:\t|require )(github\.com/oekit/([a-z0-9-]+)/([a-z0-9._-]+(?:/[a-z0-9._-]+)*))"
    r" (v\d+\.\d+\.\d+)( // indirect)?$",
    re.M)


def scan_repo_floors(repo: str, root, latest_of) -> list[str]:
    """扫一个仓内**每个** go.mod 的根模块地板，返回「落后远端最新 tag」的清单。

    **纯函数**（不碰网络、不读全局状态）：`latest_of(dep) -> str | None`
    由调用方注入 ⇒ 自测能用假仓 + 假 tag 表驱动，不需要真网络。

    为什么覆盖**仓内每个 go.mod**（根 + 嵌套模块）：嵌套模块对**别的**根模块的
    地板（如 bootstrap/nacos → concurrency）同样是漂移，且嵌套模块自己发版
    不会自动带起它对别仓的根模块地板。
    """
    known = set(MODULES)
    out: list[str] = []
    for gomod in sorted(root.glob("**/go.mod")):
        if ".git" in gomod.parts or "vendor" in gomod.parts:
            continue
        rel = str(gomod.relative_to(root))
        for m in _FLOOR_REQ_RE.finditer(gomod.read_text()):
            dep, ver = m.group(2), m.group(3)
            if dep == repo:
                continue          # 自己的 tag：本仓不与自己比
            if dep not in known:
                continue          # 非矩阵模块（无 tag 可比）⇒ 不判
            lt = latest_of(dep)
            if not lt:
                continue          # 读不到由调用方记为漂移，这里不重复记
            floor, cur = parse_semver(ver), parse_semver(lt)
            if not floor or not cur:
                out.append(f"{repo}/{rel}: 地板 {dep} {ver} 或最新 {lt} 不可解析")
                continue
            if floor < cur:
                out.append(f"{repo}/{rel}: 地板 {dep} {ver} 落后远端最新 {lt}")
    return out


def scan_repo_nested_floors(repo: str, root, latest_nested_of) -> list[str]:
    """扫一个仓内**每个** go.mod 里对**别仓嵌套模块**的 require 地板。

    **纯函数**（同 scan_repo_floors）：`latest_nested_of(module_path) -> str | None`
    由调用方注入 ⇒ 自测不联网。

    口径（oekit/oekit#104）：某仓 require `github.com/oekit/<仓>/<子路径>`
    （**别的仓**的嵌套模块）时，地板不得落后于**该嵌套模块**的远端最新 tag。

    ⚠️ 为什么要单独一个判据（而不是并进 scan_repo_floors）：
    「最新」的**定义不同**——根模块形态比的是「该仓的根 tag」，嵌套模块形态
    比的是「该嵌套模块的 tag」（形如 `hlog/v0.1.6`，**带子路径前缀**）。
    并成一个正则 ⇒ 一边用错口径 ⇒ 判据自相矛盾。

    ⚠️ **「别仓」的判定**：嵌套模块的 require 行里，`<仓>` 段是**该嵌套模块的
    父仓**（nns require `registry/nacosimpl` ⇒ 父仓是 registry）⇒
    「别仓」= **父仓名 ≠ 当前仓名**。自己仓的嵌套模块（kitex require
    `kitex/…/kitexotel`）归 check_nested_module_floors 按「子→父」方向管，
    口径不同，刻意不合并。
    """
    out: list[str] = []
    for gomod in sorted(root.glob("**/go.mod")):
        if ".git" in gomod.parts or "vendor" in gomod.parts:
            continue
        rel = str(gomod.relative_to(root))
        for m in _NESTED_FLOOR_REQ_RE.finditer(gomod.read_text()):
            module_path, dep_repo, sub, ver = m.group(1), m.group(2), m.group(3), m.group(4)
            # ⚠️ **必须校验组数与类型**（2026-11-02 变异自证实测）：
            # 正则被改回「只匹配根模块」形态时捕获组从 5 个变 4 个 ⇒
            # `ver` 拿到 None ⇒ parse_semver(None) 抛 TypeError ⇒
            # **自测崩在异常而不是判红**（异常不算「抓住变异」）。
            # ⇒ 这里显式挡掉：形状不对就跳过（并由自测的变异断言转红）。
            if not isinstance(module_path, str) or not isinstance(ver, str):
                continue
            if dep_repo == repo:
                # **自己仓的嵌套模块**归 check_nested_module_floors 按「子→父」
                # 方向管（发版正确性要求），口径不同，刻意不合并。
                continue
            lt = latest_nested_of(module_path)
            if not lt:
                continue          # 读不到由调用方记为漂移，这里不重复记
            floor, cur = parse_semver(ver), parse_semver(lt)
            if not floor or not cur:
                out.append(f"{repo}/{rel}: 嵌套地板 {module_path} {ver} 或最新 {lt} 不可解析")
                continue
            if floor < cur:
                out.append(f"{repo}/{rel}: 嵌套地板 {module_path} {ver} 落后远端最新 {lt}")
    return out


# ---------- 地板判据的数据源：origin/main（远端事实），不读本地工作树（#246） ----------
#
# 为什么（matrix-tools#246，2026-11-08 维护轮实测两次）：地板判据拿「树里的
# require」比「远端最新 tag」——树若取**本地工作树**，本地 clone 一落后，
# 报告立刻**假红一片**，且每条红都挂着「跑 align_floors.py 抬升」的可执行
# 建议 ⇒ 人会真去抬地板（而地板在远端 main 早已抬好）⇒ 判据与建议自相矛盾。
# 实测：合并 15 个地板 PR 后本地未 pull，同款读取口径报出 22 条「需先抬
# 地板」，全为假警报；`git pull --ff-only` 后归零。
#
# 修法：判据改读 **origin/main** 的 go.mod（fetch 后物化成临时树）——与
# 「最新已发布 tag」同源，两边都是远端事实；读不到 ⇒ 显式判「未核对」
# （#79：判不了 ≠ 零漂移），**不许**静默回退读本地工作树。
_origin_trees: dict[str, Path] = {}       # repo → 物化的 origin/main go.mod 树
_origin_unreadable: dict[str, str] = {}   # repo → 读不到的原因（fail-loud 用）
_stale_local_main: dict[str, int] = {}    # repo → 本地 main 落后 origin/main 的提交数
_origin_base_tmp: Path | None = None      # 所有物化树共用的临时根（atexit 清理）


def _origin_main_tree(repo: str, root: Path) -> Path | None:
    """把 origin/main 上的全部 go.mod 物化到临时目录，返回该目录；读不到 ⇒ None。

    步骤：`git fetch origin main` → `git ls-tree FETCH_HEAD` 找 go.mod →
    逐个 `git show` 物化（保持相对路径，嵌套模块的 go.mod 也在内）。
    顺带记下「本地 main 落后 origin/main 多少提交」（_stale_local_main）⇒
    报告里提示 pull——其余读工作树的判据仍会被陈旧 clone 带偏。

    返回 None = 这道判据对这个仓**未核对**；调用方必须显式报红（#79），
    不得把 None 当「零漂移」，也不得回退读本地工作树（那正是 #246 的 bug）。
    """
    global _origin_base_tmp
    import atexit
    import shutil
    import tempfile
    if repo in _origin_trees:
        return _origin_trees[repo]     # 一仓只物化一次（fetch 有网络代价）
    fetch = subprocess.run(["git", "-C", str(root), "fetch", "origin", "main"],
                           capture_output=True, text=True, timeout=90)
    if fetch.returncode != 0:
        _origin_unreadable[repo] = ((fetch.stderr or fetch.stdout).strip().splitlines()
                                    or ["fetch 失败"])[-1][:160]
        return None
    if _origin_base_tmp is None:
        _origin_base_tmp = Path(tempfile.mkdtemp(prefix="audit-origin-main-"))
        atexit.register(shutil.rmtree, _origin_base_tmp, True)
    tree = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--name-only",
                           "FETCH_HEAD"], capture_output=True, text=True, timeout=60)
    if tree.returncode != 0:
        _origin_unreadable[repo] = ((tree.stderr or "").strip().splitlines()
                                    or ["ls-tree 失败"])[-1][:160]
        return None
    # 陈旧检测（提示用，不判红）：本地 main 落后 origin/main 几个提交。
    stale = subprocess.run(["git", "-C", str(root), "rev-list", "--count",
                            "main..FETCH_HEAD"], capture_output=True, text=True, timeout=60)
    if stale.returncode == 0 and stale.stdout.strip().isdigit() \
            and int(stale.stdout.strip()) > 0:
        _stale_local_main[repo] = int(stale.stdout.strip())
    out = _origin_base_tmp / repo
    n = 0
    for rel in tree.stdout.splitlines():
        if not rel.endswith("go.mod"):
            continue
        parts = Path(rel).parts
        if ".git" in parts or "vendor" in parts:
            continue
        show = subprocess.run(["git", "-C", str(root), "show", f"FETCH_HEAD:{rel}"],
                              capture_output=True, text=True, timeout=60)
        if show.returncode != 0:
            _origin_unreadable[repo] = f"show {rel} 失败：{(show.stderr or '').strip()[:120]}"
            return None
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(show.stdout, encoding="utf-8")
        n += 1
    if n == 0:
        _origin_unreadable[repo] = "FETCH_HEAD 上一个 go.mod 都没有（仓坏了？）"
        return None
    _origin_trees[repo] = out
    return out


def check_repo_go_mod_floors():
    """各仓 go.mod 里 `github.com/oekit/*` 的地板，不得落后于该模块的远端最新 tag。

    为什么要有这道门禁（oekit/oekit#102，2026-11-01 提出 / 2026-11-02 落地）：
    此前**各仓自身的 go.mod 地板没有任何自动判据**——阶段 6 只核对 **BOM 快照**
    的新鲜度 ⇒ 2026-11-01 pkg v0.31.1 发布后，**18 处地板漂移**（跨 13 个仓）
    只能靠人翻出来，issue 提了才有人动。

    口径（matrix-tools#246 起修正）：树 = **origin/main**（fetch 后物化，见
    _origin_main_tree 的说明）——本地工作树会让「本地 clone 陈旧 ⇒ 误报一片
    红」；tag = **远端最新 tag**。两边都是远端事实，版本**按数字**比
    （字符串排序会把 `v0.3.0` 排在 `v0.31.0` 之后）。

    ⚠️ **判据独立于 align_floors.py**（那是整改工具，不是判据）：两者若共享
    同一套解析逻辑，解析漏收时两边一起漏 ⇒ 门禁全绿、而矩阵实际有漂移
    （与「门禁不得与被检对象共享判断逻辑」同族）。这里自己解析。
    """
    def latest_of(dep: str) -> str | None:
        return remote_latest(dep)

    for repo in MODULES:
        root = _origin_main_tree(repo, repo_dir(repo, BASE))
        if root is None:
            # ★ 判不了 ≠ 零漂移（#79），也**不是**「地板落后」：文案必须拦住
            # 「照着红去抬地板」的错误动作（#246 实测的危害形态）。
            check(False, f"{repo}: 地板判据**未核对**（origin/main 读不到："
                         f"{_origin_unreadable.get(repo, '未知原因')}）——判不了 ≠ 零漂移；"
                         f"先修网络/凭证（或 git fetch origin main）再跑，"
                         f"不要据此抬地板")
            continue
        for msg in scan_repo_floors(repo, root, latest_of):
            check(False, f"{msg}（跑 align_floors.py --plan 抬升）")


check_repo_go_mod_floors()


# ---------- 各仓对**别仓嵌套模块**的地板 vs 该嵌套模块远端最新 tag（#104） ----------

def _nested_tag_prefixes() -> dict[str, str]:
    """模块 import 路径 → 其嵌套 tag 前缀（**完整子路径**，非最后一段）。

    ⚠️ **这里自己解析，不复用 matrix.nested_tag_prefix**：门禁不得与被检对象
    共享判断逻辑——若共享，登记表一改或前缀推导一错，门禁跟着一起错 ⇒
    又一次「门禁全绿、实际有漂移」。

    事实依据（Go 的子目录模块规则，实测 2026-11-02）：
      github.com/oekit/logx/hlog          → tag `hlog/v0.1.6`
      github.com/oekit/kitex/…/kitexotel  → tag
        `observability/tracing/instrumentation/kitexotel/v0.7.4`
    ⇒ 前缀 = **父仓名之后的完整子路径**（不是最后一段）。

    数据来源：**各仓嵌套模块自己的 go.mod**（module 路径写在第一行）⇒
    不依赖任何登记表 ⇒ 登记表漏登记时这道门禁**照样能看见**。

    ⚠️ **口径澄清**（这里最容易搞反）：**每个嵌套模块都住在它自己父仓的目录里**
    ⇒ 扫「仓 X 里的嵌套模块」拿到的 module 路径，其父仓名**必然是 X**。
    而本判据要管的是**消费方** require **别仓**的嵌套模块（nns require
    registry/nacosimpl）⇒ 因此这里要建的是**全矩阵嵌套模块的 module 路径表**
    （供消费方那侧匹配），**不是**「本仓的嵌套模块」。
    """
    prefixes: dict[str, str] = {}
    for repo in MODULES:
        # ★ 数据源 = origin/main（#246 同款口径：本地工作树陈旧 ⇒ 前缀表残缺
        #   ⇒ 消费方地板判据静默漏检）。读不到的仓由 check_repo_go_mod_floors
        #   的「未核对」条目点名，这里不重复报。
        root = _origin_main_tree(repo, repo_dir(repo, BASE))
        if root is None:
            continue
        for gomod in sorted(root.glob("**/go.mod")):
            if gomod.parent == root:
                continue          # 仓根本身不是嵌套模块
            if ".git" in gomod.parts or "vendor" in gomod.parts:
                continue
            first = gomod.read_text().splitlines()[0].strip()
            m = re.match(r"module\s+(github\.com/oekit/(\S+))", first)
            if not m:
                continue
            module_path = m.group(1)
            parts = module_path.split("/")
            if len(parts) < 4:
                continue          # github.com/oekit/<repo> ⇒ 根模块，不是嵌套模块
            # 前缀 = 父仓名之后的**完整子路径**
            prefixes[module_path] = "/".join(parts[3:])
    return prefixes


def check_nested_floors_of_other_repo():
    """各仓 require **别仓嵌套模块**时，地板不得落后该嵌套模块的远端最新 tag。

    为什么要有这道门禁（oekit/oekit#104，2026-11-02）：
    三道既有门禁都**看不见**这一类——
      ① check_nested_module_floors 只管「子→父」（嵌套模块 require 父仓）；
      ② align_floors.py 的正则只匹配根模块形态（那是整改工具，本轮才扩）；
      ③ 阶段 6 只核对 BOM 快照的新鲜度。
    ⇒ 2026-11-02 实测出 **13 处**这类漂移（nacosimpl v0.1.0→v0.1.1、
    hlog v0.1.4→v0.1.6 …，跨 6 仓）**全靠人翻出来**——与 #102 的 18 处同族。

    口径：比的是**该嵌套模块**的远端最新 tag（形如 `hlog/v0.1.6`），
    **不是**父仓的根 tag——两者的版本号空间不同（logx 根是 v0.8.0，
    logx/hlog 是 v0.1.6）。

    ⚠️ **判据独立于 align_floors.py**：这里自己解析 require 行、自己按前缀
    过滤远端 tag、自己按数字比版本 ⇒ 三处逻辑都不与被检对象共享。
    """
    prefixes = _nested_tag_prefixes()
    if not prefixes:
        check(False, "读不到任何嵌套模块的 module 路径（嵌套地板判据形同虚设）")
        return

    def latest_nested_of(module_path: str) -> str | None:
        prefix = prefixes.get(module_path)
        if prefix is None:
            return None
        repo = module_path.split("/")[2]
        try:
            tags = remote_tags(repo)
        except RemoteReadError:
            return None          # 读不到由 remote_latest 口径记为漂移
        pat = re.compile(rf"^{re.escape(prefix)}/v(\d+)\.(\d+)\.(\d+)$")
        best = None
        for t in tags:
            m = pat.match(t)
            if not m:
                continue
            ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if best is None or ver > best:   # **按数字**比，非字符串排序
                best = ver
        return f"v{best[0]}.{best[1]}.{best[2]}" if best else None

    for repo in MODULES:
        # ★ 数据源 = origin/main（#246：同 check_repo_go_mod_floors 的口径——
        #   本地工作树陈旧 ⇒ 假红一片还挂着「去抬地板」的建议）。读不到 ⇒
        #   「未核对」判红（check_repo_go_mod_floors 已逐仓点名，不重复报）。
        root = _origin_main_tree(repo, repo_dir(repo, BASE))
        if root is None:
            continue
        for msg in scan_repo_nested_floors(repo, root, latest_nested_of):
            check(False, f"{msg}（跑 align_floors.py --plan 抬升）")

check_nested_floors_of_other_repo()

# ---------- BOM 兼容地板门禁（oekit/oekit#163） ----------
# 背景：oekit/oekit go.mod 顶部自称「矩阵兼容快照的地板 require（勿手改/勿 tidy）」，
# 但此前没有任何工具强制各模块 ≥ 该地板——MVS 会静默抬升，各模块的编译/测试
# 基线与消费方实际拿到的版本不一致（#163 实测：BOM 已到 pkg v0.35.2，而
# data/redisx/timertask/httpx 的 require 停在 v0.31.0、middleware v0.32.0）。
# 两道判据：
#   1) 矩阵内：各模块仓**每个** go.mod 的 oekit require ≥ BOM 地板；
#   2) 消费矩阵（oagentkit）：各仓 go.mod 的 oekit require ≥ BOM 地板
#      （读远端 raw go.mod——不为此克隆 14 个仓；404 = 无 go.mod，跳过）。
# 与既有地板门禁的分工：既有三道比「远端最新 tag」（每次发版都会红一次，
# 提示跟上），本门禁比「BOM 快照地板」——BOM 是发布列车对齐的**兼容下限**，
# 低于它 = 消费方按 BOM 组合时拿到该模块从未测过的依赖组合。

# 2026-11-08 gh repo list oagentkit 快照（非归档 Go 仓）；oagentkit 新增仓后更新此清单
OAGENTKIT_REPOS = [
    "agent", "agentkit", "guardx", "llm", "mcpkit", "obs", "promptx",
    "queue", "rag", "runtime", "skillkit", "textx", "toolgate", "tools",
]


def parse_bom_floors(gomod_text: str) -> dict[str, str]:
    """从 BOM go.mod 文本提取地板 {oekit 后完整路径: version}。纯函数（自测用）。"""
    out: dict[str, str] = {}
    for m in re.finditer(r"^\tgithub\.com/oekit/(\S+) (v\d+\.\d+\.\d+)$", gomod_text, re.M):
        out[m.group(1)] = m.group(2)
    return out


def _iter_oekit_requires(text: str):
    """go.mod 文本里每条 oekit require → (完整 module, oekit 后路径, 版本)。

    复用根模块/嵌套模块两个既有正则（两段式形态见上方 ⚠️ 注释）。
    """
    for m in _FLOOR_REQ_RE.finditer(text):
        yield m.group(1), m.group(2), m.group(3)
    for m in _NESTED_FLOOR_REQ_RE.finditer(text):
        yield m.group(1), f"{m.group(2)}/{m.group(3)}", m.group(4)


def scan_bom_floor_violations(floors: dict[str, str], gomod_texts: dict[str, str]) -> list[str]:
    """{label: go.mod 文本} 中所有低于 BOM 地板的 oekit require。纯函数（自测用）。"""
    out: list[str] = []
    for label, text in sorted(gomod_texts.items()):
        for full, path, ver in _iter_oekit_requires(text):
            floor = floors.get(path)
            if floor is None:
                continue          # BOM 未收录（非矩阵模块）⇒ 不判
            # 两个正则都只认三段式 v\d+.\d+.\d+ ⇒ parse_semver 必成功，无需判 None
            if parse_semver(ver) < parse_semver(floor):   # 按元组数字比，v0.9.0 < v0.10.0
                out.append(f"{label}: {full} {ver} 低于 BOM 地板 {floor}")
    return out


class _RawGoModUnavailable(Exception):
    """远端 go.mod 读取失败（非 404）——门禁不可用，判红。"""


def _fetch_raw_gomod(org: str, repo: str) -> str | None:
    """读 {org}/{repo}@main 的 go.mod 文本；404 → None（无 go.mod，非漂移）。"""
    import urllib.error
    import urllib.request
    url = f"https://raw.githubusercontent.com/{org}/{repo}/main/go.mod"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise _RawGoModUnavailable(f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise _RawGoModUnavailable(e) from e


def check_bom_floor_gate():
    """矩阵内 + oagentkit 消费方的 oekit require ≥ BOM 地板。"""
    bom = Path(BASE) / "oekit" / "go.mod"
    try:
        floors = parse_bom_floors(bom.read_text())
    except FileNotFoundError as e:
        check(False, gate_unavailable("BOM 地板门禁", e))
        return
    if not floors:
        check(False, "BOM 地板门禁不可用：oekit/oekit go.mod 解析出 0 条地板——地板形同虚设")
        return
    for repo in MODULES:
        try:
            root = repo_dir(repo, BASE)
        except FileNotFoundError:
            continue          # 仓检出失败已在阶段 2 记为漂移，不重复记
        texts = {str(g.relative_to(root)): g.read_text()
                 for g in sorted(root.glob("**/go.mod"))
                 if ".git" not in g.parts and "vendor" not in g.parts}
        for msg in scan_bom_floor_violations(floors, texts):
            check(False, f"{repo}/{msg}（跑 align_floors.py --plan 抬升）")
    for repo in OAGENTKIT_REPOS:
        try:
            text = _fetch_raw_gomod("oagentkit", repo)
        except _RawGoModUnavailable as e:
            check(False, gate_unavailable(f"oagentkit/{repo} go.mod", e))
            continue
        if text is None:
            continue          # 文档/站点仓，无 go.mod
        for msg in scan_bom_floor_violations(floors, {repo: text}):
            check(False, f"oagentkit/{msg}（oagentkit 侧跑 bump_dep_floors 追平）")


check_bom_floor_gate()

# ---------- 测试覆盖报警（oekit#157 裁决：报警输出，不进门禁退出码） ----------
# 裁决口径：测试文件数 / 生产文件数 < 0.5 → **报告里 ⚠ 一行**；不上 check()
# （覆盖治理走提醒不走阻断——kitex-examples 这类示例仓靠阻断会永久红）。


def warn_test_coverage():
    for repo in MODULES:
        try:
            root = repo_dir(repo, BASE)
        except FileNotFoundError:
            continue
        def _ok(p) -> bool:
            return ".git" not in p.parts and "vendor" not in p.parts and "testdata" not in p.parts
        prod = [p for p in root.rglob("*.go") if _ok(p) and not p.name.endswith("_test.go")]
        test = [p for p in root.rglob("*_test.go") if _ok(p)]
        if prod and len(test) < 0.5 * len(prod):
            print(f"⚠ [覆盖报警] {repo}: 测试文件 {len(test)} / 生产文件 {len(prod)} < 0.5"
                  f"（oekit#157，报警不进门禁）")


warn_test_coverage()

# ---------- dependabot 目录存在性门禁（matrix-tools#68，承接 data#39） ----------
# 背景与判据的完整说明在 scripts/dependabot_dirs.py（单一事实源，附自测）。
# 这里只做「取各仓 dependabot.yml → 交给判据 → 塞进 issues」。

def check_dependabot_directories():
    """dependabot.yml 里的 gomod 目录必须存在且含 go.mod。"""
    from dependabot_dirs import check_dirs  # noqa: E402 本地模块，与其他脚本同目录

    for repo in MODULES:
        root = repo_dir(repo, BASE)
        dep = root / ".github" / "dependabot.yml"
        if not dep.exists():
            continue
        for msg in check_dirs(root, dep.read_text(encoding="utf-8")):
            check(False, msg)


check_dependabot_directories()

# ---------- GitHub Release 对账 + oekit BOM 新鲜度（.github#8） ----------
# Release parity：每个 tag 对应一个 Release（notes=CHANGELOG 小节 + 该 tag 矩阵地板），
# `releases/latest` 因此全矩阵可解析。
#
# 匿名 REST 回退（公开仓可行）已不够用：Actions runner 共享出口 IP，匿名配额
# 只有 60 次/小时，逐仓一查就打满，于是全矩阵一起「读不到」——门禁看起来绿，
# 实际一仓没查（#26：23/24 仓被静默跳过，报告照样进 issue）。audit.yml 因此
# 必须注入 GH_TOKEN。跳过不红（本地跑才是硬门禁），但必须**汇总成一行**：
# 23 条同形 ⚠ 会把真正的漂移条目淹没在噪音里。
progress("阶段 5/7 GitHub Release 对账（24 仓）")
try:
    from gen_releases import missing_releases  # noqa: E402

    rel_missing, rel_skipped = missing_releases(list(MODULES))
    for repo, tags in rel_missing.items():
        check(False, f"{repo}: {len(tags)} 个 tag 缺 GitHub Release（跑 gen_releases.py --apply）：{tags[:3]}…")
    if rel_skipped:
        fused = sum(1 for s in rel_skipped if "限速熔断" in s)
        extra = f"，其中 {fused} 仓因限速熔断**未发起请求**" if fused else ""
        print(f"  ⚠ release 对账：{len(rel_skipped)}/{len(MODULES)} 仓未取到 Release 列表"
              f"——这部分本门禁无结论（CI 需注入 GH_TOKEN，见 audit.yml）{extra}")
        for s in rel_skipped[:5]:
            print(f"      · {s}")
        if len(rel_skipped) > 5:
            print(f"      · …… 另 {len(rel_skipped) - 5} 仓同类")
except Exception as e:  # noqa: BLE001
    # ⚠️ **不可用 ⇒ 判红**（实测 2026-11-03）：旧写法只 ⚠ ⇒ 这道门禁**整条消失**
    # （读不到 = 没结论），而报告照样印「✓ 矩阵审计通过」——假绿。
    issues.append(gate_unavailable("release 对账", e))

# tag 快照 CHANGELOG 门禁（matrix-tools#234）：tag 打在 CHANGELOG 提交之前 ⇒
# 快照顶部停在上一版 ⇒ 被钉版本无文档。main 分支看起来永远正常，逃逸只在
# tag 快照这一面——2026-11-06 抽验 oagentkit 系四模块全中。
progress("阶段 5.5/7 tag 快照 CHANGELOG（每仓最新顶层+嵌套 tag）")
try:
    r = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "changelog_tag_snapshot_gate.py")],
        capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        progress("tag 快照 CHANGELOG：全部含本版段")
    else:
        # 退出码契约由生产方定义：1=真漂移；2=读不到（判不了 ≠ 通过，#79）
        #
        # ★ **只有真漂移行才算漂移**（matrix-tools#247）：门禁的输出里，
        #   「· xxx 在历史逃逸豁免名单」是**提示**（tag 快照不可变的历史存量，
        #   门禁自己已判定为豁免），不是漂移。旧写法把**每一行非空输出**都
        #   当 issues ⇒ 2026-11-08 实测：报告里的「23 项漂移」有 3 项是这类
        #   豁免提示 ⇒ **报告在报不存在的问题**，真漂移也被噪音淹没。
        #   口径与同文件「release 对账」的跳过提示一致（那边是 print 提示，
        #   不进 issues）⇒ 提示归提示，漂移归漂移。
        drift: list[str] = []
        notes: list[str] = []
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("·"):
                notes.append(s)                      # 豁免提示：不是漂移
            elif s.startswith("❌ tag 快照 CHANGELOG 漂移"):
                notes.append(s)                      # 汇总头：细节行已逐条列出
            elif s.startswith("❌"):
                drift.append(s[1:].strip())
            else:
                drift.append(s)                      # ⚠ 判不了 ⇒ 也算红
        for n in notes:
            print(f"      · {n}")
        for d in drift:
            issues.append(f"tag-snapshot: {d}")
        if r.returncode not in (1, 2):
            issues.append(f"tag-snapshot: 门禁异常退出码 {r.returncode}")
except Exception as e:  # noqa: BLE001
    # ⚠️ **不可用 ⇒ 判红**（口径同 release 对账：判不了 ≠ 通过）
    issues.append(gate_unavailable("tag 快照 CHANGELOG", e))

# oekit BOM（github.com/oekit/oekit）新鲜度：快照必须与各仓远端 tag 一致，
# 否则 `go get github.com/oekit/oekit@latest` 拉到的是旧组合。
try:
    progress("阶段 6/7 oekit BOM 新鲜度（子进程）")
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "gen_matrix_go.py"), "--check"],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    # 退出码契约由**生产方**定义（gen_matrix_go.EXIT_*），这里不靠猜消息文本：
    # 0=一致 / 1=真漂移（产物陈旧）/ 2=读不到远端 tag（判不了 ≠ 通过，#79）。
    # 「读不到」与「有漂移」混在一起报，一次网络抖动就会变成一条假的矩阵漂移。
    _kind, _msg = gen_matrix_go.classify_check(r.returncode, out)
    if _kind != "ok":
        issues.append(_msg)
except Exception as e:  # noqa: BLE001
    # ⚠️ **不可用 ⇒ 判红**（口径与 classify_check 一致：判不了 ≠ 通过）
    issues.append(gate_unavailable("BOM 检查", e))

# ---------- 报告 ----------
progress("阶段 7/7 出报告")
#
# 覆盖证据（#79）：结论行「✓ 矩阵审计通过」**不能**自证「真的读了远端事实」——
# 读不到时这句话长得一模一样（#26：23/24 仓被跳过，报告仍显示通过）。
# 所以结论行前补一行可核对的读数：读到几个 / 有几个降级或跳过。
_read_ok = sum(1 for v in REMOTE_READ_LOG.values() if v == "ok")
_read_fail = sum(1 for v in REMOTE_READ_LOG.values() if v == "failed")
print(f"远端 tag 读取：{_read_ok}/{len(MODULES)} 仓成功"
      f"（{_read_fail} 读失败）")
if _stale_local_main:
    # matrix-tools#246：本地 main 落后的仓。地板判据已改读 origin/main 不受影响
    # （这里只提示），但**其余读本地工作树的判据**（module 行/README/AGENTS…）
    # 仍会被陈旧 clone 带偏 ⇒ 提醒先 pull，别把陈旧读数当成事实。
    print(f"本地 main 落后 origin/main：{len(_stale_local_main)} 仓"
          f"（{sorted(_stale_local_main)}）——地板判据已改读远端不受影响；"
          f"其余读本地工作树的判据可能过时，建议先 git pull --ff-only（matrix-tools#246）")
if issues:
    print(f"发现 {len(issues)} 项漂移：")
    for i in issues:
        print(f"  ❌ {i}")
    sys.exit(1)
print(f"✓ 矩阵审计通过（{len(MODULES)} 模块 + 站点）")
