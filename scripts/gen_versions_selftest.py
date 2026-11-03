#!/usr/bin/env python3
"""gen_versions 的自测：嵌套 CHANGELOG 扫全深度 + 完整子路径前缀（改造C）
+ 读不到仓必须告警（改造D）。

**这个文件防什么**

1. **改造C：嵌套 CHANGELOG 只扫一层 ⇒ 深度 4 的 kitexotel 脱离判据**
   （实测 `glob("*/go.mod")` 只给一层、`glob("**/go.mod")` 才给到
   `observability/tracing/instrumentation/kitexotel`）⇒ 它的独立 CHANGELOG
   一条 breaking 都进不了索引。

2. **改造C：版本号前缀必须是「相对仓根的完整子路径」**
   （旧写法用 `sub.name` 只取最后一段 ⇒ 与嵌套 tag 前缀
   `observability/tracing/instrumentation/kitexotel/v0.7.7` 对不上；
   版本头归一时也会把完整子路径截成最后一段）。

3. **改造C：站点 nested 条目自带 breakingIndex**（嵌套的 breaking 不再只能
   在宿主仓里找）。

4. **改造D：读不到仓/读不到依赖 ⇒ 明确告警**（不许静默消失、静默变空）。

**变异自证（--mutate）**：把实现**改回旧写法**（源码级替换 → 临时目录里
加载变异体）⇒ 对应用例必须**精确转红**。

**无副作用、不联网**：全部在临时目录里造 fixture 仓（`git init` + commit +
tag；「远端」用本地 bare 仓当 origin，fetch 不出网）。

用法：
  python3 scripts/gen_versions_selftest.py             # 正常模式
  python3 scripts/gen_versions_selftest.py --mutate     # 变异自证
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import gen_versions  # noqa: E402

# 用例级结果收集（供 --mutate 比对「精确转红集合」）
_RESULTS: dict[str, bool] = {}
_FAILURES: list[str] = []

DEEP = "a/b/c/d"          # 深度 4 的嵌套模块（真实形态：observability/tracing/instrumentation/kitexotel）
NESTED_TAG = f"{DEEP}/v0.1.0"


def _record(cid: str, good: bool, msg: str = "") -> bool:
    _RESULTS[cid] = good
    if not good and msg:
        _FAILURES.append(msg)
    return good


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{r.stderr.strip()[:200]}")
    return r.stdout.strip()


def make_repo(root: Path, name: str = "demo", *, with_deep: bool = True) -> Path:
    """造一个含**深度 4 嵌套模块**的 fixture 仓（带自己的 go.mod + CHANGELOG）。

    ⚠️ 必须写 README.md：`matrix.repo_dir` 用「go.mod 或 README.md 存在」
    判定「这个目录就是个仓」⇒ 认不出夹具时后续判据会静默失效。
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "selftest@example.invalid")
    git(d, "config", "user.name", "selftest")
    (d / "go.mod").write_text("module example.invalid/demo\n\ngo 1.24.0\n",
                              encoding="utf-8")
    (d / "README.md").write_text("# fixture\n", encoding="utf-8")
    (d / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [v0.2.0] — 2026-11-01\n\n"
        "### Breaking\n"
        "- **根模块破坏**：Foo 签名改了\n\n"
        f"## [{DEEP}/v0.2.0] — 2026-11-01\n\n"
        "### Breaking\n"
        "- **宿主仓里的嵌套段破坏**：Bar 路径变了\n",
        encoding="utf-8")
    if with_deep:
        sub = d / DEEP
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "go.mod").write_text(
            f"module example.invalid/demo/{DEEP}\n\ngo 1.26.0\n", encoding="utf-8")
        (sub / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## [v0.1.0] — 2026-11-01\n\n"
            "### Breaking\n"
            "- **嵌套自己的破坏**：Baz 没了\n",
            encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    git(d, "tag", "v0.1.0")
    if with_deep:
        git(d, "tag", NESTED_TAG)
    return d


def make_origin(root: Path, src: Path) -> Path:
    """本地 bare 仓当 origin（不出网）。"""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git(src, "remote", "add", "origin", str(origin))
    git(src, "push", "-q", "origin", "main", "--tags")
    return origin


@contextlib.contextmanager
def _base_at(mod, base: str):
    """把 mod.BASE 指向临时根（让 repo_dir 能定位夹具仓）。"""
    orig = mod.BASE
    mod.BASE = base
    try:
        yield
    finally:
        mod.BASE = orig


@contextlib.contextmanager
def _patched(mod, **attrs):
    """临时替换模块属性（用例里避开网络）。"""
    old = {k: getattr(mod, k) for k in attrs}
    for k, v in attrs.items():
        setattr(mod, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(mod, k, v)


def _versions(items) -> list[str]:
    return [it["version"] for it in items]


# ── 改造C：深度 + 完整子路径前缀 ──────────────────────────────────────────
def case_C1_nested_changelog_any_depth(gv=gen_versions) -> None:
    """**改造C 牙齿**：深度 4 的嵌套模块的**独立 CHANGELOG** 必须进索引。

    旧写法（`glob("*/go.mod")` 只扫一层）⇒ 这条根本收不到 ⇒ 必须转红。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_repo(root)
        with _base_at(gv, str(root)):
            items = gv.breaking_index("demo")
        vers = _versions(items)
        ok = f"{DEEP}/v0.1.0" in vers
        _record("C1", ok,
                f"深度 4 嵌套模块的独立 CHANGELOG 必须进索引，实得 {vers}")
        # 宿主仓 CHANGELOG 里的嵌套段也要在（版本头带**完整子路径**）
        ok2 = f"{DEEP}/v0.2.0" in vers
        _record("C2", ok2,
                f"版本头里的完整子路径不得被截成最后一段，实得 {vers}")


def case_C2_nested_prefix_is_full_subpath(gv=gen_versions) -> None:
    """**改造C 牙齿**：嵌套 CHANGELOG 的版本号前缀 = **相对仓根的完整子路径**。

    旧写法用 `sub.name`（只取最后一段）⇒ 变成 "d/v0.1.0" ⇒ 与嵌套 tag
    前缀对不上 ⇒ 必须转红。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_repo(root)
        with _base_at(gv, str(root)):
            items = gv.breaking_index("demo")
        vers = _versions(items)
        ok = f"{DEEP}/v0.1.0" in vers and "d/v0.1.0" not in vers
        _record("C3", ok,
                f"嵌套条目的前缀必须是完整子路径（{DEEP}/v0.1.0），实得 {vers}")


def case_C3_split_by_module(gv=gen_versions) -> None:
    """条目按**模块**拆开：根模块一条、嵌套模块一条（互不串门）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_repo(root)
        with _base_at(gv, str(root)):
            by = gv.breaking_index_by_module("demo")
        root_vers = _versions(by.get("", []))
        deep_vers = _versions(by.get(DEEP, []))
        ok = root_vers == ["v0.2.0"] and deep_vers == [f"{DEEP}/v0.2.0", f"{DEEP}/v0.1.0"]
        _record("C4", ok,
                f"按模块拆分必须干净：根={root_vers} 嵌套={deep_vers}")


def case_C4_site_nested_entry_has_breaking_index(gv=gen_versions) -> None:
    """**改造C**：站点 nested 条目自带 breakingIndex（嵌套的 breaking 不再
    只在宿主仓里找得到）。

    需要一个「远端」⇒ 用本地 bare 仓当 origin（不出网）。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = make_repo(root)
        make_origin(root, src)
        with _base_at(gv, str(root)):
            nested = gv.nested_versions("demo")
        ok_shape = bool(nested) and "breakingIndex" in nested[0]
        _record("C5", ok_shape,
                f"nested 条目必须带 breakingIndex，实得键集合 "
                f"{sorted(nested[0].keys()) if nested else '（无嵌套条目）'}")
        if not nested:
            return
        vers = _versions(nested[0]["breakingIndex"])
        ok_content = f"{DEEP}/v0.1.0" in vers and "v0.2.0" not in vers
        _record("C6", ok_content,
                f"nested 条目的 breakingIndex 必须是**该嵌套模块自己的**条目，"
                f"实得 {vers}")


def case_C5_module_level_breaking_index_keeps_full_set(gv=gen_versions) -> None:
    """模块级 breakingIndex 仍是**全量集合**（根 + 嵌套）。

    ⚠️ 这是与 audit 的**双向逐条比对**门禁（`check_breaking_index`）的契约：
    产物里的模块级 breakingIndex 必须与 `breaking_index(repo)` **逐条一致**
    ⇒ 少一条就报「漏收」。按模块拆开的视图请用 nested 条目与
    `breaking_index_by_module`。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = make_repo(root)
        make_origin(root, src)
        with _base_at(gv, str(root)), _patched(
                gv, MODULES=["demo"],
                latest_version=lambda r: "v0.2.0"):
            data = gv.build()
            live = gv.breaking_index("demo")
        mod_entry = data["modules"][0]
        idx = _versions(mod_entry["breakingIndex"])
        live_vers = _versions(live)
        ok = sorted(idx) == sorted(live_vers)
        _record("C7", ok,
                f"模块级 breakingIndex 必须与 breaking_index(repo) 逐条一致"
                f"（audit 双向门禁口径），实得产物={idx} 解析器={live_vers}")
        ok_all = set(idx) == {"v0.2.0", f"{DEEP}/v0.2.0", f"{DEEP}/v0.1.0"}
        _record("C8", ok_all, f"模块级 breakingIndex 应含根 + 嵌套全部条目，实得 {idx}")


# ── 改造D：读不到 ⇒ 明确告警 ─────────────────────────────────────────────
def case_D1_missing_repo_warns(gv=gen_versions) -> None:
    """读不到仓 ⇒ **必须告警**（不许静默从产物里消失）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        err = io.StringIO()
        with _base_at(gv, str(root)), _patched(gv, MODULES=["ghost"]), \
                contextlib.redirect_stderr(err):
            data = gv.build()
        out = err.getvalue()
        ok = "ghost" in out and "⚠️" in out
        _record("D1", ok, f"读不到仓必须告警（点名仓库 + ⚠️），实得 stderr={out[:200]!r}")
        _record("D2", data["modules"] == [], "读不到的模块不得出现在产物里")


def case_D2_deps_read_failure_warns(gv=gen_versions) -> None:
    """依赖读不到 ⇒ 必须告警「不是零依赖」（不许静默 deps=[]）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 只有 README.md、没有 go.mod ⇒ repo_dir 认得出它，但 sibling_deps 读不到
        d = root / "demo"
        d.mkdir()
        (d / "README.md").write_text("# fixture\n", encoding="utf-8")
        err = io.StringIO()
        with _base_at(gv, str(root)), _patched(
                gv, MODULES=["demo"],
                latest_version=lambda r: "v0.1.0",
                nested_versions=lambda r, breaking_by_module=None: []), \
                contextlib.redirect_stderr(err):
            gv.build()
        out = err.getvalue()
        ok = "demo" in out and "⚠️" in out and ("读不到" in out or "零依赖" in out)
        _record("D3", ok,
                f"依赖读不到必须告警且说明「不是零依赖」，实得 stderr={out[:240]!r}")


# ── 用例表 ────────────────────────────────────────────────────────────────
CASES = [
    ("C1 改造C：嵌套 CHANGELOG 扫全深度（深度 4 不脱离判据）",
     case_C1_nested_changelog_any_depth, ["C1"]),
    ("C2 改造C：版本头保留完整子路径（不被截成最后一段）",
     case_C1_nested_changelog_any_depth, ["C2"]),
    ("C3 改造C：嵌套条目前缀 = 相对仓根的完整子路径",
     case_C2_nested_prefix_is_full_subpath, ["C3"]),
    ("C4 改造C：条目按模块拆开（根/嵌套互不串门）",
     case_C3_split_by_module, ["C4"]),
    ("C5 改造C：站点 nested 条目自带 breakingIndex",
     case_C4_site_nested_entry_has_breaking_index, ["C5"]),
    ("C6 改造C：nested 的 breakingIndex 只含该嵌套模块自己的条目",
     case_C4_site_nested_entry_has_breaking_index, ["C6"]),
    ("C7 契约：模块级 breakingIndex 与 breaking_index(repo) 逐条一致（audit 口径）",
     case_C5_module_level_breaking_index_keeps_full_set, ["C7"]),
    ("C8 契约：模块级 breakingIndex 含根 + 嵌套全部条目",
     case_C5_module_level_breaking_index_keeps_full_set, ["C8"]),
    ("D1 改造D：读不到仓 ⇒ 明确告警", case_D1_missing_repo_warns, ["D1"]),
    ("D2 改造D：读不到的模块不静默进产物", case_D1_missing_repo_warns, ["D2"]),
    ("D3 改造D：依赖读不到 ⇒ 告警「不是零依赖」", case_D2_deps_read_failure_warns, ["D3"]),
]


def run_suite(gv=gen_versions) -> None:
    for _title, fn, ids in CASES:
        try:
            fn(gv)
        except Exception as e:  # noqa: BLE001 — 变异体把实现改崩也算「没抓住」
            for cid in ids:
                _record(cid, False, f"用例执行抛异常（变异体可能把实现改崩了）：{e!r}")


# ── 变异体（--mutate）：把实现改回旧写法 ───────────────────────────────────
def _load_mutated(name: str, replacements: list[tuple[str, str]]):
    """把 gen_versions.py 按 replacements 改回旧写法 ⇒ 临时目录里加载变异体。"""
    src = (HERE / "gen_versions.py").read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in src:
            raise AssertionError(f"变异锚点不存在（源码已变？）：{old[:80]!r}")
        src = src.replace(old, new, 1)
    tmp = Path(tempfile.mkdtemp())
    p = tmp / f"{name}.py"
    p.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mutate_glob_one_level():
    """改造C 退回旧写法：嵌套 CHANGELOG 只扫一层（`*/go.mod`）。"""
    return _load_mutated("gv_mut_glob", [(
        'for gomod in sorted(root.glob("**/go.mod")):',
        'for gomod in sorted(root.glob("*/go.mod")):',
    )])


def _mutate_prefix_last_segment():
    """改造C 退回旧写法：嵌套前缀用 `sub.name`（只取最后一段）。"""
    return _load_mutated("gv_mut_prefix", [(
        'items.extend(_scan_changelog(sub / "CHANGELOG.md", prefix=f"{rel}/", repo=repo))',
        'items.extend(_scan_changelog(sub / "CHANGELOG.md", '
        'prefix=f"{sub.name}/", repo=repo))',
    )])


def _mutate_normalize_truncates_path():
    """改造C 退回旧写法：版本头归一时把完整子路径截成最后一段。"""
    return _load_mutated("gv_mut_norm", [(
        'SEMVER_PATH_RE = re.compile(\n'
        '    r"(?:[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/)?v\\d+\\.\\d+\\.\\d+")',
        'SEMVER_PATH_RE = re.compile(r"[A-Za-z0-9_.-]*/?v\\d+\\.\\d+\\.\\d+")',
    )])


def _mutate_silent_missing_repo():
    """改造D 退回旧写法：读不到仓 ⇒ 静默跳过（零告警）。"""
    return _load_mutated("gv_mut_silent", [(
        """        except FileNotFoundError as e:
            _warn_unreadable(r, f"仓检出失败（{e}）", "该模块**不在**本次产物里")
            continue""",
        """        except FileNotFoundError:
            continue""",
    )])


def _mutate_silent_deps():
    """改造D 退回旧写法：依赖读不到 ⇒ 静默 deps=[]（零告警）。"""
    return _load_mutated("gv_mut_silent_deps", [(
        """        except Exception as e:  # noqa: BLE001
            # ⚠️ **读不到 ≠ 没有依赖**（改造D）：旧写法静默 `deps = []`
            # ⇒ 产物里这个模块的依赖列**看起来就是「零依赖」** ⇒ 下游照它
            # 升版会拉到不兼容组合，且**零告警**。
            _warn_unreadable(r, f"依赖读取失败（{e}）",
                              "matrixDeps 记为空（**读不到**，不是「零依赖」）")
            deps = []""",
        """        except Exception:
            deps = []""",
    )])


MUTATIONS = [
    # ⚠️ 期望集合必须**按该缺陷的实际影响面列全**（同 host_version_selftest 的纪律）：
    #   「嵌套 CHANGELOG 只扫一层」这一个缺陷，会同时打红 C1（深度 4 收不到）、
    #   C3（嵌套条目前缀缺了）、C4（拆分后嵌套模块少一条）、C6（nested 条目少一条）、
    #   C8（模块级少一条）——它们是同一缺陷的五个面，**必然同时转红**。
    ("改造C 退回旧写法：嵌套 CHANGELOG 只扫一层（*/go.mod）",
     _mutate_glob_one_level, {"C1", "C3", "C4", "C6", "C8"}),
    ("改造C 退回旧写法：嵌套前缀用 sub.name（只取最后一段）",
     _mutate_prefix_last_segment, {"C1", "C3", "C4", "C6", "C8"}),
    ("改造C 退回旧写法：版本头归一截断完整子路径",
     _mutate_normalize_truncates_path, {"C2", "C4", "C8"}),
    ("改造D 退回旧写法：读不到仓 ⇒ 静默跳过",
     _mutate_silent_missing_repo, {"D1"}),
    ("改造D 退回旧写法：依赖读不到 ⇒ 静默置空",
     _mutate_silent_deps, {"D3"}),
]


def main() -> int:
    if "--mutate" in sys.argv:
        ok = True
        for name, mutate, expect in MUTATIONS:
            _RESULTS.clear()
            _FAILURES.clear()
            mod = mutate()
            try:
                run_suite(mod)
            finally:
                sys.modules.pop(getattr(mod, "__name__", ""), None)
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

    _RESULTS.clear()
    _FAILURES.clear()
    run_suite(gen_versions)
    failed = [cid for cid, good in _RESULTS.items() if not good]
    for cid, good in _RESULTS.items():
        print(f"  {'✓' if good else '✗'} {cid}")
    if failed:
        print(f"\n❌ selftest 失败 {len(failed)}/{len(_RESULTS)}")
        for f_ in _FAILURES:
            print("  -", f_)
        return 1
    print(f"\n✓ gen_versions selftest 全绿（{len(_RESULTS)} 个断言面 / "
          f"{len(CASES)} 个用例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
