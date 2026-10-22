#!/usr/bin/env python3
"""oekit PR 流程：main 分支保护铺开器（直推 main 一律被挡，改动必须走 PR）。

用法：
    python3 scripts/pr_flow.py            # dry-run，打印将发生的变更
    python3 scripts/pr_flow.py --apply    # 真写 GitHub
    python3 scripts/pr_flow.py --apply --repo concurrency   # 只动一个仓

设计原则（三条，都是踩过才知道的）：
  1. **只增不改检查项**——现网 required_status_checks 的上下文由各仓自己的 workflow 决定，
     本脚本读现状、只追加 PR 要求，绝不重写。模块仓共用 go-ci.yml（Lint & Test / Lint、
     Lint & Test / Test），但个别仓额外挂了自���检查（如 pkg 的 Zero Third-Party Deps），
     假定统一会误删。
  2. **不新增无法在 PR 上产生的检查项**——要求一个只在 schedule / push-main 触发的检查项，
     等于把该仓所有 PR 永久卡死（检查永远 pending，PR 永远不能合）。
     protect-release-tags 这类 tag 规则与本脚本无关，由 rulesets 单独管。
  3. **enforce_admins 才是真开关**——GitHub 默认让管理员绕过分支保护，这正是
     "Bypassed rule violations for refs/heads/main" 的来源：检查挂着照样直推。
     只开 required_pull_request_reviews 而不开 enforce_admins 等于没开。

单人维护者场景（zy84338719 一人）：required_approving_review_count = 0——
PR 是必须的，但不必等人批准，合入者即作者。GitHub 不允许作者 approve 自己的 PR，
设成 1 会让这个 org 无人能合并。若日后有第二位维护者，调到 1 即可。

新增项：
  required_pull_request_reviews  走 PR（0 批准），stale review 自动失效
  enforce_admins                 管理员不再豁免 ← 修的就是这个
  required_status_checks.strict  分支须与 main 同步后才能合（避免合入带病基线）
  required_conversation_resolution  讨论未解决不能合
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghsafe  # noqa: E402

ORG = "oekit"

# 目标仓：24 个模块仓 + 4 个元仓。
MODULES = [
    "pkg", "concurrency", "logx", "core", "kitx",
    "metrics", "tracing", "registry",
    "data", "gormx", "redisx", "etcdx", "httpx", "network", "timertask", "middleware", "k8s",
    "nns", "nns-endpoint",
    "bootstrap", "hertz", "kitex", "workflow", "kitex-examples",
]
META = ["oekit", "matrix-tools", "oekit.github.io", ".github"]

# 无分支保护的元仓，其检查项按「能在 PR 上真正产出」手工核验过：
#   oekit.github.io  ci.yml build（PR 触发）；deploy.yml 只在 push main 跑 → 不得要求
#   .github          repo-ci.yml Scripts + workflow-lint.yml actionlint（均 push+PR 触发）
#   oekit            仅看板 workflow，无代码 CI → 不要求任何检查
#   matrix-tools     audit.yml 只有 schedule/workflow_dispatch → 要求它会卡死 PR
NO_PROTECTION_CHECKS = {
    "oekit.github.io": ["build"],
    ".github": ["Scripts", "actionlint"],
    "oekit": [],
    "matrix-tools": [],
}

REVIEW = {
    "dismiss_stale_reviews": True,
    "require_code_owner_reviews": False,
    "required_approving_review_count": 0,
}


def gh(*args: str, check: bool = True) -> tuple[int, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} 失败：{p.stderr.strip() or p.stdout.strip()}")
    return p.returncode, p.stdout


def current_protection(repo: str) -> dict | None:
    """取现状；404 = 完全无保护（返回 None，区别于空配置）。

    **「读不到」不能当成「没有保护」**（2026-10-22 实测）：GitHub API 会
    间歇性返回空正文/空对象（curl 直连同样可复现）⇒ 旧实现 JSON 解析失败
    也返回 None ⇒ 把「读不到现状」当成「这仓没有保护」⇒ 按无保护去改配置。
    ⇒ 现在的口径：404 才当「无保护」；读不到就**显式失败**。
    """
    try:
        data = ghsafe.gh_json(["api", f"repos/{ORG}/{repo}/branches/main/protection"])
    except ghsafe.GhCommandError as e:
        if e.is_not_found:
            return None
        raise SystemExit(f"{repo}: 读取分支保护失败：{e}")
    except ghsafe.GhReadError as e:
        raise SystemExit(
            f"{repo}: 读取分支保护失败（{e}）——远端偶发返回空内容时不能当成"
            "「这仓没有保护」，否则会按错误现状去改配置。脚本中止。")
    if not isinstance(data, dict) or not data:
        raise SystemExit(f"{repo}: 分支保护返回空内容，按「读不到」中止（不当作无保护）")
    return data


def desired(repo: str, cur: dict | None) -> dict:
    """按现状算出目标配置——检查项只增不改（原则 1）。"""
    if cur is None:
        contexts = NO_PROTECTION_CHECKS.get(repo)
        if contexts is None:
            raise SystemExit(f"{repo}: 无分支保护且未登记检查项，需先核验其 workflow 触发条件")
        rsc = {"strict": True, "contexts": contexts} if contexts else None
    else:
        old = cur.get("required_status_checks")
        contexts = list(old.get("contexts") or []) if old else []
        rsc = {"strict": True, "contexts": contexts} if contexts else None

    return {
        "required_status_checks": rsc,
        "enforce_admins": True,
        "required_pull_request_reviews": dict(REVIEW),
        "restrictions": None,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "block_creations": False,
        "required_conversation_resolution": True,
        "lock_branch": False,
        "allow_fork_syncing": True,
    }


def diff_summary(repo: str, cur: dict | None, want: dict) -> list[str]:
    out = []
    has_rpr = bool(cur and cur.get("required_pull_request_reviews"))
    admins_on = bool(cur and cur.get("enforce_admins", {}).get("enabled"))
    if not has_rpr:
        out.append("要求走 PR（0 批准，作者可自合）")
    if not admins_on:
        out.append("enforce_admins=true（管理员不再豁免直推）")
    old_strict = (cur or {}).get("required_status_checks", {}) or {}
    if want["required_status_checks"] and not old_strict.get("strict"):
        out.append("status checks strict=true（须与 main 同步）")
    if (want["required_status_checks"] or {}).get("contexts") and not old_strict.get("contexts"):
        out.append(f"首次登记检查项：{want['required_status_checks']['contexts']}")
    if not out:
        out.append("已符合，无需变更")
    return out


def apply(repo: str, want: dict) -> None:
    p = subprocess.run(
        ["gh", "api", "--method", "PUT",
         f"repos/{ORG}/{repo}/branches/main/protection", "--input", "-"],
        input=json.dumps(want), capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"{repo} 写入失败：{p.stderr.strip() or p.stdout.strip()}")


def main() -> None:
    args = sys.argv[1:]
    do_apply = "--apply" in args
    only = None
    if "--repo" in args:
        only = args[args.index("--repo") + 1]

    repos = [r for r in MODULES + META if only is None or r == only]
    print(f"oekit PR 流程 —— {'APPLY' if do_apply else 'DRY-RUN'}，{len(repos)} 个仓\n")

    changed = 0
    for repo in repos:
        cur = current_protection(repo)
        want = desired(repo, cur)
        notes = diff_summary(repo, cur, want)
        if notes == ["已符合，无需变更"]:
            print(f"☑ {repo:<16} {notes[0]}")
            continue
        changed += 1
        print(f"{'→' if do_apply else '·'} {repo:<16} " + "；".join(notes))
        if do_apply:
            apply(repo, want)

    print(f"\n{changed}/{len(repos)} 个仓需要变更。" + ("" if do_apply else "  加 --apply 生效。"))


if __name__ == "__main__":
    main()
