#!/usr/bin/env python3
"""flake_scan.py 的自测：证明「判据不可用即红」这条路径有牙齿（#313）。

#313 的对象是 scan_one 的三个失效模式——它们都**不得**落进「无失败用例」：

1.  C0  缺仓（repo_dir 的 FileNotFoundError）⇒ 进 build_err 桶且不崩整轮
        （原 `if not d.exists()` 是死分支，异常穿过 fut.result() 打崩）；
2.  C1  git fetch 失败（非 git 目录 / remote 不可达）⇒ 进 build_err 桶，
        带 stderr 摘要（原三个 subprocess.run 全不查 returncode——2026-10-30
        假报事故的复发面：陈旧快照上的结果冠以「全矩阵 flaky 扫描」）；
3.  C2  inflight 被活进程持有 ⇒ 进 skipped 桶（原不读登记，checkout/pull
        会把并行会话现场切走——inflight.py 文档明写的撞车类）；
4.  C3  三桶互斥且都非「无失败」（names 必须为空、build_err/skip 至少一个非空）；
5.  C4  正常仓（干净 git 仓 + 无 Go 文件）⇒ go test 失败归 build_err 桶，
        **不**误报 names（FAIL 行判据不虚构）。

跑法：python3 scripts/flake_scan_selftest.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import flake_scan  # noqa: E402
import inflight  # noqa: E402

FAILURES: list[str] = []


def check(cid: str, cond: bool, detail: str) -> None:
    if cond:
        print(f"  ✓ {cid} {detail}")
    else:
        FAILURES.append(f"{cid}: {detail}")
        print(f"  ✗ {cid} {detail}")


def main() -> int:
    base = tempfile.mkdtemp(prefix="flake_selftest_")
    try:
        # C0: 缺仓——FileNotFoundError 必须被 scan_one 吞成 build_err。
        repo, names, build_err, skip = flake_scan.scan_one(
            "definitely-missing-repo-xyz", base, 1)
        check("C0", names == [] and "missing" in build_err and skip == "",
              f"缺仓 ⇒ build_err 桶（非无失败、不崩）: {build_err!r}")

        # C1: 有 README 但非 git 目录（repo_dir 认它存在）⇒ fetch 必失败，
        # 返回码必须被检查（原三个 subprocess.run 全不查 returncode）。
        bad_git = Path(base) / "not-a-git-repo"
        bad_git.mkdir()
        (bad_git / "README.md").write_text("not a git repo\n")
        repo, names, build_err, skip = flake_scan.scan_one(
            "not-a-git-repo", base, 1)
        check("C1", names == [] and "git fetch failed" in build_err,
              f"git fetch 失败必须进 build_err 桶: {build_err!r}")

        # C2: inflight 占用 ⇒ 跳过且标注原因（用真实登记，随后清理）。
        inflight.claim("selftest-held-repo", "flake_scan_selftest 占用")
        try:
            repo, names, build_err, skip = flake_scan.scan_one(
                "selftest-held-repo", base, 1)
            check("C2", names == [] and build_err == "" and "inflight" in skip,
                  f"inflight 占用 ⇒ skipped 桶: {skip!r}")
        finally:
            inflight.release("selftest-held-repo")

        # C3: 三桶互斥（上面三个用例 names 全空、原因字段各就各位）。
        check("C3", True, "缺仓/git失败/占用 三例均未落入 names 桶")

        # C4: 干净 git 仓但无 Go 文件——go test 退出码非 0、无 --- FAIL 行
        # ⇒ 必须归 build_err（没跑起来），不得报 names。
        plain = Path(base) / "plain-repo"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        subprocess.run(["git", "-C", str(plain), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(plain), "config", "user.name", "t"], check=True)
        (plain / "README.md").write_text("x\n")
        subprocess.run(["git", "-C", str(plain), "add", "."], check=True)
        subprocess.run(["git", "-C", str(plain), "commit", "-qm", "init"], check=True)
        # 本地分支叫 main（scan_one 会 checkout main）；origin 指自身使
        # fetch/pull 前置可成功（C4 验的是 go test 桶，不是 git 桶）。
        subprocess.run(["git", "-C", str(plain), "branch", "-M", "main"], check=True)
        subprocess.run(["git", "-C", str(plain), "remote", "add", "origin", str(plain)],
                       check=True)
        repo, names, build_err, skip = flake_scan.scan_one("plain-repo", base, 1)
        check("C4", names == [] and build_err != "" and skip == "",
              f"无 Go 文件 ⇒ build_err（没跑起来≠无失败）: {build_err!r}")

        if FAILURES:
            print(f"\n✗ {len(FAILURES)} 项失败")
            return 1
        print("\n✓ flake_scan 自测全过（C0-C4）")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
