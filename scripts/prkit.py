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
    """确保在 main 且与远端一致（ff-only）。要求工作树干净（调用方保证）。

    现场若停在别的分支（上一波次遗留 / 已合入 PR 的孤儿分支），**自动归位 main**
    而不是直接退出——原实现把「现场没归位」变成整条发版链的硬失败：
    2026-10-22 实测，站点仓停在 docs/version-bump（上一轮落 PR 后留下的）
    ⇒ release_post 在站点那步直接 SystemExit，整条收尾链退出 1，
    而它要做的只是往同一个分支上再落一个 PR。

    工作树不干净时**不动**（别人的在途产物不能被 checkout 冲掉）。
    """
    br = git_branch(d)
    if br != "main":
        if is_dirty(d):
            raise SystemExit(f"{Path(d).name}: 当前在 {br} 且工作树不干净——"
                             "先落 PR/提交/清理，归位 main 后再试")
        sh(["git", "checkout", "main"], d)
        print(f"  [sync] {Path(d).name}: 现场在 {br}，已自动归位 main")
    sh(["git", "fetch", "origin", "main"], d)
    behind = sh(["git", "rev-list", "--count", "main..origin/main"], d).stdout.strip()
    if behind != "0":
        sh(["git", "pull", "--ff-only", "origin", "main"], d)


def push_branch(d, branch: str) -> None:
    """推送分支；远端同名分支若含**不在 main 上**的提交，普通 push 报
    non-fast-forward ⇒ 改用 `--force-with-lease`（带显式期望 sha）。

    2026-10-22 实测：站点仓的 docs/version-bump 分支在它唯一的 PR 被**关闭**
    后，其提交不在 main 上 ⇒ land_pr 的普通 push 报
    `! [rejected] ... (non-fast-forward)` ⇒ 整条收尾链退出 1。
    强推带 lease 是安全的：期望值是我们刚 fetch 到的远端 sha，
    别人在这期间推过 ⇒ lease 失配 ⇒ 拒绝，不覆盖他人工作。
    """
    r = sh(["git", "push", "-u", "origin", branch], d, check=False)
    if r.returncode == 0:
        return
    sh(["git", "fetch", "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], d, check=False)
    remote_sha = sh(["git", "rev-parse", "--verify", "--quiet",
                     f"refs/remotes/origin/{branch}"], d, check=False).stdout.strip()
    if not remote_sha:
        raise SystemExit(f"推送分支失败：{(r.stderr or r.stdout).strip()[:200]}")
    r2 = sh(["git", "push", f"--force-with-lease=refs/heads/{branch}:{remote_sha}",
             "-u", "origin", branch], d, check=False)
    if r2.returncode != 0:
        raise SystemExit(
            f"推送分支失败（按 lease 强推被拒——远端分支可能已被他人更新，"
            f"不覆盖）：{((r.stderr or '') + (r2.stderr or '')).strip()[:220]}")
    print(f"  [push] {branch}: 远端旧分支含不在 main 上的提交，已按 lease 强推复位")


def land_pr(d, repo: str, branch: str, commit_msg: str, title: str, body: str,
            edit: Callable[[], list[str]] | None = None, auto_merge: bool = False) -> str:
    """在 repo 上落一个 PR。edit() 在分支上执行并返回要 add 的文件清单（相对仓根）。
    返回 PR URL。分支用 -B 复用：同名残留分支重置后推送，旧 open PR 自动续用。
    auto_merge=True：创建后立即排程 auto-merge（等 checks 绿自动合，波次免手工合并）。"""
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
    push_branch(d, branch)
    r = sh(["gh", "pr", "create", "--repo", f"{ORG}/{repo}", "--head", branch,
            "--title", title, "--body", body], d, check=False)
    if r.returncode != 0:
        raise SystemExit(f"{repo}: 开 PR 失败：{(r.stderr or r.stdout).strip()[:200]}")
    url = r.stdout.strip()
    suffix = ""
    if auto_merge:
        import re as _re
        m = _re.search(r"/pull/(\d+)", url)
        if m and merge_pr(repo, int(m.group(1))).startswith("auto"):
            suffix = "（auto-merge 已排程）"
    sh(["git", "checkout", "main"], d)  # PR 已接管分支内容，本地回 main（分支留在远端供 PR 用）
    return url + suffix


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


def may_merge_without_checks(merge_state: str) -> bool:
    """auto-merge 不可用时，允许「不等 CI 直接合」的条件。

    只有**没有任何 required check 挂起**（mergeStateStatus == CLEAN）才允许。
    BLOCKED = 有检查没过/还没跑完 ⇒ 合进去就是绕过 CI ⇒ 不许。
    状态读不到（空）⇒ 保守当**不许**——「判不了」不等于「没问题」。

    提成纯函数是为了**能自测**：判据藏在 merge_pr 的控制流里时，
    没人能证明它真的按预期挡（2026-10-30 实测：旧实现是「auto 失败 ⇒ 无条件
    立即合」，在无分支保护的仓上等于不等 CI 就合，且命令成功退出、不报错）。
    """
    return (merge_state or "").strip() == "CLEAN"


def merge_pr(repo: str, number: int, method: str = "merge") -> str:
    """合并一个 PR：落后/分支过期（BEHIND/DIRTY）先 update-branch（真冲突会在此失败并上报），
    再 --auto（required checks 由 GitHub 等绿；allow_auto_merge 已全 org 启用）。
    **auto 不可用时，只有「没有任何 required check 挂起」才允许立即合**；
    checks 没绿就明确返回「等 CI 绿」，不替人做合并决定。返回终态描述。

    **为什么收紧这个回退**（2026-10-30）：旧实现是「auto 失败 ⇒ 无条件立即合」。
    在**没有分支保护**（或保护里没挂 required checks）的仓上，那条回退等于
    「不等 CI 就合」⇒ 破坏本矩阵「PR 必须 CI 全绿才能合」的约定，而且
    **它不报错**（命令成功退出）⇒ 事后才发现时已经合进去了。
    """
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
    # auto 不可用 ⇒ 重新问一次状态（auto 那次尝试期间状态可能已变）
    st2 = sh(["gh", "pr", "view", str(number), "--repo", full,
              "--json", "mergeStateStatus", "--jq", ".mergeStateStatus"],
             None, check=False).stdout.strip()
    if not may_merge_without_checks(st2):
        return (f"auto-merge 不可用，且 checks 未全绿（mergeStateStatus={st2 or '读不到'}）"
                f"——不自动合，等 CI 绿后再合（合并动作留给人或下一次调用）")
    r2 = sh(["gh", "pr", "merge", str(number), "--repo", full, f"--{method}"],
            None, check=False)
    if r2.returncode == 0:
        return "merged（无 required check 挂起，auto 不可用 ⇒ 直接合）"
    return f"合并失败：{((r.stderr or '') + (r2.stderr or '')).strip()[:160]}"
