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


def issue_state(repo: str, num: str) -> str:
    """读 issue 当前状态（closed / open）；读不到返回空串。"""
    r = subprocess.run(["gh", "issue", "view", num, "--repo", f"{ORG}/{repo}",
                        "--json", "state", "--jq", ".state"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def receipt_posted(repo: str, num: str, tag: str) -> bool:
    """回执是否**真的**落在 issue 上。

    判据取「issue 上存在提到该 tag 的评论」，不取「命令返回成功」——
    `gh issue close --comment` 对**已关闭**的 issue 会打印「already closed」
    并以 **0 退出**，评论根本不会发（实测 2026-10-21：PR 正文带 Closes #N
    会在合入时自动关单，随后回执静默丢失，而脚本仍打印「已回执并关闭」）。
    """
    r = subprocess.run(["gh", "issue", "view", num, "--repo", f"{ORG}/{repo}",
                        "--json", "comments", "--jq", "[.comments[].body] | join(\"\\n\")"],
                       capture_output=True, text=True)
    return r.returncode == 0 and tag in r.stdout


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

    # issue 往往已被 PR 正文的 "Closes #N" 在合入时**自动关闭**。此时
    # `gh issue close --comment` 会打印「already closed」并以 0 退出，**评论不会发**。
    # ⇒ 已关闭走 `gh issue comment` 单独补回执；未关闭才走 close+comment。
    state = issue_state(repo, num)
    if state == "closed":
        r = subprocess.run(["gh", "issue", "comment", num, "--repo", f"{ORG}/{repo}",
                            "--body", body], capture_output=True, text=True)
        action = "回执"
    else:
        r = subprocess.run(["gh", "issue", "close", num, "--repo", f"{ORG}/{repo}",
                            "--comment", body], capture_output=True, text=True)
        action = "回执并关闭"
    if r.returncode != 0:
        raise SystemExit(f"关单失败：{(r.stderr or r.stdout).strip()[:300]}")

    # 复核：**回执真的落地**才算数（命令返回成功 ≠ 回执已发）。
    if not receipt_posted(repo, num, tag):
        raise SystemExit(
            f"回执未落地：oekit/{repo}#{num} 上找不到 {tag} 的回执评论——"
            "命令返回成功不等于回执已发（gh issue close 对已关闭 issue 返回 0 却不贴评论）")
    if state != "closed":
        after = issue_state(repo, num)
        if after != "closed":
            raise SystemExit(f"关单未生效：oekit/{repo}#{num} 状态仍为 {after!r}")
    print(f"✓ oekit/{repo}#{num} 已{action}（{tag}）")


if __name__ == "__main__":
    main()
