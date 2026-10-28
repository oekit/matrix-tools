#!/usr/bin/env python3
"""DESC 声明的「N 合 1」必须与该仓**实际的公开子包**一致（#117）。

**为什么要有这道门禁**：pkg 的对外口径在 matrix.py 的 DESC 里写的是
「微工具集 23 合 1（…23 个包名…）」，而 pkg 实际已经有 **24** 个公开子包
（多出的是 v0.27.0 起的 `shellx`）⇒ **同一模块两套口径**。

**为什么现有门禁抓不到**：矩阵表新鲜度检查（audit.py）比的是「各仓 README 的
矩阵表 == DESC」⇒ 两边**一起过期**时它照样通过 ⇒ 这类漂移必须拿**仓里的真实
目录**当第三只眼。

本自测断言两件事：
  1. DESC 里声明的合 1 数字 == 该仓实际公开子包数；
  2. DESC 里列出的包名集合 == 该仓实际公开子包名集合（顺序不计）。

**无副作用、不联网**：只读本地仓目录；矩阵检出根由 MATRIX_BASE / 脚本位置推导。

用法：python3 scripts/desc_count_selftest.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# matrix.py 在仓根（不在 scripts/）⇒ sys.path 要指向仓根
sys.path.insert(0, str(HERE.parent))
from matrix import DESC, MODULES, repo_dir  # noqa: E402

# 「N 合 1（包1/包2/…；后续说明）」⇒ 取 N 与括号里的包名清单
# 注意：包名清单到「；」才结束（「）」在整段描述之后）⇒ **不能**要求闭合括号，
# 否则整条匹配失败 ⇒ 检查被静默跳过（这正是本自测自己踩过的坑）。
CLAIM_RE = re.compile(r"(\d+)\s*合\s*1\s*（([^；）]*)")

SKIP_DIRS = {"internal", "testdata", "vendor", "node_modules"}


def public_subpackages(repo: str) -> set[str]:
    """仓里的公开子包（有 .go 文件、且不是 internal/testdata 之类）。"""
    d = repo_dir(repo)
    out: set[str] = set()
    for sub in d.iterdir():
        if not sub.is_dir():
            continue
        if sub.name in SKIP_DIRS or sub.name.startswith((".", "_")):
            continue
        if any(sub.glob("*.go")):
            out.add(sub.name)
    return out


def main() -> int:
    failures: list[str] = []

    for repo in MODULES:
        desc = DESC.get(repo, "")
        m = CLAIM_RE.search(desc)
        if not m:
            continue  # 该仓的 DESC 不声明「N 合 1」形态 ⇒ 不适用
        claimed_n = int(m.group(1))
        claimed_names = {x.strip() for x in m.group(2).split("/") if x.strip()}
        actual = public_subpackages(repo)
        if not actual:
            continue  # 读不到仓 ⇒ 不误报（读不到 ≠ 事实为空）
        if claimed_n != len(actual):
            failures.append(
                f"{repo}: DESC 声明「{claimed_n} 合 1」，实际公开子包 {len(actual)} 个"
                f"（{sorted(actual)}）⇒ 同一模块两套口径")
        if claimed_names and claimed_names != actual:
            only_desc = sorted(claimed_names - actual)
            only_repo = sorted(actual - claimed_names)
            failures.append(
                f"{repo}: DESC 的包名清单与实际不一致"
                f"（只在 DESC：{only_desc}；只在仓库：{only_repo}）")

    if failures:
        print("✗ DESC 子包口径自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ DESC 子包口径自测通过：声明的合 1 数字与包名清单都等于实际公开子包")


if __name__ == "__main__":
    main()
