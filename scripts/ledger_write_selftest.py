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
    printf '{"state":"%s"}\\n' "${FAKE_ISSUE_STATE:-CLOSED}"
    ;;
  pr)
    if [ "${FAKE_PR_STATE:-MERGED}" = "MERGED" ]; then
      printf '{"state":"MERGED","mergeCommit":{"oid":"%s"}}\\n' "${FAKE_PR_SHA:-1111111111111111111111111111111111111111}"
    else
      printf '{"state":"%s","mergeCommit":null}\\n' "${FAKE_PR_STATE:-OPEN}"
    fi
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
                if e.get("issue") != "pkg#47" or e.get("kind") != "Bug" or e.get("priority") != "P1":
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

    if failures:
        print("\n✗ 自测失败：")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("\n✓ 自测：写入器在事实不符时**拒绝写入**、收口能报出漂移、坏账不覆盖 ⇒ 断言有牙齿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
