#!/usr/bin/env python3
"""issue 回执关单一条命令：发回执（版本 + CHANGELOG 小节 + Release 链接）并关闭。

用法：
  python3 scripts/close_issue.py <repo>#<N> --tag vX.Y.Z [--note 补充说明] [--dry-run] [--force]

守卫：--tag 必须已存在于远端（版本号必须来自已发布内容——CHANGELOG 占位头事故的
教训：不确定就写 Unreleased，回执里发不存在的版本比不回执更糟）。
嵌套 tag 写全名（如 hlog/v0.1.2），小节自动在 <sub>/CHANGELOG.md 或根 CHANGELOG 查。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import ORG, repo_dir  # noqa: E402


def tag_on_remote(repo_dir_path: Path, tag: str) -> bool:
    r = subprocess.run(["git", "ls-remote", "origin", f"refs/tags/{tag}"],
                       cwd=repo_dir_path, capture_output=True, text=True)
    return bool(r.stdout.strip())


def main() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    force = "--force" in args
    note_idx = args.index("--note") if "--note" in args else None
    note = args[note_idx + 1] if note_idx is not None else ""
    tag_idx = args.index("--tag")
    tag = args[tag_idx + 1]
    ref = next((a for a in args if "#" in a and not a.startswith("--")), None)
    if not ref or not tag or not re.fullmatch(r"v\d+\.\d+\.\d+", tag.rsplit("/", 1)[-1]):
        print(__doc__)
        raise SystemExit(2)
    m = re.fullmatch(r"([a-z0-9.-]+)#(\d+)", ref)
    if not m:
        raise SystemExit(f"issue 引用格式应为 <repo>#<N>：{ref!r}")
    repo, num = m.group(1), m.group(2)

    d = repo_dir(repo)
    if not tag_on_remote(d, tag) and not force:
        raise SystemExit(f"❌ tag {tag} 不在远端（版本号必须来自已发布内容）——确认已 release_train，"
                         f"或核对 tag 名；确实要引用未发版本需 --force")
    nested = tag.rsplit("/", 1)[0] if "/" in tag else None
    version = tag.rsplit("/", 1)[-1]
    from gen_releases import changelog_section
    sec = changelog_section(str(d), version, nested)

    parts = [f"已在 `{tag}` 修复并发布 ✅",
             "## 变更内容\n\n" + (sec or f"见 [CHANGELOG.md](https://github.com/{ORG}/{repo}/blob/main/CHANGELOG.md)。"),
             f"Release: https://github.com/{ORG}/{repo}/releases/tag/{tag}"]
    if note:
        parts.append(note)
    body = "\n\n".join(parts) + "\n"

    if dry:
        print(f"--- 将发布评论并关闭 oekit/{repo}#{num} ---\n{body}")
        return
    r = subprocess.run(["gh", "issue", "close", num, "--repo", f"{ORG}/{repo}",
                        "--comment", body], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"关单失败：{(r.stderr or r.stdout).strip()[:300]}")
    print(f"✓ oekit/{repo}#{num} 已回执并关闭（{tag}）")


if __name__ == "__main__":
    main()
