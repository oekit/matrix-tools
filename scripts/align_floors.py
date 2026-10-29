#!/usr/bin/env python3
"""矩阵内依赖地板对齐（不动点）：go.mod 的 github.com/oekit/* 地板对齐各依赖最新 tag。

**定位（2026-10-19 起）：维护轮工具，非常规跟版动作。**
发版模型是按需的（pull-based）：底层依赖（pkg/concurrency 等）发新版后，
下游地板不动、不强制发版；下游仓改代码消费到新 API 时 go get 抬地板、
随本仓版本自然发出——谁用新版本谁升级。跨仓地板漂移不是漂移，audit 不检查
（只查嵌套模块对父模块的地板，那是发版正确性要求）。本工具只在显式维护轮
（低频，如月度/大版本对齐后）全量跑，或 --repo 单仓按需对齐。

用法：
  python3 scripts/align_floors.py --plan              # 默认：列出将发生的地板抬升（只读）
  python3 scripts/align_floors.py --repo data --plan  # 单仓按需对齐预览
  python3 scripts/align_floors.py --apply             # 写入 go.mod + GOWORK=off tidy（仅 main+干净仓）
  python3 scripts/align_floors.py --apply --pr        # 变更仓逐仓开 PR（分支 docs/align-floors）
  --repos a,b,c                                       # 子集（缺省全矩阵）
  --base <矩阵检出根目录>                              # 显式指定检出根（默认取 MATRIX_BASE，其次本仓上级）

注意：地板抬升只是本地变更——对齐完成的定义是这些仓以 patch 版发版
（release_train）且下游可见。--apply 输出末尾会列出待发版仓。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import MODULES, OEKIT_REQ_RE, git_branch, is_dirty, latest_tag, repo_dir, sh  # noqa: E402
from prkit import land_pr  # noqa: E402

# 地板扫描用的 require 行正则：**容忍行尾的 `// indirect` 标记**。
#
# 为什么不直接用 matrix.OEKIT_REQ_RE：它以 `$` 收尾、不匹配 indirect 行
# （`\tgithub.com/oekit/concurrency v0.23.0 // indirect`）。而矩阵里**嵌套
# 模块**的依赖几乎全是 indirect 形态（bootstrap/nacos、bootstrap/obs、
# timertask/redis …）⇒ 用它扫描等于**看不见嵌套模块的地板**。
#
# 后果实测（2026-10-29）：bootstrap#50 只抬了根模块的 concurrency，
# 嵌套模块 nacos/obs 里的 concurrency 仍停在 v0.23.0 ⇒ CI 的
# `go mod tidy -diff` 门禁判红；而工具自报「✓ 全矩阵地板零漂移」——
# **工具说对齐了，实际没对齐**，这类假绿比报错更危险。
#
# 独立定义而不改 matrix.OEKIT_REQ_RE：后者的语义是「这个仓**直接**依赖哪些
# 矩阵模块」（dep_matrix 的依赖矩阵用），改动会影响那张表的语义。
FLOOR_REQ_RE = re.compile(
    r"^(?:\t|require )(github\.com/oekit/([a-z0-9-]+)) (v\d+\.\d+\.\d+)( // indirect)?$", re.M)

def parse_semver(v: str) -> tuple[int, int, int]:
    a, b, c = v[1:].split(".")
    return (int(a), int(b), int(c))


def find_gomods(root: Path) -> list[Path]:
    """仓内所有 go.mod（根 + 任意深度的嵌套模块），排除 vendor/.git。

    **必须递归**：嵌套模块的路径深度不固定——bootstrap/nacos、logx/sink 是
    一层，而 kitex 的 observability/tracing/instrumentation/kitexotel 在**第 4 层**。
    只扫一层（`glob("*/go.mod")`）会漏掉深层嵌套模块 ⇒ 父仓地板抬升后
    子模块 go.mod 仍指向旧版本 ⇒ CI 的 `go mod tidy -diff` 门禁判红
    （kitex#58 的真实成因：kitexotel 的 require 停在 pkg v0.15.1，
    而父仓已抬到 v0.30.0）。

    嵌套模块之间有 `replace ... => ../../..` 指向父仓源码，父仓地板一动，
    子模块的间接依赖集合随之变化 ⇒ 子模块必须同步抬，否则 tidy 不一致。
    """
    out = [root / "go.mod"]
    for gm in root.rglob("go.mod"):
        rel = gm.relative_to(root)
        if rel == Path("go.mod"):
            continue
        parts = rel.parts
        if any(p in (".git", "vendor", "node_modules", "testdata") for p in parts):
            continue
        out.append(gm)
    return sorted(out)


def plan_repo(repo: str, base: str) -> dict[Path, list[tuple[str, str, str]]]:
    """{go.mod 路径: [(完整模块路径, 现地板, 目标)]}——只含需要抬升的行。"""
    root = repo_dir(repo, base)
    plans: dict[Path, list[tuple[str, str, str]]] = {}
    gomods = find_gomods(root)
    latest_cache: dict[str, str | None] = {}
    for gm in gomods:
        if not gm.exists():
            continue
        text = gm.read_text()
        ups = []
        for m in FLOOR_REQ_RE.finditer(text):
            full, dep, ver = m.group(1), m.group(2), m.group(3)
            if dep not in latest_cache:
                latest_cache[dep] = latest_tag(dep, base)
            target = latest_cache[dep]
            if not target:
                continue
            if parse_semver(ver) < parse_semver(target):
                ups.append((full, ver, target))
        if ups:
            plans[gm] = ups
    return plans


def apply_edits(gm: Path, ups: list[tuple[str, str, str]]) -> None:
    text = gm.read_text()
    for full, ver, target in ups:
        # 保留原行前缀（块内 tab / 单行 require）——logx/bootstrap 是单行形态。
        #
        # **必须容忍行尾的 `// indirect` 标记**（2026-10-29 修）：go.mod 的间接
        # 依赖行形如 `\tgithub.com/oekit/concurrency v0.23.0 // indirect`。
        # 原正则以 `$` 收尾且不匹配该标记 ⇒ 嵌套模块（bootstrap/nacos、
        # bootstrap/obs 等）的间接依赖行**抬不动**——而 plan 阶段
        # OEKIT_REQ_RE 同样以 `$` 收尾、也匹配不到……
        # ⇒ plan 报「要抬」、apply 静默跳过 ⇒ 工具自称对齐、实际没动，
        # CI 的 `go mod tidy -diff` 门禁随后判红（bootstrap#50 的真实成因）。
        #
        # 匹配要**唯一**：同一模块可能同时有直接依赖行与 indirect 行，
        # 两行都要抬——所以按「整行」计数，允许多行命中但逐行替换。
        pat = re.compile(
            rf"^(\t|require )({re.escape(full)}) {re.escape(ver)}( // indirect)?$", re.M)
        hits = pat.findall(text)
        assert hits, f"{gm}: {full} {ver} 行未找到（计划与实际不一致？）"
        text = pat.sub(rf"\g<1>{full} {target}\g<3>", text)
    gm.write_text(text)


def apply_repo(repo: str, base: str, plans: dict[Path, list[tuple[str, str, str]]]) -> list[str]:
    """把一个仓的地板抬到位，并返回变更文件清单（相对仓根）。

    **关键：改完父仓后，该仓的**所有**嵌套模块都要重跑一次 tidy**——
    哪怕它自身没有 ups（2026-10-30 实测，kitex#65 的真实成因）。

    嵌套模块对父仓有 `replace github.com/oekit/kitex => ../../../..` ⇒ 父仓
    go.mod 的 require 一变，**子模块的依赖图随之变化**：即便子模块自己不直接
    require 那个依赖（如 kitexotel 不 require bootstrap），它的 go.sum 里也会
    多出/换掉那个依赖的哈希（经父仓传递进来）。

    ⇒ 只对「计划里有 ups 的 go.mod」跑 tidy 是不够的：kitexotel 压根不在
    计划里（它没直接 require bootstrap）⇒ 它的 go.sum 永远滞后 ⇒
    CI 的 `go mod tidy -diff` 门禁必红。而工具自报「✓ 已写入并 tidy」——
    又一次**假绿**。

    顺序：**先改全部 go.mod，再统一 tidy**（tidy 会用改后的父仓内容解析）。
    """
    d = repo_dir(repo, base)
    files: list[str] = []
    for gm, ups in plans.items():
        apply_edits(gm, ups)
    # 父仓改完后，仓内**每个**模块都要 tidy（根 + 全部嵌套模块）
    for gm in find_gomods(d):
        if run_tidy(gm.parent) != 0:
            raise SystemExit(f"{repo}/{gm.parent.relative_to(d)}: go mod tidy 失败")
    for gm in find_gomods(d):
        rel = str(gm.relative_to(d))
        if (d / rel).exists() and sh(["git", "status", "--porcelain", rel], d).stdout.strip():
            files.append(rel)
        rel_sum = rel.replace("go.mod", "go.sum")
        if (d / rel_sum).exists() and sh(["git", "status", "--porcelain", rel_sum], d).stdout.strip():
            files.append(rel_sum)
    return files


def main() -> None:
    args = sys.argv[1:]
    do_apply = "--apply" in args
    do_pr = "--pr" in args
    repos = list(MODULES)
    if "--repos" in args:
        repos = args[args.index("--repos") + 1].split(",")
    if "--repo" in args:
        repos = [args[args.index("--repo") + 1]]
    # base 定位认 MATRIX_BASE（CI 的 repos/<repo> 布局）与 --base 显式覆盖。
    # 此前硬编码 parent.parent.parent：matrix.repo_dir 的语义是
    # `base or os.environ.get("MATRIX_BASE", ...)`——base 非空就把环境变量短路掉，
    # 于是 README 承诺的 MATRIX_BASE 对本工具无效，整改命令会打到错误目录（#96）。
    base = str(Path(__file__).resolve().parent.parent.parent)
    if "--base" in args:
        base = args[args.index("--base") + 1]
    else:
        base = os.environ.get("MATRIX_BASE") or base
    base = os.path.abspath(base)

    print("align_floors：依赖地板对齐（维护轮工具 / --repo 按需，非常规跟版）")
    plans_all: dict[str, dict[Path, list[tuple[str, str, str]]]] = {}
    skipped: list[str] = []
    for repo in repos:
        plans = plan_repo(repo, base)
        if plans:
            plans_all[repo] = plans
            for gm, ups in plans.items():
                rel = gm.relative_to(repo_dir(repo, base))
                for full, ver, target in ups:
                    print(f"  {repo:<14} {rel}: {full.split('/')[-1]} {ver} → {target}")
    if not plans_all:
        print("\n✓ 全矩阵地板零漂移")
        return

    changed = list(plans_all)
    if not do_apply:
        print(f"\n{len(changed)} 仓需抬升地板（plan 只读，加 --apply 生效；--pr 直接逐仓开 PR）")
        return

    if do_pr:
        print(f"\n== {len(changed)} 仓逐仓开 PR（分支 docs/align-floors，写入在分支上执行）==")
        for repo in changed:
            d = repo_dir(repo, base)
            if is_dirty(d) or git_branch(d) != "main":
                skipped.append(repo)
                continue
            tags = sorted({t for ups in plans_all[repo].values() for _, _, t in ups})
            url = land_pr(d, repo, "docs/align-floors",
                          f"build: 矩阵依赖地板对齐（{'、'.join(tags)}）",
                          f"build: 矩阵依赖地板对齐（{'、'.join(tags)}）",
                          "align_floors.py 不动点对齐。合入后需 patch 发版（release_train）。",
                          edit=lambda repo=repo: re_apply_for_pr(repo, base), auto_merge=True)
            print(f"  ✓ {repo}: {url or '无变更'}")
    else:
        for repo in changed:
            d = repo_dir(repo, base)
            if is_dirty(d) or git_branch(d) != "main":
                skipped.append(repo)
                continue
            apply_repo(repo, base, plans_all[repo])
            print(f"  ✓ {repo} 已写入并 tidy")
        print(f"\n下一步：{', '.join(changed)}\n"
              "  → 逐仓开 PR 可加 --pr，或手工处理；合入后 release_train 打 patch tag 才算对齐完成")
    if skipped:
        print(f"跳过 {len(skipped)} 仓（非 main 或脏）：{', '.join(skipped)}")


def re_apply_for_pr(repo: str, base: str) -> list[str]:
    """land_pr 的 edit：在分支上重跑本仓的抬升（plan_repo 结果与主流程一致），返回变更文件。"""
    return apply_repo(repo, base, plan_repo(repo, base))


def run_tidy(module_dir: Path) -> int:
    """GOWORK=off tidy（外部用户视角的地板必须可解析）。"""
    env = dict(os.environ, GOWORK="off")
    r = subprocess.run(["go", "mod", "tidy"], cwd=module_dir, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-400:] + r.stderr[-400:])
    return r.returncode


if __name__ == "__main__":
    main()
