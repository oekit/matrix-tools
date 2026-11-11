#!/usr/bin/env python3
"""检查面 H（apidiff 门禁，matrix-tools#270）判定核的自测——离线、不装工具。

门禁防的是什么：**「签名变了但 CHANGELOG 没写 BREAKING」的隐式破坏**
（双实弹：concurrency PanicHandler 改签名、httpx NewClient 改返回值，
release notes 只写了别的 ⇒ 下游 11 仓编译断裂才发现）。判定核把
「apidiff 输出里哪些行算不兼容」与「版本节里怎样算标注了」两件事封成
纯函数 ⇒ 这里证明纯函数抓得住双实弹、放得行真标注，且变异可证。

断言：
  A1 兼容性差异行（新增方法/仅 compatible）⇒ 不报；
  A2 不兼容行 + 版本节带 **BREAKING** 标注 ⇒ 放行（有意识的破坏性变更）；
  A3 不兼容行 + 版本节无标注 ⇒ 红（列出全部不兼容行，不是只报条数）；
  A4 版本节读不到（空面）⇒ 视为无标注 ⇒ 红（判不了 ≠ 写了）；
  A5 中文标注（破坏性/不兼容）同样放行（矩阵 CHANGELOG 中英混写是常态）；
  M  变异自证：标注判据被改瞎（永不匹配）⇒ A2 必须转红。

用法：python3 scripts/release_train_apidiff_selftest.py [--mutate]
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "release_train.py"

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def _load_gate(src: str):
    ns: dict = {"re": re}
    for name in ("incompatible_lines", "apidiff_gate_problems"):
        m = re.search(rf"^def {name}\(.*?(?=^\S)", src, re.S | re.M)
        if not m:
            raise AssertionError(f"release_train.py 里找不到 {name}")
        exec(compile(m.group(0), "<slice>", "exec"), ns)
    m = re.search(r"^BREAKING_MARKER_RE = re\.compile\(.*?\)", src, re.S | re.M)
    exec(m.group(0), ns)
    return ns


DIFF = """Incompatible changes:
\tasync.PanicHandler: changed from func(msg string, stack []byte) to func(ctx context.Context, r any, stack []byte)
\tNewClient: changed from func(...) *Client to func(...) (*Client, error)
Compatible changes:
\tRunWithLog: added
\tOptions: added
"""


def main() -> int:
    src = TOOL.read_text(encoding="utf-8")
    if "--mutate" in sys.argv:
        # 判据已迁 gen_versions.has_breaking_annotation（#292 单一判据源）——
        # 改瞎判定调用本身（`if False`），证明判据在生效
        mutated = src.replace("if has_breaking_annotation(section):", "if False:")
        ns = _load_gate(mutated)
        sec = "## v0.1.0\n\n**BREAKING**: 改签名\n"
        p = ns["apidiff_gate_problems"](DIFF, sec)
        check(bool(p), f"M: 标注判据改瞎后，已标注形态必须转红（证明判据在生效）——实得 {p}")
        if FAILURES:
            for f in FAILURES:
                print(f"FAIL: {f}")
            return 1
        print("✓ 变异自证：标注判据改瞎 ⇒ 已标注形态转红（判据有牙齿）")
        return 0

    ns = _load_gate(src)

    # A1：兼容性差异（仅 Compatible 小节）⇒ 不报
    only_compatible = "Compatible changes:\n\tRunWithLog: added\n"
    check(ns["apidiff_gate_problems"](only_compatible, "") == [],
          "A1: 仅兼容性差异必须放行")
    check(ns["incompatible_lines"](only_compatible) == [],
          "A1b: Compatible 小节的行不算 incompatible")

    # A2：不兼容 + 确定性标注（顶格粗体标记行）⇒ 放行（#292 口径）
    sec2 = "## v0.2.0\n\n**BREAKING**: PanicHandler 签名变更，迁移说明见 xxx\n"
    check(ns["apidiff_gate_problems"](DIFF, sec2) == [],
          "A2: 顶格 **BREAKING** 标记行必须放行")

    # A3：不兼容 + 无标注 ⇒ 红，且列出 Incompatible 小节的具体行（added 不列）
    p3 = ns["apidiff_gate_problems"](DIFF, "## v0.2.0\n\n- fix: 修了个别的\n")
    check(len(p3) >= 3 and "2" in p3[0] and "PanicHandler" in p3[1]
          and "NewClient" in p3[2] and not any("RunWithLog" in x for x in p3),
          f"A3: 无标注的不兼容变更必须逐行列出（且不含 Compatible 行）——实得 {p3}")

    # A4：版本节读不到（空面）⇒ 视为无标注 ⇒ 红
    check(bool(ns["apidiff_gate_problems"](DIFF, "")),
          "A4: 空版本节必须视为无标注（判不了 ≠ 写了）")

    # A5：中文标注（条目加粗前缀形态）同样放行
    sec5 = "## v0.3.0\n\n- **破坏性**：改签名，迁移：xxx\n"
    check(ns["apidiff_gate_problems"](DIFF, sec5) == [],
          "A5: 中文破坏性标注（条目加粗前缀）必须放行（矩阵 CHANGELOG 中英混写是常态）")

    # A6：正文裸词/中段粗体强调不算标注（#292 假豁免实锤形态）
    sec6 = "## v0.4.0\n\n- fix: 修了点别的，本版无破坏性变更\n- build: 升级到不会再静默落到**互不兼容**的旧组合\n"
    check(bool(ns["apidiff_gate_problems"](DIFF, sec6)),
          "A6: 「无破坏性变更」「**互不兼容**的旧组合」等无关字样不得豁免未标注的 BREAKING（假绿）")

    # A7：全小写 **breaking** 标记行照认（case-insensitive，#292 假红面）
    sec7 = "## v0.5.0\n\n**breaking**: 改签名\n"
    check(ns["apidiff_gate_problems"](DIFF, sec7) == [],
          "A7: 全小写 **breaking** 标记行必须放行（假红）")

    # A8（#291）：changelog_section 调用形状钉——必须三参显式传 nested；
    # 两参形状必 TypeError（nested 无缺省值），字面量级断言防再漂移
    check("changelog_section(str(mod_dir), tag, None)" in src,
          "A8: apidiff 面必须三参调用 changelog_section(str(mod_dir), tag, None)")
    check(not re.search(r"changelog_section\(str\(mod_dir\),\s*tag\)", src),
          "A8b: 两参调用形状必须绝迹（无缺省值必 TypeError，#291）")

    if FAILURES:
        for f in FAILURES:
            print(f"FAIL: {f}")
        return 1
    print("✓ apidiff 门禁判定核自测通过（双实弹形态抓得住 / 真标注放得行 / 变异 --mutate 可证）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
