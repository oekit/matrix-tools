#!/usr/bin/env python3
"""维护轮到期检查：全矩阵地板漂移盘点（按需发版模型的周期视角）。

用法（CI / 本地皆可）：
  MATRIX_BASE=repos python3 scripts/maintenance_check.py --threshold 8
  python3 scripts/maintenance_check.py --threshold 8 --base /path/to/matrix-root

输出漂移明细 + 最后两行（workflow 按这两行判定分支）：
  MAINTENANCE_COUNT=<n>        真·有漂移的仓数
  MAINTENANCE_UNDETERMINED=<k> 漂移状态未知（仓读不到）的仓数

阈值语义：漂移仓数 ≥ threshold 视为「维护轮到期」——由月度 workflow 开
matrix-maintenance 标签 issue 提醒；本地跑只看报告。

**「读不到」与「真的没漂移」必须分开（#96）**：
此前「仓读不出来」被静默 continue ⇒ 24 个仓全读不到时照样输出
「✓ 全矩阵地板零漂移 / MAINTENANCE_COUNT=0」⇒ 月度 workflow 走进
「count==0 ⇒ 关闭维护轮 issue」分支 ⇒ 门禁方向反了：到期不报、反而关单。
根因是 base 定位不认 MATRIX_BASE（见下），现已修；且读不到的仓一律计入
MAINTENANCE_UNDETERMINED，且**不再打印「零漂移」**——与 audit 在 #79 对
remote tag 的处理同口径：读不到 ≠ 事实为空。

**base 定位必须认 MATRIX_BASE**：matrix.repo_dir(repo, base) 的语义是
`base or os.environ.get("MATRIX_BASE", ...)`——base 非空就把环境变量短路掉。
本脚本此前硬编码 `parent.parent.parent`，于是 docstring 里写的
`MATRIX_BASE=repos python3 scripts/maintenance_check.py` 是无效的；
CI 布局（仓克隆在 `$GITHUB_WORKSPACE/repos/<name>`）下 24 仓全部读不到。
现在 base 解析与 audit.py 同口径（MATRIX_BASE → 本仓上级），--base 可显式覆盖。
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import MODULES  # noqa: E402
from align_floors import plan_repo  # noqa: E402

# 默认 base = 本仓上级（本地工作区布局 /Volumes/zy/oekit/<repo>）；
# MATRIX_BASE 优先（CI 的 repos/<repo> 布局）；--base 显式覆盖。
BASE = os.path.abspath(
    os.environ.get("MATRIX_BASE") or str(Path(__file__).resolve().parent.parent.parent)
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=8)
    ap.add_argument(
        "--base",
        default=BASE,
        help="矩阵检出根目录（默认取 MATRIX_BASE，其次本仓上级）",
    )
    ap.add_argument("--repos", default="", help="只查这些仓（逗号分隔；缺省全矩阵）")
    ns = ap.parse_args()

    modules = [r for r in ns.repos.split(",") if r] or list(MODULES)

    drifted: dict[str, dict] = {}
    undetermined: dict[str, str] = {}
    dep_hits: Counter = Counter()
    for repo in modules:
        try:
            plans = plan_repo(repo, ns.base)
        except Exception as e:  # noqa: BLE001 单仓解析失败不拖垮盘点
            # 读不到 ⇒ 该仓漂移状态**未知**，不是「没漂移」。计入 undetermined，
            # 且绝不能让整盘点看起来像「零漂移」（#96）。
            undetermined[repo] = f"{type(e).__name__}: {e}"
            continue
        if plans:
            drifted[repo] = plans
            for ups in plans.values():
                for full, _old, _new in ups:
                    dep_hits[full.split("/")[-1]] += 1

    print(f"地板漂移盘点（按需发版模型下的维护轮视角，阈值 {ns.threshold} 仓）：")
    print(f"  检出根目录：{ns.base}")
    if not drifted and not undetermined:
        print("  ✓ 全矩阵地板零漂移")
    for repo, plans in drifted.items():
        lines = []
        for gm, ups in plans.items():
            for full, old, new in ups:
                lines.append(f"{full.split('/')[-1]} {old}→{new}")
        print(f"  {repo:<16} {'；'.join(lines)}")
    if undetermined:
        print(f"  ❌ 漂移状态未知（{len(undetermined)} 仓，读不到 ⇒ 不能当零漂移）：")
        for repo, err in sorted(undetermined.items()):
            print(f"     {repo}: {err}")
    if dep_hits:
        top = "、".join(f"{d}×{n}" for d, n in dep_hits.most_common(5))
        print(f"  落后集中面：{top}")
    print(f"\nMAINTENANCE_COUNT={len(drifted)}")
    print(f"MAINTENANCE_UNDETERMINED={len(undetermined)}")
    if len(drifted) >= ns.threshold:
        print(f"⚠ {len(drifted)} 仓 ≥ 阈值 {ns.threshold}——维护轮到期"
              f"（align_floors.py --apply --pr → 逐仓 release_train patch）")
    elif undetermined:
        print(f"⚠ 盘点不完整（{len(undetermined)} 仓未能判定）——不可据此判定「零漂移」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
