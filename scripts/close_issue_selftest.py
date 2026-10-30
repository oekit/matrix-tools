#!/usr/bin/env python3
"""close_issue.py 的自测：证明「回执真的落地」这条判据不会假绿。

**为什么要有这个自测**：2026-10-21 实测——PR 正文带 `Closes #N` 会在合入时
自动关闭 issue；随后 `gh issue close --comment` 对**已关闭**的 issue 打印
「already closed」并**以 0 退出**，评论根本没发，而旧脚本只看退出码，
于是每轮都打印「✓ 已回执并关闭」——**回执一条都没落**。

自测用一个假 `gh` 复刻真实 gh 的这个行为：
  - `gh issue view --json state`      → **CLOSED**（真实 gh 的取值是**大写**）
  - `gh issue view --json comments`   → 假 gh 记录里的评论
  - `gh issue close --comment <body>` → **什么都不做**（复刻真实 gh 的静默失败）
  - `gh issue comment <body>`         → 真的记一条评论

判据：跑 close_issue.py 后，**记录里必须真的有评论**且退出码为 0。
反证：把「已关闭 → 改走 gh issue comment」这段回退成旧写法（只调 close），
本自测必然失败（记录为空 ⇒ 脚本判「回执未落地」⇒ 非 0 退出）。

**假 gh 的 state 取值必须用真实 gh 的形态（2026-10-21 实测踩坑）**：真实
`gh issue view --json state --jq .state` 返回**大写** `CLOSED` / `OPEN`。
自测最初把 fixture 写成小写 `closed`，于是脚本里的大小写敏感比较在自测里
「通过」、在真实调用里**永远走不到已关闭分支** ⇒ 回执再次静默丢失。
⇒ fixture 用真实取值，且另加一条小写容错用例。

**2026-10-22 新增三组用例**（对应本轮实测到的三个真实缺陷）：
  1. `--no-release` 模式：不发版本的改动**原先没有任何回执通道** ⇒ 回执静默丢失；
  2. **目标是 PR 时绝不能 close**：`gh issue close` 对未合入的 PR 就是
     「不合并直接关掉」；且 PR 的 state 是 `MERGED`/`OPEN`，关单后校验
     `state == "closed"` 永远不成立 ⇒ 脚本必然误判红；
  3. **幂等**：回执已在时不重复发（否则重跑会堆一串重复回执）。

用法：python3 scripts/close_issue_selftest.py
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE

# 假 gh 的行为开关（用于反证：把评论记录功能掐掉 ⇒ 脚本必须失败而不是假绿）
FAKE_GH = r'''#!/bin/sh
# 假 gh：复刻真实 gh 在「issue 已关闭」时的静默失败。
REC="$FAKE_GH_RECORD"
CLOSE_REC="$FAKE_GH_CLOSE_REC"

# **瞬时故障注入**（2026-10-30）：前 $FAKE_GH_EOF_CALLS 次调用报 EOF，
# 之后正常——复刻真实 GitHub API 的间歇性 EOF（实测会发生的瞬时故障）。
# ghsafe 的 TRANSIENT_RE 含 "unexpected EOF" ⇒ 应自动重试 ⇒ 脚本不受影响。
if [ -n "$FAKE_GH_EOF_CALLS" ]; then
  TARGET="$FAKE_GH_EOF_TARGET"
  # ⚠️ 必须用 `grep -q -e "$TARGET"`：pattern 以 "-" 开头（--json state）时，
  # `grep -q "$TARGET"` 会把它当成**选项**而不是 pattern ⇒ 永远匹配不上
  # （实测：注入静默不生效，用例1d 假绿）。
  if [ -z "$TARGET" ] || echo "$*" | grep -q -e "$TARGET"; then
    N=$(cat "$FAKE_GH_EOF_CALLS_FILE" 2>/dev/null || echo 0)
    if [ "$N" -lt "$FAKE_GH_EOF_CALLS" ]; then
      echo $((N + 1)) > "$FAKE_GH_EOF_CALLS_FILE"
      echo "failed to get run: Get \"https://api.github.com/...\": EOF" >&2
      exit 1
    fi
  fi
fi

# PR 判定用（gh api repos/{o}/{r}/issues/{n} 的返回体）
if [ "$1" = "api" ]; then
  echo "$FAKE_GH_ISSUE_JSON"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  echo "$FAKE_GH_PR_JSON"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "checks" ]; then
  echo "$FAKE_GH_CHECKS_JSON"
  exit 0
fi
if [ "$1" = "issue" ] && [ "$2" = "view" ]; then
  if echo "$*" | grep -q "state"; then
    echo "$FAKE_GH_STATE"
    exit 0
  fi
  if echo "$*" | grep -q "comments"; then
    if [ -n "$FAKE_GH_COMMENTS_BODY" ]; then
      printf '%s\n' "$FAKE_GH_COMMENTS_BODY"
      exit 0
    fi
    [ -f "$REC" ] && cat "$REC"
    exit 0
  fi
fi
if [ "$1" = "issue" ] && [ "$2" = "close" ]; then
  # 真实 gh 对已关闭 issue：打印提示、退出 0、**不贴评论**
  # 本假 gh 额外**记录这次 close 调用**——用来断言「PR 目标绝不被 close」。
  printf '%s\n' "CLOSE_CALLED $*" >> "$CLOSE_REC"
  echo "! Issue is already closed" >&2
  exit 0
fi
if [ "$1" = "issue" ] && [ "$2" = "comment" ]; then
  if [ "$FAKE_GH_COMMENT_WORKS" = "1" ]; then
    printf '%s\n' "$*" >> "$REC"
    echo "https://example.invalid/comment"
    exit 0
  fi
  exit 0   # 评论「发出去了」但没落地（模拟静默失败）
fi
exit 0
'''

ISSUE_JSON = '{"number":45,"state":"closed"}'
PR_ISSUE_JSON = ('{"number":45,"state":"closed",'
                 '"pull_request":{"url":"https://github.com/oekit/pkg/pull/45"}}')
PR_MERGED_JSON = ('{"number":70,"state":"MERGED","mergedAt":"2026-10-22T10:16:38Z",'
                  '"mergeCommit":{"oid":"18c00875bc1d8a8fa94f6af88d3ed263485fc977"},'
                  '"url":"https://github.com/oekit/pkg/pull/70","title":"ci(workflow): x"}')
PR_OPEN_JSON = ('{"number":70,"state":"OPEN","mergedAt":null,"mergeCommit":null,'
                '"url":"https://github.com/oekit/pkg/pull/70","title":"ci(workflow): x"}')
CHECKS_JSON = '[{"bucket":"pass","name":"link","state":"SUCCESS"}]'


def run_case(tmp: Path, state: str, comment_works: str,
             extra: list[str] | None = None,
             comments_body: str | None = None,
             kind: str = "issue",
             pr_state: str = "MERGED",
             eof_calls: int = 0,
             eof_target: str = "",
             case: str = "c") -> tuple[int, str, str, str]:
    """跑一遍 close_issue.py，返回 (退出码, 评论记录, close 记录, 组合输出)。

    `case` 必须**每个用例唯一**：临时文件名由它派生——重名会让上一个用例留在
    记录文件里的评论被下一个用例读成「回执已在」（实测踩坑：用例4 与用例10
    参数完全相同 ⇒ 共享记录文件 ⇒ 用例10 误判为「重复发布了评论」）。
    """
    suffix = (f"{case}-{state}-{comment_works}-{'nr' if extra else 'tg'}-{kind}"
              f"-{pr_state}-eof{eof_calls}-{eof_target.replace(' ', '_')}")
    bindir = tmp / f"bin-{suffix}"
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rec = tmp / f"rec-{suffix}.txt"
    close_rec = tmp / f"close-{suffix}.txt"

    # 自带一个最小 MATRIX_BASE：repo_dir 只要求仓目录下有 go.mod 或 README.md。
    # ⇒ 自测不依赖本地工作区布局，CI 里也能跑（不 clone 全矩阵、不联网）。
    base = tmp / f"base-{suffix}"
    (base / "pkg").mkdir(parents=True, exist_ok=True)
    (base / "pkg" / "README.md").write_text("# fake pkg\n", encoding="utf-8")
    (base / "pkg" / "CHANGELOG.md").write_text(
        "## [v0.19.0] — 2026-10-21\n\n- fake entry\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GH_RECORD"] = str(rec)
    env["FAKE_GH_CLOSE_REC"] = str(close_rec)
    env["FAKE_GH_STATE"] = state
    if eof_calls:
        env["FAKE_GH_EOF_CALLS"] = str(eof_calls)
        env["FAKE_GH_EOF_TARGET"] = eof_target
        env["FAKE_GH_EOF_CALLS_FILE"] = str(tmp / f"eof-{suffix}.txt")
    env["FAKE_GH_COMMENT_WORKS"] = comment_works
    env["FAKE_GH_COMMENTS_BODY"] = comments_body or ""
    env["FAKE_GH_ISSUE_JSON"] = PR_ISSUE_JSON if kind == "pr" else ISSUE_JSON
    env["FAKE_GH_PR_JSON"] = PR_MERGED_JSON if pr_state == "MERGED" else PR_OPEN_JSON
    env["FAKE_GH_CHECKS_JSON"] = CHECKS_JSON
    env["MATRIX_BASE"] = str(base)

    argv = [sys.executable, str(SCRIPTS / "close_issue.py"), "pkg#45"]
    argv += extra if extra else ["--tag", "v0.19.0", "--force"]
    p = subprocess.run(argv, capture_output=True, text=True, env=env)
    recorded = rec.read_text(encoding="utf-8") if rec.exists() else ""
    closed = close_rec.read_text(encoding="utf-8") if close_rec.exists() else ""
    return p.returncode, recorded, closed, (p.stdout or "") + (p.stderr or "")


def main() -> None:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 用例 1：issue 已被 PR 的 Closes 自动关闭 ⇒ 必须补发回执（评论真的落地）
        # state 用**真实 gh 的取值（大写 CLOSED）**——fixture 形态不对，
        # 自测就会在「假的取值」上通过、在真实调用上失效。
        rc, recorded, closed, out = run_case(tmp, "CLOSED", "1", case="c1")
        if rc != 0:
            failures.append(f"用例1 期望退出码 0，实得 {rc}：{out.strip()[:200]}")
        if "v0.19.0" not in recorded:
            failures.append("用例1 回执未落地：假 gh 没收到 gh issue comment 调用")

        # ── 用例 1c/d：瞬时 EOF（2026-10-30 新增）────────────────────
        # 背景：真实 GitHub API 会间歇性返回 EOF（实测会发生的瞬时故障）。
        # 旧实现**直接 subprocess.run 调 gh** ⇒ 一次 EOF 就把整条回执打死；
        # 且 `issue_state` 失败时返回空串 ⇒ 「已关闭」与「读不到」不可区分
        # ⇒ 会把已关闭的 issue 错走 `gh issue close --comment`
        # ⇒ GitHub 静默不贴评论 ⇒ **回执丢失**（只能靠末尾复核兜住）。
        #
        # 期望：ghsafe.run_gh 识别 EOF 为瞬时错误 ⇒ 自动重试 ⇒ 脚本照常成功。
        rc1c, recorded1c, _, out1c = run_case(tmp, "CLOSED", "1", eof_calls=2, case="c1c")
        if rc1c != 0:
            failures.append(f"用例1c 期望退出码 0（EOF 应被重试后成功），实得 {rc1c}：{out1c.strip()[:200]}")
        if "v0.19.0" not in recorded1c:
            failures.append("用例1c 回执未落地（EOF 重试后应正常投递）")

        # 用例 1d：EOF 次数**超过**重试上限 ⇒ 必须**明确报错**，
        # 而不是把「读不到」当成「未关闭」去走 close 路径（回执会静默丢失）。
        # 注入目标精确到 `--json state`（状态查询）：幂等检查走的是
        # `--json comments`，必须让它正常返回 ⇒ 才能真正走到 issue_state。
        rc1d, _, closed1d, out1d = run_case(tmp, "CLOSED", "1", eof_calls=99,
                                          eof_target="--json state", case="c1d")
        if rc1d == 0:
            failures.append("用例1d 期望非 0 退出（读不到状态必须明确失败），实得 0")
        if "失败" not in out1d:
            failures.append(f"用例1d 期望报「读取失败」（读不到 ≠ 未关闭），实得：{out1d.strip()[:200]}")
        if "CLOSE_CALLED" in closed1d:
            failures.append("用例1d 读不到状态时不得走 close 路径（会把已关闭 issue 的回执静默丢掉）")

        # 用例 1b：小写 state 也要认（大小写容错）
        rc1b, recorded1b, _, out1b = run_case(tmp, "closed", "1", case="c1b")
        if rc1b != 0:
            failures.append(f"用例1b 期望退出码 0，实得 {rc1b}：{out1b.strip()[:200]}")
        if "v0.19.0" not in recorded1b:
            failures.append("用例1b 回执未落地：小写 state 未被识别为已关闭")

        # 用例 2：issue 未关闭 ⇒ 走 close+comment；此时假 gh 的 close 不贴评论，
        # 但脚本必须**判定回执未落地并失败**（而不是假绿）
        rc2, recorded2, closed2, out2 = run_case(tmp, "OPEN", "1", case="c2")
        if rc2 == 0:
            failures.append(f"用例2 期望非 0 退出（回执未落地必须报错），实得 0")
        if "回执未落地" not in out2:
            failures.append(f"用例2 期望报「回执未落地」，实得：{out2.strip()[:200]}")
        if "CLOSE_CALLED" not in closed2:
            failures.append("用例2 未关闭的 issue 应走 close 路径")

        # 用例 3：评论命令「成功」但没落地 ⇒ 同样必须失败（判据不信退出码）
        rc3, recorded3, _, out3 = run_case(tmp, "CLOSED", "0", case="c3")
        if rc3 == 0:
            failures.append(f"用例3 期望非 0 退出（回执未落地必须报错），实得 0")
        if "回执未落地" not in out3:
            failures.append(f"用例3 期望报「回执未落地」，实得：{out3.strip()[:200]}")

        # ── --no-release 模式（2026-10-22 新增）────────────────────────────
        # 背景：25 条 CI 基础设施条目用「PR 正文带 Closes #N」自动关单，
        # 自动关闭**不产生评论** ⇒ 零回执。根因是旧脚本只有 --tag 一条路径，
        # 不发版本的改动**没有任何回执通道** ⇒ 巡检链最后一步被静默跳过。
        # 用例 4：无发布回执必须真的落地（不要求 tag 存在）
        # **必须传 --note**：默认正文含「见 PR #70。」⇒ 会让「PR #70 出现在回执里」
        # 这条断言在**证据行被删掉时**仍然成立（变异测试 M5 实测漏网）⇒
        # 断言要卡在「**合入**」证据行上，而不是「PR 号出现过」这种弱判据。
        rc4, recorded4, _, out4 = run_case(
            tmp, "CLOSED", "1",
            extra=["--no-release", "--pr", "70", "--note", "改动摘要：并发组按事件类型隔离"],
            case="c4")
        if rc4 != 0:
            failures.append(f"用例4 期望退出码 0，实得 {rc4}：{out4.strip()[:200]}")
        if "无 SemVer 发布" not in recorded4:
            failures.append("用例4 回执未落地：没收到无发布模式的回执评论")
        if "改动摘要：并发组按事件类型隔离" not in recorded4:
            failures.append("用例4 回执缺变更内容：--note 没落进回执")
        if "**合入**" not in recorded4:
            failures.append("用例4 回执缺合入证据行（PR / merge commit / CI 结论）")
        if "PR #70" not in recorded4:
            failures.append("用例4 回执缺 PR 号")

        # 用例 5：无发布模式下评论没落地 ⇒ 必须失败（判据同样不信退出码）
        rc5, recorded5, _, out5 = run_case(
            tmp, "CLOSED", "0",
            extra=["--no-release", "--pr", "70", "--note", "改动摘要"],
            case="c5")
        if rc5 == 0:
            failures.append("用例5 期望非 0 退出（回执未落地必须报错），实得 0")
        if "回执未落地" not in out5:
            failures.append(f"用例5 期望报「回执未落地」，实得：{out5.strip()[:200]}")

        # 用例 6（反证）：issue 上**有**评论，但那条评论**不含本次回执的 needle**
        # ⇒ 必须判「回执未落地」。
        # 这条用例证明判据不是「issue 上有任何评论就算数」——那种判据在
        # 「旧回执残留」「评论发到别的 issue」时都会假绿。
        rc6, _, _, out6 = run_case(tmp, "CLOSED", "1",
                                   extra=["--no-release", "--pr", "70"],
                                   comments_body="以前的一条无关评论", case="c6")
        if rc6 == 0:
            failures.append("用例6 期望非 0 退出（needle 不匹配的旧评论不得算回执），实得 0")
        if "回执未落地" not in out6:
            failures.append(f"用例6 期望报「回执未落地」，实得：{out6.strip()[:200]}")

        # 用例 6b（反证·半匹配）：旧评论**带**了「无 SemVer 发布」标记但**没有** PR 号
        # ⇒ 仍然必须判「回执未落地」。
        # 这条是 6 的加强版：只断言「标记匹配」会把 needle 弱化（丢掉 PR 号）漏过去
        # （变异测试 M6 实测漏网）⇒ needle 必须**同时**满足全部条件。
        rc6b, _, _, out6b = run_case(tmp, "CLOSED", "1",
                                     extra=["--no-release", "--pr", "70"],
                                     comments_body="上一轮的回执：无 SemVer 发布", case="c6b")
        if rc6b == 0:
            failures.append("用例6b 期望非 0 退出（半匹配的旧评论不得算回执），实得 0")
        if "回执未落地" not in out6b:
            failures.append(f"用例6b 期望报「回执未落地」，实得：{out6b.strip()[:200]}")

        # 用例 7：--tag 与 --no-release 互斥（两个都传 / 都不传都要判红）
        rc7, _, _, out7 = run_case(tmp, "CLOSED", "1",
                                   extra=["--no-release", "--tag", "v0.19.0"], case="c7")
        if rc7 == 0:
            failures.append("用例7 期望非 0 退出（--tag 与 --no-release 互斥），实得 0")
        if "二选一" not in out7:
            failures.append(f"用例7 期望报「二选一」，实得：{out7.strip()[:200]}")

        # ── 目标��� PR（2026-10-22 新增）────────────────────────────────────
        # 实测：上一轮的 25 个「issue」其实是 25 个 **PR**（gh issue view 对 PR
        # 编号也能解析，state 是 MERGED/OPEN 而不是 CLOSED/OPEN）。
        # ⇒ ① 脚本绝不能对 PR 调 close（对未合入的 PR 就是「不合并直接关掉」）；
        #    ② 关单后校验 state == "closed" 对 PR 永远不成立 ⇒ 必然误判红。
        # 用例 8：PR 目标 ⇒ 只发回执、**绝不 close**、退出码 0
        rc8, recorded8, closed8, out8 = run_case(
            tmp, "MERGED", "1",
            extra=["--no-release", "--pr", "70", "--note", "改动摘要"],
            kind="pr", pr_state="MERGED", case="c8")
        if rc8 != 0:
            failures.append(f"用例8 期望退出码 0，实得 {rc8}：{out8.strip()[:200]}")
        if "CLOSE_CALLED" in closed8:
            failures.append("用例8 PR 目标被调了 gh issue close——"
                            "对未合入的 PR 就是「不合并直接关掉」，必须只发回执")
        if "无 SemVer 发布" not in recorded8:
            failures.append("用例8 PR 目标回执未落地")

        # 用例 9：PR 尚未合入 ⇒ 回执**不得**声明「已合入修复」（否则是假声明）
        rc9, recorded9, closed9, out9 = run_case(
            tmp, "OPEN", "1",
            extra=["--no-release", "--pr", "70", "--note", "改动摘要"],
            kind="pr", pr_state="OPEN", case="c9")
        if rc9 != 0:
            failures.append(f"用例9 期望退出码 0，实得 {rc9}：{out9.strip()[:200]}")
        if "CLOSE_CALLED" in closed9:
            failures.append("用例9 PR 目标被调了 gh issue close")
        if "已合入修复" in recorded9:
            failures.append("用例9 PR 未合入却声明「已合入修复」——回执不得做假声明")
        if "已提交修复" not in recorded9:
            failures.append("用例9 PR 未合入，回执应写「已提交修复（PR 尚未合入）」")

        # 用例 11（#119）：目标是 **issue**，但 --pr 指向**未合入**的 PR ⇒
        # ① 回执**不得**声明「已合入修复」（那是假声明）；
        # ② **绝不能关单**（修复尚未落地 ⇒ 关单＝问题提前从看板消失）。
        #
        # 旧实现只在「目标是 PR」时才查 PR 的合入状态 ⇒ 目标是 issue 时
        # 无条件写「已合入修复 ✅」并关单。
        # 2026-10-28 实测：`close_issue.py gormx#36 --no-release --pr 39` —— PR #39
        # 当时 OPEN（CI 已绿、未合入），回执写成「已合入修复 ✅（无 SemVer 发布）」，
        # issue 被 closed ⇒ 问题从看板消失，而修复还没进来。
        rc11, recorded11, closed11, out11 = run_case(
            tmp, "OPEN", "1",
            extra=["--no-release", "--pr", "70", "--note", "改动摘要"],
            kind="issue", pr_state="OPEN", case="c11")
        if rc11 != 0:
            failures.append(f"用例11 期望退出码 0，实得 {rc11}：{out11.strip()[:200]}")
        if "已合入修复" in recorded11:
            failures.append("用例11 PR 未合入却声明「已合入修复」——回执不得做假声明")
        if "已提交修复" not in recorded11:
            failures.append("用例11 PR 未合入，回执应写「已提交修复（PR 尚未合入）」")
        if "CLOSE_CALLED" in closed11:
            failures.append("用例11 修复未落地却关了单——问题会提前从看板消失")

        # 用例 10（幂等）：回执已在 ⇒ 不重复发（重跑不能堆一串重复回执）
        rc10, recorded10, _, out10 = run_case(
            tmp, "CLOSED", "1",
            extra=["--no-release", "--pr", "70", "--note", "改动摘要"],
            comments_body="已合入修复 ✅（无 SemVer 发布）\nPR #70", case="c10")
        if rc10 != 0:
            failures.append(f"用例10 期望退出码 0，实得 {rc10}：{out10.strip()[:200]}")
        if "幂等" not in out10:
            failures.append(f"用例10 期望报「幂等跳过」，实得：{out10.strip()[:200]}")
        if recorded10:
            failures.append("用例10 回执已在时不应再发评论（记录里出现了新评论）")

    if failures:
        print("✗ close_issue 自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ close_issue 自测通过：已关闭 issue 也会补发回执；回执未落地时判红不假绿；"
          "无发布回执模式可用；PR 目标只发回执不 close；重复执行幂等")


if __name__ == "__main__":
    main()
