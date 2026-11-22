#!/usr/bin/env python3
"""子包口径的**提取层**——「某模块有多少公开子包」这件事**只算一次**，所有判定层共用。

## 为什么要有这一层（2026-11-23 实测）

「N 合 1」这个数字在矩阵里有**三张脸**：

  1. `matrix.py` 的 `DESC`（真相源）；
  2. **各仓的真实目录**（物理事实）；
  3. **站点手写面**（`llms.txt` / `docs/guide/*.md`——它们**没有生成器**）。

原先只有 (1)↔(2) 被钉住（`desc_count_selftest.py`），**(3) 谁都不管**：
站点里写「38 合 1」还是「23 合 1」，没有任何门禁会红 ⇒ pkg 加到第 39 个子包时，
真相源会红、**站点照旧挂着旧数字**（公开面长期过期，且没人知道）。

## 形状（与 oagentkit 侧同形）

  **提取层**（本文件）= 只产出「数字 + 出处」，不复述任何判定；
  **判定层**（`desc_count_selftest.py` / `verify_site_copy.py`）= 只比对，不自己算。

一处提取、两处判定 ⇒ 两张脸不可能各算各的（**声明与事实的分叉从根上被掐断**）。

## 「读不到 ≠ 事实为空」

兄弟仓读不到时**返回 None（跳过）**，绝不退化成「0 个子包」——否则独立 clone 的
CI 会恒红，且报的是假问题（矩阵自测里已有变异自证钉这条，见 `desc_count_selftest.py --mutate`）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from matrix import DESC, MODULES, repo_dir  # noqa: E402

# 「N 合 1（包1/包2/…；后续说明）」⇒ 取 N 与括号里的包名清单
# 注意：包名清单到「；」才结束（「）」在整段描述之后）⇒ **不能**要求闭合括号，
# 否则整条匹配失败 ⇒ 检查被静默跳过（这正是本自测自己踩过的坑）。
CLAIM_RE = re.compile(r"(\d+)\s*合\s*1\s*（([^；）]*)")

# 「N 合 1」里的 N（站点/文面侧的句式，比 CLAIM_RE 宽：不要求后接括号）
COUNT_RE = re.compile(r"(\d+)\s*合\s*1")

SKIP_DIRS = {"internal", "testdata", "vendor", "node_modules"}


def declared_claims() -> dict[str, dict]:
    """各模块 DESC 里声明的「N 合 1」数字与包名清单（声明面）。"""
    out: dict[str, dict] = {}
    for repo in MODULES:
        m = CLAIM_RE.search(DESC.get(repo, ""))
        if not m:
            continue  # 该仓的 DESC 不声明「N 合 1」形态 ⇒ 不适用
        names = {x.strip() for x in m.group(2).split("/") if x.strip()}
        out[repo] = {"n": int(m.group(1)), "names": names}
    return out


def public_subpackages(repo: str, base: Path | None = None) -> set[str] | None:
    """仓里的公开子包（有 .go 文件、且不是 internal/testdata 之类）。

    **读不到仓时返回 None**（不是空集）——「读不到 ≠ 事实为空」：
    matrix-tools 在 CI 里是**独立 clone**（兄弟仓不在旁边）⇒ 此时应当**跳过**
    而不是「判定为 0 个子包 ⇒ 判红」（否则 CI 恒红，且报的还是假问题）。
    """
    try:
        d = repo_dir(repo, base=str(base) if base else None)
    except Exception:  # noqa: BLE001 — 仓读不到（独立 clone / 布局不同）⇒ 跳过
        return None
    out: set[str] = set()
    try:
        children = list(d.iterdir())
    except Exception:  # noqa: BLE001
        return None
    for sub in children:
        if not sub.is_dir():
            continue
        if sub.name in SKIP_DIRS or sub.name.startswith((".", "_")):
            continue
        if any(sub.glob("*.go")):
            out.add(sub.name)
    return out


def truth(base: Path | None = None) -> dict[str, dict]:
    """每个声明了「N 合 1」的模块：期望包数 + **出处**（谁算出来的）。

    期望值优先取**仓里的真实公开子包数**（物理事实）；仓读不到时退回 DESC 的
    声明值，并把出处写清楚——**读不到 ≠ 事实为空**，但也**不许静默**：
    调用方必须把出处一起打出来，让人知道这一条没被物理事实核对过。
    """
    out: dict[str, dict] = {}
    for repo, claim in declared_claims().items():
        actual = public_subpackages(repo, base=base)
        if actual is None:
            out[repo] = {
                "expected": claim["n"],
                "source": "DESC 声明（兄弟仓未检出，本条未与物理事实核对）",
                "declared_n": claim["n"],
                "actual_n": None,
                "names": claim["names"],
            }
        else:
            out[repo] = {
                "expected": len(actual),
                "source": "仓内真实公开子包",
                "declared_n": claim["n"],
                "actual_n": len(actual),
                "names": claim["names"],
            }
    return out
