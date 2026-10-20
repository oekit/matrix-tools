#!/usr/bin/env python3
"""各仓 AGENTS.md 首行的 DESC 行同步到 matrix.py 的 DESC 真相源。

## 为什么要有这个（2026-10-20 实测）

AGENTS.md 第 3 行是本仓的一句话说明。**约定**：它是 matrix.py 的 `DESC` 的
**前缀**（可以截断，但不能改写——截断点必须在 DESC 内）。

实测发现 3 个仓（data / core / pkg）的这行是**陈旧快照**——DESC 更新后没人
同步，AGENTS.md 里留着一段旧的能力描述。同一类错误还有 hertz：它的 AGENTS.md
写着 `WithReadyAddr`，而 hertz **没有**这个导出函数（那是 kitex 的）。

为什么值得钉死：AGENTS.md 是 AI Agent 判断「这个仓能做什么、怎么用」的入口，
它与矩阵真相源说法不一致 = 组织里有两个事实源。钉成「必须是 DESC 的前缀」后，
漂移在改 DESC 的那一刻就能被发现，而不是靠人想起来同步。

用法：
    python3 scripts/refresh_agents_desc.py            # 打印哪些仓需要同步
    python3 scripts/refresh_agents_desc.py --apply    # 直接改（本地工作树）

写多仓 PR 请走 doc_wave.py --agents（它负责开 PR + 合并）。
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from matrix import DESC, MODULES, repo_dir  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
APPLY = "--apply" in sys.argv

# AGENTS.md 的首行说明：正文前 8 行里第一个以「> 」开头的引用行。
_HEADER_RE = re.compile(r"^> (?!\*\*|当前版本)(.+)$", re.M)


def header_line(text: str) -> str | None:
    """取 AGENTS.md 的 DESC 行内容（不含前导「> 」）。"""
    head = text.splitlines()[:8]
    for l in head:
        if l.startswith("> ") and not l.startswith("> **") and not l.startswith("> 当前版本"):
            return l[2:].strip()
    return None


def sync_repo(repo: str, base: str | None = None, write: bool = False) -> list[str]:
    """把该仓 AGENTS.md 的 DESC 行同步到真相源；返回变更文件列表。

    只在**当前行不是 DESC 的前缀**时才改（截断过的仓保持截断，不强行补全）。
    """
    base = base or BASE
    d = repo_dir(repo, base)
    agents = d / "AGENTS.md"
    if not agents.exists():
        return []
    desc = DESC.get(repo, "").strip()
    if not desc:
        return []
    text = agents.read_text(encoding="utf-8")
    cur = header_line(text)
    if cur is None or desc.startswith(cur):
        return []
    # 只换那一行，其余内容（含人工维护段）不动
    lines = text.splitlines(keepends=True)
    for i, l in enumerate(lines[:8]):
        if l.startswith("> ") and not l.startswith("> **") and not l.startswith("> 当前版本"):
            lines[i] = f"> {desc}\n"
            break
    new = "".join(lines)
    if new == text:
        return []
    if write:
        agents.write_text(new, encoding="utf-8")
    return ["AGENTS.md"]


def main() -> None:
    changed = []
    for repo in MODULES:
        files = sync_repo(repo, BASE, write=APPLY)
        if files:
            changed.append(f"{repo}（{', '.join(files)}）")
            print(f"{'✓ 已改' if APPLY else '· 待改'} {repo}")
    if not changed:
        print("✓ 全矩阵 AGENTS.md 的 DESC 行都与真相源一致")
    elif not APPLY:
        print(f"\n{len(changed)} 仓待同步：{'; '.join(changed)}\n"
              f"加 --apply 直接改本地，或走 doc_wave.py --agents")


if __name__ == "__main__":
    main()
