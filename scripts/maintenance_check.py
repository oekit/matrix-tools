#!/usr/bin/env python3
"""维护轮到期检查：全矩阵地板漂移盘点（按需发版模型的周期视角）。

用法（CI / 本地皆可）：
  MATRIX_BASE=repos python3 scripts/maintenance_check.py --threshold 8

输出漂移明细 + 最后一行 MAINTENANCE_COUNT=<n>（workflow 解析用）。
阈值语义：漂移仓数 ≥ threshold 视为「维护轮到期」——由月度 workflow 开
matrix-maintenance 标签 issue 提醒；本地跑只看报告。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import MODULES  # noqa: E402
from align_floors import plan_repo  # noqa: E402

BASE = str(Path(__file__).resolve().parent.parent.parent)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=8)
    ap.add_argument("--base", default=BASE)
    ns = ap.parse_args()

    drifted: dict[str, dict] = {}
    dep_hits: Counter = Counter()
    for repo in MODULES:
        try:
            plans = plan_repo(repo, ns.base)
        except Exception as e:  # noqa: BLE001 单仓解析失败不拖垮盘点
            print(f"⚠ {repo}: 解析失败 {e}")
            continue
        if plans:
            drifted[repo] = plans
            for ups in plans.values():
                for full, _old, _new in ups:
                    dep_hits[full.split("/")[-1]] += 1

    print(f"地板漂移盘点（按需发版模型下的维护轮视角，阈值 {ns.threshold} 仓）：")
    if not drifted:
        print("  ✓ 全矩阵地板零漂移")
    for repo, plans in drifted.items():
        lines = []
        for gm, ups in plans.items():
            for full, old, new in ups:
                lines.append(f"{full.split('/')[-1]} {old}→{new}")
        print(f"  {repo:<16} {'；'.join(lines)}")
    if dep_hits:
        top = "、".join(f"{d}×{n}" for d, n in dep_hits.most_common(5))
        print(f"  落后集中面：{top}")
    print(f"\nMAINTENANCE_COUNT={len(drifted)}")
    if len(drifted) >= ns.threshold:
        print(f"⚠ {len(drifted)} 仓 ≥ 阈值 {ns.threshold}——维护轮到期"
              f"（align_floors.py --apply --pr → 逐仓 release_train patch）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
