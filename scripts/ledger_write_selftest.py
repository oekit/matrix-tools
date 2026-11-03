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
    ("封存保证退回旧写法（不查 finished_at）",
     ('if run.get("finished_at"):', 'if False:'),
     {"7"}),
    # ② 收口范围退回旧写法：只收最新一轮 ⇒ 更早的未收口轮次永久悬空
    ("收口范围退回旧写法（只收最新一轮）",
     ('pending = [r for r in runs if not r.get("finished_at")]',
      'pending = [runs[-1]] if runs else []'),
     {"9"}),
    # ③ 修正通道退回旧写法：没有 amend ⇒ 错记录改不了（永久阻塞收口）
    ("修正通道退回旧写法（amend 变成追加）",
     ('cmd_amend(args, data)', 'cmd_add_entry(args, data)'),
     {"10", "11", "12"}),
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
