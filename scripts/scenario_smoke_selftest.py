#!/usr/bin/env python3
"""scenario_smoke 自测：去重/死键/行同源/退出码四判据各有牙齿 + 变异自证。

**为什么要有这个自测**（matrix-tools#341）：冒烟的判据脚本自己先得能证明
「它抓得住自己要防的东西」，否则一次网络全绿说明不了任何事（同
consumer_probe 的 _selftest 原则）。本自测**不联网、不碰 go**——只测纯函数：

  V1  去重：两个场景共享同一 mods 集合 ⇒ 只出一个组合，场景名全部归并；
  V2  死键：mods 含矩阵外模块 ⇒ 精确点名场景+模块；全干净 ⇒ 零误报；
  V3  行同源：unique_combos 的每个组合渲染出的 go get 行 == 场景发布行
      （单源纪律——冒烟执行的行必须就是文档给用户的行，不许冒烟侧另拼）；
  V4  退出码：0/1/2 → ok/violation/unreadable 三态映射，不许串；
  M   **变异自证**：把 unique_combos 的去重短路（每个场景单独成组）⇒
      V1 的「组合数 < 场景数」断言必须失效——证明 V1 真在比去重结果。

**无副作用、不联网**：全部夹具在函数内构造，不读真实 SCENARIOS 的网络面。

用法：
    python3 scripts/scenario_smoke_selftest.py             # 正常：全绿
    python3 scripts/scenario_smoke_selftest.py --mutate    # 变异：V1 必须转红
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import scenario_smoke as ss  # noqa: E402
import gen_skill  # noqa: E402
from matrix import MODULES  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


# 夹具：两场景共享 mods（HTTP 全栈组合），一场景独有，一场景带死键
FIXTURE = [
    ("服务构建", [
        {"scene": "场景A", "mods": ["bootstrap", "hertz"]},
        {"scene": "场景B", "mods": ["hertz", "bootstrap"]},   # 同集合不同序 → 必须去重
    ]),
    ("数据访问", [
        {"scene": "场景C", "mods": ["pkg"]},
    ]),
]


def main() -> int:
    mutate = "--mutate" in sys.argv

    # V1 去重
    combos = ss.unique_combos(FIXTURE)
    if mutate:
        # 变异自证：短路去重（每场景独立成组）⇒ V1 的核心断言必须不再成立
        def broken_unique(scenarios):
            out = []
            for _g, rows in scenarios:
                for r in rows:
                    out.append((tuple(sorted(r["mods"])), [r["scene"]]))
            return out
        orig = ss.unique_combos
        ss.unique_combos = broken_unique
        mutated = ss.unique_combos(FIXTURE)
        ss.unique_combos = orig
        n_scenes = sum(len(rows) for _g, rows in FIXTURE)  # 短路后 = 场景数（3）
        check(len(mutated) == n_scenes,
              "变异自证失败：短路去重后组合数未塌回场景数（V1 断言没在比去重结果）")
        if FAILURES:
            print("❌ 变异自证失败：")
            for f in FAILURES:
                print(f"  · {f}")
            return 1
        print("✓ 变异自证通过：短路去重 ⇒ V1 组合数断言失效（原判据有牙齿）")
        return 0

    check(len(combos) == 2, f"V1 去重：3 场景共享/独有应压成 2 组合，实得 {len(combos)}")
    check(combos[0][1] == ["场景A", "场景B"],
          f"V1 归并顺序：同组合场景名应全归并，实得 {combos and combos[0][1]}")
    check(sorted(combos[0][0]) == ["bootstrap", "hertz"],
          f"V1 组合内容错：{combos and combos[0][0]}")

    # V2 死键
    bad_fixture = [("g", [{"scene": "坏场景", "mods": ["pkg", "ghost-mod"]}])]
    dead = ss.dead_keys(bad_fixture)
    check(len(dead) == 1 and "坏场景" in dead[0] and "ghost-mod" in dead[0],
          f"V2 死键应精确点名场景+模块，实得 {dead}")
    check(ss.dead_keys(FIXTURE) == [], "V2 全干净夹具应零误报")

    # V3 行同源：组合渲染行 == 场景发布行（同一 scenario_install_line 函数）
    for mods_tuple, scenes in combos:
        line = gen_skill.scenario_install_line(list(mods_tuple))
        # 找一个使用该组合的真场景，其发布行必须与组合行逐字节相等（集合同、序可能异）
        real = [r for _g, rows in gen_skill.SCENARIOS for r in rows
                if frozenset(r["mods"]) == frozenset(mods_tuple)]
        check(bool(real), f"V3 组合 {mods_tuple} 在真 SCENARIOS 无对应场景")
        if real:
            real_line = gen_skill.scenario_install_line(real[0]["mods"])
            check(line.split() and set(line.split()[2:]) == set(real_line.split()[2:]),
                  f"V3 组合行与发布行模块集合不等：{line} vs {real_line}")
    # 真 SCENARIOS 每行的行必须是 go get + 全 github.com/oekit/ 前缀
    for _g, rows in gen_skill.SCENARIOS:
        for r in rows:
            line = gen_skill.scenario_install_line(r["mods"])
            parts = line.split()
            check(parts[:2] == ["go", "get"] and
                  all(p.startswith("github.com/oekit/") for p in parts[2:]),
                  f"V3 行格式错：{line}")
            check(len(parts) == 2 + len(r["mods"]),
                  f"V3 行模块数 ≠ mods 数：{line}")

    # V4 退出码三态
    check(ss.classify(ss.EXIT_OK, "")[0] == "ok", "V4 rc=0 应为 ok")
    check(ss.classify(ss.EXIT_VIOLATION, "x")[0] == "violation", "V4 rc=1 应为 violation")
    check(ss.classify(ss.EXIT_UNREADABLE, "")[0] == "unreadable",
          "V4 rc=2 应为 unreadable")
    check(ss.EXIT_OK == 0 and ss.EXIT_VIOLATION == 1 and ss.EXIT_UNREADABLE == 2,
          "V4 退出码常量漂移")

    if FAILURES:
        print(f"❌ scenario_smoke 自测 {len(FAILURES)} 项失败：")
        for f in FAILURES:
            print(f"  · {f}")
        return 1
    n = sum(len(r) for _, r in gen_skill.SCENARIOS)
    print(f"✓ scenario_smoke 自测全绿（V1-V4；真 SCENARIOS {n} 场景行 / "
          f"{len(ss.unique_combos())} 去重组合；--mutate 变异自证可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
