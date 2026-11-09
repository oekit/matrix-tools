#!/usr/bin/env python3
"""ledger_write.py 的自测 —— 证明「事实回读」这道断言**抓得住**它要防的东西。

这个门禁防的是什么：
  台账 `.oekit-cron-state.json` 里的每条记录，会被**下一轮的去重**读。
  ⇒ 一旦台账里写进「已关单 / PR 已合 / tag 已发」而现实不是这样
  ⇒ 下一轮会跳过该 issue（重复劳动）或重复发版（tag 冲突）。

⇒ 所以写入器的关键性质是：**事实不符 ⇒ 拒绝写入**，而不是写一条假的进去。

本自测用**假 gh**（PATH 注入）+ **临时台账** ⇒ 无副作用、不联网、不依赖工作区布局。

断言（每条都要能判红）：
  1. 正常形态：假 gh 说 issue 已关 / PR 已合 / tag 存在 ⇒ 写入成功，
     且**台账里的事实字段来自回读**（不是命令行参数）。
  2. `--closed` 但假 gh 说 issue 还 OPEN ⇒ **拒绝写入**（退出码非 0），台账不被改。
  3. `--pr` 但假 gh 说 PR 还是 OPEN ⇒ **拒绝写入**。
  4. `--tag` 但远端查不到该 tag ⇒ **拒绝写入**。
  5. `--run-end` 复核：先按「已合」记一条，再把假 gh 翻成 OPEN
     ⇒ 收口必须**报出漂移**（退出码非 0）。
  6. 台账文件是坏 JSON ⇒ 明确报错且**不覆盖**原文件。

用法：python3 scripts/ledger_write_selftest.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOL = ROOT / "scripts" / "ledger_write.py"

# 假 gh：复刻真实 gh 在这几个查询上的**取值形态**（2026-11-01 实测踩坑：
# 真实 `gh issue view --json state` 返回**大写** CLOSED / OPEN）。
FAKE_GH = """#!/bin/sh
case "$1" in
  issue)
    printf '{"state":"%s","isPullRequest":%s}\\n' "${FAKE_ISSUE_STATE:-CLOSED}" "${FAKE_ISSUE_IS_PR:-false}"
    ;;
  pr)
    if [ "${FAKE_PR_STATE:-MERGED}" = "MERGED" ]; then
      printf '{"state":"MERGED","mergeCommit":{"oid":"%s"}}\\n' "${FAKE_PR_SHA:-1111111111111111111111111111111111111111}"
    else
      printf '{"state":"%s","mergeCommit":null}\\n' "${FAKE_PR_STATE:-OPEN}"
    fi
    ;;
  release)
    # gh release view <tag> --repo X --json tagName  ⇒ $1=release $2=view $3=tag
    if [ -n "${FAKE_MISSING_RELEASE:-}" ] && [ "$3" = "${FAKE_MISSING_RELEASE}" ]; then
      echo "gh: HTTP 404: Not Found (https://api.github.com/releases/$3)" >&2
      exit 1
    fi
    printf '{"tagName":"%s"}\n' "$3"
    ;;
  api)
    case "$2" in
      *"/${FAKE_MISSING_TAG:-__no_such_tag__}")
        echo "gh: HTTP 404: Not Found (https://api.github.com/$2)" >&2
        exit 1
        ;;
      *)
        printf '{"ref":"refs/tags/fake","object":{"sha":"%s"}}\\n' "${FAKE_TAG_SHA:-2222222222222222222222222222222222222222}"
        ;;
    esac
    ;;
  *)
    echo "fake gh: unsupported: $*" >&2
    exit 2
    ;;
esac
"""


def run_tool(args: list[str], env_extra: dict[str, str], ledger: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    env["OEKIT_LEDGER"] = str(ledger)
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, env=env)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bindir = tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        base_env = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}

        def ledger_of(name: str) -> Path:
            p = tmp / name
            p.write_text(json.dumps({"runs": [], "last_run": None}, ensure_ascii=False),
                         encoding="utf-8")
            return p

        # ── 用例 1：正常形态 ────────────────────────────────────────────
        led = ledger_of("ok.json")
        r = run_tool(["--new-run"], base_env, led)
        if r.returncode != 0:
            failures.append(f"用例1 开新一轮失败：{r.stdout}{r.stderr}")
        r = run_tool(["--issue", "pkg#47", "--pr", "51", "--tag", "v0.29.0",
                      "--kind", "Bug", "--priority", "P1", "--closed",
                      "--note", "分页口径修复"], base_env, led)
        if r.returncode != 0:
            failures.append(f"用例1 记入失败：{r.stdout}{r.stderr}")
        else:
            data = json.loads(led.read_text(encoding="utf-8"))
            entries = data["runs"][-1]["handled"]
            if len(entries) != 1:
                failures.append(f"用例1 台账里应有 1 条，实际 {len(entries)}")
            else:
                e = entries[0]
                # 事实字段必须来自**回读**（PR 的合并 sha 来自 gh，不是参数）
                if e.get("merge_commit") != "1111111111111111111111111111111111111111":
                    failures.append(f"用例1 merge_commit 不是回读来的：{e.get('merge_commit')}")
                if e.get("tag") != "v0.29.0" or "releases/tag/v0.29.0" not in (e.get("release") or ""):
                    failures.append(f"用例1 tag/release 不对：{e.get('tag')} / {e.get('release')}")
                if e.get("closed") is not True:
                    failures.append(f"用例1 closed 不对：{e.get('closed')}")
                # #268：落库键是全键 org/repo#N——「记的是哪个 org」必须在形态里可见
                if e.get("issue") != "oekit/pkg#47" or e.get("kind") != "Bug" or e.get("priority") != "P1":
                    failures.append(f"用例1 元数据不对：{e}")
            print("  ✓ 用例1 正常形态 ⇒ 写入成功且事实来自回读")

        # ── 用例 2：--closed 但 issue 还 OPEN ⇒ 拒绝写入 ────────────────
        led2 = ledger_of("open.json")
        run_tool(["--new-run"], base_env, led2)
        before = led2.read_text(encoding="utf-8")
        env_open = dict(base_env)
        env_open["FAKE_ISSUE_STATE"] = "OPEN"
        r = run_tool(["--issue", "pkg#48", "--closed"], env_open, led2)
        if r.returncode == 0:
            failures.append("用例2 issue 还 OPEN 却写入成功 ⇒ 断言无效")
        elif "拒绝写入" not in (r.stdout + r.stderr):
            failures.append(f"用例2 拒绝时没给出原因：{r.stdout}{r.stderr}")
        elif led2.read_text(encoding="utf-8") != before:
            failures.append("用例2 拒绝写入却把台账改了")
        else:
            print("  ✓ 用例2 issue 还 OPEN ⇒ 拒绝写入且台账未被改")

        # ── 用例 3：--pr 但 PR 还是 OPEN ⇒ 拒绝写入 ─────────────────────
        led3 = ledger_of("pr.json")
        run_tool(["--new-run"], base_env, led3)
        env_pr = dict(base_env)
        env_pr["FAKE_PR_STATE"] = "OPEN"
        r = run_tool(["--issue", "pkg#49", "--pr", "52"], env_pr, led3)
        if r.returncode == 0:
            failures.append("用例3 PR 未合却写入成功 ⇒ 断言无效")
        else:
            print("  ✓ 用例3 PR 还是 OPEN ⇒ 拒绝写入")

        # ── 用例 4：--tag 但远端没有该 tag ⇒ 拒绝写入 ────────────────────
        led4 = ledger_of("tag.json")
        run_tool(["--new-run"], base_env, led4)
        env_tag = dict(base_env)
        env_tag["FAKE_MISSING_TAG"] = "v9.9.9"
        r = run_tool(["--issue", "pkg#50", "--tag", "v9.9.9"], env_tag, led4)
        if r.returncode == 0:
            failures.append("用例4 远端无该 tag 却写入成功 ⇒ 断言无效")
        else:
            print("  ✓ 用例4 远端查不到该 tag ⇒ 拒绝写入")

        # ── 用例 5：--run-end 复核 ⇒ 现实变了必须报出漂移 ─────────────────
        led5 = ledger_of("drift.json")
        run_tool(["--new-run"], base_env, led5)
        run_tool(["--issue", "pkg#51", "--pr", "53"], base_env, led5)
        env_drift = dict(base_env)
        env_drift["FAKE_PR_STATE"] = "OPEN"   # 记录完才发现 PR 没合
        r = run_tool(["--run-end"], env_drift, led5)
        if r.returncode == 0:
            failures.append("用例5 现实漂移了却收口成功 ⇒ 复核无效")
        elif "漂移" not in (r.stdout + r.stderr):
            failures.append(f"用例5 收口没指出漂移：{r.stdout}{r.stderr}")
        else:
            print("  ✓ 用例5 收口复核 ⇒ 报出「记录 ≠ 现实」的漂移")

        # ── 用例 6：坏台账 ⇒ 明确报错且不覆盖 ────────────────────────────
        led6 = tmp / "broken.json"
        broken = '{"runs": [ {"started_at": "x" '
        led6.write_text(broken, encoding="utf-8")
        r = run_tool(["--new-run"], base_env, led6)
        if r.returncode == 0:
            failures.append("用例6 坏台账却写入成功 ⇒ 工具会毁掉可恢复的账")
        elif led6.read_text(encoding="utf-8") != broken:
            failures.append("用例6 坏台账被覆盖了")
        else:
            print("  ✓ 用例6 坏台账 ⇒ 明确报错且原文件未被覆盖")

        # ═══ #211：台账封存保证 + 收口范围 + 修正通道 ═══════════════════
        #
        # ① 封存保证：已收口的轮次**不许**再追加条目
        #    （旧实现只查「最新一轮」且不查 finished_at ⇒ 收口后仍可往封存轮次里塞）
        # ② 收口范围：`--run-end` 必须收**全部未收口轮次**
        #    （旧实现只收 `runs[-1]` ⇒ 更早的未收口轮次永久悬空、记录从未复核）
        # ③ 修正通道：`--amend` 能改错记录（否则错记录永久阻塞收口）
        #    （实测 2026-11-03：本轮真事故——并发会话误关 issue ⇒ 台账回读成
        #      closed=True ⇒ 收口被漂移拦下 ⇒ 只能绕过工具手改 JSON）

        # ── 用例 7：已收口的轮次 ⇒ 拒绝追加 ──────────────────────────────
        led7 = ledger_of("sealed.json")
        run_tool(["--new-run"], base_env, led7)
        run_tool(["--issue", "pkg#60", "--closed"], base_env, led7)
        r = run_tool(["--run-end"], base_env, led7)
        if r.returncode != 0:
            failures.append(f"用例7 收口失败：{r.stdout}{r.stderr}")
        before7 = led7.read_text(encoding="utf-8")
        r = run_tool(["--issue", "pkg#61", "--closed"], base_env, led7)
        if r.returncode == 0:
            failures.append("用例7 往已收口轮次追加成功 ⇒ 封存保证被打破")
        elif "已收口" not in (r.stdout + r.stderr):
            failures.append(f"用例7 拒绝时没说明「已收口」：{r.stdout}{r.stderr}")
        elif led7.read_text(encoding="utf-8") != before7:
            failures.append("用例7 拒绝写入却把台账改了")
        else:
            print("  ✓ 用例7 往已收口轮次追加 ⇒ 拒绝且台账未被改")

        # ── 用例 8：--new-run 之后追加 ⇒ 正常写入 ─────────────────────────
        r = run_tool(["--new-run"], base_env, led7)
        if r.returncode != 0:
            failures.append(f"用例8 开新一轮失败：{r.stdout}{r.stderr}")
        r = run_tool(["--issue", "pkg#62", "--closed"], base_env, led7)
        if r.returncode != 0:
            failures.append(f"用例8 新一轮里追加失败：{r.stdout}{r.stderr}")
        else:
            data7 = json.loads(led7.read_text(encoding="utf-8"))
            if len(data7["runs"]) != 2 or len(data7["runs"][-1]["handled"]) != 1:
                failures.append(f"用例8 台账形状不对：{data7['runs']}")
            else:
                print("  ✓ 用例8 --new-run 之后追加 ⇒ 正常写入")

        # ── 用例 9：--run-end 收口**全部**未收口轮次 ─────────────────────
        #    造两轮：第一轮**未收口**（悬空），第二轮也未收口 ⇒ 一次收口全收
        led9 = tmp / "multi.json"
        led9.write_text(json.dumps({
            "runs": [
                {"started_at": "2026-11-01T00:00:00Z", "finished_at": None,
                 "handled": [{"issue": "pkg#70", "org": "oekit", "closed": True}],
                 "also_done": [], "skip": []},
                {"started_at": "2026-11-02T00:00:00Z", "finished_at": None,
                 "handled": [{"issue": "pkg#71", "org": "oekit", "closed": True}],
                 "also_done": [], "skip": []},
            ], "last_run": "2026-11-02T00:00:00Z"}, ensure_ascii=False), encoding="utf-8")
        r = run_tool(["--run-end"], base_env, led9)
        if r.returncode != 0:
            failures.append(f"用例9 收口失败：{r.stdout}{r.stderr}")
        else:
            data9 = json.loads(led9.read_text(encoding="utf-8"))
            unclosed = [i for i, rr in enumerate(data9["runs"]) if not rr.get("finished_at")]
            if unclosed:
                failures.append(f"用例9 仍有未收口轮次 {unclosed} ⇒ 只收了最新一轮"
                                f"（更早的轮次永久悬空、记录从未复核）")
            else:
                print("  ✓ 用例9 --run-end 收口全部未收口轮次（不只最新一轮）")

        # ── 用例 10：--amend 修正错记录 ⇒ 能改，且留痕 ───────────────────
        led10 = tmp / "amend.json"
        led10.write_text(json.dumps({
            "runs": [{"started_at": "2026-11-02T00:00:00Z", "finished_at": None,
                      "handled": [{"issue": "pkg#80", "org": "oekit", "closed": True,
                                   "tag": None, "note": "错的记录"}],
                      "also_done": [], "skip": []}], "last_run": "2026-11-02T00:00:00Z"},
            ensure_ascii=False), encoding="utf-8")
        # 现实：issue 其实还 OPEN ⇒ 记录里的 closed=True 是错的
        env10 = dict(base_env)
        env10["FAKE_ISSUE_STATE"] = "OPEN"
        r = run_tool(["--issue", "pkg#80", "--amend-open"], env10, led10)
        if r.returncode != 0:
            failures.append(f"用例10 amend 失败：{r.stdout}{r.stderr}")
        else:
            data10 = json.loads(led10.read_text(encoding="utf-8"))
            e10 = data10["runs"][0]["handled"][0]
            if e10.get("closed") is not False:
                failures.append(f"用例10 closed 没被修正：{e10}")
            elif not e10.get("amended_at") or e10.get("amended_from") is None:
                failures.append(f"用例10 修正没有留痕：{e10}")
            else:
                print("  ✓ 用例10 --amend 修正错记录 ⇒ 生效且留痕（amended_at/from）")

        # ── 用例 11：amend 与现实冲突 ⇒ 拒绝（事实不可知时拒绝写入）────────
        led11 = tmp / "amend_conflict.json"
        led11.write_text(json.dumps({
            "runs": [{"started_at": "2026-11-02T00:00:00Z", "finished_at": None,
                      "handled": [{"issue": "pkg#81", "org": "oekit", "closed": False}],
                      "also_done": [], "skip": []}], "last_run": "2026-11-02T00:00:00Z"},
            ensure_ascii=False), encoding="utf-8")
        # 现实：issue 已 CLOSED ⇒ 改成 closed=True 是对的；改成 false 才是错的
        before11 = led11.read_text(encoding="utf-8")
        r = run_tool(["--issue", "pkg#81", "--amend-open"], base_env, led11)
        if r.returncode == 0:
            failures.append("用例11 amend 与现实冲突却写入成功 ⇒ 会把台账改成假的")
        elif led11.read_text(encoding="utf-8") != before11:
            failures.append("用例11 拒绝写入却把台账改了")
        else:
            print("  ✓ 用例11 amend 与现实冲突 ⇒ 拒绝且台账未被改")

        # ── 用例 12：amend 目标不存在 ⇒ 拒绝（不许凭印象改）──────────────
        led12 = tmp / "amend_missing.json"
        led12.write_text(json.dumps({"runs": [], "last_run": None}, ensure_ascii=False),
                         encoding="utf-8")
        r = run_tool(["--issue", "pkg#99", "--amend-open"], base_env, led12)
        if r.returncode == 0:
            failures.append("用例12 amend 不存在的记录却成功 ⇒ 会凭空造记录")
        else:
            print("  ✓ 用例12 amend 目标不存在 ⇒ 拒绝")

        # ── 用例 13：写入时记 fact_at（时间窗自洽的输入）──────────────────
        led13 = ledger_of("factat.json")
        run_tool(["--new-run"], base_env, led13)
        run_tool(["--issue", "pkg#85", "--closed"], base_env, led13)
        data13 = json.loads(led13.read_text(encoding="utf-8"))
        e13 = data13["runs"][-1]["handled"][0]
        if not e13.get("fact_at"):
            failures.append(f"用例13 条目没有 fact_at（时间窗自洽无从核对）：{e13}")
        else:
            print("  ✓ 用例13 写入时记 fact_at ⇒ 轮次时间窗可与事实核对")

        # ── 用例 14：全部收口后再跑 --run-end ⇒ 退出码 0（不是错误）────────
        #    旧实现会「重收最后一轮」；新语义下「没有未收口的轮次」是**干净状态**
        #    ⇒ 退出码必须仍是 0 ⇒ 否则「重复跑 --run-end」会打破调用方。
        led14 = ledger_of("allclosed.json")
        run_tool(["--new-run"], base_env, led14)
        run_tool(["--issue", "pkg#86", "--closed"], base_env, led14)
        r = run_tool(["--run-end"], base_env, led14)
        if r.returncode != 0:
            failures.append(f"用例14 首次收口失败：{r.stdout}{r.stderr}")
        before14 = led14.read_text(encoding="utf-8")
        r = run_tool(["--run-end"], base_env, led14)
        if r.returncode != 0:
            failures.append(f"用例14 全部收口后再跑 --run-end 退出码非 0"
                            f"（重复跑会打破调用方）：{r.stdout}{r.stderr}")
        elif led14.read_text(encoding="utf-8") != before14:
            failures.append("用例14 重复 --run-end 把台账改了")
        else:
            print("  ✓ 用例14 全部收口后再跑 --run-end ⇒ 退出码 0 且台账未变")

        # ═══ 2026-11-04：「无 issue 锚点的发版」通道（台账断链的根因之一）═══
        #
        # 形态：工具仓自身发版（matrix-tools v0.2.0）、umbrella 仓发版（oekit BOM v0.2.0）、
        #       纯重构发版……这些发版**没有** issue 锚点 ⇒ 旧形态 `--issue repo#N`
        #       强制要求 `#N` ⇒ **根本记不进去** ⇒ 台账与现实分叉（实测：8 次发版断链）。

        # ── 用例 15：--release-only 正常写入 ⇒ 键形态 / 事实字段都对 ──────
        led15 = ledger_of("rel_only.json")
        run_tool(["--new-run"], base_env, led15)
        r = run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9",
                      "--pr", "59", "--kind", "Chore", "--priority", "P2",
                      "--note", "工具仓自身发版"], base_env, led15)
        if r.returncode != 0:
            failures.append(f"用例15 release-only 写入失败：{r.stdout}{r.stderr}")
        else:
            data15 = json.loads(led15.read_text(encoding="utf-8"))
            entries15 = data15["runs"][-1]["handled"]
            if len(entries15) != 1:
                failures.append(f"用例15 台账里应有 1 条，实际 {len(entries15)}")
            else:
                e = entries15[0]
                if e.get("issue") != "（发版）oekit/pkg@v9.9.9":
                    failures.append(f"用例15 键形态不对（应是 （发版）oekit/pkg@v9.9.9，#268 全键）：{e.get('issue')}")
                elif not e.get("release_only"):
                    failures.append(f"用例15 记录缺 release_only 标记：{e}")
                elif e.get("tag") != "v9.9.9" or "releases/tag/v9.9.9" not in (e.get("release") or ""):
                    failures.append(f"用例15 tag/release 不对：{e.get('tag')} / {e.get('release')}")
                elif e.get("closed") is not False:
                    failures.append(f"用例15 无锚点发版不该有 closed=True：{e}")
                elif e.get("merge_commit") != "1111111111111111111111111111111111111111":
                    failures.append(f"用例15 merge_commit 不是回读来的：{e.get('merge_commit')}")
                else:
                    print("  ✓ 用例15 release-only 写入 ⇒ 键形态与事实字段都对")

        # ── 用例 16：Release 不存在 ⇒ 拒绝写入（tag 推了但没发 Release）────
        led16 = ledger_of("rel_norelease.json")
        run_tool(["--new-run"], base_env, led16)
        before16 = led16.read_text(encoding="utf-8")
        env16 = dict(base_env)
        env16["FAKE_MISSING_RELEASE"] = "v9.9.9"
        r = run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9"], env16, led16)
        if r.returncode == 0:
            failures.append("用例16 Release 不存在却写入成功 ⇒ 会把「tag 推了但没发 Release」记成已发布")
        elif "拒绝写入" not in (r.stdout + r.stderr):
            failures.append(f"用例16 拒绝时没给出原因：{r.stdout}{r.stderr}")
        elif led16.read_text(encoding="utf-8") != before16:
            failures.append("用例16 拒绝写入却把台账改了")
        else:
            print("  ✓ 用例16 Release 不存在 ⇒ 拒绝写入且台账未被改")

        # ── 用例 17：tag 不存在 ⇒ 拒绝写入 ──────────────────────────────
        led17 = ledger_of("rel_notag.json")
        run_tool(["--new-run"], base_env, led17)
        env17 = dict(base_env)
        env17["FAKE_MISSING_TAG"] = "v8.8.8"
        r = run_tool(["--release-only", "--repo", "pkg", "--tag", "v8.8.8"], env17, led17)
        if r.returncode == 0:
            failures.append("用例17 tag 不存在却写入成功 ⇒ 断言无效")
        else:
            print("  ✓ 用例17 tag 不存在 ⇒ 拒绝写入")

        # ── 用例 18：PR 未合 ⇒ 拒绝写入 ─────────────────────────────────
        led18 = ledger_of("rel_pr.json")
        run_tool(["--new-run"], base_env, led18)
        env18 = dict(base_env)
        env18["FAKE_PR_STATE"] = "OPEN"
        r = run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9",
                      "--pr", "60"], env18, led18)
        if r.returncode == 0:
            failures.append("用例18 PR 未合却写入成功 ⇒ 断言无效")
        else:
            print("  ✓ 用例18 PR 未合 ⇒ 拒绝写入")

        # ── 用例 19：release-only 记录 ⇒ --run-end **要复核它** ──────────
        #    （否则「记了没人核」＝白记：旧复核按 repo#N 解析键 ⇒ 解析不出数字 ⇒ 跳过）
        led19a = ledger_of("rel_runend_ok.json")
        run_tool(["--new-run"], base_env, led19a)
        run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9",
                  "--pr", "59"], base_env, led19a)
        r = run_tool(["--run-end"], base_env, led19a)
        if r.returncode != 0:
            failures.append(f"用例19 release-only 记录的正常收口失败：{r.stdout}{r.stderr}")
        else:
            print("  ✓ 用例19a release-only 记录 ⇒ --run-end 正常收口通过")
        # 现实变了：Release 没了 ⇒ 收口必须报出漂移（证明它**真的在复核**）
        led19b = ledger_of("rel_runend_drift.json")
        run_tool(["--new-run"], base_env, led19b)
        run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9",
                  "--pr", "59"], base_env, led19b)
        env19 = dict(base_env)
        env19["FAKE_MISSING_RELEASE"] = "v9.9.9"
        r = run_tool(["--run-end"], env19, led19b)
        if r.returncode == 0:
            failures.append("用例19 Release 消失后收口仍成功 ⇒ release-only 记录**根本没被复核**")
        elif "漂移" not in (r.stdout + r.stderr):
            failures.append(f"用例19 收口没指出漂移：{r.stdout}{r.stderr}")
        else:
            print("  ✓ 用例19 Release 消失 ⇒ --run-end 报出漂移（release-only 记录确实被复核）")

        # ── 用例 20：往**已收口**轮次追加 release-only ⇒ 拒绝（封存保证覆盖新通道）──
        led20 = ledger_of("rel_sealed.json")
        run_tool(["--new-run"], base_env, led20)
        run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9"], base_env, led20)
        r = run_tool(["--run-end"], base_env, led20)
        if r.returncode != 0:
            failures.append(f"用例20 首次收口失败：{r.stdout}{r.stderr}")
        before20 = led20.read_text(encoding="utf-8")
        r = run_tool(["--release-only", "--repo", "pkg", "--tag", "v8.8.8"], base_env, led20)
        if r.returncode == 0:
            failures.append("用例20 往已收口轮次追加 release-only 成功 ⇒ 封存保证对新通道失效")
        elif "已收口" not in (r.stdout + r.stderr):
            failures.append(f"用例20 拒绝时没说明「已收口」：{r.stdout}{r.stderr}")
        elif led20.read_text(encoding="utf-8") != before20:
            failures.append("用例20 拒绝写入却把台账改了")
        else:
            print("  ✓ 用例20 往已收口轮次追加 release-only ⇒ 拒绝且台账未被改")

        # ── 用例 21：缺 --repo ⇒ 拒绝（不许记成无名仓的发版）─────────────
        led21 = ledger_of("rel_norepo.json")
        run_tool(["--new-run"], base_env, led21)
        r = run_tool(["--release-only", "--tag", "v9.9.9"], base_env, led21)
        if r.returncode == 0:
            failures.append("用例21 缺 --repo 却写入成功 ⇒ 会记成无名仓的发版")
        else:
            print("  ✓ 用例21 缺 --repo ⇒ 拒绝写入")

        # ── 用例 22：--release-only 与 --issue 同时给 ⇒ 拒绝（一条记录一个主语）──
        led22 = ledger_of("rel_and_issue.json")
        run_tool(["--new-run"], base_env, led22)
        r = run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9",
                      "--issue", "pkg#47"], base_env, led22)
        if r.returncode == 0:
            failures.append("用例22 --release-only 与 --issue 同时给却成功 ⇒ 一条记录两种主语")
        else:
            print("  ✓ 用例22 --release-only 与 --issue 同时给 ⇒ 拒绝")

        # ── 用例 23：同一次发版重复记入 ⇒ 拒绝（台账幂等）────────────────
        led23 = ledger_of("rel_dup.json")
        run_tool(["--new-run"], base_env, led23)
        run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9"], base_env, led23)
        before23 = led23.read_text(encoding="utf-8")
        r = run_tool(["--release-only", "--repo", "pkg", "--tag", "v9.9.9"], base_env, led23)
        if r.returncode == 0:
            failures.append("用例23 同一次发版重复记入成功 ⇒ 台账会越记越脏")
        elif led23.read_text(encoding="utf-8") != before23:
            failures.append("用例23 拒绝重复写入却把台账改了")
        else:
            print("  ✓ 用例23 同一次发版重复记入 ⇒ 拒绝且台账未被改")

        # ═══ #257 / #258：--run-end --summary 收口摘要 + --issue 形状校验 ══════

        # ── 用例 24：--run-end --summary ⇒ 摘要写进该轮 summary 字段 ──────
        #    （docstring 里的收口示例此前只是文档——argparse 没有这个参数 ⇒
        #      照文档敲 rc=2、台账零字节 ⇒ 轮次永远停在未收口状态）
        led24 = ledger_of("summary.json")
        run_tool(["--new-run"], base_env, led24)
        run_tool(["--issue", "pkg#90", "--closed"], base_env, led24)
        r = run_tool(["--run-end", "--summary", "本轮 2 个 issue"], base_env, led24)
        if r.returncode != 0:
            failures.append(f"用例24 --run-end --summary 失败：{r.stdout}{r.stderr}")
        else:
            data24 = json.loads(led24.read_text(encoding="utf-8"))
            run24 = data24["runs"][-1]
            if run24.get("summary") != "本轮 2 个 issue":
                failures.append(f"用例24 summary 没写进该轮：{run24.get('summary')!r}")
            elif not run24.get("finished_at"):
                failures.append("用例24 summary 在但轮次没收口")
            else:
                print("  ✓ 用例24 --run-end --summary ⇒ 摘要写进该轮 summary 字段")

        # ── 用例 25：org/repo#N 显式形态 ⇒ 正确解析、落库键用全键（#268）──
        led25 = ledger_of("orgrepo.json")
        run_tool(["--new-run"], base_env, led25)
        r = run_tool(["--issue", "oekit/matrix-tools#254", "--note", "显式 org 形态"],
                     base_env, led25)
        if r.returncode != 0:
            failures.append(f"用例25 org/repo#N 写入失败（不应报网络错误）：{r.stdout}{r.stderr}")
        else:
            e25 = json.loads(led25.read_text(encoding="utf-8"))["runs"][-1]["handled"][0]
            if e25.get("issue") != "oekit/matrix-tools#254":
                failures.append(f"用例25 落库键应是全键 org/repo#N：{e25.get('issue')!r}")
            elif e25.get("org") != "oekit":
                failures.append(f"用例25 org 不对：{e25.get('org')!r}")
            else:
                print("  ✓ 用例25 org/repo#N ⇒ 落库键全键、org 来自串")
        # 串里显式 org 覆盖 --org 默认值
        run_tool(["--new-run"], base_env, led25)
        r = run_tool(["--org", "oekit", "--issue", "pigeonbox/demo#7"], base_env, led25)
        if r.returncode != 0:
            failures.append(f"用例25b 显式 org 覆盖 --org 失败：{r.stdout}{r.stderr}")
        else:
            e25b = json.loads(led25.read_text(encoding="utf-8"))["runs"][-1]["handled"][0]
            if e25b.get("issue") != "pigeonbox/demo#7" or e25b.get("org") != "pigeonbox":
                failures.append(f"用例25b org 覆盖不对：{e25b.get('issue')!r}/{e25b.get('org')!r}")
            else:
                print("  ✓ 用例25b 串里的 org 覆盖 --org 默认值")

        # ── 用例 26：非法形态 ⇒ 用法错误（退出码 2、台账零写入、说人话）──
        led26 = ledger_of("badshape.json")
        run_tool(["--new-run"], base_env, led26)
        before26 = led26.read_text(encoding="utf-8")
        for bad in ("oekit/a/b#1", "repo#", "#5", "repo#x", "repo name#3", "pkg#12x"):
            r = run_tool(["--issue", bad], base_env, led26)
            if r.returncode != 2:
                failures.append(f"用例26 非法形态 {bad!r} 退出码 {r.returncode}（应为 2）"
                                f"——参数非法不该归到「读 GitHub 失败」那一类")
            elif led26.read_text(encoding="utf-8") != before26:
                failures.append(f"用例26 非法形态 {bad!r} 却把台账改了")
            elif "格式" not in (r.stdout + r.stderr) and "形态" not in (r.stdout + r.stderr):
                failures.append(f"用例26 非法形态 {bad!r} 的报错没说清用法：{r.stdout}{r.stderr}")
        print("  ✓ 用例26 非法形态 ⇒ 退出码 2、零写入、报错说人话")

        # ── 用例 27：amend 通道同款形状校验 ─────────────────────────────
        led27 = ledger_of("amend_badshape.json")
        run_tool(["--new-run"], base_env, led27)
        before27 = led27.read_text(encoding="utf-8")
        r = run_tool(["--issue", "oekit/a/b#3", "--amend-open"], base_env, led27)
        if r.returncode != 2:
            failures.append(f"用例27 amend 通道非法形态退出码 {r.returncode}（应为 2）")
        elif led27.read_text(encoding="utf-8") != before27:
            failures.append("用例27 amend 非法形态却把台账改了")
        else:
            print("  ✓ 用例27 amend 通道非法形态 ⇒ 退出码 2 且台账未被改")

        # ═══ #268：org 全键化 / PR-非-issue / 主语修正通道 ══════════════════
        # 背景（#268 实测三连）：--org 默认 oekit 且不在 --issue 形态里 ⇒
        # oagentkit/matrix-tools#194（一个 OPEN issue）被静默记成
        # oekit/matrix-tools#194（一个已合并 PR），复核读同一个错主语还判「一致」。

        # ── 用例 28：主语是 PR ⇒ 一律拒绝（不管 MERGED 还是 OPEN）────────
        led28 = ledger_of("pr_as_issue.json")
        run_tool(["--new-run"], base_env, led28)
        before28 = led28.read_text(encoding="utf-8")
        env28 = dict(base_env)
        env28["FAKE_ISSUE_IS_PR"] = "true"
        env28["FAKE_ISSUE_STATE"] = "MERGED"
        r = run_tool(["--issue", "pkg#88", "--closed"], env28, led28)
        if r.returncode == 0:
            failures.append("用例28 PR（MERGED）被记成了 issue ⇒ #268 的原始事故复现")
        elif "PR" not in (r.stdout + r.stderr):
            failures.append(f"用例28 拒绝时没说清「这是 PR 不是 issue」：{r.stdout}{r.stderr}")
        elif led28.read_text(encoding="utf-8") != before28:
            failures.append("用例28 拒绝写入却把台账改了")
        else:
            print("  ✓ 用例28 主语是 PR（MERGED）⇒ 拒绝记成 issue 且台账未被改")
        env28b = dict(base_env)
        env28b["FAKE_ISSUE_IS_PR"] = "true"
        env28b["FAKE_ISSUE_STATE"] = "OPEN"
        r = run_tool(["--issue", "pkg#89"], env28b, led28)
        if r.returncode == 0:
            failures.append("用例28b OPEN 的 PR 被记成了 issue ⇒ 区分不彻底")
        else:
            print("  ✓ 用例28b 主语是 PR（OPEN）⇒ 同样拒绝")

        # ── 用例 29：--org 显式给了且与串里的 org 冲突 ⇒ 用法错误（不猜）──
        led29 = ledger_of("org_conflict.json")
        run_tool(["--new-run"], base_env, led29)
        before29 = led29.read_text(encoding="utf-8")
        r = run_tool(["--org", "oagentkit", "--issue", "oekit/pkg#5", "--closed"],
                     base_env, led29)
        if r.returncode != 2:
            failures.append(f"用例29 org 冲突退出码 {r.returncode}（应为 2，不静默猜）")
        elif led29.read_text(encoding="utf-8") != before29:
            failures.append("用例29 org 冲突却把台账改了")
        else:
            print("  ✓ 用例29 --org 与 --issue 的 org 冲突 ⇒ 退出码 2、零写入")

        # ── 用例 30：--amend-issue 换主语 ⇒ 生效、留痕、closed 按新主语重核 ──
        #    场景即 #268 的实测事故：错记成 oekit/matrix-tools#194（实为 PR）
        #    ⇒ 现在有通道把它改成真主语，不用再手改 JSON。
        led30 = tmp / "amend_subject.json"
        led30.write_text(json.dumps({
            "runs": [{"started_at": "2026-11-02T00:00:00Z", "finished_at": None,
                      "handled": [{"issue": "oekit/demo#90", "org": "oekit",
                                   "closed": True, "note": "错记的主语"}],
                      "also_done": [], "skip": []}], "last_run": "2026-11-02T00:00:00Z"},
            ensure_ascii=False), encoding="utf-8")
        env30 = dict(base_env)
        env30["FAKE_ISSUE_STATE"] = "OPEN"    # 新主语还 OPEN ⇒ closed 必须重核成 False
        r = run_tool(["--issue", "oekit/demo#90", "--amend-issue", "oagentkit/demo#91"],
                     env30, led30)
        if r.returncode != 0:
            failures.append(f"用例30 --amend-issue 失败：{r.stdout}{r.stderr}")
        else:
            e30 = json.loads(led30.read_text(encoding="utf-8"))["runs"][0]["handled"][0]
            if e30.get("issue") != "oagentkit/demo#91" or e30.get("org") != "oagentkit":
                failures.append(f"用例30 主语没换：{e30.get('issue')!r}/{e30.get('org')!r}")
            elif e30.get("closed") is not False:
                failures.append(f"用例30 closed 没按新主语重核：{e30.get('closed')!r}")
            elif not (e30.get("amended_at") and e30.get("amended_from")):
                failures.append(f"用例30 换主语没留痕：{e30}")
            else:
                print("  ✓ 用例30 --amend-issue 换主语 ⇒ 生效、closed 重核、留痕")
        # 新主语是 PR ⇒ 拒绝（防止借修正通道把 PR 再塞回来）
        before30b = led30.read_text(encoding="utf-8")
        env30b = dict(base_env)
        env30b["FAKE_ISSUE_IS_PR"] = "true"
        env30b["FAKE_ISSUE_STATE"] = "MERGED"
        r = run_tool(["--issue", "oagentkit/demo#91", "--amend-issue", "oekit/demo#99"],
                     env30b, led30)
        if r.returncode == 0:
            failures.append("用例30b --amend-issue 把主语换成了 PR ⇒ 原始事故借道复现")
        elif led30.read_text(encoding="utf-8") != before30b:
            failures.append("用例30b 拒绝换主语却把台账改了")
        else:
            print("  ✓ 用例30b --amend-issue 目标是 PR ⇒ 拒绝且台账未被改")

        # ── 用例 31：收口形态自查——键 org ≠ org 字段 ⇒ 漂移 ─────────────
        led31 = tmp / "runend_org_mismatch.json"
        led31.write_text(json.dumps({
            "runs": [{"started_at": "2026-11-02T00:00:00Z", "finished_at": None,
                      "handled": [{"issue": "oagentkit/demo#92", "org": "oekit",
                                   "closed": False}],
                      "also_done": [], "skip": []}], "last_run": "2026-11-02T00:00:00Z"},
            ensure_ascii=False), encoding="utf-8")
        r = run_tool(["--run-end"], base_env, led31)
        if r.returncode == 0:
            failures.append("用例31 键 org 与 org 字段不一致却收口成功 ⇒ #268 的静默落账照样判绿")
        elif "主语存疑" not in (r.stdout + r.stderr):
            failures.append(f"用例31 收口没指出主语存疑：{r.stdout}{r.stderr}")
        else:
            print("  ✓ 用例31 收口复核：键 org ≠ org 字段 ⇒ 报主语存疑")
        # 旧形态键（repo#N + org 字段）⇒ 必须照常复核通过（向后兼容不误报）
        led31b = tmp / "runend_oldkey.json"
        led31b.write_text(json.dumps({
            "runs": [{"started_at": "2026-11-02T00:00:00Z", "finished_at": None,
                      "handled": [{"issue": "demo#93", "org": "oekit", "closed": False}],
                      "also_done": [], "skip": []}], "last_run": "2026-11-02T00:00:00Z"},
            ensure_ascii=False), encoding="utf-8")
        r = run_tool(["--run-end"], base_env, led31b)
        if r.returncode != 0:
            failures.append(f"用例31b 旧形态键（repo#N）收口失败（不该误报）：{r.stdout}{r.stderr}")
        else:
            print("  ✓ 用例31b 旧形态键（repo#N + org 字段）⇒ 照常复核通过")

        # ── 用例 32：收口形态自查——主语实为 PR ⇒ 漂移 ────────────────────
        led32 = tmp / "runend_pr_subject.json"
        led32.write_text(json.dumps({
            "runs": [{"started_at": "2026-11-02T00:00:00Z", "finished_at": None,
                      "handled": [{"issue": "oekit/demo#94", "org": "oekit",
                                   "closed": True}],
                      "also_done": [], "skip": []}], "last_run": "2026-11-02T00:00:00Z"},
            ensure_ascii=False), encoding="utf-8")
        env32 = dict(base_env)
        env32["FAKE_ISSUE_IS_PR"] = "true"
        env32["FAKE_ISSUE_STATE"] = "MERGED"
        r = run_tool(["--run-end"], env32, led32)
        if r.returncode == 0:
            failures.append("用例32 主语是 PR 却收口成功 ⇒ 历史存量 PR 记录永远漏检")
        elif "主语是" not in (r.stdout + r.stderr):
            failures.append(f"用例32 收口没指出主语是 PR：{r.stdout}{r.stderr}")
        else:
            print("  ✓ 用例32 收口复核：主语实为 PR ⇒ 报漂移")

    if failures:
        print("\n✗ 自测失败：")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("\n✓ 自测：写入器在事实不符时**拒绝写入**、收口能报出漂移、坏账不覆盖 ⇒ 断言有牙齿")
    return 0


# ═══ 变异自证（--mutate）══════════════════════════════════════════════════
#
# **为什么要有**：新增的封存保证 / 收口范围 / 修正通道都是**门禁式断言**——
# 一旦被改成恒放行，所有症状都消失（台账照样「收口成功」）⇒ 必须自证
# 「它抓得住自己要防的东西」。
#
# **怎么构造**：把**被测脚本的源码**改写成旧实现（真实退回旧行为，
# 不是 mock 掉判据）⇒ 跑一遍自测 ⇒ 比对「精确转红集合」。
#
# ⚠️ 期望集合必须**按该缺陷的实际影响面列全**（#223 的教训）：
#   同一个缺陷可能打红多条断言 ⇒ 少列会被判「不精确」，
#   但那不是判据没牙齿，是**期望集合没列全**。

_MUTATIONS = [
    # ① 封存保证退回旧写法：不查 finished_at ⇒ 往已收口轮次里追加也不拦
    #    （★ 影响面**两条**：issue 通道（用例 7）与 release-only 通道（用例 20）
    #      —— 封存保证现在走公共函数 `_append_entry` ⇒ 两处一起失效）
    ("封存保证退回旧写法（不查 finished_at）",
     ('if run.get("finished_at"):', 'if False:'),
     {"7", "20"}),
    # ② 收口范围退回旧写法：只收最新一轮 ⇒ 更早的未收口轮次永久悬空
    ("收口范围退回旧写法（只收最新一轮）",
     ('pending = [r for r in runs if not r.get("finished_at")]',
      'pending = [runs[-1]] if runs else []'),
     {"9"}),
    # ③ 修正通道退回旧写法：没有 amend ⇒ 错记录改不了（永久阻塞收口）
    #    （★ #268 之后影响面加一条：主语修正通道（用例 30）也走 cmd_amend）
    ("修正通道退回旧写法（amend 变成追加）",
     ('cmd_amend(args, data)', 'cmd_add_entry(args, data)'),
     {"10", "11", "12", "30"}),
    # ④ 「无 issue 锚点的发版」写入通道：Release 检查退掉（tag 推了就算已发版）
    ("release-only 写入通道的 Release 检查退掉",
     ('    if not release_exists(org, repo, tag):', '    if False:'),
     {"16"}),
    # ⑤ 收口复核跳过 release-only 记录 ⇒「记了没人核」＝白记
    #    （★ 影响面**两条**：正常收口（用例 19）与封存前收口（用例 20）都靠这条
    #      分支把 release-only 键送进专项复核——跳掉后键落到通用解析，
    #      #258 的形状校验会把「键形态不认识」显式报成漂移 ⇒ 两处一起红）
    ("收口复核跳过 release-only 记录",
     ('            if e.get("release_only") or is_release_only_key(issue):',
      '            if False:'),
     {"19", "20"}),
    # ⑥ ★★ **调用点级**变异：被测函数本身保持正确，**只把接线断了**
    #    （main 不再把 --release-only 接到写入通道）
    #    ⇒ 证明「默认接线真的被跑」⇒ 避免「自测只测了函数、接线早断了却全绿」
    ("main 不再把 --release-only 接到写入通道（调用点退化）",
     ('        if args.release_only:\n            cmd_add_release_entry(args, data)',
      '        if args.release_only:\n            pass  # ← 变异：调用点退化'),
     {"15", "16", "17", "18", "19", "20", "21", "23"}),
    # ⑦ 「键换形态」变异：release-only 键退化成别的形态 ⇒ 证明键控本身有牙齿
    ("release-only 键形态被改掉",
     ('RELEASE_ONLY_PREFIX = "（发版）"', 'RELEASE_ONLY_PREFIX = "（发版X）"'),
     {"15"}),
    # ⑧a ★ #258：org/repo#N 显式形态的解析被退掉（org 组不再匹配斜杠段）
    #    ⇒ 全键 org/repo#N 被拒成用法错误（#268 之后影响面列全：落库键是全键
    #      ⇒ 收口复核/修正通道解析全键全红——7/14/24（含全键的收口）、
    #      25/25b（形态支持）、30（换主语定位旧全键条目）、31/32（收口读全键））
    ("--issue 的 org/repo#N 显式形态被退掉",
     ('ISSUE_REF_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?"',
      'ISSUE_REF_RE = re.compile(r"^(?:())?"'),
     {"25", "25b", "30", "31", "32", "7", "14", "24"}),
    # ⑨ ★ #268：写入端「PR ≠ issue」拒绝被退掉 ⇒ PR 静默记成 issue（原始事故）
    ("写入端 PR-非-issue 拒绝被退掉",
     ('    state, is_pr = issue_facts(org, repo, num)\n    if is_pr:',
      '    state, is_pr = issue_facts(org, repo, num)\n    if False:'),
     {"28", "28b"}),
    # ⑩ ★ #268：--org 与串里 org 冲突的校验被退掉 ⇒ 静默按串里 org 落账
    ("--org 冲突校验被退掉",
     ('    if args.org != DEFAULT_ORG and org != args.org:',
      '    if False:'),
     {"29"}),
    # ⑪ ★ #268：收口端主语形态自查（PR/键-org 一致性）被退掉 ⇒ 历史存量漏检
    ("收口端键-org 一致性自查被退掉",
     ('            if e_org != org:', '            if False:'),
     {"31"}),
    ("收口端 PR-主语自查被退掉",
     ('            if is_pr or state == "MERGED":', '            if False:'),
     {"32"}),
    # ⑧b ★ #258：repo 段的字符集放宽到「# 前任意」（旧写法的表面查）⇒
    #    斜杠多段/含空格形态被放行后 rc=0 而非 2（26 红），amend 通道同款
    #    放行但落库键是垃圾（27 红）——正是「放行后报网络错误、整批少记」的根
    ("--issue 的 repo 段字符集退回表面查",
     ('                          r"([A-Za-z0-9][A-Za-z0-9._-]*)#(\\d+)$")',
      '                          r"([A-Za-z0-9][^#]*)#(\\d+)$")'),
     {"26", "27"}),
]


def _run_mutations() -> int:
    """注入变异体 ⇒ 比对「精确转红集合」。"""
    src = TOOL.read_text(encoding="utf-8")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for name, (old, new), expect in _MUTATIONS:
            if old not in src:
                print(f"✗ 变异「{name}」：源里找不到待替换片段 {old!r} ⇒ 变异体没生效")
                ok = False
                continue
            mutated = src.replace(old, new, 1)
            mtool = tmp / "ledger_write_mutated.py"
            mtool.write_text(mutated, encoding="utf-8")
            # 把被测脚本指到变异体，跑一遍自测（子进程，隔离）
            sel = Path(__file__).read_text(encoding="utf-8")
            sel_mut = sel.replace(
                'TOOL = ROOT / "scripts" / "ledger_write.py"',
                f'TOOL = Path(r"{mtool}")')
            sel_path = tmp / "selftest_mutated.py"
            sel_path.write_text(sel_mut, encoding="utf-8")
            env = dict(os.environ)
            env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'scripts'}"
            r = subprocess.run([sys.executable, str(sel_path)],
                               capture_output=True, text=True, env=env)
            out = r.stdout + r.stderr
            # 变异后应当**失败**（有断言转红）⇒ 逐条抽取失败项
            got: set[str] = set()
            for line in out.splitlines():
                s = line.strip()
                if s.startswith("- 用例"):
                    for tok in s.replace("：", " ").split():
                        if tok.startswith("用例"):
                            got.add(tok.replace("用例", "").rstrip("：,."))
            if not got:
                # 变异没让任何用例失败 ⇒ 判据没牙齿（或是变异体没生效）
                print(f"✗ 变异「{name}」**没有让任何用例失败** ⇒ 判据没牙齿"
                      f"（退出码 {r.returncode}）")
                ok = False
                continue
            if got == expect:
                print(f"✓ 变异「{name}」被抓住（精确转红 {sorted(got)}）")
            else:
                ok = False
                print(f"✗ 变异「{name}」转红集合**不精确** ⇒ "
                      f"期望 {sorted(expect)}，实际 {sorted(got)}")
    if ok:
        print(f"✓ 变异自证 {len(_MUTATIONS)}/{len(_MUTATIONS)} 全部**精确**转红")
        return 0
    print(f"✗ 变异自证未过：{len(_MUTATIONS)} 个变异体里至少一个转红集合不精确")
    return 1


if __name__ == "__main__":
    if "--mutate" in sys.argv:
        sys.exit(_run_mutations())
    sys.exit(main())
