#!/usr/bin/env python3
"""**hostVersion 推导的完备性**自测（matrix-tools#223）。

**这条判据防的是什么（最隐蔽的一种漂移）**：`hostVersion` = 「这次嵌套发布
对应哪个父仓版本」。两个推导：

  - **生成器**（`gen_versions.host_version`）：靠 **commit 祖先关系**——
    候选集 = 该仓**全部父仓根 tag**，取「commit 是嵌套 tag 祖先」的最新者；
  - **门禁**（`verify_release_units.derive_nested_versions`）：靠 **tag 日期序**。

  两者**算法不同源**（#31 的纪律），但**输入曾一度同为「本地 clone 的 tag 集」**
  ⇒ 本地缺父仓根 tag 时**两边一起退化成旧值** ⇒ **假绿**。

**实测（2026-11-03，发版班，工具真实代码）**：

    本地 meta 有 hlog/v0.1.8（嵌套 tag）: True
    本地 meta 有 v0.8.2（父仓根 tag）: False
    ⇒ host_version 返回 'v0.8.1'   （正确答案应是 'v0.8.2'）
    ⇒ 告警输出：无（零 ⚠️）

⇒ 产物里 `hostVersion` **字段在、值是旧的** ⇒ **错得比「字段缺失」更隐蔽**：
字段缺失时门禁立刻判红（8 项 hostVersion 漂移），**错值会被当成事实**
（门禁 oracle C 判的是「已发布值 == 门禁自己的推导值」，两边若都基于同一份
不完整的本地 clone ⇒ 一起错 ⇒ 门禁照样绿）。

**判据要防的三件事**（每条都有对应变异体，见 MUTATIONS）：

  1. **生成侧**：本地候选集不覆盖远端父仓根 tag 全集 ⇒ 必须 **fail closed**
     （返回 None + 显式告警），**不许**返回旧值；
  2. **门禁侧**：推导输入不完整 ⇒ 必须标「未能核验」⇒ oracle C **判红**，
     **不许**因为「推导不出值」就整条静默跳过（`if d["hostVersion"] and ...`）；
  3. **门禁侧**：远端 tag 读不到（读不到 ≠ 没有）⇒ 判据不可用 ⇒ 必须能被
     看见，**不许**悄悄当成「候选集完整」。

**反证怎么构造（变异自证）**：
  - 变异体 1「生成侧退回旧写法」（删掉完备性断言）⇒ 用例 A 必须转红；
  - 变异体 2「门禁侧 oracle C 退回旧写法」（推导不出值就跳过）⇒ 用例 C 必须转红；
  - 变异体 3「门禁侧不核完备性」（拿不完整的本地集直接推导）⇒ 用例 B 必须转红。

**无副作用、不联网**：临时目录造**真实 Git 仓**（真 tag、真 commit、真祖先关系），
远端事实用注入的假 tag 列表（不碰网络）。

用法：
    python3 scripts/host_version_selftest.py            # 正常模式：全部用例通过
    python3 scripts/host_version_selftest.py --mutate   # 变异自证：精确转红集合必须精确
"""
from __future__ import annotations

import io
import contextlib
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gen_versions  # noqa: E402
import verify_release_units as vru  # noqa: E402

# 用例级结果收集（供 --mutate 变异自证比对「精确转红集合」）
_RESULTS: dict[str, bool] = {}
_FAILURES: list[str] = []


def _record(cid: str, good: bool, msg: str = "") -> bool:
    _RESULTS[cid] = good
    if not good and msg:
        _FAILURES.append(msg)
    return good


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _make_repo(root: Path, name: str, *, root_tags: list[str],
               nested_tag: str | None, nested_after: str) -> Path:
    """造一个真实 Git 仓：若干根 tag + 一个嵌套 tag（可选）。

    祖先关系：根 tag 按给定顺序逐个 commit，嵌套 tag 打在 `nested_after`
    那个根 tag 之后 ⇒ 祖先判定有真实语义。

    ⚠️ 必须写 `README.md`：`matrix.repo_dir` 用「go.mod 或 README.md 存在」
    判定「这个目录就是个仓」⇒ 没有它，`repo_dir` 认不出夹具 ⇒
    `_is_ancestor` 静默返回 False ⇒ 候选集为空（自测会假红，且掩盖真缺陷）。
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-q", ".")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "t")
    (d / "README.md").write_text("# fixture\n")
    (d / "f.txt").write_text("x\n")
    _git(d, "add", "README.md", "f.txt")
    for i, tag in enumerate(root_tags):
        (d / "f.txt").write_text(f"x{i}\n")
        _git(d, "commit", "-qam", f"c{i}")
        _git(d, "tag", "-a", tag, "-m", tag)
    if nested_tag:
        (d / "f.txt").write_text("nested\n")
        _git(d, "commit", "-qam", "nested")
        _git(d, "tag", "-a", nested_tag, "-m", nested_tag)
    return d


@contextlib.contextmanager
def _base_at(root: Path):
    """把 gen_versions.BASE 指向临时根（让 repo_dir 能定位夹具仓）。"""
    orig = gen_versions.BASE
    gen_versions.BASE = str(root)
    try:
        yield
    finally:
        gen_versions.BASE = orig


def _meta_of(repo: Path) -> dict[str, tuple[str, int]]:
    """读本地 tag 元数据（与 gen_versions._local_tag_meta 同口径）。"""
    meta: dict[str, tuple[str, int]] = {}
    out = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref",
         "--format=%(refname:short)|%(objectname)|%(*objectname)", "refs/tags"],
        capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        meta[parts[0]] = (parts[2] or parts[1], 0)
    return meta


def _with_base(tmp: Path, monkey_repo: Path):
    """把 gen_versions.BASE 指到临时根，让 repo_dir 能找到夹具仓。"""
    return monkey_repo


# ── 用例 A：生成侧完备性 ────────────────────────────────────────────────────
def case_A_generator_fails_closed_when_candidates_incomplete() -> None:
    """本地缺父仓根 tag ⇒ **必须**返回 None + 告警，**不许**返回旧值。

    这就是 #223 的原始形态：本地有 v0.8.1、缺 v0.8.2、嵌套 tag 在本地 ⇒
    旧写法返回 'v0.8.1'（正确答案是 'v0.8.2'）且**零告警**。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = _make_repo(root, "demo", root_tags=["v0.8.1"], nested_tag="sub/v0.1.0",
                          nested_after="v0.8.1")
        meta = _meta_of(repo)
        # 远端有 v0.8.2（本地没有）⇒ 候选集不完整
        expected_roots = ["v0.8.1", "v0.8.2"]

        buf = io.StringIO()
        with _base_at(root), contextlib.redirect_stdout(buf):
            got = gen_versions.host_version("demo", "sub/v0.1.0", meta,
                                            expected_roots=expected_roots)
        out = buf.getvalue()

        _record("A", got is None,
                f"A: 候选集不完整时必须返回 None（实际 {got!r}）——"
                f"返回旧值比缺字段更隐蔽（错值会被当成事实）")
        _record("A_warns", "⚠️" in out and "v0.8.2" in out,
                f"A_warns: 必须有显式告警且点名缺失的 tag（实际输出 {out!r}）")
        # 旧写法会返回 v0.8.1 —— 明确断言它**不出现**
        _record("A_no_stale", got != "v0.8.1",
                "A_no_stale: 绝不能返回降级的旧值 v0.8.1")


def case_A2_generator_ok_when_complete() -> None:
    """输入完整 ⇒ 必须**照常**推出正确值（完备性断言不许误伤）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = _make_repo(root, "demo", root_tags=["v0.8.1", "v0.8.2"],
                          nested_tag="sub/v0.1.0", nested_after="v0.8.2")
        meta = _meta_of(repo)
        buf = io.StringIO()
        with _base_at(root), contextlib.redirect_stdout(buf):
            got = gen_versions.host_version("demo", "sub/v0.1.0", meta,
                                            expected_roots=["v0.8.1", "v0.8.2"])
        _record("A2", got == "v0.8.2",
                f"A2: 输入完整时必须推出 v0.8.2（实际 {got!r}）")


def case_A3_generator_without_expected_roots_unchanged() -> None:
    """不传 expected_roots（旧调用形态）⇒ 行为不变（不误伤既有调用方）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = _make_repo(root, "demo", root_tags=["v0.8.1", "v0.8.2"],
                          nested_tag="sub/v0.1.0", nested_after="v0.8.2")
        meta = _meta_of(repo)
        with _base_at(root):
            got = gen_versions.host_version("demo", "sub/v0.1.0", meta)
        _record("A3", got == "v0.8.2",
                f"A3: 旧调用形态（不传 expected_roots）行为必须不变（实际 {got!r}）")


# ── 用例 B：门禁侧完备性 ────────────────────────────────────────────────────
def case_B_gate_detects_incomplete_candidates() -> None:
    """门禁侧：本地缺父仓根 tag ⇒ 推导必须标「未能核验」而不是给出偏旧的值。

    用真 Git 仓 + 真 tag 日期序；远端事实用注入的假列表（不联网）。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = _make_repo(root, "demo", root_tags=["v0.8.1"], nested_tag="sub/v0.1.0",
                          nested_after="v0.8.1")
        orig_remote = vru._remote_root_tags
        orig_ensure = vru._ensure_local_tags
        orig_modules = vru.NESTED_MODULES
        try:
            # 远端有 v0.8.2、本地没有；且**禁止** fetch 补齐（模拟补不齐）
            vru._remote_root_tags = lambda repo, base=None: ["v0.8.1", "v0.8.2"]
            vru._ensure_local_tags = lambda *a, **k: None
            vru.NESTED_MODULES = [{"repo": "demo", "path": "sub",
                                    "module": "example.com/demo/sub", "desc": ""}]
            derived = vru.derive_nested_versions(root)
        finally:
            vru._remote_root_tags = orig_remote
            vru._ensure_local_tags = orig_ensure
            vru.NESTED_MODULES = orig_modules
        entry = derived.get("demo/sub", {})
        _record("B", entry.get("hostVersion") is None,
                f"B: 本地候选集不完整时门禁**不许**给出推导值（实际 {entry.get('hostVersion')!r}）")
        _record("B_flag", entry.get("hostUnverifiable") is True,
                "B_flag: 必须标 hostUnverifiable（让 oracle C 能判红）")


def case_B2_gate_ok_when_complete() -> None:
    """门禁侧输入完整 ⇒ 必须照常给出推导值且不标「未能核验」。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = _make_repo(root, "demo", root_tags=["v0.8.1", "v0.8.2"],
                          nested_tag="sub/v0.1.0", nested_after="v0.8.2")
        orig_remote = vru._remote_root_tags
        orig_ensure = vru._ensure_local_tags
        orig_modules = vru.NESTED_MODULES
        try:
            vru._remote_root_tags = lambda repo, base=None: ["v0.8.1", "v0.8.2"]
            vru._ensure_local_tags = lambda *a, **k: None
            vru.NESTED_MODULES = [{"repo": "demo", "path": "sub",
                                    "module": "example.com/demo/sub", "desc": ""}]
            derived = vru.derive_nested_versions(root)
        finally:
            vru._remote_root_tags = orig_remote
            vru._ensure_local_tags = orig_ensure
            vru.NESTED_MODULES = orig_modules
        entry = derived.get("demo/sub", {})
        _record("B2", entry.get("hostVersion") == "v0.8.2",
                f"B2: 输入完整时门禁必须推出 v0.8.2（实际 {entry.get('hostVersion')!r}）")
        _record("B2_flag", not entry.get("hostUnverifiable"),
                "B2_flag: 输入完整时不得标 hostUnverifiable")


# ── 用例 C：oracle C 不得静默跳过 ────────────────────────────────────────────
def case_C_oracle_reds_on_unverifiable() -> None:
    """oracle C：推导「未能核验」⇒ 必须**判红**（旧写法整条静默跳过 ⇒ 假绿）。"""
    published = {"modules": [{"module": "demo",
                              "nested": [{"path": "sub", "latest": "v0.1.0",
                                          "hostVersion": "v0.8.1"}]}]}
    derived = {"demo/sub": {"latest": "v0.1.0", "hostVersion": None,
                             "module": "example.com/demo/sub",
                             "hostUnverifiable": True}}
    problems = vru.check_nested_version_correspondence(published, derived, None)
    _record("C", bool(problems),
            "C: 推导「未能核验」时 oracle C 必须判红（旧写法静默跳过 ⇒ 假绿）")


def case_C2_oracle_reds_on_value_mismatch() -> None:
    """oracle C：值不一致必须判红（既有行为不许被改坏）。"""
    published = {"modules": [{"module": "demo",
                              "nested": [{"path": "sub", "latest": "v0.1.0",
                                          "hostVersion": "v0.8.1"}]}]}
    derived = {"demo/sub": {"latest": "v0.1.0", "hostVersion": "v0.8.2",
                             "module": "example.com/demo/sub"}}
    problems = vru.check_nested_version_correspondence(published, derived, None)
    _record("C2", bool(problems),
            "C2: 已发布值 ≠ 推导值必须判红")


def case_C3_oracle_green_when_equal() -> None:
    """oracle C：两边一致且可核验 ⇒ 必须**判绿**（判据不许变成恒红）。"""
    published = {"modules": [{"module": "demo",
                              "nested": [{"path": "sub", "latest": "v0.1.0",
                                          "hostVersion": "v0.8.2"}]}]}
    derived = {"demo/sub": {"latest": "v0.1.0", "hostVersion": "v0.8.2",
                             "module": "example.com/demo/sub"}}
    problems = vru.check_nested_version_correspondence(published, derived, None)
    _record("C3", not problems,
            f"C3: 两边一致时必须判绿（实际报了 {problems}）")


# ── 变异体（--mutate）────────────────────────────────────────────────────────
def _mutate_generator_no_completeness():
    """生成侧退回旧写法（**删掉**完备性断言）⇒ 用例 A 必须转红。

    ⚠️ 变异必须**语义等价于旧实现**：旧实现就是「不检查候选集完备性，
    直接从本地 meta 取 max」⇒ 这里把 expected_roots 整条判据摘掉。
    """
    orig = gen_versions.host_version

    def mutated(repo, nested_tag, meta, expected_roots=None):
        return orig(repo, nested_tag, meta, expected_roots=None)

    gen_versions.host_version = mutated
    return [("host_version", orig)]


def _mutate_oracle_skip_unverifiable():
    """oracle C 退回旧写法（推导不出值就整条跳过）⇒ 用例 C 必须转红。"""
    orig = vru.check_nested_version_correspondence

    def mutated(published, derived, repos_dir=None):
        # 旧写法：`if d["hostVersion"] and ...` ⇒ 推导不出值 ⇒ 静默跳过
        stripped = {k: {kk: vv for kk, vv in v.items() if kk != "hostUnverifiable"}
                    for k, v in derived.items()}
        problems = []
        nested_by_repo = {}
        for mod in published.get("modules", []):
            for item in mod.get("nested") or []:
                nested_by_repo[f"{mod['module']}/{item['path']}"] = item
        for name, d in sorted(stripped.items()):
            got = nested_by_repo.get(name)
            if not got:
                problems.append(f"已发布 versions.json 缺嵌套模块 {name} 的条目")
                continue
            if got.get("latest") != d["latest"]:
                problems.append(f"{name}: 已发布版本 {got.get('latest')} ≠ 实际最新 {d['latest']}")
            if d["hostVersion"] and got.get("hostVersion") != d["hostVersion"]:
                problems.append(f"{name}: 已发布 hostVersion {got.get('hostVersion')!r} "
                                f"≠ 推导值 {d['hostVersion']!r}（对应父仓版本漂移）")
        return problems

    vru.check_nested_version_correspondence = mutated
    return [("check_nested_version_correspondence", orig)]


def _mutate_gate_no_completeness():
    """门禁侧退回旧写法（不核完备性，直接用不完整的本地集推导）⇒ 用例 B 必须转红。

    ⚠️ 旧实现**根本不查远端**（没有 `_remote_root_tags` 这个概念）⇒
    变异体 = 「拿不完整的本地 tag 集直接推导」。
    """
    orig = vru.derive_nested_versions

    def mutated(repos_dir):
        if repos_dir is None:
            return {}
        out = {}
        for m in vru.NESTED_MODULES:
            repo, path = m["repo"], m["path"]
            dates = vru._tag_dates(repos_dir, repo)
            if not dates:
                continue
            nested = {n: d for n, d in dates.items()
                      if n.startswith(path + "/") and
                      __import__("re").fullmatch(r"v\d+\.\d+\.\d+", n[len(path) + 1:])}
            if not nested:
                continue
            latest_tag = max(nested, key=lambda n: vru._semver_key(n[len(path) + 1:]))
            latest = latest_tag[len(path) + 1:]
            roots = {n: d for n, d in dates.items()
                     if "/" not in n and __import__("re").fullmatch(r"v\d+\.\d+\.\d+", n)}
            cands = [r for r, d in roots.items() if d <= nested[latest_tag]]
            host = max(cands, key=lambda r: (roots[r], vru._semver_key(r))) if cands else None
            out[f"{repo}/{path}"] = {"latest": latest, "hostVersion": host,
                                     "module": m["module"]}
        return out

    vru.derive_nested_versions = mutated
    return [("derive_nested_versions", orig)]


MUTATIONS = [
    # ⚠️ 期望集合必须**按该缺陷的实际影响面列全**（#223 的教训）：
    #   「删掉完备性断言」这一个缺陷，会同时打红**三个断言面**——
    #   A（未返回 None）/ A_warns（无告警）/ A_no_stale（返回了旧值）——
    #   它们是同一缺陷的三个面，**必然同时转红**。
    #   ⇒ 期望集合写成 {A} 会被判「不精确」——但那不是判据没牙齿，
    #   是**期望集合没列全**。
    ("生成侧退回旧写法（删掉完备性断言）",
     _mutate_generator_no_completeness, {"A", "A_warns", "A_no_stale"}),
    ("oracle C 退回旧写法（推导不出值就静默跳过）",
     _mutate_oracle_skip_unverifiable, {"C"}),
    ("门禁侧退回旧写法（不核完备性，直接推导）",
     _mutate_gate_no_completeness, {"B", "B_flag"}),
]


def run_suite() -> None:
    case_A_generator_fails_closed_when_candidates_incomplete()
    case_A2_generator_ok_when_complete()
    case_A3_generator_without_expected_roots_unchanged()
    case_B_gate_detects_incomplete_candidates()
    case_B2_gate_ok_when_complete()
    case_C_oracle_reds_on_unverifiable()
    case_C2_oracle_reds_on_value_mismatch()
    case_C3_oracle_green_when_equal()


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
                if attr == "host_version":
                    gen_versions.host_version = orig
                else:
                    setattr(vru, attr, orig)
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
        print("✗ hostVersion 完备性自测未过：")
        for f_ in _FAILURES:
            print("  -", f_)
        raise SystemExit(1)
    print("✓ hostVersion 完备性自测通过：生成侧候选集不完备 ⇒ fail closed + 显式告警"
          "（不再静默降级成旧值）/ 门禁侧独立核验远端 tag 全集 / "
          "oracle C 对「未能核验」判红（不再静默跳过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
