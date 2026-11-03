#!/usr/bin/env python3
"""**版本节读路径**自测：嵌套 tag 的回执/Release 正文到底读**哪一面**（#208 续修）。

**这条判据防的是什么（静默失效）**：嵌套 tag（`nacos/v0.1.3`）在仓里有**两面**：

  - **根 CHANGELOG** 的 `## [nacos/v0.1.3]` —— 承载**变更描述**（这一版改了什么）；
  - **嵌套模块自己的 CHANGELOG** 的 `## [v0.1.3]` —— 按仓内约定（见
    `release_train_selftest` 用例 3c）**只写**「跟随父仓发版」。

回执（`close_issue`）、Release 正文（`gen_releases.build_notes`）、写回前置门禁
（`check_writeback.resolve_changelog`）都靠 `changelog_section()` / 同款读法定位
这一节 ⇒ **读错一面 ⇒ 回执讲的不是这版的变更**，而且**看起来完全正常**：
条目照样定位得到（issue 号挂在那一面），只是那一条**不是变更描述**。

**实测（2026-11-02，registry#49）**：读嵌套面 ⇒ 回执的「本条修复」=
「- 随本次 train 一并发布（只发嵌套模块，父模块本次不发版）（registry#49）」
⇒ 读者读不到「这条到底改了什么」；同一版的根面明明写着
「- build: 把父仓 registry v0.5.3 与 logx v0.8.2 的地板带进已发布产物（registry#49）」。

**读法**：嵌套 **先读根面**（`[<sub>/vX.Y.Z]`）⇒ **再回退**嵌套文件（`[vX.Y.Z]`）。
⚠️ 改优先级**连已发布的回执一起修好**（重渲染即可，**不必改已发布文件**）。

**反证怎么构造（每条都有对应变异体，见 MUTATIONS）**：
  1. 优先级翻转回「嵌套文件优先」⇒ 用例 A/D 必须失败；
  2. 完全不回退根面（只读嵌套文件）⇒ 用例 A/D 必须失败；
  3. `check_writeback` 不跟着改（**口径分裂**）⇒ 用例 D 必须失败。

**无副作用、不联网**：临时目录造 fixture（真实 Git 仓形态的两面 CHANGELOG）。

用法：
    python3 scripts/changelog_section_selftest.py            # 正常模式：全部用例通过
    python3 scripts/changelog_section_selftest.py --mutate   # 变异自证：精确转红集合必须精确
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import check_writeback  # noqa: E402
import gen_releases  # noqa: E402
from gen_releases import locate_issue_entries  # noqa: E402

# 用例级结果收集（供 --mutate 变异自证比对「精确转红集合」）
_RESULTS: dict[str, bool] = {}
_FAILURES: list[str] = []

# 嵌套面（只有发布关系说明）与根面（承载变更描述）各自的标志性文本
NESTED_ONLY_TEXT = "随本次 train 一并发布（只发嵌套模块，父模块本次不发版）"
ROOT_DESC_TEXT = "把父仓 registry v0.5.3 的地板带进已发布产物"


def _record(cid: str, good: bool, msg: str = "") -> bool:
    _RESULTS[cid] = good
    if not good and msg:
        _FAILURES.append(msg)
    return good


def _fixture(tmp: Path, *, root_nested_section: bool = True,
             nested_file: bool = True) -> Path:
    """造一个 registry 形态的 fixture：根 CHANGELOG + 嵌套模块自己的 CHANGELOG。"""
    repo = tmp / "registry"
    (repo / "nacos").mkdir(parents=True, exist_ok=True)
    (repo / "go.mod").write_text("module github.com/oekit/registry\n", encoding="utf-8")
    root = "# Changelog — oekit/registry\n\n"
    if root_nested_section:
        root += (f"## [nacos/v0.1.3] — 2026-11-03\n\n"
                 f"- build: {ROOT_DESC_TEXT}（registry#49）\n\n"
                 f"关联问题单：registry#49。\n\n")
    root += "## [v0.5.3] — 2026-11-03\n\n- build: 根面自己这一版的内容\n\n"
    (repo / "CHANGELOG.md").write_text(root, encoding="utf-8")
    if nested_file:
        (repo / "nacos" / "CHANGELOG.md").write_text(
            "# Changelog — oekit/registry/nacos\n\n"
            "## [v0.1.3] — 2026-11-03\n\n"
            f"- {NESTED_ONLY_TEXT}（registry#49）\n\n"
            "关联问题单：registry#49。\n\n", encoding="utf-8")
    return repo


def _case_a(tmp: Path) -> None:
    """A：嵌套 tag ⇒ 必须读**根面**（承载变更描述的那一面）。

    旧读法（嵌套文件优先）读到的是「只有发布关系说明」的一面 ⇒ 回执的
    「本条修复」= 那句话 ⇒ 读者读不到「这条到底改了什么」。
    """
    repo = _fixture(tmp)
    sec = gen_releases.changelog_section(str(repo), "v0.1.3", "nacos")
    if sec is None:
        _record("A", False, "用例A：嵌套 tag 必须读到版本节（读到 None）")
        return
    if ROOT_DESC_TEXT not in sec:
        _record("A", False,
                f"用例A：嵌套 tag 必须读**根面**（承载变更描述的一面），"
                f"实际读到={sec!r}")
        return
    # 回执按号定位到的条目必须是**描述**，不是发布关系说明
    loc = locate_issue_entries(sec, "registry", "49")
    if len(loc.entries) != 1:
        _record("A", False, f"用例A：回执必须定位到 1 条条目（实际 {len(loc.entries)}）")
        return
    if ROOT_DESC_TEXT not in loc.entries[0]:
        _record("A", False,
                f"用例A：回执定位到的条目必须是**变更描述**，实际={loc.entries[0]!r}")
        return
    _record("A", True)


def _case_b(tmp: Path) -> None:
    """B：根面**没有**该嵌套 tag 的节（历史形态）⇒ 回退嵌套文件。"""
    repo = _fixture(tmp, root_nested_section=False)
    sec = gen_releases.changelog_section(str(repo), "v0.1.3", "nacos")
    if sec is None:
        _record("B", False, "用例B：根面没有该节时必须回退嵌套文件（读到 None）")
        return
    if NESTED_ONLY_TEXT not in sec:
        _record("B", False, f"用例B：必须回退到嵌套文件的那一面，实际读到={sec!r}")
        return
    _record("B", True)


def _case_c(tmp: Path) -> None:
    """C：根 tag（nested=None）⇒ 读根面，行为不变。"""
    repo = _fixture(tmp)
    sec = gen_releases.changelog_section(str(repo), "v0.5.3", None)
    if sec is None or "根面自己这一版的内容" not in sec:
        _record("C", False, f"用例C：根 tag 必须读根面，实际读到={sec!r}")
        return
    _record("C", True)


def _case_d(tmp: Path) -> None:
    """D：★ **已发布产物**（Release 正文）必须承载**变更描述**。

    Release 正文 = 版本节的逐字副本（`gen_releases.build_notes`）⇒ 读错一面 ⇒
    **已发布的 Release 正文**里只有一句「跟随父仓发版」⇒ 读者读不到这版改了什么。
    ⇒ 本例断言 Release 正文里**有**变更描述。
    """
    repo = _fixture(tmp)
    notes = gen_releases.build_notes(str(repo), "nacos/v0.1.3", "registry")
    if ROOT_DESC_TEXT not in notes:
        _record("D", False,
                f"用例D：Release 正文必须承载**变更描述**（读错面 ⇒ 已发布产物里只有"
                f"发布关系说明），实际={notes!r}")
        return
    if NESTED_ONLY_TEXT in notes:
        _record("D", False,
                f"用例D：Release 正文不该只有发布关系说明，实际={notes!r}")
        return
    _record("D", True)


def _case_e(tmp: Path) -> None:
    """E：★ 写回前置门禁的 `--nested` 模式**故意**读**嵌套模块自己的 CHANGELOG**。

    ⚠️ 与「回执读哪一面」是**两件事**，别混：
      - 回执/Release（`changelog_section`）⇒ 读**内容所在**的那一面（根面）；
      - 写回门禁 `--nested <sub>` ⇒ 操作者**显式指定**要查嵌套模块自己的文件。
    ⇒ 本例把这条分工**钉住**：门禁那一侧被顺手翻转 ⇒ 本用例转红
    （免得「为了口径统一」把显式目标改坏）。
    """
    repo = _fixture(tmp)
    saved_base = check_writeback.BASE
    check_writeback.BASE = str(tmp)
    try:
        f, body, head, _s = check_writeback.resolve_changelog(
            "registry", "v0.1.3", "nacos", None)
    finally:
        check_writeback.BASE = saved_base
    if body is None:
        _record("E", False, "用例E：--nested 必须定位到版本节（读到 None）")
        return
    if NESTED_ONLY_TEXT not in body:
        _record("E", False,
                f"用例E：--nested 是**显式指定**目标 ⇒ 必须读嵌套模块自己的 CHANGELOG，"
                f"实际读到={body!r}")
        return
    if head != "nacos/v0.1.3":
        _record("E", False, f"用例E：节头必须是限定形式 nacos/v0.1.3（实际 {head!r}）")
        return
    _record("E", True)


def run_suite() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _case_a(tmp)
        _case_b(tmp)
        _case_c(tmp)
        _case_d(tmp)
        _case_e(tmp)


# ── 变异体（--mutate）───────────────────────────────────────────────────────

def _mutate_priority_nested_first():
    """优先级翻转回「嵌套文件优先」（旧读法）⇒ A/D 必须转红。"""
    orig = gen_releases.changelog_section

    def mutated(repo_dir_path, version: str, nested: str | None):
        if nested:
            sub_cl = Path(repo_dir_path) / nested / "CHANGELOG.md"
            if sub_cl.exists():
                sec = gen_releases._extract(sub_cl.read_text(encoding="utf-8"), version)
                if sec is not None:
                    return sec
            root_cl = Path(repo_dir_path) / "CHANGELOG.md"
            if not root_cl.exists():
                return None
            return gen_releases._extract(root_cl.read_text(encoding="utf-8"),
                                        f"{nested}/{version}")
        return orig(repo_dir_path, version, nested)

    gen_releases.changelog_section = mutated
    return [("changelog_section", orig)]


def _mutate_never_read_root():
    """完全不回退根面（只读嵌套文件）⇒ A/D 必须转红。"""
    orig = gen_releases.changelog_section

    def mutated(repo_dir_path, version: str, nested: str | None):
        if nested:
            sub_cl = Path(repo_dir_path) / nested / "CHANGELOG.md"
            if not sub_cl.exists():
                return None
            return gen_releases._extract(sub_cl.read_text(encoding="utf-8"), version)
        return orig(repo_dir_path, version, nested)

    gen_releases.changelog_section = mutated
    return [("changelog_section", orig)]


def _mutate_writeback_also_flipped():
    """写回门禁也被顺手翻转 ⇒ 「显式指定目标」被改坏 ⇒ 只有 E 必须转红。

    ★ 这条变异的存在意义：证明「两种读法是**分工**不是**分裂**」——
      回执读**内容所在**的一面（根面），门禁 `--nested` 读**显式指定**的一面
      （嵌套文件）⇒ 两者**刻意不同** ⇒ 任何「统一口径」的顺手改动都会被抓到。
    """
    orig = check_writeback.resolve_changelog

    def mutated(repo, version, nested, file_override):
        if nested and not file_override:
            base = Path(check_writeback.BASE)
            rdir = base / repo
            root_cl = rdir / "CHANGELOG.md"
            if root_cl.exists():
                lines = root_cl.read_text(encoding="utf-8").splitlines()
                rng = check_writeback.find_section(lines, f"{nested}/{version}")
                if rng:
                    s, e = rng
                    return root_cl, "\n".join(lines[s:e]), f"{nested}/{version}", s
        return orig(repo, version, nested, file_override)

    check_writeback.resolve_changelog = mutated
    return [("resolve_changelog", orig)]


MUTATIONS = [
    ("优先级翻转回「嵌套文件优先」（旧读法）",
     _mutate_priority_nested_first, {"A", "D"}),
    ("完全不回退根面（只读嵌套文件）",
     _mutate_never_read_root, {"A", "D"}),
    ("写回门禁被顺手翻转（把分工当分裂统一掉）",
     _mutate_writeback_also_flipped, {"E"}),
]


def run_mutations() -> int:
    ok = True
    for name, mutate, expect in MUTATIONS:
        _RESULTS.clear()
        _FAILURES.clear()
        patches = mutate()
        try:
            run_suite()
        finally:
            for attr, orig in patches:
                if attr == "changelog_section":
                    gen_releases.changelog_section = orig
                else:
                    check_writeback.resolve_changelog = orig
        got = {cid for cid, good in _RESULTS.items() if not good}
        if got == expect:
            print(f"✓ 变异「{name}」被抓住（精确转红 {sorted(got)}）")
        else:
            ok = False
            print(f"✗ 变异「{name}」转红集合**不精确** ⇒ "
                  f"期望 {sorted(expect)}，实际 {sorted(got)}")
            for f_ in _FAILURES:
                print("      -", f_)
    if ok:
        print(f"✓ 变异自证 {len(MUTATIONS)}/{len(MUTATIONS)} 全部**精确**转红")
        return 0
    print(f"✗ 变异自证未过：{len(MUTATIONS)} 个变异体里至少一个转红集合不精确")
    return 1


def main() -> int:
    if "--mutate" in sys.argv:
        return run_mutations()

    _RESULTS.clear()
    _FAILURES.clear()
    run_suite()
    if _FAILURES:
        print("✗ 版本节读路径自测未过：")
        for f_ in _FAILURES:
            print("  -", f_)
        raise SystemExit(1)
    print("✓ 版本节读路径自测通过：嵌套 tag 读根面（承载变更描述的一面）、"
          "根面没有该节时回退嵌套文件、根 tag 行为不变、"
          "Release 正文承载变更描述、写回门禁 --nested 仍读显式指定的那一面")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
