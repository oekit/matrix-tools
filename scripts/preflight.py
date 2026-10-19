#!/usr/bin/env python3
"""开工前检查：动某个仓之前跑一条，确认现场没有别人在途、工作树可用。

用法：python3 scripts/preflight.py <repo> [--no-fetch]

判定：
  BLOCK（exit 1，必须先处理）
    - 在途登记被活进程占用（另一会话正在此仓上工作）
    - 工作树脏（有未提交改动，直接开工可能把别人的产物冲掉或混入自己的提交）
    - main 与远端分叉（本地有未推提交且远端有新提交，需先 rebase 裁决）
  WARN（不拦，但看清再动）
    - 陈旧登记（持有进程已死，可 inflight.py release 接管）
    - 不在 main（可能是上一波次遗留分支）
    - 落后远端 / 本地有未推提交
    - 该仓有 open PR（可能在途波次）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from matrix import DEFAULT_BASE, ORG, git_branch, is_dirty, sh  # noqa: E402
from inflight import read, age_of, held  # noqa: E402

BASE = Path(DEFAULT_BASE)
META_DIRS = ["oekit", "matrix-tools", "oekit.github.io", "profile"]


def local_dir(name: str) -> Path | None:
    for cand in (BASE / name, BASE / f"ekit-{name}"):
        if (cand / ".git").exists():
            return cand
    return None


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_fetch = "--no-fetch" in sys.argv
    if len(args) != 1:
        print(__doc__)
        raise SystemExit(2)
    repo = args[0].lstrip("/")
    d = local_dir(repo) if repo in META_DIRS else None
    if d is None:
        try:
            from matrix import repo_dir
            d = repo_dir(repo)
        except FileNotFoundError:
            raise SystemExit(f"未知仓 {repo}（MODULES/META 均无）")

    blocks: list[str] = []
    warns: list[str] = []

    # 1) 在途登记
    cur = read(repo)
    if cur:
        pid = cur.get("pid")
        desc = f"pid {pid} {age_of(cur.get('ts', 0))}前「{cur.get('note', '')}」"
        if held(cur):
            blocks.append(f"在途登记被占用：{desc}（handoff.py 看现场；确认收尾后 inflight.py release）")
        else:
            warns.append(f"陈旧登记：{desc}（可 inflight.py release {repo} 接管）")

    # 2) 工作树与分支
    if is_dirty(d):
        n = len(subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.splitlines())
        blocks.append(f"工作树脏（{n} 个文件）——先提交或 stash，别在别人产物上开工")
    br = git_branch(d)
    if br != "main":
        warns.append(f"当前在 {br}（可能是上波次遗留分支；确认无主后 git checkout main）")

    # 3) 与远端的关系
    if not no_fetch:
        subprocess.run(["git", "-C", str(d), "fetch", "origin", "main"],
                       capture_output=True, text=True)
    behind = sh(["git", "rev-list", "--count", f"{br}..origin/main"], d, check=False).stdout.strip()
    ahead = sh(["git", "rev-list", "--count", f"origin/main..{br}"], d, check=False).stdout.strip()
    if br == "main":
        if behind not in ("0", "") and ahead not in ("0", ""):
            blocks.append(f"main 与远端分叉（本地多 {ahead} 提交、远端多 {behind}）——先 rebase 裁决")
        elif behind not in ("0", ""):
            warns.append(f"落后 origin/main {behind} 个提交（git pull --ff-only）")
        elif ahead not in ("0", ""):
            warns.append(f"本地有 {ahead} 个未推提交")

    # 4) 在途 PR
    r = subprocess.run(["gh", "pr", "list", "--repo", f"{ORG}/{repo}", "--state", "open",
                        "--json", "number,title", "--jq", '.[] | "\\(.number) \\(.title)"'],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        n = len(r.stdout.strip().splitlines())
        warns.append(f"{n} 个 open PR（可能在途波次）：\n    " + r.stdout.strip().replace("\n", "\n    "))

    for w in warns:
        print(f"⚠ {w}")
    if blocks:
        for b in blocks:
            print(f"❌ {b}")
        raise SystemExit(1)
    print(f"✓ {repo} 可以开工（{len(warns)} 条注意项）" if warns else f"✓ {repo} 可以开工")


if __name__ == "__main__":
    main()
