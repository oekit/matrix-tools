#!/usr/bin/env python3
"""全矩阵 flaky 扫描：找出「CI 用 -count=1 看不见、只有重复跑才红」的用例。

**为什么需要这个工具**：矩阵 CI 的 test-script 默认是
`go test -race -count=1 ...` ⇒ **只跑一轮**。而有一类缺陷只在**第二轮
起**才显形：

- **进程级全局状态在测试间/轮次间未隔离**（实测 2026-10-30 两例）：
  - `logx`：caller 跳过注册表只增不删 ⇒ 用例自己注册的探针文件留在全局里
    ⇒ 第二轮起探针帧被自己跳过（logx#52 已修）
  - `concurrency`：`GetBreaker` 的 `sharedBreakers` 全局 ⇒ 冲突回调只在
    「首次创建」时有对比对象 ⇒ 第二轮起指纹相同 ⇒ 回调不触发
    （concurrency#119 已修）
- **计数断言把后台流量算进业务路径**（redisx#67 已修）：后台探活与业务共用
  一个计数器。

**这一类问题 CI 永远不会报**（-count=1 时第一轮总是绿的）⇒ 只能靠本地/
巡检链固定跑一轮 `-count>1` 才能发现。

**用法**：
    python3 scripts/flake_scan.py                    # 默认 count=12
    python3 scripts/flake_scan.py --count 5
    python3 scripts/flake_scan.py --repos pkg,logx   # 只扫指定仓
    python3 scripts/flake_scan.py --jobs 4           # 并发仓数（默认 4）

**输出**：每仓一个 `--- FAIL: <用例名>` 清单；有失败则退出码 1（供 CI 判红）。

**注意**：并发跑多个仓会抢 CPU ⇒ 某些**真 flaky**可能因负载而额外暴露，
这正是我们要的（CI 也是共享 runner）。但**假阳性**同样可能（资源不足导致
超时）⇒ 复跑确认：同一用例再跑一次仍红 ⇒ 才是真缺陷。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from matrix import MODULES, git_branch, repo_dir  # noqa: E402

FAIL_RE = re.compile(r"^\s*--- FAIL: (\S+)", re.M)


def scan_one(repo: str, base: str, count: int) -> tuple[str, list[str]]:
    """跑一个仓的 -race -count=N，返回 (仓名, 失败用例名列表)。"""
    d = repo_dir(repo, base)
    if not d.exists():
        return repo, []
    # 测试必须跑在**干净且最新**的 main 上：陈旧的 checkout 会把「已修」
    # 的用例报成仍失败（2026-10-30 踩过：本地落后 ⇒ audit/扫描都假报）。
    subprocess.run(["git", "-C", str(d), "fetch", "origin", "--quiet"],
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(d), "checkout", "main"], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(d), "pull", "--ff-only", "origin", "main"],
                   capture_output=True, text=True)
    env = dict(os.environ, GOWORK="off")
    r = subprocess.run(["go", "test", "-race", f"-count={count}", "./..."],
                       cwd=d, env=env, capture_output=True, text=True)
    out = r.stdout + r.stderr
    names = sorted(set(FAIL_RE.findall(out)))
    return repo, names


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("MATRIX_BASE") or
                    str(HERE.parent.parent))
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--repos", default="")
    args = ap.parse_args()

    base = os.path.abspath(args.base)
    repos = args.repos.split(",") if args.repos else list(MODULES)
    print(f"flake_scan：{len(repos)} 仓，-race -count={args.count}，并发 {args.jobs}")
    print(f"检出根目录：{base}\n")

    bad: dict[str, list[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(scan_one, r, base, args.count): r for r in repos}
        for fut in concurrent.futures.as_completed(futs):
            repo, names = fut.result()
            if names:
                bad[repo] = names
                print(f"  ✗ {repo}: {len(names)} 个用例失败")
                for n in names:
                    print(f"      --- FAIL: {n}")

    if bad:
        print(f"\n发现 {len(bad)} 仓有失败用例（共 "
              f"{sum(len(v) for v in bad.values())} 个）。")
        print("⚠️ 并发跑会放大真 flaky；**复跑确认**：同一用例单独再跑一次仍红 ⇒ 真缺陷。")
        sys.exit(1)
    print("\n✓ 全矩阵无失败用例")


if __name__ == "__main__":
    main()
