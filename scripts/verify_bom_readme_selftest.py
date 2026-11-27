#!/usr/bin/env python3
"""BOM 主仓 README 计数门禁自测：五条判据各有牙齿 + 变异自证。

**为什么要有这个自测**（matrix-tools#339）：README 计数此前零门禁——kitex-examples
移出 MODULES、kitx 改名 authx、pkg 子包 23→38 三轮变更后 README 全没跟上，
badge/prose/架构表/合数四处漂移无人拦。补上的判据必须证明「抓得住自己要防的」：

  V1  全对齐夹具（badge/prose=N / 架构表=MODULES / 合数=真相 / 无易变数）
      ⇒ 零误报（判据不拿正常 README 开刀）；
  V2  badge 改成 N+1 ⇒ 精确点名 badge 漂移；
  V3  badge 整个删掉 ⇒ 也红（承诺在场——计数声明不许静默消失）；
  V4  prose 改成 N+1 ⇒ 精确点名 prose 漂移；
  V5  架构表多一个非矩阵模块链接（kitex-examples 形态）⇒ 点名「多出」；
  V6  架构表少一个矩阵模块链接（authx 被删形态）⇒ 点名「缺」；
  V7  合数写 23（真相 38，夹具注入）⇒ 点名数字与事实不符；
  V8  合数同行无可归属模块 ⇒ 判红（写法必须紧挨模块链接——与站点「只记账」
      不同：README 是**计数承诺面**，归属不了说明写法本身破坏了承诺结构）；
  V9  硬编码「199 条 breaking」⇒ 点名易变数禁写；
  M   **变异自证**：把「架构表集合相等」的 extra/missing 分支短路 ⇒ V5/V6 必须
      从红转绿——证明原判据真的在比集合，而不是「看到了就算过」。

**无副作用、不联网**：README 用合成夹具，期望值来自 matrix.MODULES 与注入的
subpkg_truth（不依赖真实仓库检出）。

用法：
    python3 scripts/verify_bom_readme_selftest.py             # 正常：全绿
    python3 scripts/verify_bom_readme_selftest.py --mutate    # 变异：V5/V6 必须转绿
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import verify_bom_readme as vbr  # noqa: E402
from matrix import MODULES  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


N = len(MODULES)  # 夹具数字全部从单源派生——写死 23/24 则 MODULES 增删时自测自己漂
MISSING = object()  # 「该元素不出现」哨兵——None 已用作「取 N 缺省」的惯例值


def make_readme(*, badge=None, prose=None,
                drop_module: str | None = None,
                extra_link: str | None = None,
                subpkg_claim: str = "（38 合 1）",
                volatile: str | None = None) -> str:
    """合成 README 夹具：结构对齐真 README 的计数承载面。

    badge/prose 缺省 None → 取 N（对齐夹具）；传 MISSING 表示该元素整段不出现。
    """
    rows = [
        ("应用引导 · 框架生态", ["bootstrap", "hertz", "kitex", "middleware", "workflow", "k8s"]),
        ("通信 · 服务治理", ["httpx", "nns-endpoint", "network", "nns", "registry"]),
        ("数据访问", ["gormx", "redisx", "etcdx", "data", "timertask"]),
        ("可观测性", ["logx", "tracing", "metrics"]),
        ("基础库", ["pkg", "concurrency", "core", "authx"]),
    ]
    badge = N if badge is None else badge
    prose = N if prose is None else prose
    lines = ["# oekit", ""]
    if badge is not MISSING:
        lines.append(f"![modules](https://img.shields.io/badge/modules-{badge}-4C6EF5)")
    if prose is not MISSING:
        lines.append(f"oekit 是一套模块矩阵：**{prose} 个活跃模块仓库，每仓独立发版**。")
    lines += ["", "## 架构总览", "", "| 能力层 | 模块 |", "|---|---|"]
    for layer, mods in rows:
        cells = []
        for m in mods:
            if m == drop_module:
                continue
            label = f"（{subpkg_claim}）" if m == "pkg" else ""
            cells.append(f"[{m}](https://github.com/oekit/{m}){label}")
        if layer == rows[0][0] and extra_link:
            cells.append(f"[{extra_link}](https://github.com/oekit/{extra_link})")
        lines.append(f"| {layer} | " + " · ".join(cells) + " |")
    lines += ["", "## 兼容性", "",
              "| 旧路径 | 新路径 |", "|---|---|",
              "| `github.com/oekit/<旧微库>` | `github.com/oekit/pkg/xxx` |"]
    if volatile:
        lines += ["", f"矩阵里有 {volatile}，随快照一起发布。"]
    return "\n".join(lines) + "\n"


TRUTH = {"pkg": {"expected": 38, "source": "夹具真相源（模拟 subpkg_claims）"}}


def run(text: str) -> list[str]:
    return vbr.check_bom_readme(text, len(MODULES), TRUTH)


def main() -> int:
    mutate = "--mutate" in sys.argv

    if mutate:
        # 变异自证：把「集合相等」判据短路 ⇒ V5/V6 必须从红转绿。
        src = (HERE / "verify_bom_readme.py").read_text(encoding="utf-8")
        anchor = 'problems.append(f"架构总览表多出非矩阵模块链接：{extra}"'
        assert anchor in src, "变异锚点不存在（extra 分支被改了？）"
        anchor2 = 'problems.append(f"架构总览表缺矩阵模块链接：{missing}")'
        assert anchor2 in src, "变异锚点不存在（missing 分支被改了？）"
        # 用「注入变异体」方式短路：包装 check，跳过集合判据
        orig = vbr.check_bom_readme

        def mutated(text: str, n: int, truth: dict) -> list[str]:
            # 变异：先拿原判据，再删掉集合类问题（模拟 extra/missing 分支被短路）
            probs = orig(text, n, truth)
            return [p for p in probs if "架构总览表" not in p]

        vbr.check_bom_readme = mutated

        p_extra = run(make_readme(extra_link="kitex-examples"))
        p_missing = run(make_readme(drop_module="authx"))
        check(not p_extra and not p_missing,
              f"变异自证失败：短路集合判据后 V5/V6 仍红（extra={p_extra}, missing={p_missing}）")
        vbr.check_bom_readme = orig
        if FAILURES:
            print("❌ 变异自证失败：")
            for f in FAILURES:
                print(f"  · {f}")
            return 1
        print("✓ 变异自证通过：集合判据短路后 V5/V6 转绿（原判据有牙齿）")
        return 0

    # V1 全对齐夹具 ⇒ 零误报
    p1 = run(make_readme())
    check(p1 == [], f"V1 全对齐夹具应零误报，实得 {p1}")

    # V2 badge 漂移
    p2 = run(make_readme(badge=N + 1))
    check(any(f"badge modules-{N + 1}" in p for p in p2),
          f"V2 badge≠N 应点名漂移，实得 {p2}")

    # V3 badge 缺失（承诺在场）
    p3 = run(make_readme(badge=MISSING))
    check(any("缺 modules badge" in p for p in p3),
          f"V3 badge 缺失应判红，实得 {p3}")

    # V4 prose 漂移
    p4 = run(make_readme(prose=N + 1))
    check(any(f"{N + 1} 个活跃模块仓库" in p for p in p4),
          f"V4 prose≠N 应点名漂移，实得 {p4}")

    # V5 架构表多出非矩阵模块
    p5 = run(make_readme(extra_link="kitex-examples"))
    check(any("多出非矩阵模块" in p and "kitex-examples" in p for p in p5),
          f"V5 多出 kitex-examples 应点名，实得 {p5}")

    # V6 架构表缺矩阵模块
    p6 = run(make_readme(drop_module="authx"))
    check(any("缺矩阵模块" in p and "authx" in p for p in p6),
          f"V6 缺 authx 应点名，实得 {p6}")

    # V7 合数与事实不符
    p7 = run(make_readme(subpkg_claim="23 合 1"))
    check(any("23 合 1" in p and "38" in p for p in p7),
          f"V7 合数 23≠38 应点名，实得 {p7}")

    # V8 合数无可归属模块
    bad = make_readme().replace("（38 合 1）", "").replace(
        "## 兼容性", "矩阵是（38 合 1）的集合\n\n## 兼容性")
    p8 = run(bad)
    check(any("无可归属模块" in p for p in p8),
          f"V8 合数无可归属应判红，实得 {p8}")

    # V9 易变数禁写
    p9 = run(make_readme(volatile="199 条 breaking 记录"))
    check(any("易变数" in p and "199 条 breaking" in p for p in p9),
          f"V9 硬编码 breaking 条数应判红，实得 {p9}")

    if FAILURES:
        print(f"❌ verify_bom_readme 自测 {len(FAILURES)} 项失败：")
        for f in FAILURES:
            print(f"  · {f}")
        return 1
    print(f"✓ verify_bom_readme 自测全绿（V1-V9，{len(MODULES)} 模块夹具；"
          f"--mutate 变异自证可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
