#!/usr/bin/env python3
"""**检查面 G**自测：打 tag 的模块，其 oekit require 必须是**最新已发布版本**
（matrix-tools#212）。

**这条判据防的是什么**：#148 的门禁只查「依赖**可解析**」（每条 require 要么已发布、
要么本 train 一起发）⇒ **不查「地板是不是最新」** ⇒ **一个带旧地板的产物照样能发出去**。

⇒ 后果：消费方 `go get <mod>@<tag>` 拿到的是**那个 tag 的 go.mod**（旧地板）
⇒ 抬了工作树也不改变消费方拿到的内容 ⇒ 而已发布的 tag **不可重推**
⇒ 只能发新号补 ⇒ **欠一版**。
实测（2026-11-02/03）：`hlog/v0.1.8` 的快照 require `logx v0.8.1` ≠ 父仓最新
`v0.8.2`；`nacosimpl/v0.1.3` 的快照 require `registry v0.5.2` ≠ `v0.5.3`。

**三档判定**（不能一刀切）：
  · **已是最新**（require == 最新已发布版本）⇒ 放行；
  · **落后一档**（require < 最新）⇒ **拒绝**（产物带旧地板 ⇒ 欠一版）；
  · **前瞻引用**（require > 最新，指向未发布版本）⇒ **拒绝**（Go 不允许 require 未发布版本）。

**前瞻引用的例外**：该依赖**在本 train 内**时合法 —— 但**必须父仓先打 tag**
（否则嵌套产物 require 的是未发布版本 ⇒ #148 已拦）。
⇒ 本检查**只判「值」**；顺序由 #148 与发版顺序保证，错误文案指向顺序。

**反证怎么构造（每条都有对应变异体，见 _MUTATIONS）**：
  1. 判据退回「只看可解析」（= #148 现状，不查最新）⇒ 用例 B 必须转红；
  2. 「落后一档」被当成放行 ⇒ 用例 B 必须转红；
  3. 嵌套路径前缀被截成最后一段 ⇒ 用例 C2 必须转红；
  4. 远端读不到时 fail-open ⇒ 用例 D 必须转红（由「fail closed」用例守住）。

**变异自证怎么做的（★ 与其它 selftest 一致）**：把**被测脚本 `release_train.py`
的真实源码**改写成旧实现（不是 mock 掉判据）⇒ 子进程跑一遍自测 ⇒ 比对
「精确转红集合」。⚠️ 变异体必须**语义等价于旧行为**，不能只换函数名。

**无副作用、不联网**：临时目录造 go.mod 夹具；远端 tag 用注入的假列表。

用法：
    python3 scripts/release_train_latest_selftest.py            # 正常模式：全部用例通过
    python3 scripts/release_train_latest_selftest.py --mutate   # 变异自证：精确转红集合必须精确
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SELFTEST = HERE / "release_train_latest_selftest.py"
TARGET = HERE / "release_train.py"

sys.path.insert(0, str(HERE))

# ★ 被测模块可被替换（变异自证时指向「改写后的真实源码」副本）
_MODULE = os.environ.get("RELEASE_TRAIN_MODULE", "release_train")
rt = importlib.import_module(_MODULE)

from matrix import RemoteReadError  # noqa: E402

_RESULTS: dict[str, bool] = {}
_FAILURES: list[str] = []


def _record(cid: str, good: bool, msg: str = "") -> bool:
    _RESULTS[cid] = good
    if not good and msg:
        _FAILURES.append(msg)
    return good


def _module(tmp: Path, name: str, gomod: str) -> Path:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "go.mod").write_text(gomod, encoding="utf-8")
    return d


def _tags(mapping: dict[str, list[str]]):
    """造一个「按仓取远端 tag 全集」的假函数。"""
    def tags_for(repo: str, prefix: str, tags=None) -> str | None:
        lst = mapping.get(repo)
        if lst is None:
            raise RemoteReadError(f"{repo}: 远端 tag 读不到（fixture）")
        best = None
        for t in lst:
            if not t.startswith(prefix):
                continue
            ver = t[len(prefix):]
            if not ver.startswith("v"):
                continue
            if best is None or rt._semver_key(ver) > rt._semver_key(best):
                best = ver
        return best
    return tags_for


# ── 用例 A：已是最新 ⇒ 放行 ────────────────────────────────────────────────
def case_A_latest_passes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _module(tmp, "m", "module example.com/m\n\nrequire github.com/oekit/logx v0.8.2\n")
        failures: list[str] = []
        rt.check_requires_latest("m", "m", d, {}, failures, 0,
                                 tags_for=_tags({"logx": ["v0.8.1", "v0.8.2"]}))
        _record("A", not failures,
                f"A: require 已是最新应放行（实际 {failures}）")


# ── 用例 B：落后一档 ⇒ 拒绝并点名 ──────────────────────────────────────────
def case_B_stale_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _module(tmp, "m", "module example.com/m\n\nrequire github.com/oekit/logx v0.8.1\n")
        failures: list[str] = []
        rt.check_requires_latest("m", "m", d, {}, failures, 0,
                                 tags_for=_tags({"logx": ["v0.8.1", "v0.8.2"]}))
        _record("B", bool(failures),
                "B: require 落后一档必须被拒绝（产物带旧地板 ⇒ tag 不可重推 ⇒ 欠一版）")
        joined = " ".join(failures)
        _record("B_names", "v0.8.1" in joined and "v0.8.2" in joined and "logx" in joined,
                f"B_names: 报错必须点名（当前值/最新值/仓）：{joined}")


# ── 用例 C：嵌套路径 require + 跨仓 require 都参与判定 ──────────────────────
def case_C_nested_and_cross_repo() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 嵌套依赖：logx/hlog 的 tag 前缀是 `hlog/`（完整子路径）
        d = _module(tmp, "m",
                    "module example.com/m\n\n"
                    "require github.com/oekit/logx/hlog v0.1.7\n"
                    "require github.com/oekit/registry v0.5.2\n")
        failures: list[str] = []
        rt.check_requires_latest("m", "m", d, {}, failures, 0,
                                 tags_for=_tags({
                                         "logx": ["v0.8.2", "hlog/v0.1.7", "hlog/v0.1.8"],
                                         "registry": ["v0.5.2", "v0.5.3"],
                                     }))
        joined = " ".join(failures)
        _record("C", bool(failures),
                "C: 跨仓与嵌套路径的 require 都必须参与判定（实际无失败）")
        # 嵌套那条：logx/hlog v0.1.7 < 最新 hlog/v0.1.8 ⇒ 必须判落后
        _record("C_nested", "hlog" in joined,
                f"C_nested: 嵌套依赖必须被点名（前缀须是完整子路径 `hlog/`）：{joined}")
        _record("C_cross", "registry" in joined and "v0.5.3" in joined,
                f"C_cross: 跨仓依赖必须被点名（registry v0.5.2 < v0.5.3）：{joined}")


# ── 用例 C2：嵌套路径前缀不能被截成最后一段 ───────────────────────────────
def case_C2_nested_prefix_not_truncated() -> None:
    """`observability/tracing/instrumentation/kitexotel` 的前缀必须是完整子路径。

    若前缀被截成最后一段 `kitexotel/` ⇒ 取到的「最新」是别的模块的版本
    ⇒ 判定会用到**错的**基准 ⇒ 放行/拒绝都不对。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _module(tmp, "m",
                    "module example.com/m\n\n"
                    "require github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel v0.7.6\n")
        failures: list[str] = []
        rt.check_requires_latest("m", "m", d, {}, failures, 0,
                                 tags_for=_tags({
                                     "kitex": ["v0.7.5",
                                               "observability/tracing/instrumentation/kitexotel/v0.7.6",
                                               "observability/tracing/instrumentation/kitexotel/v0.7.7"],
                                 }))
        joined = " ".join(failures)
        _record("C2", bool(failures) and "v0.7.7" in joined,
                f"C2: 深层嵌套必须按完整子路径取最新（应点到 v0.7.7）：{joined}")


# ── 用例 D：远端读不到 ⇒ fail closed ───────────────────────────────────────
def case_D_remote_unreadable_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _module(tmp, "m", "module example.com/m\n\nrequire github.com/oekit/logx v0.8.2\n")
        failures: list[str] = []
        rt.check_requires_latest("m", "m", d, {}, failures, 0,
                                 tags_for=_tags({}))   # 该仓读不到 ⇒ 抛
        _record("D", bool(failures),
                "D: 远端读不到必须 fail closed（读不到 ≠ 已发布）")


# ── 用例 E：前瞻引用 ⇒ 拒绝 ────────────────────────────────────────────────
def case_E_forward_ref_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _module(tmp, "m", "module example.com/m\n\nrequire github.com/oekit/logx v0.9.0\n")
        failures: list[str] = []
        rt.check_requires_latest("m", "m", d, {}, failures, 0,
                                 tags_for=_tags({"logx": ["v0.8.2"]}))
        _record("E", bool(failures),
                "E: 前瞻引用（require 未发布版本）必须被拒绝（Go 不允许）")


# ── 用例 F：同 train 内一起发（依赖的 tag 先于本模块）⇒ 放行 ─────────────────
def case_F_planned_in_train_allowed() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _module(tmp, "m", "module example.com/m\n\nrequire github.com/oekit/logx v0.9.0\n")
        failures: list[str] = []
        # 本 train 内一起发 logx v0.9.0（idx 0）⇒ 本模块（idx 1）不算「前瞻引用」
        rt.check_requires_latest("m", "m", d, {("logx", "v0.9.0"): 0}, failures, 1,
                                 tags_for=_tags({"logx": ["v0.8.2"]}))
        _record("F", not failures,
                f"F: 同 train 内一起发（且先于本模块打 tag）的依赖不该被拒绝（实际 {failures}）")


# ── 用例 G：同 train 内但**顺序颠倒** ⇒ 拒绝并指向「顺序」 ──────────────────
def case_G_planned_but_order_reversed() -> None:
    """依赖在本 train 内，但它的 tag **排在本模块之后** ⇒ 必须拒绝。

    Go 不允许 require 未发布版本 ⇒ 顺序颠倒 ⇒ 本模块的产物会 require 一个
    **尚未发布**的版本 ⇒ 下游解析失败。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _module(tmp, "m", "module example.com/m\n\nrequire github.com/oekit/logx v0.9.0\n")
        failures: list[str] = []
        # 待发依赖 logx v0.9.0 在 idx 1，本模块在 idx 0 ⇒ 顺序颠倒
        rt.check_requires_latest("m", "m", d, {("logx", "v0.9.0"): 1}, failures, 0,
                                 tags_for=_tags({"logx": ["v0.8.2"]}))
        _record("G", bool(failures),
                "G: 同 train 内但顺序颠倒（依赖的 tag 排在本模块之后）必须被拒绝")
        joined = " ".join(failures)
        _record("G_order", "顺序" in joined and "logx" in joined and "v0.9.0" in joined,
                f"G_order: 报错必须点名「顺序」与依赖（当前值/待发 tag）：{joined}")


# ── 用例 H：父仓与嵌套**版本号相同**（实测形态 timertask v0.2.7）─────────────
def case_H_same_version_no_key_collision() -> None:
    """父仓 `timertask v0.2.7` + 嵌套 `timertask/redis v0.2.7` ⇒ 版本号**相同**。

    ⇒ 若「同 train 内」的键只用「(仓, 版本号)」⇒ 两个模块**撞键**
    ⇒ 后写的覆盖先写的 ⇒ 判定用到**错的**下标 ⇒ 合法形态被误判。
    ⇒ 故键必须是**完整 tag**（`v0.2.7` 与 `redis/v0.2.7` 区分得开）。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 嵌套模块 redis（idx 1）require 父仓 timertask v0.2.7
        d = _module(tmp, "redis",
                    "module example.com/m\n\nrequire github.com/oekit/timertask v0.2.7\n")
        # train：父仓 idx 0（tag v0.2.7）+ 嵌套 idx 1（tag redis/v0.2.7）
        planned = rt.planned_order_for("timertask", [
            ("main", tmp, "v0.2.7"),
            ("redis", tmp / "redis", "redis/v0.2.7"),
        ])
        failures: list[str] = []
        rt.check_requires_latest("timertask", "redis", d, planned, failures, 1,
                                 tags_for=_tags({"timertask": ["v0.2.6"]}))
        _record("H", not failures,
                f"H: 父仓与嵌套版本号相同时不该撞键（合法形态被误判，实际 {failures}）")


def run_suite() -> None:
    case_A_latest_passes()
    case_B_stale_rejected()
    case_C_nested_and_cross_repo()
    case_C2_nested_prefix_not_truncated()
    case_D_remote_unreadable_fails_closed()
    case_E_forward_ref_rejected()
    case_F_planned_in_train_allowed()
    case_G_planned_but_order_reversed()
    case_H_same_version_no_key_collision()


# ═══ 变异自证（--mutate）══════════════════════════════════════════════════
#
# **为什么要有**：检查面 G 是**门禁式断言** —— 一旦被改成恒放行，所有症状都消失
# （旧地板照样打进 tag）⇒ 必须自证「它抓得住自己要防的东西」。
#
# **怎么构造**：把**被测脚本 `release_train.py` 的真实源码**改写成旧实现
# （真实退回旧行为，不是 mock 掉判据）⇒ 子进程跑一遍自测 ⇒ 比对「精确转红集合」。
#
# ⚠️ 期望集合必须**按该缺陷的实际影响面列全**（#223 的教训）：
#   同一个缺陷可能打红多条断言 ⇒ 少列会被判「不精确」，
#   但那不是判据没牙齿，是**期望集合没列全**。
# ⚠️ 变异体必须**语义等价于旧实现** ⇒ 只换函数名可能不生效（#223 踩过两次）。

_MUTATIONS = [
    # ① 判据退回「只看可解析」（= #148 现状，不查「是否最新」）⇒ B/C/C2/E 必须转红
    ("判据退回「只看可解析」（= #148 现状，不查是否最新）",
     ('        if ver == latest:\n            continue  # 已是最新 ⇒ 放行\n',
      '        if True:\n            continue  # ★ 变异：退回旧实现（不查「是否最新」）⇒ 全部放行\n'),
     {"B", "B_names", "C", "C_nested", "C_cross", "C2", "E"}),
    # ② 「落后一档」被当成放行（只保留前瞻引用检查）⇒ B/C/C2 必须转红
    ("「落后一档」被当成放行",
     ('        if _semver_key(ver) < _semver_key(latest):\n            failures.append(\n',
      '        if _semver_key(ver) < _semver_key(latest):\n            continue  # ★ 变异：落后一档被当成放行\n            failures.append(\n'),
     {"B", "B_names", "C", "C_nested", "C_cross", "C2"}),
    # ③ 嵌套路径前缀被截成最后一段 ⇒ 只有 C2 必须转红
    ("嵌套路径前缀被截成最后一段",
     ('        prefix = tag[:-(len(ver))] if tag.endswith(ver) else ""\n',
      '        _head = tag[:-(len(ver))] if tag.endswith(ver) else ""\n'
      '        prefix = (_head.strip("/").rsplit("/", 1)[-1] + "/") if _head.strip("/") else ""\n'),
     {"C2"}),
    # ④ 远端读不到时 fail-open（当「没问题」）⇒ D 必须转红
    ("远端读不到时 fail-open",
     ('        except RemoteReadError as e:\n            failures.append(f"{name}: 远端 tag 读不到（{e}）⇒ 无法判定 "\n',
      '        except RemoteReadError as e:\n            continue  # ★ 变异：读不到就当没问题（fail open）\n            failures.append(f"{name}: 远端 tag 读不到（{e}）⇒ 无法判定 "\n'),
     {"D"}),
    # ⑤ 「同 train 内」不判顺序（顺序颠倒也放行）⇒ G/H_rev 必须转红
    ("「同 train 内」不判顺序（顺序颠倒也放行）",
     ('            if dep_idx < cur_idx:\n'
      '                # 依赖的 tag **先于**本模块创建 ⇒ 合法形态（父仓先打 tag）\n'
      '                continue\n',
      '            if True:\n'
      '                # ★ 变异：同 train 内一律放行（不判顺序）\n'
      '                continue\n'),
     {"G", "G_order"}),
    # ⑥ 「同 train 内」的键撞名（只用版本号 ⇒ 父仓与嵌套同版本号时互相覆盖）⇒ H 必须转红
    ("「同 train 内」的键撞名（只用版本号 ⇒ 父仓与嵌套同版本号时互相覆盖）",
     ('    return {(repo, tag): i for i, (_n, _d, tag) in enumerate(specs)}\n',
      '    return {(repo, tag.rsplit("/", 1)[-1]): i for i, (_n, _d, tag) in enumerate(specs)}\n'),
     {"H"}),
]


def _run_mutations() -> int:
    """改写真实源码 ⇒ 子进程跑自测 ⇒ 比对「精确转红集合」。"""
    src = TARGET.read_text(encoding="utf-8")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for name, (old, new), expect in _MUTATIONS:
            if old not in src:
                print(f"✗ 变异「{name}」：源里找不到待替换片段 ⇒ 变异体没生效")
                print(f"      片段：{old!r}")
                ok = False
                continue
            mutated = src.replace(old, new, 1)
            mmod = tmp / "release_train_mutated.py"
            mmod.write_text(mutated, encoding="utf-8")
            env = dict(os.environ)
            env["RELEASE_TRAIN_MODULE"] = "release_train_mutated"
            env["PYTHONPATH"] = os.pathsep.join(
                [str(tmp), str(ROOT), str(HERE)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
            r = subprocess.run([sys.executable, str(SELFTEST)],
                               capture_output=True, text=True, env=env)
            out = r.stdout + r.stderr
            got: set[str] = set()
            for line in out.splitlines():
                s = line.strip()
                if s.startswith("FAILED-CASES:"):
                    got = {c.strip() for c in s.split(":", 1)[1].split(",") if c.strip()}
            if not got:
                # 变异没让任何用例失败 ⇒ 判据没牙齿（或变异体根本没生效）
                print(f"✗ 变异「{name}」**没有让任何用例失败** ⇒ 判据没牙齿"
                      f"（退出码 {r.returncode}）")
                print(out[-800:])
                ok = False
                continue
            if got == expect:
                print(f"✓ 变异「{name}」被抓住（精确转红 {sorted(got)}）")
            else:
                ok = False
                print(f"✗ 变异「{name}」转红集合**不精确** ⇒ "
                      f"期望 {sorted(expect)}，实际 {sorted(got)}")
                print(out[-800:])
    if ok:
        print(f"✓ 变异自证 {len(_MUTATIONS)}/{len(_MUTATIONS)} 全部**精确**转红")
        return 0
    print(f"✗ 变异自证未过：{len(_MUTATIONS)} 个变异体里至少一个转红集合不精确")
    return 1


def main() -> int:
    if "--mutate" in sys.argv:
        return _run_mutations()

    _RESULTS.clear()
    _FAILURES.clear()
    run_suite()
    failed = sorted(cid for cid, good in _RESULTS.items() if not good)
    # ★ 机器可读行：变异自证靠它抽「精确转红集合」
    print("FAILED-CASES: " + ",".join(failed))
    if _FAILURES:
        print("✗ 检查面 G 自测未过：")
        for f_ in _FAILURES:
            print("  -", f_)
        raise SystemExit(1)
    print("✓ 检查面 G 自测通过：已是最新 ⇒ 放行 / 落后一档 ⇒ 拒绝并点名 / "
          "前瞻引用 ⇒ 拒绝 / 同 train 内一起发（且先于本模块打 tag）⇒ 放行 / "
          "同 train 内但顺序颠倒 ⇒ 拒绝并指向顺序 / "
          "父仓与嵌套同版本号 ⇒ 不撞键 / "
          "跨仓与嵌套路径都参与判定（完整子路径前缀）/ 远端读不到 ⇒ fail closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
