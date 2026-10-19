#!/usr/bin/env python3
"""多仓文档波次：DESC/版本行一改全矩阵脏，本工具收成 plan → 开 PR → 合并三步。

背景：改一条 DESC = 24 仓矩阵表全脏；org 开 enforce_admins 后逐仓手工
checkout/commit/push/开 PR 的波次开销极重（2026-10-19 pkg v0.15.0 波次 13 个 PR
全手工）。本工具把机械劳动收成两条命令。

用法：
  python3 scripts/doc_wave.py --tables --plan                 # 哪些仓矩阵表过期（默认 plan）
  python3 scripts/doc_wave.py --tables --pr --message "口径同步说明"   # 受影响仓逐仓开 PR
  python3 scripts/doc_wave.py --tables --merge-all            # 合并本波次全部 PR（auto 等 CI）
  python3 scripts/doc_wave.py --versions --pr                 # 安装行/当前版本行对齐最新 tag

选项：
  --tables        波次内容 = 各仓 README 矩阵总览表（DESC 变更后跑）
  --versions      波次内容 = README 安装行 + AGENTS 当前版本行（发版后跑）
  --repos a,b,c   只处理子集（默认全部受影响仓）
  --branch NAME   分支名（默认 docs/matrix-sync | docs/version-bump）
  --message MSG   提交信息（--pr 必填建议提供，默认给通用文案）
  --method X      合并方式 merge|squash|rebase（默认 merge）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import MODULES, git_branch, is_dirty, repo_dir  # noqa: E402
from prkit import land_pr, merge_pr, open_pr  # noqa: E402

BASE = str(Path(__file__).resolve().parent.parent.parent)


def parse_args() -> dict:
    a = sys.argv[1:]
    cfg = {"mode": None, "action": "plan", "repos": None, "branch": None,
           "message": None, "method": "merge"}
    if "--tables" in a:
        cfg["mode"] = "tables"
    if "--versions" in a:
        cfg["mode"] = cfg["mode"] or "versions"
    if "--pr" in a:
        cfg["action"] = "pr"
    if "--merge-all" in a:
        cfg["action"] = "merge-all"
    if "--repos" in a:
        cfg["repos"] = a[a.index("--repos") + 1].split(",")
    if "--branch" in a:
        cfg["branch"] = a[a.index("--branch") + 1]
    if "--message" in a:
        cfg["message"] = a[a.index("--message") + 1]
    if "--method" in a:
        cfg["method"] = a[a.index("--method") + 1]
    if not cfg["mode"]:
        print(__doc__)
        raise SystemExit(2)
    cfg["branch"] = cfg["branch"] or ("docs/matrix-sync" if cfg["mode"] == "tables" else "docs/version-bump")
    return cfg


def affected_repos(mode: str) -> list[str]:
    """受影响仓：tables=矩阵表过期；versions=版本行落后最新 tag。"""
    if mode == "tables":
        from gen_tables import table_drift
        return [r for r in MODULES if table_drift(repo_dir(r, BASE))]
    from refresh_versions import sync_repo
    return [r for r in MODULES if sync_repo(r, BASE, write=False)]


def do_pr(cfg: dict, repos: list[str]) -> None:
    mode, branch = cfg["mode"], cfg["branch"]
    msg = cfg["message"] or (
        "docs: 矩阵总览表 DESC 口径同步（matrix-tools doc_wave）" if mode == "tables"
        else "docs: 安装行/当前版本行对齐最新 tag（matrix-tools doc_wave）")
    title = msg
    landed, skipped = [], []
    for repo in repos:
        d = repo_dir(repo, BASE)
        if is_dirty(d):
            skipped.append(f"{repo}（脏）")
            continue
        if git_branch(d) != "main":
            skipped.append(f"{repo}（在非 main 分支）")
            continue

        def edit() -> list[str]:
            if mode == "tables":
                from gen_tables import apply_table
                return ["README.md"] if apply_table(repo) else []
            from refresh_versions import sync_repo
            return sync_repo(repo, BASE, write=True)

        url = land_pr(d, repo, branch, msg, title, f"{msg}\n\n由 matrix-tools doc_wave.py 批量生成。", edit)
        if url:
            landed.append(f"{repo} {url}")
            print(f"✓ {repo}: PR 已开")
        else:
            print(f"= {repo}: 无变更")
    if skipped:
        print(f"\n跳过 {len(skipped)} 仓：{'; '.join(skipped)}（先清现场再单独补）")
    print(f"\n{len(landed)} 个 PR 已开；CI 绿后跑：doc_wave.py --{mode} --merge-all"
          + (f" --branch {branch}" if branch != ("docs/matrix-sync" if mode == "tables" else "docs/version-bump") else ""))


def do_merge_all(cfg: dict, repos: list[str]) -> None:
    ok, pending, failed = [], [], []
    for repo in repos:
        pr = open_pr(repo, cfg["branch"])
        if not pr:
            continue
        n, _url = pr
        result = merge_pr(repo, n, cfg["method"])
        print(f"  {repo}#{n}: {result}")
        (failed if "失败" in result else pending if "排程" in result else ok).append(repo)
    print(f"\n合并完成 {len(ok)}；auto 排程 {len(pending)}；失败 {len(failed)}"
          + (f"：{', '.join(failed)}" if failed else ""))


def main() -> None:
    cfg = parse_args()
    repos = cfg["repos"] or affected_repos(cfg["mode"])
    print(f"doc_wave[{cfg['mode']}/{cfg['action']}] 分支 {cfg['branch']}：{len(repos)} 仓\n")
    for r in repos:
        print(f"  {r}")
    if cfg["action"] == "pr":
        print()
        do_pr(cfg, repos)
    elif cfg["action"] == "merge-all":
        print()
        do_merge_all(cfg, repos)
    else:
        print("\n（plan 只读；加 --pr 开批量 PR，--merge-all 合并）")


if __name__ == "__main__":
    main()
