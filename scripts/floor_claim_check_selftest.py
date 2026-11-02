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
（CI 只 checkout 本仓，且 `fetch-tags: false`）⇒ 核验层用**自造的 fixture git 仓**
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
"""
from __future__ import annotations

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
            ok &= good
            print(f"  {name} {'✅' if good else '❌'} want={want:13s} got={verdict:13s} | {why}")
            if not good:
                print(f"       {detail[:160]}")

    print("== 提取层变体（纯函数）==")
    # M5 断言词缺失 ⇒ 提取不出
    c = fcc.extract_claims("- build: core v0.9.2 → v0.12.1——抬地板带动修复传递")
    good = len(c) == 0
    ok &= good
    print(f"  M5  {'✅' if good else '❌'} 断言词缺失 ⇒ 提取 {len(c)} 条（期望 0，不得静默判绿）")

    # M6 措辞变体
    c = fcc.extract_claims("- build: core v0.9.2 → v0.12.1——core/lifecycle 字节级未变")
    good = len(c) == 1
    ok &= good
    print(f"  M6  {'✅' if good else '❌'} 措辞变体「字节级未变」⇒ 提取 {len(c)} 条（期望 1）")

    # M7 中文隔断形态
    c = fcc.extract_claims("- build: core 地板 v0.6.0 → v0.9.2——logx 唯一使用的 "
                           "core/lifecycle 逐字节未变，零行为风险")
    good = (len(c) == 1 and c[0]["old"] == "v0.6.0" and c[0]["new"] == "v0.9.2")
    ok &= good
    print(f"  M7  {'✅' if good else '❌'} 中文隔断形态「core 地板 v0.6.0 → v0.9.2」"
          f"⇒ 提取 {len(c)} 条")

    # M8 版本号必须带 v 前缀
    c = fcc.extract_claims("- build: core v0.9.2 → v0.12.1——logx 唯一用到的 "
                           "core/lifecycle 逐字节未变")
    good = len(c) == 0 or c[0]["old"] == "v0.9.2"
    ok &= good
    print(f"  M8  {'✅' if good else '❌'} 版本号带 v 前缀（否则扫描路径会全判 UNVERIFIABLE）")

    # M9 ★ 否定语境：引用旧假句并声明其不成立 ⇒ 不是待核断言
    disclaimed = ("- build: 矩阵内依赖地板对齐（align_floors 不动点）：core v0.9.2 → v0.12.1"
                  "——logx 唯一用到的是 core/lifecycle；**本区间 lifecycle/manager.go 确有变更**"
                  "（`Listen` 补 `defer signal.Stop(ch)`）⇒ 经类型别名转发该修复**穿透到 logx 用户**。"
                  "**本条为更正**：初版此处写「core/lifecycle 逐字节未变、抬地板只带动修复传递」"
                  "**不成立**——该断言在 core v0.6.0 → v0.9.2 区间成立，被照搬到新区间却未复核")
    c = fcc.extract_claims(disclaimed)
    good = len(c) == 0
    ok &= good
    print(f"  M9  {'✅' if good else '❌'} ★否定语境（引用旧假句并声明其不成立）"
          f"⇒ 提取 {len(c)} 条（期望 0；否则护栏会把「刚更正对的那条」判成假）")

    # M9b 否定语境**不得**把整条 bullet 的真信息也吃掉：同一 bullet 里若另有一条
    #      **未被否定**的断言词，仍必须提取得到
    mixed = ("- build: core v0.9.2 → v0.12.1——core/lifecycle 逐字节未变"
             "；另：初版此处写「core/config 零行为风险」**不成立**")
    c = fcc.extract_claims(mixed)
    good = len(c) == 1
    ok &= good
    print(f"  M9b {'✅' if good else '❌'} 同一 bullet 内**未被否定**的断言仍要提取"
          f"⇒ {len(c)} 条（期望 1）")

    # M10 断言词只在续行里
    cont = ("- build: 矩阵内依赖地板对齐：core v0.9.2 → v0.12.1——logx 唯一用到的\n"
            "  core/lifecycle 逐字节未变，抬地板只带动修复传递")
    c = fcc.extract_claims(cont)
    good = len(c) == 1 and c[0]["pkg"] == "lifecycle"
    ok &= good
    print(f"  M10 {'✅' if good else '❌'} 断言词只在**续行** ⇒ 提取 {len(c)} 条（期望 1）")

    # M11 别的断言类
    other_class = ("- refactor(validatorh): 底层校验迁移至 kitx/validator（pkg v0.10.0 拆分承接）"
                   "——import 机械替换，Bind*/Fail 400 错误映射行为不变；"
                   "deps: pkg v0.9.3 → v0.10.0、新增 kitx v0.1.0")
    good = fcc.broad_candidates(other_class)
    ok &= good
    print(f"  M11 {'✅' if good else '❌'} 别的断言类（「行为不变」+ 两个版本号）"
          f"应被宽口径抓到（单列人工判读）⇒ {fcc.broad_candidates(other_class)}")
    c = fcc.extract_claims(other_class)
    good = len(c) == 0
    ok &= good
    print(f"  M11b{'✅' if good else '❌'} 但**不得**被工具口径误判为同类断言（提取 0 条）⇒ {len(c)} 条")

    if live:
        ok &= run_live()
    else:
        print("（--live：跳过工作区依赖的用例：嵌套覆盖 / 节边界 / Release 正文）")

    print(f"\n变异自证：{'✅ 全部精确转红' if ok else '❌ 有变体未按预期转红'}")
    return 0 if ok else 1


def run_live() -> bool:
    """工作区依赖的用例（本地跑，不进 CI）：覆盖面 / 节边界 / Release 正文。"""
    ok = True
    print("== 覆盖面变体（依赖工作区，--live）==")
    nested_all = []
    for rd in fcc.repo_dirs():
        for rel, _ in fcc.nested_changelogs(rd):
            nested_all.append((rd.name, rel))
    good = len(nested_all) == 7
    ok &= good
    print(f"  L1 {'✅' if good else '❌'} 嵌套模块 CHANGELOG 应被枚举到 7 份，"
          f"实得 {len(nested_all)} 份：{[f'{r}/{p}' for r, p in nested_all]}")

    # ★ 已更正的那条**不得**再被判成假断言（否定语境过滤的端到端证据）
    try:
        rels = fcc.release_bodies("logx")
        v081 = dict(rels).get("v0.8.1", "")
        c = fcc.extract_claims(v081)
        good = len(c) == 0
        ok &= good
        print(f"  L2 {'✅' if good else '❌'} logx v0.8.1 的 **Release 正文**（已更正）"
              f"⇒ 提取 {len(c)} 条断言（期望 0：它声明旧句不成立，不再是待核断言）")
    except Exception as e:  # noqa: BLE001 - 网络不可用时只提示，不掩盖其它用例
        print(f"  L2 ⚠ 取 Release 正文失败（不影响其它用例）：{str(e)[:120]}")
    return ok


def main() -> int:
    live = "--live" in sys.argv
    return run(live=live)


if __name__ == "__main__":
    sys.exit(main())
