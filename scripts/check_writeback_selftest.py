#!/usr/bin/env python3
"""check_writeback.py 的自测：证明「写回前置门禁」这条路径有牙齿。

#180 的对象是**写回版本节之前**那道门禁：你要落笔的那条 bullet 里
混进了别仓的号 / 本仓的其它号时，门禁必须**抓得住**（否则回执张冠李戴）。

覆盖的用例：

1.  C0  判据**不共享**定位器的实现（不许 import `gen_releases` 的定位函数）；
2.  C1  跨仓号（`gormx#36`）⇒ 告警 `cross_repo`；
3.  C2  本仓其它号的**强形态**（`matrix-tools#181`）⇒ 告警 `same_repo_other`；
4.  C3  裸弱形态（`#181`）⇒ 告警 `bare_other`；
5.  C4  PR 引用（`PR #181` / `PRs #183、PR #184` / `pull/186`）⇒ **不告警**；
6.  C4b PR 引用与真 issue 引用同行 ⇒ 只告警真 issue 引用那一个；
7.  C5  **顶格段落**里的号（血缘声明段）⇒ **不告警**（不产生条目绑定）；
8.  C6  上游文本里**含目标短语但不在目标位置**（别的 bullet 上有 `gormx#36`）
    ⇒ 目标 bullet **不告警**（判据不是「存在性判据」）；
9.  C7  **续行**里混进别的号（#154 的规矩）⇒ 告警；
10. C8  目标 bullet 不存在 ⇒ 没有可检查的项（`--require-target` ⇒ 报错退出）；
11. C9  allowlist 生效 ⇒ 豁免 ⇒ 退出码 0；
12. C10 `--all` dry-run ⇒ 列出**所有**会告警的位置；
13. C11 `--json` 输出形状（机器可读）；
14. C12 CLI 端到端（干净 ⇒ 0；有告警 ⇒ 非 0）；
15. C13 版本节不存在 ⇒ **退出码 2**（读不到 ≠ 没有干净）；
16. C14 `--nested` 嵌套节（子目录 CHANGELOG + 根 CHANGELOG 回退）；
17. C15 allowlist 读不懂 / 类别名写错 ⇒ 报错退出（豁免**静默失效**比误报更糟）。

变异自证（每条必须**精确转红**，否则这份断言没有牙齿）：

- M1「子串 `in` 判存在」（只问「这个号在**这一节里**出现过吗」，不问「出现在
  **哪条 bullet** 里」）⇒ 用例 C6 转红；
- M2「不做 bullet 块限定（扫全文）」（整节当一条 bullet）⇒ 用例 C5、C6 转红；
- M3「门禁完全失效」（一条都不报）⇒ 所有「期望有告警」的用例转红。

⇒ 每条变异的**预期转红集合**都被写死并逐条核对（红得**精确**：
不该红的用例必须还绿着）。

用法：python3 scripts/check_writeback_selftest.py
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import check_writeback  # noqa: E402

REPO = "matrix-tools"
VERSION = "v0.2.0"
NUM = "180"

# ── fixture ────────────────────────────────────────────────────────────
# 每个 fixture 都是一份**完整**的 CHANGELOG（带版本节头），
# 便于同时覆盖「按 --file 读」与「按 $MATRIX_BASE/<repo>/CHANGELOG.md 读」两条路。

# C1：目标 bullet 里混进了**别仓**的号（#180 那天真踩到的形态）
CL_CROSS = """## [v0.2.0] — 2026-11-02

- #180：chore(tracing): 测试依赖的 sqlite 驱动换成纯 Go 实现（关联 gormx#36）
"""

# C2：本仓其它号的**强形态**
CL_SAME_STRONG = """## [v0.2.0] — 2026-11-02

- #180：与 matrix-tools#181 一起改的
"""

# C3：裸弱形态（会被定位器当成本仓的号绑定 ⇒ 归属不可判定）
CL_BARE = """## [v0.2.0] — 2026-11-02

- #180：顺手修了 #181 的事
"""

# C4：PR 引用形态 ⇒ 不告警（定位器会先剥离 PR 引用再判）
CL_PR = """## [v0.2.0] — 2026-11-02

- #180：修好了（PR #181、PRs #183、PR #184），见 pull/186
"""

# C4b：PR 引用与真 issue 引用**同行** ⇒ 只告警真 issue 引用那一个
CL_PR_PLUS_ISSUE = """## [v0.2.0] — 2026-11-02

- #180：修了（PR #181），并回应了 #182 的提问
"""

# C12：单条干净的 bullet（CLI 端到端的「干净」那一路）
CL_CLEAN = """## [v0.2.0] — 2026-11-02

- #180：修好了
"""

# C5：**顶格段落**里的号（matrix-tools 自己的惯例：血缘号顶格声明）⇒ 不告警
CL_TOPLEVEL = """## [v0.2.0] — 2026-11-02

- #180：本条修复

血缘（**顶格声明**）：关联 gormx#36 —— 这些号**故意不放 bullet 里**。
"""

# C6：上游文本里**含目标短语但不在目标位置**（别的 bullet 上有 gormx#36）
#     ⇒ 目标 bullet 不该被告警（判据必须看**位置**，不是「存在性」）
CL_UPSTREAM = """## [v0.2.0] — 2026-11-02

- #180：本条修复：口径统一了

- 另一条：讲的是别仓的事（关联 gormx#36）
"""

# C7：**续行**里混进别的号（#154 的规矩：续行里不许出现别的 issue 号）
#     ⚠️ 续行必须**紧跟** bullet（空行会断块 —— 与定位器的切块规则一致）
CL_CONT = """## [v0.2.0] — 2026-11-02

- #180：修好了
  续行里提了一句 #79 —— 那是别的号
"""

# C8：目标 bullet 不存在（本节里只有 #181）
CL_NO_TARGET = """## [v0.2.0] — 2026-11-02

- #181：别的 issue 的修复
"""

# C10：全库 dry-run（两节，各一处跨仓号）
CL_ALL = """## [v0.2.0] — 2026-11-02

- #180：本条修复（关联 gormx#36）

## [v0.1.9] — 2026-11-01

- #170：另一条（关联 pkg#46）
"""


def write_repo(base: Path, changelog: str, nested: str | None = None,
               nested_changelog: str | None = None) -> Path:
    """搭一个假的仓检出根（`<base>/<repo>/CHANGELOG.md`）⇒ 返回仓目录。"""
    rdir = base / REPO
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "README.md").write_text("# fake matrix-tools\n", encoding="utf-8")
    (rdir / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    if nested and nested_changelog is not None:
        sub = rdir / nested
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "CHANGELOG.md").write_text(nested_changelog, encoding="utf-8")
    return rdir


def run_main(args: list[str], base: Path) -> tuple[int, str]:
    """在进程内跑一次 main() ⇒ (退出码, stdout)。"""
    saved = check_writeback.BASE
    check_writeback.BASE = str(base)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = check_writeback.main(args)
    finally:
        check_writeback.BASE = saved
    return code, buf.getvalue()


def findings_of(out: str) -> list[dict]:
    """从 --json 输出里取 findings 列表。"""
    return json.loads(out)["findings"]


def categories(out: str) -> list[str]:
    return [f["category"] for f in findings_of(out)]


def run_all(root: Path, rid: str) -> list[tuple[str, str]]:
    """跑一遍全部用例，返回 [(用例号, 失败原因)]（空 = 全过）。"""
    fails: list[tuple[str, str]] = []

    def fail(cid: str, msg: str) -> None:
        fails.append((cid, msg))

    # ── C0：判据**不许**共享定位器的实现 ──
    src = (HERE / "check_writeback.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module.split(".")[0])
        elif isinstance(node, ast.Name):
            names.add(node.id)
    if "gen_releases" in mods:
        fail("C0", "check_writeback import 了 gen_releases ⇒ "
                   "门禁在用被检对象验被检对象（定位器一改口径，门禁跟着变）")
    shared = {"locate_issue_entries", "mentions_issue", "_pr_only_mentions",
              "weak_issue_re"} & names
    if shared:
        fail("C0", f"check_writeback 用了定位器的判据函数：{sorted(shared)}"
                   "⇒ 门禁必须自己扫、自己判形态")

    # ── C1：跨仓号 ⇒ cross_repo ──
    base = root / "c1"
    write_repo(base, CL_CROSS)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    fs = findings_of(out)
    if code == 0:
        fail("C1", f"跨仓号必须让门禁红（退出码非 0），实得 0：{out[:200]}")
    if len(fs) != 1:
        fail("C1", f"期望恰好 1 条告警，实得 {len(fs)}：{[f['category'] for f in fs]}")
    elif fs[0]["category"] != "cross_repo":
        fail("C1", f"跨仓号的类别必须是 cross_repo，实得 {fs[0]['category']}")
    elif fs[0]["num"] != "36":
        fail("C1", f"告警的号应是 36，实得 {fs[0]['num']}")
    else:
        if "gormx#36" not in fs[0]["matched"]:
            fail("C1", f"告警应指明命中的是 gormx#36，实得 {fs[0]['matched']}")
        if not fs[0]["bullet"].startswith("#180"):
            fail("C1", f"告警应挂在目标 bullet 上，实得 {fs[0]['bullet'][:80]}")

    # ── C2：本仓其它号（强形态）⇒ same_repo_other ──
    base = root / "c2"
    write_repo(base, CL_SAME_STRONG)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    fs = findings_of(out)
    if code == 0:
        fail("C2", f"本仓其它号必须让门禁红，实得退出码 0：{out[:200]}")
    if [f["category"] for f in fs] != ["same_repo_other"]:
        fail("C2", f"期望 same_repo_other 一条，实得 {[f['category'] for f in fs]}")
    elif fs[0]["num"] != "181":
        fail("C2", f"告警的号应是 181，实得 {fs[0]['num']}")

    # ── C3：裸弱形态 ⇒ bare_other ──
    base = root / "c3"
    write_repo(base, CL_BARE)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    fs = findings_of(out)
    if code == 0:
        fail("C3", f"裸号必须让门禁红，实得退出码 0：{out[:200]}")
    if [f["category"] for f in fs] != ["bare_other"]:
        fail("C3", f"期望 bare_other 一条，实得 {[f['category'] for f in fs]}")
    elif fs[0]["num"] != "181":
        fail("C3", f"告警的号应是 181，实得 {fs[0]['num']}")

    # ── C4：PR 引用 ⇒ 不告警 ──
    base = root / "c4"
    write_repo(base, CL_PR)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    if code != 0:
        fail("C4", f"PR 引用形态不该告警（退出码应 0），实得 {code}：{out[:200]}")
    if findings_of(out):
        fail("C4", f"PR 引用形态不该产生告警，实得 {[f['matched'] for f in findings_of(out)]}")

    # ── C4b：PR 引用与真 issue 引用同行 ⇒ 只告警真 issue 引用那一个 ──
    base = root / "c4b"
    write_repo(base, CL_PR_PLUS_ISSUE)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    fs = findings_of(out)
    if code == 0:
        fail("C4b", f"同行里的真 issue 引用必须告警，实得退出码 0：{out[:200]}")
    if len(fs) != 1 or fs[0]["num"] != "182":
        fail("C4b", f"期望只告警 #182 一条（PR #181 被剥离），"
                    f"实得 {[(f['matched'], f['num']) for f in fs]}")

    # ── C5：顶格段落 ⇒ 不告警 ──
    base = root / "c5"
    write_repo(base, CL_TOPLEVEL)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    if code != 0:
        fail("C5", f"顶格段落不产生条目绑定 ⇒ 不该告警，实得退出码 {code}：{out[:200]}")
    if findings_of(out):
        fail("C5", f"顶格段落里的号不该告警，实得 {[f['matched'] for f in findings_of(out)]}")

    # ── C6：上游文本含目标短语但不在目标位置 ⇒ 目标 bullet 不告警 ──
    base = root / "c6"
    write_repo(base, CL_UPSTREAM)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    if code != 0:
        fail("C6", f"别的 bullet 上的号不该算到目标 bullet 头上（存在性判据 ⇒ 假红），"
                   f"实得退出码 {code}：{out[:300]}")
    if findings_of(out):
        fail("C6", f"判据必须看**位置**：别的 bullet 上的 gormx#36 不该被告警，"
                   f"实得 {[f['matched'] for f in findings_of(out)]}")

    # ── C7：续行里混进别的号 ⇒ 告警（续行是条目的一部分，#154）──
    base = root / "c7"
    write_repo(base, CL_CONT)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    fs = findings_of(out)
    if code == 0:
        fail("C7", f"续行里混进的号必须告警，实得退出码 0：{out[:200]}")
    if [f["category"] for f in fs] != ["bare_other"] or fs[0]["num"] != "79":
        fail("C7", f"期望 bare_other(#79) 一条，实得 {[(f['category'], f['num']) for f in fs]}")

    # ── C8：目标 bullet 不存在 ⇒ 没有可检查的项 ──
    base = root / "c8"
    write_repo(base, CL_NO_TARGET)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    data = json.loads(out)
    if data["target_found"]:
        fail("C8", "本节里没有绑定到 #180 的 bullet ⇒ target_found 必须是 false")
    if code != 0:
        fail("C8", f"目标 bullet 不存在时不该报红（那是「没检查到」，不是「查出不干净」），"
                   f"实得退出码 {code}")
    code2, out2 = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM,
                             "--require-target", "--json"], base)
    if code2 != 2:
        fail("C8", f"--require-target 时目标 bullet 不存在 ⇒ 退出码应为 2，实得 {code2}")
    # ── C9：allowlist 生效 ⇒ 豁免 ⇒ 退出码 0 ──
    base = root / "c9"
    write_repo(base, CL_CROSS)
    al = base / "allow.json"
    al.write_text(json.dumps({"version": 1, "entries": [
        {"repo": REPO, "version": VERSION, "num": "36", "category": "cross_repo",
         "bullet": "#180：chore(tracing)", "reason": "测试用豁免"}]}, ensure_ascii=False),
        encoding="utf-8")
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM,
                          "--allowlist", str(al), "--json"], base)
    fs = findings_of(out)
    if code != 0:
        fail("C9", f"被 allowlist 豁免的告警不该让门禁红，实得退出码 {code}：{out[:200]}")
    if len(fs) != 1 or not fs[0]["allowed"]:
        fail("C9", f"豁免的告警必须仍然被列出并标成 allowed，实得 {fs}")

    # ── C10：--all dry-run ⇒ 列出所有位置 ──
    base = root / "c10"
    write_repo(base, CL_ALL)
    code, out = run_main(["--repo", REPO, "--all", "--json"], base)
    fs = findings_of(out)
    if code == 0:
        fail("C10", f"--all 必须把全库会告警的位置报出来（退出码非 0），实得 0：{out[:200]}")
    if len(fs) != 2:
        fail("C10", f"期望 2 处告警（两节各一处），实得 {len(fs)}")
    else:
        if sorted(f["version"] for f in fs) != ["v0.1.9", "v0.2.0"]:
            fail("C10", f"告警要带版本节，实得 {[f['version'] for f in fs]}")
        if any(f["category"] != "cross_repo" for f in fs):
            fail("C10", f"两处都该是 cross_repo，实得 {[f['category'] for f in fs]}")

    # ── C11：--json 输出形状 ──
    base = root / "c11"
    write_repo(base, CL_CROSS)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM, "--json"],
                         base)
    data = json.loads(out)
    for key in ("repo", "mode", "version", "num", "file", "target_found",
                "allowlist", "findings", "count", "allowed_count", "exit_code"):
        if key not in data:
            fail("C11", f"--json 输出缺字段 {key}：{sorted(data)}")
            break
    else:
        if data["repo"] != REPO or data["mode"] != "single":
            fail("C11", f"--json 的 repo/mode 不对：{data.get('repo')}/{data.get('mode')}")
        if data["version"] != VERSION or data["num"] != NUM:
            fail("C11", f"--json 的 version/num 不对：{data.get('version')}/{data.get('num')}")
        if data["count"] != 1 or data["exit_code"] != 1:
            fail("C11", f"--json 的 count/exit_code 不对：{data.get('count')}/{data.get('exit_code')}")
        if not data["findings"]:
            # ⚠️ 这里**不许**提前 return：后面的用例必须照跑
            # （提前 return 会让「变异只红了一部分」看起来像「变异没被抓住」）。
            fail("C11", f"--json 的 findings 应当有 1 条，实得 0：{out[:200]}")
        else:
            f0 = data["findings"][0]
            for key in ("repo", "version", "num", "category", "matched", "bullet",
                        "line", "allowed", "reason"):
                if key not in f0:
                    fail("C11", f"--json 的 finding 缺字段 {key}：{sorted(f0)}")
                    break
            else:
                if not isinstance(f0["line"], int) or f0["line"] < 1:
                    fail("C11", f"finding 的行号必须是正整数，实得 {f0['line']!r}")

    # ── C12：CLI 端到端（子进程）──
    base = root / "c12"
    write_repo(base, CL_CROSS)
    env = dict(os.environ)
    env["MATRIX_BASE"] = str(base)
    p = subprocess.run([sys.executable, str(HERE / "check_writeback.py"),
                        "--repo", REPO, "--version", VERSION, "--num", NUM],
                       capture_output=True, text=True, env=env)
    if p.returncode == 0:
        fail("C12", f"CLI 有告警时必须非 0 退出，实得 0：{(p.stdout or '')[:200]}")
    if "cross_repo" not in (p.stdout or ""):
        fail("C12", f"CLI 输出必须写明类别 cross_repo：{(p.stdout or '')[:200]}")
    clean = write_repo(root / "c12b", CL_CLEAN)
    env2 = dict(os.environ)
    env2["MATRIX_BASE"] = str(root / "c12b")
    p2 = subprocess.run([sys.executable, str(HERE / "check_writeback.py"),
                         "--repo", REPO, "--version", VERSION, "--num", NUM],
                        capture_output=True, text=True, env=env2)
    if p2.returncode != 0:
        fail("C12", f"CLI 干净时必须退出 0，实得 {p2.returncode}："
                    f"{(p2.stdout or '') + (p2.stderr or '')[:200]}")

    # ── C13：版本节不存在 ⇒ 退出码 2（读不到 ≠ 没有干净）──
    base = root / "c13"
    write_repo(base, CL_CROSS)
    code, out = run_main(["--repo", REPO, "--version", "v9.9.9", "--num", NUM], base)
    if code != 2:
        fail("C13", f"版本节不存在 ⇒ 退出码应为 2（读不到 ≠ 没有），实得 {code}")

    # ── C14：--nested（子目录 CHANGELOG + 根 CHANGELOG 回退）──
    base = root / "c14"
    write_repo(base, "## [sub/v0.2.0] — 2026-11-02\n\n"
                             "- #180：根文件里的嵌套节（关联 gormx#36）\n",
               nested="sub", nested_changelog=CL_CROSS)
    code, out = run_main(["--repo", REPO, "--version", VERSION, "--nested", "sub",
                           "--num", NUM, "--json"], base)
    fs = findings_of(out)
    data = json.loads(out)
    if code == 0 or len(fs) != 1:
        fail("C14", f"--nested 应读子目录 CHANGELOG 并报出跨仓号，实得退出码 {code}、"
                    f"{len(fs)} 条告警")
    elif "sub" not in str(data.get("file", "")):
        fail("C14", f"--nested 应读子目录下的 CHANGELOG，实得 {data.get('file')}")
    elif fs[0]["version"] != VERSION:
        fail("C14", f"嵌套节头的版本应是 {VERSION}，实得 {fs[0]['version']}")
    # 根文件回退形态：根 CHANGELOG 里的 `## [sub/v0.2.0]` 节
    base2 = root / "c14b"
    write_repo(base2, "## [sub/v0.2.0] — 2026-11-02\n\n"
                               "- #180：根文件里的嵌套节（关联 gormx#36）\n")
    code2, out2 = run_main(["--repo", REPO, "--version", VERSION, "--nested", "sub",
                             "--num", NUM, "--json"], base2)
    fs2 = findings_of(out2)
    if code2 == 0 or len(fs2) != 1:
        fail("C14", f"--nested 找不到子目录文件时应回退根 CHANGELOG 的 [sub/vX.Y.Z] 节，"
                    f"实得退出码 {code2}、{len(fs2)} 条告警")

    # ── C15：allowlist 读不懂 / 类别名写错 ⇒ 报错退出（豁免**静默失效**比误报更糟）──
    base = root / "c15"
    write_repo(base, CL_CROSS)
    bad_al = base / "bad.json"
    bad_al.write_text(json.dumps({"version": 1, "entries": [
        {"repo": REPO, "version": VERSION, "num": "36", "category": "cross_repoo",
         "bullet": "#180"}]}, ensure_ascii=False), encoding="utf-8")
    try:
        code, out = run_main(["--repo", REPO, "--version", VERSION, "--num", NUM,
                              "--allowlist", str(bad_al), "--json"], base)
        fail("C15", f"allowlist 的类别名写错 ⇒ 这条豁免永远匹配不上（静默失效）"
                    f"⇒ 必须报错退出，实得退出码 {code}")
    except SystemExit as e:
        if "category" not in str(e):
            fail("C15", f"报错要说清是类别名不对：{e}")
    broken = base / "broken.json"
    broken.write_text("{不是 JSON", encoding="utf-8")
    try:
        run_main(["--repo", REPO, "--version", VERSION, "--num", NUM,
                  "--allowlist", str(broken), "--json"], base)
        fail("C15", "allowlist 不是合法 JSON ⇒ 必须报错退出（读不懂的豁免表 = 没有豁免）")
    except SystemExit as e:
        if "JSON" not in str(e):
            fail("C15", f"报错要说清是 JSON 不合法：{e}")

    return fails


# ── 变异自证 ───────────────────────────────────────────────────────────
# 变异必须**真的改掉被检行为**：每个变异的**预期转红集合**都写死，
# 跑完逐条核对（红得**精确**：不该红的用例必须还绿着）。

_ORIG_CHECK_SECTION = check_writeback.check_section
_ORIG_SPLIT_BLOCKS = check_writeback.split_blocks
_ORIG_JUDGE_BLOCK = check_writeback.judge_block


def _mutate_existence_criterion():
    """M1：判据退化成「子串 `in` 判存在」。

    只问「这个号在**这一节里**出现过吗」（`f"#{num}" in section_text`），
    不问「它出现在**哪条 bullet** 里」⇒ 别的 bullet 上的号会被算到目标 bullet 头上。
    """
    def check_section(section_text, repo, version, num, line_offset=0):
        blocks = check_writeback.split_blocks(section_text.splitlines(),
                                              offset=line_offset)
        findings = []
        target_found = False
        for block in blocks:
            if not block.is_bullet:
                continue
            nums = check_writeback.bound_nums(block, repo)
            if num is not None:
                # ★ 存在性判据：整节里出现过这个号 ⇒ 这条 bullet 就算目标
                if f"#{num}" not in section_text:
                    continue
            elif not nums:
                continue
            own = nums[0] if nums else num
            target_found = True
            findings.extend(check_writeback.judge_block(block, repo, own, version))
        return findings, target_found

    check_writeback.check_section = check_section


def _mutate_no_block_scope():
    """M2：不做 bullet 块限定（扫全文）。

    检查范围从「这条 bullet 的块（首行 + 缩进续行）」放大到「**整节正文**」——
    归因（告警挂在哪条 bullet 上）不变，只有**范围**变了。
    ⇒ 别的 bullet 上的号、顶格段落里的号，都会被算到目标 bullet 头上。
    """
    def check_section(section_text, repo, version, num, line_offset=0):
        blocks = check_writeback.split_blocks(section_text.splitlines(),
                                              offset=line_offset)
        whole_lines = section_text.splitlines()
        findings = []
        target_found = False
        for block in blocks:
            if not block.is_bullet:
                continue
            nums = check_writeback.bound_nums(block, repo)
            if num is not None:
                if num not in nums:
                    continue
                own = num
            else:
                if not nums:
                    continue
                own = nums[0]
            target_found = True
            # ★ 扫全文：整节正文都算进这条 bullet（归因仍是这条 bullet）
            wide = check_writeback.Block(True, whole_lines, line_offset + 1)
            for f in check_writeback.judge_block(wide, repo, own, version):
                findings.append(f._replace(bullet=check_writeback.bullet_first_line(block),
                                           line=block.line))
        return findings, target_found

    check_writeback.check_section = check_section


def _mutate_gate_disabled():
    """M3：门禁**完全失效**（一条都不报）。"""
    check_writeback.judge_block = lambda block, repo, own_num, version: []


MUTATIONS = [
    ("判据退化成子串 in 判存在（不问位置）", _mutate_existence_criterion, {"C6"}),
    ("不做 bullet 块限定（整节当一条 bullet）", _mutate_no_block_scope, {"C5", "C6"}),
    # ⚠️ C12（CLI 端到端）**不在** M3 的转红集合里：它跑的是**子进程**，
    # 进程内的打桩够不着它 ⇒ 那是「变异够不着那条路」，不是「断言没牙齿」。
    ("门禁完全失效（一条都不报）", _mutate_gate_disabled,
     {"C1", "C2", "C3", "C4b", "C7", "C9", "C10", "C11", "C14"}),
]


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        fails = run_all(root, "base")
        if fails:
            print("✗ check_writeback 自测未过：")
            for cid, msg in fails:
                print(f"  [{cid}] {msg}")
            raise SystemExit(1)

        for i, (name, mutate, expect) in enumerate(MUTATIONS):
            try:
                mutate()
                fs = run_all(root, f"mut{i}")
            finally:
                check_writeback.check_section = _ORIG_CHECK_SECTION
                check_writeback.split_blocks = _ORIG_SPLIT_BLOCKS
                check_writeback.judge_block = _ORIG_JUDGE_BLOCK
            got = {cid for cid, _ in fs}
            if not got:
                print(f"✗ 变异「{name}」没有被抓住——这份断言没有牙齿")
                raise SystemExit(1)
            if got != expect:
                print(f"✗ 变异「{name}」转红得**不精确**：")
                print(f"    预期转红：{sorted(expect)}")
                print(f"    实得转红：{sorted(got)}")
                print(f"    多红：{sorted(got - expect)}／漏红：{sorted(expect - got)}")
                raise SystemExit(1)
            print(f"✓ 变异「{name}」被抓住（转红 {len(got)} 条：{sorted(got)}）")

    print(f"✓ check_writeback 自测通过：跨仓号/本仓其它号/裸号都抓得住；"
          f"PR 引用与顶格段落不误报；判据看**位置**不看存在性；"
          f"allowlist 与 --all/--json/CLI 都通；"
          f"变异自证 {len(MUTATIONS)}/{len(MUTATIONS)} 全部**精确**转红")


if __name__ == "__main__":
    main()
