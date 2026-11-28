#!/usr/bin/env python3
"""分层无环门禁——把「依赖严格自底向上：上层依赖下层，全矩阵无环」这句公开承诺
从**零执行承诺**变成可复算事实。

背景（为什么 2026-11-27 才有了这道门禁）：
  oekit 主仓 README 公开承诺方向与无环，但矩阵工具链里没有任何脚本真的从
  go.mod 推依赖边验方向或检环：audit 的 LAYERS 检查只防展示分层死键；
  gen_diagram 只画图不校验图与事实一致。「全矩阵无环」一直是零执行承诺
  （2026-11-27 演进评审 P1 定性：最大隐藏缺口）。

**首跑实录（2026-11-27，本门禁自己挖出来的）**：仓里存在**三套层序口径**——
  a. matrix.py MODULES 清单序：注释自 declares「依赖自底向上的顺序」（唯一事实源）；
  b. gen_skill.LAYERS / gen_diagram.LAYERS 展示五层（成员集合一致）：应用引导 →
     通信·服务治理 → 数据访问 → 可观测性 → 基础库；
  c. 真实 go.mod 依赖边（167 条，首跑实测）。
  b 与 c 有 2 处不一致（authx→logx：基础库 require 可观测性；etcdx→registry：
  数据访问 require 服务治理），而 a 与 c **零矛盾**。结论：方向判据的规范源取
  **a（MODULES 序）**——它是唯一事实源里自 declares 的依赖序，也是编译秩序的
  真话；b 是展示投影，它与 c 的对账**降级为 ⚠ 报告行**（不阻塞）——展示分层
  要不要重排（比如 logx 归基础库带）是一次独立的架构裁决，不该由门禁单方面
  把裁决结果写成红灯。先例：audit 反向地板 ⚠「可见不阻塞」。

判据（每条都锚在公开承诺/唯一事实上，不锚在我的推测上）：
  1. **承诺在场**：oekit 主仓 README（origin/main）必须仍有「自底向上 + 无环」
     承诺句。承诺被改掉/删掉时门禁红——承诺与门禁必须同进退。
  2. **方向（规范源 = MODULES 序）**：对每条内部**直接**依赖边 A→B，
     order(A) 必须 > order(B)（只能依赖序里更靠底部的单元）。
     嵌套模块继承父仓序位（依赖隔离壳，matrix.py #37 裁决）；伞仓 BOM 序位 -1
     （聚合全体是它的职责；任何模块 require 伞仓 = 向上违例）。
  3. **展示分层对账（⚠ 报告，不阻塞）**：gen_skill.LAYERS 五层与真实直接依赖
     边的方向矛盾逐条点名——供「展示分层重排」裁决用，不进退出码。
  4. **无环**：全部发版单元（23 根 + 12 嵌套 + 1 伞仓）作图跑 Tarjan 强连通
     分量；size>1 分量或自环 = 环。环检测吃**全部**边（含 indirect——indirect
     也是真实构建图边）；方向判据只吃**直接**边（indirect 是传递事实）。
  5. **读不到 ≠ 无环**（#79 口径）：任一单元 origin/main go.mod 读不到 ⇒
     EXIT_UNREADABLE 并点名，不许把「没读到」当「没有环」。

未知 oekit 路径即红：go.mod require 了 github.com/oekit/<x> 但 <x> 既不是根
模块也不是嵌套模块/伞仓 ⇒ 报「解析不到的内部依赖」（指向已删除仓/改名前旧
路径，与 audit 注释引用门禁同款口径——那道查注释，这道查真实依赖边）。

数据源纪律同 #246：读 **origin/main**（fetch 后 `git show`），不读本地工作树；
读不到 ⇒ 显式 EXIT_UNREADABLE，**不许**静默回退读本地工作树（那正是 #246 的
bug 形态）。

用法：
    python3 scripts/dep_gate.py            # 对真矩阵跑（origin/main）
    python3 scripts/dep_gate.py --json     # 机器可读输出（audit 集成用）

退出码契约（生产方定义，audit 按 classify 取用，不猜消息文本）：
    0 = EXIT_OK          承诺在场、方向零违例、零环（⚠ 对账行不影响退出码）
    1 = EXIT_VIOLATION   真漂移：承诺句缺失 / 方向违例（MODULES 序）/ 环 /
                         未知内部路径
    2 = EXIT_UNREADABLE  判不了（origin/main 读不到）——判不了 ≠ 通过（#79）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # import matrix（唯一事实源）

import matrix  # noqa: E402
from gen_skill import LAYERS  # noqa: E402  （展示五层；audit 已钉它恰好覆盖 MODULES）

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNREADABLE = 2

BOM_UNIT = "oekit"   # 伞仓 BOM：独立发版单元（matrix.BOM_RELEASE_UNIT）
BOM_BAND = -1        # 展示层：伞仓没有展示带（聚合全体是它的职责，不参与对账）

# go.mod require 行的 oekit 模块路径：根（github.com/oekit/<name>）与嵌套
# （github.com/oekit/<repo>/<path…>）统一由 _resolve_unit 消解。第 3 组钉住**本行**
# 行尾（到行尾为止）——indirect 标注只认行内的 `// indirect`；不钉行尾的话，
# 下一行若是注释行，跨行 lstrip 会把注释误判成 indirect 标注。
_OEKIT_REQ_RE = re.compile(
    r"^(?:\t|require )(github\.com/oekit/[A-Za-z0-9_./-]+) (v\S+)([ \t]*(?://.*)?)$", re.M)


class ReaderError(RuntimeError):
    """origin/main 事实读不到（不是「读到空」）——#79：读不到 = 不知道。"""


# ---------- 已登记豁免（显式、挂清偿单，不许悄悄堆积） ----------
#
# 方向判据的已知违例登记。纪律（先例：writeback_allowlist.json / version_line_known.json）：
#   - 每条豁免必须挂清偿单号与理由；
#   - 新违例不在登记内 ⇒ 照红（豁免不许变成后门）；
#   - 已登记的边消失 ⇒ 「豁免失配」判红（催删豁免，豁免不是永久身份）。
#   - 豁免只作用于方向判据，不作用于环/未知路径/承诺句。
#
# 现状（2026-11-28 #336 清偿后）：零登记。首跑曾有的 2 条（data→gormx/redisx
# 声明序漂移）已由 MODULES 重排（data 移至 etcdx 之后）根治，豁免删除——
# 机制本体保留：下次出现「方向合理、声明漂移」时按同款格式登记并挂清偿单。
WAIVERS: dict[tuple[str, str], str] = {}


# ---------- 单元表：36 个发版单元的身份、序位、展示层 ----------

def build_units() -> dict[str, dict]:
    """unit_id → {repo, path, order, band, kind}。

    unit_id 对外可读：根模块用仓名（"logx"），嵌套模块用「repo/子路径」
    （"logx/hlog"，与 import 路径 github.com/oekit/<unit_id> 一致），伞仓 "oekit"。
    order = MODULES 序位（方向判据的规范源，注释自 declares「依赖自底向上的顺序」）；
    band = gen_skill.LAYERS 展示层序号（仅用于 ⚠ 对账报告，不进判据）。
    """
    band_of = {}
    for idx, (_label, mods) in enumerate(LAYERS):
        for m in mods:
            band_of[m] = idx
    missing = sorted(set(matrix.MODULES) - set(band_of))
    if missing:
        # audit 的 LAYERS 覆盖检查是同一判据的另一面；这里再拦一次是防本脚本
        # 被**单独**跑（不经过 audit）时拿着不完整的展示层表静默漏对账。
        raise SystemExit(f"gen_skill.LAYERS 未覆盖模块：{missing}（展示层表不完整，拒绝出判据）")
    dup = [m for m in set(matrix.MODULES) if matrix.MODULES.count(m) > 1]
    if dup:
        raise SystemExit(f"MODULES 清单有重复项：{dup}（序位歧义，拒绝出判据）")
    units: dict[str, dict] = {}
    for order, repo in enumerate(matrix.MODULES):
        units[repo] = {"repo": repo, "path": "go.mod", "order": order,
                       "band": band_of[repo], "kind": "root"}
    for n in matrix.NESTED_MODULES:
        uid = n["module"].removeprefix("github.com/oekit/")
        parent = units[n["repo"]]
        units[uid] = {"repo": n["repo"], "path": f"{n['path']}/go.mod",
                      "order": parent["order"], "band": parent["band"],
                      "kind": "nested"}  # 继承父仓序位与展示层
    units[BOM_UNIT] = {"repo": BOM_UNIT, "path": "go.mod",
                       "order": len(matrix.MODULES),  # 伞仓在一切之上：require 全体是职责，被 require 即向上违例
                       "band": BOM_BAND, "kind": "bom"}
    return units


def _resolve_unit(import_path: str, units: dict[str, dict]) -> str | None:
    """github.com/oekit/… import 路径 → unit_id；矩阵外路径返回 None。

    嵌套优先于根：require github.com/oekit/bootstrap/nacos 时依赖的是嵌套单元
    「bootstrap/nacos」，不是 bootstrap 根——按矩阵表依赖列的旧 bug（根前缀截断）
    反向绷紧：最长匹配先试嵌套。
    """
    suffix = import_path.removeprefix("github.com/oekit/")
    best = None
    for uid, u in units.items():
        if u["kind"] != "nested":
            continue
        if suffix == uid or suffix.startswith(uid + "/"):
            if best is None or len(uid) > len(best):
                best = uid
    if best is not None:
        return best
    if suffix == BOM_UNIT:
        return BOM_UNIT           # 伞仓自身：github.com/oekit/oekit（模块 require 伞仓 = 向上违例面）
    root = suffix.split("/", 1)[0]
    if root in units and units[root]["kind"] == "root":
        return root
    return None


# ---------- 依赖边推导 ----------

def extract_edges(gomod_text: str, units: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """一份 go.mod → (内部边列表, 解析不到的 oekit 路径列表)。

    边：{"to": unit_id, "indirect": bool}；「from」由调用方补（go.mod 属主）。
    同一单元 direct 覆盖 indirect（方向判据吃 direct、环检测两者都吃，
    direct 蕴含 indirect 边）。
    """
    indirect_of: dict[str, bool] = {}
    unknown: set[str] = set()
    for m in _OEKIT_REQ_RE.finditer(gomod_text):
        path, _version, tail = m.group(1), m.group(2), m.group(3)
        uid = _resolve_unit(path, units)
        if uid is None:
            # 不是矩阵单元的 github.com/oekit/* 路径：可能指向已删除仓/旧路径。
            # 与 audit「注释引用不存在模块」同款口径——但这里查的是真实依赖边，
            # 比注释陈旧重一档，直接进违例。
            unknown.add(path)
            continue
        # direct 覆盖 indirect（同单元两者并存时按 direct 计）。
        indirect_of[uid] = indirect_of.get(uid, True) and ("// indirect" in tail)
    return ([{"to": t, "indirect": i} for t, i in indirect_of.items()], sorted(unknown))


# ---------- origin/main 读取（#246 纪律） ----------

def make_git_reader(base: str | None = None):
    """默认 reader：每仓 fetch origin main 一次，然后 git show FETCH_HEAD:<path>。

    与 audit._origin_main_tree 同纪律不同实现：这里按「单元的 go.mod 相对路径」
    逐个 show（不物化整棵树），因为本门禁只需要 go.mod，不需要树里的其他文件。
    读不到 ⇒ ReaderError（含仓与原因），调用方必须转 EXIT_UNREADABLE。
    """
    base = base or matrix.DEFAULT_BASE
    fetched: set[str] = set()

    def read(repo: str, relpath: str) -> str:
        root = matrix.repo_dir(repo, base)
        if repo not in fetched:
            r = subprocess.run(
                ["git", "-C", str(root), "fetch", "origin", "main"],
                capture_output=True, text=True, timeout=90, env=matrix.git_env())
            if r.returncode != 0:
                raise ReaderError(
                    f"{repo}: fetch origin main 失败："
                    f"{((r.stderr or r.stdout).strip().splitlines() or ['无输出'])[-1][:160]}")
            fetched.add(repo)
        r = subprocess.run(
            ["git", "-C", str(root), "show", f"FETCH_HEAD:{relpath}"],
            capture_output=True, text=True, timeout=60, env=matrix.git_env())
        if r.returncode != 0:
            raise ReaderError(f"{repo}: origin/main 上读不到 {relpath}："
                              f"{(r.stderr or '').strip().splitlines()[-1][:160]}")
        return r.stdout

    return read


def read_commitment(read) -> str:
    """读 oekit 主仓 origin/main 的 README（承诺句载体）。"""
    return read(BOM_UNIT, "README.md")


# ---------- 判据 ----------

def check_direction(edges_by_unit: dict[str, list[dict]],
                    units: dict[str, dict]) -> list[tuple[str, str, str]]:
    """方向判据（规范源 = MODULES 序）：A→B 要求 order(A) > order(B)。

    返回结构化违例 (frm, to, msg)——豁免登记按 (frm, to) 匹配，消息由 run() 统一格式化。
    """
    problems: list[tuple[str, str, str]] = []
    for frm, edges in sorted(edges_by_unit.items()):
        for e in edges:
            if e["indirect"]:
                continue  # 方向只判直接依赖（docstring 判据 2）
            to = e["to"]
            if to == frm:
                continue  # 自环归环检测管
            of, ot = units[frm]["order"], units[to]["order"]
            if ot > of:
                problems.append((frm, to,
                                 f"方向违例：{frm}（序 {of}）→ {to}（序 {ot}）"
                                 f"——依赖了序位比自己高的单元，违反 MODULES「依赖自底向上的顺序」"))
            elif ot == of:
                # 同序位 ⟹ 同仓家族（嵌套继承父仓序位）。方向语义：
                #   子 require 父 / 兄弟嵌套互 require = 隔离设计的正方向，放行
                #   （嵌套存在的前提就是它依赖父仓提供的接口）；
                #   父仓 require 自己的嵌套模块 = 把隔离的重依赖拉回父仓图，判红
                #   （2026-11-27 核实真矩阵零此类边；首跑误报子→父后修正）。
                if units[to]["kind"] == "nested" and units[frm]["kind"] != "nested" \
                        and units[to]["repo"] == units[frm]["repo"]:
                    problems.append((frm, to,
                                     f"方向违例（同仓序位）：{frm} → {to}——父仓 require 自己的"
                                     f"嵌套模块会抵消依赖隔离，嵌套的方向应是子 require 父"))
    return problems


def band_mismatches(edges_by_unit: dict[str, list[dict]], units: dict[str, dict],
                    band_label: dict[int, str]) -> list[str]:
    """展示五层 vs 真实直接依赖方向的对账（⚠ 报告用，不进退出码）。

    展示层语义：上层带（序号小）依赖下层带（序号大）。真实边若从低带指向
    高带（下层展示带 require 上层展示带）= 展示与事实不一致——不是代码错、
    就是展示分层该重排，留给架构裁决（见 docstring 首跑实录）。
    """
    lines = []
    for frm, edges in sorted(edges_by_unit.items()):
        if units[frm]["kind"] == "bom":
            continue  # 伞仓聚合全体是职责，没有展示带语义
        for e in edges:
            if e["indirect"]:
                continue
            to = e["to"]
            if to == frm or units[to]["kind"] == "bom":
                continue
            bf, bt = units[frm]["band"], units[to]["band"]
            if bt < bf:
                lines.append(
                    f"展示分层与依赖方向不一致：{frm}（{band_label[bf]}）→ "
                    f"{to}（{band_label[bt]}）——展示带说后者在上，依赖却说前者用它；"
                    f"待展示分层重排裁决（不阻塞）")
    return lines


def check_cycles(edges_by_unit: dict[str, list[dict]]) -> list[list[str]]:
    """Tarjan 强连通分量；返回全部环（size>1 分量 + 自环）。"""
    graph = {u: sorted({e["to"] for e in es}) for u, es in edges_by_unit.items()}
    index, low, on_stack, stack = {}, {}, set(), []
    counter = [0]
    comps: list[list[str]] = []

    def strongconnect(v: str):
        # 迭代化 Tarjan（链状深图不值一次爆栈风险）。
        work = [(v, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                on_stack.add(node)
            advanced = False
            for i in range(pi, len(graph.get(node, []))):
                w = graph[node][i]
                if w not in index:
                    work[-1] = (node, i + 1)
                    work.append((w, 0))
                    advanced = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                comps.append(comp)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    for v in graph:
        if v not in index:
            strongconnect(v)
    cycles = [sorted(c) for c in comps if len(c) > 1]
    for u, es in edges_by_unit.items():
        if any(e["to"] == u for e in es):
            cycles.append([u])  # 自环
    return cycles


# ---------- 主流程 ----------

def run(read, out) -> int:
    """判据主体。read=(repo, relpath)->text；out=输出函数。返回退出码。"""
    units = build_units()
    band_label = {BOM_BAND: "伞仓"} | {i: label for i, (label, _) in enumerate(LAYERS)}

    # 判据 1：承诺在场（承诺漂移 = 门禁守着一句仓里不认的话）
    commitment_problem = None
    try:
        readme = read_commitment(read)
        line = next((ln for ln in readme.splitlines()
                     if "自底向上" in ln and "无环" in ln), None)
        if line is None:
            commitment_problem = ("oekit 主仓 README（origin/main）找不到「自底向上 + 无环」"
                                  "承诺句——公开承诺被改/删，门禁与承诺必须同进退："
                                  "先对齐承诺表述，再决定本门禁去留")
    except ReaderError as e:
        out(f"❌ 判不了（#79）：承诺句核对失败：{e}")
        return EXIT_UNREADABLE

    # 边推导（全单元）
    edges_by_unit: dict[str, list[dict]] = {}
    unknown_paths: list[str] = []
    unreadable: list[str] = []
    for uid, u in sorted(units.items()):
        try:
            text = read(u["repo"], u["path"])
        except ReaderError as e:
            unreadable.append(str(e))
            continue
        edges, unknown = extract_edges(text, units)
        edges_by_unit[uid] = edges
        unknown_paths.extend(f"{uid}: require 解析不到的内部路径 {x}" for x in unknown)

    if unreadable:
        out(f"❌ 判不了（#79）：{len(unreadable)} 个单元的 origin/main go.mod 读不到，"
            f"方向与环都无从判起：")
        for e in unreadable:
            out(f"   · {e}")
        return EXIT_UNREADABLE

    n_edges = sum(len(es) for es in edges_by_unit.values())
    n_direct = sum(1 for es in edges_by_unit.values() for e in es if not e["indirect"])

    problems: list[str] = []
    if commitment_problem:
        problems.append(commitment_problem)
    problems.extend(unknown_paths)

    # 方向违例 × 豁免登记：登记内的降为 ⚠ 报告行（带清偿单号），
    # 登记外的照红；已登记的边消失 = 豁免失配，照红催删。
    direction = check_direction(edges_by_unit, units)
    waived_lines: list[str] = []
    direct_pairs = {(frm, e["to"]) for frm, es in edges_by_unit.items()
                    for e in es if not e["indirect"]}
    for frm, to, msg in direction:
        if (frm, to) in WAIVERS:
            waived_lines.append(f"⚠ 已登记豁免：{msg}——{WAIVERS[(frm, to)]}")
        else:
            problems.append(msg)
    for (frm, to), reason in sorted(WAIVERS.items()):
        if (frm, to) not in direct_pairs:
            problems.append(f"豁免失配：({frm} → {to}) 已不在依赖边里——删掉这条豁免"
                            f"（豁免不是永久身份）；原由：{reason}")

    cycles = check_cycles(edges_by_unit)
    for c in cycles:
        if len(c) == 1:
            problems.append(f"环（自环）：{c[0]} 的 go.mod require 了自己")
        else:
            problems.append("环：" + " → ".join(c) + " → " + c[0])

    mismatches = band_mismatches(edges_by_unit, units, band_label)

    out(f"单元 {len(units)}（根 {len(matrix.MODULES)} + 嵌套 {len(matrix.NESTED_MODULES)}"
        f" + 伞仓 1）· 内部边 {n_edges}（直接 {n_direct}）· "
        f"承诺句 {'在场 ✓' if not commitment_problem else '缺失 ✗'}")
    for m in waived_lines:
        out(f"  {m}")
    for m in mismatches:
        out(f"  ⚠ {m}")
    if problems:
        out(f"发现 {len(problems)} 项违例：")
        for p in problems:
            out(f"  ❌ {p}")
        return EXIT_VIOLATION
    out("✅ 承诺成立："
        + (f"方向违例仅 {len(waived_lines)} 条已登记豁免（清偿中），"
           if waived_lines else "方向零违例（MODULES 序），")
        + "全矩阵无环（36 单元全量核对，数据源 origin/main"
        + (f"；{len(mismatches)} 条展示分层对账 ⚠ 待裁决" if mismatches else "")
        + "）")
    return EXIT_OK


def classify(rc: int, out_text: str) -> tuple[str, str]:
    """audit 集成用：退出码 → (类别, 消息)。契约由本文件定义，不猜文本。"""
    if rc == EXIT_OK:
        return "ok", "dep_gate: 方向零违例（MODULES 序），全矩阵无环"
    if rc == EXIT_VIOLATION:
        tail = [ln for ln in out_text.splitlines() if "❌" in ln]
        head = tail[0].strip() if tail else "dep_gate: 存在违例"
        return "violation", f"dep_gate: {head}"
    return "unreadable", "dep_gate: origin/main 读不到，无环判不了（判不了 ≠ 通过）"


def main() -> int:
    ap = argparse.ArgumentParser(description="分层无环门禁（README 公开承诺的可复算化）")
    ap.add_argument("--json", action="store_true", help="只输出 classify 用的一行摘要")
    args = ap.parse_args()
    lines: list[str] = []
    rc = run(make_git_reader(), lines.append)
    if args.json:
        kind, msg = classify(rc, "\n".join(lines))
        print(json.dumps({"kind": kind, "msg": msg}, ensure_ascii=False))
    else:
        for ln in lines:
            print(ln)
    return rc


if __name__ == "__main__":
    sys.exit(main())
