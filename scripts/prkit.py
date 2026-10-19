#!/usr/bin/env python3
"""PR 落地共用件：多仓文档波次（doc_wave / release_post / align_floors）共用的
「同步 main → 开分支 → 改文件 → 提交 → 推送 → 开 PR → 合并」流程。

org 全量 enforce_admins 后直推 main 一律 GH006，任何跨仓变更都必须走 PR。
单人维护者 0 批准即可自合；required checks 由各仓 workflow 决定，--auto 交给
GitHub 等检查绿后自动合。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from matrix import ORG, git_branch, is_dirty, sh  # noqa: E402


def sync_main(d) -> None:
    """确保在 main 且与远端一致（ff-only）。要求工作树干净（调用方保证）。"""
    br = git_branch(d)
    if br != "main":
        raise SystemExit(f"{Path(d).name}: 当前在 {br}，落 PR 须从 main 出发")
    sh(["git", "fetch", "origin", "main"], d)
    behind = sh(["git", "rev-list", "--count", "main..origin/main"], d).stdout.strip()
    if behind != "0":
        sh(["git", "pull", "--ff-only", "origin", "main"], d)


def land_pr(d, repo: str, branch: str, commit_msg: str, title: str, body: str,
            edit: Callable[[], list[str]] | None = None) -> str:
    """在 repo 上落一个 PR。edit() 在分支上执行并返回要 add 的文件清单（相对仓根）。
    返回 PR URL。分支用 -B 复用：同名残留分支重置后推送，旧 open PR 自动续用。"""
    if is_dirty(d):
        raise SystemExit(f"{repo}: 工作树不干净，拒绝落 PR")
    sync_main(d)
    sh(["git", "checkout", "-B", branch], d)
    try:
        files = edit() if edit else []
    except Exception:
        # 入口时树是干净的，此刻的脏全是本波次半截产物——丢弃并回 main
        sh(["git", "reset", "--hard", "HEAD"], d)
        sh(["git", "checkout", "main"], d)
        sh(["git", "branch", "-D", branch], d)
        raise
    if not files:
        sh(["git", "checkout", "main"], d)
        sh(["git", "branch", "-D", branch], d)
        return ""  # 无变更，未产生 PR
    sh(["git", "add", *files], d)
    sh(["git", "commit", "-m", commit_msg], d)
    sh(["git", "push", "-u", "origin", branch], d)
    r = sh(["gh", "pr", "create", "--repo", f"{ORG}/{repo}", "--head", branch,
            "--title", title, "--body", body], d, check=False)
    if r.returncode != 0:
        raise SystemExit(f"{repo}: 开 PR 失败：{(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout.strip()


def open_pr(repo: str, head: str) -> tuple[int, str] | None:
    """该 head 分支当前 open 的 PR（number, url），无则 None。"""
    r = subprocess.run(["gh", "pr", "list", "--repo", f"{ORG}/{repo}", "--head", head,
                        "--state", "open", "--json", "number,url",
                        "--jq", ".[0] | \"\\(.number)|\\(.url)\""],
                       capture_output=True, text=True)
    out = r.stdout.strip()
    if r.returncode != 0 or not out or out.startswith("null"):
        return None
    n, url = out.split("|", 1)
    return int(n), url


def merge_pr(repo: str, number: int, method: str = "merge") -> str:
    """合并一个 PR：落后/分支过期（BEHIND/DIRTY）先 update-branch（真冲突会在此失败并上报），
    再 --auto（required checks 由 GitHub 等绿；allow_auto_merge 已全 org 启用）。
    auto 不可用时回退立即合。返回终态描述。"""
    full = f"{ORG}/{repo}"
    st = sh(["gh", "pr", "view", str(number), "--repo", full,
             "--json", "mergeStateStatus", "--jq", ".mergeStateStatus"],
            None, check=False).stdout.strip()
    if st in ("BEHIND", "DIRTY"):
        r = sh(["gh", "pr", "update-branch", str(number), "--repo", full], None, check=False)
        if r.returncode != 0:
            return f"合并失败：分支过期且 update-branch 失败（大概率真冲突）：{(r.stderr or r.stdout).strip()[:140]}"
    r = sh(["gh", "pr", "merge", str(number), "--repo", full,
            f"--{method}", "--auto"], None, check=False)
    if r.returncode == 0:
        return "auto-merge 已排程（等 checks 绿）"
    r2 = sh(["gh", "pr", "merge", str(number), "--repo", full, f"--{method}"],
            None, check=False)
    if r2.returncode == 0:
        return "merged"
    return f"合并失败：{((r.stderr or '') + (r2.stderr or '')).strip()[:160]}"
