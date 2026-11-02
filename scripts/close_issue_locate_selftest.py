#!/usr/bin/env python3
"""close_issue 回执「变更内容」定位能力自测（matrix-tools#153）。

**为什么要有这个自测**：#153 的现象是——回执的「变更内容」整段取自**被引版本的
CHANGELOG 版本节**，而版本节里常常没有记这条 issue 的修复 ⇒ 回执里那段文字
**与本 issue 无关**（读者照着回执找不到「这条到底改了什么」），而且**静默**：
脚本照发回执、照关单，没有任何提示。

修法是**按 issue 号在版本节里定位**：定位到就把那条条目单独列出来；定位不到
就**明说**（只提示、不阻断——存量已发回执里有 21% 命中「版本节未提到」，
硬阻断会把它们全挡住）。

**这份自测要证明的东西**：定位器**抓得住**自己要防的东西。定位一旦被改成
「恒不命中」或「恒命中」，回执就退回到旧行为（要么永远说定位不到、要么永远
指着第一条），而这两种退化在真实使用里都**看不出来**（回执照发、关单照关）。

**反证怎么构造**（本自测必须能在下列变异下转红）：

  1. 定位器恒不命中（entries 永远为空）⇒ 「定位得到」的断言必须失败；
  2. 定位器恒命中（永远返回第一条）⇒ 「定位不到」的断言必须失败；
  3. 不排除 `PR #N`（PR 与 issue 共用编号空间）⇒ 「PR 号不算 issue 引用」必须失败；
  4. 「只在叙述里提到」也说成「未提到」⇒ 两种说法必须被分开断言。

**#158 追加的判据**（回执不许做事实断言）：

  5. 回执又写上「**不含本条修复**」⇒ 「版本节没点名」推不出「这一版不含它」
     （一版可以捆绑多条修复、只点名其中一部分 ⇒ 修复就在这一版里）⇒ 必须转红；
  6. **Release 正文证据被丢掉**（第四种情形被说成「无法判定」）⇒ 必须转红；
  7. 没有证据却断言「很可能就在这一版里」⇒ 必须转红。

**无副作用、不联网**：纯函数（定位 + 回执正文组装），不碰 GitHub。
（Release 正文证据由调用方传入 `release_evidence`，本自测直接喂 True/False/None，
不联网。）

用法：python3 scripts/close_issue_locate_selftest.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import close_issue  # noqa: E402
import gen_releases  # noqa: E402


# 版本节形态一：bullet 末尾带 `（#153）`，且带子 bullet（子 bullet 属于同一条目）
SECTION_WITH_ENTRY = """### fix(tools)：close_issue 回执能定位到本条修复（#153）

回执的「变更内容」整段取自版本节 ⇒ 版本节没记这条时，回执引的是别的修复。

- fix(tools): 回执按 issue 号定位版本节条目，定位不到就明说（#153）
  - 定位到 ⇒ 条目单独列出，其余变更折叠
  - 定位不到 ⇒ 提示版本节漏记
- feat(tools): 无关的另一条变更（#120）

关联问题单：matrix-tools#153。
"""

# 版本节形态二：只有别的 issue
SECTION_OTHER_ISSUE = """### fix(tools)：别的修复（#120）

- fix(tools): 与本 issue 无关的改动（#120）
"""

# 版本节形态三：只在叙述文字里提到本 issue（没有绑定到具体条目）
SECTION_PROSE_ONLY = """### fix(tools)：别的修复（#120）

正文里提到了 matrix-tools#153，但它没有绑定到任何具体条目。

- fix(tools): 与本 issue 无关的改动（#120）
"""

# 版本节形态四：只提到 PR 号（PR 与 issue 共用编号空间 ⇒ 不能当 issue 引用）
SECTION_PR_ONLY = """### docs(changelog)：补记（PR #153）

- docs(changelog): 补记 v0.1.21 的漏记（PR #153）
"""


def all_assertions() -> list[str]:
    """跑一遍全部断言，返回 failures 列表（空 = 全过）。"""
    failures: list[str] = []
    locate = gen_releases.locate_issue_entries
    build = close_issue.build_change_content

    # ── 定位器 ────────────────────────────────────────────────
    loc = locate(SECTION_WITH_ENTRY, "matrix-tools", "153")
    if len(loc.entries) != 1:
        failures.append(f"定位器：期望命中 1 条，实际 {len(loc.entries)} 条：{loc.entries}")
    else:
        if "- fix(tools): 回执按 issue 号定位版本节条目" not in loc.entries[0]:
            failures.append(f"定位器：命中的不是本条修复：{loc.entries[0]!r}")
        if "定位到 ⇒ 条目单独列出" not in loc.entries[0]:
            failures.append("定位器：命中的条目没带上它的子 bullet（条目块应含续行）")
        if "回执按 issue 号定位版本节条目" in loc.rest:
            failures.append("定位器：已命中的条目仍留在 rest 里（会被重复展示）")
        if "无关的另一条变更" not in loc.rest:
            failures.append("定位器：rest 丢了没命中的条目")
    if not loc.mentioned:
        failures.append("定位器：命中了却没标 mentioned")

    loc2 = locate(SECTION_OTHER_ISSUE, "matrix-tools", "153")
    if loc2.entries:
        failures.append(f"定位器：别的 issue 的条目被误命中：{loc2.entries}")
    if loc2.mentioned:
        failures.append("定位器：只提到别的 issue，却标成 mentioned")

    loc3 = locate(SECTION_PROSE_ONLY, "matrix-tools", "153")
    if loc3.entries:
        failures.append(f"定位器：叙述文字里的提及被当成了具体条目：{loc3.entries}")
    if not loc3.mentioned:
        failures.append("定位器：叙述文字里提到了却没标 mentioned")

    loc4 = locate(SECTION_PR_ONLY, "matrix-tools", "153")
    if loc4.entries:
        failures.append(f"定位器：「PR #153」被当成了 issue 引用：{loc4.entries}")
    if loc4.mentioned:
        failures.append("定位器：「PR #153」被当成了 issue 提及（PR 与 issue 共用编号空间）")

    # 数字边界：#1530 不是 #153
    loc5 = locate("- fix(x): 见 #1530\n", "matrix-tools", "153")
    if loc5.entries or loc5.mentioned:
        failures.append("定位器：#1530 被当成了 #153（数字边界没守住）")

    # PR **连号列举**：「PR #148、#149」里的两个号都是 PR 号（复核发现的洞）
    loc6 = locate("- docs(x): 补记（PR #148、#153）\n", "matrix-tools", "153")
    if loc6.entries or loc6.mentioned:
        failures.append(f"定位器：「PR #148、#153」里的 #153 被当成了 issue 引用：{loc6.entries}")
    if not loc6.pr_only:
        failures.append("定位器：「只以 PR 形态出现」没有标出来（回执答不出「为什么定位不到」）")

    # 同行既有 PR 又有 issue 引用 ⇒ issue 那个号必须仍然算数
    loc7 = locate("- fix(x): PR #149 修了 #153\n", "matrix-tools", "153")
    if not loc7.entries:
        failures.append("定位器：同行里「PR #149 修了 #153」的 #153 被误当成 PR 号")

    # 只以 PR 形态出现 ⇒ pr_only 态（不是「提到」）
    loc8 = locate(SECTION_PR_ONLY, "matrix-tools", "153")
    if loc8.mentioned:
        failures.append("定位器：PR 形态被算成了「提到」")
    if not loc8.pr_only:
        failures.append("定位器：PR 形态没标成 pr_only")

    # ── #172：跨仓同号**不得**被当成本仓的号 ──────────────────────
    #   弱形态 `#N` 前面若是**别的仓名**（`pkg#46` / `concurrency#56`）
    #   ⇒ 那是**别仓**的 issue 号 ⇒ 不是本仓 #46 ⇒ 不得绑定、不得算 mentioned。
    #   （issue 号在 org 内**大量重号** ⇒ 每仓都有 #46/#56/#85… ⇒ 极易踩中）
    loc9 = locate("- fix(x): 实测 pkg#46 / pkg#48 / concurrency#56 全绿\n",
                  "matrix-tools", "46")
    if loc9.entries:
        failures.append(f"定位器：跨仓号 pkg#46 被当成本仓 #46 绑定了（张冠李戴）："
                        f"{loc9.entries}")
    if loc9.mentioned:
        failures.append("定位器：跨仓号 pkg#46 被算成本仓 #46 的「提到」")
    if loc9.pr_only:
        failures.append("定位器：跨仓号 pkg#46 被算成「只以 PR 形态出现」"
                        "（它是别仓的 issue 号，不是 PR 号）")

    # 强形态不受影响：`matrix-tools#153` 仍必须绑定
    loc10 = locate("- fix(x): 修好了（matrix-tools#153）\n", "matrix-tools", "153")
    if not loc10.entries:
        failures.append("定位器：强形态 matrix-tools#153 没绑定（左边界加过头了）")

    # 真引用仍必须绑定：`（#153）` / `#153：…`
    loc11 = locate("- fix(x): 修好了（#153）\n", "matrix-tools", "153")
    if not loc11.entries:
        failures.append("定位器：真引用「（#153）」没绑定（左边界加过头了）")
    loc12 = locate("- #153：修好了某件事\n", "matrix-tools", "153")
    if not loc12.entries:
        failures.append("定位器：bullet 首行的「#153：…」没绑定")

    # 两处口径必须**一致**：`mentions_issue` 与 `_pr_only_mentions` 同源
    # （只改一处 ⇒ 出现「弱形态不认了、但 pr_only 仍认」的**口径分裂**
    #   ⇒ 那正是 #153/#154/#158 这一族的根因：两边读法分裂）
    from gen_releases import mentions_issue, _pr_only_mentions  # noqa: E402
    cross_line = "- fix(x): 实测 pkg#46 全绿\n"
    if mentions_issue(cross_line, "matrix-tools", "46"):
        failures.append("定位器：mentions_issue 把跨仓号 pkg#46 当成本仓 #46")
    if not mentions_issue(cross_line, "pkg", "46"):
        failures.append("定位器：mentions_issue 对**本仓** pkg 的 #46 也不认了"
                        "（跨仓判据把真引用也误伤了）")
    if _pr_only_mentions(cross_line, "46"):
        failures.append("定位器：_pr_only_mentions 把跨仓号 pkg#46 当成 PR 形态"
                        "（两处口径分裂了）")

    # ── 回执正文 ──────────────────────────────────────────────
    body = build(SECTION_WITH_ENTRY, "matrix-tools", "153", "v0.1.30")
    if "**本条修复**" not in body:
        failures.append(f"回执：定位到了却没写「本条修复」：{body[:200]}")
    if "回执按 issue 号定位版本节条目" not in body:
        failures.append("回执：没带出本条修复的条目")
    if "无关的另一条变更" not in body:
        failures.append("回执：其余变更没带出来（读者看不到这一版的全貌）")
    if "未提到本 issue" in body:
        failures.append("回执：明明定位到了却报「未提到本 issue」")
    if "**不含本条修复**" in body:
        failures.append("回执：明明定位到了却说「不含本条修复」")

    body2 = build(SECTION_OTHER_ISSUE, "matrix-tools", "153", "v0.1.30")
    if "该版本节未提到本 issue" not in body2:
        failures.append(f"回执：版本节没提到本 issue 时没有明说：{body2[:200]}")
    # #158：旧实现在这里断言「**不含本条修复**」——那是关于**该版本内容**的事实断言，
    # 而「版本节没点名」**推不出**它（一版可以捆绑多条修复、只点名其中一部分 ⇒
    # 修复就在这一版里）⇒ 现在只说「无法从版本节判定」。
    if "**不含本条修复**" in body2:
        failures.append("回执：版本节没提到本 issue 时断言了「不含本条修复」——"
                        "这是与事实可能相反的断言（#158）")
    if "无法从版本节判定" not in body2:
        failures.append("回执：版本节没提到本 issue 时没有说「无法从版本节判定」")
    if "很可能就在这一版里" in body2:
        failures.append("回执：没有 Release 正文证据却断言「很可能就在这一版里」")
    if "**本条修复**" in body2:
        failures.append("回执：定位不到却写成了「本条修复」")
    if "与本 issue 无关的改动" not in body2:
        failures.append("回执：定位不到时也没给出该版本的全部变更")

    # #158 的**第四种**：版本节没提，但 **Release 正文点名了本 issue**
    # ⇒ 本条修复很可能就在这一版里（版本节漏记）——判据依据不在版本节里。
    body_b = build(SECTION_OTHER_ISSUE, "matrix-tools", "153", "v0.1.30", True)
    if "Release 正文点名了本 issue" not in body_b:
        failures.append(f"回执：Release 正文点名了本 issue 时没有说明（第四种情形）：{body_b[:200]}")
    if "很可能就在这一版里" not in body_b:
        failures.append("回执：Release 正文点名了本 issue 时没有说「很可能就在这一版里」")
    if "**不含本条修复**" in body_b:
        failures.append("回执：有 Release 正文证据却仍断言「不含本条修复」")
    if "该版本节未提到本 issue" not in body_b:
        failures.append("回执：第四种情形里没有说「该版本节未提到本 issue」")

    # #158：**证据为 False**（Release 正文读了但没点名）时也不许断言情形 B
    body_c = build(SECTION_OTHER_ISSUE, "matrix-tools", "153", "v0.1.30", False)
    if "很可能就在这一版里" in body_c:
        failures.append("回执：Release 正文没点名却断言「很可能就在这一版里」")
    if "**不含本条修复**" in body_c:
        failures.append("回执：Release 正文没点名时断言了「不含本条修复」（#158）")

    body3 = build(SECTION_PROSE_ONLY, "matrix-tools", "153", "v0.1.30")
    if "未绑定到具体条目" not in body3:
        failures.append(f"回执：「只在叙述里提到」的说法缺失：{body3[:200]}")
    if "该版本节未提到本 issue" in body3:
        failures.append("回执：「只在叙述里提到」被说成了「未提到」——两种说法必须分开")

    body_pr = build(SECTION_PR_ONLY, "matrix-tools", "153", "v0.1.30")
    if "只以 PR 形态出现过" not in body_pr:
        failures.append(f"回执：PR 形态的说法缺失：{body_pr[:200]}")
    if "该版本节未提到本 issue" in body_pr:
        failures.append("回执：PR 形态被说成了「未提到」——两种说法必须分开")
    # #158：PR 形态分支原来断言「本 issue 的修复**不在这一节**」——同样是事实断言
    if "修复**不在这一节**" in body_pr:
        failures.append("回执：PR 形态分支断言了「修复不在这一节」——与事实可能相反（#158）")

    # 定位不到也必须给出正文（**只提示不阻断**）
    for name, b in (("未提到", body2), ("未绑定到条目", body3)):
        if not b.strip():
            failures.append(f"回执：{name} 的情形下正文是空的（定位不到也不该阻断回执）")

    # 没有 CHANGELOG 时回退到链接
    body4 = build(None, "matrix-tools", "153", "v0.1.30")
    if "CHANGELOG.md" not in body4:
        failures.append(f"回执：没有 CHANGELOG 时没有回退到链接：{body4[:200]}")

    return failures


def run_mutation(name: str, mutate) -> list[str]:
    """在变异下跑断言，返回 failures（空 = 变异没被抓到）。"""
    saved = (gen_releases.mentions_issue, gen_releases.locate_issue_entries)
    saved_pr_only = gen_releases._pr_only_mentions
    saved_build = close_issue.build_change_content
    try:
        mutate()
        return all_assertions()
    finally:
        gen_releases.mentions_issue, gen_releases.locate_issue_entries = saved
        # ⚠️ `_pr_only_mentions` 也必须还原（#172 新增的打桩点）
        # ⇒ 否则它会**污染后续变异** ⇒ 后面的变异结果不可信。
        gen_releases._pr_only_mentions = saved_pr_only
        close_issue.build_change_content = saved_build


def _mutate_never_hit():
    gen_releases.locate_issue_entries = lambda section, repo, num: gen_releases.IssueLocation(
        entries=[], rest=section, mentioned=False, pr_only=False)


def _mutate_always_hit():
    def _f(section, repo, num):
        first = next((l for l in section.splitlines()
                      if gen_releases.BULLET_RE.match(l)), "")
        return gen_releases.IssueLocation(entries=[first] if first else [],
                                          rest="", mentioned=True, pr_only=False)
    gen_releases.locate_issue_entries = _f


def _mutate_pr_counts():
    def _f(line: str, repo: str, num: str) -> bool:
        return bool(re.search(r"#" + re.escape(num) + r"\b", line))
    gen_releases.mentions_issue = _f


def _mutate_merge_two_messages():
    orig = gen_releases.locate_issue_entries

    def _f(section, repo, num):
        loc = orig(section, repo, num)
        # 把「只在叙述里提到」也降级成「完全没提到」
        return gen_releases.IssueLocation(entries=loc.entries, rest=loc.rest,
                                          mentioned=False, pr_only=loc.pr_only)
    gen_releases.locate_issue_entries = _f


def _mutate_restore_false_assertion():
    """把「无法从版本节判定」换回旧的事实断言「**不含本条修复**」（#158）。"""
    orig = close_issue.build_change_content

    def _f(sec, repo, num, tag, release_evidence=None):
        return orig(sec, repo, num, tag, release_evidence).replace(
            "本条修复是否在这一版里**无法从版本节判定**", "**不含本条修复**")
    close_issue.build_change_content = _f


def _mutate_ignore_release_evidence():
    """丢掉 Release 正文证据（第四种情形被说成「无法判定」）。"""
    orig = close_issue.build_change_content

    def _f(sec, repo, num, tag, release_evidence=None):
        return orig(sec, repo, num, tag, None)
    close_issue.build_change_content = _f


def _mutate_claim_case_b_without_evidence():
    """没有证据也断言情形 B（「很可能就在这一版里」）。"""
    orig = close_issue.build_change_content

    def _f(sec, repo, num, tag, release_evidence=None):
        return orig(sec, repo, num, tag, True)
    close_issue.build_change_content = _f


def _mutate_cross_repo_counts():
    """#172：弱形态**不带左边界** ⇒ `pkg#46` 被当成本仓 #46 ⇒ 张冠李戴。"""
    def _f(line: str, repo: str, num: str) -> bool:
        if re.search(re.escape(f"{repo}#{num}") + r"\b", line):
            return True
        stripped = gen_releases.PR_REF_RE.sub(" ", line)
        return bool(re.search(r"#" + re.escape(num) + r"\b", stripped))
    gen_releases.mentions_issue = _f


def _mutate_pr_only_cross_repo():
    """#172：`_pr_only_mentions` 认跨仓号 ⇒ 与 `mentions_issue` 口径分裂。

    ⚠️ **这个变异必须有牙齿**：`pr_only` 分支只在「`mentions_issue` 不命中、
    但 `_pr_only_mentions` 命中」时可达 ⇒ 语料必须让两者**判据不同**。

    实测（2026-11-02）：**旧逻辑的 `_pr_only_mentions` 对 `pkg#46` 本来就返回
    False** —— 它的第一道检查是「抹掉 PR 引用后**还在** ⇒ 它是 issue 引用 ⇒
    return False」⇒ 跨仓号（不是 PR 引用）会通过这道检查 ⇒ 旧逻辑同样不把
    跨仓号判成 pr_only。
    ⇒ 所以 #172 正文里「`_pr_only_mentions` 对 `pkg#46` 也会返回 True」这句
    **不准确**（实测证伪）；pr_only 侧**本来就**不误判跨仓号。
    ⇒ 真正需要一起改的是**口径一致性**（不是「pr_only 也错」）——
    两边共用同一个 `weak_issue_re` ⇒ 未来任一侧改动不会造成分裂。

    为让本变异**真的改了行为**（否则「没被抓住」只说明变异无效），
    这里打桩成「跨仓号 ⇒ pr_only 命中」，与 `mentions_issue` 的判定**相反**
    ⇒ 分裂 ⇒ `pr_only` 分支可达 ⇒ 判据能抓住它。
    """
    def _pr(line: str, num: str) -> bool:
        return bool(re.search(r"#" + re.escape(num) + r"\b", line))
    gen_releases._pr_only_mentions = _pr


MUTATIONS = [
    ("定位器恒不命中", _mutate_never_hit),
    ("定位器恒命中", _mutate_always_hit),
    ("PR 号被当 issue 引用", _mutate_pr_counts),
    ("「未绑定到条目」被说成「未提到」", _mutate_merge_two_messages),
    ("回执又写上「不含本条修复」假断言", _mutate_restore_false_assertion),
    ("Release 正文证据被丢掉", _mutate_ignore_release_evidence),
    ("没证据却断言「很可能就在这一版里」", _mutate_claim_case_b_without_evidence),
    ("跨仓号被当本仓号（#172 原始缺陷）", _mutate_cross_repo_counts),
    ("pr_only 认跨仓号 ⇒ 口径分裂（#172）", _mutate_pr_only_cross_repo),
]


def main() -> None:
    failures = all_assertions()
    if failures:
        print(f"❌ 断言失败（{len(failures)} 条）：")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("✓ 定位与回执正文断言全过")

    missed: list[str] = []
    for name, mutate in MUTATIONS:
        f = run_mutation(name, mutate)
        if not f:
            missed.append(name)
            print(f"✗ 变异「{name}」没有被抓住——这份断言没有牙齿")
        else:
            print(f"✓ 变异「{name}」被抓住（{len(f)} 条断言转红）")
    if missed:
        raise SystemExit(1)
    print(f"✓ 变异自证 {len(MUTATIONS)}/{len(MUTATIONS)} 全部转红")


if __name__ == "__main__":
    main()
