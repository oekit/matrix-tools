#!/usr/bin/env python3
"""GitHub Release 对账/回填（oekit/.github#8）：每个 tag 对应一个 Release。

#8 之前 oekit 全矩阵只有 tag、没有任何 Release（`releases/latest` 全部 404）——
下游没有 breaking change 的归档位，release-please / goreleaser 类工具无从消费，
「这一版验证过的模块组合」也没有地方声明。本脚本把缺口收成一条命令：

  - notes = CHANGELOG.md 里同版本小节（嵌套 tag 先找 `<sub>/CHANGELOG.md` 的
    `[vX.Y.Z]`，再回退根 CHANGELOG 的 `[<sub>/vX.Y.Z]`，如 bootstrap/nacos）；
  - notes 末尾附「矩阵内依赖地板」段：该 tag go.mod 里全部 github.com/oekit/*
    require 行——这正是「这一组是 CI 验证过的兼容组合」的可审计声明；
  - 除最新根模块 tag 外全部 `--latest=false`，保证 `releases/latest` 指向最新版；
  - 幂等：已存在的 Release 跳过，重复跑无副作用。

用法：
  python3 scripts/gen_releases.py                 # dry-run：列出缺 Release 的 tag
  python3 scripts/gen_releases.py --apply         # 创建缺失的 Release
  python3 scripts/gen_releases.py --check         # 门禁模式：有缺失则 exit 1（audit 调用）
  python3 scripts/gen_releases.py --repo data --apply
  python3 scripts/gen_releases.py --repo data --tag v0.9.0 --apply   # 只补一个
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, ORG, repo_dir  # noqa: E402
from gen_versions import _remote_tags  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))

OEKIT_DEP_RE = re.compile(r"^\s*github\.com/oekit/\S+\s+v\S+", re.M)
VER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def gh(*args: str, check: bool = True) -> tuple[int, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} 失败：{(p.stderr or p.stdout).strip()[:300]}")
    return p.returncode, p.stdout


def semver_key(tag: str):
    ver = tag.rsplit("/", 1)[-1]
    m = VER_RE.match(ver)
    if not m:
        return (0, 0, 0, tag)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), tag)


def existing_releases(repo: str) -> set[str]:
    """远端已存在的 Release tag 名。匿名 API 回退：CI 里 GITHUB_TOKEN 只对本仓有效。"""
    code, out = gh("release", "list", "--repo", f"{ORG}/{repo}", "--limit", "300",
                   "--json", "tagName", "--jq", ".[].tagName", check=False)
    if code == 0:
        return set(out.split())
    # 回退：匿名 REST（公开仓可行；限速失败则调用方按不可核对跳过）
    code, out = gh("api", f"repos/{ORG}/{repo}/releases?per_page=100",
                   "--jq", ".[].tag_name", check=False)
    if code == 0:
        return set(out.split())
    raise PermissionError(f"{repo}: 无法读取 Release 列表（{(out or '').strip()[:120]}）")


def changelog_section(repo_dir_path, version: str, nested: str | None) -> str | None:
    """取 CHANGELOG 同版本小节正文。嵌套 tag 优先 <sub>/CHANGELOG.md 的 [vX.Y.Z]，
    回退根 CHANGELOG 的 [<sub>/vX.Y.Z]（bootstrap/nacos 形态）；文件缺失返回 None
    （调用方回退 CHANGELOG 链接）。"""
    if nested:
        sub_cl = os.path.join(repo_dir_path, nested, "CHANGELOG.md")
        if os.path.exists(sub_cl):
            sec = _extract(open(sub_cl, encoding="utf-8").read(), version)
            if sec is not None:
                return sec
        root_cl = os.path.join(repo_dir_path, "CHANGELOG.md")
        if not os.path.exists(root_cl):
            return None
        return _extract(open(root_cl, encoding="utf-8").read(), f"{nested}/{version}")
    root_cl = os.path.join(repo_dir_path, "CHANGELOG.md")
    if not os.path.exists(root_cl):
        return None
    return _extract(open(root_cl, encoding="utf-8").read(), version)


def _extract(text: str, head: str) -> str | None:
    pat = re.compile(r"(?ms)^## \[" + re.escape(head) + r"\][^\n]*\n(.*?)(?=^## \[|\Z)")
    m = pat.search(text)
    return m.group(1).strip() if m else None


def go_mod_at(repo_dir_path, tag: str, nested: str | None) -> str | None:
    rel = os.path.join(nested, "go.mod") if nested else "go.mod"
    r = subprocess.run(["git", "-C", repo_dir_path, "show", f"{tag}:{rel}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def build_notes(repo_dir_path, tag: str, repo: str) -> str:
    nested = tag.rsplit("/", 1)[0] if "/" in tag else None
    version = tag.rsplit("/", 1)[-1]
    parts = [f"Release of `{ORG}/{repo}` `{tag}`（annotated tag，由 release_train 门禁后打点）。"]
    sec = changelog_section(repo_dir_path, version, nested)
    if sec:
        parts.append("## 变更记录\n\n" + sec)
    else:
        parts.append(f"变更记录见 [CHANGELOG.md](https://github.com/{ORG}/{repo}/blob/{tag}/CHANGELOG.md)。")
    gm = go_mod_at(repo_dir_path, tag, nested)
    if gm:
        deps = sorted(set(OEKIT_DEP_RE.findall(gm)))
        if deps:
            parts.append(
                "## 矩阵内依赖地板（该 tag 的 go.mod，CI 验证过的组合）\n\n```go.mod\n"
                + "\n".join(deps) + "\n```")
    return "\n\n".join(parts) + "\n"


def plan_repo(repo: str, only_tag: str | None = None) -> tuple[list[str], str | None, list[str]]:
    """返回 (缺失 tag 列表[升序], 最新根 tag, 该仓全部 tag)。"""
    tags = _remote_tags(repo)          # 降序
    tags_asc = sorted(tags, key=semver_key)
    if only_tag:
        tags_asc = [t for t in tags_asc if t == only_tag]
    latest_root = next((t for t in tags if "/" not in t), None)
    have = existing_releases(repo)
    missing = [t for t in tags_asc if t not in have]
    return missing, latest_root, tags_asc


def apply_repo(repo: str, missing: list[str], latest_root: str | None) -> int:
    d = str(repo_dir(repo, BASE))
    created = 0
    for tag in missing:
        notes = build_notes(d, tag, repo)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(notes)
            path = f.name
        try:
            args = ["release", "create", tag, "--repo", f"{ORG}/{repo}",
                    "--title", tag, "--notes-file", path, "--verify-tag"]
            if tag != latest_root:
                args.append("--latest=false")
            gh(*args)
            created += 1
            print(f"  ✓ {repo} {tag}" + (" (latest)" if tag == latest_root else ""))
        finally:
            os.unlink(path)
    return created


def missing_releases(repos: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """audit 门禁用：返回 ({repo: 缺失 tag}, 因权限/限速无法核对的仓)。"""
    out: dict[str, list[str]] = {}
    skipped: list[str] = []
    for repo in repos:
        try:
            missing, _, _ = plan_repo(repo)
            if missing:
                out[repo] = missing
        except (RuntimeError, PermissionError) as e:
            skipped.append(f"{repo}: {e}")
    return out, skipped


def main() -> int:
    args = sys.argv[1:]
    do_apply = "--apply" in args
    do_check = "--check" in args
    only_repo = args[args.index("--repo") + 1] if "--repo" in args else None
    only_tag = args[args.index("--tag") + 1] if "--tag" in args else None
    repos = [only_repo] if only_repo else list(MODULES)

    mode = "APPLY" if do_apply else ("CHECK" if do_check else "DRY-RUN")
    print(f"GitHub Release 对账（{mode}，{len(repos)} 仓）\n")
    total_missing = 0
    skipped: list[str] = []
    for repo in repos:
        try:
            missing, latest_root, _ = plan_repo(repo, only_tag)
        except (RuntimeError, PermissionError) as e:
            skipped.append(f"{repo}: {e}")
            continue
        if not missing:
            continue
        total_missing += len(missing)
        root_extra = f"，latest={latest_root}" if latest_root else "（无根模块 tag，全部 --latest=false）"
        print(f"→ {repo}: 缺 {len(missing)} 个 Release（{missing[0]} … {missing[-1]}{root_extra}）")
        if do_apply:
            apply_repo(repo, missing, latest_root)

    if skipped:
        print("\n无法核对（权限/限速，门禁模式下跳过不红）：")
        for s in skipped:
            print(f"  ⚠ {s}")
    if do_check:
        if total_missing:
            print(f"\n❌ {total_missing} 个 tag 缺 Release（跑 gen_releases.py --apply）")
            return 1
        print("✓ 全部 tag 均有 Release")
        return 0
    if not do_apply:
        print(f"\n共 {total_missing} 个缺失。加 --apply 创建。")
    elif total_missing == 0:
        print("\n✓ 无缺失。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
