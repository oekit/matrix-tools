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

**无副作用、不联网、不依赖工作区布局**：全部用内存里的 llms.txt 文本造 fixture
（判据 `check_llms_units` 只吃文本，不需要文件系统）。

用法：python3 scripts/verify_release_units_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # matrix.py 在仓根，不在 scripts/ 下

from matrix import MODULES, NESTED_MODULES  # noqa: E402
from verify_release_units import (  # noqa: E402
    _GO_GET_RE,
    check_llms_units,
    release_unit_count,
    render_nested_section,
)

BOM_MOD = "github.com/oekit/oekit"


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


def main() -> None:
    failures: list[str] = []

    base = make_llms()

    # 用例 0：合法文本 ⇒ 必须零问题（先证明 fixture 本身是合法的，
    # 否则后面「变异必须判红」可能是因为 fixture 本来就坏）
    problems = check_llms_units(base)
    if problems:
        failures.append(f"用例0 期望零问题（合法 fixture），实得 {problems}")

    # 用例 1（关键反证，#82 的原始形态）：删掉伞仓安装行 ⇒ 必须判红
    mutated = drop_install_line(base, BOM_MOD)
    if BOM_MOD in mutated:
        failures.append("用例1 fixture 变异失败：伞仓安装行还在")
    problems = check_llms_units(mutated)
    if not problems:
        failures.append("用例1 期望抓到「llms.txt 缺伞仓 BOM 安装行」，实得零问题")
    elif not any("伞仓" in p for p in problems):
        failures.append(f"用例1 报错未点名伞仓：{problems}")

    # 用例 2：删掉某个嵌套模块的安装行 ⇒ 必须判红（确认新检查没顶掉老的）
    target_nested = NESTED_MODULES[0]
    mutated = drop_install_line(base, target_nested["module"])
    problems = check_llms_units(mutated)
    if not problems:
        failures.append(f"用例2 期望抓到「缺嵌套模块 {target_nested['module']} 的安装行」，实得零问题")
    elif not any(target_nested["module"] in p for p in problems):
        failures.append(f"用例2 报错未点名该嵌套模块：{problems}")

    # 用例 3：删掉某个根模块条目 ⇒ 必须判红（确认根模块覆盖没被顶掉）
    target_root = MODULES[0]
    mutated = drop_root_entry(base, target_root)
    problems = check_llms_units(mutated)
    if not problems:
        failures.append(f"用例3 期望抓到「llms.txt 缺根模块 {target_root} 条目」，实得零问题")
    elif not any(target_root in p for p in problems):
        failures.append(f"用例3 报错未点名该根模块：{problems}")

    # 用例 4：口径必须是 34（24 根 + 9 嵌套 + 1 伞仓）——
    # 与 canonical 口径（gen_tables.count_units）和站点文案一致。
    # 口径分裂过（门禁说 33、站点说 34），这里钉住。
    root, nested, bom, total = release_unit_count()
    if (root, nested, bom) != (len(MODULES), len(NESTED_MODULES), 1):
        failures.append(f"用例4 发版单元口径不对：{root} 根 + {nested} 嵌套 + {bom} 伞仓")
    if total != root + nested + bom:
        failures.append(f"用例4 合计口径不自洽：{total} ≠ {root}+{nested}+{bom}")
    if bom != 1:
        failures.append(f"用例4 伞仓应为 1 个发版单元，实得 {bom}"
                        f"（伞仓有独立 tag + 独立 Release + go get 安装路径）")

    if failures:
        print("✗ 发版单元门禁自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print(f"✓ 发版单元门禁自测通过：删掉伞仓/嵌套/根模块的安装信息都抓得到；"
          f"口径 {total} 个发版单元（{root} 根 + {nested} 嵌套 + {bom} 伞仓）")


if __name__ == "__main__":
    main()
