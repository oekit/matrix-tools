#!/usr/bin/env python3
"""重新生成各仓 README 的「矩阵总览」表；缺段落的仓自动在文末追加。
用法：python3 scripts/gen_tables.py [--push]

导入安全：本模块被 audit.py / doc_wave.py import 复用渲染函数——写入逻辑
必须在 main() 里，不能落在模块顶层（2026-10-19 修复：顶层循环曾使每次
audit 都静默改写全矩阵 README、矩阵表新鲜度检查自我满足）。"""
from __future__ import annotations
import os
import re
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, DESC, repo_dir, ensure_clean_all, sibling_deps, sh  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
PUSH = "--push" in sys.argv

# 门面 BOM 仓：它有根 go.mod，但**不是矩阵模块**（只是各模块版本地板的快照）。
BOM_REPO = "oekit"

# 站点文案里表示「模块数量」的单位 → 口径键。**单一真相**：门禁
# verify_site_copy.py 与文案生成器都从这里取，改一处全生效。
# 只认这几个精确措辞：站点里还有「23 个基础工具包」（pkg 内部包数）这类数字，
# 模糊匹配会误判（误报多的门禁会被绕过）。
COUNT_UNITS = {
    "独立发版的 Go 模块仓库": "matrix_modules",
    "独立发版的 Go 模块": "matrix_modules",
    "独立发版的仓库": "matrix_modules",
    "活跃模块仓库": "matrix_modules",
    "活跃模块": "matrix_modules",
    "模块": "matrix_modules",
    "嵌套模块": "nested_modules",
    "发版单元": "release_units",
}


def count_units(repos_dir: str | None = None) -> dict:
    """各口径的计数。

    `repos_dir` 给了 → **独立 oracle**：直接扫文件系统里的 go.mod，不看任何生成物。
    这是「声明的模块数是否还对得上现实」的判据；不给则只给声明口径。

    三个口径不一样，别选一个当「唯一正确值」：
      根矩阵模块数 = len(MODULES)（站点文案用这个，与「矩阵总览」表同口径）
      根 go.mod 数  = 25（24 矩阵模块 + 门面 BOM 仓 oekit）
      嵌套模块数    = 7（深度 ≥2 的 go.mod，各自独立发版）
      发版单元总数  = 根矩阵模块 + 嵌套模块（不含门面 BOM 仓）
    """
    counts = {
        "matrix_modules": len(MODULES),
        "root_gomods": None,
        "nested_modules": None,
        "total_gomods": None,
        "release_units": None,
    }
    if not repos_dir:
        return counts
    from pathlib import Path
    base = Path(repos_dir).resolve()
    roots = [p for p in sorted(base.iterdir()) if p.is_dir() and (p / "go.mod").is_file()]
    nested = sum(1 for r in roots for g in r.glob("*/go.mod") if ".git" not in g.parts)
    counts["root_gomods"] = len(roots)
    counts["nested_modules"] = nested
    counts["total_gomods"] = len(roots) + nested
    counts["release_units"] = counts["matrix_modules"] + nested
    return counts


def render_count_sentence(counts: dict) -> str:
    """规范计数句——文案要改时照抄这句，不用自己算。

    口径齐全时把三个数都写出来（读者不必猜哪个是哪个）；缺嵌套口径时只写根模块数。
    """
    n = counts["matrix_modules"]
    if counts.get("release_units") is None:
        return f"{n} 个独立发版的 Go 模块"
    return (f"{n} 个独立发版的 Go 模块（根模块），另有 {counts['nested_modules']} 个嵌套模块"
            f"各自独立发版，合计 {counts['release_units']} 个发版单元")

def deps_link(repo: str) -> str:
    deps = sibling_deps(repo, BASE)
    return "、".join(f"[{d}](https://github.com/oekit/{d})" for d in deps) or "—"

def render_table_for(repo: str) -> str:
    """渲染本仓视角的矩阵总览段（供 audit 比对）。"""
    lines = ["| 模块 | 内容 | 矩阵内依赖 |", "|---|---|---|"]
    for r in MODULES:
        link = f"[{r}](https://github.com/oekit/{r})"
        deps = "、".join(f"[{d}](https://github.com/oekit/{d})" for d in sibling_deps(r, BASE)) or "—"
        from matrix import DESC as D
        lines.append(f"| {link} | {D[r]} | {deps} |")
    tail = ""
    if repo == "bootstrap":
        tail = "\n\n> 依赖严格自底向上；微库已于 2026-10 合并为 [pkg](https://github.com/oekit/pkg)，旧微库仓已删除。"
    return f"## 矩阵总览（{len(MODULES)} 活跃模块，自动生成）\n\n" + "\n".join(lines) + tail

def render_global() -> str:
    lines = ["| 模块 | 内容 | 矩阵内依赖 |", "|---|---|---|"]
    for r in MODULES:
        lines.append(f"| [{r}](https://github.com/oekit/{r}) | {DESC[r]} | {deps_link(r)} |")
    return f"## 矩阵总览（{len(MODULES)} 活跃模块，自动生成）\n\n" + "\n".join(lines)

def table_rows(section: str) -> str:
    return "\n".join(l for l in section.splitlines() if l.startswith("|"))


def readme_table_section(d) -> str:
    t = (d / "README.md").read_text()
    m = re.search(r"## 矩阵总览.*?(?=\n## |\Z)", t, flags=re.S)
    return m.group(0).strip() if m else ""


def table_drift(d) -> bool:
    """矩阵表是否过期（audit 同款行级比对）。"""
    return table_rows(readme_table_section(d)) != table_rows(render_global())


def apply_table(repo: str) -> bool:
    """单仓重写矩阵总览段，返回是否发生变化。"""
    d = repo_dir(repo, BASE)
    readme = d / "README.md"
    before = readme.read_text()
    # 行级扫描定位全部矩阵段（标题行 + 其后连续的空行/表格行）。
    # 此前用非贪婪正则只替换第一段，历史遗留的旧段永远残留
    # （2026-10-16 发现 22 仓均有重复矩阵段），改为全删后写入一份。
    lines = before.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("## 矩阵总览"):
            j = i + 1
            while j < len(lines) and (lines[j].strip() == "" or lines[j].startswith("|")):
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    if not spans:
        readme.write_text(before.rstrip() + "\n\n" + render_table_for(repo) + "\n")
        return True
    first_span, rest = spans[0], spans[1:]
    drop: set[int] = set()
    for s, e in rest:
        drop.update(range(s, e))
    out = []
    for x, line in enumerate(lines):
        if x in drop:
            continue
        if first_span[0] <= x < first_span[1]:
            if x == first_span[0]:
                out.append(render_global() + "\n")
            continue
        out.append(line)
    t = "".join(out)
    assert t.count("## 矩阵总览") == 1, f"{repo}: 矩阵段去重失败"
    changed = t != before
    if changed:
        readme.write_text(t)
    return changed


def main() -> None:
    ensure_clean_all([repo_dir(r, BASE) for r in MODULES], "gen_tables")
    for repo in MODULES:
        changed = apply_table(repo)
        print(f"{'✎' if changed else '='} oekit/{repo}", flush=True)

    if PUSH:
        for repo in MODULES:
            d = repo_dir(repo, BASE)
            if sh(["git", "status", "--porcelain", "README.md"], d).stdout.strip():
                sh(["git", "add", "README.md"], d)
                sh(["git", "commit", "-q", "-m", "docs: 矩阵总览表重新生成（matrix-tools）"], d)
                sh(["git", "push", "-q"], d)
                print(f"⬆ oekit/{repo}")
    print("完成")


if __name__ == "__main__":
    main()
