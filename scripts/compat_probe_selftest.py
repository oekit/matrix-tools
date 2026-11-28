#!/usr/bin/env python3
"""compat_probe 自测：N-1 选择/退出码裁决/单元枚举三判据有牙齿 + 变异自证。

**为什么要有这个自测**（matrix-tools#343）：两探针的判据脚本自己先得能证明
「它抓得住自己要防的东西」（同 scenario_smoke / consumer_probe 的自测原则）。
本自测**不联网、不碰 go/git**——只测纯函数：

  V1  N-1 选择：semver 过滤（prerelease/嵌套前缀滤掉）+ 降序取第二新；
      不足 2 个 semver tag ⇒ None（可见跳过，不是读不到）；
  V2  单元枚举：all_units == 23 根 + 12 嵌套 + 伞仓，路径全 github.com/oekit/，
      无重复（伞仓不在 MODULES ⇒ 不会与根模块撞）；
  V3  退出码裁决：A 失败 > 读不到 > 全绿 三态优先级不许串；
      探针 B 的 warn 不进退出码（0.x 裁决在 main 侧，裁决函数只吃 A/unreadable）；
  M   **变异自证**：把 finalize 短路成恒 EXIT_OK ⇒ V3 的「A 失败 ⇒ 1」断言
      必须失效——证明 V3 真在裁决而不是恒绿。

**无副作用、不联网**：全部夹具在函数内构造。

用法：
    python3 scripts/compat_probe_selftest.py             # 正常：全绿
    python3 scripts/compat_probe_selftest.py --mutate    # 变异：V3 必须转红
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import compat_probe as cp  # noqa: E402
from matrix import BOM_REPO, MODULES, NESTED_MODULES, ORG  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def main() -> int:
    mutate = "--mutate" in sys.argv

    if mutate:
        # 变异自证：短路 finalize 恒绿 ⇒ V3 的裁决断言必须失效
        orig = cp.finalize
        cp.finalize = lambda **kw: cp.EXIT_OK
        rc_wanted = cp.finalize(a_failures=["x"], unreadables=[])  # 短路后恒 0
        cp.finalize = orig
        check(rc_wanted == cp.EXIT_OK,
              f"变异自证失败：短路后未恒绿（得 {rc_wanted}）——变异没生效")
        # 关键：短路后的值必须与正常裁决不同，否则 V3 断言本来就抓不住
        normal = orig(a_failures=["x"], unreadables=[])
        check(normal == cp.EXIT_VIOLATION and rc_wanted != normal,
              "变异自证失败：短路值与正常裁决相同（V3 断言无牙齿）")
        if FAILURES:
            print("❌ 变异自证失败：")
            for f in FAILURES:
                print(f"  · {f}")
            return 1
        print("✓ 变异自证通过：短路 finalize ⇒ V3 裁决断言失效（原判据有牙齿）")
        return 0

    # V1 N-1 选择
    tags = ["v1.0.0", "v0.9.0", "v1.2.0", "v1.2.0-rc1", "v1.1.0"]
    check(cp.n_minus_one(tags) == "v1.1.0",
          f"V1 应取第二新 v1.1.0（prerelease 滤掉），实得 {cp.n_minus_one(tags)}")
    check(cp.n_minus_one(["v1.0.0"]) is None, "V1 单 tag 应返回 None")
    check(cp.n_minus_one([]) is None, "V1 空 tag 应返回 None")
    check(cp.n_minus_one(["abc", "v1.0.0"]) is None,
          "V1 非 semver 滤掉后不足 2 个应返回 None")
    check(cp.n_minus_one(["hlog/v0.1.4", "v2.0.0", "v1.5.0"]) == "v1.5.0",
          "V1 嵌套前缀 tag 应被滤掉（SEMVER_RE 锚定 ^v）")

    # V2 单元枚举
    units = cp.all_units()
    roots = [f"github.com/{ORG}/{m}" for m in MODULES]
    nested = [n["module"] for n in NESTED_MODULES]
    umbrella = f"github.com/{ORG}/{BOM_REPO}"
    check(len(units) == len(MODULES) + len(NESTED_MODULES) + 1,
          f"V2 单元数应为根+嵌套+伞仓，实得 {len(units)}")
    check(units[:len(roots)] == roots, "V2 根模块段顺序/内容错")
    check(units[len(roots):len(roots) + len(nested)] == nested, "V2 嵌套段错")
    check(units[-1] == umbrella, "V2 末位应是伞仓")
    check(len(set(units)) == len(units), "V2 单元枚举有重复")
    check(all(u.startswith(f"github.com/{ORG}/") for u in units), "V2 非 oekit 路径混入")

    # V3 退出码裁决
    check(cp.finalize(a_failures=[], unreadables=[]) == cp.EXIT_OK, "V3 全绿应为 0")
    check(cp.finalize(a_failures=["x"], unreadables=[]) == cp.EXIT_VIOLATION,
          "V3 A 失败应为 1")
    check(cp.finalize(a_failures=[], unreadables=["y"]) == cp.EXIT_UNREADABLE,
          "V3 读不到应为 2")
    check(cp.finalize(a_failures=["x"], unreadables=["y"]) == cp.EXIT_VIOLATION,
          "V3 A 失败应优先于读不到（违例盖过覆盖不全）")
    check(cp.EXIT_OK == 0 and cp.EXIT_VIOLATION == 1 and cp.EXIT_UNREADABLE == 2,
          "V3 退出码常量漂移")
    # classify 三态映射
    check(cp.classify(cp.EXIT_OK, "")[0] == "ok", "V3 classify(0)=ok")
    check(cp.classify(cp.EXIT_VIOLATION, "e")[0] == "violation", "V3 classify(1)=violation")
    check(cp.classify(cp.EXIT_UNREADABLE, "")[0] == "unreadable",
          "V3 classify(2)=unreadable")

    if FAILURES:
        print(f"❌ compat_probe 自测 {len(FAILURES)} 项失败：")
        for f in FAILURES:
            print(f"  · {f}")
        return 1
    print(f"✓ compat_probe 自测全绿（V1-V3；{len(cp.all_units())} 单元枚举 / "
          f"{len(MODULES)} 根模块 N-1 面；--mutate 变异自证可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
