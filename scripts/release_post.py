#!/usr/bin/env python3
"""发版后一条命令：版本行对齐 → versions.json → oekit BOM → audit 门禁 → 手工待办清单。

背景：release_train 管到 tag+Release 为止，之后的 refresh_versions / gen_versions /
audit 靠记得跑——versions.json 陈旧 6/24、DESC 漏网、安装行 18 仓漂移都是"忘了跑某步"。
发版链条自此收成两条命令：
  python3 scripts/release_train.py <repo> main=vX.Y.Z --apply
  python3 scripts/release_post.py <repo>

发版模型是按需的（2026-10-19 起）：**只发你改的仓**——下游消费新依赖 API 时
go get 抬地板、随本仓版本发出；本命令只处理目标仓的收尾 + 组合锚点更新
（versions.json / BOM 推荐组合）。全量传参（缺省全部模块仓）留给维护轮。

用法：python3 scripts/release_post.py [repo ...] [--no-pr] [--no-audit] [--no-versions-json]
                [--no-bom] [--bom-release] [--merge-all]
  --no-pr           只改本地文件不开 PR
  --no-bom          跳过 oekit BOM 推荐组合更新
  --bom-release     BOM PR 合入后自动打 tag + Release（缺省只开 PR 并打印待办）
  --merge-all       把本命令开的 PR 一并合并
  --help            打印本说明后退出（不执行任何真实动作）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import (MODULES, SITE_REPO, bump_patch, declared_version, git_branch,  # noqa: E402
                    is_dirty, latest_tag, repo_dir, sh, tag_exists)
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
    # --help 必须**只打印不动作**：本脚本的默认行为就是开 PR + 打 tag，
    # 有人（或 agent）习惯性传 --help 探用法 ⇒ 真跑了一条收尾链
    # （2026-10-22 实测：误触发生成了一份陈旧的站点 PR 与 BOM PR，
    #  两者内容都早于当轮打 tag ⇒ 只能关单重来）。
    # 共享 python-ci 之所以「刻意不执行脚本本体」，根因就是这类脚本缺 --help 守卫。
    if "--help" in args or "-h" in args:
        print(__doc__)
        return
    repos = [a for a in args if not a.startswith("--")]
    no_pr = "--no-pr" in args
    no_audit = "--no-audit" in args
    no_vjson = "--no-versions-json" in args
    no_bom = "--no-bom" in args
    bom_release = "--bom-release" in args
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
                          edit=lambda repo=repo: sync_repo(repo, write=True), auto_merge=True)
            print(f"  ✓ {repo}: {url or '无变更'}")

    # 3) versions.json
    if not no_vjson:
        print("\n== 3) 站点 versions.json ==")
        site = repo_dir(SITE_REPO)
        if no_pr:
            r = subprocess.run([sys.executable, str(SCRIPTS / "gen_versions.py")],
                               capture_output=True, text=True)
            print("  " + (r.stdout.strip() or r.stderr.strip()[:200]))
            if is_dirty(site):
                print("  ⚠ 站点仓有未提交变更（--no-pr 模式，手工提交）")
        else:
            # 写入必须发生在 land_pr 的分支上（先写后落 PR 会被干净树守卫拒绝）
            #
            # 站点 versions.json 是**生成物** ⇒ 这里重置它是既有约定（重生成即可
            # 复原）。但**必须说出来**：原来这一步是静默的 `git checkout --`，
            # 别人看到自己的改动突然消失却没有任何说明。
            # ⚠️ 只重置 versions.json 这**一个生成物**——站点的 llms.txt 是
            # **手写文件**（verify_site_copy.py 明说它没有生成器），它的未提交
            # 改动是别人的在途工作，**不许重置**。
            if is_dirty(site):
                _dirty = sh(["git", "status", "--porcelain", "--", "docs/public/versions.json"],
                            site, check=False).stdout.strip()
                if _dirty:
                    print(f"  ↺ 站点 versions.json 有未提交改动（生成物，将重置后重生成）")
                sh(["git", "checkout", "--", "docs/public/versions.json"], site)

            def edit_site():
                r = subprocess.run([sys.executable, str(SCRIPTS / "gen_versions.py")],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    raise SystemExit(f"gen_versions 失败：{(r.stderr or r.stdout)[-200:]}")
                print("  " + (r.stdout.strip() or r.stderr.strip()[:200]))
                return ["docs/public/versions.json"]

            url = land_pr(site, SITE_REPO, VERSION_BRANCH,
                          "docs: versions.json 重生成（release_post）",
                          "docs: versions.json 重生成（release_post）",
                          "release_post.py 发版后自动跟进。",
                          edit=edit_site,
                          # **排 auto-merge**（#115）：此前站点 PR 开了就没人管——
                          # CI 全绿后它**静默悬着**，半截链没人收（实测 2026-10-28：
                          # 站点 PR #104 全绿却一直 OPEN，只能手工合）。
                          # 与模块仓版本行 PR / BOM PR 的处理保持一致。
                          auto_merge=True)
            print(f"  ✓ 站点 PR: {url or '无变更'}")

    # 3.5) oekit BOM（按需发版下的组合锚点：github.com/oekit/oekit@latest = 推荐组合）
    if not no_bom and not no_pr:
        print("\n== 3.5) oekit BOM 推荐组合 ==")
        oekit = repo_dir("oekit")
        if is_dirty(oekit) or git_branch(oekit) != "main":
            print("  ⚠ oekit 仓非 main/脏，跳过（手工 gen_matrix_go + PR）")
        else:
            sh(["git", "pull", "--ff-only", "origin", "main"], oekit)
            bom_next = bump_patch(latest_tag("oekit") or "v0.1.0")

            def edit_bom():
                r = subprocess.run([sys.executable, str(SCRIPTS / "gen_matrix_go.py"),
                                    "--version", bom_next], capture_output=True, text=True)
                if r.returncode != 0:
                    raise SystemExit(f"gen_matrix_go 失败：{(r.stderr or r.stdout)[-200:]}")
                print("  " + (r.stdout.strip() or "").replace("\n", "\n  "))
                return ["go.mod", "matrix.go"]

            url = land_pr(oekit, "oekit", "chore/bom-follow",
                          f"chore: BOM {bom_next}（发版后推荐组合更新）",
                          f"chore: BOM {bom_next}",
                          "release_post.py 发版后自动跟进（推荐组合锚点，非强制跟版）。",
                          edit=edit_bom, auto_merge=True)
            print(f"  ✓ BOM PR: {url or '无变更'}")
            if bom_release and url:
                pr = open_pr("oekit", "chore/bom-follow")
                if pr:
                    print(f"  merge: {merge_pr('oekit', pr[0])}")
                sh(["git", "pull", "--ff-only", "origin", "main"], oekit)
                sh(["git", "tag", "-a", bom_next, "-m", f"oekit {bom_next}"], oekit)
                sh(["git", "push", "origin", bom_next], oekit)
                sh(["gh", "release", "create", bom_next, "--repo", "oekit/oekit",
                    "--title", bom_next,
                    "--notes", "BOM 推荐组合更新（release_post 自动跟进）。"], oekit)
                print(f"  ✓ BOM {bom_next} 已发布")
            elif url:
                print(f"  待办：BOM PR 合入后打 {bom_next}（--bom-release 可自动完成）")
    elif not no_bom and no_pr:
        print("\n== 3.5) oekit BOM：--no-pr 模式跳过（手工 gen_matrix_go + PR）")

    # 4) audit 门禁
    if not no_audit:
        print("\n== 4) audit ==")
        r = subprocess.run([sys.executable, str(SCRIPTS / "audit.py")],
                           capture_output=True, text=True)
        tail = (r.stdout + r.stderr).strip().splitlines()
        for line in tail[-15:]:
            print("  " + line)
        if r.returncode != 0:
            print("  ⚠ audit 未全绿（上表最后几条）——常见为版本行/BOM PR 在途未合、"
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
