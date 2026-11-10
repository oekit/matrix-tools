#!/usr/bin/env python3
"""issue_sweep.py 的自测：用**假 gh**（无网络、无副作用）证明盘点不会假绿。

**为什么要有这个自测**：issue_sweep 是清零战的作战地图——地图漏了单，
后面所有「清零」都是在错误的账本上开工。历史教训（2026-11 三轮实测）每一条
都是「地图错了一条缝」：

  1. `gh search` 首轮漏单（oekit 第三轮实测）⇒ 双路枚举 + 差集逐单复核定胜负。
     自测证明：search 漏的单能从列表路补回（recon.search_miss=1），
     search 里的幽灵单（索引脏数据）能被单条 404 剔掉（recon.phantom=1）。
  2. **空页截断**（ghsafe 家族教训：偶发空 ≠ 到头了）⇒ 上一页满页时，
     空页必须重试。自测用一个**只在第一次读返回空**的陷阱页证明：
     150 单全数收回、对账闭合。反证：把空页重试变异掉（读到空就当终点），
     本自测必然抓到截断（--mutate）。
  3. REST issue 列表**混着 PR** ⇒ 按 `pull_request` 键剔除且对账闭合。
  4. 分级优先级：merged-pr > re-report > pr-open > stale > work，单条只进一级。
  5. 重报聚类：数字归一化后「audit 报 3 个…」≡「audit 报 5 个…」，最早单为 canonical。
  6. **白名单约束两条路径**：--repo core 时路径 B 的白名单外结果不许被
     verify / 收录（初版真实漏掉了这条，自测先于合入抓到）。
  7. **只读契约**：整轮扫描的 gh 调用日志里不许出现 close / comment / PATCH /
     DELETE——关单回执是 close_issue.py 的职责。

**假 gh 的形态必须贴近真实 gh**（close_issue_selftest 的教训：fixture 形态
造假，自测就测了个寂寞）：`gh api` 输出裸 JSON；`auth status` 退出码判认证；
404 时 stderr 带 `Not Found (HTTP 404)`（GhCommandError.is_not_found 靠这个判）；
500 时 stderr 带 `HTTP 500`（TRANSIENT_RE 靠这个重试）。

用法：python3 scripts/issue_sweep_selftest.py
      python3 scripts/issue_sweep_selftest.py --mutate   # 变异自证：砍掉空页重试必须被抓住
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPT = HERE / "issue_sweep.py"
ORG = "fakeorg"

FAKE_GH = r'''#!/usr/bin/env python3
"""假 gh：按 ISSUE_SWEEP_FAKE_DB 回放 API；每次调用追加进 ISSUE_SWEEP_FAKE_LOG。"""
import json, os, sys
from urllib.parse import parse_qs

argv = sys.argv[1:]
log = os.environ.get("ISSUE_SWEEP_FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\n")

db_path = os.environ.get("ISSUE_SWEEP_FAKE_DB")
db = json.load(open(db_path)) if db_path else {}


def die(msg, rc=1):
    sys.stderr.write(msg + "\n")
    sys.exit(rc)


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def state_get(path):
    if path and os.path.exists(path):
        return json.load(open(path))
    return {}


def state_put(path, st):
    if path:
        json.dump(st, open(path, "w"))


def repo_obj(name):
    r = db["repos"][name]
    return {"name": name, "open_issues_count": r.get("open_issues_count", 0),
            "archived": r.get("archived", False), "private": r.get("private", False),
            "pushed_at": r.get("pushed_at", "2026-11-09T00:00:00Z")}


def issue_lite(full, num):
    """search 结果里的 issue-lite（ghost 单来自 search_extra，不在 issues 库里）。"""
    extra = db.get("search_extra", {}).get(f"{full}#{num}")
    if extra:
        return extra
    name = full.split("/", 1)[1]
    for it in db.get("issues", {}).get(name, []):
        if it["number"] == num:
            return {"repository": {"nameWithOwner": full}, "number": num,
                    "title": it.get("title", ""), "state": "open",
                    "createdAt": it.get("created_at", ""),
                    "updatedAt": it.get("updated_at", ""),
                    "url": f"https://github.com/{full}/issues/{num}"}
    die(f"search_lite missing {full}#{num}")


if argv[:2] == ["auth", "status"]:
    sys.exit(0)  # 认证 OK

if argv[:1] == ["api"]:
    base, _, qs = argv[1].partition("?")
    q = {k: v[0] for k, v in parse_qs(qs).items()}
    parts = base.strip("/").split("/")

    if parts[0] in ("orgs", "users") and len(parts) == 3 and parts[2] == "repos":
        if parts[0] == "orgs" and not db.get("orgs_endpoint", True):
            die("gh: Not Found (HTTP 404)")
        per = int(q.get("per_page", "30")); page = int(q.get("page", "1"))
        names = sorted(db["repos"])
        emit([repo_obj(n) for n in names[(page - 1) * per: page * per]])

    if parts[0] == "repos":
        org, name = parts[1], parts[2]
        if len(parts) == 4 and parts[3] == "issues":
            if name == "failrepo":
                die('gh: Get "https://api.github.com/repos/%s/%s/issues": HTTP 500' % (org, name))
            per = int(q.get("per_page", "30")); page = int(q.get("page", "1"))
            key = f"{org}/{name}/issues"
            # 空页陷阱：该页**第一次读**返回 []，之后读才给真数据——
            # 复刻 GitHub 偶发空集合（ghsafe 模块口径的实测场景）。
            trap = db.get("empty_page_trap", {}).get(key)
            st_path = os.environ.get("ISSUE_SWEEP_FAKE_STATE")
            st = state_get(st_path)
            sk = f"{key}:page{page}"
            if trap == page and sk not in st:
                st[sk] = 1
                state_put(st_path, st)
                emit([])
            items = db.get("issues", {}).get(name, [])
            emit(items[(page - 1) * per: page * per])
        if len(parts) == 5 and parts[3] == "issues" and parts[4].isdigit():
            num = int(parts[4])
            for it in db.get("issues", {}).get(name, []):
                if it["number"] == num:
                    if it.get("pull_request"):
                        die("gh: Not Found (HTTP 404)")
                    emit({"number": num, "title": it.get("title", ""),
                          "state": "open", "html_url": f"https://github.com/{org}/{name}/issues/{num}",
                          "created_at": it.get("created_at", ""),
                          "updated_at": it.get("updated_at", ""),
                          "comments": it.get("comments", 0),
                          "labels": it.get("labels", [])})
            die("gh: Not Found (HTTP 404)")
        if len(parts) == 6 and parts[4].isdigit() and parts[5] == "timeline":
            emit(db.get("timelines", {}).get(f"{org}/{name}#{parts[4]}", []))
        if len(parts) == 5 and parts[3] == "pulls" and parts[4].isdigit():
            p = db.get("pulls", {}).get(name, {}).get(parts[4])
            if p is None:
                die("gh: Not Found (HTTP 404)")
            emit(p)
    die(f"fake gh: 未识别的调用 {' '.join(argv[:3])}", rc=2)

if argv[:2] == ["search", "issues"]:
    opts = argv[2:]
    owner = opts[opts.index("--owner") + 1]
    skey = "default"
    if "--sort" in opts:
        skey = opts[opts.index("--order") + 1] if "--order" in opts else "desc"
    out = []
    for key in db.get("search_visible", {}).get(skey, []):
        full, _, num = key.rpartition("#")
        if full.split("/", 1)[0] != owner:
            continue
        out.append(issue_lite(full, int(num)))
    emit(out)

die(f"fake gh: 未识别的调用 {' '.join(argv[:3])}", rc=2)
'''


class FakeGH:
    """一个测试用例一套 tmp 目录：假 gh 可执行 + db/log/state 文件。"""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="issue_sweep_selftest_"))
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.db_path = self.dir / "db.json"
        self.log_path = self.dir / "log.jsonl"
        self.state_path = self.dir / "state.json"

    def set_db(self, db: dict) -> None:
        self.db_path.write_text(json.dumps(db))

    def run(self, *extra_args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["ISSUE_SWEEP_FAKE_DB"] = str(self.db_path)
        env["ISSUE_SWEEP_FAKE_LOG"] = str(self.log_path)
        env["ISSUE_SWEEP_FAKE_STATE"] = str(self.state_path)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--org", ORG, "--sleep", "0", *extra_args],
            capture_output=True, text=True, env=env)

    def log(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(l) for l in self.log_path.read_text().splitlines() if l.strip()]

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def big_title(i: int) -> str:
    """批量单用**字母**去重：数字会被归一化吃掉（重报聚类的既定行为）。"""
    return f"batch {chr(97 + i // 26)}{chr(97 + i % 26)}"


def make_db(*, orgs_endpoint: bool = True, include_failrepo: bool = False,
            big_count: int = 105) -> dict:
    """标准战况库：core 6 单（五级各占其一）+ small 1 单（跨仓同题）+ big N 单（翻页用）。

    core 六单的分工：
      #1 audit 报 3 个…（work，重报簇 canonical）
      #2 audit 报 5 个…（re-report → #1；数字归一化后 Jaccard=1.0）
      #3 CI 门禁误报（merged-pr：timeline 引用已合入 PR 7）
      #4 agent 卡死在 Waiter（pr-open：timeline 引用 open PR 8）
      #5 陈年单（stale：updated 2026-05-01）
      #6 审计漂移报告（work；与 small#1 跨仓同题）
    search 三排序并集**故意漏掉 core#4**（search 漏单用例）、**故意多出 ghost#9**
    （幽灵单用例，单条读 404）。
    """
    labels = lambda n: [{"name": n}]  # noqa: E731
    core_issues = [
        {"number": 1, "title": "audit 报 3 个 tag 缺 Release", "labels": labels("audit"),
         "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-11-08T00:00:00Z",
         "comments": 0},
        {"number": 2, "title": "audit 报 5 个 tag 缺 Release", "labels": labels("audit"),
         "created_at": "2026-10-20T00:00:00Z", "updated_at": "2026-11-08T00:00:00Z",
         "comments": 0},
        {"number": 3, "title": "CI 门禁误报", "labels": labels("ci"),
         "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-11-07T00:00:00Z",
         "comments": 1},
        {"number": 4, "title": "agent 卡死在 Waiter", "labels": labels("bug"),
         "created_at": "2026-10-03T00:00:00Z", "updated_at": "2026-11-06T00:00:00Z",
         "comments": 2},
        {"number": 5, "title": "陈年单", "labels": [],
         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z",
         "comments": 0},
        {"number": 6, "title": "审计漂移报告", "labels": labels("audit"),
         "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-09T00:00:00Z",
         "comments": 0},
    ]
    small_issues = [
        {"number": 1, "title": "审计漂移报告", "labels": labels("audit"),
         "created_at": "2026-11-02T00:00:00Z", "updated_at": "2026-11-09T00:00:00Z",
         "comments": 0},
    ]
    big_issues = [{"number": n, "title": big_title(n - 1), "labels": [],
                   "created_at": "2026-11-05T00:00:00Z",
                   "updated_at": "2026-11-09T00:00:00Z", "comments": 0}
                  for n in range(1, big_count + 1)]

    repos = {"core": {"open_issues_count": 6}, "small": {"open_issues_count": 1},
             "big": {"open_issues_count": big_count}}
    if include_failrepo:
        repos["failrepo"] = {"open_issues_count": 1}

    pr = {"url": "https://api.github.com/repos/fakeorg/core/pulls/7"}
    db = {
        "orgs_endpoint": orgs_endpoint,
        "repos": repos,
        "issues": {"core": core_issues, "small": small_issues, "big": big_issues},
        "pulls": {"core": {"7": {"merged": True, "state": "closed"},
                           "8": {"merged": False, "state": "open"}}},
        "timelines": {
            "fakeorg/core#3": [{"event": "cross-referenced",
                                "source": {"issue": {"number": 7, "pull_request": pr,
                                                     "repository": {"full_name": "fakeorg/core"}}}}],
            "fakeorg/core#4": [{"event": "cross-referenced",
                                "source": {"issue": {"number": 8, "pull_request": dict(pr, url=pr["url"].replace("7", "8")),
                                                     "repository": {"full_name": "fakeorg/core"}}}}],
        },
        "search_extra": {
            "fakeorg/ghost#9": {"repository": {"nameWithOwner": "fakeorg/ghost"},
                                "number": 9, "title": "幽灵单", "state": "open",
                                "createdAt": "2026-11-03T00:00:00Z",
                                "updatedAt": "2026-11-03T00:00:00Z", "url": ""},
        },
    }
    visible = ([f"fakeorg/core#{n}" for n in range(1, 7) if n != 4]
               + ["fakeorg/small#1"]
               + [f"fakeorg/big#{n}" for n in range(1, big_count + 1)]
               + ["fakeorg/ghost#9"])
    db["search_visible"] = {"default": visible, "asc": visible, "desc": visible}
    return db


def load_json(outdir: Path, org: str) -> dict:
    matches = sorted(outdir.glob(f"{org}-issues-*.json"))
    assert len(matches) == 1, f"期望恰好一份 JSON 工单，实际 {matches}"
    return json.loads(matches[0].read_text())


def log_has(log: list, prefix: list[str]) -> bool:
    """前缀匹配一条 gh 调用。prefix 统一转 list 比较（tuple == list 恒 False 的坑）。"""
    prefix = list(prefix)
    return any(e[:len(prefix)] == prefix for e in log)


# ---------------------------------------------------------------- 用例


def test_full_classification(outdir: Path) -> None:
    """主战况：翻页、PR 剔除、双路对账、五级分级、跨仓同题、只读契约、写盘。"""
    fx = FakeGH()
    try:
        fx.set_db(make_db())
        r = fx.run("--out", str(outdir), "--stale-days", "90")
        assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"

        data = load_json(outdir, ORG)
        totals = data["totals"]
        assert totals["repos"] == 3, totals
        assert totals["prs_seen"] == 0, totals
        assert totals["issues"] == 112, totals  # 6 + 1 + 105，core#4 从列表路收回

        b = totals["buckets"]
        assert b == {"merged-pr": 1, "re-report": 1, "pr-open": 1, "stale": 1,
                     "work": 108}, b

        issues = {i["key"]: i for i in data["issues"]}
        rr = issues[f"{ORG}/core#2"]
        assert rr["bucket"] == "re-report" and rr["canonical_of"] == f"{ORG}/core#1", rr
        assert (rr["re_report_of_sim"] or 0) >= 0.55, rr  # 数字归一化后完全同题
        mp = issues[f"{ORG}/core#3"]
        assert mp["bucket"] == "merged-pr", mp
        assert any(p["merged"] for p in mp["pr_refs"]), mp
        po = issues[f"{ORG}/core#4"]
        assert po["bucket"] == "pr-open" and any(p["state"] == "open" for p in po["pr_refs"]), po
        assert issues[f"{ORG}/core#5"]["bucket"] == "stale", issues[f"{ORG}/core#5"]
        assert issues[f"{ORG}/small#1"]["cross_repo_dup"] == [f"{ORG}/core#6"], \
            issues[f"{ORG}/small#1"]
        assert issues[f"{ORG}/core#6"]["cross_repo_dup"] == [], "首见单不标跨仓同题"

        integ = data["integrity"]
        assert integ["failed_repos"] == [], integ
        assert integ["count_mismatch"] == [], integ
        rec = integ["reconciliation"]
        assert rec["status"] == "ok", rec
        assert rec["search_miss_keys"] == [f"{ORG}/core#4"], rec  # search 漏单被对账抓出
        assert rec["phantom_keys"] == [f"{ORG}/ghost#9"], rec     # 幽灵单被 404 剔掉

        # 翻页真的发生了（big 105 单 ⇒ 100+5 两页）
        log = fx.log()
        assert log_has(log, ["api", f"repos/{ORG}/big/issues?per_page=100&page=2"]), \
            "big 仓应有第 2 页请求"

        # 只读契约：整轮不允许出现任何写型调用（关单/评论/打标/PATCH/DELETE）
        for e in log:
            blob = " ".join(e)
            for banned in ("issue close", "issue comment", "issue edit",
                           "issue lock", "-X PATCH", "-X DELETE", "PATCH", "DELETE"):
                assert banned not in blob, f"只读契约被破坏：{e}"

        md = sorted(outdir.glob(f"{ORG}-issues-*.md"))
        assert len(md) == 1 and "merged-pr" in md[0].read_text()
    finally:
        fx.cleanup()


def test_empty_page_trap(outdir: Path) -> None:
    """空页陷阱：第 2 页第一次读返回空 ⇒ 必须重试收回，不许当终点截断。"""
    fx = FakeGH()
    try:
        db = make_db(big_count=150)
        db["empty_page_trap"] = {"fakeorg/big/issues": 2}
        fx.set_db(db)
        r = fx.run("--out", str(outdir), "--no-timeline")
        assert r.returncode == 0, r.stderr
        data = load_json(outdir, ORG)
        assert data["totals"]["issues"] == 157, data["totals"]  # 6+1+150
        assert data["integrity"]["count_mismatch"] == [], data["integrity"]
    finally:
        fx.cleanup()


def test_users_fallback(outdir: Path) -> None:
    """orgs 端点 404 ⇒ 回退 users 端点（个人命名空间）。"""
    fx = FakeGH()
    try:
        fx.set_db(make_db(orgs_endpoint=False, big_count=1))
        r = fx.run("--out", str(outdir), "--no-timeline")
        assert r.returncode == 0, r.stderr
        data = load_json(outdir, ORG)
        assert data["totals"]["repos"] == 3, data["totals"]
        assert log_has(fx.log(), ["api", f"users/{ORG}/repos?per_page=100&page=1"]), \
            "orgs 404 后应回退 users 端点"
    finally:
        fx.cleanup()


def test_repo_whitelist(outdir: Path) -> None:
    """--repo 白名单约束**两条**路径：路径 A 不扫白名单外，路径 B 不 verify 白名单外。"""
    fx = FakeGH()
    try:
        fx.set_db(make_db(big_count=10))
        r = fx.run("--out", str(outdir), "--no-timeline", "--repo", "core")
        assert r.returncode == 0, r.stderr
        data = load_json(outdir, ORG)
        assert data["totals"]["repos"] == 1 and data["totals"]["issues"] == 6, data["totals"]
        log = fx.log()
        assert not log_has(log, ["api", f"repos/{ORG}/big/issues?per_page=100&page=1"]), \
            "白名单外的仓不许进路径 A"
        assert not log_has(log, ["api", f"repos/{ORG}/big/issues/1"]), \
            "白名单外的 search 结果不许被逐条 verify"
        assert data["integrity"]["reconciliation"]["phantom_keys"] == [], \
            "白名单外的幽灵单不应计入战报"
    finally:
        fx.cleanup()


def test_no_timeline(outdir: Path) -> None:
    """--no-timeline：不读 timeline，merged-pr/pr-open 分级随之不可判（fail-open 到基础分级）。"""
    fx = FakeGH()
    try:
        fx.set_db(make_db(big_count=1))
        r = fx.run("--out", str(outdir), "--no-timeline")
        assert r.returncode == 0, r.stderr
        data = load_json(outdir, ORG)
        assert not log_has(fx.log(), ["api", f"repos/{ORG}/core/issues/3/timeline?per_page=100&page=1"])
        issues = {i["key"]: i for i in data["issues"]}
        assert issues[f"{ORG}/core#3"]["pr_refs"] == [] and \
            issues[f"{ORG}/core#3"]["bucket"] != "merged-pr", issues[f"{ORG}/core#3"]
    finally:
        fx.cleanup()


def test_failed_repo_exit2(outdir: Path) -> None:
    """读失败的仓：读不到 ≠ 没有——如实记 failed_repos 并以 2 退出，不许静默绿。"""
    fx = FakeGH()
    try:
        fx.set_db(make_db(big_count=1, include_failrepo=True))
        r = fx.run("--out", str(outdir), "--no-timeline")
        assert r.returncode == 2, f"读失败的仓必须 exit 2，实际 {r.returncode}\n{r.stderr}"
        data = load_json(outdir, ORG)
        assert data["integrity"]["failed_repos"] == ["failrepo"], data["integrity"]
        assert data["totals"]["repos"] == 4 and data["totals"]["issues"] == 8, data["totals"]
    finally:
        fx.cleanup()


def _mutated_script(tmp: Path) -> Path:
    """把空页重试变异成「读到空就当终点」（复刻天真的分页写法），自证自测抓得住。"""
    shutil.copy(HERE / "ghsafe.py", tmp / "ghsafe.py")
    shutil.copy(REPO / "matrix.py", tmp / "matrix.py")  # matrix 在仓根，不在 scripts/
    src = SCRIPT.read_text()
    needle = ("        if not data and prev_full:\n"
              "            data = ghsafe.read_until_nonempty(read, rounds=empty_rounds,\n"
              "                                              backoff=empty_backoff) or []")
    assert needle in src, "变异锚点不在脚本里（脚本已改，更新变异）"
    mutated = src.replace(needle,
                          "        if not data and prev_full:\n"
                          "            data = []  # MUTATED: 空页即终点")
    out = tmp / "issue_sweep.py"
    out.write_text(mutated)
    return out


def test_mutate_empty_page_retry(outdir: Path) -> None:
    """变异自证：砍掉空页重试（天真分页）⇒ 截断必须留下指纹。

    实测变异体的行为（比预期更有教育意义）：路径 A 确实被截成 100/150，
    **但双路对账把丢掉的 50 单从 search 路逐单复核全数找回**（标 search-only），
    且 open_issues_count 对账红灯亮起——防线纵深生效。
    所以「抓住变异」的判据是两条指纹：
      1. big 仓 count_mismatch（expected 150 vs collected 100）
      2. 恰好 50 条 big issue 带 search-only 标记（列表路漏、search 路救回）
    """
    tmp = Path(tempfile.mkdtemp(prefix="issue_sweep_mutant_"))
    fx = FakeGH()
    try:
        mutant = _mutated_script(tmp)
        fx.set_db(make_db(big_count=150))
        db = json.loads(fx.db_path.read_text())
        db["empty_page_trap"] = {"fakeorg/big/issues": 2}
        fx.set_db(db)
        env = os.environ.copy()
        env["PATH"] = f"{fx.bin}:{env['PATH']}"
        env["ISSUE_SWEEP_FAKE_DB"] = str(fx.db_path)
        env["ISSUE_SWEEP_FAKE_LOG"] = str(fx.dir / "mutant_log.jsonl")
        env["ISSUE_SWEEP_FAKE_STATE"] = str(fx.dir / "mutant_state.json")
        r = subprocess.run([sys.executable, str(mutant), "--org", ORG, "--sleep", "0",
                            "--out", str(outdir), "--no-timeline"],
                           capture_output=True, text=True, env=env)
        data = load_json(outdir, ORG)
        integ = data["integrity"]
        mism = [m for m in integ["count_mismatch"] if m["repo"] == "big"]
        assert mism, f"变异体的对账必须红（big 仓应有 50 单失踪）：{integ}"
        assert mism[0]["collected"] == 100 and mism[0]["expected(open_issues_count)"] == 150, mism
        only = [i for i in data["issues"]
                if i["repo"] == "big" and "search-only" in i["flags"]]
        assert len(only) == 50, \
            f"被截断的 50 单只能靠 search 路 + 逐单复核找回，实际 {len(only)}"
        print("  变异自证 ✅ 天真分页在空页处截断——对账红灯 + 50 条 search-only 指纹双双亮起"
              "（双路对账兜住了截断，但天真分页本身必须被对账抓住）")
    finally:
        fx.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    mutate = "--mutate" in argv
    outroot = Path(tempfile.mkdtemp(prefix="issue_sweep_out_"))
    cases = [
        ("主战况（翻页/PR 剔除/双路对账/五级分级/只读契约/写盘）", test_full_classification),
        ("空页陷阱（偶发空 ≠ 终点，150 单全数收回）", test_empty_page_trap),
        ("orgs 404 回退 users 端点", test_users_fallback),
        ("--repo 白名单约束两条路径", test_repo_whitelist),
        ("--no-timeline fail-open", test_no_timeline),
        ("读失败仓 fail-loud（exit 2）", test_failed_repo_exit2),
    ]
    if mutate:
        cases = [("变异自证：砍掉空页重试必须被抓住", test_mutate_empty_page_retry)]
    failures = 0
    for name, fn in cases:
        outdir = outroot / fn.__name__
        outdir.mkdir()
        try:
            fn(outdir)
            print(f"✅ {name}")
        except AssertionError as e:
            failures += 1
            print(f"✗ {name}\n   {e}")
    shutil.rmtree(outroot, ignore_errors=True)
    if failures:
        print(f"\n{failures} 个用例失败")
        return 1
    print(f"\n全部 {len(cases)} 个用例通过（假 gh，无网络无副作用）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
