#!/usr/bin/env python3
"""闭合规划器：这一轮该发哪几个号、按什么顺序发，才能把地板漂移收敛掉。

**从 `.oekit-cron-state/floor_closure_plan.py` 迁入 matrix-tools/scripts/**
（matrix-tools#242 建议修法 4）：原脚本位于非 git 目录，无版本、无自测、
无回归保护；本轮实测出的口径错误（133 ⇒ 104）无处落盘。

**两个口径（#242 修复的核心）**：

1. **④ 模拟读「新树」，不读旧产物**。旧实现按 `read_gomod_at(latest_tag)` 读
   **本轮发布前**的已发布 tag 产物 ⇒ 本轮要发的模块会被算成「自己落后于自己」
   ⇒ 残留数 100% 虚高（实测 29 个待发单元时 133 条残留全部落在这类身上）。
   正确口径：**plan 内单元的「本轮产物」= main 工作树**（新 tag 打在 main 上，
   工作树 = 本轮重抬后的地板）。
2. **显式建模「层间重抬地板」**。发 X ⇒ 任何 require X 旧版的已发布产物当场
   变旧 ⇒ 下游必须在**发版前**重抬一次地板再发。旧实现只给扁平拓扑序、
   没有重抬这一步 ⇒ 按它照发必然留下「新产物带旧地板」的一批。模拟里体现为：
   发布 X 后，所有**尚未发布**的 plan 单元的地板抬到当前 sim_latest；
   `--no-relift` 可退回「不重抬」口径（残留必须 > 0 ⇒ 证明重抬是收敛的
   必要条件，selftest 钉死这对断言）。

**输出口径（#242 建议修法 3）**：
  - ④ 残留 = 「按建议流程（按序发 + 层间重抬）走完之后仍残留的条数」——
    应当为 **0**；不为 0 说明依赖图或判据有问题（fail-loud）；
  - 「本轮会制造多少条**临时**落后」单独输出（发 X 到重抬下游之间的窗口）；
  - 分层重抬清单：第 N 层发完后、第 N+1 层发版前，要先对哪些仓 align 哪些依赖。

按需发版模型下，「本轮不发」的单元旧地板**不是**残留（pull-based：谁用新
API 谁升级）——它们单独列在「未入轮次」提示里，不进残留计数。

用法：
  env MATRIX_BASE=/path/to/oekit python3 scripts/floor_closure_plan.py [--dry]
  python3 scripts/floor_closure_plan.py --base /path/to/oekit --repos logx,registry
  python3 scripts/floor_closure_plan.py --no-relift   # 反证口径：不建模层间重抬

只读工具（--dry 仅为兼容旧用法保留）；退出码：0 正常；1 按建议流程走完仍有
残留（判据承诺被打破）；2 读不到远端 tag / 本地检出（判不了 ≠ 零残留，#79）。

自测：scripts/floor_closure_plan_selftest.py（A→B→C 三仓夹具，不联网）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import BOM_REPO, MODULES, ORG, RemoteReadError, bump_patch, repo_dir, remote_tags  # noqa: E402
from release_train import parse_go_mod_requires  # noqa: E402  （require 解析只此一份）

EXIT_OK, EXIT_RESIDUAL, EXIT_UNREADABLE = 0, 1, 2


class Unit(NamedTuple):
    """一个发版单元（根模块 / 嵌套模块 / 伞仓 BOM）。"""

    path: str            # 模块 import 路径（dep 匹配键；同仓嵌套同号不撞键，#242）
    repo: str            # 宿主仓名
    sub: str | None      # 嵌套子路径（tag 前缀）；根模块 / BOM 为 None
    label: str           # 展示名（如 logx、logx/sink、oekit(BOM)）
    is_bom: bool = False


def semver(v: str) -> tuple[int, int, int]:
    m = re.match(r"v(\d+)\.(\d+)\.(\d+)$", v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


# ---------- 事实读取（可注入：selftest 全内存替身，不联网不建仓） ----------

def latest_of_unit(unit: Unit, tags_fn) -> str | None:
    """该单元远端最新已发布版本号（前缀过滤 + **按数字**比；无 tag ⇒ None）。"""
    tags = tags_fn(unit.repo)
    prefix = f"{unit.sub}/" if unit.sub else ""
    best: tuple[tuple[int, int, int], str] | None = None
    for t in tags:
        if not t.startswith(prefix):
            continue
        ver = t[len(prefix):]
        if not re.fullmatch(r"v\d+\.\d+\.\d+", ver):
            continue
        k = semver(ver)
        if best is None or k > best[0]:
            best = (k, ver)
    return best[1] if best else None


def artifact_gomod(d: Path, tag: str, sub: str | None, read_at=None) -> str | None:
    """**已发布产物**的 go.mod（git show tag:...，tag 快照是唯一真相）。"""
    if read_at is not None:
        return read_at(d, tag, sub)
    rel = f"{sub}/go.mod" if sub else "go.mod"
    r = subprocess.run(["git", "-C", str(d), "show", f"{tag}:{rel}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def worktree_gomod(d: Path, sub: str | None) -> str | None:
    """**main 工作树**的 go.mod（本轮 plan 单元的「新 tag 的树」，#242 口径）。"""
    p = (d / sub / "go.mod") if sub else (d / "go.mod")
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def requires_of(text: str | None) -> list[tuple[str, str]]:
    """go.mod 文本里 github.com/oekit/* 的 require（解析只此一份：复用
    release_train.parse_go_mod_requires，排除 replace 块）。"""
    if not text:
        return []
    return [(p, v) for p, v in parse_go_mod_requires(text)
            if p.startswith("github.com/oekit/")]


# ---------- 规划 ----------

def build_plan(inv: dict[str, dict]) -> list[str]:
    """本轮要发的单元：**已发布产物**里有地板落后于该依赖最新版的（含 BOM——
    它钉全部模块，只要本轮有任何别的单元要发，BOM 的旧快照就是落后）。

    判据只看**已发布产物**（latest tag 的 go.mod），不看工作树——「产物带旧
    地板」才是发版要修的东西；工作树已抬而未发版的，恰好说明该单元该发。
    """
    plan: list[str] = []
    for key, u in inv.items():
        if u["latest"] is None:
            continue          # 从未发版的单元：没有「已发布产物」可修
        stale = any(dep in inv and inv[dep]["latest"]
                    and semver(ver) < semver(inv[dep]["latest"])
                    for dep, ver in u["artifact_floors"])
        if stale:
            plan.append(key)
    return plan


def topo_order(plan: list[str], inv: dict[str, dict]) -> list[str]:
    """建议发号顺序：拓扑序（被依赖者先发），Kahn 算法、同层按 key 排序
    （输出确定）。成环 ⇒ 抛 ValueError（环内没有合法发版顺序，必须显式失败）。"""
    plan_set = set(plan)
    deps_of = {k: set() for k in plan}        # k 依赖的 plan 内单元
    dependents = {k: set() for k in plan}     # 依赖 k 的 plan 内单元
    for k in plan:
        for dep, _v in inv[k]["artifact_floors"]:
            if dep in plan_set and dep != k:
                deps_of[k].add(dep)
                dependents[dep].add(k)
    ready = sorted(k for k in plan if not deps_of[k])
    order: list[str] = []
    while ready:
        k = ready.pop(0)
        order.append(k)
        for d2 in sorted(dependents[k]):
            deps_of[d2].discard(k)
            if not deps_of[d2] and d2 not in order and d2 not in ready:
                ready.append(d2)
        ready.sort()
    if len(order) != len(plan):
        stuck = sorted(set(plan) - set(order))
        raise ValueError(f"依赖图成环，发不出拓扑序：{stuck}")
    return order


def topo_layers(order: list[str], inv: dict[str, dict]) -> dict[str, int]:
    """每个单元的拓扑深度（0 起）——分层重抬清单的分层依据。"""
    layer = {k: 0 for k in order}
    pos = {k: i for i, k in enumerate(order)}
    for k in order:
        for dep, _v in inv[k]["artifact_floors"]:
            if dep in pos and dep != k:
                layer[k] = max(layer[k], layer[dep] + 1)
    return layer


def simulate(order: list[str], inv: dict[str, dict], plan: list[str],
             worktree_floors: dict[str, list[tuple[str, str]]],
             re_lift: bool = True) -> dict:
    """按 ③ 的顺序模拟本轮发版，返回 {residual, temporary_lag, residual_detail}。

    - ``worktree_floors``：plan 内单元的**本轮产物**（main 工作树的地板，#242
      的口径修正——不再读旧 tag 的产物）；
    - ``re_lift``：发布每个单元后，把它**尚未发布**的下游（plan 内）的地板抬到
      当前 sim_latest（= 发版前重抬）。False ⇒ 退回「不重抬」口径，用于证明
      「不重抬 ⇒ 残留 > 0」（selftest 的成对断言）。

    残留只数**本轮 plan 内**单元的最终产物对 sim_latest 的落后；plan 外单元
    不发版，其旧地板是按需发版模型的正常状态（pull-based），不进残留。
    """
    sim_latest: dict[str, str] = {}
    for key, u in inv.items():
        if u["latest"]:
            sim_latest[key] = u["latest"]
    # 模拟期间「本轮产物」的地板（可被重抬改写）
    floors = {k: list(worktree_floors.get(k, inv[k]["artifact_floors"])) for k in plan}
    temporary_lag = 0
    published_this_round: set[str] = set()
    for k in order:
        # ① 发版前重抬：本单元地板抬到当前 sim_latest。临时落后只数**本轮**
        #    发布造成的抬动（dep 已在本轮先发）——工作树落后于「本轮根本没发
        #    版的依赖」是第一轮 align 的存量，不算本轮制造的窗口。
        if re_lift:
            for i, (dep, ver) in enumerate(floors[k]):
                cur = sim_latest.get(dep)
                if cur and semver(ver) < semver(cur):
                    floors[k][i] = (dep, cur)
                    if dep in published_this_round:
                        temporary_lag += 1
        # ② 发布
        latest = inv[k]["latest"]
        if latest:
            sim_latest[k] = bump_patch(latest)
        published_this_round.add(k)
    # ③ 残留：plan 内单元最终产物 vs 最终 sim_latest（按建议流程应为 0）
    residual_detail: list[str] = []
    for k in plan:
        for dep, ver in floors[k]:
            cur = sim_latest.get(dep)
            if cur and semver(ver) < semver(cur):
                residual_detail.append(
                    f"{inv[k]['unit'].label} require {dep} {ver} ≠ 最新 {cur}")
    return {"residual": len(residual_detail),
            "residual_detail": residual_detail,
            "temporary_lag": temporary_lag}


# ---------- 主流程 ----------

def discover_units(base: str) -> list[Unit]:
    """全矩阵发版单元：根模块 + 嵌套模块（从仓内嵌套 go.mod 推导，文件系统
    事实，不依赖登记表）+ 伞仓 BOM（排最后——它钉全部模块，天然是最后一层）。"""
    units: list[Unit] = []
    for repo in MODULES:
        units.append(Unit(path=f"github.com/{ORG}/{repo}", repo=repo, sub=None, label=repo))
        d = repo_dir(repo, base)
        for gm in sorted(d.glob("**/go.mod")):
            if gm.parent == d:
                continue
            parts = gm.relative_to(d).parts
            if any(p in (".git", "vendor", "node_modules", "testdata") for p in parts):
                continue
            try:
                first = gm.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            except (OSError, IndexError):
                continue
            m = re.match(r"module\s+github\.com/oekit/(.+?)\s*$", first.strip())
            if m:
                sub = m.group(1)
                units.append(Unit(path=f"github.com/{ORG}/{repo}/{sub}", repo=repo,
                                  sub=sub, label=f"{repo}/{sub}"))
    units.append(Unit(path=f"github.com/{ORG}/{BOM_REPO}", repo=BOM_REPO, sub=None,
                      label=f"{BOM_REPO}(BOM)", is_bom=True))
    return units


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="地板闭合规划器（只读；④ 模拟按 #242 口径）")
    ap.add_argument("--dry", action="store_true", help="兼容旧用法：本工具本来就是只读")
    ap.add_argument("--base", default=os.environ.get("MATRIX_BASE")
                    or str(Path(__file__).resolve().parent.parent.parent),
                    help="矩阵检出根目录（默认取 MATRIX_BASE，其次本仓上级）")
    ap.add_argument("--repos", default="", help="只规划这些宿主仓（逗号分隔；缺省全矩阵）")
    ap.add_argument("--no-relift", action="store_true",
                    help="反证口径：不建模「层间重抬」（模拟结果必然残留 > 0，用于对照）")
    ns = ap.parse_args(argv)
    base = os.path.abspath(ns.base)
    only_repos = {r for r in ns.repos.split(",") if r}

    try:
        units = discover_units(base)
        if only_repos:
            units = [u for u in units if u.repo in only_repos or u.is_bom]
        from matrix import enable_tag_cache
        enable_tag_cache()
        tags_fn = lambda repo: remote_tags(repo, base)  # noqa: E731
        inv: dict[str, dict] = {}
        for u in units:
            d = repo_dir(u.repo, base)
            latest = latest_of_unit(u, tags_fn)
            tag = latest
            if latest and u.sub:
                tag = f"{u.sub}/{latest}"
            floors = requires_of(artifact_gomod(d, tag or "", u.sub) or "")
            inv[u.path] = {"unit": u, "latest": latest, "artifact_floors": floors,
                           "dir": d}
    except RemoteReadError as e:
        print(f"❌ 读不到远端 tag ⇒ 无法规划（**不是「零残留」**）：{e}", file=sys.stderr)
        return EXIT_UNREADABLE
    except FileNotFoundError as e:
        print(f"❌ 读不到本地检出 ⇒ 无法规划（判不了 ≠ 零残留，#79）：{e}", file=sys.stderr)
        return EXIT_UNREADABLE

    # 盘点
    print(f"闭合规划（{len(inv)} 个发版单元；检出根 {base}；"
          f"重抬建模：{'开' if not ns.no_relift else '关（反证口径）'}）")
    for key in sorted(inv):
        u = inv[key]
        if u["latest"] is None:
            print(f"  · {u['unit'].label}: 从未发版（跳过）")

    plan = build_plan(inv)
    if not plan:
        print("\n✓ 全部已发布产物的地板都是最新的——本轮无需发版（零残留）")
        return EXIT_OK
    print(f"\n③ 本轮待发 {len(plan)} 个单元（已发布产物地板落后）：")
    for key in plan:
        print(f"  - {inv[key]['unit'].label}  下一版 {bump_patch(inv[key]['latest'])}")

    try:
        order = topo_order(plan, inv)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return EXIT_RESIDUAL
    # BOM 永远排最后：它钉全部模块，只有别的都发完才知道它该钉什么
    bom_key = next((k for k in plan if inv[k]["unit"].is_bom), None)
    if bom_key:
        order = [k for k in order if k != bom_key] + [bom_key]
    print("\n③ 建议发号顺序（拓扑序：被依赖者先发；BOM 最后）：")
    for i, key in enumerate(order, 1):
        print(f"  {i}. {inv[key]['unit'].label} → {bump_patch(inv[key]['latest'])}")

    # ④ 模拟——**plan 内单元读 main 工作树**（新 tag 的树），plan 外读旧产物
    worktree_floors: dict[str, list[tuple[str, str]]] = {}
    for key in plan:
        u = inv[key]["unit"]
        d = repo_dir(u.repo, base)
        wt = requires_of(worktree_gomod(d, u.sub))
        worktree_floors[key] = wt
    sim = simulate(order, inv, plan, worktree_floors, re_lift=not ns.no_relift)
    print(f"\n④ 模拟（按建议顺序发版 + 层间重抬）：")
    print(f"  临时落后：本轮会制造 {sim['temporary_lag']} 条（发 X ⇒ 下游旧一档 ⇒ "
          f"重抬之前那段窗口——这不是债，流程走完即消）")
    print(f"  残留：{sim['residual']} 条（按建议流程走完仍残留的条数，应为 0；"
          f"plan 外单元的旧地板是按需发版的正常状态，不计入）")
    for line in sim["residual_detail"][:12]:
        print(f"    - {line}")
    if len(sim["residual_detail"]) > 12:
        print(f"    - …… 另 {len(sim['residual_detail']) - 12} 条同类")

    # ⑤ 分层重抬清单：第 N 层发完后、第 N+1 层发版前，先 align 下游
    layer = topo_layers(order, inv)
    by_layer: dict[int, list[str]] = {}
    for k in order:
        by_layer.setdefault(layer[k], []).append(k)
    print("\n⑤ 分层重抬清单（每层发完后、下一层发版前，先跑："
          "python3 scripts/align_floors.py --apply --pr --repos <下游仓>）：")
    prev_units: list[str] = []
    for depth in sorted(by_layer):
        if prev_units:
            prev_repos = {inv[p]["unit"].repo for p in prev_units}
            deps_needed = sorted({dep for k in by_layer[depth]
                                  for dep, _v in inv[k]["artifact_floors"]
                                  if dep in inv and inv[dep]["unit"].repo in prev_repos})
            repos_needed = sorted({inv[dep]["unit"].repo for dep in deps_needed})
            print(f"  第 {depth} 层（{'、'.join(inv[k]['unit'].label for k in sorted(by_layer[depth]))}）"
                  f"发版前：对 {('、'.join(repos_needed)) or '（无）'} 重抬 "
                  f"{('、'.join(inv[dep]['unit'].label for dep in deps_needed)) or '（无新增）'}")
        else:
            print(f"  第 0 层（{'、'.join(inv[k]['unit'].label for k in sorted(by_layer[depth]))}）："
                  f"发版前用第一轮 align 的结果即可")
        prev_units.extend(by_layer[depth])

    if sim["residual"]:
        print(f"\n❌ 按建议流程走完仍有 {sim['residual']} 条残留——判据承诺（收敛到 0）"
              f"被打破，上面的明细就是缺口", file=sys.stderr)
        return EXIT_RESIDUAL
    print("\n✓ 按建议顺序发 + 层间重抬 ⇒ 残留 0（收敛）")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
