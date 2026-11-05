#!/usr/bin/env python3
"""tag 快照 CHANGELOG 门禁自测（#234）。

注入读取层（内存 dict 当「GitHub 内容面」），不联网、无副作用。判据面：

  A. **逃逸主形态**（#234 实录）：tag 快照 CHANGELOG 顶部停在上一版 ⇒ 必须判红；
  B. tag 快照含本版段 ⇒ 通过；
  C. **嵌套 tag 三态**：根面含 `## [sub/vX.Y.Z]` ⇒ 通过；根面缺但嵌套文件
     含（带前缀或裸版本头）⇒ 通过；两面都缺 ⇒ 判红；
  D. tag 快照整个无 CHANGELOG.md ⇒ 判红（不是「无文件=无漂移」）；
  E. **读取失败 ⇒ 退出码 2**（判不了 ≠ 通过；不得伪装成「零漂移」绿）；
  F. 无 tag 的仓 ⇒ 合法跳过；
  G. 多嵌套前缀各取最新 ⇒ 全部被核对（不许只查顶层）。

**变异自证（--mutate）**：对门禁注入变异体，断言「精确转红集合」：
  M1. 只查顶层 tag（跳过嵌套面）⇒ G 转红；
  M2. 段头匹配放宽成「文件里出现版本号字串即可」⇒ A 转绿（防「缺段」误判
      通过：上一版段尾的 `关联问题单` 里常含旧版本号，字串匹配是假通过）；
  M3. 读失败吞成 None（=「无文件」）⇒ E 转红（fail-open 复辟必被抓）。

用法：
    python3 scripts/changelog_tag_snapshot_selftest.py           # 正常：全绿
    python3 scripts/changelog_tag_snapshot_selftest.py --mutate  # 变异：精确转红
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import changelog_tag_snapshot_gate as gate  # noqa: E402

_RESULTS: dict[str, bool] = {}
_FAILURES: list[str] = []


def _record(cid: str, good: bool, msg: str = "") -> bool:
    _RESULTS[cid] = good
    if not good and msg:
        _FAILURES.append(msg)
    return good


# ---------- fixture ----------
#
# 「GitHub 内容面」：{(repo, ref, path): 文本}。缺失 = 404。
# reads_fail 非空 = 这些键读失败（抛 RuntimeError）。

def make_read_fn(files: dict, fails: set | None = None):
    fails = fails or set()

    def read_fn(repo: str, ref: str, path: str) -> str | None:
        if (repo, ref, path) in fails:
            raise RuntimeError(f"{repo}@{ref}:{path}: boom")
        return files.get((repo, ref, path))
    return read_fn


def make_tags_fn(tags_by_repo: dict):
    def tags_fn(repo: str) -> list[str]:
        if repo not in tags_by_repo:
            raise RuntimeError(f"{repo}: 远端 tag 读不到（fixture 未定义）")
        return tags_by_repo[repo]
    return tags_fn


def prev_snap(version: str, prev_version: str) -> str:
    """tag 快照的「缺本版段」形态：顶部停在上一版；正文里含本版版本号的
    字串（模拟上一版段的关联问题单/前置说明——正是字串匹配会假通过的来源）。"""
    return f"## [{prev_version}] — 2026-10-23\n\n- 上一版内容（本版 {version} 前置说明）\n"


def cur_snap(version: str, prev_version: str) -> str:
    """tag 快照含本版段的正确形态。"""
    return (f"## [{version}] — 2026-11-06\n\n- 本版内容（#234 的反面）\n\n"
            + prev_snap(version, prev_version))


PREV_OK = prev_snap("v0.12.6", "v0.12.5")           # toolgate v0.12.6 的逃逸形态
CUR_OK = cur_snap("v0.12.6", "v0.12.5")


def base_fixture() -> tuple[dict, dict]:
    """toolgate：主形态仓（v0.12.6 快照缺本版段）；others：全绿仓。"""
    files = {
        ("toolgate", "v0.12.6", "CHANGELOG.md"): PREV_OK,           # A：逃逸主形态
        ("toolgate", "v0.12.5", "CHANGELOG.md"): PREV_OK,
        ("others", "v1.0.0", "CHANGELOG.md"): cur_snap("v1.0.0", "v0.9.0"),  # B
        ("nested", "sub/v0.2.9", "CHANGELOG.md"): PREV_OK,          # C1：根面缺
        ("nested", "sub/v0.2.9", "sub/CHANGELOG.md"): cur_snap("sub/v0.2.9", "sub/v0.2.8"),
        ("nested2", "sub/v0.3.0", "CHANGELOG.md"): PREV_OK,         # C2：嵌套面裸版本头
        ("nested2", "sub/v0.3.0", "sub/CHANGELOG.md"): cur_snap("v0.3.0", "v0.2.9"),
        ("nested3", "sub/v0.4.0", "CHANGELOG.md"): PREV_OK,         # C3：两面都缺
        ("nested3", "sub/v0.4.0", "sub/CHANGELOG.md"): PREV_OK,
        ("nofile", "v2.0.0", "CHANGELOG.md"): None,                 # D：tag 无 CHANGELOG
    }
    tags = {
        "toolgate": ["v0.12.5", "v0.12.6"],
        "others": ["v0.9.0", "v1.0.0"],
        "nested": ["sub/v0.2.8", "sub/v0.2.9"],
        "nested2": ["sub/v0.3.0"],
        "nested3": ["sub/v0.4.0"],
        "nestedmany": ["a/v1.1.0", "a/v1.2.0", "b/v2.0.0"],         # G：多前缀
        ("nofile"): ["v2.0.0"],
        "notags": [],                                               # F：无 tag 合法
    }
    files[("nestedmany", "a/v1.2.0", "CHANGELOG.md")] = cur_snap("a/v1.2.0", "a/v1.1.0")
    files[("nestedmany", "b/v2.0.0", "CHANGELOG.md")] = cur_snap("b/v2.0.0", "b/v1.9.0")
    return files, tags


def run_case(name: str, files, tags, expect_exit: int, fails: set | None = None) -> None:
    rc = gate.run(list(tags.keys()), read_fn=make_read_fn(files, fails), tags_fn=make_tags_fn(tags))
    _record(name, rc == expect_exit, f"{name}: exit={rc}, want {expect_exit}")


def main() -> int:
    mutate = "--mutate" in sys.argv
    files, tags = base_fixture()

    def repo_files(*repos: str) -> dict:
        return {k: v for k, v in files.items() if k[0] in repos}

    def repo_tags(*repos: str) -> dict:
        return {k: v for k, v in tags.items() if k in repos}

    if not mutate:
        # A：逃逸主形态（全量跑 + 单仓跑都必须红）
        run_case("A_escape_shape", files, tags, gate.EXIT_DRIFT)
        rc = gate.run(["toolgate"], read_fn=make_read_fn(files), tags_fn=make_tags_fn(tags))
        _record("A_target_repo", rc == gate.EXIT_DRIFT, f"A 单仓: exit={rc}")
        run_case("B_ok", repo_files("others"), repo_tags("others"), gate.EXIT_OK)
        run_case("C1_root_face", repo_files("nested"), repo_tags("nested"), gate.EXIT_OK)
        run_case("C2_nested_bare", repo_files("nested2"), repo_tags("nested2"), gate.EXIT_OK)
        run_case("C3_both_missing", repo_files("nested3"), repo_tags("nested3"), gate.EXIT_DRIFT)
        run_case("D_no_changelog_file", repo_files("nofile"), repo_tags("nofile"), gate.EXIT_DRIFT)
        run_case("F_no_tags", repo_files("notags"), repo_tags("notags"), gate.EXIT_OK)
        run_case("G_multi_prefix", repo_files("nestedmany"), repo_tags("nestedmany"), gate.EXIT_OK)
        # E：读失败 ⇒ 退出码 2（判不了 ≠ 通过）
        run_case("E_read_fail", repo_files("others"), repo_tags("others"), gate.EXIT_UNAVAILABLE,
                 fails={("others", "v1.0.0", "CHANGELOG.md")})
        # H：豁免名单——名单里的 tag 快照缺段不判红；不在名单的照样判。
        orig_exempt = gate.HISTORIC_SNAPSHOT_EXEMPT
        gate.HISTORIC_SNAPSHOT_EXEMPT = {"histsink:sink/v0.1.0"}
        try:
            h_files = {
                ("histsink", "sink/v0.1.0", "CHANGELOG.md"): prev_snap("sink/v0.1.0", "v0.0.9"),
                ("histsink", "sink/v0.1.0", "sink/CHANGELOG.md"): prev_snap("sink/v0.1.0", "v0.0.9"),
            }
            h_tags = {"histsink": ["sink/v0.1.0"]}
            rc_h = gate.run(["histsink"], read_fn=make_read_fn(h_files), tags_fn=make_tags_fn(h_tags))
            _record("H_exempt_skips", rc_h == gate.EXIT_OK,
                    f"H: 豁免名单应跳过(exit={rc_h})")
            h2_tags = {"histother": ["sink/v0.1.0"]}
            rc_h2 = gate.run(["histother"], read_fn=make_read_fn(h_files), tags_fn=make_tags_fn(h2_tags))
            _record("H_nonexempt_still_red", rc_h2 == gate.EXIT_DRIFT,
                    f"H: 非名单仓照样判红(exit={rc_h2})")
        finally:
            gate.HISTORIC_SNAPSHOT_EXEMPT = orig_exempt
        # 段头正则的独立小判据：上一版段「关联问题单」里的版本号不许误命中
        text = "## [v0.12.5] - 2026-10-23\n\n- build: 地板对齐 v0.12.6 前置（勿混淆）\n"
        _record("R_regex_not_string", not gate.section_re("v0.12.6").search(text),
                "R: 段头正则误命中正文字串")

        if all(_RESULTS.values()):
            print(f"\u2713 自测全绿（{len(_RESULTS)} 用例）")
            return 0
        print(f"\u274c 自测失败 {len(_FAILURES)} 项：")
        for f in _FAILURES:
            print(f"  \u274c {f}")
        return 1

    # ---------- 变异自证：精确转红集合 ----------
    # 变异直接 patch 在 gate 模块上（types.ModuleType 复制的函数 __globals__
    # 仍指向原模块，赋值不生效——实测踩过），finally 恢复。
    print("== 变异自证：精确转红集合 ==")

    # M1：只查顶层 tag（嵌套面漏检复辟）⇒「顶层全绿、嵌套漂移」必假绿。
    m1_tags = {"m1": ["v1.0.0", "sub/v9.9.9"]}
    m1_files = {
        ("m1", "v1.0.0", "CHANGELOG.md"): cur_snap("v1.0.0", "v0.9.0"),   # 顶层绿
        ("m1", "sub/v9.9.9", "CHANGELOG.md"): prev_snap("sub/v9.9.9", "sub/v9.9.8"),
        ("m1", "sub/v9.9.9", "sub/CHANGELOG.md"): prev_snap("sub/v9.9.9", "sub/v9.9.8"),
    }
    rc_orig = gate.run(["m1"], read_fn=make_read_fn(m1_files), tags_fn=make_tags_fn(m1_tags))
    _record("M1_orig_red", rc_orig == gate.EXIT_DRIFT, f"M1 原版应判嵌套漂移: exit={rc_orig}")
    orig_pick = gate.pick_tags
    try:
        gate.pick_tags = lambda tags_list: [t for t in orig_pick(tags_list) if "/" not in t]
        rc_m1 = gate.run(["m1"], read_fn=make_read_fn(m1_files), tags_fn=make_tags_fn(m1_tags))
    finally:
        gate.pick_tags = orig_pick
    _record("M1_mutated_green_is_the_bug", rc_m1 == gate.EXIT_OK,
            f"M1 变异后应假绿(exit={rc_m1})——证明原版确实在查嵌套面")

    # M2：段头匹配放宽成字串包含 ⇒ A（顶部停在上一版、正文含版本号字串）必假绿。
    import re as _re
    orig_re = gate.section_re
    try:
        gate.section_re = lambda version: _re.compile(_re.escape(version))
        rc_m2 = gate.run(["toolgate"], read_fn=make_read_fn(files), tags_fn=make_tags_fn(tags))
    finally:
        gate.section_re = orig_re
    _record("M2_mutated_green_is_the_bug", rc_m2 == gate.EXIT_OK,
            f"M2 字串匹配应假绿(exit={rc_m2})——证明段头判定不是字串包含")

    # M3：读失败吞成「读到含本版段的快照」（fail-open 的真实危害形态）⇒ 必假绿。
    failing = make_read_fn(repo_files("others"), fails={("others", "v1.0.0", "CHANGELOG.md")})

    def read_fn_swallows(repo, ref, path):
        try:
            return failing(repo, ref, path)
        except Exception:
            return cur_snap(ref, "v0.9.0")
    rc_m3 = gate.run(["others"], read_fn=read_fn_swallows, tags_fn=make_tags_fn(tags))
    _record("M3_mutated_green_is_the_bug", rc_m3 == gate.EXIT_OK,
            f"M3 吞错应假绿(exit={rc_m3})——证明原版读失败不伪装通过")

    expected = {"M1_orig_red", "M1_mutated_green_is_the_bug", "M2_mutated_green_is_the_bug",
                "M3_mutated_green_is_the_bug"}
    bad = [k for k, v in _RESULTS.items() if k in expected and not v]
    if bad:
        print(f"\u274c 变异自证失败 {len(bad)} 项：{bad}")
        return 1
    print(f"\u2713 变异自证精确（{len(expected)} 判据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
