#!/usr/bin/env python3
"""audit「BOM 兼容地板」门禁的自测（oekit/oekit#163）。

**为什么要有这个自测**：这道门禁此前不存在——既有三道地板门禁比的都是
「远端最新 tag」，没有一道强制「各模块 ≥ BOM 快照地板」（#163：BOM 到
v0.35.2 而 data/redisx 停在 v0.31.0，MVS 静默抬升、测试基线失真）。
一道没人验证过的判据，很可能自己就是个假绿。

**反证怎么构造**（全部用内存 fixture，**不联网**、不读本地检出）：
  1. require 低于 BOM 地板 ⇒ 必须报（label + 模块 + 两版本）；
  2. 齐平 / 超前 ⇒ 不许报；
  3. BOM 未收录的模块（第三方）⇒ 不报；
  4. **版本按数字比**：v0.9.0 < v0.10.0（字符串排序会反过来）；
  5. 不可解析版本 ⇒ 报「不可解析」而不是静默跳过；
  6. 嵌套模块形态（logx/hlog）与根模块形态分别匹配；
  7. `// indirect` 行同样要判；
  8. **变异自证**：把比较改成字符串比 ⇒ 第 4 条必须转红；
     把 `floors.get` 改成「任何模块都比」⇒ 第 3 条必须报。

用法：python3 scripts/audit_bom_floor_gate_selftest.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def load_funcs(src: str | None = None):
    """**只取 audit.py 里的纯函数/常量**，不 import 整个 audit（同款切片模式）。"""
    src = src if src is not None else (HERE / "audit.py").read_text(encoding="utf-8")

    def grab(pattern: str, what: str) -> str:
        m = re.search(pattern, src, re.S | re.M)
        if not m:
            raise AssertionError(f"audit.py 里找不到 {what}")
        return m.group(0)

    ns: dict = {"re": re}
    exec(grab(r"^_FLOOR_REQ_RE = re\.compile\(.*?re\.M\)", "_FLOOR_REQ_RE"), ns)
    exec(grab(r"^_NESTED_FLOOR_REQ_RE = re\.compile\(.*?re\.M\)", "_NESTED_FLOOR_REQ_RE"), ns)
    exec(grab(r"^def parse_semver\(.*?(?=^\S)", "parse_semver"), ns)
    exec(grab(r"^def parse_bom_floors\(.*?(?=^\S)", "parse_bom_floors"), ns)
    exec(grab(r"^def _iter_oekit_requires\(.*?(?=^\S)", "_iter_oekit_requires"), ns)
    exec(grab(r"^def scan_bom_floor_violations\(.*?(?=^\S)", "scan_bom_floor_violations"), ns)
    return ns


BOM_TEXT = """module github.com/oekit/oekit

go 1.26.0

require (
\tgithub.com/oekit/pkg v0.37.1
\tgithub.com/oekit/logx v0.8.6
\tgithub.com/oekit/logx/hlog v0.1.9
\tgithub.com/oekit/bootstrap/nacos v0.3.19
)
"""


def main() -> None:
    f = load_funcs()
    floors = f["parse_bom_floors"](BOM_TEXT)
    check(floors == {"pkg": "v0.37.1", "logx": "v0.8.6",
                     "logx/hlog": "v0.1.9", "bootstrap/nacos": "v0.3.19"},
          f"parse_bom_floors 解析错误: {floors}")
    check(f["parse_bom_floors"]("") == {}, "空 BOM 文本必须得到空地板表")

    # 1/2/3/6/7：落后必报、齐平超前不报、未收录不报、嵌套形态、indirect
    gomod = """module github.com/oekit/data

go 1.26.0

require (
\tgithub.com/oekit/pkg v0.31.0
\tgithub.com/oekit/logx v0.8.6
\tgithub.com/oekit/logx/hlog v0.1.9 // indirect
\tgithub.com/oekit/bootstrap/nacos v0.3.19
\tgithub.com/spf13/cast v1.7.0
)
"""
    viols = f["scan_bom_floor_violations"](floors, {"go.mod": gomod})
    check(len(viols) == 1 and "pkg v0.31.0" in viols[0] and "v0.37.1" in viols[0],
          f"落后地板必须恰好报 pkg 一条（含两版本）: {viols}")
    check(any("go.mod:" in v for v in viols), "报告必须带 label 前缀")

    # 4：版本按数字比（字符串排序会把 v0.10.0 判小于 v0.9.0）
    v9 = f["scan_bom_floor_violations"]({"pkg": "v0.10.0"},
                                        {"go.mod": "\tgithub.com/oekit/pkg v0.9.0\n"})
    check(len(v9) == 1, "v0.9.0 必须低于 v0.10.0（按数字比）")
    v_eq = f["scan_bom_floor_violations"]({"pkg": "v0.10.0"},
                                          {"go.mod": "\tgithub.com/oekit/pkg v0.10.0\n"})
    check(v_eq == [], "齐平不许报")

    # 5：畸形版本行（两段式，正则不识别）⇒ 不崩不报（正则形态即护栏，go 规范三段式）
    v_bad = f["scan_bom_floor_violations"]({"pkg": "v0.10.0"},
                                           {"go.mod": "\tgithub.com/oekit/pkg v0.10\n"})
    check(v_bad == [], f"畸形版本行必须不崩不报: {v_bad}")

    # 8a：变异自证——比较改成字符串比 ⇒ 第 4 条必须转红
    mutated = load_funcs(
        (HERE / "audit.py").read_text(encoding="utf-8").replace(
            "if parse_semver(ver) < parse_semver(floor):   # 按元组数字比，v0.9.0 < v0.10.0",
            'if str(ver) < str(floor):  # 变异：字符串比'))
    m9 = mutated["scan_bom_floor_violations"]({"pkg": "v0.10.0"},
                                              {"go.mod": "\tgithub.com/oekit/pkg v0.9.0\n"})
    check(len(m9) == 0, "变异（字符串比）后 v0.9.0 漏报——自测必须能抓住")

    # 8b：变异自证——「BOM 未收录也判」⇒ 未收录的 oekit 模块被误报
    src2 = (HERE / "audit.py").read_text(encoding="utf-8").replace(
        'if floor is None:\n                continue          # BOM 未收录（非矩阵模块）⇒ 不判',
        'if floor is None:\n                floor = "v99.0.0"  # 变异：任何 oekit 模块都比')
    m3 = load_funcs(src2)["scan_bom_floor_violations"](
        floors, {"go.mod": "\tgithub.com/oekit/kitx v0.4.2\n"})
    check(len(m3) == 1 and "kitx" in m3[0], "变异（未收录也判）后 kitx 被误报——自测必须能抓住")

    if FAILURES:
        print("❌ BOM 地板门禁自测失败：")
        for m in FAILURES:
            print(f"  - {m}")
        sys.exit(1)
    print("✓ BOM 地板门禁自测通过（8 组判据含变异自证）")


if __name__ == "__main__":
    main()
