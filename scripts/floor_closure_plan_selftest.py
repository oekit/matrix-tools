#!/usr/bin/env python3
"""floor_closure_plan.py 的自测（matrix-tools#242）。

**这条判据防的是什么**：闭合规划器的「④ 模拟」此前读**旧 tag 的产物** ⇒
本轮要发的模块被算成「自己落后于自己」⇒ 残留数 100% 虚高（实测 133 条残留
全部落在这类）；且旧实现没建模「层间重抬地板」⇒ 按它的扁平拓扑序照发必然
留下「新产物带旧地板」的一批——工具承诺的「收敛到 0」兑现不了。

断言（A→B→C 三仓夹具，B/C 的已发布产物地板落后一层；全部可判红）：
  1. plan 判定：三个单元都因「已发布产物地板落后」入轮次；
  2. ③ 拓扑序：被依赖者先发（A→B→C）；同仓嵌套同版本号不撞键；
  3. ④ 数据源（本单核心）：**plan 内单元的「本轮产物」读 main 工作树**
     ⇒ 按「发 + 层间重抬」走完残留 = 0；
  4. 反证成对断言：同一夹具**不重抬**（--no-relift 口径）⇒ 残留 > 0
     ——证明「层间重抬」是收敛的必要条件，不是文档里的一句提醒；
  5. 变异自证（#242 缺陷①）：把模拟数据源换回**旧 tag 产物**（worktree
     口径换成 artifact 口径）⇒ 残留 > 0 ⇒ 证明「读新树」这个口径修正
     真的有牙齿（退回旧口径时本自测必红）；
  6. 临时落后单独计数：本轮发布制造的窗口（B 发 v1.0.1 后 C 要重抬）
     ≠ 残留。

**无副作用、不联网**：全内存夹具（unit/inv/worktree 表），不建仓不打 API。

用法：python3 scripts/floor_closure_plan_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from floor_closure_plan import (Unit, build_plan, simulate, semver,  # noqa: E402
                                topo_layers, topo_order)

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def unit(path: str, repo: str, sub: str | None = None, bom: bool = False) -> Unit:
    return Unit(path=path, repo=repo, sub=sub,
                label=path.removeprefix("github.com/oekit/") + ("(BOM)" if bom else ""),
                is_bom=bom)


def build_fixture() -> tuple[dict, dict, list[str]]:
    """B→C 级联夹具（A 是已发到最新的底层依赖）：

    - A：latest **v1.0.1**（远端最新已发布），产物无 oekit 依赖 ⇒ 本轮不发；
    - B：latest v1.0.0，产物 require A v1.0.0（落后 A 的 v1.0.1）⇒ 入轮次；
      工作树已抬到 A v1.0.1（=「新 tag 的树」）；
    - C：latest v1.0.0，产物 require B v0.9.0（落后）⇒ 入轮次；
      工作树 require B v1.0.0（抬过 B 的已发布最新，但**没抬到 B 本轮将发的
      v1.0.1**——正是「层间重抬」要补的那一刀）。
    """
    A = "github.com/oekit/a"
    B = "github.com/oekit/b"
    C = "github.com/oekit/c"
    inv = {
        A: {"unit": unit(A, "a"), "latest": "v1.0.1",
            "artifact_floors": []},
        B: {"unit": unit(B, "b"), "latest": "v1.0.0",
            "artifact_floors": [(A, "v1.0.0")]},
        C: {"unit": unit(C, "c"), "latest": "v1.0.0",
            "artifact_floors": [(B, "v0.9.0")]},
    }
    worktree = {
        A: [],
        B: [(A, "v1.0.1")],   # main 已抬到 A 远端最新
        C: [(B, "v1.0.0")],   # 抬过 B 的已发布最新，没抬到本轮将发的 v1.0.1
    }
    return inv, worktree, [B, C]


def case_plan_and_order() -> None:
    inv, _wt, keys = build_fixture()
    plan = build_plan(inv)
    check(sorted(plan) == sorted(keys),
          f"用例1 B/C 都应入轮次（产物地板落后）；A 不入（已最新）：plan={sorted(plan)}")
    order = topo_order(plan, inv)
    check(order == keys, f"用例2 拓扑序应 B→C（被依赖者先发）：{order}")
    layers = topo_layers(order, inv)
    check(layers[keys[0]] == 0 and layers[keys[1]] == 1,
          f"用例2 分层应 0/1：{layers}")


def case_same_version_no_collision() -> None:
    """同仓嵌套单元同版本号不撞键（键 = 完整模块路径，#250 同族教训）。"""
    reg_nacos = "github.com/oekit/registry/nacos"
    reg_impl = "github.com/oekit/registry/nacosimpl"
    reg = "github.com/oekit/registry"
    inv = {
        reg: {"unit": unit(reg, "registry"), "latest": "v0.5.0", "artifact_floors": []},
        reg_nacos: {"unit": unit(reg_nacos, "registry", "nacos"), "latest": "v0.1.0",
                    "artifact_floors": [(reg, "v0.4.9")]},
        reg_impl: {"unit": unit(reg_impl, "registry", "nacosimpl"), "latest": "v0.1.0",
                   "artifact_floors": [(reg, "v0.4.9")]},
    }
    plan = build_plan(inv)
    check(sorted(plan) == sorted([reg_nacos, reg_impl]),
          f"同仓同号嵌套单元应各自入轮次（不撞键）：{sorted(plan)}")
    order = topo_order(plan, inv)
    check(sorted(order) == sorted([reg_nacos, reg_impl]),
          f"轮次序只含两个嵌套单元（父仓已是最新，不入轮次）：{order}")


def case_simulation_converges() -> None:
    """④ 正口径：读工作树 + 层间重抬 ⇒ 残留 0；临时落后单独计。"""
    inv, wt, keys = build_fixture()
    sim = simulate(keys, inv, keys, wt, re_lift=True)
    check(sim["residual"] == 0,
          f"用例3 按建议流程走完应零残留（#242 的承诺）：{sim['residual_detail']}")
    check(sim["temporary_lag"] == 1,
          f"用例6 临时落后应恰 1 条（C 重抬 B v1.0.0→v1.0.1，B 是本轮发的；"
          f"B 对 A 的抬动是第一轮 align 的存量，不算）：{sim['temporary_lag']}")


def case_no_relift_diverges() -> None:
    """反证：不建模层间重抬 ⇒ 残留 > 0（收敛性依赖这一步，不是文案）。"""
    inv, wt, keys = build_fixture()
    sim = simulate(keys, inv, keys, wt, re_lift=False)
    check(sim["residual"] > 0,
          "用例4 不重抬必须残留 > 0（否则「层间重抬」的必要性没被钉住）")
    check(any("c" in d and "b" in d for d in sim["residual_detail"]),
          f"用例4 残留明细应落在 C 的 B 地板上：{sim['residual_detail'][:3]}")


def case_old_source_diverges() -> None:
    """变异自证（#242 缺陷①）：数据源换回旧 tag 产物 ⇒ 残留数虚高。

    旧口径把「本轮产物」读成已发布 tag 的 go.mod ⇒ 连「本轮自己要发的模块」
    的旧地板也被算成残留 ⇒ 100% 虚高（实测 133 vs 104）。本断言钉死：同一
    夹具下旧口径的残留**严格多于**新口径——口径退化可被自测看见。
    """
    inv, wt, keys = build_fixture()
    old_source = {k: inv[k]["artifact_floors"] for k in keys}   # ← 旧口径的取数
    sim_old = simulate(keys, inv, keys, old_source, re_lift=False)
    sim_new = simulate(keys, inv, keys, wt, re_lift=False)
    check(sim_old["residual"] > sim_new["residual"],
          f"用例5 旧口径残留（{sim_old['residual']}）必须严格大于新口径"
          f"（{sim_new['residual']}）——否则「④ 读新树」退化时自测抓不住")


def main() -> None:
    case_plan_and_order()
    case_same_version_no_collision()
    case_simulation_converges()
    case_no_relift_diverges()
    case_old_source_diverges()
    if FAILURES:
        print("✗ 自测失败：")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("✓ 闭合规划器自测通过：plan/拓扑序/同号不撞键判定正确；"
          "④ 读工作树 + 层间重抬 ⇒ 残留 0；不重抬或读旧产物 ⇒ 残留 > 0（成对反证）")


if __name__ == "__main__":
    main()
