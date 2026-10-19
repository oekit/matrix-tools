#!/usr/bin/env python3
"""发版后一条命令：版本行对齐 → versions.json 重生成 → audit 门禁 → 手工待办清单。

背景：release_train 管到 tag+Release 为止，之后的 refresh_versions / gen_versions /
audit 靠记得跑——versions.json 陈旧 6/24、DESC 漏网、安装行 18 仓漂移都是"忘了跑某步"。
发版链条自此收成两条命令：
  python3 scripts/release_train.py <repo> main=vX.Y.Z --apply
  python3 scripts/release_post.py

用法：python3 scripts/release_post.py [repo ...] [--no-pr] [--no-audit] [--no-versions-json] [--merge-all]
  缺省处理全部模块仓；--no-pr 只改本地文件不开 PR；--merge-all 把本命令开的 PR 一并合并。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import MODULES, SITE_REPO, git_branch, is_dirty, latest_tag, repo_dir  # noqa: E402
from prkit import land_pr, merge_pr, open_pr  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
VERSION_BRANCH = "docs/version-bump"


def open_prs_merged(repos: list[str]) -> None:
    for repo in repos:
        pr = open_pr(repo, VERSION_BRANCH)
        if pr:
            print(f"  {repo}#{pr[0]}: {merge_pr(repo, pr[0])}")


def main() -> None:
    args = sys.argv[1:]
    repos = [a for a in args if not a.startswith("--")]
    no_pr = "--no-pr" in args
    no_audit = "--no-audit" in args
    no_vjson = "--no-versions-json" in args
    merge_all = "--merge-all" in args
    targets = repos or list(MODULES)

    # 1) 版本行对齐（非 main / 脏仓跳过并告警，不碰在途现场）
    #    --no-pr：直接写本地文件（人工提交）；否则只计算，写入由 land_pr 的 edit 在分支上执行
    print("== 1) README 安装行 + AGENTS 当前版本行 ==")
    from refresh_versions import sync_repo
    changed_repos: list[str] = []
    skipped: list[str] = []
    for repo in targets:
        d = repo_dir(repo)
        if is_dirty(d) or git_branch(d) != "main":
            skipped.append(repo)
            continue
        lt = latest_tag(repo)
        files = sync_repo(repo, write=bool(no_pr))
        if files:
            changed_repos.append(repo)
            print(f"  ✎ {repo} → {lt}（{'、'.join(files)}）")
        else:
            print(f"  ☑ {repo}")
    if skipped:
        print(f"  ⚠ 跳过 {len(skipped)} 仓（非 main 或脏，避免碰在途现场）：{', '.join(skipped)}")

    # 2) 开 PR
    if changed_repos and not no_pr:
        print(f"\n== 2) {len(changed_repos)} 仓版本行 PR ==")
        for repo in changed_repos:
            lt = latest_tag(repo)
            d = repo_dir(repo)
            url = land_pr(d, repo, VERSION_BRANCH,
                          f"docs: 版本引用对齐 {lt}",
                          f"docs: 版本引用对齐 {lt}",
                          "release_post.py 发版后自动跟进。",
                          edit=lambda repo=repo: sync_repo(repo, write=True))
            print(f"  ✓ {repo}: {url or '无变更'}")

    # 3) versions.json
    if not no_vjson:
        print("\n== 3) 站点 versions.json ==")
        r = subprocess.run([sys.executable, str(SCRIPTS / "gen_versions.py")],
                           capture_output=True, text=True)
        print("  " + (r.stdout.strip() or r.stderr.strip()[:200]))
        site_changed = is_dirty(repo_dir(SITE_REPO))
        if site_changed:
            if no_pr:
                print("  ⚠ 站点仓有未提交变更（--no-pr 模式，手工提交）")
            else:
                url = land_pr(repo_dir(SITE_REPO), SITE_REPO, VERSION_BRANCH,
                              "docs: versions.json 重生成（release_post）",
                              "docs: versions.json 重生成（release_post）",
                              "release_post.py 发版后自动跟进。",
                              edit=lambda: ["docs/public/versions.json"])
                print(f"  ✓ 站点 PR: {url or '无变更'}")

    # 4) audit 门禁
    if not no_audit:
        print("\n== 4) audit ==")
        r = subprocess.run([sys.executable, str(SCRIPTS / "audit.py")],
                           capture_output=True, text=True)
        tail = (r.stdout + r.stderr).strip().splitlines()
        for line in tail[-15:]:
            print("  " + line)
        if r.returncode != 0:
            print("  ⚠ audit 未全绿（上表最后几条）——多为版本行 PR 未合（对账基于远端）"
                  "或站点文档待跟进，逐条处置后复跑")

    # 5) 手工待办
    print("\n== 5) 手工待办（无法自动化的口径面）==")
    print("  - DESC 如有新组件/新规则：改 matrix-tools/matrix.py → doc_wave.py --tables --pr")
    print("  - 站点 guide（http/rpc/logging/migration 等）涉及本版行为的段落")
    print("  - 站点 llms.txt 模块段 / 决策树如涉及本版 API")
    if merge_all and not no_pr:
        print("\n== merge-all ==")
        open_prs_merged(changed_repos)


if __name__ == "__main__":
    main()
