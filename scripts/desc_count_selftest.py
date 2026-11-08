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


def public_subpackages(repo: str) -> set[str] | None:
    """仓里的公开子包（有 .go 文件、且不是 internal/testdata 之类）。

    **读不到仓时返回 None**（不是空集）——「读不到 ≠ 事实为空」：
    matrix-tools 在 CI 里是**独立 clone**（兄弟仓不在旁边）⇒ 此时应当**跳过**
    而不是「判定为 0 个子包 ⇒ 判红」（否则 CI 恒红，且报的还是假问题）。
    """
    try:
        d = repo_dir(repo)
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


def main() -> int:
    failures: list[str] = []
    checked = 0
    skipped: list[str] = []

    applicable = 0
    for repo in MODULES:
        desc = DESC.get(repo, "")
        m = CLAIM_RE.search(desc)
        if not m:
            continue  # 该仓的 DESC 不声明「N 合 1」形态 ⇒ 不适用
        applicable += 1
        claimed_n = int(m.group(1))
        claimed_names = {x.strip() for x in m.group(2).split("/") if x.strip()}
        actual = public_subpackages(repo)
        if actual is None:
            # 仓读不到（CI 里 matrix-tools 是独立 clone，兄弟仓不在旁边）⇒ 记账。
            # 「读不到 ≠ 事实为空」——既不当假红，也**不许**静默绿（见下）。
            skipped.append(repo)
            continue
        checked += 1
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

    # ★ 「一条都没核对」不许和「全部通过」共用 exit 0（matrix-tools#254）：
    #   CI 里 matrix-tools 是独立 clone ⇒ 事实源（兄弟仓目录）不存在 ⇒ 判据
    #   空转却打「✓ 通过」⇒ pkg 口径漂移第三次复发（23→24→26）时无人拦截。
    #   退出码 2 = 「未核对」（audit 的 0/1/2 口径）——不是 1（没有真漂移
    #   证据），也绝不能是 0。
    if not checked:
        print("✗ DESC 子包口径自测**未核对**：可核对的仓一个都没有"
              f"（DESC 声明「N 合 1」的仓有 {applicable} 个，本地全部缺检出）"
              "——判据拿不到事实源 ≠ 通过；CI 里请先把兄弟仓 checkout 到 "
              "MATRIX_BASE 下再跑（matrix-tools#254）。")
        return 2
    # ★ 覆盖度断言：核对数 == 应核对数（#254）。部分兄弟仓缺失 ⇒ 剩下的声明
    #   没核对却照样绿，正是「空跑」的缩小版 ⇒ 同样 fail-loud。
    if checked != applicable:
        print(f"✗ DESC 子包口径自测**未核对完整**：应核对 {applicable} 个仓，"
              f"实际只核对 {checked} 个（缺检出：{sorted(skipped)}）"
              "——事实源不全 ≠ 通过；补齐兄弟仓检出后重跑（matrix-tools#254）。")
        return 2
    if failures:
        print("✗ DESC 子包口径自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print(f"✓ DESC 子包口径自测通过：声明的合 1 数字与包名清单都等于实际公开子包"
          f"（{checked}/{applicable} 个声明仓全部核对）")
    return 0


def _mutate() -> int:
    """变异自证（matrix-tools#254 验收 3）：把「仓读不到 ⇒ None（跳过）」退化成
    「返回 set()（判定 0 个子包）」⇒ 在同样的空检出环境里，退出码必须从 2（未核对）
    变成 1（假红）——证明跳过语义真被依赖、且「未核对」判据抓的正是空跑这条路径。
    （防两种反方向的错：跳过退化为「判定 0 个子包 ⇒ 假红」；或未核对退化为「照旧绿」。）"""
    import importlib.util
    import os
    import tempfile

    src = (HERE / "desc_count_selftest.py").read_text(encoding="utf-8")
    anchor = "⇒ 跳过\n        return None"
    assert anchor in src, "变异锚点不存在（public_subpackages 的跳过分支被改了？）"
    mut_src = src.replace(anchor, "⇒ 跳过\n        return set()", 1)
    tmp = Path(tempfile.mkdtemp()) / "desc_count_mutated.py"
    tmp.write_text(mut_src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("desc_count_mutated", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["desc_count_mutated"] = mod
    spec.loader.exec_module(mod)

    import matrix
    empty = tempfile.mkdtemp(prefix="desc-mut-empty-")
    saved = matrix.DEFAULT_BASE
    matrix.DEFAULT_BASE = empty          # 两个模块共用，改一处都生效
    try:
        try:
            rc_orig = main()
        except SystemExit as e:
            rc_orig = e.code
        sys.stdout = sys.__stdout__
        print(f"  （原版在空检出下退出码 = {rc_orig}，期望 2=未核对）")
        try:
            rc_mut = mod.main()
        except SystemExit as e:
            rc_mut = e.code
        print(f"  （变异体在空检出下退出码 = {rc_mut}，期望 1=假红被抓住）")
    finally:
        matrix.DEFAULT_BASE = saved
    if rc_orig == 2 and rc_mut == 1:
        print("✓ 变异自证：跳过退化成「判 0 个子包」必转红（exit 1），"
              "而原版对空检出如实报「未核对」（exit 2）——两个方向都没退化")
        return 0
    print(f"✗ 变异自证失败：rc_orig={rc_orig}（期望 2）、rc_mut={rc_mut}（期望 1）")
    return 1


if __name__ == "__main__":
    if "--mutate" in sys.argv:
        sys.exit(_mutate())
    sys.exit(main())
