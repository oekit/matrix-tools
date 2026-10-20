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
    GO_FLOOR_BASE_MAX, GO_FLOOR_BUSINESS_OK, GO_FLOOR_BASE_LAYER, GO_FLOOR_EXEMPT,
)
from verify_site_copy import check_counts as site_check_counts, check_llms_coverage, check_ghost_roots, count_units  # noqa: E402

BASE = os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", ".."))
BASE = os.path.abspath(BASE)
REMOTE_TAGS = "--remote-tags" in sys.argv
EXEMPT_QUICKUSE = {"pkg", "kitex-examples"}      # pkg 全文即用法；kitex-examples 有「## 运行」
EXEMPT_TABLE = set()                              # 矩阵总览表所有仓都必须有

issues: list[str] = []


def check(cond: bool, msg: str):
    if not cond:
        issues.append(msg)


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
# 归属校验用：**全限定**的 oekit 模块路径 `github.com/oekit/<name>`。
# 刻意不认裸 `<name>/`——裸形式无法区分「别仓」与「本仓的子包」。
ORACLE_OWNPATH_RE = re.compile(r"github\.com/oekit/([a-z0-9-]+)")


def _oracle_norm(s: str) -> str:
    """归一化：去 markdown 记号与空白，只留可比的文字内容。"""
    return re.sub(r"\s+", " ", re.sub(r"[*`#>\s]+", " ", s)).strip()


def _oracle_markers(text: str) -> list[tuple[str, str, str]]:
    """扫 CHANGELOG 原文 → [(形态, 版本头, 关键文本)]。独立于解析器。"""
    out: list[tuple[str, str, str]] = []
    ver = ""
    for raw in text.splitlines():
        m = ORACLE_VERHEAD_RE.match(raw)
        if m:
            ver = m.group(1).strip()
            continue
        s = raw.strip()
        if not s:
            continue
        # (a) `### Breaking` 段标题——该版本应当有对应条目
        if s.startswith("###"):
            title = s.lstrip("#").strip()
            if ORACLE_WORD_RE.search(title):
                out.append(("section", ver, title))
            continue
        # (b) 顶格独立成行的 `**BREAKING**: ...`——必须被逐条覆盖
        if raw.startswith("**") and ORACLE_WORD_RE.search(s):
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
        for gomod in sorted(root.glob("*/go.mod")):
            sources.append((gomod.parent / "CHANGELOG.md", f"{gomod.parent.name}/"))
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


# ---------- 模块仓 ----------
for repo in MODULES:
    d = repo_dir(repo, BASE)
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
        lt = latest_tag(repo, BASE, remote=REMOTE_TAGS)
        if lt:
            check(m.group(1) == lt, f"{repo}: 安装行 {m.group(1)} ≠ 最新 {lt}（跑 refresh_versions.py）")
    else:
        print(f"[warn] {repo}: README 无安装行（仅提示）")
    # 6b) AGENTS 当前版本行（与 README 安装行同一套对齐纪律）
    agents = d / "AGENTS.md"
    ma = re.search(r"当前版本：`(v[0-9.]+)`", agents.read_text()) if agents.exists() else None
    if ma:
        lt2 = latest_tag(repo, BASE, remote=REMOTE_TAGS)
        if lt2:
            check(ma.group(1) == lt2, f"{repo}: AGENTS 当前版本 {ma.group(1)} ≠ 最新 {lt2}（跑 refresh_versions.py）")

# ---------- 门面壳同步（data 八壳 ← 源包导出符号） ----------
# 「源仓新增导出符号时本壳需同步」的跟版税此前靠人记（2026-10-16 曾漏
# pkg v0.6.0 的 BatchGetter），现为 audit 硬门禁。
from shell_sync import check_sync  # noqa: E402 延迟导入同目录脚本

for d in check_sync(BASE):
    issues.append(d)

# ---------- 站点仓 ----------
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
            lt = latest_tag(repo, BASE, remote=REMOTE_TAGS)
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


check_breaking_oracle()
check_dead_paths()

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
        for gomod in sorted(root.glob("*/go.mod")):
            sub = gomod.parent.name
            text = gomod.read_text()
            for req in re.finditer(r"^(?:\t|require )(github\.com/oekit/(\S+)) (v\S+)$", text, re.M):
                dep_repo, ver = req.group(2), req.group(3)
                if dep_repo != repo:  # 只校验对父模块的 require
                    continue
                lt = latest_tag(repo, BASE, remote=REMOTE_TAGS)
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

# ---------- GitHub Release 对账 + oekit BOM 新鲜度（.github#8） ----------
# Release parity：每个 tag 对应一个 Release（notes=CHANGELOG 小节 + 该 tag 矩阵地板），
# `releases/latest` 因此全矩阵可解析。
#
# 匿名 REST 回退（公开仓可行）已不够用：Actions runner 共享出口 IP，匿名配额
# 只有 60 次/小时，逐仓一查就打满，于是全矩阵一起「读不到」——门禁看起来绿，
# 实际一仓没查（#26：23/24 仓被静默跳过，报告照样进 issue）。audit.yml 因此
# 必须注入 GH_TOKEN。跳过不红（本地跑才是硬门禁），但必须**汇总成一行**：
# 23 条同形 ⚠ 会把真正的漂移条目淹没在噪音里。
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
except Exception as e:  # noqa: BLE001 对账不可用不阻塞其余门禁
    print(f"  ⚠ release 对账不可用：{e}")

# oekit BOM（github.com/oekit/oekit）新鲜度：快照必须与各仓远端 tag 一致，
# 否则 `go get github.com/oekit/oekit@latest` 拉到的是旧组合。
try:
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "gen_matrix_go.py"), "--check"],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        issues.append(out.splitlines()[-1] if out else "oekit: BOM 新鲜度检查失败")
except Exception as e:  # noqa: BLE001
    print(f"  ⚠ BOM 检查跳过：{e}")

# ---------- 报告 ----------
if issues:
    print(f"发现 {len(issues)} 项漂移：")
    for i in issues:
        print(f"  ❌ {i}")
    sys.exit(1)
print(f"✓ 矩阵审计通过（{len(MODULES)} 模块 + 站点）")
