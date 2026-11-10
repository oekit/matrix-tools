#!/usr/bin/env python3
"""issue 存量扫描器：org 全仓 open issue 一条命令盘点 + 分类战报（**只读，绝不关单**）。

用法：
  python3 scripts/issue_sweep.py                          # org 默认取 matrix.ORG
  python3 scripts/issue_sweep.py --org oagentkit --out .issue-sweep
  python3 scripts/issue_sweep.py --org oekit --repo core,hlog
  python3 scripts/issue_sweep.py --no-timeline            # 跳过逐单 timeline（快）
  python3 scripts/issue_sweep.py --stale-days 30

它把 2026-11 三轮手工「issue 清零战」（oekit 2026-11-08 第三轮 36 单、
yi-nology 2026-11-08 第二轮、oagentkit 2026-11-10 双轮 45 单）的**开场盘点**
固化成程序。每轮手工盘点都踩同一批坑，本脚本是这些坑的程序化总结：

1. **gh search 首轮漏单 ⇒ 双路枚举 + 逐单复核定胜负**（oekit 第三轮实测）：
   `gh search` 的搜索索引有分片延迟，单靠它扫存量会漏单；手工补救是
   「按 createdAt 重排复核」。程序化 ⇒ 路径 A（REST 全仓分页列表）与
   路径 B（`gh search issues` 三种排序取并集）各自全量枚举，差集里的每一条
   都用单 issue 读（`GET /repos/{o}/{r}/issues/{n}`）定胜负：
   404 ⇒ 幽灵单（已关/已删，路径 B 的索引脏数据）；200 ⇒ 真单，标 `search-only`
   （仅 search 可见——列表路漏了它）。
   **任何一路说了不算，单条读才算数。**

2. **REST 全程，不碰 GraphQL**（oagentkit 2026-11-10 实测）：
   大批量扫描下 GraphQL 限速先死（TLS 层限流要等恢复）；本脚本只用
   REST + `gh search`，单 issue 维度一次 timeline 一条读，天然稀疏。

3. **REST issue 列表混着 PR ⇒ 按 `pull_request` 键剔除**（close_issue.py 同源教训）：
   `GET /repos/{o}/{r}/issues` 的返回**包含 PR**；把 PR 当 issue 计数，
   盘点就虚高。剔除后与仓库元数据的 `open_issues_count`（含 PR）对账，
   `collected + prs_seen == expected` 不成立要明说，不许静默。

4. **空页 ≠ 终点**（ghsafe 家族教训，2026-10-22 实测单次读 10 次 2 次空）：
   分页循环里第 N 页读到空，可能是「到头了」也可能是**偶发空**——后者会
   截断枚举 ⇒ 漏单。规则：上一页是满页 ⇒ 本页空按偶发空处理（重试 N 轮）；
   上一页本就不满 ⇒ 空才是真终点（不浪费轮次）。

5. **重报单先聚类再动手**（oekit 第三轮实测「审计 issue 近半是重报单」）：
   巡检类 issue（audit drift / maintenance 提醒）每周重开，标题只差数字。
   归一化标题（数字→`N`、标点→空格、小写）后按 token Jaccard ≥ 阈值聚类，
   最早单为 canonical，其余标 `re-report`——**动手前先看 canonical**，
   不然修 5 遍同一个根因。

6. **只读，关单不归它管**：关单必须带回执且「回执真落地」判据在
   `close_issue.py`（单一职责）。本脚本产出**工单清单**，清零动作用
   close_issue.py 执行。任何 issue 在这里都不会被 close / comment / label。

分级（清零顺序，优先级从高到低，单条 issue 只进一级）：
  merged-pr   疑似修复已合入但单未关（timeline 有 cross-referenced 的已合入 PR——
              2026-11-10 实测 oekit 41 条：修复 PR 标题/正文引用了单号但没用
              closing keyword，合入后单静默开着；抽验确认后用 close_issue.py
              补回执关单，零成本清零）
  re-report   疑似重报单（同仓相似标题，指向 canonical）→ 先验证 canonical 再决定
  pr-open     已有 open PR 在修 → 等合入，别重复动手
  stale       超过 --stale-days（默认 90）无活动 → 先确认是否仍有效
  work        真实待修（工单主体，按 updated 龄升序）
配套完整性段：读失败的仓（读不到 ≠ 没有，#96 同款口径）、单数对账、
双路对账结论（search-miss / phantom / 截断）。

退出码：0 正常；2 有仓读失败（战报仍写盘，但调用方必须注意到有缺口）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghsafe  # noqa: E402
from ghsafe import GhCommandError, GhReadError  # noqa: E402

try:
    from matrix import ORG as DEFAULT_ORG  # noqa: E402
except Exception:  # pragma: no cover - 独立运行（非本仓 checkout）时兜底
    DEFAULT_ORG = "oekit"

PER_PAGE = 100
MAX_PAGES = 50  # 50*100=5000 单/仓的保险丝；真超了会在 integrity 里如实报截断

# 分级优先级：单条 issue 只进**最高**优先级的一级（先判 merged-pr，最后才是 work）。
BUCKET_ORDER = ["merged-pr", "re-report", "pr-open", "stale", "work"]
BUCKET_DESC = {
    "merged-pr": "疑似修复已合入但单未关（merged PR 引用了本单；抽验确认后用 close_issue.py 补回执关单）",
    "re-report": "疑似重报单（先验证 canonical 再动手）",
    "pr-open": "已有 open PR 在修（等合入）",
    "stale": "陈年无活动（先确认是否仍有效）",
    "work": "真实待修（工单主体）",
}


# ---------------------------------------------------------------- 时间工具


def age_days(iso: str) -> int:
    """GitHub ISO 时间（…Z）到今天的整天数。解析失败如实报 -1，不猜 0。"""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return -1
    return (datetime.now(timezone.utc) - dt).days


# ---------------------------------------------------------------- 数据模型


@dataclass
class PRRef:
    """issue timeline 里 cross-referenced 到的 PR（可能是别的仓的）。"""

    repo: str  # full_name（org/name）
    number: int
    merged: bool | None = None  # None = 没查到（--no-timeline / 读失败）
    state: str | None = None


@dataclass
class Issue:
    org: str
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    comments: int = 0
    labels: list[str] = field(default_factory=list)
    pr_refs: list[PRRef] = field(default_factory=list)
    timeline_error: bool = False
    bucket: str = "work"
    canonical_of: str | None = None  # re-report 指向的 canonical（repo#num）
    canonical_key: str | None = None  # 作为 canonical 时自身的 key
    re_report_of_sim: float | None = None
    cross_repo_dup: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # search-miss / phantom 等

    @property
    def key(self) -> str:
        return f"{self.org}/{self.repo}#{self.number}"


@dataclass
class RepoMeta:
    name: str
    open_issues_count: int = 0
    archived: bool = False
    private: bool = False
    pushed_at: str = ""


# ---------------------------------------------------------------- REST 读取层


def _api_json(path: str) -> Any:
    """单次 REST 读（瞬时错误已由 ghsafe 重试；空集合按「读不到」重试后报错）。

    「在不在」类查询不要开 allow_empty_collection——空正是要防的误判来源
    （ghsafe 模块口径）。
    """
    out, _err = ghsafe.run_gh(["api", path])
    body = out.strip()
    if not body:
        raise GhReadError(f"gh api {path} 正文为空")
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise GhReadError(f"gh api {path} 正文不可解析：{e}")


def _paginate(path_templ: str, *, max_pages: int = MAX_PAGES,
              empty_rounds: int = ghsafe.EMPTY_ROUNDS, empty_backoff: float = 0.4) -> tuple[list, bool]:
    """分页读列表，返回 (全部条目, 是否截断)。

    空页规则（见模块 docstring 第 4 条）：上一页满页 ⇒ 空按偶发空处理
    （read_until_nonempty 重试）；上一页不满 ⇒ 空才是真终点（不浪费轮次）。

    empty_rounds/empty_backoff 给「空是常态」的小端点（如 issue timeline——
    大多数 issue 没有任何 cross-reference）调低重试成本：默认 4 轮 0.4 起步的
    等待对百条 issue 的逐单 timeline 是分钟级灾难；3 轮 0.15 起步仍能把
    偶发空（实测率 ~20~30% 单轮）压到千分之一以下。
    """
    items: list = []
    page = 1
    prev_full = True
    while True:
        def read() -> list:
            data = _api_json(f"{path_templ}?per_page={PER_PAGE}&page={page}")
            if not isinstance(data, list):
                raise GhReadError(f"gh api {path_templ} page={page} 不是列表")
            return data

        data = read()
        if not data and prev_full:
            data = ghsafe.read_until_nonempty(read, rounds=empty_rounds,
                                              backoff=empty_backoff) or []
        if not data:
            break  # 上一页不满（或重试后仍空）⇒ 真终点
        items.extend(data)
        prev_full = len(data) == PER_PAGE
        if not prev_full or page >= max_pages:
            return items, page >= max_pages and prev_full
        page += 1
    return items, False


def list_repos(org: str, whitelist: set[str] | None) -> list[RepoMeta]:
    """org 全仓枚举；orgs 端点 404 ⇒ 回退 users 端点（个人命名空间）。"""
    try:
        raw, _trunc = _paginate(f"orgs/{org}/repos")
    except GhCommandError as e:
        if not e.is_not_found:
            raise
        raw, _trunc = _paginate(f"users/{org}/repos")
    repos: list[RepoMeta] = []
    for r in raw:
        name = r.get("name") or ""
        if not name:
            continue
        if whitelist is not None and name not in whitelist:
            continue
        repos.append(RepoMeta(
            name=name,
            open_issues_count=int(r.get("open_issues_count") or 0),
            archived=bool(r.get("archived")),
            private=bool(r.get("private")),
            pushed_at=r.get("pushed_at") or "",
        ))
    if whitelist is not None:
        missing = whitelist - {r.name for r in repos}
        if missing:
            raise GhReadError(f"白名单里的仓在 org {org} 下读不到：{sorted(missing)}")
    return sorted(repos, key=lambda r: r.name)


def list_open_issues(org: str, repo: str) -> tuple[list[Issue], int]:
    """路径 A：REST 全仓 open 列表（含 PR，按 `pull_request` 键剔除）。

    返回 (issues, prs_seen)。prs_seen 是列表里混进来的 PR 数——
    上层拿它与 open_issues_count 对账。
    """
    raw, _trunc = _paginate(f"repos/{org}/{repo}/issues")
    issues: list[Issue] = []
    prs = 0
    for r in raw:
        if r.get("pull_request"):
            prs += 1
            continue
        issues.append(Issue(
            org=org,
            repo=repo,
            number=int(r.get("number") or 0),
            title=r.get("title") or "",
            url=r.get("html_url") or "",
            created_at=r.get("created_at") or "",
            updated_at=r.get("updated_at") or "",
            comments=int(r.get("comments") or 0),
            labels=[l.get("name", "") for l in (r.get("labels") or []) if isinstance(l, dict)],
        ))
    return issues, prs


def search_open_issues(org: str, max_search: int) -> dict[str, dict]:
    """路径 B：`gh search issues` 三种排序取并集（搜索索引有分片延迟，
    单排序不可信——oekit 第三轮漏单教训）。返回 {key: issue-lite}。"""
    merged: dict[str, dict] = {}
    for sort_args in ([], ["--sort", "created", "--order", "desc"],
                      ["--sort", "created", "--order", "asc"]):
        args = ["search", "issues", "--owner", org, "--state", "open",
                "--limit", str(max_search), "--json",
                "repository,number,title,state,createdAt,updatedAt,url", *sort_args]
        out, _err = ghsafe.run_gh(args)
        body = out.strip()
        if not body:
            continue  # 某一路读到空不算失败，三路并集后看总量
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        for r in data if isinstance(data, list) else []:
            rep = r.get("repository") or {}
            full = rep.get("nameWithOwner") or ""
            if not full or "/" not in full:
                continue
            key = f"{full}#{r.get('number')}"
            if key not in merged:
                merged[key] = {
                    "repo_full": full, "number": r.get("number"),
                    "title": r.get("title") or "", "state": r.get("state") or "",
                    "createdAt": r.get("createdAt") or "", "updatedAt": r.get("updatedAt") or "",
                    "url": r.get("url") or "",
                }
    return merged


def verify_single(org: str, repo: str, number: int) -> Issue | None:
    """单 issue 读定胜负。404 ⇒ None（幽灵单/已关）；其它错误 fail loud。"""
    try:
        r = _api_json(f"repos/{org}/{repo}/issues/{number}")
    except GhCommandError as e:
        if e.is_not_found:
            return None
        raise
    if r.get("pull_request") or r.get("state") != "open":
        return None
    return Issue(
        org=org, repo=repo, number=int(r.get("number") or number),
        title=r.get("title") or "", url=r.get("html_url") or "",
        created_at=r.get("created_at") or "", updated_at=r.get("updated_at") or "",
        comments=int(r.get("comments") or 0),
        labels=[l.get("name", "") for l in (r.get("labels") or []) if isinstance(l, dict)],
    )


_pr_cache: dict[str, PRRef] = {}


def pr_ref(full_name: str, number: int) -> PRRef:
    """查 PR 的 merged/state（按 (repo,number) 缓存；读失败标 None，不猜）。"""
    ck = f"{full_name}#{number}"
    if ck in _pr_cache:
        return _pr_cache[ck]
    ref = PRRef(repo=full_name, number=number, merged=None, state=None)
    try:
        r = _api_json(f"repos/{full_name}/pulls/{number}")
        ref.merged = bool(r.get("merged"))
        ref.state = r.get("state") or None
    except GhCommandError as e:
        if not e.is_not_found:
            raise
    _pr_cache[ck] = ref
    return ref


def timeline_pr_refs(org: str, repo: str, number: int) -> tuple[list[PRRef], bool]:
    """issue timeline 里的 cross-referenced PR（含跨仓引用），返回 (refs, error)。"""
    try:
        events, _trunc = _paginate(f"repos/{org}/{repo}/issues/{number}/timeline",
                                   max_pages=3, empty_rounds=ghsafe.EMPTY_ROUNDS,
                                   empty_backoff=0.02)
    except (GhCommandError, GhReadError):
        return [], True
    refs: dict[str, PRRef] = {}
    for ev in events:
        if not isinstance(ev, dict) or ev.get("event") != "cross-referenced":
            continue
        src = (ev.get("source") or {}).get("issue") or {}
        if not src.get("pull_request"):
            continue
        src_repo = ((src.get("repository") or {}).get("full_name")) or f"{org}/{repo}"
        prn = src.get("number")
        if not prn:
            continue
        refs[f"{src_repo}#{prn}"] = pr_ref(src_repo, int(prn))
    return list(refs.values()), False


# ---------------------------------------------------------------- 聚类与分级


def norm_title(title: str) -> tuple[str, ...]:
    """标题归一化：数字→N（版本号/计数漂移）、标点→空格、小写、切 token。"""
    t = re.sub(r"\d+", "N", title.lower())
    t = re.sub(r"[^\w]+", " ", t)
    return tuple(w for w in t.split() if w)


def jaccard(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def cluster_re_reports(issues: list[Issue], threshold: float) -> None:
    """同仓内聚类重报单：最早单为 canonical，其余标 re-report。

    只在**同仓**内聚（巡检类 issue 是按仓重开的，跨仓同题是各自仓的巡检单，
    跨仓重合另行标 cross_repo_dup 参考信息，不进分级）。
    """
    by_repo: dict[str, list[Issue]] = {}
    for it in issues:
        by_repo.setdefault(it.repo, []).append(it)
    for repo_issues in by_repo.values():
        repo_issues.sort(key=lambda i: (i.created_at, i.number))
        clusters: list[tuple[tuple[str, ...], Issue]] = []
        for it in repo_issues:
            nt = norm_title(it.title)
            best_sim, best_issue = 0.0, None
            for cnt, canon in clusters:
                sim = jaccard(nt, cnt)
                if sim > best_sim:
                    best_sim, best_issue = sim, canon
            if best_issue is not None and best_sim >= threshold:
                it.bucket = "re-report"
                it.canonical_of = best_issue.key
                it.re_report_of_sim = round(best_sim, 2)
                best_issue.canonical_key = best_issue.key
            else:
                clusters.append((nt, it))
    # 跨仓同题（归一化后完全一致）只标参考信息
    seen: dict[tuple[str, ...], list[str]] = {}
    for it in sorted(issues, key=lambda i: (i.created_at, i.key)):
        nt = norm_title(it.title)
        if nt in seen and it.key not in seen[nt]:
            it.cross_repo_dup = seen[nt][:]
        seen.setdefault(nt, []).append(it.key)


def classify(it: Issue, stale_days: int) -> None:
    """单条分级：优先级从高到低，命中一级即停（见 BUCKET_ORDER）。"""
    if it.bucket == "re-report":  # 聚类已定（优先级高于 pr-open/stale/work）
        return
    if any(p.merged for p in it.pr_refs):
        it.bucket = "merged-pr"
    elif any(p.state == "open" for p in it.pr_refs):
        it.bucket = "pr-open"
    elif age_days(it.updated_at) >= stale_days:
        it.bucket = "stale"
    else:
        it.bucket = "work"


# ---------------------------------------------------------------- 战报


def render_md(res: "SweepResult") -> str:
    now = res.generated_at
    lines = [
        f"# issue 存量战报 · org={res.org} · {now}",
        "",
        f"- 仓：{len(res.repos)}（archived {sum(1 for r in res.repos if r.archived)}，"
        f"白名单 {len(res.whitelist) if res.whitelist else '全量'}）",
        f"- open issue 合计：**{len(res.issues)}**（列表混入 PR {res.prs_seen_total} 条已剔除；"
        f"timeline {'已读' if res.timeline else '跳过（--no-timeline）'}）",
        f"- 双路对账（REST 列表 × gh search 三排序并集）：{res.recon_summary()}",
        f"- 单数对账：collected+prs_seen == open_issues_count "
        f"{'✅ 全部成立' if not res.count_mismatch else '⚠️ ' + str(len(res.count_mismatch)) + ' 仓不成立（见 integrity）'}",
        f"- 读失败仓：{len(res.failed_repos)} 个"
        + (f"（{', '.join(res.failed_repos)}）" if res.failed_repos else ""),
        "",
        "## 分级（清零顺序）",
        "",
    ]
    for b in BUCKET_ORDER:
        items = [i for i in res.issues if i.bucket == b]
        lines.append(f"### {b}（{len(items)}）— {BUCKET_DESC[b]}")
        lines.append("")
        if not items:
            lines.append("（无）")
            lines.append("")
            continue
        lines.append("| issue | 标题 | 龄(天) | 指向/关联 |")
        lines.append("|---|---|---|---|")
        for i in sorted(items, key=lambda x: (x.repo, x.number)):
            ref = i.canonical_of or ""
            if b == "merged-pr":
                ref = "、".join(f"{p.repo}#{p.number}(merged)" for p in i.pr_refs if p.merged)
            elif b == "pr-open":
                ref = "、".join(f"{p.repo}#{p.number}" for p in i.pr_refs if p.state == "open")
            if i.cross_repo_dup:
                ref = (ref + " " if ref else "") + f"跨仓同题: {', '.join(i.cross_repo_dup)}"
            if i.flags:
                ref = (ref + " " if ref else "") + " ".join(i.flags)
            lines.append(
                f"| {i.key} | {i.title[:70]} | {age_days(i.updated_at)} | {ref} |")
        lines.append("")
    if res.failed_repos or res.count_mismatch or res.recon:
        lines += ["## integrity", "", "```json",
                  json.dumps(res.integrity_blob(), ensure_ascii=False, indent=2), "```", ""]
    lines += [f"JSON 工单：见同目录 .json 文件（bucket 字段即分级）。"
              f"清零动作一律走 `close_issue.py <repo>#<N> --tag …`（回执落地判据在那边）。", ""]
    return "\n".join(lines)


class SweepResult:
    def __init__(self) -> None:
        self.org = ""
        self.generated_at = ""
        self.repos: list[RepoMeta] = []
        self.issues: list[Issue] = []
        self.whitelist: set[str] | None = None
        self.prs_seen_total = 0
        self.timeline = True
        self.failed_repos: list[str] = []
        self.count_mismatch: list[dict] = []
        self.recon: dict = {}
        self.search_truncated = False

    def recon_summary(self) -> str:
        if self.recon.get("status") == "failed":
            return f"⚠️ search 路径失败（{self.recon.get('error')}），仅单路枚举"
        extra, phantom = self.recon.get("search_miss", 0), self.recon.get("phantom", 0)
        return (f"search 漏单 {extra} 条（已逐单复核采信）、幽灵单 {phantom} 条（404 已剔除）"
                + ("、search 结果截断于 --max-search" if self.search_truncated else ""))

    def integrity_blob(self) -> dict:
        return {
            "failed_repos": self.failed_repos,
            "count_mismatch": self.count_mismatch,
            "reconciliation": self.recon,
        }


def sweep(org: str, whitelist: set[str] | None, *, stale_days: int, threshold: float,
          do_timeline: bool, max_search: int, sleep_s: float, verbose: bool) -> SweepResult:
    res = SweepResult()
    res.org = org
    res.whitelist = whitelist
    res.timeline = do_timeline
    res.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not ghsafe.gh_authenticated():
        raise SystemExit("gh 未认证（gh auth status 失败）——扫描器拒绝匿名降级跑")

    # 1) 枚举仓
    res.repos = list_repos(org, whitelist)
    print(f"[sweep] org={org} 仓数={len(res.repos)}", file=sys.stderr)

    # 2) 路径 A：逐仓 REST 列表
    issues: list[Issue] = []
    for r in res.repos:
        try:
            got, prs = list_open_issues(org, r.name)
        except (GhCommandError, GhReadError) as e:
            res.failed_repos.append(r.name)
            print(f"[sweep]   {r.name}: 读失败 {e}", file=sys.stderr)
            continue
        res.prs_seen_total += prs
        collected = len(got) + prs
        if collected != r.open_issues_count:
            res.count_mismatch.append({
                "repo": r.name, "expected(open_issues_count)": r.open_issues_count,
                "collected": collected,
            })
        issues.extend(got)
        if verbose:
            print(f"[sweep]   {r.name}: {len(got)} issue + {prs} PR", file=sys.stderr)
        time.sleep(sleep_s)
    print(f"[sweep] 路径A：{len(issues)} issue / {res.prs_seen_total} PR 剔除 / "
          f"{len(res.failed_repos)} 仓读失败", file=sys.stderr)

    # 3) 路径 B：gh search 三排序并集 + 差集逐单复核
    by_key = {i.key: i for i in issues}
    try:
        found = search_open_issues(org, max_search)
        res.search_truncated = len(found) >= max_search
        miss, phantom = [], []
        for key, lite in found.items():
            # 白名单同样约束路径 B——否则白名单外的单会被逐条 verify 甚至收进战报
            if whitelist is not None:
                lname = lite["repo_full"].rsplit("/", 1)[-1]
                if lname not in whitelist:
                    continue
            if key not in by_key:
                full = lite["repo_full"]
                o, _, name = full.partition("/")
                single = verify_single(o, name, int(lite["number"] or 0))
                if single is not None:
                    single.flags.append("search-only")  # 仅 search 可见（列表路漏了）
                    issues.append(single)
                    by_key[single.key] = single
                else:
                    phantom.append(key)
        for key in by_key:
            if key not in found:
                miss.append(key)
        res.recon = {"status": "ok", "search_miss": len(miss), "phantom": len(phantom),
                     "search_miss_keys": miss, "phantom_keys": phantom}
        print(f"[sweep] 路径B：search 并集 {len(found)}；漏单 {len(miss)} / 幽灵 {len(phantom)}",
              file=sys.stderr)
    except (GhCommandError, GhReadError) as e:
        res.recon = {"status": "failed", "error": str(e)[:200]}
        print(f"[sweep] 路径B失败：{e}（仅单路枚举，战报如实标注）", file=sys.stderr)

    # 4) timeline 增强（--no-timeline 跳过；读失败的单不判 merged/pr-open，fail-open 到基础分级）
    if do_timeline:
        for idx, it in enumerate(issues):
            refs, err = timeline_pr_refs(it.org, it.repo, it.number)
            it.pr_refs = refs
            it.timeline_error = err
            if verbose and (refs or err):
                print(f"[sweep]   timeline {it.key}: {len(refs)} PR 引用"
                      + ("（读失败）" if err else ""), file=sys.stderr)
            time.sleep(sleep_s)
            if idx % 20 == 19:
                print(f"[sweep] timeline 进度 {idx + 1}/{len(issues)}", file=sys.stderr)

    # 5) 聚类 + 分级
    cluster_re_reports(issues, threshold)
    for it in issues:
        classify(it, stale_days)
    res.issues = sorted(issues, key=lambda i: (i.repo, i.number))
    return res


def print_summary(res: SweepResult) -> None:
    print(f"\n=== issue 存量战报 · org={res.org} · {res.generated_at} ===")
    print(f"仓 {len(res.repos)}（读失败 {len(res.failed_repos)}）· "
          f"open issue {len(res.issues)}（PR 剔除 {res.prs_seen_total}）")
    for b in BUCKET_ORDER:
        items = [i for i in res.issues if i.bucket == b]
        print(f"  {b:10s} {len(items):4d}  {BUCKET_DESC[b]}")
        for i in sorted(items, key=lambda x: (x.repo, x.number))[:5]:
            suffix = f" → {i.canonical_of}" if i.canonical_of else ""
            print(f"      {i.key} {i.title[:60]}{suffix}")
        if len(items) > 5:
            print(f"      … 其余 {len(items) - 5} 条见战报")
    print(f"对账：{res.recon_summary()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="issue 存量扫描器（只读）")
    ap.add_argument("--org", default=DEFAULT_ORG)
    ap.add_argument("--repo", default="", help="逗号分隔白名单（默认全仓）")
    ap.add_argument("--out", default="", help="战报写盘目录（默认只打印）")
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--similarity", type=float, default=0.55,
                    help="重报聚类 Jaccard 阈值（默认 0.55）")
    ap.add_argument("--no-timeline", action="store_true", help="跳过逐单 timeline 读")
    ap.add_argument("--max-search", type=int, default=1000, help="gh search limit（上限 1000）")
    ap.add_argument("--sleep", type=float, default=0.15, help="逐仓/逐单读之间的礼貌间隔（秒）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    whitelist = {s.strip() for s in args.repo.split(",") if s.strip()} or None
    res = sweep(args.org, whitelist, stale_days=args.stale_days,
                threshold=args.similarity, do_timeline=not args.no_timeline,
                max_search=args.max_search, sleep_s=args.sleep, verbose=args.verbose)
    print_summary(res)

    if args.out:
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        md_path = outdir / f"{args.org}-issues-{stamp}.md"
        js_path = outdir / f"{args.org}-issues-{stamp}.json"
        md_path.write_text(render_md(res), encoding="utf-8")
        payload = {
            "org": res.org, "generated_at": res.generated_at,
            "totals": {"repos": len(res.repos), "issues": len(res.issues),
                       "prs_seen": res.prs_seen_total,
                       "buckets": {b: sum(1 for i in res.issues if i.bucket == b)
                                   for b in BUCKET_ORDER}},
            "integrity": res.integrity_blob(),
            "issues": [],
        }
        for i in res.issues:
            d = asdict(i)
            d["key"] = i.key  # asdict 只序列化字段，property 要手补（工单主键）
            d["age_days"] = age_days(i.updated_at)
            payload["issues"].append(d)
        js_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"战报已写盘：{md_path}\n工单 JSON：{js_path}")

    return 2 if res.failed_repos else 0


if __name__ == "__main__":
    sys.exit(main())
