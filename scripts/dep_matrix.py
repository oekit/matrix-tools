#!/usr/bin/env python3
"""依赖版本矩阵：repo × 矩阵内依赖的地板版本一览（按需发版模型的可观测面）。

用法：
  python3 scripts/dep_matrix.py                # 全矩阵：每仓一行，列出其依赖地板
  python3 scripts/dep_matrix.py pkg            # 单依赖视角：谁在用哪个版本（★=最新）

回答「谁还在用 pkg 旧版」「升 con currency 前谁在用什么」——按需发版下
各仓地板参差是常态，本工具让组合现状可决策（按需采纳 or 触发维护轮）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from matrix import MODULES, OEKIT_REQ_RE, latest_tag, repo_dir  # noqa: E402

def floors_of(repo: str) -> dict[str, str]:
    """repo 根 go.mod 的全部 oekit 依赖地板（嵌套 go.mod 不计——那是父模块跟随面）。"""
    d = repo_dir(repo)
    text = (d / "go.mod").read_text()
    return {m.group(2): m.group(3) for m in OEKIT_REQ_RE.finditer(text)}


def semver(v: str) -> tuple[int, int, int]:
    a, b, c = v[1:].split(".")
    return (int(a), int(b), int(c))


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    table = {r: floors_of(r) for r in MODULES}

    if args:
        dep = args[0]
        latest = latest_tag(dep)
        holders = [(r, f[dep]) for r, f in table.items() if dep in f]
        if not holders:
            print(f"无仓依赖 {dep}")
            return
        holders.sort(key=lambda x: semver(x[1]))
        print(f"{dep} 最新 {latest}，被 {len(holders)} 仓依赖：")
        for r, v in holders:
            mark = "★" if v == latest else " "
            print(f"  {mark} {v:<10} {r}")
        outdated = sum(1 for _, v in holders if v != latest)
        if outdated:
            print(f"  → {outdated} 仓落后（按需采纳或维护轮：align_floors.py --repo <仓> --apply --pr）")
        return

    # 全矩阵视图
    all_deps = sorted({d for f in table.values() for d in f})
    latest = {d: latest_tag(d) for d in all_deps}
    print(f"{'repo':<16}" + "".join(f"{d:<14}" for d in all_deps))
    for r in MODULES:
        row = table[r]
        cells = ""
        for d in all_deps:
            v = row.get(d, "—")
            mark = "*" if d in row and v == latest[d] else (" " if d in row else "")
            cells += f"{v + mark:<14}"
        print(f"{r:<16}{cells}")
    print("\n(* = 该依赖最新版；— = 无此依赖。落后分布细看：dep_matrix.py <依赖名>)")


if __name__ == "__main__":
    main()
