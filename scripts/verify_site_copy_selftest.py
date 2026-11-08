#!/usr/bin/env python3
"""站点内容门禁自测：集合判据（deps.json / deps-table / llms.txt 嵌套段）。

**为什么要有这个自测**（matrix-tools#253）：三块站点产物此前完全不被覆盖——
deps.json 是 .json（scan_site_files 只收 .md/.txt）、deps-table 是「行集合」
（不是「N 个单元」句式）、llms.txt 嵌套段（check_llms_coverage 只比「## 模块」段）。
实测 deps.json 漏了一个发版单元 6 天没有任何门禁红。补上的三条集合判据必须有牙齿：

  V1  全对齐夹具 ⇒ 三条集合判据零误报；
  V2  deps.json 删一个单元 ⇒ 精确点名缺的它（判据有牙齿）；
  V3  deps.json 多一个登记表外单元 ⇒ 精确点名多的它（**集合相等**，不是存在性）；
  V4  deps-table 删一行 ⇒ 必须报「与 deps.json 不一致」；
  V5  llms.txt 嵌套段把 logx/sink 改成 logx/sinks ⇒ 必须报缺+多；
  V6  deps.json 缺文件 ⇒ 两条相关判据都 fail-loud（读不到 ≠ 没有，不许静默跳过）；
  V7  llms.txt 缺整个「## 嵌套模块」段 ⇒ fail-loud；
  M   **变异自证**：把「集合相等」换成「只查存在性」（删掉 missing 分支）⇒
      V2 必须从红转绿——证明原判据真的在比集合，而不是「只看站点里有的」。

**无副作用、不联网**：站点仓用临时目录合成夹具，期望值来自 matrix.py 登记表。

用法：
    python3 scripts/verify_site_copy_selftest.py             # 正常：全绿
    python3 scripts/verify_site_copy_selftest.py --mutate    # 变异：V2 必须转绿
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import verify_site_copy as vsc  # noqa: E402
from matrix import MODULES, NESTED_MODULES  # noqa: E402
from gen_tables import BOM_REPO  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def expected_units() -> set[str]:
    return vsc.expected_units()


def make_site(root: Path, *, units: set[str] | None = None,
              table_rows: set[str] | None = None,
              nested_entries: set[str] | None = None,
              with_deps_json: bool = True,
              with_nested_section: bool = True) -> Path:
    """合成站点仓夹具：deps.json + getting-started.md(deps-table) + llms.txt。"""
    docs = root / "docs"
    (docs / "public").mkdir(parents=True)
    (docs / "guide").mkdir(parents=True)
    want = units if units is not None else expected_units()
    rows = table_rows if table_rows is not None else want
    nested = (nested_entries if nested_entries is not None
              else {f"{n['repo']}/{n['path']}" for n in NESTED_MODULES})
    if with_deps_json:
        (docs / "public" / "deps.json").write_text(json.dumps({
            "generatedAt": "2026-11-08T00:00:00Z",
            "org": "oekit",
            "units": [{"unit": u, "module": f"github.com/oekit/{u}", "ok": True,
                       "consumerModules": 1, "thirdParty": 0, "thirdPartyList": [],
                       "ownModules": [f"github.com/oekit/{u}"]} for u in sorted(want)],
        }, ensure_ascii=False), encoding="utf-8")
    lines = ["# 上手", "<!-- deps-table:start -->", "## 装它要拖多少东西（消费方实测）", ""]
    for u in sorted(rows):
        lines.append(f"| [`{u}`](https://github.com/oekit/{u}) | 2 | 0 | 零第三方 |")
    lines += ["", "<!-- deps-table:end -->"]
    (docs / "guide" / "getting-started.md").write_text("\n".join(lines) + "\n",
                                                       encoding="utf-8")
    llms = ["## 模块", ""]
    for m in MODULES:
        llms.append(f"- [{m}](https://github.com/oekit/{m}): {m} 的说明")
    llms += ["", "## 嵌套模块（可选装 · 依赖隔离）", ""]
    if with_nested_section:
        for n in sorted(nested):
            llms.append(f"- [{n}](https://github.com/oekit/{n.replace('/', '/tree/main/')}"
                        f"): {n} 的说明")
            llms.append(f"  - 安装：`go get github.com/oekit/{n}@latest`")
            llms.append("")
    llms += ["## 文档", "", "- 更多见站点"]
    (docs / "public" / "llms.txt").write_text("\n".join(llms) + "\n", encoding="utf-8")
    return root


def main() -> int:
    mutate = "--mutate" in sys.argv
    vsc_mod = vsc
    if mutate:
        # 变异：把「集合相等」换成「只查存在性」（只看站点里有的合不合法，
        # 不再追「登记表里有而站点没有的」）⇒ V2（缺单元）必须从红转绿。
        src = (HERE / "verify_site_copy.py").read_text(encoding="utf-8")
        anchor = ("    if missing:\n"
                  "        problems.append(f\"deps.json 的 units 缺 {len(missing)} 个发版单元：{missing}\"")
        assert anchor in src, "变异锚点不存在（check_deps_units 的 missing 分支被改了？）"
        mut_src = src.replace(anchor,
                              "    if False and missing:\n" + anchor.split("\n", 1)[1], 1)
        import importlib.util
        import types
        tmp = Path(tempfile.mkdtemp()) / "vsc_mutated.py"
        tmp.write_text(mut_src, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("vsc_mutated", tmp)
        vsc_mod = importlib.util.module_from_spec(spec)
        sys.modules["vsc_mutated"] = vsc_mod
        spec.loader.exec_module(vsc_mod)

    with tempfile.TemporaryDirectory() as td:
        site = make_site(Path(td))

        # V1：全对齐 ⇒ 三条集合判据零误报
        check(not vsc_mod.check_deps_units(site), "V1: 全对齐夹具不许误报（deps.json）")
        units, p0 = vsc_mod.read_deps_units(site)
        check(units is not None and not p0, "V1: deps.json 必须可读且无问题")
        check(not vsc_mod.check_deps_table(site, units), "V1: 全对齐夹具不许误报（deps-table）")
        check(not vsc_mod.check_llms_nested_coverage(site), "V1: 全对齐夹具不许误报（嵌套段）")

        # V2：删一个单元 ⇒ 精确点名缺的它
        dropped = sorted(expected_units())[0]
        site2 = make_site(Path(td) / "s2", units=expected_units() - {dropped})
        p2 = vsc_mod.check_deps_units(site2)
        check(len(p2) == 1 and dropped in p2[0] and "缺" in p2[0],
              f"V2: 删单元必须被点名（实得 {p2}）")
        check("extra" not in json.dumps(p2, default=str), "V2: 不该报多余的")

        # V3：多一个登记表外单元 ⇒ 精确点名（集合相等 ≠ 只查存在性）
        site3 = make_site(Path(td) / "s3", units=expected_units() | {"ghost-unit"})
        p3 = vsc_mod.check_deps_units(site3)
        check(len(p3) == 1 and "ghost-unit" in p3[0] and "没有" in p3[0],
              f"V3: 多出的单元必须被点名（实得 {p3}）")

        # V4：deps-table 删一行 ⇒ 必须报「不一致」
        site4 = make_site(Path(td) / "s4", table_rows=expected_units() - {dropped})
        units4, _ = vsc_mod.read_deps_units(site4)
        p4 = vsc_mod.check_deps_table(site4, units4)
        check(len(p4) == 1 and dropped in p4[0] and "不一致" in p4[0],
              f"V4: 删表行必须报不一致（实得 {p4}）")
        # deps.json 缺时退回与登记表比——不许静默跳过
        site4b = make_site(Path(td) / "s4b", table_rows=expected_units() - {dropped},
                           with_deps_json=False)
        p4b = vsc_mod.check_deps_table(site4b, None)
        check(any(dropped in x for x in p4b),
              f"V4b: deps.json 缺时表格照样要跟登记表比（实得 {p4b}）")

        # V5：嵌套段条目改名（logx/sink → logx/sinks）⇒ 缺+多都要点名
        nested = {f"{n['repo']}/{n['path']}" for n in NESTED_MODULES}
        renamed = {("logx/sinks" if n == "logx/sink" else n) for n in nested}
        site5 = make_site(Path(td) / "s5", nested_entries=renamed)
        p5 = vsc_mod.check_llms_nested_coverage(site5)
        check(any("logx/sink" in x and "缺" in x for x in p5)
              and any("logx/sinks" in x for x in p5),
              f"V5: 改名必须同时点名缺与多（实得 {p5}）")

        # V6：deps.json 缺文件 ⇒ 两条相关判据 fail-loud
        site6 = make_site(Path(td) / "s6", with_deps_json=False)
        p6 = vsc_mod.check_deps_units(site6)
        check(len(p6) == 1 and "缺" in p6[0], f"V6: 缺 deps.json 必须点名（实得 {p6}）")
        units6, p6b = vsc_mod.read_deps_units(site6)
        check(units6 is None and len(p6b) == 1,
              "V6: 读不到必须返回 None 并点名（读不到 ≠ 空集）")
        check(not vsc_mod.check_deps_table(site6, units6) or
              all("deps.json 的问题另有判据点名" in x or "缺" in x for x in
                  vsc_mod.check_deps_table(site6, units6)),
              "V6: deps.json 缺时 deps-table 判据不许静默跳过")

        # V7：嵌套段整段被删 ⇒ fail-loud
        site7 = make_site(Path(td) / "s7", with_nested_section=False)
        p7 = vsc_mod.check_llms_nested_coverage(site7)
        check(len(p7) == 1 and "嵌套模块" in p7[0], f"V7: 段被删必须点名（实得 {p7}）")

    if mutate:
        # 变异下的期望：V2（缺单元）从红转绿，其余照旧
        site = make_site(Path(tempfile.mkdtemp()) / "m")
        dropped = sorted(expected_units())[0]
        site_bad = make_site(Path(tempfile.mkdtemp()) / "mb",
                             units=expected_units() - {dropped})
        ok_clean = not vsc_mod.check_deps_units(site)
        caught = bool(vsc_mod.check_deps_units(site_bad))
        if ok_clean and not caught:
            print(f"✓ 变异「只查存在性」生效：缺单元（{dropped}）不再被点名"
                  f" ⇒ 证明原判据真的在比集合（存在性检查抓不住『缺』）")
            return 0
        print(f"✗ 变异自证失败：ok_clean={ok_clean} caught={caught}"
              f" —— 变异后 V2 必须转绿（抓不住缺单元）、V1 必须仍绿")
        return 1

    if FAILURES:
        print("✗ 站点内容门禁自测失败：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        return 1
    n_nested = len(NESTED_MODULES)
    print(f"✓ 站点内容门禁自测通过：全对齐零误报 / 缺单元点名 / 多单元点名（集合相等）/ "
          f"deps-table 不一致 / 嵌套段改名+整段删除 / 缺 deps.json fail-loud"
          f"（{len(expected_units())} 单元 × {n_nested} 嵌套，--mutate 变异自证可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
