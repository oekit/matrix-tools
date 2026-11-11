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

    # ── I 系夹具（matrix-tools#259：模板 + 文案形状）——正常与 --mutate 都要用 ──
    # core=零内部依赖根模块（模板必须明示「不要写地板」）；
    # boot/obs=有内部依赖的嵌套模块（模板必须带「带出新的地板：本版号」）；
    # boot2=根面裸版本头（模板必须补全成完整 tag 名）；
    # lintok=段在且文案完好；lintbad=段在但文案残缺（#259 实测形态）。
    i_files = {
        ("core", "v0.13.3", "CHANGELOG.md"): prev_snap("v0.13.3", "v0.13.2"),
        ("core", "v0.13.3", "go.mod"): "module github.com/oekit/core\n\ngo 1.24\n",
        ("boot", "obs/v0.2.14", "CHANGELOG.md"): prev_snap("obs/v0.2.14", "obs/v0.2.13"),
        ("boot", "obs/v0.2.14", "go.mod"):
            "module github.com/oekit/bootstrap/obs\n\ngo 1.24\n\n"
            "require github.com/oekit/metrics v0.5.0\n",
        ("boot2", "obs/v0.2.14", "CHANGELOG.md"): prev_snap("obs/v0.2.14", "v0.2.13"),
        ("boot2", "obs/v0.2.14", "go.mod"):
            "module github.com/oekit/bootstrap/obs\n\ngo 1.24\n\n"
            "require github.com/oekit/metrics v0.5.0\n",
        ("lintok", "v1.2.0", "CHANGELOG.md"): cur_snap("v1.2.0", "v1.1.0"),
        ("lintbad", "v1.3.0", "CHANGELOG.md"):
            "## [v1.3.0] - 2026-11-08\n\n"
            "- chore: 补齐本版版本节——上一个已发布 tag（）的 CHANGELOG 快照缺本版段"
            "（tag 快照不可变），本版补上并带出新的地板v1.2.9\n\n"
            + prev_snap("v1.3.0", "v1.2.9"),
    }
    i_tags = {"core": ["v0.13.2", "v0.13.3"], "boot": ["obs/v0.2.14"],
              "boot2": ["obs/v0.2.14"], "lintok": ["v1.2.0"], "lintbad": ["v1.3.0"]}

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

        # I1：零内部依赖 ⇒ 模板给全上一版 tag 名与「不要写地板」的明示
        d1 = gate.check_tag("core", "v0.13.3", make_read_fn(i_files))
        _record("I1_template_no_deps",
                d1 is not None and "修复模板" in d1 and "（v0.13.2）" in d1
                and "不要写「带出新的地板」" in d1 and "带出新的地板：v0.13.3" not in d1,
                f"I1: 零内部依赖的模板不对：{d1}")
        # I2：有内部依赖 ⇒ 模板带「带出新的地板：本版号」
        d2 = gate.check_tag("boot", "obs/v0.2.14", make_read_fn(i_files))
        _record("I2_template_with_deps",
                d2 is not None and "带出新的地板：v0.2.14" in d2
                and "（obs/v0.2.13）" in d2,
                f"I2: 有内部依赖的模板不对：{d2}")
        # I3：嵌套面裸版本头 ⇒ 模板补全成完整 tag 名（#259 实测的 `（obs/）` 缺陷形态）
        d3 = gate.check_tag("boot2", "obs/v0.2.14", make_read_fn(i_files))
        _record("I3_template_nested_full_name",
                d3 is not None and "（obs/v0.2.13）" in d3 and "（v0.2.13）" not in d3,
                f"I3: 裸版本头没有补全成完整 tag 名：{d3}")
        # I4：lint 判据单元——四种残缺形态各自命中；完好文案零误报
        bad_text = ("- chore: 补齐本版版本节——上一个已发布 tag（）的 CHANGELOG 快照缺本版段"
                    "（tag 快照不可变），本版补上并带出新的地板v1.2.9\n")
        p_bad = gate.lint_repair_bullet(bad_text, "v1.3.0")
        _record("I4_lint_catches_broken",
                len(p_bad) >= 2 and any("空占位" in x for x in p_bad)
                and any("缺分隔符" in x for x in p_bad),
                f"I4: 残缺文案必须被抓（空括号+缺分隔符）：{p_bad}")
        p_prefix = gate.lint_repair_bullet(
            "- chore: 补齐本版版本节——上一个已发布 tag（obs/）的快照缺本版段\n", "v1.3.1")
        _record("I4b_lint_prefix_only",
                len(p_prefix) == 1 and "路径前缀" in p_prefix[0],
                f"I4b: 前缀无版本形态必须单独命中：{p_prefix}")
        p_ver = gate.lint_repair_bullet(
            "- chore: 补齐本版版本节——本版补上并带出新的地板：v1.2.9\n", "v1.3.1")
        _record("I4c_lint_wrong_floor_ver",
                len(p_ver) == 1 and "不是本版号" in p_ver[0] and "v1.2.9" in p_ver[0]
                and "v1.3.1" in p_ver[0],
                f"I4c: 「新的地板」后跟旧版号必须命中：{p_ver}")
        p_ok = gate.lint_repair_bullet(cur_snap("v1.2.0", "v1.1.0")
                                       + "- feat: 依赖 logx 地板 v0.8.0 → v0.8.1\n", "v1.2.0")
        _record("I4d_lint_clean_no_false_positive", not p_ok,
                f"I4d: 完好文案（含合法的依赖地板叙述）不许误报：{p_ok}")
        # I4e（matrix-tools#286 实测误报回归）：Go 方法名后缀空括号是合法行文，
        # 不许判「空占位括号」残缺；ASCII 带空白占位 `( )` 仍须命中。
        p_call = gate.lint_repair_bullet(
            "- fix: 白名单外整条丢弃并经 Err() 累计、Strict() 当场 panic、"
            "Page() 归一；上游 Drain() 后 nc.Close()（目标态已达成）\n", "v1.3.0")
        _record("I4e_lint_gocall_no_false_positive", not p_call,
                f"I4e: Go 调用后缀空括号不许误报：{p_call}")
        p_sp = gate.lint_repair_bullet(
            "- chore: 补齐本版版本节——上一个已发布 tag( ) 的快照缺本版段\n",
            "v1.3.0")
        _record("I4e2_lint_ascii_spaced_paren_still_hit",
                any("空占位" in x for x in p_sp),
                f"I4e2: ASCII 带空白空括号占位仍须命中：{p_sp}")
        # I4g：Go 调用语法不许误报（#289——与 #286 同族，正则已并收）——recover()/Errors()/Close()/min()
        # 是合法 API 引用，非「空占位括号」
        p_call = gate.lint_repair_bullet(
            "- fix: defer recover() 拦不到、Errors() 恒空、min() 截断、nc.Close()\n", "v1.2.0")
        _record("I4g_lint_go_call_syntax_no_false_positive", not p_call,
                f"I4e: Go 调用语法不许误报为空占位括号：{p_call}")
        # I4f：独立占位 `（）`/`( )` 照拦（#289 修复不放走真缺陷）
        p_ph = gate.lint_repair_bullet(
            "- chore: 补齐本版版本节——上一个已发布 tag（ ）的快照缺本版段\n", "v1.2.0")
        _record("I4f_lint_placeholder_still_caught",
                len(p_ph) == 1 and "空占位" in p_ph[0],
                f"I4f: 独立占位括号必须照拦：{p_ph}")
        # I5：端到端——段在但文案残缺 ⇒ EXIT_DRIFT 且点名「文案残缺」；完好 ⇒ EXIT_OK
        run_case("I5_lint_bad_section_drifts", i_files,
                 {"lintbad": i_tags["lintbad"]}, gate.EXIT_DRIFT)
        d5 = gate.lint_tag_section("lintbad", "v1.3.0", make_read_fn(i_files))
        _record("I5b_lint_names_defect",
                len(d5) >= 1 and "文案残缺" in d5[0] and "快照不可变" in d5[0],
                f"I5b: 端到端残缺必须被点名：{d5}")
        run_case("I6_clean_section_still_ok", i_files,
                 {"lintok": i_tags["lintok"]}, gate.EXIT_OK)

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

    # M4（matrix-tools#259）：文案形状校验被摘掉 ⇒ 残缺段照样判绿
    #（正是 #259 要堵的洞：半成品文案随 tag 永久留痕、门禁却放行）。
    rc_m4_orig = gate.run(["lintbad"], read_fn=make_read_fn(i_files),
                          tags_fn=make_tags_fn(i_tags))
    _record("M4_orig_red", rc_m4_orig == gate.EXIT_DRIFT,
            f"M4 原版应判文案残缺: exit={rc_m4_orig}")
    orig_lint = gate.lint_tag_section
    try:
        gate.lint_tag_section = lambda repo, tag, read_fn: []
        rc_m4 = gate.run(["lintbad"], read_fn=make_read_fn(i_files),
                         tags_fn=make_tags_fn(i_tags))
    finally:
        gate.lint_tag_section = orig_lint
    _record("M4_mutated_green_is_the_bug", rc_m4 == gate.EXIT_OK,
            f"M4 摘掉文案校验应假绿(exit={rc_m4})——证明原版在核文案形状")

    # M5（matrix-tools#259）：修复模板被摘掉 ⇒ 报错退回「只说缺段、不给可照抄
    # 的东西」（#259 的根因形态：修复者只能自己造句 ⇒ 残缺）。
    orig_tpl = gate.repair_template
    try:
        gate.repair_template = lambda *a, **k: ""
        d_m5 = gate.check_tag("toolgate", "v0.12.6", make_read_fn(files))
    finally:
        gate.repair_template = orig_tpl
    _record("M5_template_removed_is_the_bug",
            d_m5 is not None and "修复模板" not in d_m5,
            "M5: 模板摘除后消息里不该再有模板（证明原版消息自带模板）")

    expected = {"M1_orig_red", "M1_mutated_green_is_the_bug", "M2_mutated_green_is_the_bug",
                "M3_mutated_green_is_the_bug", "M4_orig_red",
                "M4_mutated_green_is_the_bug", "M5_template_removed_is_the_bug"}
    bad = [k for k, v in _RESULTS.items() if k in expected and not v]
    if bad:
        print(f"\u274c 变异自证失败 {len(bad)} 项：{bad}")
        return 1
    print(f"\u2713 变异自证精确（{len(expected)} 判据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
