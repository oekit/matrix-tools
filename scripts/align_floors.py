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
from matrix import MODULES, git_branch, is_dirty, latest_tag, repo_dir, sh  # noqa: E402
from prkit import land_pr  # noqa: E402

REQ_RE = re.compile(r"^\t(github\.com/oekit/([a-z0-9-]+)) (v\d+\.\d+\.\d+)$", re.M)


def parse_semver(v: str) -> tuple[int, int, int]:
    a, b, c = v[1:].split(".")
    return (int(a), int(b), int(c))


def plan_repo(repo: str, base: str) -> dict[Path, list[tuple[str, str, str]]]:
    """{go.mod 路径: [(完整模块路径, 现地板, 目标)]}——只含需要抬升的行。"""
    root = repo_dir(repo, base)
    plans: dict[Path, list[tuple[str, str, str]]] = {}
    gomods = [root / "go.mod"] + sorted(root.glob("*/go.mod"))
    latest_cache: dict[str, str | None] = {}
    for gm in gomods:
        if not gm.exists():
            continue
        text = gm.read_text()
        ups = []
        for m in REQ_RE.finditer(text):
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
        old, new = f"\t{full} {ver}", f"\t{full} {target}"
        assert text.count(old) == 1, f"{gm}: {old} 不唯一"
        text = text.replace(old, new)
    gm.write_text(text)


def main() -> None:
    args = sys.argv[1:]
    do_apply = "--apply" in args
    do_pr = "--pr" in args
    repos = list(MODULES)
    if "--repos" in args:
        repos = args[args.index("--repos") + 1].split(",")
    if "--repo" in args:
        repos = [args[args.index("--repo") + 1]]
    base = str(Path(__file__).resolve().parent.parent.parent)

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
                          edit=lambda repo=repo: re_apply_for_pr(repo, base))
            print(f"  ✓ {repo}: {url or '无变更'}")
    else:
        for repo in changed:
            d = repo_dir(repo, base)
            if is_dirty(d) or git_branch(d) != "main":
                skipped.append(repo)
                continue
            for gm, ups in plans_all[repo].items():
                apply_edits(gm, ups)
                if run_tidy(gm.parent) != 0:
                    raise SystemExit(f"{repo}/{gm.parent.name}: go mod tidy 失败")
            print(f"  ✓ {repo} 已写入并 tidy")
        print(f"\n下一步：{', '.join(changed)}\n"
              "  → 逐仓开 PR 可加 --pr，或手工处理；合入后 release_train 打 patch tag 才算对齐完成")
    if skipped:
        print(f"跳过 {len(skipped)} 仓（非 main 或脏）：{', '.join(skipped)}")


def re_apply_for_pr(repo: str, base: str) -> list[str]:
    """land_pr 的 edit：在分支上重跑本仓的抬升（plan_repo 结果与主流程一致），返回变更文件。"""
    d = repo_dir(repo, base)
    files = []
    for gm, ups in plan_repo(repo, base).items():
        apply_edits(gm, ups)
        if run_tidy(gm.parent) != 0:
            raise SystemExit(f"{repo}: go mod tidy 失败")
        files.append(str(gm.relative_to(d)))
        rel_sum = str(gm.relative_to(d)).replace("go.mod", "go.sum")
        if (d / rel_sum).exists() and sh(["git", "status", "--porcelain", rel_sum], d).stdout.strip():
            files.append(rel_sum)
    return files


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
