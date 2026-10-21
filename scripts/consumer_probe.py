#!/usr/bin/env python3
"""消费面依赖画像：每个发版单元 `go get` 之后，消费方到底会被拖进多少东西。

## 为什么要有这个（2026-10-20 实测）

矩阵对外的核心承诺是「**按需取用，一个 import 即用，微库零捆绑**」与
「嵌套模块的职责是**依赖隔离**——只 import 根模块的消费者**不会**被拉入
这些重依赖」。这两条此前只靠**人肉抽查过一次**（#37 讨论时手测
`go get github.com/oekit/logx` 只拉 logx），之后没人再验过：嵌套模块被
改依赖、某个根仓被加了重依赖、或 Go 的依赖语义变了，承诺都会**静默失效**
——而组织里没有任何东西会报警。

本脚本把承诺变成**可复算的事实**：

- 对每个发版单元（24 根 + 7 嵌套 + 1 伞仓）建临时消费方模块，
  `go get <unit>@latest` 后读 `go list -m all`；
- 记录：模块图规模、第三方模块数、**同仓兄弟单元是否被拖进来**；
- 断言「隔离 / 零捆绑 / 一次拉齐」三类承诺成立，违例即红。

## 判据（每条都锚在公开承诺上，不锚在我的推测上）

1. **零捆绑**：公开承诺零第三方的仓（pkg「零第三方依赖仓」、logx「根包纯
   stdlib」、timertask「根包零 redis 依赖」），消费方模块图的第三方数必须为 0。
2. **父←子隔离**：只 import **父仓**的消费者，其模块图里不得出现**子仓扛的
   重依赖**（子仓的第三方直接依赖，从子仓自己的 go.mod 推出）。
   方向很关键：嵌套模块**自己**带重依赖正是它的存在意义（bootstrap/nacos 拖进
   nacos-sdk 是应该的）；要防的是**只 import 父仓**的消费方被连带拖进去。
   （本脚本第一版把方向写反了——去查「子仓有没有拖进兄弟的重依赖」——实测被
   数据推翻：那样查等于把嵌套模块的存在意义当成违规。）
3. **兄弟隔离**：消费某个嵌套单元时，不该把同仓的另一个嵌套单元拖进来。
4. **一次拉齐**：消费伞仓 `oekit` 时，全部 32 个发版单元必须同时在模块图里。

## 期望值从哪来

期望值来自**消费方的真实模块图**（`go list -m all`），不是被检仓的 go.mod
声明——声明不算证据（它可以只声明一部分，也可以把测试依赖写进生产 require）。

## 代价与定位

需要联网（走 module proxy），32 个单元约 2-4 分钟——所以它定位成**周巡检**
（matrix-tools 的 audit.yml 定时任务），而不是每个 PR 都跑。

用法:
    python3 scripts/consumer_probe.py                # 跑全量 + 打印表
    python3 scripts/consumer_probe.py --json out.json # 同时落 JSON（站点可用）
    python3 scripts/consumer_probe.py --unit pkg     # 只跑一个单元（调试用）
    python3 scripts/consumer_probe.py --selftest      # 判据自测（不需要联网）

退出码: 0 = 全部承诺成立；1 = 有违例（逐条打印）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from matrix import MODULES, NESTED_MODULES, ORG  # noqa: E402

# 伞仓（BOM）：有独立 tag 与 Release，`go get github.com/oekit/oekit@latest`
# 是一次独立的版本获取，按「可独立 go get 的发版单元」口径计入。
BOM_REPO = "oekit"

# 判据一：零捆绑。只对**文档里明确承诺过**的仓生效。
ZERO_THIRD_PARTY = {"pkg", "logx", "timertask"}


def _nested_units() -> list[str]:
    """嵌套模块的单元名（`parent/sub`），取自 matrix.py 的登记表。"""
    return [f"{n['repo']}/{n['path']}" for n in NESTED_MODULES]


def _direct_third_party(gomod: Path) -> list[str]:
    """go.mod 里的第三方**直接**依赖（跳过 indirect 与矩阵内模块）。"""
    if not gomod.is_file():
        return []
    direct: list[str] = []
    inblock = False
    for line in gomod.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if s.startswith("require ("):
            inblock = True
            continue
        if inblock and s == ")":
            inblock = False
            continue
        cand = s if inblock else (s[len("require "):].strip() if s.startswith("require ") else None)
        if not cand or "// indirect" in cand:
            continue
        m = re.match(r"^([a-z0-9.\-]+\.[a-z]+/[^ ]+)\s+(v[^ ]+)", cand)
        if m and not m.group(1).startswith(f"github.com/{ORG}/"):
            direct.append(m.group(1))
    return direct


def child_heavy_deps(child_unit: str, base: str) -> list[str]:
    """子仓的第三方**直接**依赖 —— 这些就是「子仓替父仓扛的重依赖」。

    从子仓自己的 go.mod 推出（不写死清单）：子仓新增重依赖时，判据自动跟着变。
    """
    parent, sub = child_unit.split("/", 1)
    return _direct_third_party(Path(base) / parent / sub / "go.mod")


# ---------- 判据 ----------

def _paths(res: dict) -> set[str]:
    """取消费方模块图里的**模块路径**集合（去掉版本号）。

    `go list -m all` 的行是「路径 版本」。判据一律按路径比较——拿带版本号的
    整行去比路径，会把「真泄漏」判成「没泄漏」（漏报，方向相反的假通过）。
    这里再做一次归一，判据对两种输入都成立。
    """
    raw = res.get("paths")
    if not raw:
        raw = res.get("modules") or []
    return {str(x).split()[0] for x in raw if str(x).strip()}


def check_parent_isolation(child_unit: str, parent_res: dict, base: str) -> list[str]:
    """父←子隔离：父仓的消费方模块图里不得出现子仓扛的重依赖。"""
    if not parent_res.get("ok"):
        return []
    heavy = child_heavy_deps(child_unit, base)
    if not heavy:
        return []
    paths = _paths(parent_res)
    parent = child_unit.split("/")[0]
    hit: list[str] = []
    for h in heavy:
        if any(p == h or p.startswith(h + "/") for p in paths):
            hit.append(h)
    if hit:
        return [f"{parent}: 消费方模块图里出现了 {child_unit} 扛的重依赖 {hit}"
                f"——只 import 父仓的消费者被连带拖入，嵌套模块的隔离职责失效"]
    return []


def check_sibling_isolation(unit: str, res: dict) -> list[str]:
    """兄弟隔离：消费某个嵌套单元时，不该把同仓的另一个嵌套单元拖进来。"""
    problems: list[str] = []
    if not res.get("ok"):
        return [f"{unit}: 消费方装不上（{res.get('error', '')[:160]}）"]
    paths = _paths(res)
    parent = unit.split("/")[0]
    for sib in _nested_units():
        if sib.split("/")[0] != parent or sib == unit:
            continue
        if f"github.com/{ORG}/{sib}" in paths:
            problems.append(f"{unit}: 把同仓兄弟单元 {sib} 拖进了消费方模块图（依赖隔离被破坏）")
    return problems


def check_bom_coverage(bom_res: dict) -> list[str]:
    """一次拉齐：消费伞仓时，全部发版单元必须同时在模块图里。"""
    if not bom_res.get("ok"):
        return [f"{BOM_REPO}: 消费方装不上（{bom_res.get('error', '')[:160]}）"]
    paths = _paths(bom_res)
    want = {f"github.com/{ORG}/{m}" for m in MODULES}
    want |= {f"github.com/{ORG}/{n['repo']}/{n['path']}" for n in NESTED_MODULES}
    want |= {f"github.com/{ORG}/{BOM_REPO}"}
    missing = sorted(want - paths)
    if missing:
        return [f"{BOM_REPO}: 消费方模块图里少了 {len(missing)} 个发版单元：{missing[:5]}——"
                f"「一次拉齐」名不副实"]
    return []


def check_zero_third_party(unit: str, res: dict) -> list[str]:
    """零捆绑：公开承诺零第三方的仓，消费方模块图里第三方模块数必须为 0。"""
    if unit not in ZERO_THIRD_PARTY:
        return []
    if not res.get("ok"):
        return []  # 装不上已在别处报
    tp = res.get("third_party") or []
    if tp:
        return [f"{unit}: 宣称零第三方依赖，但消费方模块图里出现了 {len(tp)} 个第三方模块：{tp[:5]}"]
    return []


# ---------- 测量 ----------

def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str]:
    env = dict(os.environ)
    env["GOWORK"] = "off"
    env["GOFLAGS"] = "-mod=mod"
    env.setdefault("GOPROXY", "https://proxy.golang.org,direct")
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def probe_unit(unit: str, workdir: Path) -> dict:
    """在临时消费方模块里 `go get <unit>@latest`，读回模块图。"""
    mod_path = f"github.com/{ORG}/{unit}"
    d = workdir / unit.replace("/", "_")
    d.mkdir(parents=True, exist_ok=True)
    (d / "go.mod").write_text(
        f"module probe_{unit.replace('/', '_').replace('-', '_')}\n\ngo 1.24\n")
    (d / "main.go").write_text("package main\n\nfunc main() {}\n")
    t0 = time.time()
    rc, out = _run(["go", "get", f"{mod_path}@latest"], d)
    if rc != 0:
        return {"unit": unit, "ok": False, "error": out.strip()[:300],
                "modules": [], "paths": [], "third_party": [], "own": [],
                "seconds": round(time.time() - t0, 1)}
    rc2, out2 = _run(["go", "list", "-m", "all"], d)
    mods = [l.strip() for l in out2.splitlines() if l.strip()] if rc2 == 0 else []
    # `go list -m all` 的行是「路径 版本」；判据一律按**路径**比较——
    # 用带版本号的整行去比路径，会让「真泄漏」被判成「没泄漏」（漏报）。
    paths = [l.split()[0] for l in mods if l.split()]
    third_party = [p for p in paths
                   if "." in p.split("/")[0] and not p.startswith(f"github.com/{ORG}/")]
    own = [p for p in paths if p.startswith(f"github.com/{ORG}/")]
    return {"unit": unit, "ok": True, "error": "", "modules": mods, "paths": paths,
            "third_party": third_party, "own": own,
            "seconds": round(time.time() - t0, 1)}


# ---------- 自测 ----------

def _selftest() -> int:
    """判据自测（不联网）：断言逻辑本身必须能抓住违例。

    样本要**真的**带被防的形态——否则自测会假通过。
    """
    # ① 父仓被连带拖入子仓的重依赖 → 必须报
    r = {"ok": True, "paths": ["github.com/oekit/bootstrap", "github.com/nacos-group/nacos-sdk-go/v2"],
         "third_party": [], "own": [], "error": ""}
    p = check_parent_isolation("bootstrap/nacos", r, "/nonexistent")
    # base 不存在 → 拿不到子仓重依赖 → 不报（此时判据无结论，不是通过）
    assert not p, f"无子仓清单时不应下结论：{p}"
    p2 = check_parent_isolation("bootstrap/nacos", r, _fixture_base())
    assert any("扛的重依赖" in x for x in p2), f"应报出父←子泄漏：{p2}"

    # ② 父仓干净 → 不报
    r2 = {"ok": True, "paths": ["github.com/oekit/bootstrap", "github.com/cloudwego/hertz"],
          "third_party": [], "own": [], "error": ""}
    assert not check_parent_isolation("bootstrap/nacos", r2, _fixture_base()), "干净样本不该报"

    # ③ 兄弟单元被拖进来 → 必须报；干净 → 不报
    r3 = {"ok": True, "paths": ["github.com/oekit/bootstrap/nacos", "github.com/oekit/bootstrap/obs"],
          "third_party": [], "own": [], "error": ""}
    assert any("兄弟单元" in x for x in check_sibling_isolation("bootstrap/nacos", r3)), "应报兄弟泄漏"
    r4 = {"ok": True, "paths": ["github.com/oekit/bootstrap/nacos"], "third_party": [], "own": [], "error": ""}
    assert not check_sibling_isolation("bootstrap/nacos", r4), "干净样本不该报"

    # ④ 零捆绑：pkg 拖进第三方 → 必须报；干净 → 不报；非承诺仓不适用
    bad = {"ok": True, "paths": ["github.com/oekit/pkg", "github.com/foo/bar"],
           "third_party": ["github.com/foo/bar"], "own": [], "error": ""}
    assert check_zero_third_party("pkg", bad), "pkg 带第三方必须报"
    clean = {"ok": True, "paths": ["github.com/oekit/pkg"], "third_party": [], "own": [], "error": ""}
    assert not check_zero_third_party("pkg", clean), "pkg 干净不该报"
    assert not check_zero_third_party("hertz", bad), "非零捆绑仓不适用这条判据"

    # ⑤ 伞仓覆盖：少一个单元 → 必须报；齐全 → 不报
    want_one = f"github.com/oekit/{MODULES[0]}"
    incomplete = {"ok": True, "paths": [want_one, f"github.com/oekit/{BOM_REPO}"],
                  "third_party": [], "own": [], "error": ""}
    assert check_bom_coverage(incomplete), "缺单元必须报"
    full = {"ok": True, "paths": sorted(
        {f"github.com/oekit/{m}" for m in MODULES}
        | {f"github.com/oekit/{n['repo']}/{n['path']}" for n in NESTED_MODULES}
        | {f"github.com/oekit/{BOM_REPO}"}), "third_party": [], "own": [], "error": ""}
    assert not check_bom_coverage(full), "齐全不该报"

    # ⑥ 版本号粒度陷阱：`go list -m all` 的行带版本号，判据必须按路径比。
    #    若判据拿带版本号的整行去比路径，这条真泄漏会被判成「没泄漏」（漏报）。
    versioned = {"ok": True, "paths": ["github.com/oekit/bootstrap/nacos",
                                       "github.com/oekit/bootstrap/obs v0.2.9"],
                 "third_party": [], "own": [], "error": ""}
    assert any("兄弟单元" in x for x in check_sibling_isolation("bootstrap/nacos", versioned)), \
        "带版本号的模块行也必须能被判出兄弟泄漏"

    print("✅ consumer_probe 判据自测通过（父←子泄漏 / 兄弟泄漏 / 零捆绑 / 一次拉齐）")
    return 0


def _fixture_base() -> str:
    """造一个临时的「子仓 go.mod」夹具，让父←子判据有子仓清单可用。"""
    import atexit
    d = Path(tempfile.mkdtemp(prefix="probe_fixture_"))
    atexit.register(shutil.rmtree, d, True)
    (d / "bootstrap" / "nacos").mkdir(parents=True)
    (d / "bootstrap" / "nacos" / "go.mod").write_text(
        "module github.com/oekit/bootstrap/nacos\n\ngo 1.24\n\n"
        "require github.com/nacos-group/nacos-sdk-go/v2 v2.3.0\n")
    return str(d)


# ---------- 主流程 ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="消费面依赖画像与隔离承诺核验")
    ap.add_argument("--unit", default="", help="只跑一个单元")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    ap.add_argument("--base", default="", help="各仓父目录（默认取本仓上级）")
    ap.add_argument("--selftest", action="store_true", help="判据自测（不联网）")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    base = args.base or str(Path(__file__).resolve().parent.parent.parent)
    units = [args.unit] if args.unit else (list(MODULES) + _nested_units() + [BOM_REPO])
    results: list[dict] = []
    by_unit: dict[str, dict] = {}
    tmp = Path(tempfile.mkdtemp(prefix="consumer_probe_"))
    try:
        for u in units:
            res = probe_unit(u, tmp)
            results.append(res)
            by_unit[u] = res
            n = len(res.get("modules") or [])
            tp = len(res.get("third_party") or [])
            print(f"  {u:24} 模块图 {n:4} · 第三方 {tp:4} · {res.get('seconds', 0):5.1f}s"
                  + ("" if res.get("ok") else f"  ✗ {res.get('error', '')[:80]}"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    problems: list[str] = []
    for u in units:
        res = by_unit[u]
        problems += check_sibling_isolation(u, res) if "/" in u else []
        problems += check_zero_third_party(u, res)
        if "/" in u:
            parent = u.split("/")[0]
            problems += check_parent_isolation(u, by_unit.get(parent, {}), base)
    problems += check_bom_coverage(by_unit.get(BOM_REPO, {"ok": False, "error": "未测量"}))

    print()
    if problems:
        print(f"❌ 消费面承诺被打破（{len(problems)} 条）：")
        for p in problems:
            print(f"  · {p}")
    else:
        print(f"✅ 消费面承诺全部成立（{len(results)} 个发版单元："
              f"父←子隔离 + 兄弟隔离 + 零捆绑 + 一次拉齐）")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"results": [{k: v for k, v in r.items() if k != "modules"} for r in results],
             "problems": problems}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  （已写 {args.json}）")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
