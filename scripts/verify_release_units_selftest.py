#!/usr/bin/env python3
"""发版单元门禁的自测：证明这道判据「抓得住自己要防的东西」，且不假绿。

**为什么要有这个自测**（2026-10-23 实测，issue #82）：这道门禁原先只校验
24 根 + 9 嵌套 = 33 个发版单元，**漏了伞仓 BOM 仓 `oekit`**（第 34 个）。
把 llms.txt 里伞仓那行 `go get github.com/oekit/oekit@latest` 整段删掉，
门禁**照样全绿**（实测 exit=0）⇒ 站点「llms.txt 覆盖 34 个发版单元」的
承诺**无人守**。

一道没人验证过的门禁，很可能自己就是个假绿——本仓 selftest job 的既有
原则（`close_issue_selftest` / `dependabot_dirs_selftest` / `ghsafe_selftest` /
`remote_reads_selftest`）在这里同样适用。

**反证怎么构造**：把 llms.txt 的关键内容分别掐掉，本自测必须**失败**：
  1. 伞仓安装行被删 ⇒ 必须判红（这是 #82 的原始形态）。
  2. 某个嵌套模块的安装行被删 ⇒ 必须判红（确认新增的伞仓检查没把老的顶掉）。
  3. 某个根模块条目被删 ⇒ 必须判红（确认根模块覆盖没被顶掉）。

**「判不了 ≠ 通过」的三处漏检（2026-11-03 实测修）**：门禁自己的纪律写着
「读不到 ⇒ 判据不可用 ⇒ 不静默放过」，实现却是反的 ⇒ **判据失效时门禁照绿**
⇒ 对下游说谎：
  4. **远端读不到**（`matrix.remote_tags` 抛 `RemoteReadError`）⇒ 完备性核验
     整段被跳过 ⇒ 照旧产出 hostVersion 且**不标**未能核验（用例 D1/D2）。
  5. **产物里有、推导里没有**的嵌套模块 ⇒ oracle C 只做正向差集 ⇒ 对它
     **完全失明** ⇒ 它在产物里的版本可以是任意错值（用例 D3/D4/D8）。
  6. **`hostVersion` 推导为 None 且未标「未能核验」** ⇒ 整条静默跳过 ⇒
     产物里的错值（哪怕 v9.9.9）也不判红（用例 D5）。

**无副作用、不联网、不依赖工作区布局**：llms.txt 用内存文本造 fixture；
嵌套模块的推导用**临时目录里的真实 Git 仓**（真 tag、真 commit、真日期序），
远端事实用**注入的假 tag 列表**（不碰网络）。

用法：
    python3 scripts/verify_release_units_selftest.py            # 正常模式
    python3 scripts/verify_release_units_selftest.py --mutate   # 变异自证
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # matrix.py 在仓根，不在 scripts/ 下

import matrix  # noqa: E402
from matrix import MODULES, NESTED_MODULES, RemoteReadError  # noqa: E402
import verify_release_units as vru  # noqa: E402
from verify_release_units import (  # noqa: E402
    _GO_GET_RE,
    check_llms_units,
    release_unit_count,
    render_nested_section,
)

BOM_MOD = "github.com/oekit/oekit"

# 用例级结果收集（供 --mutate 变异自证比对「精确转红集合」）
_RESULTS: dict[str, bool] = {}
_FAILURES: list[str] = []


def _record(cid: str, good: bool, msg: str = "") -> bool:
    _RESULTS[cid] = good
    if not good and msg:
        _FAILURES.append(msg)
    return good


def make_llms() -> str:
    """造一份**合法**的 llms.txt 文本：24 根条目 + 9 嵌套条目 + 9 嵌套安装行 + 伞仓安装行。

    根模块条目形态与站点一致（`- [name](url): 描述`），伞仓按站点现状放在
    正文里（它不是矩阵模块，不进「## 模块」段）。
    """
    lines = ["# oekit 机器可读索引（fixture）", "",
             "- 一次拉齐经 CI 验证的兼容组合：`go get github.com/oekit/oekit@latest`", ""]
    lines += ["## 模块", ""]
    for m in MODULES:
        lines.append(f"- [{m}](https://github.com/oekit/{m}): 描述")
    lines += ["", render_nested_section()]
    return "\n".join(lines) + "\n"


def drop_install_line(text: str, module: str) -> str:
    """删掉某个 module 的 `go get ...` 安装行。"""
    out = []
    for line in text.splitlines():
        m = _GO_GET_RE.search(line)
        if m and m.group(1) == module:
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def drop_root_entry(text: str, module: str) -> str:
    """删掉某个根模块的条目行。"""
    return "\n".join(
        line for line in text.splitlines()
        if not line.startswith(f"- [{module}](https://github.com/oekit/{module})")
    ) + "\n"


# ── 真实 Git 夹具（嵌套模块推导用）──────────────────────────────────────────
def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _make_repo(root: Path, name: str, *, root_tags: list[str],
               nested_tag: str | None) -> Path:
    """造一个**真实** Git 仓：若干根 tag（各带日期序）+ 可选的嵌套 tag。

    ⚠️ 必须写 `README.md`：`matrix.repo_dir` 用「go.mod 或 README.md 存在」
    判定「这个目录就是个仓」⇒ 没有它，远端读不到（夹具里会被误判成
    「远端读不到」⇒ 掩盖真缺陷）。

    tag 用**附注 tag**（`tag -a`）⇒ `%(creatordate:unix)` 取的是 tagger 日期
    ⇒ 门禁的「tag 日期序」推导在夹具里有真实语义。
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


def _derive_with_remote(root: Path, remote_behavior) -> dict:
    """在**注入的远端行为**下跑门禁自己的推导（不联网、不碰真仓）。

    `remote_behavior(repo, base)` → list（假的远端 tag 集）或抛异常（读不到）。
    """
    orig_remote = matrix.remote_tags
    orig_ensure = vru._ensure_local_tags
    orig_modules = vru.NESTED_MODULES
    try:
        matrix.remote_tags = remote_behavior
        vru._ensure_local_tags = lambda *a, **k: None  # 不许真的去 fetch
        vru.NESTED_MODULES = [{"repo": "demo", "path": "sub",
                                "module": "example.com/demo/sub", "desc": ""}]
        return vru.derive_nested_versions(root)
    finally:
        matrix.remote_tags = orig_remote
        vru._ensure_local_tags = orig_ensure
        vru.NESTED_MODULES = orig_modules


def _published(*entries: tuple[str, str, str]) -> dict:
    """造一份已发布 versions.json 的 nested 片段：(path, latest, hostVersion)。"""
    return {"modules": [{"module": "logx",
                          "nested": [{"path": p, "latest": l, "hostVersion": h}
                                     for p, l, h in entries]}]}


def _derived(name: str, latest: str | None, host: str | None, *,
             unverifiable: bool = False) -> dict:
    return {name: {"latest": latest, "hostVersion": host,
                   "module": "github.com/oekit/logx/hlog",
                   "hostUnverifiable": unverifiable}}


# ── 用例 L：llms.txt 覆盖（#82 的原始形态）─────────────────────────────────
def case_L0_legal_fixture_has_no_problem() -> None:
    """合法 fixture ⇒ 必须零问题（先证明 fixture 本身合法，否则后面的
    「变异必须判红」可能只是因为 fixture 本来就坏）。"""
    problems = check_llms_units(make_llms())
    _record("L0", not problems,
            f"L0 期望零问题（合法 fixture），实得 {problems}")


def case_L1_umbrella_install_line_removed_must_red() -> None:
    """删掉伞仓安装行 ⇒ 必须判红（#82 的原始形态）。"""
    base = make_llms()
    mutated = drop_install_line(base, BOM_MOD)
    if BOM_MOD in mutated:
        _record("L1", False, "L1 fixture 变异失败：伞仓安装行还在")
        return
    problems = check_llms_units(mutated)
    _record("L1", bool(problems) and any("伞仓" in p for p in problems),
            f"L1 期望抓到「llms.txt 缺伞仓 BOM 安装行」，实得 {problems}")


def case_L2_nested_install_line_removed_must_red() -> None:
    """删掉某个嵌套模块的安装行 ⇒ 必须判红（确认新检查没顶掉老的）。"""
    target = NESTED_MODULES[0]
    problems = check_llms_units(drop_install_line(make_llms(), target["module"]))
    _record("L2", bool(problems) and any(target["module"] in p for p in problems),
            f"L2 期望抓到「缺嵌套模块 {target['module']} 的安装行」，实得 {problems}")


def case_L3_root_entry_removed_must_red() -> None:
    """删掉某个根模块条目 ⇒ 必须判红（确认根模块覆盖没被顶掉）。"""
    target = MODULES[0]
    problems = check_llms_units(drop_root_entry(make_llms(), target))
    _record("L3", bool(problems) and any(target in p for p in problems),
            f"L3 期望抓到「llms.txt 缺根模块 {target} 条目」，实得 {problems}")


def case_L4_unit_count_is_canonical() -> None:
    """口径必须是 canonical 口径（24 根 + 9 嵌套 + 1 伞仓）——不许分裂。"""
    root, nested, bom, total = release_unit_count()
    _record("L4", (root, nested, bom) == (len(MODULES), len(NESTED_MODULES), 1),
            f"L4 发版单元口径不对：{root} 根 + {nested} 嵌套 + {bom} 伞仓")
    _record("L4_total", total == root + nested + bom,
            f"L4 合计口径不自洽：{total} ≠ {root}+{nested}+{bom}")
    _record("L4_bom", bom == 1,
            f"L4 伞仓应为 1 个发版单元，实得 {bom}"
            f"（伞仓有独立 tag + 独立 Release + go get 安装路径）")


# ── 用例 D1/D2：缺陷 1（远端读不到 ⇒ 必须判红）──────────────────────────────
def case_D1_remote_unreadable_must_be_unverifiable() -> None:
    """缺陷 1：远端 tag **读不到** ⇒ 推导必须标「未能核验」且**不产出** hostVersion。

    旧实现：`except Exception: return None` ⇒ `if remote_roots is not None:`
    整段完备性核验被跳过 ⇒ 照旧产出 hostVersion（可能偏旧）且**不标**未能核验。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_repo(root, "demo", root_tags=["v0.8.1", "v0.8.2"], nested_tag="sub/v0.1.0")

        def boom(repo, base=None):
            raise RemoteReadError(f"{repo}: 远端 tag 读不到（夹具注入）")

        entry = _derive_with_remote(root, boom).get("demo/sub", {})
        _record("D1a", entry.get("hostUnverifiable") is True,
                f"D1a: 远端读不到时必须标 hostUnverifiable"
                f"（实际 {entry.get('hostUnverifiable')!r}）")
        _record("D1b", entry.get("hostVersion") is None,
                f"D1b: 远端读不到时必须**不产出** hostVersion"
                f"（实际 {entry.get('hostVersion')!r}）——候选集完整性未知时给出的值"
                f"可能偏旧 ⇒ 不能当事实")


def case_D2_remote_unreadable_makes_oracle_red() -> None:
    """缺陷 1 的端到端形态：远端读不到 ⇒ oracle C 必须判红。

    夹具里产物写的 hostVersion **恰好等于**本地旧值（v0.8.2）⇒
    只有「未能核验」这条判据能抓住它 ⇒ 证明门禁不会因为「值碰巧对」而放过。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_repo(root, "demo", root_tags=["v0.8.1", "v0.8.2"], nested_tag="sub/v0.1.0")

        def boom(repo, base=None):
            raise RemoteReadError(f"{repo}: 远端 tag 读不到（夹具注入）")

        derived = _derive_with_remote(root, boom)
        published = {"modules": [{"module": "demo",
                                  "nested": [{"path": "sub", "latest": "v0.1.0",
                                              "hostVersion": "v0.8.2"}]}]}
        problems = vru.check_nested_version_correspondence(published, derived, None)
        _record("D2", bool(problems) and any("未能核验" in p for p in problems),
                f"D2: 远端读不到 ⇒ oracle C 必须判红（实际 {problems}）")


# ── 用例 D3/D4：缺陷 2（产物里有、推导里没有 ⇒ 必须判红）────────────────────
def case_D3_published_not_in_derived_must_red() -> None:
    """缺陷 2：published 有、derived 没有 ⇒ 必须报「未能核验」（旧实现返回 []）。

    实测复现：published 里 logx/hlog 写成 v0.99.9、logx/sink 写成 v0.0.1，
    derived={} ⇒ 旧实现零告警。
    """
    published = _published(("hlog", "v0.99.9", "v9.9.9"),
                           ("sink", "v0.0.1", "v0.0.1"))
    problems = vru.check_nested_version_correspondence(published, {}, None)
    _record("D3", bool(problems)
            and any("logx/hlog" in p for p in problems)
            and any("logx/sink" in p for p in problems),
            f"D3: published 有而 derived 没有 ⇒ 必须点名报「未能核验」（实际 {problems}）")


def case_D4_partially_derived_must_red_for_the_missing_one() -> None:
    """缺陷 2 的另一半：derived **非空**但缺某个模块 ⇒ 那个模块仍必须被点名。"""
    published = _published(("hlog", "v0.1.9", "v0.8.2"),
                           ("sink", "v0.0.1", "v0.0.1"))
    derived = _derived("logx/hlog", "v0.1.9", "v0.8.2")
    problems = vru.check_nested_version_correspondence(published, derived, None)
    _record("D4", bool(problems) and any("logx/sink" in p for p in problems),
            f"D4: derived 缺 logx/sink ⇒ 必须点名它「未能核验」（实际 {problems}）")


# ── 用例 D5：缺陷 3（hostVersion=None 且未标未能核验 ⇒ 必须判红）────────────
def case_D5_host_version_none_must_red() -> None:
    """缺陷 3：`hostVersion` 推导为 None 且**未标**「未能核验」⇒ 必须判红。

    旧实现 `if d["hostVersion"] and ...` ⇒ 推导不出值 ⇒ **整条静默跳过**
    ⇒ 产物里的 v9.9.9 也不判红。
    """
    published = _published(("hlog", "v0.1.9", "v9.9.9"))
    derived = _derived("logx/hlog", "v0.1.9", None)
    problems = vru.check_nested_version_correspondence(published, derived, None)
    _record("D5", bool(problems) and any("hostVersion" in p for p in problems),
            f"D5: hostVersion=None 且未标未能核验 ⇒ 必须判红（旧实现整条静默跳过）"
            f"（实际 {problems}）")


# ── 用例 D6/D7：新判据不许变成恒红 / 既有行为不许被改坏 ─────────────────────
def case_D6_equal_and_verifiable_must_be_green() -> None:
    """两边一致且可核验 ⇒ 必须零问题（判据不许变成恒红）。"""
    published = _published(("hlog", "v0.1.9", "v0.8.2"))
    derived = _derived("logx/hlog", "v0.1.9", "v0.8.2")
    problems = vru.check_nested_version_correspondence(published, derived, None)
    _record("D6", not problems,
            f"D6: 两边一致且可核验时必须零问题（实际报了 {problems}）")


def case_D7_derived_but_not_published_still_red() -> None:
    """既有行为不许被改坏：derived 有、published 没有 ⇒ 报「缺条目」。"""
    derived = _derived("logx/hlog", "v0.1.9", "v0.8.2")
    problems = vru.check_nested_version_correspondence({"modules": []}, derived, None)
    _record("D7", bool(problems) and any("缺嵌套模块" in p for p in problems),
            f"D7: derived 有而 published 没有 ⇒ 必须报「缺条目」（实际 {problems}）")


# ── 用例 D8：缺陷 2 的根因（推不出来的模块不许从 derived 里消失）────────────
def case_D8_underivable_module_must_not_vanish() -> None:
    """推不出事实的嵌套模块**必须登记**在 derived 里（旧实现直接 `continue`）。

    夹具：仓里有根 tag 但**没有嵌套 tag** ⇒ 推导不出 latest ⇒
    旧实现让它从 derived 里彻底消失 ⇒ oracle C 对它完全失明。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_repo(root, "demo", root_tags=["v0.8.1"], nested_tag=None)
        derived = _derive_with_remote(root, lambda repo, base=None: ["v0.8.1"])
        entry = derived.get("demo/sub")
        _record("D8a", entry is not None,
                "D8a: 推不出来的模块必须**登记**在 derived 里（旧实现让它消失）")
        _record("D8b", bool(entry)
                and entry.get("latest") is None
                and entry.get("hostUnverifiable") is True,
                f"D8b: 登记形态必须是「未能核验」（latest=None + hostUnverifiable=True）"
                f"（实际 {entry}）")
        published = {"modules": [{"module": "demo",
                                  "nested": [{"path": "sub", "latest": "v9.9.9",
                                              "hostVersion": "v9.9.9"}]}]}
        problems = vru.check_nested_version_correspondence(published, derived, None)
        _record("D8c", bool(problems) and any("demo/sub" in p for p in problems),
                f"D8c: 推不出来的模块在产物里的任意错值都必须被抓（实际 {problems}）")


def run_suite() -> None:
    case_L0_legal_fixture_has_no_problem()
    case_L1_umbrella_install_line_removed_must_red()
    case_L2_nested_install_line_removed_must_red()
    case_L3_root_entry_removed_must_red()
    case_L4_unit_count_is_canonical()
    case_D1_remote_unreadable_must_be_unverifiable()
    case_D2_remote_unreadable_makes_oracle_red()
    case_D3_published_not_in_derived_must_red()
    case_D4_partially_derived_must_red_for_the_missing_one()
    case_D5_host_version_none_must_red()
    case_D6_equal_and_verifiable_must_be_green()
    case_D7_derived_but_not_published_still_red()
    case_D8_underivable_module_must_not_vanish()


# ── 变异体（--mutate）────────────────────────────────────────────────────────
def _mutate_remote_unreadable_treated_as_complete():
    """变异体 M1：把「远端**读不到**」当成「远端根 tag 集为空（候选集完整）」。

    ⚠️ 这与旧实现**行为等价**：旧实现 `except Exception: return None` ⇒
    `if remote_roots is not None:` 整段跳过 ⇒ 等价于「候选集完整、不标未能核验」。
    ⇒ 用「远端返回空集」这个等价形态注入，比改源码更贴近旧语义。
    """
    orig = vru._remote_root_tags
    vru._remote_root_tags = lambda repo, base=None: []
    return [("_remote_root_tags", orig)]


def _mutant_oracle_C(hole: str):
    """oracle C 变异体：`hole` 指明要**摘掉**哪个新判据（= 退回该判据的旧写法）。

    - hole="reverse"    ⇒ 删掉**反向差集**那段（旧写法：只做正向差集 ⇒
      published 有而 derived 没有 ⇒ 静默放过）；
    - hole="host_none" ⇒ 把 hostVersion 比较退回旧写法
      `if d["hostVersion"] and ...`（推导不出值 ⇒ **整条静默跳过**）。

    ⚠️ 这里是 oracle C 的**逐份拷贝**（去掉被摘掉的判据）——变异体必须与旧写法
    **行为等价**，所以只能拷贝，不能转发（原函数转发会把被摘掉的判据带回来）。
    """
    def mutated(published, derived, repos_dir=None):
        problems: list[str] = []
        nested_by_repo: dict[str, dict] = {}
        for mod in published.get("modules", []):
            for item in mod.get("nested") or []:
                nested_by_repo[f"{mod['module']}/{item['path']}"] = item
        for name, d in sorted(derived.items()):
            got = nested_by_repo.get(name)
            if not got:
                problems.append(f"已发布 versions.json 缺嵌套模块 {name} 的条目")
                continue
            if d.get("latest") is None:
                problems.append(f"{name}: 嵌套模块版本 **未能核验**")
                continue
            if got.get("latest") != d["latest"]:
                problems.append(f"{name}: 已发布版本 {got.get('latest')} ≠ 实际最新 {d['latest']}")
            if d.get("hostUnverifiable"):
                problems.append(f"{name}: hostVersion **未能核验**")
                continue
            if hole != "host_none" and d.get("hostVersion") is None:
                problems.append(f"{name}: hostVersion **未能核验**")
                continue
            if d.get("hostVersion") and got.get("hostVersion") != d["hostVersion"]:
                problems.append(f"{name}: 已发布 hostVersion {got.get('hostVersion')!r} "
                                f"≠ 推导值 {d['hostVersion']!r}（对应父仓版本漂移）")
        if hole != "reverse":
            declared = {f"{m['repo']}/{m['path']}" for m in vru.NESTED_MODULES}
            for name in sorted(nested_by_repo):
                if name in derived:
                    continue
                if name in declared:
                    problems.append(f"{name}: **未能核验**（推导不出）")
                else:
                    problems.append(f"{name}: **未能核验**（矩阵未登记）")
        if repos_dir is not None:
            for name, item in sorted(nested_by_repo.items()):
                repo, path = name.split("/", 1)
                if not (repos_dir / repo / path).is_dir():
                    problems.append(f"已发布 versions.json 登记了嵌套模块 {name}，"
                                    f"但源码树里该目录已不存在（已撤回？）")
        return problems

    return mutated


def _mutate_oracle_C_no_reverse_diff():
    """变异体 M2：oracle C 退回旧写法——只做正向差集。"""
    orig = vru.check_nested_version_correspondence
    vru.check_nested_version_correspondence = _mutant_oracle_C("reverse")
    return [("check_nested_version_correspondence", orig)]


def _mutate_oracle_C_skip_host_none():
    """变异体 M3：oracle C 退回旧写法——hostVersion 推导不出就整条静默跳过。"""
    orig = vru.check_nested_version_correspondence
    vru.check_nested_version_correspondence = _mutant_oracle_C("host_none")
    return [("check_nested_version_correspondence", orig)]


MUTATIONS = [
    # ⚠️ 期望集合必须**按该缺陷的实际影响面列全**（#223 的教训）：
    #   「删掉一个判据」会同时打红**几个断言面**——它们是同一缺陷的多个面，
    #   **必然同时转红**。⇒ 期望集合写成 {D1a} 会被判「不精确」。
    ("门禁侧退回旧语义：远端读不到 ⇒ 当成候选集完整（不标未能核验）",
     _mutate_remote_unreadable_treated_as_complete, {"D1a", "D1b", "D2"}),
    ("oracle C 退回旧写法：只做正向差集（产物里有而推导里没有 ⇒ 静默放过）",
     _mutate_oracle_C_no_reverse_diff, {"D3", "D4"}),
    ("oracle C 退回旧写法：hostVersion 推导不出 ⇒ 整条静默跳过",
     _mutate_oracle_C_skip_host_none, {"D5"}),
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
        print("✗ 发版单元门禁自测未过：")
        for f in _FAILURES:
            print("  -", f)
        raise SystemExit(1)
    root, nested, bom, total = release_unit_count()
    print(f"✓ 发版单元门禁自测通过：删掉伞仓/嵌套/根模块的安装信息都抓得到；"
          f"口径 {total} 个发版单元（{root} 根 + {nested} 嵌套 + {bom} 伞仓）；"
          f"远端读不到 / 产物里有而推导里没有 / hostVersion 推导不出 ⇒ 三处都判红"
          f"（「判不了 ≠ 通过」）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
