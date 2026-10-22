#!/usr/bin/env python3
"""issue 回执关单一条命令：发回执（版本 + CHANGELOG 小节 + Release 链接）并关闭。

用法：
  python3 scripts/close_issue.py <repo>#<N> --tag vX.Y.Z [--note 补充说明] [--dry-run] [--force]
  python3 scripts/close_issue.py <repo>#<N> --no-release --pr <N> [--note 补充说明] [--dry-run]

守卫：--tag 必须已存在于远端（版本号必须来自已发布内容——CHANGELOG 占位头事故的
教训：不确定就写 Unreleased，回执里发不存在的版本比不回执更糟）。
嵌套 tag 写全名（如 hlog/v0.1.2），小节自动在 <sub>/CHANGELOG.md 或根 CHANGELOG 查。

--no-release：本次改动**不发 SemVer**（CI 基础设施 / 文档 / 治理配置），因此没有
tag、CHANGELOG 小节、Release 链接可引。

为什么需要这个模式（2026-10-22 实测）：上一轮一次性处理 25 条 CI 基础设施 issue，
全部用「PR 正文带 `Closes #N`」在合入时自动关单 ⇒ 自动关闭**不产生任何评论**
⇒ 25 条 issue 零回执、看板却已 Done ⇒ 回执静默丢失。
根因不在 gh，而在**本脚本没有无发布路径**：回执内容围绕「版本 + CHANGELOG 小节 +
Release 链接」组织，硬要求 --tag ⇒ 不发版本的改动**没有任何可用的回执通道**，
巡检链走到最后一步只能跳过 ⇒ 结构性缺口。
⇒ 补 --no-release 模式：回执写「已合入修复 ✅（无 SemVer 发布）」+ 变更内容 +
合入 PR / merge commit / CI 结论；判据仍然是**评论真的落地**（不信退出码）。

**目标是 PR 时只发回执、绝不 close**（2026-10-22 实测）：GitHub 的 `issues`
端点与 `gh issue view` 对 **PR 编号**都能解析，PR 的 state 是 `MERGED`/`OPEN`
（不是 issue 的 `CLOSED`/`OPEN`）。上一轮开的 25 个「issue」实际是 25 个 PR，
走旧逻辑会 ① 对 PR 调 `gh issue close`（对**未合入**的 PR 就是「不合并直接关掉」）
② 关单后校验 `state == "closed"` 永远不成立 ⇒ 脚本必然误判红。
⇒ 本脚本先判 PR：PR 只发回执（合入才是 PR 的关闭），且回执在 PR 未合入时
写「已提交修复（PR 尚未合入）」而**不**写「已合入修复」（不做假声明）。

**幂等**：回执已在（上一轮已发 / 手工补发过）⇒ 跳过投递直接收工，
避免重跑在评论区堆一串重复回执。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import ORG, repo_dir  # noqa: E402
import ghsafe  # noqa: E402


def tag_on_remote(repo_dir_path: Path, tag: str) -> bool:
    r = subprocess.run(["git", "ls-remote", "origin", f"refs/tags/{tag}"],
                       cwd=repo_dir_path, capture_output=True, text=True)
    return bool(r.stdout.strip())


def issue_state(repo: str, num: str) -> str:
    """读 issue 当前状态（closed / open）；读不到返回空串。

    **归一化成小写**：真实 `gh issue view --json state --jq .state` 返回的是
    **大写**（CLOSED / OPEN）。大小写敏感的比较会让「已关闭 ⇒ 改走
    gh issue comment」这条分支**永远走不到**——于是脚本对自动关闭的 issue
    一律调 `gh issue close --comment`，而真实 gh 对已关闭 issue 静默不贴评论
    （打印 already closed、退出 0）⇒ 回执再次丢失（2026-10-21 实测：
    上一轮的修复只补了「回执没落地要判红」，没补「大小写」，
    于是判据抓到了丢失、投递路径却仍是坏的）。
    """
    r = subprocess.run(["gh", "issue", "view", num, "--repo", f"{ORG}/{repo}",
                        "--json", "state", "--jq", ".state"],
                       capture_output=True, text=True)
    return r.stdout.strip().lower() if r.returncode == 0 else ""


def receipt_posted(repo: str, num: str, needles: list[str]) -> bool:
    """回执是否**真的**落在 issue 上。

    判据取「issue 上存在一条**同时**提到全部 needle 的评论」，不取「命令返回成功」——
    `gh issue close --comment` 对**已关闭**的 issue 会打印「already closed」
    并以 **0 退出**，评论根本不会发（实测 2026-10-21：PR 正文带 Closes #N
    会在合入时自动关单，随后回执静默丢失，而脚本仍打印「已回执并关闭」）。

    needle 取「本次回执独有的字符串」：发版模式取 tag，无发布模式取
    「无 SemVer 发布」标记（+ PR 号）。**不能**取「issue 上有任何评论」——
    那种判据在「评论发到了别的 issue」或「旧回执还在」时都会假绿。
    """
    r = subprocess.run(["gh", "issue", "view", num, "--repo", f"{ORG}/{repo}",
                        "--json", "comments", "--jq", "[.comments[].body] | join(\"\\n\")"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    body = r.stdout
    return all(n in body for n in needles)


def pr_meta(repo: str, pr: str) -> dict:
    """取 PR 的合入证据：merge commit / 合入时间 / CI 结论。

    取不到就返回空 dict（回执退化为只给 PR 链接）——**不得**因为取不到证据
    就让整条回执失败：回执的**投递**是硬要求，证据的**完整**是软要求。
    """
    out: dict = {"number": pr, "url": f"https://github.com/{ORG}/{repo}/pull/{pr}"}
    r = subprocess.run(["gh", "pr", "view", pr, "--repo", f"{ORG}/{repo}",
                        "--json", "number,title,url,mergedAt,mergeCommit,state"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        try:
            out.update(json.loads(r.stdout))
        except json.JSONDecodeError:
            pass
    c = subprocess.run(["gh", "pr", "checks", pr, "--repo", f"{ORG}/{repo}",
                        "--json", "name,state,bucket"],
                       capture_output=True, text=True)
    if c.returncode == 0:
        try:
            out["checks"] = json.loads(c.stdout)
        except json.JSONDecodeError:
            pass
    return out


def target_is_pr(repo: str, num: str) -> bool:
    """目标编号是 **PR** 还是 **issue**。

    为什么要分：GitHub 的 `issues` 端点与 `gh issue view` 对 PR 编号**都能解析**，
    但 PR 的 state 是 `MERGED`/`OPEN`（不是 issue 的 `CLOSED`/`OPEN`）。
    2026-10-22 实测：上一轮开的 25 个 PR 被当成「issue」走关单流程 ⇒
    ① 脚本走 `gh issue close` 想把 PR 关掉 ⇒ 对**未合入**的 PR 就是「不合并直接关闭」；
    ② 关单后校验 `state == "closed"` 永远不成立 ⇒ 脚本必然判红。
    ⇒ 认 PR 就**只发回执、绝不 close**（合入才是 PR 的「关闭」）。

    **为什么不能把「读不到」当成「不是 PR」**（2026-10-22 实测）：
    GitHub API 会**间歇性返回空集合**（`GET /repos/{o}/{r}/issues/{n}` 偶发返回
    `{}` / 空正文，curl 直连同样可复现）⇒ 旧实现 JSON 解析失败就 `return False`
    ⇒ 判成「这是 issue」⇒ 对未合入的 PR 调 `gh issue close`
    ⇒ v0.1.8 刚修掉的坑**从这条缝里复活**。
    ⇒ 现在的口径：读不到就**报错中止**（宁可脚本失败，也不能把 PR 当 issue 关掉）；
    「是不是 PR」仍以 REST 返回体里的 `pull_request` 字段为准（PR 的该字段必在）。
    """
    try:
        data = ghsafe.gh_json(["api", f"repos/{ORG}/{repo}/issues/{num}"])
    except ghsafe.GhCommandError as e:
        if e.is_not_found:
            return False          # 编号不存在 ⇒ 既不是 issue 也不是 PR
        raise SystemExit(f"{repo}#{num}: 读取 issue 元数据失败：{e}")
    except ghsafe.GhReadError as e:
        raise SystemExit(
            f"{repo}#{num}: 读取 issue 元数据失败（{e}）——"
            "远端偶发返回空内容时不能把它当成「不是 PR」，"
            "否则会对未合入的 PR 执行 gh issue close。脚本中止。")
    return bool(isinstance(data, dict) and data.get("pull_request"))


def pr_lines(meta: dict) -> list[str]:
    """把 PR 元数据渲染成回执正文行。"""
    if not meta:
        return []
    lines = [f"**合入**：PR #{meta.get('number')}（{meta.get('url')}）"]
    sha = (meta.get("mergeCommit") or {}).get("oid")
    when = meta.get("mergedAt")
    if sha:
        lines[0] += f"，merge commit `{sha[:8]}`"
    if when:
        lines[0] += f"，{when}"
    checks = meta.get("checks") or []
    if checks:
        icon = {"pass": "✅", "fail": "❌", "pending": "⏳", "skipping": "⏭️", "cancel": "🚫"}
        rendered = " ／ ".join(f"`{c.get('name')}` {icon.get(c.get('bucket'), c.get('bucket'))}"
                               for c in checks)
        lines.append(f"**CI**：{rendered}")
    return lines


def main() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    force = "--force" in args
    no_release = "--no-release" in args
    note_idx = args.index("--note") if "--note" in args else None
    note = args[note_idx + 1] if note_idx is not None and note_idx + 1 < len(args) else ""
    pr_idx = args.index("--pr") if "--pr" in args else None
    pr = args[pr_idx + 1] if pr_idx is not None and pr_idx + 1 < len(args) else ""
    has_tag = "--tag" in args
    tag = args[args.index("--tag") + 1] if has_tag and args.index("--tag") + 1 < len(args) else ""

    if no_release == has_tag:
        print(__doc__)
        raise SystemExit("❌ --tag 与 --no-release 恰好要二选一（发版回执 / 无发布回执）")
    if has_tag and not re.fullmatch(r"v\d+\.\d+\.\d+", tag.rsplit("/", 1)[-1]):
        print(__doc__)
        raise SystemExit(2)
    ref = next((a for a in args if "#" in a and not a.startswith("--")), None)
    if not ref:
        print(__doc__)
        raise SystemExit(2)
    m = re.fullmatch(r"([a-z0-9.-]+)#(\d+)", ref)
    if not m:
        raise SystemExit(f"issue 引用格式应为 <repo>#<N>：{ref!r}")
    repo, num = m.group(1), m.group(2)

    if not no_release:
        d = repo_dir(repo)
        if not tag_on_remote(d, tag) and not force:
            raise SystemExit(f"❌ tag {tag} 不在远端（版本号必须来自已发布内容）——确认已 release_train，"
                             f"或核对 tag 名；确实要引用未发版本需 --force")
    if pr and not re.fullmatch(r"\d+", pr):
        raise SystemExit(f"--pr 应为 PR 号，实得 {pr!r}")
    meta = pr_meta(repo, pr) if pr else {}
    is_pr = target_is_pr(repo, num)
    pr_merged = (meta.get("state") == "MERGED") if (is_pr and meta) else False

    if no_release:
        head = ("已合入修复 ✅" if (not is_pr or pr_merged)
                else "已提交修复（PR 尚未合入）")
        parts = [f"{head}（**无 SemVer 发布**：本次改动不发版本）",
                 "## 变更内容\n\n" + (note or f"见 PR #{pr}。")]
        parts += pr_lines(meta)
        needles = ["无 SemVer 发布"] + ([f"PR #{pr}"] if pr else [])
    else:
        nested = tag.rsplit("/", 1)[0] if "/" in tag else None
        version = tag.rsplit("/", 1)[-1]
        from gen_releases import changelog_section
        sec = changelog_section(str(repo_dir(repo)), version, nested)

        parts = [f"已在 `{tag}` 修复并发布 ✅",
                 "## 变更内容\n\n" + (sec or f"见 [CHANGELOG.md](https://github.com/{ORG}/{repo}/blob/main/CHANGELOG.md)。"),
                 f"Release: https://github.com/{ORG}/{repo}/releases/tag/{tag}"]
        parts += pr_lines(meta)
        if note:
            parts.append(note)
        needles = [tag]

    body = "\n\n".join(parts) + "\n"

    if dry:
        print(f"--- 将发布评论并关闭 oekit/{repo}#{num} ---\n{body}")
        return

    # 幂等：回执已在（上一轮已发过 / 手工补发过）⇒ 不重复发，直接收工。
    # 没有这一步，重跑会把同一条回执发 N 遍（issue 评论区会堆一串重复回执）。
    if receipt_posted(repo, num, needles):
        print(f"✓ oekit/{repo}#{num} 回执已在（幂等跳过，未重复发布）")
        return

    # issue 往往已被 PR 正文的 "Closes #N" 在合入时**自动关闭**。此时
    # `gh issue close --comment` 会打印「already closed」并以 0 退出，**评论不会发**。
    # ⇒ 已关闭走 `gh issue comment` 单独补回执；未关闭才走 close+comment。
    # ⇒ 目标是 **PR** 时**只发回执、绝不 close**：合入才是 PR 的「关闭」，
    #    对未合入的 PR 调 close 就是「不合并直接关掉」。
    if is_pr:
        r = subprocess.run(["gh", "issue", "comment", num, "--repo", f"{ORG}/{repo}",
                            "--body", body], capture_output=True, text=True)
        action = "回执（PR 不关闭）"
    else:
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
    if not receipt_posted(repo, num, needles):
        raise SystemExit(
            f"回执未落地：oekit/{repo}#{num} 上找不到 {needles} 的回执评论——"
            "命令返回成功不等于回执已发（gh issue close 对已关闭 issue 返回 0 却不贴评论）")
    if not is_pr:
        after = issue_state(repo, num)
        if after != "closed":
            raise SystemExit(f"关单未生效：oekit/{repo}#{num} 状态仍为 {after!r}")
    label = tag if tag else "无发布"
    extra = "" if (not is_pr or pr_merged) else "；⚠️ PR 尚未合入，回执未声明「已合入」"
    print(f"✓ oekit/{repo}#{num} 已{action}（{label}）{extra}")


if __name__ == "__main__":
    main()
