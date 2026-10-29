#!/usr/bin/env python3
"""现场交接清单：新会话开工前先跑这一条，看清上一批并行会话留下的现场。

（此前现场状态靠 memory 召回"开工前必查在途"，2026-10-19 起机器可查——
checkout -f 冲掉在途产物、并行 sweep 连推远端，根因都是新会话不知道现场。）

用法：python3 scripts/handoff.py [--fetch]
  --fetch  先逐仓 fetch --prune（准但慢，28 仓约 1 分钟；默认用上次 fetch 的快照）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from matrix import MODULES, DEFAULT_BASE, local_dir  # noqa: E402
from inflight import entries  # noqa: E402

BASE = Path(DEFAULT_BASE)
# 元仓：本地目录名与线上仓库名（profile = 线上 oekit/.github）
META_DIRS = ["oekit", "matrix-tools", "oekit.github.io", "profile"]


def repo_state(d: Path, fetch: bool) -> dict:
    if fetch:
        subprocess.run(["git", "-C", str(d), "fetch", "--prune", "origin"],
                       capture_output=True, text=True)
    sb = subprocess.run(["git", "-C", str(d), "status", "-sb"],
                        capture_output=True, text=True).stdout.splitlines()
    head = sb[0] if sb else ""
    ma = re.search(r"ahead (\d+)", head)
    mb = re.search(r"behind (\d+)", head)
    ahead = int(ma.group(1)) if ma else 0
    behind = int(mb.group(1)) if mb else 0
    branch = head[3:].split("...")[0].strip() if head else "?"
    return {
        "branch": branch,
        "dirty": sum(1 for l in sb[1:] if l.strip()),
        "ahead": ahead,
        "behind": behind,
        "upstream": "..." in head,
    }


def open_prs() -> list[dict]:
    for fields in ("repository,number,title,headRefName,updatedAt",
                   "repository,number,title"):
        r = subprocess.run(["gh", "search", "prs", "--owner", "oekit", "--state", "open",
                            "--limit", "100", "--json", fields],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return json.loads(r.stdout or "[]")
    return []


def main() -> None:
    fetch = "--fetch" in sys.argv
    infl = entries()

    print("== 在途登记 ==")
    if not infl:
        print("  （无）")
    for repo, data, alive in infl:
        mark = "●" if alive else "○陈旧(可接管)"
        print(f"  {mark} {repo:<16} pid {data.get('pid')}「{data.get('note', '')}」")

    print("\n== 本地工作树（非 main / 脏 / 与远端有出入）==")
    notable = 0
    for name in MODULES + META_DIRS:
        d = local_dir(name)
        if d is None:
            continue
        st = repo_state(d, fetch)
        probs = []
        if st["branch"] != "main":
            probs.append(f"分支 {st['branch']}")
        if st["dirty"]:
            probs.append(f"脏 {st['dirty']} 文件")
        if st["ahead"]:
            probs.append(f"未推 {st['ahead']} 提交")
        if st["behind"]:
            probs.append(f"落后远端 {st['behind']}")
        if not st["upstream"]:
            probs.append("无 upstream")
        if probs:
            notable += 1
            print(f"  {name:<16} {'；'.join(probs)}")
    if not notable:
        print("  （全部干净，均在 main）")

    if "--ci" in sys.argv:
        print("\n== main CI 健康（各仓最近一次 run）==")
        bad = 0
        for name in MODULES:
            d = local_dir(name)
            if d is None:
                continue
            r = subprocess.run(["gh", "run", "list", "--repo", f"oekit/{name}",
                                "--branch", "main", "--limit", "1",
                                "--json", "conclusion,displayTitle,createdAt"],
                               capture_output=True, text=True)
            try:
                run = json.loads(r.stdout or "[]")
                run = run[0] if run else None
            except json.JSONDecodeError:
                run = None
            if not run:
                print(f"  ? {name:<16} 无 run 记录")
                continue
            c = run["conclusion"]
            mark = {"success": "✓", "cancelled": "⊘", "skipped": "⊘"}.get(c, "✗")
            if mark == "✗":
                bad += 1
            print(f"  {mark} {name:<16} {run['conclusion']:<10} {run['createdAt'][:16]} {run['displayTitle'][:44]}")
        if bad:
            print(f"  → {bad} 仓 main CI 红（先修复再在其上叠发版）")

    prs = open_prs()
    print(f"\n== org open PR（{len(prs)}）==")
    for pr in prs[:30]:
        repo = pr.get("repository", {}).get("nameWithOwner", "?")
        head = f" <- {pr['headRefName']}" if pr.get("headRefName") else ""
        print(f"  {repo}#{pr['number']}{head} {pr['title'][:56]}")
    if len(prs) > 30:
        print(f"  …共 {len(prs)} 个")

    print("\n开工纪律：动某仓前跑 preflight.py <repo>；收尾 inflight.py release <repo>；"
          "跨仓文档变更走 doc_wave.py，不要手工逐仓 checkout。")


if __name__ == "__main__":
    main()
