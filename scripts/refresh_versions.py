#!/usr/bin/env python3
"""各仓 README 安装行 + AGENTS.md「当前版本」行刷新到最新 tag。
用法：python3 scripts/refresh_versions.py [--push]

覆盖口径（matrix-tools#250）：**根模块 + 嵌套发版单元**都算——
  - 根安装行：`go get github.com/oekit/logx@v0.8.2`（按仓名定位）；
  - 嵌套安装行：`go get github.com/oekit/logx/sink@v0.1.0`（按**模块 import
    路径**定位，NESTED_MODULES 登记），版本取 `latest_nested_tag`（嵌套 tag
    带前缀，根 latest_tag 读不到）。
    旧实现只刷根模块：12 个嵌套发版单元的安装行**永不刷新**（7/7 实测陈旧，
    最坏落后 15 个 patch），且没有任何门禁会发现。
    ⚠️ 定位必须按**完整模块路径**，不能按「仓库名 + 第一段」——registry/nacos
    与 registry/nacosimpl、logx/sink 与 logx/hlog 都是同仓多单元，互相会覆盖。

--push 走 PR（matrix-tools#250 顺带）：org 全量 enforce_admins ⇒ 直推 main
一律被拒（`--push` 的旧实现直接 `git push` ⇒ 该入口实际不可用）。现在与
release_post 同款：写文件 → 开分支（docs/version-bump）→ PR → auto-merge。

sync_repo 供 doc_wave/release_post import 复用（写入逻辑不在模块顶层）。"""
from __future__ import annotations  # PEP604（str | None）需 3.10+；本机 python3 可能是 3.9

import os, pathlib, re, sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import (MODULES, NESTED_MODULES, repo_dir, ensure_clean_all,  # noqa: E402
                    latest_nested_tag, latest_tag)
from prkit import land_pr  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
PUSH = "--push" in sys.argv
VERSION_BRANCH = "docs/version-bump"


def sync_repo(repo: str, base: str | None = None, write: bool = True) -> list[str]:
    """单仓版本行对齐最新 tag（根 + 嵌套单元）。write=False 只算不写（plan 用）。
    返回变更文件名清单。

    「最新 tag」读**远端**（remote=True，#79 口径）：本地 tag ref 常陈旧（pull
    不带 tag / 浅克隆 / 长期不 fetch）⇒ 用本地起跳会把旧版本号写进 README/AGENTS
    （版本回退事故，2026-11-03 实录：BOM 产物 Version 被写回旧号）；读不到远端
    ⇒ 抛 RemoteReadError fail-loud，绝不拿本地陈旧 tag 顶着写。"""
    d = repo_dir(repo, base)
    lt = latest_tag(repo, base, remote=True)
    changed: list[str] = []
    readme = d / "README.md"
    t = readme.read_text()
    t2 = t
    if lt is not None:  # 无 tag 的新仓跳过根行，避免写入 @None
        t2 = re.sub(r"(go get github\.com/oekit/%s@)v[0-9.]+" % re.escape(repo), rf"\g<1>{lt}", t2)
    # 嵌套发版单元的安装行（matrix-tools#250）：同一份 README 里
    # `go get github.com/oekit/logx/sink@v0.1.0` 的版本纪律与根行相同。
    # 嵌套模块**没有自己的 README**（实测 7 个嵌套目录均无）⇒ 父仓这几行是它们
    # 唯一的安装指引。按 latest_nested_tag（嵌套 tag 带前缀）取版本；登记里没有
    # 嵌套 tag（None）的单元跳过。
    for nm in NESTED_MODULES:
        if nm["repo"] != repo:
            continue
        lt_n = latest_nested_tag(nm["module"], base)
        if lt_n is None:
            continue
        # 按**完整模块路径**定位（README 里的形态是 `github.com/oekit/logx/sink`，
        # 即 repo/path——path 是完整子路径，kitexotel 的 4 层也对），同仓多单元
        # 互不覆盖。nm["module"] 自带 github.com/oekit/ 前缀，不能再拼一遍。
        locator = f"{nm['repo']}/{nm['path']}"
        t2 = re.sub(r"(go get github\.com/oekit/%s@)v[0-9.]+" % re.escape(locator),
                    rf"\g<1>{lt_n}", t2)
    if t2 != t:
        changed.append("README.md")
        if write:
            readme.write_text(t2)
    agents = d / "AGENTS.md"
    if agents.exists():
        ta = agents.read_text()
        if lt is not None:
            ta2 = re.sub(r"(当前版本：`)v[0-9.]+(`)", rf"\g<1>{lt}\g<2>", ta, count=1)
        else:
            ta2 = ta
        if ta2 != ta:
            changed.append("AGENTS.md")
            if write:
                agents.write_text(ta2)
    return changed


def main() -> None:
    ensure_clean_all([repo_dir(r, BASE) for r in MODULES], "refresh_versions")
    for repo in MODULES:
        if PUSH:
            # --push 走 PR（不直推 main——org 全量 enforce_admins，直推必被拒，
            # 旧实现因此实际不可用）。写入发生在 land_pr 的分支 edit 里
            # （先写后落 PR 会被干净树守卫拒绝）。
            changed = sync_repo(repo, BASE, write=False)
            if not changed:
                print(f"☑ {repo} 已对齐")
                continue
            lt = latest_tag(repo, BASE, remote=True)
            d = repo_dir(repo, BASE)
            url = land_pr(d, repo, VERSION_BRANCH,
                          f"docs: 版本引用对齐 {lt}",
                          f"docs: 版本引用对齐 {lt}",
                          "refresh_versions.py 自动跟进（README 安装行 + AGENTS 当前版本行，"
                          "嵌套发版单元的安装行一并刷新，matrix-tools#250）。",
                          edit=lambda repo=repo: sync_repo(repo, BASE, write=True),
                          auto_merge=True)
            print(f"✓ {repo} → {lt}（{'、'.join(changed)}）：{url or '无变更'}")
        else:
            changed = sync_repo(repo, BASE, write=True)
            if changed:
                lt = latest_tag(repo, BASE, remote=True)  # 同 sync_repo：远端才是「最新」
                print(f"✓ {repo} → {lt}（{'、'.join(changed)}）")
            else:
                print(f"☑ {repo} 已对齐")


if __name__ == "__main__":
    main()
