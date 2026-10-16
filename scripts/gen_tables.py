#!/usr/bin/env python3
"""重新生成各仓 README 的「矩阵总览」表；缺段落的仓自动在文末追加。
用法：python3 scripts/gen_tables.py [--push]"""
from __future__ import annotations
import os
import re
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, DESC, repo_dir, sibling_deps, sh  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
PUSH = "--push" in sys.argv

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

NEW_GLOBAL = render_global()

for repo in MODULES:
    d = repo_dir(repo, BASE)
    readme = d / "README.md"
    t = readme.read_text()
    # 行级扫描定位全部矩阵段（标题行 + 其后连续的空行/表格行）。
    # 此前用非贪婪正则只替换第一段，历史遗留的旧段永远残留
    # （2026-10-16 发现 22 仓均有重复矩阵段），改为全删后写入一份。
    lines = t.splitlines(keepends=True)
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
    if spans:
        # 第一段位置写入新生成的全局视角表，其余段整段删除
        s0, e0 = spans[0]
        drop: set[int] = set()
        for s, e in spans[1:]:
            drop.update(range(s, e))
        out = []
        for x, line in enumerate(lines):
            if x in drop:
                continue
            if s0 <= x < e0:
                if x == s0:
                    out.append(render_global() + "\n")
                continue
            out.append(line)
        t = "".join(out)
    else:
        t = t.rstrip() + "\n\n" + render_table_for(repo) + "\n"
    assert t.count("## 矩阵总览") == 1, f"{repo}: 矩阵段去重失败"
    readme.write_text(t)
    print(f"✓ oekit/{repo}", flush=True)

if PUSH:
    import matrix
    for repo in MODULES:
        d = repo_dir(repo, BASE)
        if sh(["git", "status", "--porcelain", "README.md"], d).stdout.strip():
            sh(["git", "add", "README.md"], d)
            sh(["git", "commit", "-q", "-m", "docs: 矩阵总览表重新生成（matrix-tools）"], d)
            sh(["git", "push", "-q"], d)
            print(f"⬆ oekit/{repo}")
print("完成")
