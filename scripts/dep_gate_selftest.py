#!/usr/bin/env python3
"""dep_gate 自测——分层无环门禁的判据回归（合成夹具，零网络零 git）。

为什么要有这道自测：dep_gate 是「零执行承诺」变门禁的第一道方向/环判据。
它红错了（假红）会让人去「修」不存在的违例；它绿错了（假绿）会让全矩阵
无环继续当零执行承诺。两个方向都必须有夹具钉死。

夹具口径：MODULES 夹具序 = 自底向上（pkg(0) 最底 … hertz(5) 最顶），
与真实矩阵同构；展示带（gen_skill.LAYERS 夹具）与序位对齐，但 T15 专门构造
「带与序错位」验证 ⚠ 对账只报告不阻塞。

覆盖面（每条对应 dep_gate 的一条判据或一个已知坑）：
  T1  干净分层图 → EXIT_OK，边数正确
  T2  序位违例（底部单元 require 顶部单元）→ 违例点名两单元与两个序位
  T3  序位相容边放行（高位 require 低位）
  T4  环（A→B→A）→ 环判据抓住（环边必有一条序位违例，只断言环消息在场）
  T5  自环（require 自己）→ 环判据报自环
  T6  同仓序位：父仓 require 自己的嵌套模块 → 违例（隔离方向应是子 require 父，
      2026-11-27 已核实真矩阵零此类边）；父↔嵌套双向 → 环
  T7  indirect 标注：方向判据与 ⚠ 对账都跳过 indirect 边，环判据仍吃
  T8  行尾 indirect 判定不跨行（下一行是注释行不误判）——正则钉行尾的回归
  T9  伪版本依赖行照常出边（#306 形态）
  T10 伞仓 BOM：BOM require 全体放行（序位在最顶）；模块 require BOM → 序位违例
      （且不出 ⚠ 对账行——伞仓没有展示带）
  T11 未知 oekit 路径（已删除仓）→ 违例点名
  T12 承诺句缺失 → 违例（承诺与门禁同进退）
  T13 读不到（reader 抛 ReaderError）→ EXIT_UNREADABLE（#79：判不了 ≠ 零漂移）
  T14 变异自证：干净夹具注入单条序位违例 ⇒ 转红集合**恰好**是那一条（不多报）
  T15 展示带与序位错位（logx→bootstrap 型，2026-11-27 首跑实录的 authx→logx
      同构）→ rc 仍 OK，只出 ⚠ 对账行点名两边

用法：python3 scripts/dep_gate_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import dep_gate  # noqa: E402
import matrix  # noqa: E402


class Fixture:
    """合成矩阵：gomod 由字典提供；read 签名与 dep_gate.make_git_reader 一致。"""

    def __init__(self, gomods: dict[str, str],
                 readme: str = "依赖严格自底向上：**上层依赖下层，全矩阵无环**。"):
        self.gomods = gomods
        self.readme = readme

    def read(self, repo: str, relpath: str) -> str:
        if relpath == "README.md":
            if self.readme is None:
                raise dep_gate.ReaderError(f"{repo}: README 读不到")
            return self.readme
        key = f"{repo}/{relpath}"
        if key not in self.gomods:
            raise dep_gate.ReaderError(f"{repo}: origin/main 上读不到 {relpath}")
        return self.gomods[key]


# 夹具层序：MODULES 序（自底向上）与展示带（gen_skill.LAYERS 结构）对齐
ORDER_6 = ["pkg", "logx", "data", "middleware", "bootstrap", "hertz"]
BANDS_2 = [("上层", ["hertz", "middleware", "bootstrap"]),
           ("底层", ["pkg", "logx", "data"])]
NESTED_2 = [{"repo": "bootstrap", "path": "nacos", "module": "github.com/oekit/bootstrap/nacos",
             "desc": "夹具嵌套单元"}]

REAL_MODULES, REAL_NESTED = list(matrix.MODULES), list(matrix.NESTED_MODULES)


def use_fixture_matrix():
    matrix.MODULES[:] = ORDER_6
    matrix.NESTED_MODULES[:] = NESTED_2


def restore_real_matrix():
    matrix.MODULES[:] = REAL_MODULES
    matrix.NESTED_MODULES[:] = REAL_NESTED


def run_with(bands, fixture, waivers: dict | None = None):
    """build_units 与 run 都要看到夹具展示带与夹具豁免表（序位取 matrix.MODULES）。

    豁免默认清空（隔离真实矩阵的 2 条 data 豁免）；waivers 专测豁免三态。
    """
    saved_layers, saved_waivers = list(dep_gate.LAYERS), dict(dep_gate.WAIVERS)
    dep_gate.LAYERS[:] = bands
    dep_gate.WAIVERS.clear()
    dep_gate.WAIVERS.update(waivers or {})
    try:
        lines: list[str] = []
        rc = dep_gate.run(fixture.read, lines.append)
        return rc, lines
    finally:
        dep_gate.LAYERS[:] = saved_layers
        dep_gate.WAIVERS.clear()
        dep_gate.WAIVERS.update(saved_waivers)


def gomod(requires: list[str]) -> str:
    if not requires:
        return "module github.com/oekit/x\n\ngo 1.24\n"
    body = "\n".join(f"\t{r}" for r in requires)
    return f"module github.com/oekit/x\n\ngo 1.24\n\nrequire (\n{body}\n)\n"


def clean_fixture() -> dict[str, str]:
    """六根 + 一嵌套 + 伞仓，序位全部相容的干净基线（T14 复用作基线）。"""
    return {
        "hertz/go.mod": gomod(["github.com/oekit/middleware v1.0.0", "github.com/oekit/pkg v1.0.0"]),
        "middleware/go.mod": gomod(["github.com/oekit/pkg v1.0.0"]),
        "bootstrap/go.mod": gomod(["github.com/oekit/data v1.0.0"]),
        "bootstrap/nacos/go.mod": gomod(["github.com/oekit/pkg v1.0.0"]),
        "pkg/go.mod": gomod([]),
        "logx/go.mod": gomod([]),
        "data/go.mod": gomod([]),
        "oekit/go.mod": gomod(["github.com/oekit/hertz v1.0.0", "github.com/oekit/pkg v1.0.0"]),
    }


failures: list[str] = []


def case(name: str, cond: bool, detail: str = ""):
    tag = "✓" if cond else "❌"
    print(f"  {tag} {name}" + (f"——{detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    print("dep_gate 自测（合成夹具，零网络）")
    use_fixture_matrix()
    try:
        _t1_clean()
        _t2_order_violation()
        _t3_order_ok()
        _t4_cycle()
        _t5_self_loop()
        _t6_intra_repo()
        _t7_indirect()
        _t8_indirect_not_across_lines()
        _t9_pseudo_version()
        _t10_bom()
        _t11_unknown_path()
        _t12_commitment_missing()
        _t13_unreadable()
        _t14_mutation_exact()
        _t15_band_mismatch_report_only()
        _t16_waived_edge()
        _t17_stale_waiver()
        _t18_unwaived_new_edge()
    finally:
        restore_real_matrix()
    if failures:
        print(f"\n{len(failures)} 个用例失败：{failures}")
        return 1
    print("\n全部通过")
    return 0


def _t1_clean():
    rc, lines = run_with(BANDS_2, Fixture(clean_fixture()))
    case("T1 干净图放行", rc == dep_gate.EXIT_OK, "\n".join(lines))
    case("T1 边数 7（直接）", any("内部边 7（直接 7）" in ln for ln in lines),
         "\n".join(lines))


def _t2_order_violation():
    f = clean_fixture()
    f["pkg/go.mod"] = gomod(["github.com/oekit/hertz v1.0.0"])   # 序 0 require 序 5
    rc, lines = run_with(BANDS_2, Fixture(f))
    probs = [ln for ln in lines if "方向违例" in ln]
    case("T2 序位违例判红", rc == dep_gate.EXIT_VIOLATION, "\n".join(lines))
    case("T2 恰好 1 条方向违例", len(probs) == 1, str(probs))
    case("T2 点名两单元与序位", bool(probs) and "pkg" in probs[0]
         and "hertz" in probs[0] and "序 0" in probs[0] and "序 5" in probs[0], str(probs))


def _t3_order_ok():
    f = clean_fixture()
    f["data/go.mod"] = gomod(["github.com/oekit/logx v1.0.0"])   # 序 2 require 序 1
    rc, _ = run_with(BANDS_2, Fixture(f))
    case("T3 序位相容边放行", rc == dep_gate.EXIT_OK)


def _t4_cycle():
    f = clean_fixture()
    f["pkg/go.mod"] = gomod(["github.com/oekit/data v1.0.0"])
    f["data/go.mod"] = gomod(["github.com/oekit/pkg v1.0.0"])    # 底部互指 = 环
    rc, lines = run_with(BANDS_2, Fixture(f))
    case("T4 二元环判红", rc == dep_gate.EXIT_VIOLATION, "\n".join(lines))
    case("T4 环消息含两成员", any("环：" in ln and "pkg" in ln and "data" in ln
                                 for ln in lines), "\n".join(lines))


def _t5_self_loop():
    f = clean_fixture()
    f["pkg/go.mod"] = gomod(["github.com/oekit/pkg v1.0.0"])     # require 自己
    rc, lines = run_with(BANDS_2, Fixture(f))
    case("T5 自环判红", rc == dep_gate.EXIT_VIOLATION, "\n".join(lines))
    case("T5 报自环", any("自环" in ln and "pkg" in ln for ln in lines), "\n".join(lines))


def _t6_intra_repo():
    # 子 require 父 = 隔离设计的正方向（嵌套存在的前提），必须放行——
    # 首跑曾误报此方向，本用例是回归钉。
    f = clean_fixture()  # bootstrap/nacos → pkg + bootstrap → data，无父→子边
    rc, lines = run_with(BANDS_2, Fixture(f))
    case("T6 子 require 父放行（隔离正方向）", rc == dep_gate.EXIT_OK, "\n".join(lines))
    f0 = clean_fixture()
    f0["bootstrap/nacos/go.mod"] = gomod(["github.com/oekit/bootstrap v1.0.0"])  # 子→父
    rc0, lines0 = run_with(BANDS_2, Fixture(f0))
    case("T6 子→父显式边放行", rc0 == dep_gate.EXIT_OK, "\n".join(lines0))
    # 父→己嵌套 = 抵消隔离，判红
    f1 = clean_fixture()
    f1["bootstrap/go.mod"] = gomod(["github.com/oekit/bootstrap/nacos v1.0.0"])
    rc1, lines1 = run_with(BANDS_2, Fixture(f1))
    case("T6 父 require 己嵌套判红（同仓序位）",
         rc1 == dep_gate.EXIT_VIOLATION
         and any("同仓序位" in ln and "bootstrap/nacos" in ln for ln in lines1),
         "\n".join(lines1))
    # 父↔嵌套双向 = 环
    f2 = clean_fixture()
    f2["bootstrap/go.mod"] = gomod(["github.com/oekit/bootstrap/nacos v1.0.0"])
    f2["bootstrap/nacos/go.mod"] = gomod(["github.com/oekit/bootstrap v1.0.0"])
    rc2, lines2 = run_with(BANDS_2, Fixture(f2))
    case("T6 父↔嵌套双向判环",
         rc2 == dep_gate.EXIT_VIOLATION
         and any("环：" in ln and "bootstrap/nacos" in ln for ln in lines2),
         "\n".join(lines2))


def _t7_indirect():
    f = clean_fixture()
    f["logx/go.mod"] = gomod(["github.com/oekit/hertz v1.0.0 // indirect"])
    rc, lines = run_with(BANDS_2, Fixture(f))
    case("T7 indirect 序位违例不判（方向与 ⚠ 都跳过）", rc == dep_gate.EXIT_OK
         and not any("方向违例" in ln or "不一致" in ln for ln in lines), "\n".join(lines))
    # indirect 边参与成环：logx→pkg(indirect) + pkg→logx(direct) = 环
    f2 = clean_fixture()
    f2["logx/go.mod"] = gomod(["github.com/oekit/pkg v1.0.0 // indirect"])
    f2["pkg/go.mod"] = gomod(["github.com/oekit/logx v1.0.0"])
    rc2, lines2 = run_with(BANDS_2, Fixture(f2))
    case("T7 indirect 边参与环检测", rc2 == dep_gate.EXIT_VIOLATION
         and any("环：" in ln and "logx" in ln for ln in lines2), "\n".join(lines2))


def _t8_indirect_not_across_lines():
    f = clean_fixture()
    # T8 坑回归：direct 行的**下一行**是注释行时，行尾判定不许把它当成
    # 本行的 indirect 标注（正则钉行尾的动机）。
    f["logx/go.mod"] = ("module github.com/oekit/logx\n\ngo 1.24\n\nrequire (\n"
                        "\tgithub.com/oekit/hertz v1.0.0\n"
                        "\t// hertz 在序位之上：这条 direct 违例必须被抓\n"
                        ")\n")
    rc, lines = run_with(BANDS_2, Fixture(f))
    case("T8 注释下一行不吞方向违例", rc == dep_gate.EXIT_VIOLATION
         and any("方向违例" in ln and "logx" in ln for ln in lines), "\n".join(lines))


def _t9_pseudo_version():
    f = clean_fixture()
    f["middleware/go.mod"] = gomod(
        ["github.com/oekit/pkg v1.0.0",
         "github.com/oekit/data v0.14.3-0.20261103123456-abcdef123456"])  # 序 3 → 序 2（#306 形态）
    rc, lines = run_with(BANDS_2, Fixture(f))
    case("T9 伪版本行照常出边", rc == dep_gate.EXIT_OK
         and any("内部边 8（直接 8）" in ln for ln in lines), "\n".join(lines))


def _t10_bom():
    rc, _ = run_with(BANDS_2, Fixture(clean_fixture()))
    case("T10 BOM require 全体放行（序位最顶）", rc == dep_gate.EXIT_OK)
    f = clean_fixture()
    f["pkg/go.mod"] = gomod(["github.com/oekit/oekit v0.1.11"])  # 模块依赖伞仓
    rc2, lines2 = run_with(BANDS_2, Fixture(f))
    case("T10 模块 require 伞仓判序位违例",
         rc2 == dep_gate.EXIT_VIOLATION
         and any("方向违例" in ln and "pkg" in ln and "oekit" in ln for ln in lines2),
         "\n".join(lines2))
    case("T10 伞仓不出 ⚠ 对账行（没有展示带）",
         not any("不一致" in ln and "oekit" in ln for ln in lines2), "\n".join(lines2))


def _t11_unknown_path():
    f = clean_fixture()
    f["hertz/go.mod"] = gomod(["github.com/oekit/removed-repo v1.0.0"])
    rc, lines = run_with(BANDS_2, Fixture(f))
    case("T11 未知 oekit 路径判红", rc == dep_gate.EXIT_VIOLATION, "\n".join(lines))
    case("T11 点名已删除仓", any("removed-repo" in ln for ln in lines), "\n".join(lines))


def _t12_commitment_missing():
    rc, lines = run_with(BANDS_2, Fixture(clean_fixture(),
                                          readme="依赖随便长：无承诺句。"))
    case("T12 承诺句缺失判红", rc == dep_gate.EXIT_VIOLATION, "\n".join(lines))
    case("T12 消息指向承诺漂移", any("承诺" in ln for ln in lines), "\n".join(lines))


def _t13_unreadable():
    lines2: list[str] = []
    saved = list(dep_gate.LAYERS)
    dep_gate.LAYERS[:] = BANDS_2
    try:
        rc2 = dep_gate.run(Fixture({}).read, lines2.append)
    finally:
        dep_gate.LAYERS[:] = saved
    case("T13 全读不到 → EXIT_UNREADABLE", rc2 == dep_gate.EXIT_UNREADABLE,
         "\n".join(lines2))
    case("T13 消息点名判不了", any("判不了" in ln for ln in lines2), "\n".join(lines2))


def _t14_mutation_exact():
    """干净基线注入一条序位违例 ⇒ 违例集合恰好多那一条（变异自证：精确转红）。"""
    rc_clean, _ = run_with(BANDS_2, Fixture(clean_fixture()))
    mutated = clean_fixture()
    mutated["data/go.mod"] = gomod(["github.com/oekit/hertz v1.0.0"])  # 序 2 → 序 5
    rc_mut, lines_mut = run_with(BANDS_2, Fixture(mutated))
    probs = [ln for ln in lines_mut if "❌" in ln]
    case("T14 干净基线放行", rc_clean == dep_gate.EXIT_OK)
    case("T14 注入单边后判红", rc_mut == dep_gate.EXIT_VIOLATION)
    case("T14 转红恰好 1 条且命中", len(probs) == 1 and "data" in probs[0]
         and "hertz" in probs[0], str(probs))


def _t15_band_mismatch_report_only():
    """带与序错位：带表把 logx 抬进上层带，而序位里 logx(1) 在 pkg(0) 之上——
    logx→pkg 序位相容（不红），但带表说 pkg 在 logx 之下 ⇒ ⚠ 对账行必须点名
    （2026-11-27 首跑实录的 authx→logx 同构：序位说合法、展示带说不一致）。"""
    bands_misaligned = [("上层", ["hertz", "middleware", "bootstrap", "logx"]),
                        ("底层", ["pkg", "data"])]
    f = clean_fixture()
    f["data/go.mod"] = gomod(["github.com/oekit/logx v1.0.0"])  # 序 2 → 序 1 相容；带表底层→上层
    rc, lines = run_with(bands_misaligned, Fixture(f))
    case("T15 带错位序位相容 → 放行", rc == dep_gate.EXIT_OK, "\n".join(lines))
    case("T15 ⚠ 对账行点名两边与两条带",
         any("⚠" in ln and "展示分层与依赖方向不一致" in ln
             and "data" in ln and "logx" in ln and "底层" in ln and "上层" in ln
             for ln in lines), "\n".join(lines))


def _t16_waived_edge():
    """登记内违例 → 放行 + ⚠ 报告行带理由（豁免可见，不许静默）。
    选 data→hertz 避免成环（豁免只作用于方向判据，环照红——见 docstring）。"""
    f = clean_fixture()
    f["data/go.mod"] = gomod(["github.com/oekit/hertz v1.0.0"])  # 序 2 → 序 5 违例（不成环）
    rc, lines = run_with(BANDS_2, Fixture(f),
                         waivers={("data", "hertz"): "夹具豁免：清偿单 #999"})
    case("T16 登记内违例放行", rc == dep_gate.EXIT_OK, "\n".join(lines))
    case("T16 ⚠ 报告行带原由与单号",
         any("已登记豁免" in ln and "data" in ln and "hertz" in ln
             and "夹具豁免：清偿单 #999" in ln for ln in lines), "\n".join(lines))


def _t17_stale_waiver():
    """已登记的边消失 → 豁免失配判红（催删，豁免不是永久身份）。"""
    rc, lines = run_with(BANDS_2, Fixture(clean_fixture()),
                         waivers={("data", "hertz"): "边已不存在的陈年豁免"})
    case("T17 豁免失配判红", rc == dep_gate.EXIT_VIOLATION, "\n".join(lines))
    case("T17 消息催删豁免",
         any("豁免失配" in ln and "data" in ln and "hertz" in ln and "删掉" in ln
             for ln in lines), "\n".join(lines))


def _t18_unwaived_new_edge():
    """登记外新违例照红——豁免不许变成后门（与 T2 同边但带上无关豁免）。"""
    f = clean_fixture()
    f["pkg/go.mod"] = gomod(["github.com/oekit/hertz v1.0.0"])
    rc, lines = run_with(BANDS_2, Fixture(f),
                         waivers={("data", "middleware"): "毫不相干的豁免"})
    case("T18 登记外违例照红", rc == dep_gate.EXIT_VIOLATION, "\n".join(lines))
    case("T18 无豁免报告行", not any("已登记豁免" in ln for ln in lines),
         "\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
