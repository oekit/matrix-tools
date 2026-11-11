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
import re
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


# 「多仓文档产物」——生成器改了但产物散在 N 个仓的 README/AGENTS 里，
# 单仓 regen+PR 模型套不上（matrix-tools#276：只登记 versions/bom 时，
# 改 gen_tables/refresh_versions/refresh_agents_desc 会得到假的
# 「✓ 无产物待落地——可直接合入」⇒ 24 仓矩阵表瞬间全漂移）。
# 判定模型：产物已落地 = 该模式的 **plan 归零**（doc_wave --<mode> --plan
# 以**当前工作树的新生成器**重算受影响仓；0 = 全部仓的产物已是新生成器的
# 输出 ⇒ 生成器 PR 合入不会再产生漂移）。
ADVISORY = {
    "tables": ("gen_tables.py", "tables"),
    "version-lines": ("refresh_versions.py", "versions"),
    "agents-desc": ("refresh_agents_desc.py", "agents"),
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


# 产物里的时间戳字段：每次重生成都会变，但**不代表内容变了**。
# 判「有没有实质变化」必须剔除它们——否则脚本每次都报「产物有变化」，
# 排查者会学会忽略这个输出，工具就废了。
TS_PATTERNS = (
    re.compile(r'^\s*"generatedAt":.*$', re.M),          # versions.json
    re.compile(r'^const GeneratedAt = ".*"$', re.M),      # matrix.go
    re.compile(r"^// 生成时间 .*$", re.M),                 # matrix.go 头注释
)


def _normalize(text: str) -> str:
    for pat in TS_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


def substantive_change(d: Path, rel: str) -> bool:
    """该产物文件相对 HEAD 是否有**实质**变化（剔除时间戳）。"""
    cur = d / rel
    if not cur.exists():
        return True
    old = subprocess.run(["git", "-C", str(d), "show", f"HEAD:{rel}"],
                         capture_output=True, text=True).stdout
    if not old:
        return True
    try:
        return _normalize(cur.read_text(encoding="utf-8")) != _normalize(old)
    except UnicodeDecodeError:
        return True


def substantive_files(d: Path, files: list[str]) -> list[str]:
    """变化文件里**剔除**只有时间戳差异的（其余才算要落地）。"""
    out = []
    for rel in files:
        if substantive_change(d, rel):
            out.append(rel)
    return out


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
    ap.add_argument("--only", default="",
                    help="只处理某类产物（versions / bom / tables / version-lines / agents-desc）")
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

    all_keys = list(PRODUCTS) + list(ADVISORY)
    keys = [args.only] if args.only else all_keys
    for k in keys:
        if k not in all_keys:
            raise SystemExit(f"未知产物 {k}（可选：{', '.join(all_keys)}）")
    todo: list[tuple[str, str, list[str]]] = []
    advisory_todo: list[str] = []
    for key in keys:
        if key not in all_keys:  # #293①：advisory 键合法（--only help 明列五键），不许被 PRODUCTS 守卫拒
            raise SystemExit(f"未知产物 {key}（可选：{', '.join(all_keys)}）")
        if key not in PRODUCTS:
            continue  # advisory 键走下方 ADVISORY 循环
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
        real = substantive_files(d, ch)
        if not real:
            print(f"    仅时间戳变化（{', '.join(ch)}）——不算实质变更，无需落地")
            continue
        print(f"    产物变化：{', '.join(real)}")
        todo.append((key, repo, real))

    # advisory 面（matrix-tools#276）：改了 gen_tables / refresh_versions /
    # refresh_agents_desc ⇒ 必须先用**新**生成器把 N 仓产物落完（plan 归零）
    # 才能「直接合入生成器 PR」——否则就是本单实测的 24 仓瞬间全漂移。
    for key, (gen, mode) in ADVISORY.items():
        if key not in keys:
            continue
        # #293②：不再按「分支 diff 里的生成器文件名」过滤触发——matrix.py 等
        # 事实输入变更同样产出漂移（947f182 纯 DESC 编辑零提示实证），且 plan
        # 归零本就是状态判据（注释模型）：统一跑，靠归零判定，触发面无旁路。
        r = subprocess.run(
            [sys.executable, str(here / "scripts" / "doc_wave.py"), f"--{mode}", "--plan"],
            capture_output=True, text=True, timeout=600)
        m = re.search(r"分支 \S+：(\d+) 仓", r.stdout)
        n = int(m.group(1)) if m else -1
        if n != 0:
            advisory_todo.append(
                f"{key}（{gen}）：doc_wave --{mode} --plan 还有 "
                f"{'未知（plan 输出不可解析，请手工确认）' if n < 0 else f'{n} 仓'} 产物未落地"
                f"⇒ 先 `doc_wave --{mode} --pr` → 等 CI → `doc_wave --{mode} --merge-all`，"
                f"plan 归零后再合生成器 PR")
        else:
            print(f"[{key}] plan 已归零——{gen} 的新输出已全部落地")

    if not todo and not advisory_todo:
        print("\n✓ 无产物待落地——生成器 PR 可直接合入")
        return 0

    print("\n落地顺序（每步都等前一步合并后再做下一步）：")
    for i, (key, repo, ch) in enumerate(todo, 1):
        print(f"  {i}. {repo}：{', '.join(ch)}（产物 {key}）")

    if advisory_todo:
        print("\n⚠ 多仓文档产物未随本分支的新生成器落地（matrix-tools#276）：")
        for a in advisory_todo:
            print(f"  - {a}")
        if not todo:
            return 1
    if not args.apply:
        print("\n（只检查模式；加 --apply 开 PR）")
        print("⚠ 关键：产物必须由**新**生成器产出——上面跑的就是新生成器，"
              "不要用旧生成器重跑后再落地（那样门禁会报「多出」，见 matrix-tools#34）")
        return 1  # #293③：check 模式 todo/advisory_todo 任一非空统一非零（此前 todo 非空反而 0，退出码倒挂）

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
