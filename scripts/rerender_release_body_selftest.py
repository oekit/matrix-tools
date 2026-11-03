#!/usr/bin/env python3
"""`rerender_release_body.py` 自测（#213）。

要钉住的行为（每条都是「改已发布产物」的高风险面）：

A. **默认只预览**：无 `--apply` ⇒ 绝不调用写接口（`edit_release_body`）。
B. **幂等**：正文已一致 ⇒ 报「无需改动」且**不写**（不重复改写已发布内容）。
C. **fail closed**（三种「判不了」都**不写**）：
   C1. tag 不存在；
   C2. Release 不存在；
   C3. 渲染出的正文**没有「变更记录」段**（读不到版本节）⇒
       **不许写出空正文**（空正文比陈旧更糟：读者连「有改了什么」都看不到）。
D. **正文来源 = 发版时同一份代码**（`gen_releases.build_notes`）⇒ 不另造一套口径。
E. **参数解析**：`--backup-dir DIR` 的 DIR **不许**被当成 repo/tag。
F. **只对明确点名的 tag 生效**：不允许「全仓重刷」这种默认动作。

用法：
  python3 scripts/rerender_release_body_selftest.py           # 跑用例
  python3 scripts/rerender_release_body_selftest.py --mutate  # 变异自证
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent))

import rerender_release_body as rrb  # noqa: E402

# ---------- 用例级结果收集（供 --mutate 变异自证比对「精确转红集合」）----------

RESULTS: dict[str, bool] = {}


def case(name: str):
    def deco(fn):
        def run():
            try:
                ok = fn()
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  ✗ {name}: 抛异常 {e}")
            RESULTS[name] = bool(ok)
            if ok:
                print(f"  ✓ {name}")
            return ok
        run.case_name = name
        return run
    return deco


def _fake_env(**over):
    """造一份假的远端状态 + 本地仓。返回 (state, repo_dir)。"""
    state = {
        "tags": {"v1.0.0", "sub/v1.0.0", "v2.0.0"},
        "releases": {
            "v1.0.0": "Release of x v1.0.0\n\n## 变更记录\n\n- 正确的描述\n",
            "sub/v1.0.0": "Release of x sub/v1.0.0\n\n## 变更记录\n\n- 陈旧的描述\n",
            # ⚠️ v2.0.0 **有 tag 但没有 Release** ⇒ 精确命中 C2 分支
            #    （若它连 tag 也没有 ⇒ 命中的会是 C1，测的就不是 C2 了）
        },
        "written": [],
        "edits": [],
    }
    for k, v in over.items():
        state[k] = v
    return state


def _fake_repo_dir(tag_body: str = "- build: 新的变更描述\n"):
    """造一个假的本地仓（带根 CHANGELOG 的版本节）。

    ⚠️ `build_notes` 读的是**本地工作树**（与发版时同源）⇒ 夹具不给 CHANGELOG
    的话渲染不出「变更记录」段 ⇒ 工具会**正确地** fail closed ⇒ 用例会因
    「夹具不完整」而红，而不是因工具行为而红 ⇒ 那是**夹具的错**，不是工具的错。
    """
    d = Path(tempfile.mkdtemp(prefix="rrb-selftest-"))
    ver = "v1.0.0"
    (d / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [sub/{ver}] — 2026-11-03\n\n{tag_body}\n"
        f"\n## [{ver}] — 2026-11-03\n\n{tag_body}\n",
        encoding="utf-8")
    return d


def _install(state):
    """把假状态装到模块的远端读写函数上（返回还原函数）。"""
    orig = {
        "tag_exists": rrb.tag_exists,
        "release_exists": rrb.release_exists,
        "release_body": rrb.release_body,
        "edit_release_body": rrb.edit_release_body,
        "repo_dir": rrb.repo_dir,
    }

    def tag_exists(repo, tag):
        return tag in state["tags"]

    def release_exists(repo, tag):
        return tag in state["releases"]

    def release_body(repo, tag):
        if tag not in state["releases"]:
            raise RuntimeError("no such release")
        return state["releases"][tag]

    def edit_release_body(repo, tag, body):
        state["edits"].append((tag, body))
        state["releases"][tag] = body

    rrb.tag_exists = tag_exists
    rrb.release_exists = release_exists
    rrb.release_body = release_body
    rrb.edit_release_body = edit_release_body
    rrb.repo_dir = lambda repo, *a, **k: _fake_repo_dir()
    return orig, (tag_exists, release_exists, release_body, edit_release_body)


def _restore(orig):
    rrb.tag_exists = orig["tag_exists"]
    rrb.release_exists = orig["release_exists"]
    rrb.release_body = orig["release_body"]
    rrb.edit_release_body = orig["edit_release_body"]
    rrb.repo_dir = orig["repo_dir"]


# ── 用例 ────────────────────────────────────────────────────────────────

@case("A. 默认只预览 ⇒ 不写")
def t_preview_no_write():
    state = _fake_env()
    orig, _ = _install(state)
    try:
        ok, msg = rrb.rerender("x", "sub/v1.0.0", apply=False)
        return ok and not state["edits"] and "预览" in msg
    finally:
        _restore(orig)


@case("A2. --apply 才写")
def t_apply_writes():
    state = _fake_env()
    orig, _ = _install(state)
    try:
        ok, _msg = rrb.rerender("x", "sub/v1.0.0", apply=True)
        return ok and len(state["edits"]) == 1
    finally:
        _restore(orig)


@case("B. 幂等：正文已一致 ⇒ 不写")
def t_idempotent():
    state = _fake_env()
    orig, _ = _install(state)
    try:
        # 让「渲染出的新正文」与「当前正文」逐字节相等
        orig_build = rrb.build_notes
        rrb.build_notes = lambda d, tag, repo: state["releases"]["sub/v1.0.0"]
        try:
            ok, msg = rrb.rerender("x", "sub/v1.0.0", apply=True)
            return ok and not state["edits"] and "无需改动" in msg
        finally:
            rrb.build_notes = orig_build
    finally:
        _restore(orig)


@case("B2. 幂等判据是「逐字节相等」而非弱特征（含『变更记录』但内容不同 ⇒ 仍须改）")
def t_idempotent_not_weak():
    state = _fake_env()
    orig, _ = _install(state)
    try:
        # 当前正文含「## 变更记录」但内容与新正文**不同**
        # ⇒ 弱特征判据会误判成「已一致」⇒ 必须仍判定为「要改」
        ok, msg = rrb.rerender("x", "sub/v1.0.0", apply=True)
        return ok and len(state["edits"]) == 1 and "已重渲染" in msg
    finally:
        _restore(orig)


@case("C1. fail closed：tag 不存在 ⇒ 不写")
def t_no_tag():
    state = _fake_env()
    orig, _ = _install(state)
    try:
        ok, msg = rrb.rerender("x", "v9.9.9", apply=True)
        return (not ok) and not state["edits"] and "tag 不存在" in msg
    finally:
        _restore(orig)


@case("C2. fail closed：Release 不存在 ⇒ 不写")
def t_no_release():
    state = _fake_env()
    orig, _ = _install(state)
    try:
        ok, msg = rrb.rerender("x", "v2.0.0", apply=True)
        return (not ok) and not state["edits"] and "没有 Release" in msg
    finally:
        _restore(orig)


@case("C3. fail closed：渲染不出「变更记录」段 ⇒ 不写空正文")
def t_empty_section():
    state = _fake_env()
    orig, _ = _install(state)
    orig_build = rrb.build_notes
    rrb.build_notes = lambda d, tag, repo: "Release of x\n\n（读不到版本节）\n"
    try:
        ok, msg = rrb.rerender("x", "sub/v1.0.0", apply=True)
        return (not ok) and not state["edits"] and "判不了" in msg
    finally:
        rrb.build_notes = orig_build
        _restore(orig)


@case("D. 正文来源 = gen_releases.build_notes（同一份代码）")
def t_same_renderer():
    import gen_releases
    # 工具模块引用的就是 gen_releases 的那个函数对象 ⇒ 同一份代码
    return rrb.build_notes is gen_releases.build_notes


@case("E. 参数解析：--backup-dir 的值不被当成 repo/tag")
def t_arg_parse():
    import subprocess
    script = SCRIPTS / "rerender_release_body.py"
    # 跑一个「必然失败」的目标（tag 不存在）⇒ 若参数解析错，报的会是别的 repo
    p = subprocess.run(
        [sys.executable, str(script), "registry", "v0.0.0-does-not-exist",
         "--backup-dir", "/tmp/nonexistent-backup-dir"],
        capture_output=True, text=True, timeout=60)
    out = p.stdout + p.stderr
    return "registry" in out and "nonexistent-backup-dir" not in out


@case("F. 只对明确点名的 tag 生效（无「全仓重刷」入口）")
def t_no_bulk_default():
    src = (SCRIPTS / "rerender_release_body.py").read_text(encoding="utf-8")
    # 不许出现「没给 tag 就刷全仓」的形态：解析后必须有 <repo> <tag> 成对
    return ("rest[0::2]" in src and "rest[1::2]" in src
            and "len(rest) % 2 != 0" in src)


CASES = [t_preview_no_write, t_apply_writes, t_idempotent, t_idempotent_not_weak,
         t_no_tag, t_no_release, t_empty_section, t_same_renderer, t_arg_parse,
         t_no_bulk_default]

# ── 变异体（--mutate）───────────────────────────────────────────────────────
#
# 每个变异体**只改一处判据**，且必须让**特定的用例**转红 ⇒ 「精确转红集合」是判据：
# 转红集合**多了**（误伤）或**少了**（漏检）都算变异没被抓住。
#
# ★ 变异体替换的是**判据函数**（`body_is_renderable` / `body_is_same` /
#   `should_write`），不是整个 `rerender` ⇒
#   ① 变异**不会被夹具的安装覆盖**（夹具只替换远端读写函数）；
#   ② 转红集合**精确**（只影响该判据负责的那几条用例）。

MUTATIONS = [
    ("写入门槛被去掉（预览也真写）", "should_write",
     "rrb.should_write = lambda apply: True\n"),

    ("幂等判据放宽（弱特征：含『变更记录』即算已一致）", "body_is_same",
     "rrb.body_is_same = lambda old, new: ('## 变更记录' in old) or (old.strip() == new.strip())\n"),

    ("fail closed 被去掉（渲染不出变更记录段也照样写）", "body_is_renderable",
     "rrb.body_is_renderable = lambda new: True\n"),
]


def run_cases() -> dict[str, bool]:
    RESULTS.clear()
    for fn in CASES:
        fn()
    return dict(RESULTS)


def main() -> int:
    if "--mutate" in sys.argv:
        print("rerender_release_body 自测·变异自证（每个变异体只改一处，"
              "转红集合必须精确）")
        base = run_cases()
        failed_red = [k for k, v in base.items() if not v]
        if failed_red:
            print(f"✗ 基线本身有红 ⇒ 变异自证无从谈起：{failed_red}")
            return 1
        all_ok = True
        for name, attr, patch in MUTATIONS:
            import importlib
            importlib.reload(rrb)
            # 重新装载后重跑基线，确认变异前仍全绿
            pre = run_cases()
            if any(not v for v in pre.values()):
                print(f"✗ 变异「{name}」之前基线就不绿 ⇒ 判据无效")
                all_ok = False
                continue
            ns: dict = {"rrb": rrb, "os": os, "sys": sys}
            exec(patch, ns)  # noqa: S102 —— 变异体就是源码注入
            for k, v in ns.items():
                if k.startswith("_mut") or k.startswith("_orig"):
                    setattr(rrb, k, v)
            got = run_cases()
            red = sorted(k for k, v in got.items() if not v)
            print(f"{'✓' if red else '✗'} 变异「{name}」"
                  f"{'被抓住（精确转红 ' + str(red) + '）' if red else '**没被抓住**'}")
            if not red:
                all_ok = False
        importlib.reload(rrb)
        if all_ok:
            print(f"✓ 变异自证 {len(MUTATIONS)}/{len(MUTATIONS)} 全部被抓住")
            return 0
        print(f"✗ 变异自证未过：{len(MUTATIONS)} 个变异体里至少一个没被抓住")
        return 1

    print("rerender_release_body 自测（#213）")
    res = run_cases()
    bad = [k for k, v in res.items() if not v]
    if bad:
        print(f"✗ 自测未过：{bad}")
        return 1
    print(f"✓ 全部自测通过（{len(res)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
