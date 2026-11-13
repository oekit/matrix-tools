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

**输出**：每仓一个 `--- FAIL: <用例名>` 清单；有失败、有仓**没跑起来**
（构建失败 / git 前置失败 / 缺仓）或有仓**没扫**（在途登记持有而跳过）则
退出码 1（供 CI 判红）——三者都不是「无失败用例」（#139/#313）。

**注意**：并发跑多个仓会抢 CPU ⇒ 某些**真 flaky**可能因负载而额外暴露，
这正是我们要的（CI 也是共享 runner）。但**假阳性**同样可能（资源不足导致
超时）⇒ 复跑确认：同一用例再跑一次仍红 ⇒ 才是真缺陷。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from matrix import MODULES, git_branch, repo_dir  # noqa: E402
from inflight import held as inflight_held, read as inflight_read  # noqa: E402

FAIL_RE = re.compile(r"^\s*--- FAIL: (\S+)", re.M)
# 编译/构建失败（不是用例失败）：go test 退出码非 0 且没有 "--- FAIL" 行
BUILD_ERR_RE = re.compile(r"^(?:.*\n)*?[^\n]*\b(?:build failed|undefined:|cannot use|"
                          r"syntax error|no required module)\b", re.M)


def go_available() -> bool:
    """go 命令是否可用。

    **用 shutil.which 而不是 subprocess.run(["go", ...])**：后者在 go 不存在时
    抛 FileNotFoundError ⇒ 检测本身崩掉 ⇒ 报的是 traceback 而不是
    「Go 不可用」⇒ 反而更难看出是环境问题（2026-10-30 实测）。
    """
    return shutil.which("go") is not None


def scan_one(repo: str, base: str, count: int) -> tuple[str, list[str], str, str]:
    """跑一个仓的 -race -count=N。

    返回 (仓名, 失败用例名列表, 没跑起来的原因, 在途跳过的原因)——后两者
    互斥且都**不等于「无失败用例」**。

    **三类「没跑起来」必须与「用例失败」分开报**（#313 收编前两类）：
      1. 构建/编译失败（2026-10-30 加）：`go test` 编译不过时退出码非 0 但
         输出里没有 `--- FAIL` 行 ⇒ 只按 `--- FAIL` 收集会把该仓记成
         「无失败用例」⇒ 扫描看起来跑过了，实际什么都没测；
      2. git 前置失败（#313）：fetch/checkout/pull 任一失败都说明现场**不在
         干净且最新的 main 上**——陈旧 checkout 把「已修」的用例报成仍失败
         （2026-10-30 假报事故），停在别的分支则连测试对象都不是 main。
         此前三条命令全部不看 returncode，注释里点名要防的事故可原样复发；
      3. 缺仓（#313）：`repo_dir` 对不存在的仓直接 raise FileNotFoundError，
         此前 `if not d.exists()` 是永不可达的死分支，异常穿过 fut.result()
         打崩整轮扫描（而不是按契约返回「没跑起来」）。
    """
    # 在途互斥（#313）：与 preflight/handoff/release_train 同一面。该仓被活
    # 进程持有时**不碰它的检出**——直接 checkout/pull 会把对方的现场切走。
    cur = inflight_read(repo)
    if cur and inflight_held(cur):
        why = (f"在途登记持有（pid {cur.get('pid')}，"
               f"{cur.get('note', '')}）——不切分支、不跑测试")
        return repo, [], "", why

    try:
        d = repo_dir(repo, base)
    except FileNotFoundError as e:
        # 缺仓 ≠ 零失败：按「没跑起来」上报，绝不让单仓缺失打崩整轮。
        return repo, [], f"仓检出缺失：{e}", ""
    if not d.exists():
        return repo, [], f"仓检出不存在：{d}", ""

    # 测试必须跑在**干净且最新**的 main 上：陈旧的 checkout 会把「已修」
    # 的用例报成仍失败（2026-10-30 踩过：本地落后 ⇒ audit/扫描都假报）。
    # 三条 git 前置命令逐条查 returncode（#313）：任一失败 ⇒ 该仓按「没跑
    # 起来」上报并带上 stderr 摘要，**不**在错误现场上跑测试——否则结果会
    # 冠以「全矩阵 flaky 扫描」进报告，比假报更糟。
    for step in (["fetch", "origin", "--quiet"],
                 ["checkout", "main"],
                 ["pull", "--ff-only", "origin", "main"]):
        g = subprocess.run(["git", "-C", str(d), *step],
                           capture_output=True, text=True)
        if g.returncode != 0:
            tail = (g.stderr or g.stdout or "").strip().splitlines()
            detail = tail[-1][:200] if tail else f"exit={g.returncode}"
            return repo, [], (f"git {step[0]} 失败（exit={g.returncode}）："
                              f"{detail}"), ""

    env = dict(os.environ, GOWORK="off")
    r = subprocess.run(["go", "test", "-race", f"-count={count}", "./..."],
                       cwd=d, env=env, capture_output=True, text=True)
    out = r.stdout + r.stderr
    names = sorted(set(FAIL_RE.findall(out)))
    build_err = ""
    if r.returncode != 0 and not names:
        # 退出码非 0 却没有用例失败 ⇒ 编译/构建/环境问题
        m = BUILD_ERR_RE.search(out)
        build_err = (m.group(0).strip().splitlines()[-1] if m
                     else out.strip().splitlines()[-1] if out.strip()
                     else f"exit={r.returncode}")
    return repo, names, build_err, ""


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

    # **Go 不可用 ⇒ 直接失败**（2026-10-30 加）：没有 go 命令时每个仓的
    # go test 都会立刻失败 ⇒ 若照旧只收集 `--- FAIL`，结果是「全矩阵无失败
    # 用例」——**看起来扫过了，实际一个测试都没跑**。这比报错危险得多
    # （与 align_floors 的三个假绿同族：判据没覆盖真实失败面）。
    if not go_available():
        print("✗ 找不到 go 命令——flaky 扫描无法执行（**不是**「无失败用例」）")
        print("  ⇒ 巡检链必须装 Go（maintenance.yml 的 setup-go 步骤）")
        sys.exit(2)

    bad: dict[str, list[str]] = {}
    # 「没跑起来」桶：构建/编译失败 + git 前置失败 + 缺仓（#313 三类同口径）。
    not_run: dict[str, str] = {}
    # 「没扫」桶：在途登记持有 ⇒ 不碰对方检出（#313）。同样**不是**「无失败
    # 用例」——全矩阵的 claim 只有真跑过的仓才算数。
    skipped: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(scan_one, r, base, args.count): r for r in repos}
        for fut in concurrent.futures.as_completed(futs):
            repo = futs[fut]
            try:
                repo, names, not_run_err, skip_err = fut.result()
            except Exception as e:  # noqa: BLE001 —— 单仓异常不得打崩整轮
                # scan_one 内已捕获 repo_dir 的 FileNotFoundError；这里兜的是
                # 其余未预期异常——整轮崩掉等于「一个仓出事、全矩阵没结论」，
                # 与「没跑起来 ≠ 无失败用例」同一族（#313）。
                not_run[repo] = f"{type(e).__name__}: {e}"
                print(f"  ✗ {repo}: **扫描异常**（不是用例失败）——{type(e).__name__}: {e}")
                continue
            if skip_err:
                skipped[repo] = skip_err
                print(f"  ⊘ {repo}: 跳过（在途占用，未扫描）——{skip_err}")
            if not_run_err:
                not_run[repo] = not_run_err
                print(f"  ✗ {repo}: **没跑起来**（不是用例失败）——{not_run_err}")
            if names:
                bad[repo] = names
                print(f"  ✗ {repo}: {len(names)} 个用例失败")
                for n in names:
                    print(f"      --- FAIL: {n}")

    if not_run:
        print(f"\n⚠️ {len(not_run)} 仓**没跑起来**"
              f"（构建失败 / git 前置失败 / 缺仓）：")
        for repo, err in not_run.items():
            print(f"    {repo}: {err}")
        print("  ⇒ 这些仓的测试**未被检验**，不能算「无失败用例」。")
    if skipped:
        print(f"\n⚠️ {len(skipped)} 仓**没扫**（在途登记持有，为不冲掉对方现场"
              f"而跳过）：")
        for repo, why in skipped.items():
            print(f"    {repo}: {why}")
        print("  ⇒ 这些仓**未被扫描**，同样不能算「无失败用例」"
              f"（收尾后 inflight.py release 再复扫）。")
    if bad:
        print(f"\n发现 {len(bad)} 仓有失败用例（共 "
              f"{sum(len(v) for v in bad.values())} 个）。")
        print("⚠️ 并发跑会放大真 flaky；**复跑确认**：同一用例单独再跑一次仍红 ⇒ 真缺陷。")
    # 判据不可用即红（#139/#313）：没跑起来、没扫、有失败——任一成立都不能
    # 输出「✓ 全矩阵无失败用例」。
    if bad or not_run or skipped:
        sys.exit(1)
    print("\n✓ 全矩阵无失败用例")


if __name__ == "__main__":
    main()
