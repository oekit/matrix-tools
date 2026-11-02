#!/usr/bin/env python3
"""audit「各仓对**别仓嵌套模块**的地板」这道门禁的自测（oekit/oekit#104）。

**为什么要有这个自测**：这道门禁此前**不存在**——
  · check_nested_module_floors 只管「子→父」（嵌套模块 require 父仓）；
  · scan_repo_floors 只管**根模块**形态；
  · 阶段 6 只核对 BOM 快照新鲜度。
⇒ 「根仓 require 别仓嵌套模块」这一类漂移**三道门禁都看不见** ⇒
2026-11-02 实测 13 处（跨 6 仓）只能靠人翻出来。

一道没人验证过的判据，很可能自己就是个假绿。

**反证怎么构造**（全部用临时目录造 fixture，**不联网**）：
  1. 嵌套地板落后 ⇒ 必须被判出来（清单里带仓/文件/模块/两版号）；
  2. 嵌套地板齐平 ⇒ 不许误报；
  3. **自己仓的嵌套模块不归本判据**（归「子→父」方向）；
  4. 非嵌套形态（根模块）不归本判据；
  5. `// indirect` 行也要判；
  6. **版本按数字比**：v0.3.0 < v0.31.0（字符串排序会反过来）；
  7. **变异自证**：把「按数字比」改成字符串比 ⇒ 第 6 条必须转红；
     把嵌套形态正则退回「只匹配根模块」⇒ 第 1 条必须转红。

用法：python3 scripts/audit_nested_floor_gate_selftest.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def write_gomod(path: Path, requires: list[tuple[str, str]], indirect: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["module example.com/probe", "", "go 1.24.0", "", "require ("]
    for dep, ver in requires:
        lines.append(f"\tgithub.com/oekit/{dep} {ver}" + (" // indirect" if indirect else ""))
    lines += [")", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def load_scan_func(src: str | None = None):
    """**只取 audit.py 里的纯函数**，不 import 整个 audit。

    为什么不能 `import audit`：audit.py 是**脚本**——import 它会把整个审计
    （读 24 仓远端 tag、跑全部判据）执行一遍 ⇒ 自测变慢且依赖网络。
    这里按源码切片取 `parse_semver` + `scan_repo_nested_floors` 两个纯函数。
    """
    src = src if src is not None else (HERE / "audit.py").read_text(encoding="utf-8")

    def grab(name: str) -> str:
        m = re.search(rf"^def {name}\(.*?(?=^\S)", src, re.S | re.M)
        if not m:
            raise AssertionError(f"audit.py 里找不到 {name}")
        return m.group(0)

    ns: dict = {"re": re}
    # 常量也必须进命名空间——scan_repo_nested_floors 依赖 _NESTED_FLOOR_REQ_RE
    m = re.search(r"^_NESTED_FLOOR_REQ_RE = re\.compile\(.*?re\.M\)", src, re.S | re.M)
    if not m:
        raise AssertionError("audit.py 里找不到 _NESTED_FLOOR_REQ_RE")
    exec(m.group(0), ns)
    exec(grab("parse_semver"), ns)
    exec(grab("scan_repo_nested_floors"), ns)
    return ns["scan_repo_nested_floors"], ns["parse_semver"]


def test_detects_lagging_nested_floor() -> None:
    scan, _ = load_scan_func()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "hertz"
        write_gomod(root / "go.mod", [("registry/nacosimpl", "v0.1.0"),
                                      ("logx/hlog", "v0.1.4")])
        write_gomod(root / "sub" / "go.mod", [("logx/kitexlog", "v0.1.6")])
        latest = {
            "github.com/oekit/registry/nacosimpl": "v0.1.1",
            "github.com/oekit/logx/hlog": "v0.1.6",
            "github.com/oekit/logx/kitexlog": "v0.1.6",
        }
        out = scan("hertz", root, lambda m: latest.get(m))
        joined = "\n".join(out)
        check(len(out) == 2, f"应判出 2 处落后，实得 {len(out)}：{out}")
        check("registry/nacosimpl" in joined and "v0.1.0" in joined and "v0.1.1" in joined,
              f"清单里应含 nacosimpl 的两版号：{out}")
        check("logx/hlog" in joined and "v0.1.4" in joined and "v0.1.6" in joined,
              f"清单里应含 hlog 的两版号：{out}")
        check("kitexlog" not in joined,
              f"齐平的地板不该被判：{out}")


def test_no_false_positive() -> None:
    scan, _ = load_scan_func()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "hertz"
        write_gomod(root / "go.mod", [("registry/nacosimpl", "v0.1.1")])
        latest = {"github.com/oekit/registry/nacosimpl": "v0.1.1"}
        out = scan("hertz", root, lambda m: latest.get(m))
        check(not out, f"齐平不该判出：{out}")


def test_own_nested_module_excluded() -> None:
    """**自己仓的嵌套模块**归「子→父」方向（check_nested_module_floors）管。

    本判据是「根仓 → **别仓**嵌套模块」方向 ⇒ 自己仓的必须不判，
    否则两个方向的口径会打架（同一个 require 行被两条判据用不同口径比）。
    """
    scan, _ = load_scan_func()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "kitex"
        write_gomod(root / "go.mod", [("observability/tracing/instrumentation/kitexotel",
                                       "v0.7.0")])
        latest = {"github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel":
                  "v0.7.4"}
        out = scan("kitex", root, lambda m: latest.get(m))
        check(not out, f"自己仓的嵌套模块不该被本判据判出：{out}")


def test_root_module_shape_excluded() -> None:
    """根模块形态归 scan_repo_floors 管 ⇒ 本判据不判（口径不重叠）。"""
    scan, _ = load_scan_func()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "hertz"
        write_gomod(root / "go.mod", [("pkg", "v0.31.0")])
        out = scan("hertz", root, lambda m: "v0.31.1")
        check(not out, f"根模块形态不该被本判据判出：{out}")


def test_indirect_line_detected() -> None:
    scan, _ = load_scan_func()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "nns"
        write_gomod(root / "go.mod", [("registry/nacosimpl", "v0.1.0")], indirect=True)
        latest = {"github.com/oekit/registry/nacosimpl": "v0.1.1"}
        out = scan("nns", root, lambda m: latest.get(m))
        check(len(out) == 1, f"indirect 行也必须判出：{out}")


def test_numeric_compare() -> None:
    scan, parse = load_scan_func()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "x"
        # ⚠️ 反例必须选**字符串与数字结论相反**的那一对：
        #   v0.3.0 vs v0.31.0 —— 字符串比较恰好同向（'.'0x2E < '1'0x31），
        #   抓不住「退回字符串排序」的变异；
        #   v0.9.0 vs v0.31.0 —— 字符串说 v0.9.0 > v0.31.0（'9'0x39 > '3'0x31），
        #   数字说 v0.9.0 < v0.31.0 ⇒ **结论相反** ⇒ 抓得住变异。
        write_gomod(root / "go.mod", [("logx/hlog", "v0.9.0")])
        latest = {"github.com/oekit/logx/hlog": "v0.31.0"}
        out = scan("x", root, lambda m: latest.get(m))
        check(len(out) == 1,
              f"版本必须按数字比：v0.9.0 对 v0.31.0 应判落后，实得 {out}")
        check(parse("v0.9.0") < parse("v0.31.0"),
              "数字比较必须给出 v0.9.0 < v0.31.0")
        check("v0.9.0" > "v0.31.0",
              "字符串排序在本反例上确实给出相反结论（记录该事实）")


def test_mutation_is_detected() -> None:
    """**变异自证**：两处变异 ⇒ 对应断言必须转红（不是崩异常）。"""
    audit_src = (HERE / "audit.py").read_text(encoding="utf-8")

    # 变异 1：嵌套形态正则退回「只匹配根模块」⇒ 落后判不出
    orig = audit_src
    mutant_re = (
        '_NESTED_FLOOR_REQ_RE = re.compile(\n'
        '    r"^(?:\\t|require )(github\\.com/oekit/([a-z0-9-]+)) '
        '(v\\d+\\.\\d+\\.\\d+)( // indirect)?$",\n'
        '    re.M)'
    )
    mutated = re.sub(
        r'_NESTED_FLOOR_REQ_RE = re\.compile\(.*?re\.M\)',
        lambda _m: mutant_re,
        orig, count=1, flags=re.S)
    check(mutated != orig, "变异 1 注入失败（嵌套形态正则没被替换）")
    if mutated != orig:
        try:
            scan, _ = load_scan_func(mutated)
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "hertz"
                write_gomod(root / "go.mod", [("registry/nacosimpl", "v0.1.0")])
                latest = {"github.com/oekit/registry/nacosimpl": "v0.1.1"}
                out = scan("hertz", root, lambda m: latest.get(m))
                check(not out,
                      "变异 1（正则退回只匹配根模块）本应让嵌套落后判不出，"
                      "但断言没抓住——判据没牙齿")
        except Exception as e:      # noqa: BLE001
            check(False, f"变异 1 下自测崩溃而非判红：{e!r}")

    # 变异 2：版本比较退回字符串排序 ⇒ v0.3.0 vs v0.31.0 判不出
    mutated2 = orig.replace(
        "            if floor < cur:\n"
        "                out.append(f\"{repo}/{rel}: 嵌套地板 {module_path} {ver} 落后远端最新 {lt}\")",
        "            if ver < lt:   # 变异：字符串比较\n"
        "                out.append(f\"{repo}/{rel}: 嵌套地板 {module_path} {ver} 落后远端最新 {lt}\")")
    check(mutated2 != orig, "变异 2 注入失败（版本比较没被替换）")
    if mutated2 != orig:
        try:
            scan, _ = load_scan_func(mutated2)
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "x"
                write_gomod(root / "go.mod", [("logx/hlog", "v0.9.0")])
                latest = {"github.com/oekit/logx/hlog": "v0.31.0"}
                out = scan("x", root, lambda m: latest.get(m))
                check(not out,
                      "变异 2（版本比较退回字符串）本应让 v0.9.0 vs v0.31.0 判不出，"
                      "但断言没抓住——判据没牙齿")
        except Exception as e:      # noqa: BLE001
            check(False, f"变异 2 下自测崩溃而非判红：{e!r}")


def main() -> int:
    test_detects_lagging_nested_floor()
    test_no_false_positive()
    test_own_nested_module_excluded()
    test_root_module_shape_excluded()
    test_indirect_line_detected()
    test_numeric_compare()
    test_mutation_is_detected()

    if FAILURES:
        print(f"❌ audit 嵌套模块地板门禁自测：{len(FAILURES)} 条未通过")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("✓ audit 嵌套模块地板门禁自测全绿（6 组判据 + 2 项变异自证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
