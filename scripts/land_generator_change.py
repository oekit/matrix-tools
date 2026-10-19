#!/usr/bin/env python3
"""跨仓生成器变更的落地顺序工具（matrix-tools#34）。

## 为什么需要这个脚本

audit 门禁在 **PR 模式**下比对的是「**当前生成器的输出** vs **已发布产物**」
（站点 versions.json / oekit BOM 的 go.mod+matrix.go）。改了生成器之后，
两侧只有一种组合能对上：

| 落地顺序 | 生成器 | 已发布产物 | 门禁结果 |
|---|---|---|---|
| A. 先合生成器 | 新 | 旧 | ❌ 漏收 N 条（产物缺新条目） |
| B. 用旧生成器落产物 | 旧 | 新 | ❌ 多出 N 条（产物有旧生成器产不出的条目） |
| C. **用新生成器落产物 → 再合生成器** | 新 | 新 | ✅ 通过 |

**最容易漏的是 B**：直觉上「先落产物再合生成器」听起来对，但产物必须由
**新**生成器产出——用旧生成器产出的产物落下去，门禁照样红，且报错只说
「漏收/多出 N 条」，完全不提落地顺序，排查成本高（#34 的实测记录）。

本脚本把顺序从「人记」变成「脚本执行」：跑生成器 → diff 产物 → 按序开产物 PR
（站点 → BOM）→ 打印「产物 PR 已开，等它们合并后就可以合生成器 PR 了」。

## 用法

```bash
python3 scripts/land_generator_change.py                    # 只检查：跑生成器 + diff + 列出待落地产物
python3 scripts/land_generator_change.py --apply            # 执行：按序开产物 PR
python3 scripts/land_generator_change.py --branch feat/x    # 指定生成器改动所在分支（默认当前分支）
python3 scripts/land_generator_change.py --only versions    # 只处理某一类产物（versions / bom）
```

默认是**只读检查**（跑生成器会写产物文件，但不 commit、不 push、不开 PR），
`--apply` 才会推分支、开 PR。

## 产物清单

| 产物 | 生成器 | 落地仓 | 说明 |
|---|---|---|---|
| versions | scripts/gen_versions.py | oekit.github.io | `docs/public/versions.json` |
| bom | scripts/gen_matrix_go.py | oekit | `go.mod` + `matrix.go`（兼容组合快照） |
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import ORG, repo_dir  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
SITE_REPO = "oekit.github.io"
BOM_REPO = "oekit"

# 产物定义：key → (生成器脚本, 落地仓, 产物相对路径, PR 标题, PR 正文)
PRODUCTS = {
    "versions": (
        "gen_versions.py",
        SITE_REPO,
        ["docs/public/versions.json"],
        "docs: 版本矩阵产物随生成器变更更新",
        "随 `matrix-tools` 的生成器变更重新生成（产物由**新**生成器产出）。\n\n"
        "落地顺序：先合本 PR（产物），再合生成器 PR——否则 audit 门禁会报"
        "「漏收/多出」（见 matrix-tools#34）。",
    ),
    "bom": (
        "gen_matrix_go.py",
        BOM_REPO,
        ["go.mod", "matrix.go"],
        "feat(bom): 兼容组合快照随生成器变更更新",
        "随 `matrix-tools` 的生成器变更重新生成（产物由**新**生成器产出）。\n\n"
        "落地顺序：先合本 PR（产物），再合生成器 PR——否则 audit 的 BOM 新鲜度门禁"
        "会报「BOM 陈旧」（见 matrix-tools#34）。",
    ),
}


def sh(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"{' '.join(args)} 失败：{(r.stderr or r.stdout)[-400:]}")
    return r


def is_dirty(d: Path) -> bool:
    return bool(subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip())


def current_branch(d: Path) -> str:
    return subprocess.run(["git", "-C", str(d), "branch", "--show-current"],
                          capture_output=True, text=True).stdout.strip()


def run_generator(script: str) -> None:
    """跑生成器（生成器自带干净树守卫——产物必须落在已提交基线上）。"""
    print(f"  · 跑生成器 {script} …")
    r = subprocess.run([sys.executable, str(Path(__file__).parent / script)],
                       cwd=str(Path(__file__).parent), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"生成器 {script} 失败：{(r.stderr or r.stdout)[-600:]}")
    out = (r.stdout + r.stderr).strip().splitlines()
    for line in out[-3:]:
        print(f"    {line}")


def changed_files(d: Path, files: list[str]) -> list[str]:
    r = subprocess.run(["git", "-C", str(d), "status", "--porcelain", "--"] + files,
                       capture_output=True, text=True)
    return [l[3:].strip() for l in r.stdout.splitlines() if l.strip()]


def open_pr(repo: str, branch: str, title: str, body: str, message: str) -> str:
    """建分支 → 提交 → 推 → 开 PR（已存在同分支 PR 时复用）。"""
    d = repo_dir(repo, BASE)
    sh(["git", "-C", str(d), "checkout", "-B", branch], d)
    sh(["git", "-C", str(d), "add", "-A"], d)
    sh(["git", "-C", str(d), "commit", "-m", message], d)
    sh(["git", "-C", str(d), "push", "-u", "origin", branch], d)
    existing = subprocess.run(
        ["gh", "pr", "list", "-R", f"{ORG}/{repo}", "--head", branch, "--state", "open",
         "--json", "number", "--jq", ".[].number"],
        cwd=d, capture_output=True, text=True).stdout.strip()
    if existing:
        print(f"    PR 已存在（#{existing}），未重复创建")
        return f"https://github.com/{ORG}/{repo}/pull/{existing.splitlines()[0]}"
    url = subprocess.run(
        ["gh", "pr", "create", "-R", f"{ORG}/{repo}", "--base", "main", "--head", branch,
         "--title", title, "--body", body],
        cwd=d, capture_output=True, text=True)
    if url.returncode != 0:
        raise SystemExit(f"开 PR 失败：{(url.stderr or url.stdout)[-400:]}")
    print(f"    {url.stdout.strip()}")
    return url.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="跨仓生成器变更的落地顺序工具")
    ap.add_argument("--apply", action="store_true", help="真的开 PR（默认只检查）")
    ap.add_argument("--branch", default="", help="生成器改动所在分支（默认当前分支）")
    ap.add_argument("--only", default="", help="只处理某类产物（versions / bom）")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent.parent
    branch = args.branch or current_branch(here)
    if not branch:
        print("✗ 不知道生成器分支（当前是 detached HEAD？用 --branch 指定）")
        return 2
    print(f"生成器分支：{branch}")
    if branch != current_branch(here):
        sh(["git", "-C", str(here), "checkout", branch], here)
    if is_dirty(here):
        raise SystemExit("matrix-tools 工作树不干净——生成器改动必须先提交")

    keys = [args.only] if args.only else list(PRODUCTS)
    todo: list[tuple[str, str, list[str]]] = []
    for key in keys:
        if key not in PRODUCTS:
            raise SystemExit(f"未知产物 {key}（可选：{', '.join(PRODUCTS)}）")
        script, repo, files, _title, _body = PRODUCTS[key]
        d = repo_dir(repo, BASE)
        if is_dirty(d):
            raise SystemExit(f"{repo} 工作树不干净——生成器产物必须落在已提交基线上")
        print(f"\n[{key}] {script} → {repo}")
        run_generator(script)
        ch = changed_files(d, files)
        if not ch:
            print("    产物无变化——无需落地")
            continue
        print(f"    产物变化：{', '.join(ch)}")
        todo.append((key, repo, ch))

    if not todo:
        print("\n✓ 无产物待落地——生成器 PR 可直接合入")
        return 0

    print("\n落地顺序（每步都等前一步合并后再做下一步）：")
    for i, (key, repo, ch) in enumerate(todo, 1):
        print(f"  {i}. {repo}：{', '.join(ch)}（产物 {key}）")

    if not args.apply:
        print("\n（只检查模式；加 --apply 开 PR）")
        print("⚠ 关键：产物必须由**新**生成器产出——上面跑的就是新生成器，"
              "不要用旧生成器重跑后再落地（那样门禁会报「多出」，见 matrix-tools#34）")
        return 0

    prs = []
    for key, repo, _ch in todo:
        script, _repo, _files, title, body = PRODUCTS[key]
        pr_branch = f"chore/land-{key}-generator"
        print(f"\n→ 开 {repo} 的产物 PR（分支 {pr_branch}）")
        url = open_pr(repo, pr_branch, title, body, f"chore: 生成器产物更新（{key}）")
        prs.append((repo, url))

    print("\n✓ 产物 PR 已开：")
    for repo, url in prs:
        print(f"  · {repo}: {url}")
    print("\n下一步：等这些产物 PR 合并后，再合生成器 PR"
          "（顺序反了 audit 门禁会红，见 matrix-tools#34）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
