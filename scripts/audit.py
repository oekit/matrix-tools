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

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, SITE_REPO, STALE_RE, repo_dir, module_line, sibling_deps, latest_tag  # noqa: E402

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


# ---------- 模块仓 ----------
for repo in MODULES:
    d = repo_dir(repo, BASE)
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
    idx = site / "docs" / "index.md"
    check(idx.exists() and f"{len(MODULES)} 个独立发版" in idx.read_text(), f"site: 首页计数非 {len(MODULES)}")
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
except FileNotFoundError as e:
    issues.append(f"site: {e}")

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
            for req in re.finditer(r"^\t(github\.com/oekit/(\S+)) (v\S+)$", text, re.M):
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
# `releases/latest` 因此全矩阵可解析。CI 里 GITHUB_TOKEN 只对本仓有效时自动降级为
# 匿名 REST（公开仓可行），限速失败则跳过不红——本地跑才是硬门禁。
try:
    from gen_releases import missing_releases  # noqa: E402

    rel_missing, rel_skipped = missing_releases(list(MODULES))
    for repo, tags in rel_missing.items():
        check(False, f"{repo}: {len(tags)} 个 tag 缺 GitHub Release（跑 gen_releases.py --apply）：{tags[:3]}…")
    for s in rel_skipped:
        print(f"  ⚠ release 对账跳过：{s}")
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
