#!/usr/bin/env python3
"""release_train「被 tag 模块的 oekit 依赖必须下游可解析」门禁自测（matrix-tools#148）。

**为什么要有这个自测**：这条门禁防的是**静默失效**——

  嵌套模块对父仓有 `replace github.com/oekit/<父仓> => ../`（指向本地源码）
  ⇒ 本仓 `go mod tidy -diff` / `go build` **全绿** ⇒ 门禁前的既有检查**全都不出声**
  ⇒ 而下游消费方解析该嵌套模块时，Go **忽略依赖里的 replace** ⇒ 会照着 go.mod
    去模块代理取父仓的 require 版本 ⇒ 该版本没发布 ⇒ 消费方直接解析失败。

  也就是说：**这条门禁一旦被改成恒绿，所有症状都消失**（本地全绿、CI 全绿），
  破坏只会在消费方那边显形 ⇒ 必须自证「它抓得住自己要防的东西」。

**★ 这条自测此前有一个「声称 ≠ 事实」的缺口**：下面的「反证怎么构造」原本只是
docstring 里的一段**声明**，脚本里**没有任何机制去验证它** ⇒ 自测可能「一直跑过」
却从未证明过自己真能转红。⇒ 现在补 `--mutate` 模式：注入变异体 ⇒ 比对
**精确转红集合**（该红的红、不该红的还绿）⇒ 并已挂进 CI（`.github/workflows/audit.yml`）。

**反证怎么构造（现在每一条都有对应的变异体，见 MUTATIONS）**：

  1. 门禁函数直接 return（恒绿）⇒ 用例 2/3b/4/5/6/12/13 必须失败；
  2. `required_tag_form` 把嵌套路径当**根形态**（用最后一段或空子路径）⇒ 用例 5/5b/10 必须失败；
  3. 远端读不到时 fail-open（当成「已发布」）⇒ 用例 6/13 必须失败；
  4. 把 replace 块里的条目当 require ⇒ 用例 1/3/8 必须失败；
  5. 「只发嵌套模块」守卫退化成**一刀切**（恒拒绝）⇒ 用例 11 必须失败；
  6. 同一守卫退化成**恒放行** ⇒ 用例 12/13 必须失败。

**无副作用、不联网**：纯函数 + 临时目录造 fixture，远端事实用注入的假 tag_exists。

用法：
    python3 scripts/release_train_requires_selftest.py            # 正常模式：全部用例通过
    python3 scripts/release_train_requires_selftest.py --mutate   # 变异自证：精确转红集合必须精确
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_train  # noqa: E402
from matrix import RemoteReadError  # noqa: E402


# ── 用例级结果收集（供 --mutate 变异自证比对「精确转红集合」）──────────
#   ★ 为什么要逐用例登记：只收一个扁平 failures 列表的话，变异体把某个用例
#     弄红时无法定位「是谁红了」⇒ 变异自证就退化成「跑一下看看红不红」，
#     证明不了「转红集合是否精确」。
_RESULTS: dict[str, bool] = {}
_FAILURES: list[str] = []


def _record(cid: str, good: bool, msg: str = "") -> bool:
    """登记一个用例的结果；不通过时把说明追加到 _FAILURES。"""
    _RESULTS[cid] = good
    if not good and msg:
        _FAILURES.append(msg)
    return good


def fake_tags(published: dict[str, set[str]]):
    """假 tag_exists：published[repo] 是该仓已发布的 tag 集合。"""
    def _f(repo: str, tag: str) -> bool:
        return tag in published.get(repo, set())
    return _f


def fake_unreadable(repo: str, tag: str) -> bool:
    raise RemoteReadError(f"模拟远端读不到：{repo}@{tag}")


def write_gomod(tmp: Path, body: str) -> Path:
    d = tmp / "mod"
    d.mkdir(parents=True, exist_ok=True)
    (d / "go.mod").write_text(body, encoding="utf-8")
    return d


def run(body: str, planned: set[tuple[str, str]], published: dict[str, set[str]],
        tag_exists=None) -> list[str]:
    """跑一次门禁，返回 failures 列表。"""
    with tempfile.TemporaryDirectory() as td:
        mod = write_gomod(Path(td), body)
        failures: list[str] = []
        release_train.check_requires_resolvable(
            "demo", "main", mod, planned, failures,
            tag_exists=tag_exists if tag_exists is not None else fake_tags(published))
        return failures


def run_suite() -> None:
    """跑全部用例并逐个登记结果（正常模式与 --mutate 模式共用这一份）。"""

    # --- 用例 1：父仓地板**已发布** ⇒ 放行 ---
    body = ("module example.com/nested\n\ngo 1.21\n\n"
            "require github.com/oekit/demo v0.2.6\n\n"
            "replace github.com/oekit/demo => ../\n")
    f = run(body, {("demo", "v0.2.7")}, {"demo": {"v0.2.6"}})
    _record("1", not f, f"用例1：父仓地板已发布时应放行，实际判红={f}")

    # --- 用例 2：父仓地板**未发布**且不在本 train ⇒ 判红（核心用例）---
    f = run(body, {("demo", "v0.2.7")}, {"demo": {"v0.2.5"}})
    good = bool(f)
    msg = "用例2：父仓地板未发布且不在本 train 时必须判红（这是本门禁要防的那一刀）"
    if good:
        for token in ("v0.2.6", "demo"):
            if token not in f[0]:
                good = False
                msg = f"用例2：判红消息里应点名 {token}（实际={f[0]!r}）"
                break
    _record("2", good, msg)

    # --- 用例 3：父仓地板**就是本 train 要发的版本** ⇒ 放行 ---
    f = run(body, {("demo", "v0.2.6")}, {"demo": {"v0.2.5"}})
    _record("3", not f, f"用例3：父仓地板在本 train 内一起发版时应放行，实际判红={f}")

    # --- 用例 3b：planned_tags 必须**带仓名**（同名版本在别的仓是另一个东西）---
    f = run(body, {("other", "v0.2.6")}, {"demo": {"v0.2.5"}})
    _record("3b", bool(f),
            "用例3b：planned 里的 tag 必须按 (仓, tag) 配对——"
            "别的仓的同名 v0.2.6 不算本仓父仓已发版")

    # --- 用例 4：根模块依赖**另一个仓**的未发布版本 ⇒ 判红 ---
    body4 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require github.com/oekit/other v0.9.9\n")
    f = run(body4, {("demo", "v0.2.7")}, {"other": {"v0.9.8"}})
    good = bool(f)
    msg = "用例4：依赖另一个仓的未发布版本时必须判红"
    if good and "other" not in f[0]:
        good = False
        msg = f"用例4：判红消息应点名 other（实际={f[0]!r}）"
    _record("4", good, msg)

    # --- 用例 5：跨仓**嵌套模块**依赖 ⇒ tag 形态必须是「子路径/版本」---
    body5 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require github.com/oekit/logx/hlog v0.1.6\n")
    # 代理里只有根形态 v0.1.6（logx 根模块的 tag）⇒ 嵌套形态 hlog/v0.1.6 不存在 ⇒ 判红
    f = run(body5, set(), {"logx": {"v0.1.6"}})
    _record("5", bool(f),
            "用例5：跨仓嵌套依赖必须按「子路径/版本」形态找 tag——"
            "只认根形态 v0.1.6 会把「读不到」当成「已发布」")
    # 正确的形态存在 ⇒ 放行
    f = run(body5, set(), {"logx": {"hlog/v0.1.6"}})
    _record("5b", not f, f"用例5b：嵌套形态 hlog/v0.1.6 已发布时应放行，实际判红={f}")

    # --- 用例 5c/5d：**多层**嵌套（子路径 ≠ 最后一段）---
    #   ★ 为什么要单独测多层：用例 5/5b 的路径是 `logx/hlog`（**单层**）⇒
    #     「只取最后一段」这个 bug 对它**完全无效** ⇒ 光有用例 5/5b 抓不到
    #     「子路径被截断」这类退化（实测：变异自证把用例 5/5b 判为绿，
    #     只有用例 10 抓到了 ⇒ 是**用例有盲区**，不是变异体写错）。
    #   ⇒ 多层路径必须用 `kitex/observability/tracing/instrumentation/kitexotel`。
    body5c = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
              "require github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel "
              "v0.7.4\n")
    # 代理里只有「最后一段」形态 kitexotel/v0.7.4 ⇒ 完整子路径形态不存在 ⇒ 判红
    f = run(body5c, set(), {"kitex": {"kitexotel/v0.7.4"}})
    _record("5c", bool(f),
            "用例5c：多层嵌套依赖必须按**完整子路径**找 tag——"
            "只认最后一段形态会把「读不到」当成「已发布」")
    # 完整子路径形态存在 ⇒ 放行
    f = run(body5c, set(),
            {"kitex": {"observability/tracing/instrumentation/kitexotel/v0.7.4"}})
    _record("5d", not f,
            f"用例5d：多层嵌套的完整子路径形态已发布时应放行，实际判红={f}")

    # --- 用例 6：远端读不到 ⇒ **fail closed**（读不到 ≠ 已发布）---
    f = run(body4, set(), {}, tag_exists=fake_unreadable)
    _record("6", bool(f), "用例6：远端读不到时必须判红（fail closed），不得当成「已发布」")

    # --- 用例 7：非 oekit 依赖 ⇒ 忽略 ---
    body7 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require golang.org/x/sync v0.7.0\n\n"
             "require github.com/stretchr/testify v1.9.0\n")
    f = run(body7, set(), {})
    _record("7", not f, f"用例7：非 oekit 依赖不该判红，实际={f}")

    # --- 用例 8：replace 块里的条目**不是** require ---
    # （require 块里的 other v0.9.8 是**已发布**的真依赖 ⇒ 不该判红；
    #   replace 块里的 phantom v8.8.8 未发布，但它根本不是 require ⇒ 更不该判红）
    body8 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require (\n\tgithub.com/oekit/other v0.9.8 // indirect\n)\n\n"
             "replace (\n\tgithub.com/oekit/phantom v8.8.8 => ../phantom\n)\n")
    f = run(body8, set(), {"other": {"v0.9.8"}})
    _record("8", not f, f"用例8：replace 块里的条目不该被当成 require，实际={f}")

    # --- 用例 9：块状 require + // indirect 注释 ⇒ 正确解析 ---
    body9 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require (\n\tgithub.com/oekit/other v0.9.8 // indirect\n)\n")
    f = run(body9, set(), {"other": {"v0.9.8"}})
    _record("9", not f, f"用例9：块状 require（含 indirect 注释）应正确解析并放行，实际判红={f}")

    # --- 用例 10：required_tag_form 的形态对照 ---
    cases = [
        ("github.com/oekit/demo", "v0.2.6", ("demo", "v0.2.6")),
        ("github.com/oekit/logx/hlog", "v0.1.6", ("logx", "hlog/v0.1.6")),
        ("github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel",
         "v0.7.4",
         ("kitex", "observability/tracing/instrumentation/kitexotel/v0.7.4")),
    ]
    bad = []
    for path, ver, want in cases:
        got = release_train.required_tag_form(path, ver)
        if got != want:
            bad.append(f"required_tag_form({path!r}, {ver!r}) = {got}，期望 {want}")
    _record("10", not bad, "用例10：" + "；".join(bad))

    # ══ 守卫层（用例 11-14）：「本次 train 只有嵌套模块」时的**条件守卫** ══
    #   ★ 这层此前**没有任何用例覆盖** ⇒ 一刀切守卫（只发嵌套 ⇒ 无条件拒绝）
    #     曾长期挡死「把新地板带进已发布产物」的唯一修法而无人发现。
    def gate(published: dict[str, set[str]], tag_exists=None) -> tuple[bool, str]:
        """跑 nested_only_gate：只发一个嵌套模块（无 main=）⇒ 返回 (放行?, 文本)。"""
        with tempfile.TemporaryDirectory() as td:
            mod = write_gomod(Path(td),
                              "module github.com/oekit/demo\n\ngo 1.21\n\n"
                              "require github.com/oekit/dep v1.2.3\n")
            specs = [("sub", mod, "sub/v0.1.0")]
            return release_train.nested_only_gate(
                "demo", specs,
                tag_exists=tag_exists if tag_exists is not None else fake_tags(published))

    # --- 用例 11：只发嵌套 + require **已发布** ⇒ 必须**放行** ---
    ok, msg = gate({"dep": {"v1.2.3"}})
    _record("11", ok,
            "用例11：require 已发布时只发嵌套**必须放行**"
            f"（一刀切守卫会误杀安全用例 ⇒ 这是它要防的）实际={msg[:160]!r}")

    # --- 用例 12：只发嵌套 + require **未发布** ⇒ 必须**拒绝**且报错点名 ---
    ok, msg = gate({"dep": set()})
    good = not ok
    m = "用例12：require 未发布时只发嵌套**必须拒绝**" \
        "（否则下游消费方解析失败 —— matrix-tools#148）"
    if good and ("v1.2.3" not in msg or "dep" not in msg):
        good = False
        m = f"用例12：报错必须点名未发布的 require（实际={msg[:200]!r}）"
    _record("12", good, m)

    # --- 用例 13：只发嵌套 + 远端读不到 ⇒ **fail closed**（读不到 ≠ 已发布） ---
    ok, msg = gate({}, tag_exists=fake_unreadable)
    _record("13", not ok, "用例13：远端读不到时必须判红（fail closed），不得当成「已发布」")

    # --- 用例 14：train 里**带 main=** ⇒ 守卫不介入（直接放行，不做任何判定） ---
    with tempfile.TemporaryDirectory() as td:
        mod = write_gomod(Path(td), "module github.com/oekit/demo\n\ngo 1.21\n")
        ok, msg = release_train.nested_only_gate(
            "demo", [("main", mod, "v0.2.0"), ("sub", mod, "sub/v0.1.0")])
        _record("14", ok and not msg,
                f"用例14：train 带 main= 时守卫不应介入（ok={ok}, msg={msg[:120]!r}）")


# ── 变异自证（--mutate）─────────────────────────────────────────────────
#   ★ 每个变异体 = 替换被检代码里的**判定点**（不是替换整个被检函数，
#     否则测的就不是它了）⇒ 期望转红集合按「该变异真的破坏了谁」写死。
#   ★ **多红是正确行为**：判定点被换掉时，凡是依赖它的用例都该红；
#     漏写一个 ⇒ 框架判「转红不精确」⇒ 逼人按真实依赖关系写全。
def _mutate_gate_always_block():
    """守卫退化成**一刀切**（只发嵌套 ⇒ 恒拒绝）⇒ 用例 11 必须抓到。"""
    orig = release_train.gate_verdict
    release_train.gate_verdict = lambda unresolved: True
    return [("gate_verdict", orig)]


def _mutate_gate_never_block():
    """守卫退化成**恒放行**（未发布 require 也放行）⇒ 用例 12/13 必须抓到。"""
    orig = release_train.gate_verdict
    release_train.gate_verdict = lambda unresolved: False
    return [("gate_verdict", orig)]


def _mutate_fail_open():
    """远端读不到时 fail-open（当成「已发布」）⇒ 用例 6/13 必须抓到。"""
    orig = release_train.remote_read_failed
    release_train.remote_read_failed = lambda exc: False
    return [("remote_read_failed", orig)]


def _mutate_gate_always_green():
    """依赖可解析性门禁恒绿（直接 return）⇒ 用例 2/3b/4/5/6/12/13 必须抓到。"""
    orig = release_train.check_requires_resolvable
    release_train.check_requires_resolvable = (
        lambda repo, name, module_dir, planned_tags, failures, tag_exists=None: None)
    return [("check_requires_resolvable", orig)]


def _mutate_tag_form_last_segment():
    """required_tag_form 把嵌套路径当**根形态**（只取最后一段）⇒ 用例 5/5b/10 必须抓到。"""
    def bad(module_path: str, version: str):
        parts = module_path.split("/")
        if len(parts) < 3 or parts[0] != "github.com" or parts[1] != "oekit":
            raise ValueError(f"不是 oekit 模块路径：{module_path}")
        target_repo = parts[2]
        subs = parts[3:]
        if subs and re.fullmatch(r"v[2-9]\d*", subs[-1]):
            return target_repo, version
        if not subs:
            return target_repo, version
        return target_repo, subs[-1] + "/" + version   # ★ 错：只取最后一段
    orig = release_train.required_tag_form
    release_train.required_tag_form = bad
    return [("required_tag_form", orig)]


def _mutate_replace_as_require():
    """replace 块里的条目被当成 require ⇒ 用例 1/3/8 必须抓到。"""
    orig = release_train.parse_go_mod_requires

    def patched(text: str):
        out = list(orig(text))
        # ★ replace 块里的条目**可能没有版本号**（`replace X => ../`）⇒ 正则要覆盖
        #   两种形态，否则这个变异体抓不到「无版本号 replace」那条路径。
        for m in re.finditer(r"(github\.com/oekit/[^\s]+)(?:\s+v[^\s]+)?\s*=>", text):
            out.append((m.group(1), "v0.0.0"))
        return out

    release_train.parse_go_mod_requires = patched
    return [("parse_go_mod_requires", orig)]


MUTATIONS = [
    ("守卫退化成一刀切（只发嵌套 ⇒ 恒拒绝）",
     _mutate_gate_always_block, {"11"}),
    ("守卫退化成恒放行（未发布 require 也放行）",
     _mutate_gate_never_block, {"12", "13"}),
    ("远端读不到 fail-open（当成已发布）",
     _mutate_fail_open, {"6", "13"}),
    ("依赖可解析性门禁恒绿（直接 return）",
     _mutate_gate_always_green, {"2", "3b", "4", "5", "5c", "6", "12", "13"}),
    ("required_tag_form 把嵌套路径当根形态（只取最后一段）",
     _mutate_tag_form_last_segment, {"5c", "5d", "10"}),
    ("replace 块里的条目被当成 require",
     _mutate_replace_as_require, {"1", "3", "8"}),
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
                setattr(release_train, attr, orig)
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
        print("✗ release_train 依赖可解析性门禁自测未过：")
        for f_ in _FAILURES:
            print("  -", f_)
        raise SystemExit(1)
    print("✓ release_train 依赖可解析性门禁自测通过：未发布的父仓地板/跨仓依赖判红、"
          "本 train 同发放行、远端读不到 fail closed、replace 块不当 require；"
          "守卫层：只发嵌套时 require 已发布⇒放行 / 未发布⇒拒绝 / 读不到⇒fail closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
