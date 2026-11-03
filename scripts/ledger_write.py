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
        # ★ **事实时刻**（#211 的第三个面）：轮次的 started_at/finished_at 必须
        #   **不早于**它所记录事实的发生时刻 ⇒ 否则读者会把这一轮的工作时间算错
        #   （实测 2026-11-02：第 516 轮时间窗比它记录的 tag 发布时刻早 6 分半）。
        # ⇒ 写入时记下「这条事实被观察到的那一刻」，收口时据此核对时间窗自洽。
        "fact_at": now_iso(),
    }

    run = latest_run(data)
    if run is None:
        run = {"started_at": now_iso(), "finished_at": None, "handled": [], "also_done": [], "skip": []}
        data.setdefault("runs", []).append(run)
    # ★ **封存保证**（matrix-tools#211）：已收口的轮次**不许再追加**。
    #   `--run-end` 的职责是「把这一轮每条事实**再回读一遍**」⇒ 收口之后追加的
    #   条目**从未经过那次复核** ⇒ 封存承诺失真。
    #   且轮次的 started_at/finished_at 早于它记录的事实 ⇒ 时间窗与事实不符
    #   （实测 2026-11-02：第 516 轮时间窗比它记录的 tag 发布时刻早 6 分半）。
    #   ⇒ 取严：拒绝写入，提示先 `--new-run`（台账是审计线索，宁可多一条命令）。
    if run.get("finished_at"):
        raise Refused(
            f"最新一轮（started_at={run.get('started_at')}）**已收口**"
            f"（finished_at={run.get('finished_at')}）⇒ 拒绝往封存轮次里追加条目："
            f"收口时只复核过当时那批事实，之后追加的条目从未被复核，"
            f"且轮次时间窗会早于所记录事实。"
            f"⇒ 请先 `--new-run` 开新一轮再记。")
    run.setdefault("handled", []).append(entry)
    data["last_run"] = run.get("started_at")
    print(f"✓ 已记入：{args.issue}  issue状态={state}"
          + (f"  PR#{args.pr}({merge_commit[:8]})" if args.pr else "")
          + (f"  tag={args.tag}" if args.tag else "")
          + f"  closed={entry['closed']}")


def cmd_amend(args, data: dict[str, Any]) -> None:
    """**修正**一条已写入的记录（matrix-tools#211 的第三个面）。

    为什么需要（实测 2026-11-03）：台账**没有**修正能力 ⇒ 一条错记录
    （例如「已关单」但 issue 实际被别的会话关错了、或 tag 写错）
    ⇒ 收口复核会**永远**判红 ⇒ 该轮**永久无法收口** ⇒ 悬空轮次堆积。
    本轮真实事故：并发会话误关 issue ⇒ 台账回读成 `closed=True` 而现实是
    `closed=False` ⇒ 收口被漂移拦下 ⇒ 只能绕过工具手改台账 JSON。

    ⇒ 这里给一条**显式、受控**的修正通道：
      ① 修正的字段**只允许**事实类字段（`closed` / `tag` / `pr` / `note`
         / `board_status`）——**不许**改 `issue` / `org`（那会换掉记录的主语）；
      ② 修正**必须留下痕迹**（`amended_at` + `amended_from` 记下原值），
         封存轮次也不豁免（这是**修错**，不是**加条目**）；
      ③ 修正确实回读 GitHub 核对：改了 `closed`/`tag`/`pr` ⇒ 事实必须与现实
         一致，否则拒绝（事实不可知时拒绝写入，与本工具既有口径一致）。
    """
    target = args.issue
    repo, _, num_s = target.partition("#")
    if not repo or not num_s.isdigit():
        raise SystemExit(f"✗ --issue 格式应为 repo#N，收到：{target}")
    num = int(num_s)
    org = args.org

    # 找到目标条目（跨轮次找，找不到就报错——不许凭印象改）
    hits: list[tuple[dict, dict]] = []
    for run in data.get("runs") or []:
        for e in run.get("handled") or []:
            if e.get("issue") == target and e.get("org", org) == org:
                hits.append((run, e))
    if not hits:
        raise Refused(f"台账里没有 {org}/{target} 的记录 ⇒ 无从修正")
    if len(hits) > 1:
        idxs = [i for i, (r, _e) in enumerate(hits)]
        raise Refused(f"台账里有 {len(hits)} 条 {org}/{target} 的记录"
                      f"（第 {idxs} 条，按轮次顺序）⇒ 请先确认要改哪一条")

    _run, entry = hits[0]

    # ① 只允许改事实类字段
    allowed = ("closed", "tag", "pr", "note", "board_status")
    changes: dict[str, Any] = {}
    if args.amend_closed is not None:
        changes["closed"] = bool(args.amend_closed)
    if args.amend_tag is not None:
        changes["tag"] = args.amend_tag
    if args.amend_pr is not None:
        changes["pr"] = args.amend_pr
    if args.amend_note is not None:
        changes["note"] = args.amend_note
    if args.amend_board is not None:
        changes["board_status"] = args.amend_board
    illegal = set(changes) - set(allowed)
    if illegal:
        raise SystemExit(f"✗ 不许修改的字段：{sorted(illegal)}（只允许 {list(allowed)}）")
    if not changes:
        raise SystemExit("✗ 没有要修正的内容（用 --amend-closed / --amend-tag / "
                         "--amend-pr / --amend-note / --amend-board 指定）")

    # ③ 修正后的事实必须与现实一致（事实不可知 ⇒ 拒绝）
    state = issue_state(org, repo, num)
    if "closed" in changes and changes["closed"] and state != "CLOSED":
        raise Refused(f"{org}/{target} 在 GitHub 上是 {state or '未知'}，**没关** ⇒ "
                      f"拒绝把记录改成 closed=True（事实不可知时拒绝写入）")
    if "closed" in changes and not changes["closed"] and state == "CLOSED":
        raise Refused(f"{org}/{target} 在 GitHub 上**确实已关**（{state}）⇒ "
                      f"拒绝把记录改成 closed=False（那会与现实相反）")
    if "tag" in changes and changes["tag"] and not tag_exists(org, repo, changes["tag"]):
        raise Refused(f"远端没有 tag {org}/{repo}@{changes['tag']} ⇒ 拒绝写入")
    if "pr" in changes and changes["pr"]:
        pr_state, merge_commit = pr_facts(org, repo, int(changes["pr"]))
        if pr_state != "MERGED":
            raise Refused(f"PR {org}/{repo}#{changes['pr']} 当前状态是 {pr_state or '未知'}，"
                          f"**不是 MERGED** ⇒ 拒绝记成「已交付」")
        changes["merge_commit"] = merge_commit or None

    # ② 留痕：原值 + 修正时刻
    old = {k: entry.get(k) for k in changes}
    entry.update(changes)
    entry["amended_at"] = now_iso()
    entry["amended_from"] = old
    print(f"✓ 已修正 {org}/{target}：{old} → {changes}"
          f"（留痕 amended_at={entry['amended_at']}）")


def cmd_run_end(args, data: dict[str, Any]) -> None:
    """收口：把每条事实**再回读一遍** ⇒ 记录期间现实变了就报出来。

    ★ **收口范围**（matrix-tools#211 的第二个面）：旧实现只收口**最新一轮**
    （`latest_run` = `runs[-1]`）⇒ **更早的未收口轮次永久悬空**、其记录**从未复核**。
    实测（2026-11-03）：3 轮悬空（其中一轮是上一轮我自己开的）⇒ 收口只覆盖了
    最后一轮，其余记录从头到尾没人核过 ⇒ 台账里的「已收口」不是全量保证。

    ⇒ 现在收口**所有未收口轮次**（`finished_at` 为空者），逐轮复核；
      已收口的轮次**不动**（封存保证，见 `cmd_add_entry`）。
    """
    runs = data.get("runs") or []
    if not runs:
        raise SystemExit("✗ 台账里没有任何一轮记录 ⇒ 无从收口")
    pending = [r for r in runs if not r.get("finished_at")]
    if not pending:
        # 已全部收口 ⇒ **不是错误**：状态本来就是「干净的」。
        # （旧实现会重收最后一轮；新语义下「收口」= 收掉**未收口**的轮次
        #   ⇒ 没有未收口的轮次 ⇒ 无事可做 ⇒ 退出码 0，
        #   保持「重复跑 --run-end」不破坏调用方。）
        print(f"✓ 没有未收口的轮次（{len(runs)} 轮全部已收口）⇒ 无需收口")
        return
    drift: list[str] = []
    for run in pending:
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
    # ★ **时间窗自洽**（#211 的第三个面）：轮次的 finished_at 必须**不早于**
    #   它所记录事实的发生时刻 ⇒ 否则读者会把这一轮的工作时间算错。
    #   实测（2026-11-02）：第 516 轮时间窗比它记录的 tag 发布时刻早 6 分半。
    #   ⇒ 收口时若发现**本轮**的 finished_at 早于条目事实时刻 ⇒ 显式报出来
    #     （不静默改历史：那会篡改「当时发生了什么」的记录）。
    now = now_iso()
    for run in pending:
        for e in run.get("handled", []):
            at = e.get("fact_at")
            if at and at > now:
                drift.append(f"{e.get('issue')}：条目记录的事实时刻 {at} **晚于**收口时刻 {now}"
                             f"（轮次时间窗与事实不自洽）")
    for run in pending:
        run["finished_at"] = now
        data["last_run"] = run["started_at"]
    if drift:
        print("✗ 收口复核发现**漂移**（台账记录与现实不一致 ⇒ 台账已记，但内容不可信）：")
        for d in drift:
            print(f"    - {d}")
        raise SystemExit(1)
    print(f"✓ 已收口 {len(pending)} 轮（finished_at={now}，"
          f"共 {sum(len(r.get('handled', [])) for r in pending)} 条记录，事实全部复核通过）")


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
    # ── --amend：修正一条已写入的记录（#211 的第三个面）──
    p.add_argument("--amend-closed", dest="amend_closed", action="store_true",
                   default=None, help="修正记录的 closed 字段（回读 GitHub 核对）")
    p.add_argument("--amend-open", dest="amend_closed", action="store_false",
                   help="修正记录的 closed 字段为 false")
    p.add_argument("--amend-tag", dest="amend_tag", default=None, help="修正记录的 tag")
    p.add_argument("--amend-pr", dest="amend_pr", type=int, default=None, help="修正记录的 PR 号")
    p.add_argument("--amend-note", dest="amend_note", default=None, help="修正记录的备注")
    p.add_argument("--amend-board", dest="amend_board", default=None, help="修正记录的看板状态")
    args = p.parse_args(argv)

    if not any([args.new_run, args.run_end, args.issue]):
        p.error("至少给一个动作：--new-run / --run-end / --issue")

    path = Path(args.ledger)
    data = load_ledger(path)
    try:
        if args.new_run:
            cmd_new_run(data)
        if args.issue:
            if any(v is not None for v in (args.amend_closed, args.amend_tag, args.amend_pr,
                                          args.amend_note, args.amend_board)):
                cmd_amend(args, data)
            else:
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
