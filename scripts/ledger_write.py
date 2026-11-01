#!/usr/bin/env python3
"""巡检台账（.oekit-cron-state.json）的写入器 —— **事实从 GitHub 回读**，不照抄执行者的说法。

为什么要有这个工具（2026-11-01 实测背景）：

1. **「记状态」这一环一直是手拼 JSON** ⇒ 形状会漂、字段会漏（键名靠记忆），
   而且每次手拼都可能写出与**现实不一致**的内容。
2. 更要命的是**台账与现实会分叉**：
   - 实测（2026-10-31）：台账记「本地分支 @ xxx，PR 未开」的 5 处，**实际早已合入 main**；
   - 实测（2026-11-01）：台账里「已关单」的说法与 `gh issue view` 的实际状态不一致。
   ⇒ 而下一轮的去重**读台账** ⇒ 分叉会直接变成「重复劳动 / 重复发版」。

⇒ 所以本工具的定位不是「帮写 JSON」，而是**写入前把事实从 GitHub 读回来，
与要记的内容对照；对不上就拒绝写入**（宁可台账缺一条，也不要台账有一条假的）。

用法（记一条）：
    python3 scripts/ledger_write.py --issue pkg#47 --pr 51 --tag v0.29.0 \\
        --kind Bug --priority P1 --closed --note "修复分页口径"

用法（开新一轮 / 收口）：
    python3 scripts/ledger_write.py --new-run
    python3 scripts/ledger_write.py --run-end --summary "本轮 2 个 issue"

退出码：
    0 成功；1 事实与要记的内容不符（**拒绝写入**）；2 用法/IO 错误。

环境：
    OEKIT_LEDGER  台账路径（默认 /Volumes/zy/oekit/.oekit-cron-state.json）
    --org         仓所属 org（默认 oekit；pigeonbox 系用 pigeonbox）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from matrix import ORG  # noqa: E402,F401  —— 事实层（org/仓库解析）只从这里取
import ghsafe  # noqa: E402  —— 远端读取层（把「读到空」与「事实为空」分开）

DEFAULT_LEDGER = Path(os.environ.get(
    "OEKIT_LEDGER", "/Volumes/zy/oekit/.oekit-cron-state.json"))

# 台账的形状（与 .oekit-cron-state.json 现有结构一致）
RUN_KEYS = ("started_at", "finished_at", "handled", "also_done", "skip")
ENTRY_KEYS = ("issue", "org", "kind", "priority", "pr", "merge_commit",
              "tag", "release", "closed", "board_status", "note")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Refused(Exception):
    """事实与要写的内容不符 ⇒ 拒绝写入（退出码 1）。"""


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"runs": []}
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"✗ 台账不是合法 JSON（{path}）：{e}\n"
                         f"  ⇒ **不覆盖**原文件（避免把可恢复的坏文件写成不可恢复的空账）")
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise SystemExit(f"✗ 台账结构不对（{path}）：缺 runs 数组 ⇒ 不覆盖原文件")
    return data


def save_ledger(path: Path, data: dict[str, Any]) -> None:
    """原子写：先写临时文件再 rename ⇒ 中途失败不会把台账写成半截。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def issue_state(org: str, repo: str, num: int) -> str:
    """issue 在 GitHub 上的真实状态（大写 OPEN/CLOSED）。"""
    data = ghsafe.gh_json(["issue", "view", str(num), "--repo", f"{org}/{repo}",
                           "--json", "state"])
    return str(data.get("state", "")).upper()


def pr_facts(org: str, repo: str, num: int) -> tuple[str, str]:
    """PR 的真实状态与合并 commit（sha）。"""
    data = ghsafe.gh_json(["pr", "view", str(num), "--repo", f"{org}/{repo}",
                           "--json", "state,mergeCommit"])
    state = str(data.get("state", "")).upper()
    sha = ""
    mc = data.get("mergeCommit") or {}
    if isinstance(mc, dict):
        sha = str(mc.get("oid", "") or "")
    return state, sha


def tag_exists(org: str, repo: str, tag: str) -> bool:
    """远端有没有这个 tag。

    ⚠️ 「读不到」与「不存在」必须分开（ghsafe 的语义）：
    404 ⇒ 确定不存在；其它非零退出码 ⇒ 抛异常 ⇒ **不**当成「不存在」。
    """
    try:
        ghsafe.gh_json(["api", f"repos/{org}/{repo}/git/refs/tags/{tag}"])
        return True
    except ghsafe.GhCommandError as e:
        detail = f"{e.stderr}\n{e.stdout}".lower()
        if "404" in detail or "not found" in detail:
            return False
        raise
    except ghsafe.GhReadError:
        # 读到了但正文为空/不可解析 ⇒ **不能**当成「不存在」（这是门禁静默放行的老坑）
        raise Refused(f"读远端 tag {org}/{repo}@{tag} 时正文不可解析 ⇒ "
                      f"无法判定它是否存在（拒绝写入，别把「读不到」记成「没有」）")


def latest_run(data: dict[str, Any]) -> dict[str, Any] | None:
    runs = data.get("runs") or []
    return runs[-1] if runs else None


def cmd_new_run(data: dict[str, Any]) -> None:
    run = {"started_at": now_iso(), "finished_at": None, "handled": [], "also_done": [], "skip": []}
    data.setdefault("runs", []).append(run)
    data["last_run"] = run["started_at"]
    print(f"→ 新一轮台账记录：started_at={run['started_at']}")


def cmd_add_entry(args, data: dict[str, Any]) -> None:
    org = args.org
    repo, _, num_s = args.issue.partition("#")
    if not repo or not num_s.isdigit():
        raise SystemExit(f"✗ --issue 格式应为 repo#N，收到：{args.issue}")
    num = int(num_s)

    # ── 事实回读（写入的字段来自这里，不是来自命令行参数）──
    state = issue_state(org, repo, num)

    merge_commit = ""
    if args.pr:
        pr_state, merge_commit = pr_facts(org, repo, args.pr)
        if pr_state != "MERGED":
            raise Refused(f"PR {org}/{repo}#{args.pr} 当前状态是 {pr_state or '未知'}，"
                          f"**不是 MERGED** ⇒ 拒绝记成「已交付」")
    if args.tag and not tag_exists(org, repo, args.tag):
        raise Refused(f"远端没有 tag {org}/{repo}@{args.tag} ⇒ 拒绝写入「已发版」")
    if args.closed and state != "CLOSED":
        # `gh issue view <n>` 对 **PR 号**会返回 PR 的状态（MERGED/OPEN）⇒
        # 明确说清「这是 PR 不是 issue」，否则措辞会让人以为是状态读错了。
        if state == "MERGED":
            raise Refused(f"{org}/{repo}#{num} 是 **PR**（状态 MERGED），不是 issue ⇒ "
                          f"不能记成「已关单」")
        raise Refused(f"issue {org}/{repo}#{num} 在 GitHub 上还是 {state or '未知'}，"
                      f"**没关** ⇒ 拒绝记成「已关单」")

    release = (f"https://github.com/{org}/{repo}/releases/tag/{args.tag}"
               if args.tag else None)
    entry = {
        "issue": args.issue, "org": org, "kind": args.kind, "priority": args.priority,
        "pr": args.pr, "merge_commit": merge_commit or None,
        "tag": args.tag, "release": release,
        "closed": bool(args.closed) or state == "CLOSED",
        "board_status": args.board, "note": args.note or "",
    }

    run = latest_run(data)
    if run is None:
        run = {"started_at": now_iso(), "finished_at": None, "handled": [], "also_done": [], "skip": []}
        data.setdefault("runs", []).append(run)
    run.setdefault("handled", []).append(entry)
    data["last_run"] = run.get("started_at")
    print(f"✓ 已记入：{args.issue}  issue状态={state}"
          + (f"  PR#{args.pr}({merge_commit[:8]})" if args.pr else "")
          + (f"  tag={args.tag}" if args.tag else "")
          + f"  closed={entry['closed']}")


def cmd_run_end(args, data: dict[str, Any]) -> None:
    """收口：把最新一轮的每条事实**再回读一遍** ⇒ 记录期间现实变了就报出来。"""
    run = latest_run(data)
    if run is None:
        raise SystemExit("✗ 台账里没有任何一轮记录 ⇒ 无从收口")
    drift: list[str] = []
    for e in run.get("handled", []):
        org = e.get("org", "oekit")
        issue = str(e.get("issue", ""))
        repo, _, num_s = issue.partition("#")
        if not num_s.isdigit():
            continue
        num = int(num_s)
        try:
            state = issue_state(org, repo, num)
        except Exception as e:  # noqa: BLE001 —— 读不到就报「无法判定」，不猜
            drift.append(f"{issue}：issue 状态读不到（{e}）⇒ 无法判定")
            continue
        if e.get("closed") and state != "CLOSED":
            drift.append(f"{issue}：记录里写了 closed，但 GitHub 上是 {state}")
        pr = e.get("pr")
        if pr:
            try:
                pr_state, _sha = pr_facts(org, repo, int(pr))
            except Exception as ex:  # noqa: BLE001
                drift.append(f"{issue}：PR#{pr} 读不到（{ex}）⇒ 无法判定")
                continue
            if pr_state != "MERGED":
                drift.append(f"{issue}：记录里写了 PR#{pr} 已合，但 GitHub 上是 {pr_state}")
        tag = e.get("tag")
        if tag:
            try:
                if not tag_exists(org, repo, tag):
                    drift.append(f"{issue}：记录里写了 tag {tag}，但远端查不到")
            except Exception as ex:  # noqa: BLE001
                drift.append(f"{issue}：tag {tag} 无法判定（{ex}）")
    run["finished_at"] = now_iso()
    data["last_run"] = run["started_at"]
    if drift:
        print("✗ 收口复核发现**漂移**（台账记录与现实不一致 ⇒ 台账已记，但内容不可信）：")
        for d in drift:
            print(f"    - {d}")
        raise SystemExit(1)
    print(f"✓ 本轮已收口：finished_at={run['finished_at']}（{len(run.get('handled', []))} 条记录，事实全部复核通过）")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="巡检台账写入器（事实从 GitHub 回读）")
    p.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="台账路径")
    p.add_argument("--org", default="oekit", help="仓所属 org（oekit / pigeonbox）")
    p.add_argument("--new-run", action="store_true", help="开新一轮记录")
    p.add_argument("--run-end", action="store_true", help="收口最新一轮（并复核每条事实）")
    p.add_argument("--issue", help="repo#N")
    p.add_argument("--pr", type=int, help="PR 号（会回读并要求已合并）")
    p.add_argument("--tag", help="tag（会回读远端，要求存在）")
    p.add_argument("--kind", default="Chore", help="Bug/Enhancement/Refactor/Docs/Chore")
    p.add_argument("--priority", default="P2", help="P0/P1/P2")
    p.add_argument("--board", default=None, help="看板状态（Done / None）")
    p.add_argument("--closed", action="store_true", help="声明已关单（会回读并要求确实关了）")
    p.add_argument("--note", default="", help="备注")
    args = p.parse_args(argv)

    if not any([args.new_run, args.run_end, args.issue]):
        p.error("至少给一个动作：--new-run / --run-end / --issue")

    path = Path(args.ledger)
    data = load_ledger(path)
    try:
        if args.new_run:
            cmd_new_run(data)
        if args.issue:
            cmd_add_entry(args, data)
        if args.run_end:
            cmd_run_end(args, data)
    except Refused as e:
        print(f"✗ 拒绝写入：{e}")
        return 1
    except ghsafe.GhCommandError as e:
        print(f"✗ 读 GitHub 失败（{e}）⇒ 事实不可知 ⇒ 不写入")
        return 1
    except ghsafe.GhReadError as e:
        print(f"✗ 读 GitHub 失败（{e}）⇒ 事实不可知 ⇒ 不写入")
        return 1

    save_ledger(path, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
