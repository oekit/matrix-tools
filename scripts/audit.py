#!/usr/bin/env python3
"""全矩阵审计：module 行 / 残留引用 / README 门面 / AGENTS / CHANGELOG / 安装行版本 / 矩阵表新鲜度 / 站点。
用法：
  python3 scripts/audit.py               # 本地检出（MATRIX_BASE 或默认路径）
  MATRIX_BASE=repos python3 scripts/audit.py --remote-tags   # Actions 克隆布局
输出：stdout 报告；发现漂移时 exit 1（供 CI 判定）。"""
from __future__ import annotations
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

# ---------- 站点仓 ----------
try:
    site = repo_dir(SITE_REPO, BASE)
    llms = site / "docs" / "public" / "llms.txt"
    check(llms.exists(), "site: 缺 docs/public/llms.txt")
    idx = site / "docs" / "index.md"
    check(idx.exists() and "23 个独立发版" in idx.read_text(), "site: 首页计数非 23")
    r = subprocess.run(["grep", "-rl", "--exclude-dir=.git",
                        "--exclude-dir=dist", "--exclude-dir=cache",
                        "-e", "github.com/yi-nology", "-e", "github.com/openekit", "."],
                       cwd=site, capture_output=True, text=True)
    if r.stdout.strip():
        issues.append(f"site: 残留旧引用 {r.stdout.strip().splitlines()[:3]}")
    for g in ["getting-started", "logging", "errors", "http-service",
              "rpc-service", "observability", "migration", "ai-agent"]:
        check((site / "docs" / "guide" / f"{g}.md").exists(), f"site: 缺指南 {g}")
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

# ---------- 报告 ----------
if issues:
    print(f"发现 {len(issues)} 项漂移：")
    for i in issues:
        print(f"  ❌ {i}")
    sys.exit(1)
print(f"✓ 矩阵审计通过（{len(MODULES)} 模块 + 站点）")
