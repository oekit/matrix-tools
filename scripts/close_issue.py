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
from matrix import ORG, RemoteReadError, remote_tag_exists, repo_dir  # noqa: E402
import ghsafe  # noqa: E402


def tag_on_remote(repo: str, tag: str) -> bool:
    """远端是否**真的**已有该 tag —— 读不到 ⇒ 抛 RemoteReadError（#85）。

    此前是 `bool(stdout.strip())`：ls-remote 失败（SSH 掉线 / 网络抖动）时
    stdout 为空 ⇒ 返回 False ⇒ 报「tag 不在远端」——**把「读不到」说成
    「没有」**。这条错误信息会把人推向 `--force`（脚本文案里就写着
    「确实要引用未发版本需 --force」），而真实原因可能只是网络抖了一下。

    现在统一走 matrix.remote_tag_exists（canonical HTTPS → origin 兜底 →
    失败抛错）：「读到空」与「读不到」严格分开。
    """
    return remote_tag_exists(repo, tag)


def gh_deliver(args: list[str]) -> subprocess.CompletedProcess:
    """投递回执（发评论 / 关单）。

    **走 ghsafe.run_gh ⇒ 瞬时错误（限速/5xx/超时/EOF）自动重试**
    （2026-10-30 改：旧实现直接 subprocess.run，一次 EOF 就整条回执失败；
    而 GitHub API 的 EOF 是**实测会发生的**瞬时故障）。

    **重试是安全的**：若首次其实已成功、只是响应丢了，重试不会造成重复回执——
    末尾的 `receipt_posted` 复核会认出「回执已在」；而 `gh issue close --comment`
    对**已关闭**的 issue 本就静默不贴评论（那是既有行为，靠复核兜住）。

    返回一个带 returncode/stdout/stderr 的对象，形状与 subprocess.run 一致
    （调用方按 `r.returncode != 0` 判失败）。
    """
    try:
        out, err = ghsafe.run_gh(args)
    except ghsafe.GhCommandError as e:
        return subprocess.CompletedProcess(args=args, returncode=e.returncode,
                                          stdout=e.stdout, stderr=e.stderr)
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr=err)


def issue_state(repo: str, num: str) -> str:
    """读 issue 当前状态（closed / open）。

    **归一化成小写**：真实 `gh issue view --json state --jq .state` 返回的是
    **大写**（CLOSED / OPEN）。大小写敏感的比较会让「已关闭 ⇒ 改走
    gh issue comment」这条分支**永远走不到**——于是脚本对自动关闭的 issue
    一律调 `gh issue close --comment`，而真实 gh 对已关闭 issue 静默不贴评论
    （打印 already closed、退出 0）⇒ 回执再次丢失（2026-10-21 实测：
    上一轮的修复只补了「回执没落地要判红」，没补「大小写」，
    于是判据抓到了丢失、投递路径却仍是坏的）。

    **读不到 ⇒ 明确报错**（2026-10-30 改）：旧实现失败时返回空串，
    而空串**同时**表示「未关闭」与「读不到」⇒ 读不到时会把
    **已关闭**的 issue 错走 `gh issue close --comment` ⇒ GitHub 静默不贴
    评论 ⇒ 回执丢失（只能靠末尾的复核兜住，失败得晚且难查）。
    现在走 ghsafe.run_gh：**瞬时错误（限速/5xx/超时/EOF）内部重试**，
    重试后仍读不到 ⇒ 抛错，绝不猜。
    """
    try:
        out, _err = ghsafe.run_gh(["issue", "view", num, "--repo", f"{ORG}/{repo}",
                                    "--json", "state", "--jq", ".state"])
    except ghsafe.GhCommandError as e:
        raise SystemExit(
            f"读 issue 状态失败（oekit/{repo}#{num}）：{(e.stderr or e.stdout).strip()[:300]}"
            "——读不到 ≠ 未关闭，绝不据此选投递路径")
    return out.strip().lower()


def receipt_posted(repo: str, num: str, needles: list[str]) -> bool:
    """回执是否**真的**落在 issue 上。

    判据取「issue 上存在一条**同时**提到全部 needle 的评论」，不取「命令返回成功」——
    `gh issue close --comment` 对**已关闭**的 issue 会打印「already closed」
    并以 **0 退出**，评论根本不会发（实测 2026-10-21：PR 正文带 Closes #N
    会在合入时自动关单，随后回执静默丢失，而脚本仍打印「已回执并关闭」）。

    needle 取「本次回执独有的字符串」：发版模式取 tag，无发布模式取
    「无 SemVer 发布」标记（+ PR 号）。**不能**取「issue 上有任何评论」——
    那种判据在「评论发到了别的 issue」或「旧回执还在」时都会假绿。

    **读不到 ⇒ 明确报错**（2026-10-30 改）：旧实现失败时返回 False（=「没发过」）
    ⇒ 重投 ⇒ 评论区堆一串重复回执。而「读不到」的真实含义是**不知道发没发过**
    ⇒ 不敢重投 ⇒ 报错让人重跑（幂等由下次成功读到时保证）。
    """
    try:
        out, _err = ghsafe.run_gh(["issue", "view", num, "--repo", f"{ORG}/{repo}",
                                    "--json", "comments", "--jq",
                                    "[.comments[].body] | join(\"\\n\")"])
    except ghsafe.GhCommandError as e:
        raise SystemExit(
            f"读 issue 评论失败（oekit/{repo}#{num}）：{(e.stderr or e.stdout).strip()[:300]}"
            "——读不到 ≠ 没发过，不能据此重投（会堆重复回执）")
    body = out
    return all(n in body for n in needles)


def pr_meta(repo: str, pr: str) -> dict:
    """取 PR 的合入证据：merge commit / 合入时间 / CI 结论。

    取不到就返回空 dict（回执退化为只给 PR 链接）——**不得**因为取不到证据
    就让整条回执失败：回执的**投递**是硬要求，证据的**完整**是软要求。

    **瞬时错误（限速/5xx/超时/EOF）由 ghsafe.run_gh 内部重试**；
    重试后仍读不到 ⇒ 退化为「无证据」（软失败，符合本函数的既定语义）。
    """
    out: dict = {"number": pr, "url": f"https://github.com/{ORG}/{repo}/pull/{pr}"}
    try:
        raw, _err = ghsafe.run_gh(["pr", "view", pr, "--repo", f"{ORG}/{repo}",
                                    "--json", "number,title,url,mergedAt,mergeCommit,state"])
        try:
            out.update(json.loads(raw))
        except json.JSONDecodeError:
            pass
    except ghsafe.GhCommandError:
        pass  # 证据缺失 ⇒ 软失败（回执照发）
    try:
        raw, _err = ghsafe.run_gh(["pr", "checks", pr, "--repo", f"{ORG}/{repo}",
                                    "--json", "name,state,bucket"])
        try:
            out["checks"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
    except ghsafe.GhCommandError:
        pass  # 无 check / 读不到 ⇒ 软失败
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

    if not no_release and not force:
        # --force 是「刻意的越权」（确实要引用未发版本）⇒ 显式跳过核对，不发读请求；
        # 非 force 时读远端：读不到 ⇒ 报错（**不是**「不存在」），真·不存在 ⇒ 拦。
        try:
            on_remote = tag_on_remote(repo, tag)
        except RemoteReadError as e:
            raise SystemExit(f"❌ 读不到远端 tag（**不是**「不存在」）——修好网络后重试；"
                             f"--force 只用于「确实要引用未发版本」，不要拿它盖住读失败：{e}")
        if not on_remote:
            raise SystemExit(f"❌ tag {tag} 不在远端（版本号必须来自已发布内容）——确认已 release_train，"
                             f"或核对 tag 名；确实要引用未发版本需 --force")
    if pr and not re.fullmatch(r"\d+", pr):
        raise SystemExit(f"--pr 应为 PR 号，实得 {pr!r}")
    meta = pr_meta(repo, pr) if pr else {}
    is_pr = target_is_pr(repo, num)
    # **--pr 指向的 PR 是否已合入，与「目标是不是 PR」无关**（#119）：
    #
    # 旧实现只在「目标是 PR」时才查 meta ⇒ 目标是 **issue** 时**无条件**写
    # 「已合入修复 ✅」并把 issue **关掉** ⇒ 回执是**假声明**（PR 还没合入），
    # 且问题在修复落地前就从看板上消失。
    #
    # 2026-10-28 实测：`close_issue.py gormx#36 --no-release --pr 39` —— PR #39
    # 当时 OPEN（CI 已绿、未合入），回执写成「已合入修复 ✅（无 SemVer 发布）」，
    # issue 被 closed。
    linked_state = str(meta.get("state") or "")
    linked_merged = linked_state == "MERGED"
    pr_merged = linked_merged
    # 修复尚未落地（--pr 指向未合入的 PR）⇒ **只发回执、绝不关单**：
    # 由 PR 正文的 `Closes #N` 在合入时关单。
    #
    # 「读不到 PR 状态」按**未合入**处理（fail-closed）：宁可多发一条回执、
    # 少关一个单，也不要在修复未落地时把问题从看板抹掉。
    pending = bool(pr) and not linked_merged and no_release

    if no_release:
        head = ("已提交修复（PR 尚未合入）" if pending else "已合入修复 ✅")
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
    # ⇒ **--pr 指向未合入的 PR** 时同样**只发回执、绝不 close**（#119）：
    #    修复尚未落地 ⇒ 关单就是「问题从看板上消失了，而修复还没进来」。
    if is_pr or pending:
        r = gh_deliver(["issue", "comment", num, "--repo", f"{ORG}/{repo}",
                        "--body", body])
        action = "回执（PR 不关闭）" if is_pr else "回执（修复未落地，不关单）"
    else:
        state = issue_state(repo, num)
        if state == "closed":
            r = gh_deliver(["issue", "comment", num, "--repo", f"{ORG}/{repo}",
                            "--body", body])
            action = "回执"
        else:
            r = gh_deliver(["issue", "close", num, "--repo", f"{ORG}/{repo}",
                            "--comment", body])
            action = "回执并关闭"
    if r.returncode != 0:
        raise SystemExit(f"关单失败：{(r.stderr or r.stdout).strip()[:300]}")

    # 复核：**回执真的落地**才算数（命令返回成功 ≠ 回执已发）。
    if not receipt_posted(repo, num, needles):
        raise SystemExit(
            f"回执未落地：oekit/{repo}#{num} 上找不到 {needles} 的回执评论——"
            "命令返回成功不等于回执已发（gh issue close 对已关闭 issue 返回 0 却不贴评论）")
    if not is_pr and not pending:
        after = issue_state(repo, num)
        if after != "closed":
            raise SystemExit(f"关单未生效：oekit/{repo}#{num} 状态仍为 {after!r}")
    label = tag if tag else "无发布"
    if pending:
        extra = ("；⚠️ PR 尚未合入 ⇒ 回执未声明「已合入」，且**未关单**"
                 "（由 PR 正文的 Closes #N 在合入时关）")
    elif is_pr and not pr_merged:
        extra = "；⚠️ PR 尚未合入，回执未声明「已合入」"
    else:
        extra = ""
    print(f"✓ oekit/{repo}#{num} 已{action}（{label}）{extra}")


if __name__ == "__main__":
    main()
