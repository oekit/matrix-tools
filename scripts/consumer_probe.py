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
5. **装得上**（其余判据的**前置条件**）：消费方 `go get <unit>@latest` 必须成功。
   装不上（代理抖动 / 网络 / tag 缺失）时**所有**隔离判据都没有结论 ⇒ 必须**报出来**
   ——**读不到 ≠ 干净**。旧实现里这条只挂在嵌套模块上 ⇒ 24 个**根模块**任何一个
   装不上，脚本照样打印「✅ 消费面承诺全部成立」（假绿）。

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

# 判据用的 module proxy：**显式指定，不吃环境变量**（matrix-tools#248）。
#
# 旧写法是 setdefault（外部已设置的 GOPROXY 优先）⇒ 比如（比如巡检规范
# 推荐给本地离线编译用的 `https://goproxy.cn,direct`）会**静默改写**判据的
# 事实源：落后一个版本的镜像上 `@latest` 解析到旧版本 ⇒ 模块图少一个发版单元
# ⇒「一次拉齐」被误判成「承诺被打破」——同一个 commit 换个代理结论相反
# ⇒ 这条「可复算的事实」本身不可复算（2026-11-08 实测两个代理结论相反）。
# 现在：代理只认 `--proxy` 参数（默认官方源），**环境变量一律进不去**，
# 且实际使用的代理打进报告头 ⇒ 读者一眼知道结论出自哪个镜像。
DEFAULT_PROXY = "https://proxy.golang.org,direct"
PROXY = DEFAULT_PROXY      # main() 按 --proxy 覆写；自测直接改这个全局来验


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


def _under_nested_module(repo_dir: Path, rel: Path) -> bool:
    """这个文件是否位于某个**嵌套模块**目录内（该目录或它的任一上级有 go.mod）。

    ⚠️ 只看**第一层**会漏掉深层嵌套——kitex 的 kitexotel 在
    `observability/tracing/instrumentation/kitexotel`（深度 4），而第一层
    `observability/` 没有 go.mod ⇒ kitexotel 的 import 会被算成**父仓**的
    ⇒ 父←子泄漏判据变成假阴性（父仓看起来「自己 import 了」）。
    """
    parts = rel.parts[:-1]  # 去掉文件名，只留目录部分
    for k in range(1, len(parts) + 1):
        if (repo_dir.joinpath(*parts[:k]) / "go.mod").is_file():
            return True
    return False


def _source_imports(repo_dir: Path) -> set[str]:
    """仓内所有 Go 源码 import 到的第三方路径（**排除嵌套模块目录**——它们是独立 module）。"""
    out: set[str] = set()
    if not repo_dir.is_dir():
        return out
    for p in sorted(repo_dir.rglob("*.go")):
        rel = p.relative_to(repo_dir)
        if len(rel.parts) > 1 and _under_nested_module(repo_dir, rel):
            continue  # 嵌套模块（任意深度）：不算父仓的依赖
        try:
            body = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        out |= set(re.findall(r'"([a-z0-9.\-]+\.[a-z]+/[^"]+)"', body))
    return out


def _imports_module(imports: set[str], mod: str) -> bool:
    """源码是否 import 了这个**模块**（按模块前缀比，不按全等）。

    ⚠️ 必须按前缀：源码里写的是**子包路径**（`github.com/cloudwego/kitex/server`），
    而 go.mod 里列的是**模块路径**（`github.com/cloudwego/kitex`）⇒ 拿全等去比会
    把「父仓确实 import 了这个模块」误判成「没 import」⇒ **假阳性**。
    实测 2026-10-27：kitex 父仓被判「替 kitexotel / kitex-istio 背重依赖」，
    而它自己的 bootstrap.go 就 import 了 `github.com/cloudwego/kitex/server`。
    """
    return any(ip == mod or ip.startswith(mod + "/") for ip in imports)


def check_parent_isolation(child_unit: str, parent_res: dict, base: str) -> list[str]:
    """父←子隔离：父仓的 go.mod 不得**直接** require 子仓扛的重依赖。

    判据的归因很关键（第一版写错过）：父仓**不依赖**子仓（隔离的方向就是父←子），
    所以父仓消费方模块图里出现的任何库，**都不可能是子仓拖进来的**——它要么来自
    父仓自己的依赖，要么来自父仓依赖的传递闭包。

    因此唯一能判定的违例是：**父仓的 go.mod 里直接列着子仓的重依赖**（拆分没做干净
    ——父仓仍然 require 着子仓 isolate 的那份），且**父仓自己的源码并不 import 它**
    （否则是父仓自己 legitimate 的用法）。

    踩过的坑：registry/nacos 用 yaml.v3，而 registry 的消费方图里也有 yaml.v3
    （经 core 进来）——第一版判据把"图里有"当成"子仓拖的"，报了一条假阳性。
    """
    if not parent_res.get("ok"):
        return []
    heavy = child_heavy_deps(child_unit, base)
    if not heavy:
        return []
    parent = child_unit.split("/")[0]
    parent_dir = Path(base) / parent
    parent_direct = set(_direct_third_party(parent_dir / "go.mod"))
    parent_imports = _source_imports(parent_dir)
    # 「父仓自己 import 了它吗」按**模块前缀**判（源码里写的是子包路径，
    # go.mod 里是模块路径）⇒ 全等比会造出假阳性。
    leak = sorted(h for h in heavy
                  if h in parent_direct and not _imports_module(parent_imports, h))
    if leak:
        return [f"{parent}: go.mod 仍直接 require 子模块 {child_unit} 扛的重依赖 {leak}"
                f"（且本仓源码并未 import 它们）——拆分没做干净，父仓仍在替子模块背依赖"]
    return []


def check_sibling_isolation(unit: str, res: dict) -> list[str]:
    """兄弟隔离：消费某个嵌套单元时，不该把同仓的另一个嵌套单元拖进来。

    ⚠️ 「装不上」**不由本函数判**（统一由 `check_installable` 判）：装不上时消费方
       模块图是空的，兄弟泄漏**无从谈起**（无结论 ≠ 通过）⇒ 归到统一判据报，
       免得同一个故障在两条判据里各报一次。
    """
    problems: list[str] = []
    if not res.get("ok"):
        return []
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


def check_installable(unit: str, res: dict) -> list[str]:
    """装得上：消费方 `go get <unit>@latest` 必须成功。

    ★ **口径纪律：读不到 ≠ 干净**。装不上（代理抖动 / 网络 / tag 缺失）时，
      **所有**隔离判据都没有结论 ⇒ 必须**报出来**，不能当成「没问题」。

      ⚠️ 旧实现里这条判据**只挂在嵌套模块上**（经 `check_sibling_isolation` 的
         `if not res.get("ok")` 分支）⇒ **24 个根模块**任何一个装不上时，
         `check_zero_third_party` 里那句「装不上已在别处报」是**假话**
         ⇒ consumer_probe 照样打印「✅ 消费面承诺全部成立」（假绿）。
      ⇒ 现在装不上对**所有**单元一视同仁（根模块与嵌套模块一致）。
    """
    if not res.get("ok"):
        return [f"{unit}: 消费方装不上（{(res.get('error') or '未测量')[:160]}）"]
    return []


def check_zero_third_party(unit: str, res: dict) -> list[str]:
    """零捆绑：公开承诺零第三方的仓，消费方模块图里第三方模块数必须为 0。"""
    if unit not in ZERO_THIRD_PARTY:
        return []
    if not res.get("ok"):
        return []  # 装不上由统一判据 check_installable 报（这里不重复报）
    tp = res.get("third_party") or []
    if tp:
        return [f"{unit}: 宣称零第三方依赖，但消费方模块图里出现了 {len(tp)} 个第三方模块：{tp[:5]}"]
    return []


def collect_problems(units: list[str], by_unit: dict, base: str) -> list[str]:
    """把各条判据的结论汇成 problems 列表（**main 与自测走同一条路径**）。

    ★ 抽成函数不是为了好看：自测要能**复刻 main 的分派**并断言它的结论
      （旧实现的分派对根模块的「装不上」完全无感 ⇒ problems = [] ⇒ 报绿）。
      判据写在 main 里 ⇒ 自测只能测判据函数、测不到**分派** ⇒ 分派漏了判据没人知道。

    伞仓（oekit）的「装不上」由 `check_bom_coverage` 报（它的口径不同：装不上时
    要报「少了哪些发版单元」，不能只说装不上）⇒ 这里跳过它，避免同一个故障报两次。

    ★ **伞仓没被测量就不做判定**（matrix-tools#255）：`--unit` 模式只测一个单元
      ⇒ `by_unit` 里没有伞仓 ⇒ 旧写法把「伞仓缺席」喂成 {"ok": False,
      "error": "未测量"} ⇒ 判出一条**不存在的故障**
      （「伞仓装不上」）并退出码 1 ⇒ 单单元调试入口**必然假红**。
      口径：**未测量既不是绿也不是红**——没测的单元不产生任何问题。
    """
    problems: list[str] = []
    for u in units:
        res = by_unit[u]
        # ★ 统一判据：**装不上**对**所有**单元一视同仁（根模块与嵌套模块一致）。
        if u != BOM_REPO:
            problems += check_installable(u, res)
        problems += check_sibling_isolation(u, res) if "/" in u else []
        problems += check_zero_third_party(u, res)
        if "/" in u:
            parent = u.split("/")[0]
            problems += check_parent_isolation(u, by_unit.get(parent, {}), base)
    if BOM_REPO in by_unit:
        problems += check_bom_coverage(by_unit[BOM_REPO])
    return problems


# ---------- 测量 ----------

def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str]:
    env = dict(os.environ)
    env["GOWORK"] = "off"
    env["GOFLAGS"] = "-mod=mod"
    # ★ **硬设**，不用 setdefault（matrix-tools#248）：判据的事实源必须显式、
    #   可复算——外部 GOPROXY 静默改写代理 ⇒ 落后镜像上的「@latest」解析到旧
    #   版本 ⇒ 把「镜像没收录新版本」当成「承诺被打破」（假红）。
    env["GOPROXY"] = PROXY
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
    # ① 父仓的 go.mod 仍直接 require 子仓的重依赖 → 必须报
    r = {"ok": True, "paths": ["github.com/oekit/bootstrap"],
         "third_party": [], "own": [], "error": ""}
    p = check_parent_isolation("bootstrap/nacos", r, "/nonexistent")
    # base 不存在 → 拿不到子仓重依赖 → 不报（此时判据无结论，不是通过）
    assert not p, f"无子仓清单时不应下结论：{p}"
    p2 = check_parent_isolation("bootstrap/nacos", r, _fixture_base(leak=True))
    assert any("扛的重依赖" in x for x in p2), f"应报出父←子泄漏：{p2}"

    # ② 父仓的 go.mod 干净（没替子模块背依赖）→ 不报
    r2 = {"ok": True, "paths": ["github.com/oekit/bootstrap"], "third_party": [], "own": [], "error": ""}
    assert not check_parent_isolation("bootstrap/nacos", r2, _fixture_base(leak=False)), \
        "父仓没 require 子仓的重依赖时不该报"

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

    # ⑦ 粒度陷阱：源码 import 的是**子包**，go.mod 里是**模块路径** ⇒ 必须按前缀判。
    #    2026-10-27 实测踩过：kitex 父仓 import `github.com/cloudwego/kitex/server`，
    #    判据拿全等比 ⇒ 误判成「父仓没 import」⇒ 报了两条**假阳性**
    #    （「父仓替 kitexotel / kitex-istio 背重依赖」——而 kitex 的 go.mod 是对的）。
    #    ⛔ 这条断言本身就是反证样本：若前缀比退回全等，它立刻变红。
    base3 = _fixture_base(leak=True, parent_imports_subpkg=True)
    p3 = check_parent_isolation("bootstrap/nacos", r, base3)
    assert not p3, f"父仓 import 的是该模块的子包时不该报泄漏（假阳性）：{p3}"

    # ⑧ 深层嵌套模块的 import 不该算成父仓的（kitexotel 在深度 4）。
    #    只排除第一层 ⇒ kitexotel 的 import 会算到父仓头上 ⇒ 泄漏判据变假阴性。
    base4 = _fixture_base(leak=True, deep_nested=True)
    imp = _source_imports(Path(base4) / "bootstrap")
    assert "github.com/nacos-group/nacos-sdk-go/v2/config" not in imp, \
        f"深层嵌套模块的 import 被算成父仓的了：{imp}"

    # ⑨ ★ **装不上**必须被抓住 —— 对**所有**单元一视同仁（根模块与嵌套模块一致）。
    #    ⚠️ 旧实现的分派：根模块只跑 check_zero_third_party（装不上时返回 []）
    #      ⇒ 24 个根模块任何一个装不上，problems 仍是 [] ⇒ 报「✅ 全部成立」（假绿）。
    #    ⛔ 这组断言就是反证样本：把分派退回旧写法（去掉 check_installable），
    #      它立刻变红（见交付记录里的变异自证）。
    full_ok = {"ok": True, "error": "", "paths": sorted(
        {f"github.com/oekit/{m}" for m in MODULES}
        | {f"github.com/oekit/{n['repo']}/{n['path']}" for n in NESTED_MODULES}
        | {f"github.com/oekit/{BOM_REPO}"}),
        "third_party": [], "own": [], "modules": []}
    ok_nested = {"ok": True, "error": "", "paths": ["github.com/oekit/bootstrap/nacos"],
                 "third_party": [], "own": [], "modules": []}
    broken = {"ok": False, "error": "proxy timeout", "paths": [], "third_party": [],
              "own": [], "modules": []}
    # 根模块装不上 ⇒ 必须有且只有一条 problems（旧的 main 分派下这里是 []）
    p_root = collect_problems(["pkg", "oekit"],
                              {"pkg": broken, "oekit": full_ok}, "/nonexistent")
    assert len(p_root) == 1 and "pkg" in p_root[0] and "装不上" in p_root[0], \
        f"根模块装不上必须被报出（旧实现这里 problems=[]）：{p_root}"
    # 嵌套模块装不上 ⇒ 同样必须被报（不许因改判据而回退）
    p_nested = collect_problems(["bootstrap/nacos", "oekit"],
                                {"bootstrap/nacos": broken, "oekit": full_ok}, "/nonexistent")
    assert len(p_nested) == 1 and "bootstrap/nacos" in p_nested[0] and "装不上" in p_nested[0], \
        f"嵌套模块装不上必须被报出：{p_nested}"
    # 全部装得上 ⇒ 不得有任何「装不上」结论（判据不许误报）
    p_ok = collect_problems(
        ["pkg", "bootstrap/nacos", "oekit"],
        {"pkg": full_ok, "bootstrap/nacos": ok_nested, "oekit": full_ok}, "/nonexistent")
    assert not p_ok, f"装得上时不该有问题（误报）：{p_ok}"
    # 判据函数本身：装不上 ⇒ 报；装得上 ⇒ 不报
    assert check_installable("pkg", broken), "根模块装不上必须报"
    assert check_installable("bootstrap/nacos", broken), "嵌套模块装不上必须报"
    assert not check_installable("pkg", {"ok": True, "error": ""}), "装得上不该报"
    assert not check_zero_third_party("pkg", broken), \
        "装不上不归零捆绑判据管（统一判据报，别重复报）"

    # ⑩ ★ --unit 模式：**未测量的单元不得产生任何 problem**（matrix-tools#255）。
    #    旧写法把「伞仓缺席」喂成 {"ok": False, "error": "未测量"} ⇒ 单单元调试
    #    入口必然多出一条「oekit: 消费方装不上（未测量）」的假红并退出码 1。
    #    ⛔ 变异自证：把分派退回「伞仓缺席 ⇒ 默认未测量」的旧写法
    #      ⇒ 本组断言立刻转红（见下面的源码级锚点断言）。
    unit_like = {"ok": True, "error": "", "paths": ["github.com/oekit/logx/sink"],
                 "third_party": [], "own": [], "modules": []}
    p_unit = collect_problems(["logx/sink"], {"logx/sink": unit_like}, "/nonexistent")
    assert p_unit == [], f"--unit 模式下未测量的伞仓不得产生 problem（#255）：{p_unit}"
    # 被点名的单元真有问题 ⇒ 真红必须还在（不许把真红一起消掉）
    p_unit_bad = collect_problems(["logx/sink"], {"logx/sink": broken}, "/nonexistent")
    assert len(p_unit_bad) == 1 and "logx/sink" in p_unit_bad[0], \
        f"--unit 模式下被点名单元的真问题必须报出：{p_unit_bad}"
    # --unit oekit（伞仓自己）⇒ 一次拉齐判据照常跑（点名伞仓 = 要判它）
    p_bom_unit = collect_problems(["oekit"], {"oekit": full_ok}, "/nonexistent")
    assert not p_bom_unit, f"--unit oekit 时伞仓判据应照常成立：{p_bom_unit}"

    # ⑪ ★ 代理口径：**环境变量 GOPROXY 不得改写判据事实源**（matrix-tools#248）。
    #    实测：GOPROXY=goproxy.cn（落后一个版本的镜像）⇒ @latest 解析到旧版本
    #    ⇒「一次拉齐」被误判成「承诺被打破」⇒ 同一 commit 换代理结论相反。
    #    ⛔ 变异自证：把 `env["GOPROXY"] = PROXY` 退回 `env.setdefault(...)` ⇒
    #      下面的子进程读到的就是外部环境变量 ⇒ 断言转红（连同源码锚点断言）。
    _old_goproxy = os.environ.get("GOPROXY")
    os.environ["GOPROXY"] = "https://goproxy.cn,direct"
    try:
        rc_env, out_env = _run([sys.executable,
                                "-c", "import os; print(os.environ.get('GOPROXY'))"],
                               Path.cwd(), timeout=60)
    finally:
        if _old_goproxy is None:
            os.environ.pop("GOPROXY", None)
        else:
            os.environ["GOPROXY"] = _old_goproxy
    assert rc_env == 0 and out_env.strip() == DEFAULT_PROXY, \
        (f"_run 子进程必须看到工具指定的代理（{DEFAULT_PROXY}），"
         f"而不是外部环境变量改写的值（#248）：rc={rc_env} out={out_env.strip()!r}")

    # 源码级锚点（两条变异自证）：退回旧写法时这里直接拦下
    #（锚点串拆开写——否则本断言自己的源码就含锚点，永远自红）
    _src = Path(__file__).read_text(encoding="utf-8")
    assert ("env.set" + 'default("GOPROXY"') not in _src, \
        "变异自证：GOPROXY 又回到 setdefault——外部环境变量会静默改写判据事实源（#248）"
    assert "if BOM_REPO in by_unit:" in _src and ("by_unit.get(BOM" + "_REPO,") not in _src, \
        "变异自证：伞仓缺席又被当成「装不上」喂给判据——--unit 入口必然假红（#255）"

    print("✅ consumer_probe 判据自测通过（父←子泄漏 / 兄弟泄漏 / 零捆绑 / 一次拉齐 / "
          "子包粒度 / 深层嵌套 / 装不上 / --unit 只判点名单元 / 代理口径不吃环境变量）")
    return 0


def _fixture_base(leak: bool = True, parent_imports_subpkg: bool = False,
                  deep_nested: bool = False) -> str:
    """造夹具：子仓 go.mod 带重依赖；父仓 go.mod 是否也带（leak=True = 拆分没做干净）。

    父仓的源码**不** import 那个重依赖——所以「父仓 require 了它」就是实打实的
    泄漏（父仓在替子模块背依赖）。

    parent_imports_subpkg：父仓源码 import 该重依赖的**子包**（真实世界的常态：
      源码写 `github.com/cloudwego/kitex/server`，go.mod 写模块路径
      `github.com/cloudwego/kitex`）⇒ 这时**不该**报泄漏。
    deep_nested：子仓放在**深层**目录（`a/b/c`）⇒ 用来验「嵌套模块排除要覆盖任意深度」。
    """
    import atexit
    d = Path(tempfile.mkdtemp(prefix="probe_fixture_"))
    atexit.register(shutil.rmtree, d, True)
    sub = "bootstrap/nacos"
    if deep_nested:
        sub = "bootstrap/deep/nested/leaf"
    (d / sub).mkdir(parents=True)
    (d / sub / "go.mod").write_text(
        "module github.com/oekit/bootstrap/nacos\n\ngo 1.24\n\n"
        "require github.com/nacos-group/nacos-sdk-go/v2 v2.3.0\n")
    parent = "module github.com/oekit/bootstrap\n\ngo 1.24\n\n"
    if leak:
        parent += "require github.com/nacos-group/nacos-sdk-go/v2 v2.3.0\n"
    (d / "bootstrap" / "go.mod").write_text(parent)
    if parent_imports_subpkg:
        (d / "bootstrap" / "app.go").write_text(
            "package bootstrap\n\n"
            "import _ \"github.com/nacos-group/nacos-sdk-go/v2/config\"\n")
    else:
        (d / "bootstrap" / "app.go").write_text(
            "package bootstrap\n\n// 父仓自己不用 nacos-sdk\n")
    return str(d)


# ---------- 主流程 ----------

def main() -> int:
    global PROXY
    ap = argparse.ArgumentParser(description="消费面依赖画像与隔离承诺核验")
    ap.add_argument("--unit", default="", help="只跑一个单元（调试用；只判被点名的单元）")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    ap.add_argument("--base", default="", help="各仓父目录（默认取本仓上级）")
    ap.add_argument("--proxy", default=DEFAULT_PROXY,
                    help=f"判据事实源用的 module proxy（默认 {DEFAULT_PROXY}；"
                         f"环境变量 GOPROXY 不影响本判据，matrix-tools#248）")
    ap.add_argument("--selftest", action="store_true", help="判据自测（不联网）")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    PROXY = args.proxy

    # stdout 行缓冲：重定向/管道时 Python 默认块缓冲 ⇒ 整个运行期间日志都是 0 字节，
    # 进程被杀/超时时**一个字都留不下**（2026-10-25 audit.py 踩过同一个坑）。
    # 本脚本 34 个单元逐个 `go list -m all`，单次要好几分钟 ⇒ 进度必须能实时看见。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    base = args.base or str(Path(__file__).resolve().parent.parent.parent)
    units = [args.unit] if args.unit else (list(MODULES) + _nested_units() + [BOM_REPO])
    # 报告头带上**实际使用的代理**（matrix-tools#248）：「消费面承诺成立与否」
    # 依赖判据当时的 proxy ⇒ 结论必须能复算 ⇒ 读者得知道事实源出自哪个镜像。
    print(f"  （判据事实源 module proxy：{PROXY}——环境变量 GOPROXY 改写不了它）")
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

    problems = collect_problems(units, by_unit, base)

    print()
    if problems:
        print(f"❌ 消费面承诺被打破（{len(problems)} 条）：")
        for p in problems:
            print(f"  · {p}")
    else:
        print(f"✅ 消费面承诺全部成立（{len(results)} 个发版单元："
              f"装得上 + 父←子隔离 + 兄弟隔离 + 零捆绑 + 一次拉齐）")
    if args.unit:
        # matrix-tools#255：--unit 是单单元调试入口——被点名的单元没问题就是没问题，
        # 不得被「没测过的伞仓」造出的假红淹没。这里明说覆盖面，覆盖不足不装作全量。
        total = len(list(MODULES) + _nested_units() + [BOM_REPO])
        print(f"ℹ --unit 模式：本次只测了 {len(units)}/{total} 个发版单元"
              f"（只判被点名的单元；未测单元不做判定）")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"results": [{k: v for k, v in r.items() if k != "modules"} for r in results],
             "problems": problems}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  （已写 {args.json}）")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
