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
  --bom-release     BOM PR 合入后自动打 tag + Release（缺省只开 PR 并打印待办）。
                    幂等（matrix-tools#252）：按**远端事实**决定动作——tag/Release
                    都在 ⇒ 打印「已完成」；tag 在而 Release 缺 ⇒ 直接补建（正文
                    从该 tag 的 go.mod 渲染「本次钉住了哪些版本」，不再用占位串）；
                    PR 已合入而 tag 未打 ⇒ 就地补打。
  --merge-all       把本命令开的 PR 一并合并
  --help            打印本说明后退出（不执行任何真实动作）

缺省仓枚举含伞仓 BOM（matrix-tools#251）——37 个发版单元里它此前不在任何
按 MODULES 枚举的工具里，版本行/Release 无工具覆盖。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import (BOM_REPO, MODULES, ORG, SITE_REPO, bump_patch, declared_version,  # noqa: E402
                    git_branch, is_dirty, latest_tag, remote_tag_exists, repo_dir, sh, tag_exists)
from prkit import land_pr, merge_pr, open_pr  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
VERSION_BRANCH = "docs/version-bump"


def _remote_release_exists(repo: str, tag: str) -> bool:
    """远端是否已有该 tag 的 GitHub Release（matrix-tools#252 续跑幂等的判据）。

    404 ⇒ 确定没有；其余失败（网络/限速/权限）⇒ 显式抛出，**不当**「不存在」
    （把「读不到」说成「没有」会漏建 Release——ghsafe/#79 的同一口径）。
    """
    import ghsafe
    try:
        ghsafe.gh_json(["release", "view", tag, "--repo", f"{ORG}/{repo}", "--json", "tagName"])
        return True
    except ghsafe.GhCommandError as e:
        if e.is_not_found:
            return False
        raise


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
    # 缺省枚举含伞仓 BOM（matrix-tools#251）：它是 37 个发版单元里的 1 个，
    # 版本行纪律（README/AGENTS）对它同样适用——此前按 MODULES 枚举时伞仓
    # 的文档口径没有工具覆盖。
    targets = repos or (list(MODULES) + [BOM_REPO])

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
        # 版本号一律读**远端** tag（本地 tag ref 常陈旧：pull 不带 tag、浅克隆/
        # 长期不 fetch ⇒ 用本地起跳会把「最新」写成旧版——2026-11-03 实录见 3.5 节）
        lt = latest_tag(repo, remote=True)
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
            lt = latest_tag(repo, remote=True)  # 同上：远端 tag 才是「最新」
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
        oekit = repo_dir(BOM_REPO)
        if is_dirty(oekit) or git_branch(oekit) != "main":
            print("  ⚠ oekit 仓非 main/脏，跳过（手工 gen_matrix_go + PR）")
        else:
            sh(["git", "pull", "--ff-only", "origin", "main"], oekit)
            # 版本号必须从**远端**最新 tag 起跳（2026-11-03 实录事故）：本地 tags
            # 陈旧（v0.1.53）而远端已发到 v0.1.70 ⇒ bump_patch 算出撞旧号的
            # v0.1.54 ⇒ BOM 的 `const Version` 从 v0.1.70 被写回 v0.1.54（版本
            # 回退，#120 的 diff 实证）⇒ 一律 latest_tag(..., remote=True)。
            bom_prev = latest_tag(BOM_REPO, remote=True)
            bom_next = bump_patch(bom_prev or "v0.1.0")
            # ── 续跑幂等（matrix-tools#252）：先判**远端事实**，再决定做什么──
            #   tag 在 + Release 在 ⇒ 已完成（连跑两次第二刀必须是这句）；
            #   tag 在 + Release 缺 ⇒ 直接补 Release（旧实现靠「本次有没有产生
            #     PR」这个间接信号决定要不要打 tag/建 Release ⇒ PR 已合入时
            #     重跑无变更 ⇒ url 为空 ⇒ Release 永远补不回来——v0.2.7 有 tag
            #     无 Release 即这个缺口的存量足迹）；
            #   tag 不在 ⇒ 才走 PR → merge → tag → Release。
            #   （旧实现此处对「远端已有同名 tag」一律 fail-loud——那条守卫防的
            #     是本地陈旧撞旧号；bump 现在必然从远端最新起跳，能撞上的只有
            #     「上一轮自己打过却没走完」的同一版 ⇒ 是续跑场景，不是事故。）
            tag_on_remote = remote_tag_exists(BOM_REPO, bom_next)
            rel_on_remote = _remote_release_exists(BOM_REPO, bom_next) if tag_on_remote else False
            if tag_on_remote and rel_on_remote:
                print(f"  ✓ BOM {bom_next} 已发布（tag 与 Release 均在远端）——无需重复")
            else:
                if tag_on_remote:
                    print(f"  ↺ BOM {bom_next} 的 tag 已在远端、Release 缺失 ⇒ 直接补建"
                          f"（#252 续跑幂等；旧实现此形态永远补不回来）")
                else:
                    def edit_bom():
                        r = subprocess.run([sys.executable, str(SCRIPTS / "gen_matrix_go.py"),
                                            "--version", bom_next], capture_output=True, text=True)
                        if r.returncode != 0:
                            raise SystemExit(f"gen_matrix_go 失败：{(r.stderr or r.stdout)[-200:]}")
                        print("  " + (r.stdout.strip() or "").replace("\n", "\n  "))
                        return ["go.mod", "matrix.go"]

                    url = land_pr(oekit, BOM_REPO, "chore/bom-follow",
                                  f"chore: BOM {bom_next}（发版后推荐组合更新）",
                                  f"chore: BOM {bom_next}",
                                  "release_post.py 发版后自动跟进（推荐组合锚点，非强制跟版）。",
                                  edit=edit_bom, auto_merge=True)
                    print(f"  ✓ BOM PR: {url or '无变更'}")
                    if bom_release and url:
                        pr = open_pr(BOM_REPO, "chore/bom-follow")
                        if pr:
                            print(f"  merge: {merge_pr(BOM_REPO, pr[0])}")
                        sh(["git", "pull", "--ff-only", "origin", "main"], oekit)
                        sh(["git", "tag", "-a", bom_next, "-m", f"oekit {bom_next}"], oekit)
                        sh(["git", "push", "origin", bom_next], oekit)
                    elif bom_release and not url:
                        # PR 无变更（产物已在 main）而 tag 还不在 ⇒ 就地补打。
                        # 旧实现只打印「待办：…（--bom-release 可自动完成）」——
                        # 但重跑同样走进这个空 url 分支 ⇒ 待办永远完不成（#252：
                        # 提示语必须与实际可执行性一致）。
                        print("  BOM PR 无变更（产物已在 main）⇒ 就地打 tag")
                        sh(["git", "tag", "-a", bom_next, "-m", f"oekit {bom_next}"], oekit)
                        sh(["git", "push", "origin", bom_next], oekit)
                    elif url:
                        print(f"  待办：BOM PR 合入后打 {bom_next} 并建 Release"
                              f"（--bom-release 可自动完成；PR 已合入而 tag 未打时"
                              f"重跑本命令会就地补打）")
                if bom_release:
                    # 建 Release（tag 可能是上面刚打的，也可能是上一轮挂在建
                    # Release 那步留下的）。正文从**该 tag 的 go.mod 实际内容**
                    # 渲染「本次钉住了哪些版本」（#252）——不再用写死的占位串。
                    from gen_releases import bom_release_notes
                    from gen_matrix_go import ensure_local_tag
                    ensure_local_tag(str(oekit), bom_next)
                    notes = bom_release_notes(str(oekit), bom_next, bom_prev)
                    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                                     encoding="utf-8") as f:
                        f.write(notes)
                        notes_path = f.name
                    try:
                        sh(["gh", "release", "create", bom_next, "--repo",
                            f"{ORG}/{BOM_REPO}", "--title", bom_next,
                            "--notes-file", notes_path], oekit)
                    finally:
                        import os as _os
                        _os.unlink(notes_path)
                    print(f"  ✓ BOM {bom_next} 已发布（正文含本次钉住的组合与地板 diff）")
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
