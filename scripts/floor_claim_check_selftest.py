#!/usr/bin/env python3
"""地板类 CHANGELOG 断言核验器的自测（**离线**：不依赖工作区、不联网）。

为什么要有这个自测
----------------
`scripts/floor_claim_check.py` 是「**可证伪**断言」的核验器：它把
「`core/lifecycle` 逐字节未变」这类话**落到 git 对象层**去核（两版本的文件
清单 + blob sha 全一致才算成立）。
⇒ 判据本身必须**先自证**：否则「0 告警」只是「没抓到」，不是「没有」。

**离线是硬要求**
--------------
护栏要能挂进 PR 的 CI ⇒ 自测**不许**依赖工作区里那些仓的 clone / tag / Release
（CI 只 checkout 本仓 ⇒ 默认浅克隆拿不到别仓 tag）⇒ 核验层用**自造的 fixture git 仓**
（`git init` + commit + tag，与 `gen_matrix_go_selftest.py` 同一套路）⇒
**不联网、不读工作区、结果确定**。

覆盖面（每条都必须**精确**转红/转绿；「0 告警」不是证据，「变异能转红」才是）
----------------------------------------------------------------------
核验层（对 fixture 仓）：
  C0  真实成立对（v0.6.0→v0.9.2 的 lifecycle）⇒ 期望 IDENTICAL
  M1  张冠李戴：同版本区间、换成有差异的包（config）⇒ 期望 DIFFERS
  M2  断言搬家：新区间（v0.9.2→v0.12.1）的 lifecycle ⇒ 期望 DIFFERS
  M3  读不到当成立：目标 tag 不存在 ⇒ 期望 UNVERIFIABLE（**不是** IDENTICAL）
  M4  包路径不存在 ⇒ 期望 UNVERIFIABLE
  M13 ★ **同一串字、在两个版本节 ⇒ 真伪必须不同**（真伪绑「文本自己声明的区间」）
  M13b ★ 同一串字 ⇒ 抽出的区间必须各自不同
  M13c ★ **端到端** scan()：同一串字两节 ⇒ 提取 2 条、一成立一为假
覆盖面（★ 「读不到 ≠ 干净」「扫了 N 个 ⇒ N 有基准」）：
  M14a ★ 嵌套面读不到 origin/main ⇒ nested_changelogs 必须**如实带出**错误
  M14  ★ 嵌套面读不到 ⇒ scan() 必须把它记进 nested_unreadable（不许沉默）
  M15  ★ 期望集合 = 矩阵登记表 ⇒ 没检出 / 没有 CHANGELOG.md 的仓必须被点名
  M15b ★ 报告文本里必须出现「未检出 / 未扫描」（不许沉默）
提取层（纯函数）：
  M5  断言词缺失 ⇒ 提取 0 条（不得静默判绿）
  M6  措辞变体（「字节级未变」）⇒ 提取 1 条
  M7  中文隔断形态（「core 地板 v0.6.0 → v0.9.2」）⇒ 提取 1 条
  M8  版本号必须带 v 前缀（否则扫描路径会全判 UNVERIFIABLE）
  M9  ★ **否定语境**：断言词出现在「被否定的引用」里 ⇒ 提取 0 条
      （logx v0.8.1 更正后的 bullet **原文引用了旧假句**并声明其不成立 ⇒
       不过滤的话，护栏上线第一件事就是把「刚更正对的那条」判成假 ⇒ 自己打自己）
  M10 断言词只在**续行**里 ⇒ 提取 1 条（否则假干净）
  M11 别的断言类（「行为不变」+ 两个版本号）⇒ 本工具口径提取 0 条，
      但宽口径**要**抓到（⇒ 单列人工判读，不静默漏掉）

用法：
    python3 scripts/floor_claim_check_selftest.py            # 离线（CI 跑这个）
    python3 scripts/floor_claim_check_selftest.py --live     # 加跑工作区依赖的用例
    python3 scripts/floor_claim_check_selftest.py --mutate   # 变异自证（注入变异体
                                                              #   ⇒ 比对「精确转红集合」：
                                                              #   该红的红、不该红的还绿）
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import floor_claim_check as fcc  # noqa: E402


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{p.stderr.strip()[:200]}")
    return p.stdout.strip()


def build_fixture(root: Path) -> Path:
    """造一个 fixture 依赖仓，让三种判定都能复现。

    - `lifecycle/`：v0.6.0 与 v0.9.2 **逐字节相同**，v0.12.1 **变了** ⇒
      覆盖「成立对」与「断言搬家（真假断言）」两种形态；
    - `config/`：v0.6.0 与 v0.9.2 **不同** ⇒ 覆盖「张冠李戴」；
    - 没有 `v9.9.9` 这个 tag、没有 `no_such_pkg_zzz` 这个包路径 ⇒
      覆盖「读不到 ⇒ 不许当成立」。
    """
    d = root / "fixture-dep"
    d.mkdir(parents=True, exist_ok=True)
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "selftest@example.invalid")
    git(d, "config", "user.name", "selftest")

    def write(rel: str, text: str) -> None:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(msg: str, tag: str) -> None:
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", msg)
        git(d, "tag", tag)

    write("lifecycle/a.txt", "same\n")           # v0.6.0
    write("config/b.txt", "cfg-old\n")
    commit("v0.6.0", "v0.6.0")

    write("config/b.txt", "cfg-new\n")           # v0.9.2：lifecycle 不变，config 变
    commit("v0.9.2", "v0.9.2")

    write("lifecycle/a.txt", "same\n+5 lines\n")  # v0.12.1：lifecycle 变了
    commit("v0.12.1", "v0.12.1")
    return d


# ── 用例级结果收集（供 --mutate 变异自证比对「精确转红集合」）──────────
#   每个用例把自己的 ID 记在这里 ⇒ 变异体注入后跑一遍 ⇒ 比对哪些用例转红。
_RESULTS: dict[str, bool] = {}

# ── 输出口径：`--mutate` 模式下的「红」是**预期红**，不得让人读成自测失败 ──
#   ★ 背景（matrix-tools#205）：`--mutate` 会在每个变异体注入后跑一遍用例集，
#     那些 ❌ 是**故意打红**的（用例**应当**转红 ⇒ 正是变异被抓住的证据）；
#     但汇总行原写「自测结果：❌ 有用例未通过」⇒ **红 ≠ 失败**这条口径
#     **没有自带在输出里** ⇒ 人读 CI 日志会误判成「自测挂了」，
#     而 CI 判的其实是**退出码**（0 ⇒ 绿）。
#   ⇒ 修法：`--mutate` 下用 🔴（预期红）替代 ❌，并把汇总行改成自解释文案。
#   ⇒ **退出码语义不变**（CI 判退出码，不判符号）。
_MUTATE_MODE = False


def _mark(good: bool) -> str:
    """用例结果标记。`--mutate` 下的失败是**预期红** ⇒ 用 🔴 与真失败区分。"""
    if good:
        return "✅"
    return "🔴" if _MUTATE_MODE else "❌"


def _record(cid: str, good: bool) -> bool:
    _RESULTS[cid] = good
    return good


def run(live: bool = False) -> int:
    ok = True
    print("== 核验层变体（fixture 仓，离线）==")
    with tempfile.TemporaryDirectory() as td:
        dep = build_fixture(Path(td))
        cases = [
            ("C0", "v0.6.0", "v0.9.2", "lifecycle", "IDENTICAL", "真实成立对"),
            ("M1", "v0.6.0", "v0.9.2", "config", "DIFFERS", "张冠李戴（同区间换包）"),
            ("M2", "v0.9.2", "v0.12.1", "lifecycle", "DIFFERS", "断言搬家（新区间）"),
            ("M3", "v0.9.2", "v9.9.9", "lifecycle", "UNVERIFIABLE", "tag 不存在 ⇒ 读不到≠成立"),
            ("M4", "v0.9.2", "v0.12.1", "no_such_pkg_zzz", "UNVERIFIABLE", "包路径不存在"),
        ]
        for name, old, new, pkg, want, why in cases:
            verdict, detail = fcc.verify_claim(dep, old, new, pkg)
            good = verdict == want
            ok &= _record(name, good)
            print(f"  {name} {_mark(good)} want={want:13s} got={verdict:13s} | {why}")
            if not good:
                print(f"       {detail[:160]}")

        # M13 ★ **同一串字、在两个版本节 ⇒ 真伪必须不同**（真伪绑「文本自己声明的区间」）
        #   真实语料原型：logx 的「logx 唯一使用的 core/lifecycle 逐字节未变，零行为风险」
        #   —— 同一串字，在 v0.7.1 节（区间 core v0.6.0→v0.9.2）**成立**，
        #     被照搬到 v0.8.1 节（区间 core v0.9.2→v0.12.1）⇒ **为假**。
        #   ⇒ 若核验器只认「字符串」不认「区间」，同一句会被当成同一条断言 ⇒ 误判
        #     （真话被当成假话，或假话被当成真话）。
        #   区间取自 fixture：lifecycle 在 v0.6.0→v0.9.2 **相同**、
        #   在 v0.9.2→v0.12.1 **不同** ⇒ 同一串字 ⇒ 一成立一为假。
        _same_text = "logx 唯一使用的 core/lifecycle 逐字节未变，零行为风险"
        v_hold, d_hold = fcc.verify_claim(dep, "v0.6.0", "v0.9.2", "lifecycle")
        v_fake, d_fake = fcc.verify_claim(dep, "v0.9.2", "v0.12.1", "lifecycle")
        good = (v_hold == "IDENTICAL" and v_fake == "DIFFERS")
        ok &= _record("M13", good)
        print(f"  M13 {_mark(good)} ★同一串字两节 ⇒ 旧区间={v_hold}"
              f"（期望 IDENTICAL） / 新区间={v_fake}（期望 DIFFERS）")
        if not good:
            print(f"       hold: {d_hold[:150]}")
            print(f"       fake: {d_fake[:150]}")
        # 同一串字 ⇒ **提取**出来的区间必须各自不同（否则核验层拿到的区间是错的）
        _c_old = fcc.extract_claims(f"- build: core v0.6.0 → v0.9.2——{_same_text}")
        _c_new = fcc.extract_claims(f"- build: core v0.9.2 → v0.12.1——{_same_text}")
        good2 = (len(_c_old) == 1 and len(_c_new) == 1
                 and _c_old[0]["old"] == "v0.6.0" and _c_new[0]["old"] == "v0.9.2")
        ok &= _record("M13b", good2)
        print(f"  M13b{_mark(good2)} 同一串字抽出**各自**的区间"
              f"⇒ old={_c_old[0]['old'] if _c_old else None}"
              f" / new={_c_new[0]['old'] if _c_new else None}")
        if not good2:
            print(f"       extracted old={_c_old} new={_c_new}")

    print("== 提取层变体（纯函数）==")
    # M5 断言词缺失 ⇒ 提取不出
    c = fcc.extract_claims("- build: core v0.9.2 → v0.12.1——抬地板带动修复传递")
    good = len(c) == 0
    ok &= _record("M5", good)
    print(f"  M5  {_mark(good)} 断言词缺失 ⇒ 提取 {len(c)} 条（期望 0，不得静默判绿）")

    # M6 措辞变体
    c = fcc.extract_claims("- build: core v0.9.2 → v0.12.1——core/lifecycle 字节级未变")
    good = len(c) == 1
    ok &= _record("M6", good)
    print(f"  M6  {_mark(good)} 措辞变体「字节级未变」⇒ 提取 {len(c)} 条（期望 1）")

    # M7 中文隔断形态
    c = fcc.extract_claims("- build: core 地板 v0.6.0 → v0.9.2——logx 唯一使用的 "
                           "core/lifecycle 逐字节未变，零行为风险")
    good = (len(c) == 1 and c[0]["old"] == "v0.6.0" and c[0]["new"] == "v0.9.2")
    ok &= _record("M7", good)
    print(f"  M7  {_mark(good)} 中文隔断形态「core 地板 v0.6.0 → v0.9.2」"
          f"⇒ 提取 {len(c)} 条")

    # M8 版本号必须带 v 前缀
    c = fcc.extract_claims("- build: core v0.9.2 → v0.12.1——logx 唯一用到的 "
                           "core/lifecycle 逐字节未变")
    good = len(c) == 0 or c[0]["old"] == "v0.9.2"
    ok &= _record("M8", good)
    print(f"  M8  {_mark(good)} 版本号带 v 前缀（否则扫描路径会全判 UNVERIFIABLE）")

    # M9 ★ 否定语境：引用旧假句并声明其不成立 ⇒ 不是待核断言
    disclaimed = ("- build: 矩阵内依赖地板对齐（align_floors 不动点）：core v0.9.2 → v0.12.1"
                  "——logx 唯一用到的是 core/lifecycle；**本区间 lifecycle/manager.go 确有变更**"
                  "（`Listen` 补 `defer signal.Stop(ch)`）⇒ 经类型别名转发该修复**穿透到 logx 用户**。"
                  "**本条为更正**：初版此处写「core/lifecycle 逐字节未变、抬地板只带动修复传递」"
                  "**不成立**——该断言在 core v0.6.0 → v0.9.2 区间成立，被照搬到新区间却未复核")
    c = fcc.extract_claims(disclaimed)
    good = len(c) == 0
    ok &= _record("M9", good)
    print(f"  M9  {_mark(good)} ★否定语境（引用旧假句并声明其不成立）"
          f"⇒ 提取 {len(c)} 条（期望 0；否则护栏会把「刚更正对的那条」判成假）")

    # M9b 否定语境**不得**把整条 bullet 的真信息也吃掉：同一 bullet 里若另有一条
    #      **未被否定**的断言词，仍必须提取得到
    mixed = ("- build: core v0.9.2 → v0.12.1——core/lifecycle 逐字节未变"
             "；另：初版此处写「core/config 零行为风险」**不成立**")
    c = fcc.extract_claims(mixed)
    good = len(c) == 1
    ok &= _record("M9b", good)
    print(f"  M9b {_mark(good)} 同一 bullet 内**未被否定**的断言仍要提取"
          f"⇒ {len(c)} 条（期望 1）")

    # M10 断言词只在续行里
    cont = ("- build: 矩阵内依赖地板对齐：core v0.9.2 → v0.12.1——logx 唯一用到的\n"
            "  core/lifecycle 逐字节未变，抬地板只带动修复传递")
    c = fcc.extract_claims(cont)
    good = len(c) == 1 and c[0]["pkg"] == "lifecycle"
    ok &= _record("M10", good)
    print(f"  M10 {_mark(good)} 断言词只在**续行** ⇒ 提取 {len(c)} 条（期望 1）")

    # M11 别的断言类
    other_class = ("- refactor(validatorh): 底层校验迁移至 kitx/validator（pkg v0.10.0 拆分承接）"
                   "——import 机械替换，Bind*/Fail 400 错误映射行为不变；"
                   "deps: pkg v0.9.3 → v0.10.0、新增 kitx v0.1.0")
    good = fcc.broad_candidates(other_class)
    ok &= _record("M11", good)
    print(f"  M11 {_mark(good)} 别的断言类（「行为不变」+ 两个版本号）"
          f"应被宽口径抓到（单列人工判读）⇒ {fcc.broad_candidates(other_class)}")
    c = fcc.extract_claims(other_class)
    good = len(c) == 0
    ok &= _record("M11b", good)
    print(f"  M11b{_mark(good)} 但**不得**被工具口径误判为同类断言（提取 0 条）⇒ {len(c)} 条")

    # M12 scan() 的仓级聚合：必须按**仓**算（有断言的仓不能因为最后一个来源面没断言
    #     而被算成「无断言」）—— 复核班实测发现的假干净（旧实现读文件级计数）
    ok &= run_scan_aggregate()
    ok &= run_end_to_end_same_string()
    ok &= run_nested_unreadable()
    ok &= run_expected_set()

    if live:
        ok &= run_live()
    else:
        print("（--live：跳过工作区依赖的用例：嵌套覆盖 / 节边界 / Release 正文）")

    # ⚠️ 这里判的是「**所有用例是否通过**」，**不是**变异自证。
    #   变异自证（注入变异体 ⇒ 哪些用例精确转红）走 `--mutate` 模式。
    #   （措辞曾写成「变异自证：全部精确转红」⇒ 在 --mutate 运行时，
    #     被变异体故意打红的用例会把这行打成 ❌ ⇒ 误读成「框架失败」⇒ 已改。）
    if _MUTATE_MODE:
        # ★ 自解释：这些红是**预期红**（变异被抓住的证据），不是自测失败。
        print("\n变异注入中（以下 🔴 为**预期红**，不代表自测失败；"
              "CI 判的是退出码，不是符号）")
    else:
        print(f"\n自测结果：{'✅ 全部用例通过' if ok else '❌ 有用例未通过'}")
    return 0 if ok else 1


def run_scan_aggregate() -> bool:
    """scan() 的「N 个仓无此类断言」必须按**仓**算，不是按最后一个来源面算。

    复核班实测（旧实现按文件级计数、在仓级读最后一个来源面的值）：
    全 org 扫描会输出「**25 个仓无此类断言；提取断言 2 条**」——同屏自相矛盾，
    且与「为假 0」叠读成「所有仓都没这类断言」= **假干净**。
    """
    ok = True
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        # 一个**有**断言的仓 + 一个**没有**断言的仓
        a = ws / "repo-a"
        (a / ".git").mkdir(parents=True)
        (a / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [v0.1.0]\n\n"
            "- build: 矩阵内依赖地板对齐：core v0.6.0 → v0.9.2——core/lifecycle 逐字节未变\n",
            encoding="utf-8")
        b = ws / "repo-b"
        (b / ".git").mkdir(parents=True)
        (b / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [v0.1.0]\n\n- fix: 别的改动，与地板断言无关\n",
            encoding="utf-8")
        old_ws = fcc.WORKSPACE
        fcc.WORKSPACE = ws
        try:
            res = fcc.scan(include_releases=False)
        finally:
            fcc.WORKSPACE = old_ws
        good = (res["scanned_repos"] == 2
                and res["repos_without_claims"] == 1
                and res["total_claims"] == 1)
        ok &= _record("M12", good)
        print(f"  M12 {_mark(good)} 仓级聚合：2 个仓里**1 个**无此类断言"
              f"（实得 scanned_repos={res['scanned_repos']}、"
              f"repos_without_claims={res['repos_without_claims']}、"
              f"total_claims={res['total_claims']}）")
    return ok


def run_end_to_end_same_string() -> bool:
    """M13c：**端到端**跑 `scan()` —— 同一串字在两个版本节 ⇒ 一成立一为假。

    M13 只直接调 `verify_claim`（核验层两态）；M13c 走**真实扫描路径**：
    造一个含「同一串字两个版本节」的仓 ⇒ `scan()` 提取 2 条断言 ⇒
    判定必须是 **一成立一为假**（不是两条一样）。
    ⇒ 若 scan 路径上丢了「文本声明的区间」（只认字符串），这里会红。

    依赖仓名必须是 `core`（与 bullet 里的 dep 一致）⇒ fixture 造好后改名。
    """
    ok = True
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        dep = build_fixture(ws)
        (ws / "core").mkdir()
        for item in dep.iterdir():          # fixture-dep → core
            shutil.move(str(item), str(ws / "core" / item.name))
        repo = ws / "logx"
        (repo / ".git").mkdir(parents=True)
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## [v0.7.1] — 2026-10-19\n\n"
            "- build: core 地板 v0.6.0 → v0.9.2——logx 唯一使用的 "
            "core/lifecycle 逐字节未变，零行为风险\n\n"
            "## [v0.8.1] — 2026-11-03\n\n"
            "- build: 矩阵内依赖地板对齐：core v0.9.2 → v0.12.1——logx 唯一使用的 "
            "core/lifecycle 逐字节未变，零行为风险\n",
            encoding="utf-8")
        old_ws = fcc.WORKSPACE
        fcc.WORKSPACE = ws
        try:
            res = fcc.scan(include_releases=False)
        finally:
            fcc.WORKSPACE = old_ws
        verdicts = sorted(f["verdict"] for f in res["findings"])
        good = (len(res["findings"]) == 2
                and verdicts == ["DIFFERS", "IDENTICAL"]
                and res["repos_without_claims"] == 0
                and res["total_claims"] == 2)
        ok &= _record("M13c", good)
        print(f"  M13c{_mark(good)} ★端到端 scan()：同一串字两节 ⇒ "
              f"判定={verdicts} 提取={res['total_claims']} 条、"
              f"无断言仓={res['repos_without_claims']}（期望 2 条 / [DIFFERS, IDENTICAL] / 0）")
        for f in res["findings"]:
            print(f"        · {f['section']:<12} {f['old']} → {f['new']}  "
                  f"{f['pkg']:<12} {f['verdict']}")
    return ok


def run_nested_unreadable() -> bool:
    """M14：嵌套面**读不到**（没有 origin/main）必须被记进 nested_unreadable。

    ⚠️ 旧实现：`nested_changelogs` 在 `git ls-tree origin/main` 失败时 `return []`
       ⇒ 嵌套面覆盖率 0 **且不留任何记录**（scan() 的 unreadable 记账在
       「文件读取循环」里，列表为空 ⇒ 循环不执行 ⇒ **一条 unreadable 都没有**）
       ⇒ 报告照样干净（假干净）—— 正好把本工具「读不到 ≠ 干净」的纪律漏在门口。
    """
    ok = True
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        repo = ws / "repo-noref"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "selftest")
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [v0.1.0]\n\n- fix: 与地板断言无关\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m init")
        # 故意**不建 origin/main**（本地仓没有远端引用）⇒ ls-tree origin/main 必失败
        raw = fcc.nested_changelogs(repo)
        items, err = raw if isinstance(raw, tuple) else (list(raw), "")
        good = (items == [] and err != "")
        ok &= _record("M14a", good)
        print(f"  M14a{_mark(good)} 嵌套面读不到 origin/main ⇒ 必须**如实带出**"
              f"（清单 {len(items)} 份 / 错误 {err[:70]}）")
        old_ws = fcc.WORKSPACE
        fcc.WORKSPACE = ws
        try:
            res = fcc.scan(include_releases=False)
        finally:
            fcc.WORKSPACE = old_ws
        hit = [u for u in res["nested_unreadable"] if u.startswith("repo-noref")]
        good2 = len(hit) == 1
        ok &= _record("M14", good2)
        print(f"  M14 {_mark(good2)} scan() 必须把该仓记进 nested_unreadable"
              f"（读不到 ≠ 干净 ⇒ 单列）⇒ 实得 {res['nested_unreadable']}")
    return ok


def run_expected_set() -> bool:
    """M15：扫描范围必须以**矩阵登记表**为期望集合 ⇒ 没检出的仓必须被点名。

    ⚠️ 旧实现：`repo_dirs()` 只按**目录特征**扫（有 .git + 有 CHANGELOG.md）
       ⇒ 「扫描 N 个仓」这个 N 完全取决于工作区目录 ⇒ CI 里少 clone 一个仓
       ⇒ 那个仓的断言**一条都不查**，报告照样干净（假干净）。
    """
    ok = True
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        # 只检出登记表里的一个仓（pkg 在 matrix.MODULES 里）
        pkg = ws / "pkg"
        (pkg / ".git").mkdir(parents=True)
        (pkg / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [v0.1.0]\n\n- fix: 与地板断言无关\n", encoding="utf-8")
        # 另一个登记仓：**检出了但没有 CHANGELOG.md** ⇒ 同样必须被点名
        (ws / "logx" / ".git").mkdir(parents=True)
        old_ws = fcc.WORKSPACE
        fcc.WORKSPACE = ws
        try:
            res = fcc.scan(include_releases=False)
        finally:
            fcc.WORKSPACE = old_ws
        unscanned = {u["repo"]: u["reason"] for u in res["unscanned_repos"]}
        good = ("pkg" not in unscanned
                and "logx" in unscanned
                and "CHANGELOG" in unscanned.get("logx", "")
                and "pkg" in res["expected_repos"]
                and len(res["expected_repos"]) >= len(fcc.MODULES))
        ok &= _record("M15", good)
        print(f"  M15 {_mark(good)} ★期望集合=矩阵登记表（{len(res['expected_repos'])} 个）："
              f"pkg 已扫（不该进未扫描清单）、logx 检出但无 CHANGELOG.md ⇒ 必须被点名"
              f"（实得 logx={unscanned.get('logx')}、未扫描 {len(unscanned)} 个）")
        # 报告**文本**里必须出现「未检出 / 未扫描」⇒ 不许沉默
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["floor_claim_check.py", "--no-releases"]
        fcc.WORKSPACE = ws
        try:
            with contextlib.redirect_stdout(buf):
                fcc.main()
        finally:
            sys.argv = old_argv
            fcc.WORKSPACE = old_ws
        out = buf.getvalue()
        named = ("未检出" in out) or ("未扫描" in out)
        good2 = named and ("logx" in out)
        ok &= _record("M15b", good2)
        print(f"  M15b{_mark(good2)} 报告文本必须点名未扫描的仓（不许沉默）"
              f"⇒ 含「未检出/未扫描」={named}、含 logx={'logx' in out}")
    return ok


def run_live() -> bool:
    """工作区依赖的用例（本地跑，不进 CI）：覆盖面 / 节边界 / Release 正文。"""
    ok = True
    print("== 覆盖面变体（依赖工作区，--live）==")
    nested_all = []
    for rd in fcc.repo_dirs():
        items, _err = fcc.nested_changelogs(rd)
        for rel, _ in items:
            nested_all.append((rd.name, rel))
    good = len(nested_all) == 7
    ok &= _record("L1", good)
    print(f"  L1 {_mark(good)} 嵌套模块 CHANGELOG 应被枚举到 7 份，"
          f"实得 {len(nested_all)} 份：{[f'{r}/{p}' for r, p in nested_all]}")

    # ★ 已更正的那条**不得**再被判成假断言（否定语境过滤的端到端证据）
    try:
        rels = fcc.release_bodies("logx")
        v081 = dict(rels).get("v0.8.1", "")
        c = fcc.extract_claims(v081)
        good = len(c) == 0
        ok &= _record("L2", good)
        print(f"  L2 {_mark(good)} logx v0.8.1 的 **Release 正文**（已更正）"
              f"⇒ 提取 {len(c)} 条断言（期望 0：它声明旧句不成立，不再是待核断言）")
    except Exception as e:  # noqa: BLE001 - 网络不可用时只提示，不掩盖其它用例
        print(f"  L2 ⚠ 取 Release 正文失败（不影响其它用例）：{str(e)[:120]}")
    return ok



# ── 变异自证（--mutate）─────────────────────────────────────────────────
#   纪律：「0 告警」不是证据，「变异能转红」才是。
#   每个变异体**真的改掉被检行为**，并写死它**应该**让哪些用例转红
#   ⇒ 跑完逐条核对（转红必须**精确**：不该红的用例必须还绿着）。
_ORIG_EXTRACT = fcc.extract_claims


def _mutate_extract_ignores_old_range():
    """M13 靶子：提取层**丢掉文本声明的 old 区间** ⇒ 同一串字两节抽出同一个区间
    ⇒ 「真伪绑区间」这条判据就废了（真话会被当成假话，或反过来）。"""
    def patched(bullet_text: str):
        out = _ORIG_EXTRACT(bullet_text)
        for c in out:
            c["old"] = "v9.9.9"   # ★ 无论文本声明什么区间，一律当成这个区间起比
        return out
    return patched


def _mutate_verify_ignores_range():
    """核验层**忽略文本声明的区间** ⇒ 永远拿同一个 tag 比 ⇒ 同一串字两节判成一样。"""
    orig = fcc.verify_claim

    def patched(dep_dir, old, new, pkgpath):
        return orig(dep_dir, "v0.6.0", "v0.6.0", pkgpath)
    return patched


def _mutate_verify_always_identical():
    """核验层**永远判成立** ⇒ 同一串字两节都判 IDENTICAL ⇒ 端到端路径必须转红。"""
    def patched(dep_dir, old, new, pkgpath):
        return "IDENTICAL", "变异：假装永远成立"
    return patched


def _mutate_nested_swallows_error():
    """M14 靶子：**吞掉** origin/main 的读不到（= 旧实现的 `if rc != 0: return []`）。

    ⇒ 嵌套面覆盖率 0 **且不留任何记录** ⇒ M14a / M14 必须转红。
    """
    orig = fcc.nested_changelogs

    def patched(repo_dir):
        items, _err = orig(repo_dir)
        return items, ""      # ★ 错误被吞掉 ⇒ 报告里不再出现「读不到」
    return patched


def _mutate_expected_set_ignores_registry():
    """M15 靶子：**期望集合**退化成空（= 旧实现：只按目录特征扫，没有登记表）。

    ⇒ 「未检出的仓」无从点名 ⇒ M15 / M15b 必须转红。
    """
    def patched():
        return []
    return patched


MUTATIONS = [
    # ⚠️ **预期转红集合必须按「该变异真的破坏了谁」写死**，不能只写自己那条：
    #   多红是**正确行为**（变异确实同时破坏了那些用例）⇒ 漏写反而会被框架判成
    #   「转红不精确」⇒ 逼人把集合写对。
    # ① 提取层丢掉 old 区间 ⇒ M7 / M8（断言 old 区间）+ M13b（抽各自区间）
    #    + M13c（端到端 scan() 也走提取层）⇒ 全红。
    ("提取层丢掉文本声明的 old 区间（同一串字两节变成同一条）",
     "extract_claims", _mutate_extract_ignores_old_range, {"M7", "M8", "M13b", "M13c"}),
    # ② 核验层忽略区间（永远拿同一个 tag 比）⇒ 所有**走 verify_claim** 的用例都受影响：
    #    C0 仍绿（它本就要 IDENTICAL）⇒ M1 / M2 / M3（判定翻转）+ M13 / M13c。
    ("核验层忽略文本声明的区间（永远拿同一个 tag 比）",
     "verify_claim", _mutate_verify_ignores_range, {"M1", "M2", "M3", "M13", "M13c"}),
    # ③ 核验层永远判成立 ⇒ 凡期望非 IDENTICAL 的都翻转 ⇒ M1 / M2 / M3 / M4
    #    （M4 期望 UNVERIFIABLE）+ M13 / M13c；C0 仍绿（本就期望 IDENTICAL）。
    ("核验层永远判成立（同一串字两节都判真）",
     "verify_claim", _mutate_verify_always_identical, {"M1", "M2", "M3", "M4", "M13", "M13c"}),
    # ④ ★ 嵌套面读不到被吞掉（旧实现）⇒ 嵌套面覆盖率 0 且无任何记录 ⇒ M14a / M14。
    ("嵌套面读不到 origin/main 被吞掉（旧实现：静默返回空清单）",
     "nested_changelogs", _mutate_nested_swallows_error, {"M14a", "M14"}),
    # ⑤ ★ 期望集合退化成空（旧实现：没有矩阵登记表）⇒ 未检出的仓无从点名
    #    ⇒ M15 / M15b。
    ("期望集合退化成空（旧实现：没有矩阵登记表，扫描范围=工作区目录）",
     "expected_repo_names", _mutate_expected_set_ignores_registry, {"M15", "M15b"}),
]


def run_mutations() -> int:
    """跑变异自证：注入每个变异体 ⇒ 比对「精确转红集合」。"""
    ok = True
    for i, (name, attr, mutate, expect) in enumerate(MUTATIONS):
        _RESULTS.clear()
        saved = getattr(fcc, attr)
        try:
            setattr(fcc, attr, mutate())
            run(live=False)
        finally:
            setattr(fcc, attr, saved)
        got = {cid for cid, good in _RESULTS.items() if not good}
        if not got:
            print(f"✗ 变异「{name}」没有被抓住——这份断言没有牙齿")
            ok = False
            continue
        if got != expect:
            print(f"✗ 变异「{name}」转红得**不精确**：")
            print(f"    预期转红：{sorted(expect)}")
            print(f"    实得转红：{sorted(got)}")
            print(f"    多红：{sorted(got - expect)}／漏红：{sorted(expect - got)}")
            ok = False
            continue
        print(f"✓ 变异「{name}」被抓住（精确转红 {sorted(got)}）")
    if ok:
        print(f"✓ 变异自证 {len(MUTATIONS)}/{len(MUTATIONS)} 全部**精确**转红")
    return 0 if ok else 1


def main() -> int:
    global _MUTATE_MODE
    if "--mutate" in sys.argv:
        _MUTATE_MODE = True   # ★ 输出用 🔴 标「预期红」⇒ 别被误读成自测失败
        return run_mutations()
    live = "--live" in sys.argv
    return run(live=live)


if __name__ == "__main__":
    sys.exit(main())
