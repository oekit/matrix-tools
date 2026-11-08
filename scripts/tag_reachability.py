#!/usr/bin/env python3
"""tag 可达性巡检（matrix-tools#240）：每个已发布 tag 指向的提交必须可解析。

**背景（#240）**：2026-11-08 消费方（pigeonbox/core 等）按 tag 走 GitHub
compare/commits API 提取变更日志，全线断链——

    GET /repos/oekit/{mod}/git/refs/tags/{tag} → object.sha
    GET /repos/oekit/{mod}/commits/{sha}       ⇒ 422 "No commit found for SHA"
    GET /repos/oekit/{mod}/compare/{A}...{B}   ⇒ 404（无共同祖先）

同期 `go mod download` 全部正常 ⇒ 代理缓存的模块内容没问题，是**仓库侧 tag
指向的提交已不可达**（疑似发版 sweep 历史重写时丢弃了被 tag 引用的提交）。
go.mod 解析零影响，受伤的是一切 API 侧的变更日志提取/对账工具。

**判据**：对每个已发布 tag（含嵌套前缀 tag），取它指向的 commit（轻量 tag 的
sha 本身是 commit；annotated tag 用 `git ls-remote` 的 `^{}` peeled 行），断言
`GET /repos/{org}/{repo}/commits/{sha}` 可解析。404/422 ⇒ **悬空**（tag 是
对外承诺，指向的提交必须永久可达——发版 sweep 不得丢弃被 tag 引用的提交）。

配额：tag → commit 的映射走 `git ls-remote`（**不打 API**）；每 tag 一次
REST 调用（优先 REST，限流友好）；`--limit N` 每仓只查最新 N 个（巡检省配额，
存量全量盘点时不带）。悬空 tag 清单打印到 stdout；进度走 stderr。

退出码：0 = 全部可达；1 = 有悬空 tag（清单已列出）；2 = 读不到（网络/限速/
ls-remote 失败）⇒ 判不了 ≠ 通过（#79 口径）。

自测：scripts/tag_reachability_selftest.py（注入 API 判定层，不联网；--mutate）。

用法：
  python3 scripts/tag_reachability.py                # 全矩阵（MODULES + 伞仓 BOM）
  python3 scripts/tag_reachability.py --repo logx    # 单仓
  python3 scripts/tag_reachability.py --limit 5      # 每仓只查最新 5 个
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import BOM_REPO, MODULES, ORG, RemoteReadError, git_env, repo_dir  # noqa: E402
import ghsafe  # noqa: E402

EXIT_OK, EXIT_DANGLING, EXIT_UNREADABLE = 0, 1, 2


def progress(msg: str) -> None:
    print(f"[tag-reach] {msg}", file=sys.stderr, flush=True)


def tag_commit_shas(repo: str, base: str | None = None) -> dict[str, str]:
    """{tag 名: 指向的 commit sha}。

    `git ls-remote --tags` 每仓**一次调用**（不走 API，省配额）：
    - 轻量 tag：`refs/tags/<name>` 行的 sha 就是 commit；
    - annotated tag：peeled 行 `refs/tags/<name>^{}` 的 sha 才是 commit
      （前者是 tag 对象）⇒ 有 peeled 行时以它为准。
    读不到 ⇒ 抛 RemoteReadError（读不到 ≠ 没有，#79）——canonical HTTPS 优先、
    clone origin 兜底，与 matrix.remote_tags 同一套换序，各试 3 次。
    """
    d = repo_dir(repo, base)
    try:
        origin = subprocess.check_output(
            ["git", "-C", str(d), "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001 —— origin 读不到就用 canonical
        origin = None
    urls: list[str] = []
    canonical = f"https://github.com/{ORG}/{repo}.git"
    if origin and (origin.startswith("file://") or origin.startswith("/")):
        urls.append(origin)      # 本地路径的 origin 优先（本地镜像/离线夹具）
    urls.append(canonical)
    if origin and origin not in urls:
        urls.append(origin)
    last_err = ""
    for url in urls:
        for attempt in range(3):
            try:
                r = subprocess.run(["git", "ls-remote", "--tags", url],
                                   capture_output=True, text=True, timeout=60,
                                   env=git_env())
            except Exception as e:  # noqa: BLE001
                r = None
                last_err = str(e)
            if r is not None and r.returncode == 0:
                out: dict[str, str] = {}
                peeled: dict[str, str] = {}
                for line in r.stdout.splitlines():
                    # ls-remote 行形态：<sha>\t<refname>
                    sha, _, ref = line.partition("\t")
                    ref = ref.strip()
                    sha = sha.strip()
                    if ref.endswith("^{}"):
                        # annotated tag 的 peeled commit 行：这才是 tag 指向的提交
                        peeled[ref[len("refs/tags/"):-3]] = sha
                        continue
                    if not ref.startswith("refs/tags/"):
                        continue
                    name = ref[len("refs/tags/"):]
                    if not re.fullmatch(r"(?:[A-Za-z0-9._-]+/)?v\d+\.\d+\.\d+", name):
                        continue     # 非发版 tag（别的形态）不查
                    out[name] = sha
                for name, sha in peeled.items():
                    if name in out:
                        out[name] = sha
                return out
            if r is not None:
                last_err = (r.stderr or "").strip()[:200]
            time.sleep(0.5 * (attempt + 1))
    raise RemoteReadError(
        f"{repo}: ls-remote 读不到 tag 表（已试 {'、'.join(urls)}；{last_err}）"
        f"——读不到 ≠ 没有（#79），不能当成「零悬空」")


def commit_exists(org: str, repo: str, sha: str, api_fn=None) -> bool:
    """`GET /repos/{org}/{repo}/commits/{sha}` 是否可解析（#240 的判定点）。

    200 ⇒ True；404 / 422（"No commit found for SHA"）⇒ False（**悬空**：
    提交不可达）；其余失败（限速/网络/权限）⇒ 向上抛（判不了 ≠ 可达，
    fail-loud）。

    ``api_fn(org, repo, sha) -> None``：执行 API 调用的那一步，可注入（自测
    不联网）；调用失败（404/422/限速……）以 GhCommandError 表达，**悬空与否
    的分类在本函数里**（判定点必须可注入 runner、不可被替身跳过分类）。
    """
    if api_fn is None:
        def api_fn(_org, _repo, _sha):  # noqa: E306
            ghsafe.run_gh(["api", f"repos/{_org}/{_repo}/commits/{_sha}"])
    try:
        api_fn(org, repo, sha)
        return True
    except ghsafe.GhCommandError as e:
        blob = f"{e.stderr}\n{e.stdout}"
        if ("404" in blob or "422" in blob or "No commit found" in blob
                or "Not Found" in blob):
            return False
        raise


def inspect_repo(repo: str, base: str | None, limit: int, api_fn=None) -> tuple[list[str], bool]:
    """单仓巡检：返回 (悬空清单, 是否真的核对过)。

    读不到（RemoteReadError / API 抛错）⇒ 向上抛，由调用方按「判不了」计。
    """
    tags = tag_commit_shas(repo, base)
    if limit:
        def vkey(name: str):
            ver = name.rsplit("/", 1)[-1]
            m = re.match(r"v(\d+)\.(\d+)\.(\d+)$", ver)
            return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
        names = sorted(tags, key=vkey, reverse=True)[:limit]
    else:
        names = sorted(tags)
    dangling: list[str] = []
    for name in names:
        sha = tags[name]
        if not sha:
            raise RemoteReadError(f"{repo}: tag {name} 拿不到 commit sha（peeled 行缺失）")
        if not commit_exists(ORG, repo, sha, api_fn):
            dangling.append(f"{repo}@{name}（{sha[:12]}… 指向的提交不可达 ⇒ "
                            f"compare/commits API 对该 tag 全线 404/422，matrix-tools#240）")
    return dangling, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="tag 可达性巡检（#240：悬空 tag 清单）")
    ap.add_argument("--repo", action="append", default=[], help="只查这些仓（可多次）")
    ap.add_argument("--limit", type=int, default=0,
                    help="每仓只查最新 N 个 tag（0 = 全量；巡检省配额用）")
    ap.add_argument("--base", default=os.environ.get("MATRIX_BASE")
                    or str(Path(__file__).resolve().parent.parent.parent),
                    help="矩阵检出根目录（默认取 MATRIX_BASE，其次本仓上级）")
    ns = ap.parse_args(argv)
    repos = ns.repo or (list(MODULES) + [BOM_REPO])
    base = os.path.abspath(ns.base)

    all_dangling: list[str] = []
    checked = 0
    for i, repo in enumerate(repos, 1):
        progress(f"巡检 {i}/{len(repos)} {repo}")
        try:
            dangling, _ = inspect_repo(repo, base, ns.limit)
        except RemoteReadError as e:
            print(f"❌ {e}", file=sys.stderr)
            return EXIT_UNREADABLE
        except ghsafe.GhCommandError as e:
            # API 侧读不到（限速/网络/权限）⇒ 判不了 ≠ 可达，fail-loud（#79）
            print(f"❌ {repo}: commits API 读不到 ⇒ 无法判定（{e}）", file=sys.stderr)
            return EXIT_UNREADABLE
        checked += 1
        all_dangling.extend(dangling)
    print(f"\ntag 可达性巡检：核对 {checked}/{len(repos)} 仓，"
          f"悬空 {len(all_dangling)} 个")
    for d in all_dangling:
        print(f"  ❌ {d}")
    if all_dangling:
        print("\n处置口径（matrix-tools#240）：tag 是对外承诺，指向的提交必须永久可达；"
              "发版 sweep 禁止丢弃被 tag 引用的提交（要重写历史就同时重打 tag 或留 alias ref）。"
              "存量悬空 tag 可重打到等价内容的新提交并公告（成本高，可选；至少用本巡检拦住增量）。")
        return EXIT_DANGLING
    print("✓ 全部已发布 tag 的提交均可解析（API 侧变更日志提取不断链）")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
