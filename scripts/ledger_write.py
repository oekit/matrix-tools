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

`--issue` 两种**显式**形态（matrix-tools#258）：
    repo#N       org 取 --org（默认 oekit）——**兼容旧调用**；落库键仍是全键
    org/repo#N   串里显式给了 org ⇒ 覆盖 --org（推荐形态，#268）
**落库键是全键 `org/repo#N`**（matrix-tools#268）：「记的是哪个 org」必须在
形态里可见——org 只能靠 --org 外部传时，记错 org 的同号 issue 会静默落账、
复核还判它「一致」（实测：oagentkit/matrix-tools#194 被记成 oekit/matrix-tools#194
——后者是个已合并 PR，账上凭空多出一条）。旧台账里的 `repo#N` 键仍可读
（复核时 org 回退到条目自身的 org 字段）。

**PR 与 issue 显式区分**（#268）：记入时读 REST issue 端点的 `pull_request`
键（#308：`gh issue view --json isPullRequest` 在 gh 2.102.0 是字段级确定性
错误 ⇒ 全量 fail-closed），是 PR 一律拒绝并说清
（要记交付请把号填进 --pr 字段，别冒充 issue 主语）。

`--org` 显式给了且与串里的 org 冲突 ⇒ **用法错误**（退出码 2），不静默猜。
其余形态（如 `a/b/c#1`、`repo#`、`#5`）⇒ **用法错误**（退出码 2、零写入），
错误信息说人话——旧实现只查表面，`org/repo#N` 被放行后撞「gh issue view
--repo oekit/oekit/matrix-tools」报成网络错误 ⇒ 19 条整批少记。

用法（开新一轮 / 收口）：
    python3 scripts/ledger_write.py --new-run
    python3 scripts/ledger_write.py --run-end --summary "本轮 2 个 issue"
`--summary`（matrix-tools#257）：收口时把摘要写进该轮的 `summary` 字段
（docstring 里的示例命令此前只是文档——argparse 没有这个参数 ⇒ 照文档敲
直接 rc=2、台账零字节 ⇒ 轮次永远停在未收口状态）。

退出码：
    0 成功；1 事实与要记的内容不符（**拒绝写入**）；2 用法/IO 错误。

环境：
    OEKIT_LEDGER  台账路径（默认 /Volumes/zy/oekit/.oekit-cron-state.json）
    --org         兜底 org（--issue 省略 org 段时用；默认 oekit；已知 org：
                  oekit / pigeonbox / oagentkit——推荐直接写全键 org/repo#N）
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
DEFAULT_ORG = "oekit"   # --org 的默认值；「显式给了 --org」= args.org != DEFAULT_ORG

# 台账的形状（与 .oekit-cron-state.json 现有结构一致；summary 是 --run-end
# --summary 写入的可选键，matrix-tools#257）
RUN_KEYS = ("started_at", "finished_at", "handled", "also_done", "skip", "summary")
ENTRY_KEYS = ("issue", "org", "kind", "priority", "pr", "merge_commit",
              "tag", "release", "closed", "board_status", "note")

# ── --issue 的形状校验（matrix-tools#258）────────────────────────────────
#
# 旧实现只有一句 `partition("#")`：只保证「# 后面是数字」「# 前面非空」，
# 不保证前面真的是仓名 ⇒ `oekit/matrix-tools#254` 这种含斜杠形态被**原样**
# 当成 repo 传下去 ⇒ `gh issue view 254 --repo oekit/oekit/matrix-tools`
# 报「error connecting to oekit / check your internet connection」——
# **形态错被说成网络故障** ⇒ 19 条整批少记，排查方向还被带偏。
# repo 段允许**前导点**（matrix-tools#280）：`.github` 是 GitHub 的特殊仓名
# （治理仓 / 共享 workflow 仓），oekit/.github 与 oagentkit/.github 都真实存在
# ——旧正则要求 repo 首字符是字母/数字 ⇒ 这类仓的 issue 一笔都记不进台账。
# 防误收：`.` / `..` / `..x` 这类路径形态仍拒绝（那不是仓名）。
ISSUE_REF_RE = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9._-]*)/)?"
                          r"([A-Za-z0-9._-]+)#(\d+)$")


def parse_issue_ref(text: str, default_org: str) -> tuple[str, str, int]:
    """issue 引用 → (org, repo, num)。两种显式形态：

      repo#N      org 用 default_org（--org，**兼容旧调用**）；
      org/repo#N  串里显式给了 org ⇒ 覆盖 default_org（推荐形态，#268）。

    落库键由调用方取**全键** `org/repo#N`（#268）：org 只活在 --org 里时，
    记错 org 的同号 issue 会静默落账、复核还判「一致」。旧台账里的 `repo#N`
    键仍可读——复核方按条目自身的 org 字段解析。
    （#258 时代键统一成 repo#N 的理由是「复核拿 oekit/matrix-tools 当 repo
    会读不到」——那是复核没跟着键形态走的问题；复核已改为按全键解析，
    不再是键带 org 的理由。）

    其余形态 ⇒ ValueError（人话），调用方转成**用法错误**（退出码 2、零写入）
    ——参数非法 ≠「读 GitHub 失败 ⇒ 事实不可知」，不能进同一类重试。
    """
    m = ISSUE_REF_RE.match(str(text).strip())
    if not m:
        raise ValueError(
            f"--issue 形态应为 repo#N 或 org/repo#N（如 matrix-tools#254 或 "
            f"oekit/matrix-tools#254），收到：{text!r}。"
            f"repo 段只允许字母/数字/._-，org 至多一段（斜杠多段必是写错）——"
            f"org 不用拼进 repo（org 用 --org 或 org/repo#N 显式给）。")
    org = m.group(1) or default_org
    repo = m.group(2)
    # 路径形态守卫（#280）：允许前导点后，`.` / `..` / `..foo` 这类「那是路径
    # 不是仓名」的形态要拦下（报人话，退出码 2），只放行 `.github` 这类真实仓名。
    if repo in (".", "..") or repo.startswith(".."):
        raise ValueError(
            f"--issue 的 repo 段是路径形态（{repo!r}），不是仓名。"
            f"前导点只用于特殊仓名如 .github（matrix-tools#280）。收到：{text!r}")
    return org, repo, int(m.group(3))


def _entry_matches(e: dict[str, Any], org: str, repo: str, num: int) -> bool:
    """台账条目是否指向 (org, repo, num)——按解析后的主语比，不按键串（#268）。

    旧条目的键是 `repo#N`（org 在字段里）、新条目是全键 `org/repo#N`；
    按键串比对会让一半形态永远匹配不上。解析不出主语的条目（历史脏数据）
    不匹配——宁可「找不到记录」报错，也不拿错条目当修正目标。
    """
    try:
        e_org, e_repo, e_num = parse_issue_ref(str(e.get("issue", "")),
                                               str(e.get("org") or org))
    except ValueError:
        return False
    return e_org == org and e_repo == repo and e_num == num


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


def issue_facts(org: str, repo: str, num: int) -> tuple[str, bool]:
    """issue 在 GitHub 上的真实状态（大写 OPEN/CLOSED）与「是不是 PR」。

    ★ PR 与 issue 显式区分（matrix-tools#268）：旧行为只读 state ⇒ 把已合并的
    PR 静默记成 issue（且若恰逢 CLOSED 形态还会自动推断 closed=True，账上
    凭空多出「已关单」）。调用方据此拒绝「把 PR 记成 issue」。

    读法走 REST（#308）：`gh issue view --json isPullRequest` 在 gh 2.102.0
    是字段级**确定性**错误（`Unknown JSON field`，两个入口都没有该字段）⇒
    任何 `--issue` 记录 100% fail-closed 写不进。REST 的 issue 端点对 PR
    返回 `pull_request` 键（等价判据，且 REST 不吃 GraphQL 限流）。
    """
    data = ghsafe.gh_json(["api", f"repos/{org}/{repo}/issues/{num}"])
    return str(data.get("state", "")).upper(), "pull_request" in data


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


def release_exists(org: str, repo: str, tag: str) -> bool:
    """远端有没有这个 tag 对应的 GitHub Release。

    为什么单独一条（2026-11-03 实测）：「tag 已打」与「Release 已发布」是**两件事**
    —— oekit 的发版链要求两者都在（tag 推了但 Release 没建 ⇒ 消费方拿不到变更记录）。
    台账若只记 tag ⇒ 下一轮去重会以为「这条已发布」⇒ 而 Release 缺失这件事没人知道。
    ⇒ 无 issue 锚点的发版记录**必须**同时核到 Release。
    """
    try:
        ghsafe.gh_json(["release", "view", tag, "--repo", f"{org}/{repo}", "--json", "tagName"])
        return True
    except ghsafe.GhCommandError as e:
        detail = f"{e.stderr}\n{e.stdout}".lower()
        if "404" in detail or "not found" in detail:
            return False
        raise
    except ghsafe.GhReadError:
        raise Refused(f"读远端 Release {org}/{repo}@{tag} 时正文不可解析 ⇒ "
                      f"无法判定它是否存在（拒绝写入，别把「读不到」记成「没有」）")


# ── 「无 issue 锚点的发版」通道（2026-11-03 实测补的缺口）────────────────
#
# 形态：工具仓自身发版（matrix-tools v0.2.0）、umbrella 仓发版（oekit BOM v0.2.0）、
#       纯重构发版……这些发版**没有** issue 锚点 ⇒ 旧形态 `--issue repo#N`
#       强制要求 `#N` ⇒ **根本记不进去**。
# 实测：后台巡检的台账在 hertz v0.8.13 之后**断了 8 次发版**，其中 **5 次**
#       （hertz v0.9.0 / matrix-tools v0.1.58 / matrix-tools v0.2.0 /
#         oekit v0.1.81 / oekit v0.2.0）**无锚点** ⇒ 工具不支持 ⇒ 只能不记
#       ⇒ 台账与现实分叉（下一轮去重/复核看不见它们）。
RELEASE_ONLY_PREFIX = "（发版）"


def release_only_key(repo: str, tag: str, org: str | None = None) -> str:
    """无 issue 锚点发版的台账键（与真实 issue 键 `repo#N` **不同形态** ⇒ 不撞）。

    #268 同款修法：键里带 **org**（`（发版）org/repo@tag`）——org 只活在
    --org 里时，记错 org 的同仓同 tag 发版会静默落账、复核还判「一致」。
    org=None 兼容旧键形态（复核旧条目时用）。
    """
    if org:
        return f"{RELEASE_ONLY_PREFIX}{org}/{repo}@{tag}"
    return f"{RELEASE_ONLY_PREFIX}{repo}@{tag}"


def is_release_only_key(key: str) -> bool:
    """这条记录是不是「无 issue 锚点的发版」记录。"""
    return str(key).startswith(RELEASE_ONLY_PREFIX)


def _append_entry(entry: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """把条目追加到**最新一轮**（未收口）⇒ 封存保证（matrix-tools#211）。

    为什么抽成公共函数：新增的「无 issue 锚点的发版」通道也必须受**同一条**
    封存保证约束 ⇒ 两处各抄一遍判据 ⇒ 将来改一处忘一处 ⇒ 封存承诺失真。
    """
    run = latest_run(data)
    if run is None:
        run = {"started_at": now_iso(), "finished_at": None,
               "handled": [], "also_done": [], "skip": []}
        data.setdefault("runs", []).append(run)
    # ★ **封存保证**（matrix-tools#211）：已收口的轮次**不许再追加**。
    #   `--run-end` 的职责是「把这一轮每条事实**再回读一遍**」⇒ 收口之后追加的
    #   条目**从未经过那次复核** ⇒ 封存承诺失真。
    #   且轮次的 started_at/finished_at 早于它记录的事实 ⇒ 时间窗与事实不符。
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
    return run


def cmd_add_release_entry(args, data: dict[str, Any]) -> None:
    """记一条**没有 issue 锚点**的发版（工具仓 / umbrella 仓 / 纯重构）。

    为什么需要（实测 2026-11-03）：后台巡检的台账在 hertz v0.8.13 之后**断了 8 次发版**
    —— 其中 5 次**根本没有 issue 锚点** ⇒ 旧的 `--issue repo#N` 强制形态让它们
    **记不进去** ⇒ 台账与现实分叉。

    ⇒ 这里给一条**显式**通道 `--release-only --repo <repo> --tag <tag>`：
      ① 事实回读**不打折**：tag 必须存在 **且** GitHub Release 必须存在
         （tag 推了但 Release 没建 ⇒ 消费方拿不到变更记录 ⇒ 不能记成「已发版」）；
      ② 给了 `--pr` 则 PR 必须已合并；
      ③ 键用 `（发版）org/repo@tag`（#268：org 进键，记错 org 不再静默；
         旧形态 `（发版）repo@tag` 仍可读）⇒ 与真实 issue 键**不同形态、不撞**；
      ④ `cmd_run_end` 也**复核**这类键 ⇒ 否则「记了没人核」＝白记。
    """
    org = args.org
    repo = (args.repo or "").strip()
    tag = (args.tag or "").strip()
    if not repo:
        raise SystemExit("✗ --release-only 必须给 --repo <repo>（如 hertz / matrix-tools / oekit）")
    if not tag:
        raise SystemExit("✗ --release-only 必须给 --tag <tag>")
    if args.closed:
        raise SystemExit("✗ --release-only 不接受 --closed（这条记录没有 issue 可关）")

    key = release_only_key(repo, tag, org)
    # 去重按 (org, repo, tag) 三元组，**不按键串**（#268）：旧形态键
    # `（发版）repo@tag` 与新形态键 `（发版）org/repo@tag` 指向同一次发版，
    # 按键串比对会漏判 ⇒ 同一次发版记两条。
    for r in data.get("runs") or []:
        for e in r.get("handled") or []:
            if (e.get("org", org) == org and str(e.get("repo") or "") == repo
                    and str(e.get("tag") or "") == tag):
                raise Refused(f"台账里已经有 {org}/{repo}@{tag} 的发版记录"
                              f"（键 {e.get('issue')}）⇒ 拒绝重复写入"
                              f"（同一次发版只该有一条；要改它请用 --amend-*）")

    # ── 事实回读（写入的字段来自这里，不是来自命令行参数）──
    if not tag_exists(org, repo, tag):
        raise Refused(f"远端没有 tag {org}/{repo}@{tag} ⇒ 拒绝记成「已发版」")
    if not release_exists(org, repo, tag):
        raise Refused(f"远端没有 {org}/{repo}@{tag} 对应的 GitHub Release ⇒ "
                      f"拒绝记成「已发版」（tag 推了但 Release 没建 ⇒ "
                      f"消费方拿不到变更记录 ⇒ 不能记「已发布」）")
    merge_commit = ""
    if args.pr:
        pr_state, merge_commit = pr_facts(org, repo, args.pr)
        if pr_state != "MERGED":
            raise Refused(f"PR {org}/{repo}#{args.pr} 当前状态是 {pr_state or '未知'}，"
                          f"**不是 MERGED** ⇒ 拒绝记成「已交付」")

    entry = {
        "issue": key, "org": org, "repo": repo, "release_only": True,
        "kind": args.kind, "priority": args.priority,
        "pr": args.pr, "merge_commit": merge_commit or None,
        "tag": tag,
        "release": f"https://github.com/{org}/{repo}/releases/tag/{tag}",
        "closed": False, "board_status": args.board, "note": args.note or "",
        "fact_at": now_iso(),
    }
    _append_entry(entry, data)
    print(f"✓ 已记入（无 issue 锚点的发版）：{key}"
          + (f"  PR#{args.pr}({merge_commit[:8]})" if args.pr else "")
          + f"  tag={tag}  Release=已核到")


def _review_release_only(org: str, e: dict[str, Any], label: str) -> list[str]:
    """复核一条「无 issue 锚点的发版」记录 ⇒ 返回漂移列表（空 = 无漂移）。

    ★ 为什么要单独一条：这类记录**没有 issue** ⇒ 旧复核逻辑按 `repo#N` 解析
    键 ⇒ 解析不出数字 ⇒ **直接 continue 跳过** ⇒ 「记了没人核」＝白记。
    ⇒ 这里按记录里的 repo/tag 回读：tag 在不在、**Release 在不在**、PR 合没合。
    """
    drift: list[str] = []
    repo = str(e.get("repo") or "")
    tag = str(e.get("tag") or "")
    if not repo or not tag:
        drift.append(f"{label}：记录里缺 repo/tag ⇒ 无法复核（不能当成「没问题」）")
        return drift
    # 形态自查（matrix-tools#268）：键里显式带的 org/repo/tag 与条目字段不一致
    # ⇒ 主语存疑——旧复核只读条目字段回读远端，键与字段「各说各话」照样判绿
    # （记错 org 的同仓同 tag 发版就这样静默落账）。
    if is_release_only_key(label):
        k_repo, _, k_tag = label[len(RELEASE_ONLY_PREFIX):].partition("@")
        k_org: str | None = None
        if "/" in k_repo:
            k_org, k_repo = k_repo.split("/", 1)
        if k_org is not None and k_org != org:
            drift.append(f"{label}：键里 org（{k_org}）与条目 org 字段（{org}）不一致"
                         f"⇒ 主语存疑（#268 形态自查）")
        if k_repo and k_repo != repo:
            drift.append(f"{label}：键里 repo（{k_repo}）与条目 repo 字段（{repo}）不一致")
        if k_tag and k_tag != tag:
            drift.append(f"{label}：键里 tag（{k_tag}）与条目 tag 字段（{tag}）不一致")
    try:
        if not tag_exists(org, repo, tag):
            drift.append(f"{label}：记录里写了 tag {tag}，但远端查不到")
    except Exception as ex:  # noqa: BLE001 —— 读不到就报「无法判定」，不猜
        drift.append(f"{label}：tag {tag} 无法判定（{ex}）")
    try:
        rel_ok = release_exists(org, repo, tag)
    except Exception as ex:  # noqa: BLE001
        rel_ok = None
        drift.append(f"{label}：Release {tag} 无法判定（{ex}）")
    if rel_ok is False:
        drift.append(f"{label}：记录里写了 tag {tag} 的发版，但远端没有对应的 GitHub Release")
    pr = e.get("pr")
    if pr:
        try:
            pr_state, _sha = pr_facts(org, repo, int(pr))
        except Exception as ex:  # noqa: BLE001
            drift.append(f"{label}：PR#{pr} 读不到（{ex}）⇒ 无法判定")
        else:
            if pr_state != "MERGED":
                drift.append(f"{label}：记录里写了 PR#{pr} 已合，但 GitHub 上是 {pr_state}")
    return drift


def latest_run(data: dict[str, Any]) -> dict[str, Any] | None:
    runs = data.get("runs") or []
    return runs[-1] if runs else None


def cmd_new_run(data: dict[str, Any]) -> None:
    run = {"started_at": now_iso(), "finished_at": None, "handled": [], "also_done": [], "skip": []}
    data.setdefault("runs", []).append(run)
    data["last_run"] = run["started_at"]
    print(f"→ 新一轮台账记录：started_at={run['started_at']}")


def cmd_add_entry(args, data: dict[str, Any]) -> None:
    # 形状校验（matrix-tools#258）：repo#N / org/repo#N，其余 ⇒ 用法错误。
    # 解析失败发生在写任何东西之前 ⇒ 台账零写入（与「拒绝写入」同一纪律）。
    try:
        org, repo, num = parse_issue_ref(args.issue, args.org)
    except ValueError as e:
        print(f"✗ 用法错误：{e}")
        raise SystemExit(2) from None
    # org 一致性（matrix-tools#268）：--org 显式给了且与串里的 org 冲突 ⇒ 报错，
    # 不静默猜。--org 处于默认值时以串里的显式 org 为准（兼容旧调用）。
    if args.org != DEFAULT_ORG and org != args.org:
        print(f"✗ 用法错误：--issue 的 org（{org}）与 --org（{args.org}）不一致"
              f"⇒ 拒绝猜测（#268：主语必须显式且可核查）")
        raise SystemExit(2) from None
    # 落库键用**全键** org/repo#N（#268）：「记的是哪个 org」必须在形态里可见。
    key = f"{org}/{repo}#{num}"

    # ── 事实回读（写入的字段来自这里，不是来自命令行参数）──
    state, is_pr = issue_facts(org, repo, num)
    if is_pr:
        # PR 一律拒绝记成 issue（#268）：不管它 MERGED 还是 OPEN——
        # 「记一条 issue」与「记一条交付」是两件事，主语混用会让去重失效。
        raise Refused(f"{org}/{repo}#{num} 是 **PR**（REST 端点带 pull_request 键，"
                      f"状态 {state or '未知'}），不是 issue ⇒ 拒绝把 PR 记成 issue；"
                      f"要记交付请把号填进 --pr 字段（或用 --release-only 记发版）")

    merge_commit = ""
    if args.pr:
        pr_state, merge_commit = pr_facts(org, repo, args.pr)
        if pr_state != "MERGED":
            raise Refused(f"PR {org}/{repo}#{args.pr} 当前状态是 {pr_state or '未知'}，"
                          f"**不是 MERGED** ⇒ 拒绝记成「已交付」")
    if args.tag and not tag_exists(org, repo, args.tag):
        raise Refused(f"远端没有 tag {org}/{repo}@{args.tag} ⇒ 拒绝写入「已发版」")
    if args.closed and state != "CLOSED":
        raise Refused(f"issue {org}/{repo}#{num} 在 GitHub 上还是 {state or '未知'}，"
                      f"**没关** ⇒ 拒绝记成「已关单」")

    release = (f"https://github.com/{org}/{repo}/releases/tag/{args.tag}"
               if args.tag else None)
    entry = {
        "issue": key, "org": org, "kind": args.kind, "priority": args.priority,
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

    _append_entry(entry, data)
    print(f"✓ 已记入：{key}（org={org}）  issue状态={state}"
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
         / `board_status`），外加**主语修正** `--amend-issue`（#268 的第④条：
         错记的 org/repo#N——比如把 oagentkit 的 issue 记成了 oekit 同号的
         已合并 PR——要能改回来，否则只能手工改 JSON）；
      ② 修正**必须留下痕迹**（`amended_at` + `amended_from` 记下原值），
         封存轮次也不豁免（这是**修错**，不是**加条目**）；
      ③ 修正确实回读 GitHub 核对：改了 `closed`/`tag`/`pr` ⇒ 事实必须与现实
         一致，否则拒绝（事实不可知时拒绝写入，与本工具既有口径一致）；
         改主语 ⇒ 新主语必须是真实存在的 **issue**（不是 PR）。
    """
    target = args.issue
    org = args.org
    # ★ 「无 issue 锚点的发版」记录的键形态是 `（发版）[org/]repo@<tag>` ⇒ 修正
    #   通道也要认它（否则一条错的发版记录**永远**修不了 ⇒ 那一轮**永远**收不了口）。
    rel_only = is_release_only_key(target)
    tag_in_key = ""
    if rel_only:
        _body = target[len(RELEASE_ONLY_PREFIX):]
        _krepo, _, tag_in_key = _body.partition("@")
        if "/" in _krepo:
            org, repo = _krepo.split("/", 1)   # 键里显式带的 org 覆盖 --org（#268）
        else:
            repo = _krepo
        if not repo or not tag_in_key:
            raise SystemExit(f"✗ --issue 格式应为 repo#N 或 （发版）[org/]repo@tag，收到：{target}")
        num = None
        key = target
    else:
        # 形状校验同 cmd_add_entry（#258）：org/repo#N 显式形态解析出 org；
        # 其余非法形态 ⇒ 用法错误（退出码 2），**不进**「读 GitHub 失败 ⇒
        # 事实不可知」那一类。
        try:
            org, repo, num = parse_issue_ref(target, args.org)
        except ValueError as e:
            print(f"✗ 用法错误：{e}")
            raise SystemExit(2) from None
        key = f"{org}/{repo}#{num}"

    # 找到目标条目（跨轮次找，找不到就报错——不许凭印象改）。
    # 匹配按 (org, repo, num) 三元组而非键串（#268）：旧条目的键是 repo#N、
    # 新条目是 org/repo#N，按键串比对会让一半形态永远「找不到记录」。
    hits: list[tuple[dict, dict]] = []
    for run in data.get("runs") or []:
        for e in run.get("handled") or []:
            if rel_only:
                if (e.get("release_only") and e.get("org", org) == org
                        and str(e.get("repo") or "") == repo
                        and str(e.get("tag") or "") == tag_in_key):
                    hits.append((run, e))
            elif _entry_matches(e, org, repo, num):
                hits.append((run, e))
    if not hits:
        raise Refused(f"台账里没有 {org}/{repo}#{num if num is not None else tag_in_key}"
                      f" 的记录 ⇒ 无从修正")
    if len(hits) > 1:
        idxs = [i for i, (r, _e) in enumerate(hits)]
        raise Refused(f"台账里有 {len(hits)} 条 {org}/{repo}#{num} 的记录"
                      f"（第 {idxs} 条，按轮次顺序）⇒ 请先确认要改哪一条")

    _run, entry = hits[0]
    # 校验主体：换了主语 ⇒ 后面的 closed/tag/pr 事实核对新主语（旧主语的事实
    # 不外推到新主语）；没换 ⇒ 核对原主语。
    v_org, v_repo = org, repo

    # ① 只允许改事实类字段 + 主语修正通道（--amend-issue，#268）
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
    if not changes and not args.amend_issue:
        raise SystemExit("✗ 没有要修正的内容（用 --amend-closed / --amend-tag / "
                         "--amend-pr / --amend-note / --amend-board / "
                         "--amend-issue 指定）")

    # ③ 修正后的事实必须与现实一致（事实不可知 ⇒ 拒绝）
    if rel_only:
        # 无 issue 锚点 ⇒ 没有 issue 状态可核 ⇒ 只核 tag / Release / PR
        if "closed" in changes:
            raise SystemExit("✗ 无 issue 锚点的发版记录没有 closed 字段可改")
        if args.amend_issue:
            raise SystemExit("✗ 无 issue 锚点的发版记录没有 issue 主语可改"
                             "（要换成有锚点形态请删掉重记）")
        new_tag = changes.get("tag") or tag_in_key
        if not tag_exists(org, repo, new_tag):
            raise Refused(f"远端没有 tag {org}/{repo}@{new_tag} ⇒ 拒绝写入")
        if not release_exists(org, repo, new_tag):
            raise Refused(f"远端没有 {org}/{repo}@{new_tag} 对应的 GitHub Release ⇒ 拒绝写入")
        if "tag" in changes and changes["tag"] != tag_in_key:
            # 键由 repo+tag **派生** ⇒ 改 tag 必须同步改键，否则键与内容不符
            changes["issue"] = release_only_key(repo, changes["tag"],
                                                org if "/" in _krepo else None)
    else:
        if args.amend_issue:
            # 主语修正（#268 的第④条）：错记的 org/repo#N 要能改回来——
            # 旧实现不许改主语 ⇒ 错记只能手工改 JSON。新主语必须是真实存在的
            # **issue**（不是 PR——那是本单要防的原始事故）。
            try:
                new_org, new_repo, new_num = parse_issue_ref(args.amend_issue, args.org)
            except ValueError as e:
                print(f"✗ 用法错误：--amend-issue {e}")
                raise SystemExit(2) from None
            try:
                new_state, new_is_pr = issue_facts(new_org, new_repo, new_num)
            except Exception as e:  # noqa: BLE001 —— 读不到就拒绝，不猜
                raise Refused(f"--amend-issue 目标 {new_org}/{new_repo}#{new_num} "
                              f"读不到（{e}）⇒ 无法确认它是真实 issue ⇒ 拒绝改主语")
            if new_is_pr:
                raise Refused(f"--amend-issue 目标 {new_org}/{new_repo}#{new_num} "
                              f"是 **PR** 不是 issue ⇒ 拒绝改主语（#268）")
            changes["issue"] = f"{new_org}/{new_repo}#{new_num}"
            changes["org"] = new_org
            v_org, v_repo = new_org, new_repo
            # 改主语后 closed 要按新主语重核（旧主语的 closed 事实不外推）
            if "closed" not in changes:
                changes["closed"] = new_state == "CLOSED"
        else:
            state, _is_pr = issue_facts(org, repo, num)
            if "closed" in changes and changes["closed"] and state != "CLOSED":
                raise Refused(f"{org}/{repo}#{num} 在 GitHub 上是 {state or '未知'}，**没关** ⇒ "
                              f"拒绝把记录改成 closed=True（事实不可知时拒绝写入）")
            if "closed" in changes and not changes["closed"] and state == "CLOSED":
                raise Refused(f"{org}/{repo}#{num} 在 GitHub 上**确实已关**（{state}）⇒ "
                              f"拒绝把记录改成 closed=False（那会与现实相反）")
        if "tag" in changes and changes["tag"] and not tag_exists(v_org, v_repo, changes["tag"]):
            raise Refused(f"远端没有 tag {v_org}/{v_repo}@{changes['tag']} ⇒ 拒绝写入")
    if "pr" in changes and changes["pr"]:
        pr_state, merge_commit = pr_facts(v_org, v_repo, int(changes["pr"]))
        if pr_state != "MERGED":
            raise Refused(f"PR {v_org}/{v_repo}#{changes['pr']} 当前状态是 {pr_state or '未知'}，"
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
            # ★ 「无 issue 锚点的发版」记录也要**复核**（否则「记了没人核」＝白记）：
            #   旧逻辑按 `repo#N` 解析键 ⇒ 解析不出数字 ⇒ 直接 continue 跳过。
            if e.get("release_only") or is_release_only_key(issue):
                drift.extend(_review_release_only(org, e, issue))
                continue
            # 键解析走 parse_issue_ref（#258 同一套）：台账键应为 repo#N；
            # 历史上若混进 org/repo#N 或别的形态 ⇒ 解析不出就**显式报漂移**
            # （旧写法 `isdigit()` 不匹配就静默 continue ⇒ 那条记录从未被复核
            #  ⇒「记了没人核」＝白记）。
            try:
                e_org, repo, num = parse_issue_ref(issue, org)
            except ValueError:
                drift.append(f"{issue}：台账键形态不认识（应为 repo#N 或 org/repo#N）"
                             f"⇒ 无法复核（不能当成「没问题」）")
                continue
            # 形态自查（matrix-tools#268）：全键里显式带的 org 与条目 org 字段
            # 不一致 ⇒ 主语存疑——这正是「记错 org 的同号 issue 静默落账」在
            # 账面上唯一能露头的缝（复核读的是同一个错主语，不查就判「一致」）。
            if e_org != org:
                drift.append(f"{issue}：键里 org（{e_org}）与条目 org 字段（{org}）"
                             f"不一致 ⇒ 主语存疑（#268 形态自查）")
                continue
            try:
                state, is_pr = issue_facts(e_org, repo, num)
            except Exception as e:  # noqa: BLE001 —— 读不到就报「无法判定」，不猜
                drift.append(f"{issue}：issue 状态读不到（{e}）⇒ 无法判定")
                continue
            # 形态自查（#268 的第⑤条）：主语是 PR（REST 带 pull_request 键或
            # 状态 MERGED）却被记成 issue ⇒ 判漂移——写入端已拒绝新犯，这里拦历史存量。
            if is_pr or state == "MERGED":
                drift.append(f"{issue}：主语是 **PR**（状态 {state or '未知'}）"
                             f"不是 issue ⇒ 台账把它记成了 issue（#268 形态自查）")
                continue
            if e.get("closed") and state != "CLOSED":
                drift.append(f"{issue}：记录里写了 closed，但 GitHub 上是 {state}")
            pr = e.get("pr")
            if pr:
                try:
                    pr_state, _sha = pr_facts(e_org, repo, int(pr))
                except Exception as ex:  # noqa: BLE001
                    drift.append(f"{issue}：PR#{pr} 读不到（{ex}）⇒ 无法判定")
                    continue
                if pr_state != "MERGED":
                    drift.append(f"{issue}：记录里写了 PR#{pr} 已合，但 GitHub 上是 {pr_state}")
            tag = e.get("tag")
            if tag:
                try:
                    if not tag_exists(e_org, repo, tag):
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
        # --summary（matrix-tools#257）：收口摘要写进该轮的 summary 字段——
        # docstring 里的示例命令此前只是文档（argparse 没有这个参数 ⇒ 照文档
        # 敲直接 rc=2、台账零字节 ⇒ 轮次永远停在未收口状态）。
        if getattr(args, "summary", None):
            run["summary"] = args.summary
        data["last_run"] = run["started_at"]
    if drift:
        print("✗ 收口复核发现**漂移**（台账记录与现实不一致 ⇒ 台账已记，但内容不可信）：")
        for d in drift:
            print(f"    - {d}")
        raise SystemExit(1)
    print(f"✓ 已收口 {len(pending)} 轮（finished_at={now}，"
          f"共 {sum(len(r.get('handled', [])) for r in pending)} 条记录，事实全部复核通过"
          + (f"；summary={args.summary!r}" if getattr(args, "summary", None) else "") + "）")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="巡检台账写入器（事实从 GitHub 回读）")
    p.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="台账路径")
    p.add_argument("--org", default=DEFAULT_ORG,
                   help="兜底 org（--issue 省略 org 段时用；默认 oekit；已知 org："
                        "oekit / pigeonbox / oagentkit——推荐 --issue 直接写全键 org/repo#N，#268）")
    p.add_argument("--new-run", action="store_true", help="开新一轮记录")
    p.add_argument("--run-end", action="store_true", help="收口最新一轮（并复核每条事实）")
    p.add_argument("--summary", default=None,
                   help="收口摘要（配合 --run-end，写进该轮 summary 字段；#257）")
    p.add_argument("--issue", help="repo#N 或 org/repo#N（推荐全键 org/repo#N，#268；"
                                   "省 org 段时 org 取 --org）")
    p.add_argument("--release-only", action="store_true",
                   help="记一次**无 issue 锚点**的发版（工具仓/umbrella 仓/纯重构）；配合 --repo/--tag")
    p.add_argument("--repo", help="仓名（配合 --release-only 用，如 hertz / matrix-tools / oekit）")
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
    p.add_argument("--amend-issue", dest="amend_issue", default=None,
                   help="修正记录的主语（repo#N 或 org/repo#N；新主语必须是真实 issue "
                        "不是 PR；#268 的修正通道）")
    args = p.parse_args(argv)

    if args.release_only and args.issue:
        p.error("--release-only 与 --issue 不能同时给（一条记录要么有 issue 锚点，要么没有）")
    if args.release_only and any(v is not None for v in (args.amend_closed, args.amend_tag,
                                                        args.amend_pr, args.amend_note,
                                                        args.amend_board, args.amend_issue)):
        p.error("--release-only 不支持 --amend-*（修正通道用 --issue 定位记录）")
    if not any([args.new_run, args.run_end, args.issue, args.release_only]):
        p.error("至少给一个动作：--new-run / --run-end / --issue / --release-only")
    if args.issue:
        # 形状校验前置（#258）：**用法错误在 argparse 阶段就以退出码 2 报出**、
        # 台账零写入——旧实现把 `org/repo#N` 放行到 gh 调用之后，报成「网络
        # 错误」（exit 1），调用方会把参数错误当基础设施故障去重试。
        if is_release_only_key(args.issue):
            pass  # release-only 键形态（（发版）<repo>@<tag>）由 cmd_amend 自己解析
        else:
            try:
                parse_issue_ref(args.issue, args.org)
            except ValueError as e:
                p.error(str(e))

    path = Path(args.ledger)
    data = load_ledger(path)
    try:
        if args.new_run:
            cmd_new_run(data)
        if args.issue:
            if any(v is not None for v in (args.amend_closed, args.amend_tag, args.amend_pr,
                                          args.amend_note, args.amend_board, args.amend_issue)):
                cmd_amend(args, data)
            else:
                cmd_add_entry(args, data)
        if args.release_only:
            cmd_add_release_entry(args, data)
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
