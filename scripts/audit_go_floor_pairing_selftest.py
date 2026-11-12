#!/usr/bin/env python3
"""审计「Go 地板三层口径 + go.mod/CI 成对」门禁（check_go_floor_pairing）的自测。

**为什么要有这个自测**（matrix-tools#307）：
这道门禁的上限常量 `GO_FLOOR_BASE_MAX` 曾停在 `(1, 24)`，而基础层已随五波次
现代化整体抬到 `go 1.26.0` ⇒ pkg/concurrency/logx/core/kitx/metrics **恒红 6 条**
⇒ audit 门禁「Fail on drift」对任何 matrix-tools PR 恒不满足——门禁自己把
仓库焊死。修常量的改动（见 #304 内 cf14415）**没有自测**：常量与
`GO_FLOOR_BUSINESS_OK` 上界再次分裂时（谁只改了一个），没有任何机器会发现。
本自测把三条口径钉死：**上界同值不误报 / 越界必判 / 成对红线不放水**。

**反证怎么构造**（全部用临时目录造 fixture，**不联网**）：
  1. 基础层 go 1.26.0（= BUSINESS_OK 上界）⇒ 不许报红（#307 的恒红形态）；
  2. 基础层 go 1.27.0（越上限）⇒ 必须判红，且文案带上限值（不写死 1.24）；
  3. 基础层 go.mod/CI 成对（1.26 / '1.26'）⇒ 不报；CI 用 1.25 ⇒ 成对红线必判；
  4. 业务层 1.24 / 1.25 / 1.26 ⇒ 不报；1.27 ⇒ 判红（不在允许档位）；
  5. 豁免层（tracing）1.26 ⇒ 不报；
  6. 无 go.mod ⇒ None 分支直接跳过（不误报）；
  7. **常量关系钉**：`max(GO_FLOOR_BUSINESS_OK) == GO_FLOOR_BASE_MAX`——两常量
     再次分裂（只改一个）⇒ 本条直接红；
  8. **变异自证**：把 ns 里的 `GO_FLOOR_BASE_MAX` 改回 `(1, 24)` ⇒ 形态 1 必须
     从绿转红（证明本自测真的钉住了常量，而不是恒绿）。

用法：python3 scripts/audit_go_floor_pairing_selftest.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import matrix  # noqa: E402  （纯数据模块：常量/清单/正则，无网络无副作用）

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def slice_func(src: str, name: str) -> str:
    """从 audit.py 源码里切出一个顶层函数（含其紧邻的文档字符串）。

    为什么不 import audit：audit.py 是**脚本**——import 它会把整个审计
    （读 24 仓远端 tag、跑全部判据）执行一遍 ⇒ 自测变慢且依赖网络。
    与 audit_floor_gate_selftest 同款「源码切片」手法。
    """
    start = src.index(f"def {name}(")
    # 函数之后可能直接跟模块级可执行代码（audit.py 是脚本，顶层会真跑），
    # 切片必须在「下一个顶层 def / 顶层横幅注释 / class」处截断，绝不把
    # 模块级调用（如 enable_tag_cache()）带进来。
    cuts = [p for p in (src.find("\ndef ", start + 1),
                        src.find("\n# ----------", start + 1),
                        src.find("\nclass ", start + 1)) if p != -1]
    end = min(cuts) if cuts else len(src)
    return src[start:end]


def load_pairing_gate(src: str | None = None):
    """切片装载 check_go_floor_pairing 及其两个读文件助手，返回 (fn, ns)。"""
    src = src if src is not None else (REPO / "scripts" / "audit.py").read_text(encoding="utf-8")
    ns: dict = {
        "re": re,
        "Path": Path,
        "GO_FLOOR_BASE_MAX": matrix.GO_FLOOR_BASE_MAX,
        "GO_FLOOR_BUSINESS_OK": matrix.GO_FLOOR_BUSINESS_OK,
        "GO_FLOOR_BASE_LAYER": matrix.GO_FLOOR_BASE_LAYER,
        "GO_FLOOR_EXEMPT": matrix.GO_FLOOR_EXEMPT,
    }
    findings: list[str] = []

    def record(cond, msg):  # audit.check 的最小替身：只记判红项
        if not cond:
            findings.append(msg)

    ns["check"] = record
    for fn in ("_go_directive", "_ci_go_version", "check_go_floor_pairing"):
        exec(compile(slice_func(src, fn), "<pairing-slice>", "exec"), ns)
    ns["_findings"] = findings
    return ns["check_go_floor_pairing"], ns


def write_repo(root: Path, go: str | None, ci: str | None) -> Path:
    d = root / "probe"
    d.mkdir(parents=True, exist_ok=True)
    if go is not None:
        (d / "go.mod").write_text(f"module example.com/probe\n\ngo {go}\n", encoding="utf-8")
    if ci is not None:
        wf = d / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "ci.yml").write_text(f"name: ci\non: push\njobs:\n  test:\n"
                                   f"    steps:\n      - uses: actions/setup-go\n"
                                   f"        with:\n          go-version: '{ci}'\n",
                                   encoding="utf-8")
    return d


def run(fn, ns, repo: str, go: str | None, ci: str | None) -> list[str]:
    ns["_findings"].clear()
    with tempfile.TemporaryDirectory() as td:
        d = write_repo(Path(td), go, ci)
        fn(repo, d)
    return list(ns["_findings"])


def main() -> int:
    fn, ns = load_pairing_gate()

    # ── 形态①：基础上限 = BUSINESS_OK 上界（1.26）⇒ 不许报红（#307 恒红形态）──
    got = run(fn, ns, "pkg", "1.26.0", "1.26")
    check(got == [], f"形态① 基础层 go 1.26.0（= 上界）应零判红，got={got}")

    # ── 形态②：越上限 1.27 ⇒ 必判红，且文案带常量推导的上限值 ──
    got = run(fn, ns, "pkg", "1.27.0", "1.27")
    check(len(got) == 1 and "高于 1.26" in got[0],
          f"形态② 基础层 go 1.27.0 应判红且文案含「高于 1.26」，got={got}")

    # ── 形态③：成对红线——go.mod 1.26 / CI 1.25 必判（门禁只放宽地板不放宽成对）──
    got = run(fn, ns, "pkg", "1.26.0", "1.25")
    check(any("CI 却用 1.25" in m for m in got),
          f"形态③ go.mod/CI 不成对应判红，got={got}")

    # ── 形态④：业务层三档不报；越档 1.27 判红 ──
    for ver in ("1.24.0", "1.25.0", "1.26.0"):
        got = run(fn, ns, "hertz", ver, ver.rsplit(".", 1)[0])
        check(got == [], f"形态④ 业务层 go {ver} 应零判红，got={got}")
    got = run(fn, ns, "hertz", "1.27.0", "1.27")
    check(len(got) == 1 and "不在允许档位" in got[0],
          f"形态④ 业务层 go 1.27.0 应判「不在允许档位」，got={got}")

    # ── 形态⑤：豁免层（tracing）1.26 不报 ──
    got = run(fn, ns, "tracing", "1.26.0", "1.26")
    check(got == [], f"形态⑤ 豁免层 tracing 应零判红，got={got}")

    # ── 形态⑥：无 go.mod ⇒ None 分支跳过（不误报）──
    got = run(fn, ns, "pkg", None, None)
    check(got == [], f"形态⑥ 无 go.mod 应跳过不判，got={got}")

    # ── 形态⑦：常量关系钉——上限必须等于 BUSINESS_OK 上界（防两常量再分裂）──
    check(max(matrix.GO_FLOOR_BUSINESS_OK) == matrix.GO_FLOOR_BASE_MAX,
          f"形态⑦ GO_FLOOR_BASE_MAX {matrix.GO_FLOOR_BASE_MAX} 与 "
          f"max(GO_FLOOR_BUSINESS_OK) {max(matrix.GO_FLOOR_BUSINESS_OK)} 分裂——"
          f"两常量必须同值（#307）")

    # ── 形态⑧：变异自证——上限改回 (1, 24) ⇒ 形态① 必须从绿转红 ──
    fn_m, ns_m = load_pairing_gate()
    ns_m["GO_FLOOR_BASE_MAX"] = (1, 24)
    got = run(fn_m, ns_m, "pkg", "1.26.0", "1.26")
    check(len(got) == 1 and "高于 1.24" in got[0],
          f"形态⑧ 变异（上限回退 1.24）后形态① 必须转红，got={got}")

    if FAILURES:
        print("✗ check_go_floor_pairing 自测失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("✓ check_go_floor_pairing 自测通过（8 形态：上界不误报 / 越界必判 / "
          "成对红线 / 业务三档 / 豁免 / 无 go.mod / 常量关系钉 / 变异自证）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
