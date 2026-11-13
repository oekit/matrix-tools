#!/usr/bin/env python3
"""检查面 I（matrix-tools#318）自测：--issue 的修复必须是待打 tag 的祖先。

**为什么要有这个自测**：timertask#217 实锤的**声称劈叉**——prewrite CHANGELOG
（声称含 #197）→ 同分钟 tag v0.5.3 → 修复 PR 16 分钟后才合入 main ⇒
消费者按 CHANGELOG 定位 `go get @v0.5.3` 拿不到声称的修复；回执
「已在 v0.5.3 修复并发布」不实。检查面 I 在打 tag 前拦这个形态，
本自测断言拦得住。

**口径**（与实现一致，每条独立）：
  ① 同仓引用 PR 仍 OPEN ⇒ 红（先合再 tag）
  ② 同仓 MERGED 且是 HEAD 祖先 ⇒ 放行
  ③ 同仓 MERGED 但**不是**祖先 ⇒ 红（声称劈叉——tag 里没有）
  ④ 定位不到引用 PR ⇒ ⚠ 放行（纯手改/直推，判不了 ≠ 红）
  ⑤ 跨仓 OPEN ⇒ 仅提示（本仓无祖先关系可判）
  ⑥ 跨仓 MERGED ⇒ 放行（合了即认）
  ⑦ 引用 PR 全 CLOSED 未合 ⇒ ⚠ 放行（改道直推可能）
  ⑧ timeline/PR 状态读不到 ⇒ 红（fail closed——读不到 ≠ 没有）
  ⑨ **调用点**：main() 真的调了 check_issue_fix_ancestor（ast 断言，
     不依赖文本排版——「函数对、调用点错」是纯函数自测的天然盲区）

**变异自证**（--mutate，构造退化 ⇒ 对应用例必须转红）：
  M1 OPEN 不拦（删 ① 的 append）⇒ 用例①转红
  M2 祖先判定恒真（删 ③ 的非零分支）⇒ 用例③转红

**无副作用、不联网**：gh/git 全部 monkeypatch stub。

用法：python3 scripts/release_train_issue_ancestor_selftest.py [--mutate]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_train  # noqa: E402


class _Stub:
    """可编程的 sh/issue_fix_prs/pr_merge_state 三件套。"""

    def __init__(self, prs=None, states=None, ancestor_rc=0):
        self.prs = prs or {}
        self.states = states or {}
        self.ancestor_rc = ancestor_rc
        self.git_calls: list[list[str]] = []

    def sh(self, args, cwd=None, check=True, **kw):
        class R:
            def __init__(self, rc, out=""):
                self.returncode = rc
                self.stdout = out
        if args[:2] == ["git", "rev-parse"]:
            return R(0, "deadbeef\n")
        if args[:2] == ["git", "merge-base"]:
            self.git_calls.append(args)
            return R(self.ancestor_rc)
        raise AssertionError(f"未预期的 sh 调用: {args}")

    def fix_prs(self, repo, num):
        return self.prs.get(f"{repo}#{num}", [])

    def merge_state(self, repo, num):
        return self.states[num]


def _run(repo="timertask", prs=None, states=None, ancestor_rc=0, issues=("timertask#197",)):
    stub = _Stub(prs, states, ancestor_rc)
    old = (release_train.issue_fix_prs, release_train.pr_merge_state, release_train.sh)
    release_train.issue_fix_prs = stub.fix_prs
    release_train.pr_merge_state = stub.merge_state
    release_train.sh = stub.sh
    failures: list[str] = []
    try:
        release_train.check_issue_fix_ancestor(
            repo, Path("/tmp"), list(issues), failures)
    finally:
        release_train.issue_fix_prs, release_train.pr_merge_state, release_train.sh = old
    return failures, stub


def assertions() -> list[str]:
    bad: list[str] = []
    f, _ = _run(prs={"timertask#197": [209]},
                states={209: {"state": "OPEN", "mergeCommit": {}}})
    if not any("仍 OPEN" in x for x in f):
        bad.append("① 同仓 OPEN PR 应拦（先合再 tag）")
    f, s = _run(prs={"timertask#197": [209]},
                states={209: {"state": "MERGED",
                              "mergeCommit": {"oid": "abc123def456"}}},
                ancestor_rc=0)
    if f:
        bad.append(f"② MERGED+祖先应放行，却红了：{f}")
    if not any(a[3] == "abc123def456" for a in s.git_calls):
        bad.append("② 应对 mergeCommit 跑 merge-base --is-ancestor")
    f, _ = _run(prs={"timertask#197": [209]},
                states={209: {"state": "MERGED",
                              "mergeCommit": {"oid": "abc123def456"}}},
                ancestor_rc=1)
    if not any("不是待打 tag 的祖先" in x for x in f):
        bad.append("③ MERGED 但非祖先应拦（声称劈叉）")
    f, _ = _run(prs={})
    if f:
        bad.append(f"④ 定位不到 PR 应放行（判不了 ≠ 红）：{f}")
    f, _ = _run(repo="core", issues=("timertask#197",),
                prs={"timertask#197": [209]},
                states={209: {"state": "OPEN", "mergeCommit": {}}})
    if f:
        bad.append(f"⑤ 跨仓 OPEN 应仅提示不红：{f}")
    f, _ = _run(repo="core", issues=("timertask#197",),
                prs={"timertask#197": [209]},
                states={209: {"state": "MERGED",
                              "mergeCommit": {"oid": "abc"}}})
    if f:
        bad.append(f"⑥ 跨仓 MERGED 应放行：{f}")
    f, _ = _run(prs={"timertask#197": [209]},
                states={209: {"state": "CLOSED", "mergeCommit": {}}})
    if f:
        bad.append(f"⑦ 全 CLOSED 未合应放行（改道直推可能）：{f}")
    return bad


def fail_closed_assertions() -> list[str]:
    """⑧ 读不到 ⇒ 红（fail closed）。"""
    bad: list[str] = []
    import ghsafe

    def boom(repo, num):
        raise ghsafe.GhReadError("gh api timeline 正文为空")

    old = (release_train.issue_fix_prs, release_train.sh)
    stub = _Stub()
    release_train.issue_fix_prs = boom
    release_train.sh = stub.sh          # 实现先跑 rev-parse——sh 也得 stub
    try:
        failures: list[str] = []
        release_train.check_issue_fix_ancestor(
            "timertask", Path("/tmp"), ["timertask#197"], failures)
        if not failures:
            bad.append("⑧ timeline 读不到应 fail closed")
    finally:
        release_train.issue_fix_prs, release_train.sh = old
    return bad


def call_site_assertions() -> list[str]:
    """⑨ main() 必须真的调 check_issue_fix_ancestor（ast 断言）。"""
    bad: list[str] = []
    tree = ast.parse(Path(release_train.__file__).read_text(encoding="utf-8"))
    called = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "check_issue_fix_ancestor"
        for n in ast.walk(tree))
    if not called:
        bad.append("⑨ main() 未调用 check_issue_fix_ancestor——检查面 I 没接线")
    return bad


def mutate() -> bool:
    """变异自证：把「OPEN 拦截」或「非祖先拦截」删掉 ⇒ 对应用例必须转红。"""
    src = Path(release_train.__file__).read_text(encoding="utf-8")
    mutations = [
        ("M1 OPEN 不拦",
         "f\"{ref}: 修复 PR #{pr} 仍 OPEN——先合入再 tag，\"",
         "f\"\""),
        ("M2 非祖先不拦",
         "f\"{ref}: 修复 PR #{pr}（{sha[:8]}）已合但**不是待打 tag 的祖先**——\"",
         "f\"\""),
    ]
    ok = True
    for name, old, new in mutations:
        if old not in src:
            print(f"  [mutate] {name}: 锚文本找不到——实现已变，请回写自测")
            ok = False
            continue
        patched = src.replace(old, new)
        ns: dict = {"__file__": str(Path(release_train.__file__))}
        exec(compile(patched, "release_train_mutated", "exec"), ns)
        case = ("timertask#197", [209])
        if "M1" in name:
            bad = _run_mut(ns, case, {"state": "OPEN", "mergeCommit": {}})
            if not bad:
                print(f"  [mutate] {name}: 用例①仍绿 ⇒ 自测抓不到退化")
                ok = False
        else:
            bad = _run_mut(ns, case, {"state": "MERGED",
                                      "mergeCommit": {"oid": "abc123def456"}}, rc=1)
            if not bad:
                print(f"  [mutate] {name}: 用例③仍绿 ⇒ 自测抓不到退化")
                ok = False
        print(f"  [mutate] {name}: 已验证转红")
    return ok


def _run_mut(ns, case, state, rc=0):
    """在变异后的模块命名空间里跑对应用例，返回失败清单。"""
    stub = _Stub({case[0]: case[1]}, {case[1][0]: state}, rc)
    ns["issue_fix_prs"] = stub.fix_prs
    ns["pr_merge_state"] = stub.merge_state
    ns["sh"] = stub.sh
    failures: list[str] = []
    ns["check_issue_fix_ancestor"]("timertask", Path("/tmp"), [case[0]], failures)
    return failures


def main() -> int:
    bad = assertions() + fail_closed_assertions() + call_site_assertions()
    if "--mutate" in sys.argv:
        if not mutate():
            bad.append("变异自证失败")
    if bad:
        print("✗ 检查面 I（#318）自测未过：")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("✓ 检查面 I（#318）自测通过：OPEN 拦 / 祖先放行 / 非祖先拦 / "
          "定位不到放行 / 跨仓口径 / fail-closed / 调用点接线 全部符合契约")
    return 0


if __name__ == "__main__":
    sys.exit(main())
