#!/usr/bin/env python3
"""组合兼容探测——BOM 快照地板组合编译（红级）+ N-1 落后一档（可见不阻塞）。

## 为什么要有这个（matrix-tools#343，2026-11-27 演进评审 P3）

「这些模块当前互相兼容」是 BOM 的**显式承诺**（README：go get oekit@latest
一次拉齐一组经矩阵 CI 验证的兼容版本），但从未被整体编译验证：

  - 各仓 CI          单模块 @ 自己的地板 —— 看不到全矩阵 MVS 交互；
  - consumer_probe   单模块 @latest 装得上（go list，**不 build**）；
  - scenario_smoke   场景子集 @latest 编译（非地板、非全量单图）；
  - kitex-examples   全套 @ **它自己的**地板，只编译它自己的包；
  - BOM 地板门禁     各仓地板**版本号** ≥ BOM —— 只比数字，不编译。

MVS 取全图最大值：单独装 A 时 A 的传递依赖是 A 地板版本，A+B 同装时被 B 抬高
——A 对抬高后传递依赖的兼容性**只有全量单图编译才测得到**。

## 两探针与裁决

### 探针 A——BOM 快照地板组合编译（红级）
temp module → `go get github.com/oekit/oekit@latest`（README 快速开始的**发布行
原文**——MVS 自动落 BOM 地板，不解析 go.mod，测的就是消费方真实路径）→ 逐单元
`go build <unit>/...`（23 根 + 12 嵌套 + 伞仓 = 36 单元，消费方视角全包编译；
无根包模块 pkg/concurrency/core/network 走子树模式，#341 实证可用）。
失败 ⇒ EXIT_VIOLATION：快照承诺被打破，**可修复**（对齐地板 → 重生成 BOM）。

### 探针 B——N-1 落后一档（⚠ 可见不阻塞，0.x 语义裁决）
每个根模块 X（≥2 个 semver tag）：`go get oekit@latest` → `go mod edit -replace
X=X@N-1`（**replace 强制钉住**——floor > N-1 时纯 require 会被 MVS 抬回去，
探针就测不到 N-1 了）→ `go list -m` 断言解析 == N-1（不符 = 判不了，点名不
静默）→ `go build X/...`（旧代码 × 新依赖）。
失败 ⇒ **⚠ 报告不进退出码**：0.x 语义下旧版与新依赖不兼容属**合法演进**，
已发布 tag 不可变、无可执行修复（对侧是消费者升 X，不是本仓改旧版）——与
audit 反向地板「可见不阻塞」同款裁决；信号价值 = 升级路径摩擦可见（release
note / BOM 文档可引用）。tag 经 git ls-remote 读（无需 clone）；tag 不足 ⇒
可见跳过。

## GOFLAGS=-mod=mod（#341 首跑实录同款）
go get 只记录**被命名包**所需 go.sum 条目，build 拉全包子树要更多条目 ⇒ 缺条目
假红。-mod=mod 自动补记账（报错原文 "to add: go get ..." 给的修复路径），
不掩盖 MVS 解析失败 / 编译错 / 网络 404。

## 退出码契约（生产方定义）

    0 = EXIT_OK          探针 A 全绿（探针 B 只产 ⚠ 与跳过）
    1 = EXIT_VIOLATION   探针 A 有单元编译失败——快照承诺被打破
    2 = EXIT_UNREADABLE  判不了：go 缺失 / go get oekit@latest 失败 /
                         ls-remote 失败 / replace 后解析 ≠ N-1（#79）
    ⚠ 不影响退出码：探针 B 编译失败（0.x 合法演进）、tag 不足跳过
    （A 失败优先于读不到：具体违例盖过覆盖不全，读不到项仍点名在报告里）

用法：
    python3 scripts/compat_probe.py            # 对真网络跑（schedule job 同款）
    python3 scripts/compat_probe.py --json     # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # import matrix（唯一事实源）

import matrix  # noqa: E402
from matrix import BOM_REPO, MODULES, NESTED_MODULES, ORG  # noqa: E402

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNREADABLE = 2

SEMVER_RE = re.compile(r"^v\d+\.\d+\.\d+$")
GET_TIMEOUT = 300    # go get（冷缓存首批要拉全依赖树）
BUILD_TIMEOUT = 600  # go build 单单元
LSREMOTE_TIMEOUT = 60


class Unreadable(Exception):
    """判不了（#79）：go 缺失 / 发布行装不上 / ls-remote 失败 / 解析不符。"""


def semver_key(v: str) -> tuple[int, ...]:
    """semver 降序排序键（与 matrix.latest_tag 同口径；非 semver 由调用方先滤掉）。"""
    return tuple(int(x) for x in v[1:].split("."))


def n_minus_one(tags: list[str]) -> str | None:
    """第二新的 semver tag；不足 2 个 ⇒ None（无 N-1，可见跳过，不是读不到）。"""
    sem = sorted({t for t in tags if SEMVER_RE.match(t)}, key=semver_key, reverse=True)
    return sem[1] if len(sem) >= 2 else None


def ls_root_tags(repo: str) -> list[str]:
    """git ls-remote 读根模块全部 tag（无需 clone）。失败抛 Unreadable。"""
    url = f"https://github.com/{ORG}/{repo}.git"
    last_err = ""
    for _ in range(3):  # 瞬时网络抖动重试（同 matrix._ls_remote_tags 的 attempts）
        r = subprocess.run(["git", "ls-remote", "--tags", "--refs", url],
                           capture_output=True, text=True, timeout=LSREMOTE_TIMEOUT)
        if r.returncode == 0:
            tags = []
            for line in r.stdout.splitlines():
                ref = line.split("refs/tags/")[-1]
                if SEMVER_RE.match(ref):  # 根模块 tag；嵌套前缀（hlog/v0.1.4）在此滤掉
                    tags.append(ref)
            return tags
        last_err = (r.stderr or r.stdout or "").strip()[:300]
    raise Unreadable(f"{repo}: ls-remote 失败（{last_err}）")


def all_units() -> list[str]:
    """36 个发版单元的 import 路径：23 根 + 12 嵌套 + 伞仓。"""
    units = [f"github.com/{ORG}/{m}" for m in MODULES]
    units += [n["module"] for n in NESTED_MODULES]
    units.append(f"github.com/{ORG}/{BOM_REPO}")
    return units


def _env() -> dict[str, str]:
    return {**os.environ, "GOFLAGS": "-mod=mod"}


def _tail(s: str, n: int = 600) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else "…" + s[-n:]


def _run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=_env())


def probe_a(workdir: Path) -> list[str]:
    """探针 A：BOM 快照地板组合编译。返回失败清单（空 = 全绿）。

    go mod init / go get 失败 = 判不了（发布行装不上时无法定义「快照编译」
    这个测试）⇒ Unreadable；单元 build 失败 = 违例 ⇒ 进返回值。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    if _run(["go", "mod", "init", "compat-a"], workdir, 30).returncode != 0:
        raise Unreadable("go mod init 失败（temp 目录/go 环境）")
    line = f"go get github.com/{ORG}/{BOM_REPO}@latest"
    get = _run(line.split(), workdir, GET_TIMEOUT)
    if get.returncode != 0:
        raise Unreadable(f"{line} 失败：{_tail(get.stderr or get.stdout)}")
    failures: list[str] = []
    for u in all_units():
        b = _run(["go", "build", f"{u}/..."], workdir, BUILD_TIMEOUT)
        if b.returncode != 0:
            failures.append(f"go build {u}/... 失败：{_tail(b.stderr or b.stdout)}")
    if failures:
        # 失败时附上解析出的版本组合——地板对齐排查直接看这几十行
        lm = _run(["go", "list", "-m", "all"], workdir, 120)
        if lm.returncode == 0:
            oekit_lines = [l for l in lm.stdout.splitlines() if f"github.com/{ORG}/" in l]
            failures.append("解析组合（go list -m all）：\n" + "\n".join(oekit_lines))
    return failures


def probe_b_unit(repo: str, prev: str, workdir: Path) -> tuple[str, str]:
    """探针 B 单元：N-1 钉住后编译。返回 (status, msg)，status ∈ {ok, warn, unreadable}。"""
    workdir.mkdir(parents=True, exist_ok=True)
    if _run(["go", "mod", "init", "compat-b"], workdir, 30).returncode != 0:
        raise Unreadable("go mod init 失败（temp 目录/go 环境）")
    line = f"go get github.com/{ORG}/{BOM_REPO}@latest"
    get = _run(line.split(), workdir, GET_TIMEOUT)
    if get.returncode != 0:
        raise Unreadable(f"{line} 失败：{_tail(get.stderr or get.stdout)}")
    mod = f"github.com/{ORG}/{repo}"
    _run(["go", "mod", "edit", f"-replace={mod}={mod}@{prev}"], workdir, 30)
    lm = _run(["go", "list", "-m", mod], workdir, 120)
    if lm.returncode != 0:
        raise Unreadable(f"{repo}: go list -m 失败：{_tail(lm.stderr or lm.stdout)}")
    resolved = lm.stdout.split()[-1] if lm.stdout.strip() else ""
    if resolved != prev:
        # replace 没生效（MVS/缓存异常）——测的不是 N-1 就是判不了，不许当通过
        raise Unreadable(f"{repo}: replace 后解析 {resolved} ≠ N-1 {prev}（探针失效）")
    b = _run(["go", "build", f"{mod}/..."], workdir, BUILD_TIMEOUT)
    if b.returncode != 0:
        return "warn", (f"{repo} N-1={prev} 编译失败（0.x 合法演进，可见不阻塞）："
                        f"{_tail(b.stderr or b.stdout)}")
    return "ok", f"{repo} N-1={prev} 编译过"


def classify(rc: int, out: str) -> tuple[str, str]:
    """退出码 → (kind, message)。kind ∈ {ok, violation, unreadable}。"""
    if rc == EXIT_OK:
        return "ok", "BOM 快照地板组合编译全绿"
    if rc == EXIT_VIOLATION:
        return "violation", "BOM 快照组合编译失败（matrix-tools#343）：" + out.strip()
    return "unreadable", out.strip() or "判不了（go / 发布行 / ls-remote）——判不了 ≠ 通过"


def finalize(*, a_failures: list[str], unreadables: list[str]) -> int:
    """退出码裁决：A 失败 > 读不到 > 全绿（⚠ 不进退出码）。"""
    if a_failures:
        return EXIT_VIOLATION
    if unreadables:
        return EXIT_UNREADABLE
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="组合兼容探测（BOM 地板编译 + N-1）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    if shutil.which("go") is None or shutil.which("git") is None:
        return _finish(args, EXIT_UNREADABLE, ["go/git 工具缺失——判不了"], [], [])
    if not MODULES:
        return _finish(args, EXIT_UNREADABLE, ["MODULES 为空——判不了"], [], [])

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001 — reconfigure 不可用不挡主流程
        pass

    a_failures: list[str] = []
    unreadables: list[str] = []
    warns: list[str] = []
    skipped: list[str] = []
    tested = 0

    with tempfile.TemporaryDirectory(prefix="oekit-compat-") as td:
        root = Path(td)
        # ---- 探针 A：BOM 快照地板组合编译（红级）----
        print(f"▸ 探针 A：BOM 快照地板组合编译（{len(all_units())} 单元单图）")
        try:
            a_failures = probe_a(root / "a")
        except Unreadable as e:
            unreadables.append(f"探针 A：{e}")
        except FileNotFoundError:
            return _finish(args, EXIT_UNREADABLE, ["go 工具在跑动中消失"], [], [])
        if a_failures:
            for f in a_failures:
                print(f"  ❌ {f}")
        else:
            print(f"  ✓ 全部 {len(all_units())} 单元编译过")

        # ---- 探针 B：N-1 落后一档（⚠ 不阻塞）----
        print(f"▸ 探针 B：N-1 落后一档（{len(MODULES)} 根模块，replace 钉住）")
        for i, repo in enumerate(MODULES, 1):
            try:
                tags = ls_root_tags(repo)
            except Unreadable as e:
                unreadables.append(f"探针 B：{e}")
                continue
            prev = n_minus_one(tags)
            if prev is None:
                skipped.append(f"{repo}: 仅 {len(set(tags))} 个 semver tag（无 N-1，可见跳过）")
                print(f"  - [{i}/{len(MODULES)}] {repo} 跳过（无 N-1）")
                continue
            try:
                status, msg = probe_b_unit(repo, prev, root / f"b{i}")
            except Unreadable as e:
                unreadables.append(f"探针 B：{e}")
                continue
            except FileNotFoundError:
                return _finish(args, EXIT_UNREADABLE, ["go 工具在跑动中消失"], [], [])
            tested += 1
            if status == "warn":
                warns.append(msg)
                print(f"  ⚠ [{i}/{len(MODULES)}] {msg}")
            else:
                print(f"  ✓ [{i}/{len(MODULES)}] {msg}")

    return _finish(args, finalize(a_failures=a_failures, unreadables=unreadables),
                   a_failures, unreadables, warns, skipped, tested)


def _finish(args, rc: int, a_failures: list, unreadables: list,
            warns: list | None = None, skipped: list | None = None,
            tested: int = 0) -> int:
    warns, skipped = warns or [], skipped or []
    if args.json:
        print(json.dumps({
            "kind": {EXIT_OK: "ok", EXIT_VIOLATION: "violation",
                     EXIT_UNREADABLE: "unreadable"}[rc],
            "probe_a_failures": a_failures,
            "probe_b_warns": warns,
            "probe_b_skipped": skipped,
            "unreadable": unreadables,
            "probe_b_tested": tested,
        }, ensure_ascii=False, indent=1))
    else:
        kind, msg = classify(rc, "\n".join(a_failures + unreadables))
        mark = "✓" if kind == "ok" else "❌"
        print(f"{mark} {msg}")
        if warns:
            print(f"⚠ 探针 B 可见不阻塞 {len(warns)} 项（0.x 合法演进，详见上文）")
        if skipped:
            print(f"- 探针 B 可见跳过 {len(skipped)} 项（无 N-1 tag）")
        if unreadables and kind != "unreadable":
            print(f"⚠ 判不了 {len(unreadables)} 项（不进退出码，详见上文）")
        if unreadables and kind == "unreadable":
            for u in unreadables:
                print(f"  · {u}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
