#!/usr/bin/env python3
"""审计「各仓自身 go.mod 地板 vs 远端最新 tag」这道门禁的自测。

**为什么要有这个自测**（oekit/oekit#102，2026-11-02）：
这道门禁此前**不存在**——audit 只核对 BOM 快照的新鲜度 ⇒ 2026-11-01
pkg v0.31.1 发布后，**18 处地板漂移**（跨 13 个仓）只能靠人翻出来。
一道没人验证过的判据，很可能自己就是个假绿。

**反证怎么构造**（全部用临时目录造 fixture，**不联网**）：
  1. 地板落后 ⇒ 必须被判出来（且清单里带上仓/文件/依赖/两版号）；
  2. 地板齐平 ⇒ 不许误报；
  3. **版本按数字比**：v0.3.0 < v0.31.0（字符串排序会反过来）——
     地板 v0.3.0 对最新 v0.31.0 ⇒ 必须判落后；
  4. `// indirect` 行也要判（嵌套模块的依赖几乎全是 indirect 形态）；
  5. 嵌套模块的 go.mod 也要扫（bootstrap/nacos → concurrency 这类）；
  6. 自己的 tag 不与自己比；非矩阵模块不判；
  7. **变异自证**：把「按数字比」改成字符串比 ⇒ 第 3 条必须转红。

**origin/main 数据源**（matrix-tools#246，本地 bare 仓当 origin，fetch 不出网）：
  O1. 远端 main 地板已抬、本地工作树仍旧版 ⇒ 判据读 origin/main 得**零漂移**；
      同一棵本地工作树若被直接当数据源（#246 的旧 bug）⇒ 假红 1 条；
  O2. origin 读不到（fetch 失败）⇒ _origin_main_tree 返回 None（调用方按
      「未核对」判红，不许回退读本地工作树、也不许当零漂移）；
  O3. 嵌套模块的 go.mod 也在物化树里（判据扫得到）；
  O4. 陈旧检测：本地 main 落后 origin/main ⇒ _stale_local_main 记账；
  O5. **变异自证**：把门禁的数据源改回本地工作树 ⇒ O1 形态必须从绿转红。

用法：python3 scripts/audit_floor_gate_selftest.py
"""
from __future__ import annotations

import re
import subprocess
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
    这里按源码切片取 `parse_semver` + `scan_repo_floors` 两个纯函数。
    """
    src = src if src is not None else (HERE / "audit.py").read_text(encoding="utf-8")
    ns: dict = {"re": re, "MODULES": ("pkg", "concurrency", "hertz", "data",
                                      "middleware", "bootstrap")}
    # 两段分别切：parse_semver 一段、scan_repo_floors 一段
    # （中间隔着 check_nested_module_floors 与它的**调用**——那会真去读远端）。
    # 另外把模块级的 _FLOOR_REQ_RE 一并带进来（scan_repo_floors 依赖它）。
    for start, end in (("_FLOOR_REQ_RE = re.compile(", "def scan_repo_floors"),
                       ("def parse_semver", "def check_nested_module_floors"),
                       ("def scan_repo_floors", "def check_repo_go_mod_floors")):
        body = src[src.index(start):src.index(end)]
        exec(compile(body, "<audit-slice>", "exec"), ns)
    return ns["parse_semver"], ns["scan_repo_floors"]


def main() -> None:
    parse_semver, scan_repo_floors = load_scan_func()

    def scan(repo: str, root: Path, latest: dict[str, str]) -> list[str]:
        return scan_repo_floors(repo, root, lambda dep: latest.get(dep))

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)

        # ── 形态①：地板落后 ⇒ 判出来 ──
        root = base / "hertz"
        write_gomod(root / "go.mod", [("pkg", "v0.31.0")])
        out = scan("hertz", root, {"pkg": "v0.31.1"})
        check(len(out) == 1, f"落后地板必须被判出来：out={out}")
        check(out and "pkg v0.31.0" in out[0] and "v0.31.1" in out[0],
              f"清单必须带依赖与两版号：{out}")

        # ── 形态②：地板齐平 ⇒ 不许误报 ──
        out = scan("hertz", root, {"pkg": "v0.31.0"})
        check(not out, f"齐平地板不许误报：out={out}")

        # ── 形态③：版本按数字比（v0.9.0 < v0.10.0） ──
        # 字符串排序会把 "v0.9.0" 排在 "v0.10.0" **之后**（'9' > '1'）⇒ 漏判。
        # （取 v0.9.0/v0.10.0 这一对而不是 v0.3.0/v0.31.0：后者字符串序恰好与
        #   数字序一致，用它做变异自证**证不出**判据有没有牙齿。）
        root2 = base / "data"
        write_gomod(root2 / "go.mod", [("pkg", "v0.9.0")])
        out = scan("data", root2, {"pkg": "v0.10.0"})
        check(len(out) == 1,
              f"版本必须按数字比（v0.9.0 落后 v0.10.0）——字符串排序会反过来：out={out}")

        # ── 形态④：indirect 行也要判 ──
        root3 = base / "middleware"
        write_gomod(root3 / "go.mod", [("concurrency", "v0.23.2")], indirect=True)
        out = scan("middleware", root3, {"concurrency": "v0.23.3"})
        check(len(out) == 1, f"indirect 依赖行也要判：out={out}")

        # ── 形态⑤：嵌套模块的 go.mod 也要扫 ──
        root4 = base / "bootstrap"
        write_gomod(root4 / "nacos" / "go.mod", [("concurrency", "v0.23.2")])
        out = scan("bootstrap", root4, {"concurrency": "v0.23.3"})
        check(len(out) == 1, f"嵌套模块的 go.mod 也要扫：out={out}")
        check(out and "nacos/go.mod" in out[0], f"清单要带上嵌套模块的相对路径：{out}")

        # ── 形态⑥：自己的 tag 不与自己比；非矩阵模块不判 ──
        root5 = base / "pkg"
        write_gomod(root5 / "go.mod", [("pkg", "v0.31.0"), ("notamodule", "v0.0.1")])
        out = scan("pkg", root5, {"pkg": "v0.31.1"})
        check(not out, f"自己的 tag 不与自己比、非矩阵模块不判：out={out}")

        # ── 形态⑧：伪版本行要被看见且按基础段比较（#306） ──
        # post-tag 形态 v0.14.3-0.<ts>-<sha> 与无 tag 形态 v0.0.0-<ts>-<sha>：
        # 旧正则（三段数字+行尾）让它们整条隐身 ⇒ 门禁假绿。
        # （dep 取本自测假 MODULES 清单内的仓——known 判据只放行矩阵模块。）
        root6 = base / "data"
        write_gomod(root6 / "go.mod", [("concurrency", "v0.14.3-0.20261111201459-c0f8a3febb68")])
        out = scan("data", root6, {"concurrency": "v0.15.0"})
        check(len(out) == 1, f"伪版本行要被看见且落后要判（v0.14.3-0.… < v0.15.0）：out={out}")
        out = scan("data", root6, {"concurrency": "v0.14.3"})
        check(not out, f"伪版本基础段齐平不误报（v0.14.3-0.… ≥ v0.14.3）：out={out}")
        root7 = base / "middleware"
        write_gomod(root7 / "go.mod", [("pkg", "v0.0.0-20261111201459-c0f8a3febb68")])
        out = scan("middleware", root7, {"pkg": "v0.46.0"})
        check(len(out) == 1, f"无 tag 伪版本（v0.0.0-<ts>-<sha>）也要判：out={out}")

    # ── 形态⑦：变异自证 —— 数字比改成字符串比 ⇒ 形态③必须转红 ──
    src = (HERE / "audit.py").read_text(encoding="utf-8")
    check("parse_semver(ver), parse_semver(lt)" in src,
          "audit.py 的地板比较必须用 parse_semver（数字比）——字符串排序会把 "
          "v0.9.0 排在 v0.10.0 之后")
    # 变异：把**解析**去掉，直接比版本字符串（这正是「按字符串比」的经典写法）
    mut = src.replace("floor, cur = parse_semver(ver), parse_semver(lt)",
                      "floor, cur = ver, lt")
    check(mut != src, "变异未生效：找不到比较点")
    if mut != src:
        _, mut_scan = load_scan_func(mut)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            write_gomod(root / "go.mod", [("pkg", "v0.9.0")])
            out = mut_scan("data", root, lambda dep: "v0.10.0")
            check(not out,
                  f"变异自证：把数字比改成字符串比后，v0.9.0 vs v0.10.0 应被漏判 ⇒ "
                  f"原判据必须依赖数字比（本次变异下 out={out}）")

    # ── 数据源 = origin/main（#246）：远端地板已抬、本地工作树陈旧 ⇒ 不得假红 ──
    _origin_main_cases()

    if FAILURES:
        print("audit 地板门禁自测失败：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("audit 地板门禁自测通过：落后判出、齐平不误报、数字比、indirect 行、"
          "嵌套模块 go.mod、自身/非矩阵模块不判、数据源读 origin/main（本地陈旧不假红）、"
          "读不到 fail-loud，变异自证有效")


# ---------- #246：数据源 = origin/main ----------
#
# 夹具（全本地、不联网）：bare 仓当 origin；work = 普通 clone（工作树停在旧版）；
# fresh = 另一个 clone，把地板抬到「远端最新」后 push ⇒ origin/main 已抬、
# work 的工作树与本地 main 都停在旧版——正是 #246 实测的假红形态。


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{r.stderr.strip()[:200]}")
    return r.stdout.strip()


def _make_floor_fixture(td: Path, lifted: bool) -> Path:
    """work clone：本地工作树与 main 都停在地板 v0.31.0；
    lifted=True 时另经 fresh clone 把 origin/main 抬到 v0.31.1（work 不 pull）。"""
    origin = td / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main",
                    str(origin)], check=True)
    work = td / "work"
    # ★ 不用 clone：空 bare 仓 clone 出来的 unborn HEAD 分支名随 git 默认
    #   （CI 的 git 默认 master），而 checkout -B 在 unborn HEAD 上不保证生效
    #   ⇒ CI 实测 push 报「src refspec main does not match any」。
    #   git init -b main 显式钉死分支名，任何 git 版本行为一致。
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "config", "user.email", "selftest@example.invalid")
    _git(work, "config", "user.name", "selftest")
    (work / "go.mod").write_text(
        "module example.com/data\n\ngo 1.24\n\n"
        "require (\n\tgithub.com/oekit/pkg v0.31.0\n)\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "old floor")
    _git(work, "push", "-q", "-u", "origin", "main")
    if lifted:
        fresh = td / "fresh"
        subprocess.run(["git", "clone", "-q", str(origin), str(fresh)], check=True)
        _git(fresh, "config", "user.email", "selftest@example.invalid")
        _git(fresh, "config", "user.name", "selftest")
        (fresh / "nested" / "nacos").mkdir(parents=True)
        (fresh / "go.mod").write_text(
            "module example.com/data\n\ngo 1.24\n\n"
            "require (\n\tgithub.com/oekit/pkg v0.31.1\n)\n", encoding="utf-8")
        (fresh / "nested" / "nacos" / "go.mod").write_text(
            "module example.com/data/nacos\n\ngo 1.24\n\n"
            "require (\n\tgithub.com/oekit/concurrency v0.23.3 // indirect\n)\n",
            encoding="utf-8")
        _git(fresh, "add", "-A")
        _git(fresh, "commit", "-q", "-m", "lift floors to latest")
        _git(fresh, "push", "-q", "origin", "main")
    return work


def _load_audit_funcs(src: str) -> dict:
    """按名取 audit.py 的纯函数/门禁函数（不 import 整个 audit——那是跑全审计）。"""

    def grab(name: str) -> str:
        m = re.search(rf"^def {name}\(.*?(?=^\S)", src, re.S | re.M)
        if not m:
            raise AssertionError(f"audit.py 里找不到 {name}")
        return m.group(0)

    ns: dict = {"re": re, "subprocess": subprocess, "Path": Path,
                "_origin_trees": {}, "_origin_unreadable": {},
                "_stale_local_main": {}, "_origin_base_tmp": None}
    for name in ("_origin_main_tree", "scan_repo_floors", "check_repo_go_mod_floors"):
        exec(compile(grab(name), "<audit-slice>", "exec"), ns)
    m = re.search(r"^_FLOOR_REQ_RE = re\.compile\(.*?re\.M\)", src, re.S | re.M)
    exec(m.group(0), ns)
    exec(compile(grab("parse_semver"), "<audit-slice>", "exec"), ns)
    return ns


def _origin_main_cases() -> None:
    src = (HERE / "audit.py").read_text(encoding="utf-8")
    check("_origin_main_tree" in src, "audit.py 里找不到 _origin_main_tree（#246 修法被拆了？）")

    with tempfile.TemporaryDirectory() as td_s:
        td = Path(td_s)
        work = _make_floor_fixture(td, lifted=True)
        ns = _load_audit_funcs(src)
        # MODULES 要含 pkg/concurrency：scan_repo_floors 只判登记表里的依赖
        #（known = set(MODULES)），夹具的 require 指向 pkg 与 concurrency。
        ns["MODULES"] = ("pkg", "concurrency", "data")
        ns["BASE"] = str(td)
        ns["repo_dir"] = lambda repo, base: work
        ns["remote_latest"] = lambda dep: {"pkg": "v0.31.1", "concurrency": "v0.23.3"}.get(dep)
        issues: list[str] = []
        ns["check"] = lambda cond, msg: issues.append(msg) if not cond else None

        # ── O1：门禁读 origin/main ⇒ 远端已抬 ⇒ 零漂移（本地工作树仍旧版，不参与）──
        ns["check_repo_go_mod_floors"]()
        check(not issues,
              f"O1: origin/main 地板已抬 ⇒ 门禁必须零漂移（本地工作树陈旧不得假红，"
              f"#246）——实得 issues={issues}")
        # 反证：同一棵本地工作树直接当数据源（#246 的旧形态）⇒ 假红 1 条
        tree_out = ns["scan_repo_floors"]("data", work, ns["remote_latest"])
        check(len(tree_out) == 1 and "v0.31.0" in tree_out[0],
              f"O1 反证：本地工作树直接当数据源必须红 1 条（证明新旧判据有别）：{tree_out}")

        # ── O3：嵌套模块的 go.mod 也在物化树里 ──
        tree = ns["_origin_main_tree"]("data", work)
        check(tree is not None, "O3: 物化树不应为 None")
        if tree is not None:
            nested = tree / "nested" / "nacos" / "go.mod"
            check(nested.is_file() and "v0.23.3" in nested.read_text(encoding="utf-8"),
                  "O3: 嵌套模块的 go.mod 必须随 origin/main 一起物化")
            out = ns["scan_repo_floors"]("data", tree, ns["remote_latest"])
            check(not out, f"O3: 物化树上齐平地板不许误报：{out}")

        # ── O4：陈旧检测记账（本地 main 落后 origin/main）──
        check(ns["_stale_local_main"].get("data", 0) >= 1,
              f"O4: 本地 main 落后必须被记账（提示先 pull）——实得 {ns['_stale_local_main']}")

        # ── O5：变异自证 —— 门禁数据源改回本地工作树 ⇒ O1 形态必须从绿转红 ──
        mut = src.replace("root = _origin_main_tree(repo, repo_dir(repo, BASE))",
                          "root = repo_dir(repo, BASE)")
        check(mut != src, "O5 变异未生效：找不到门禁的数据源行")
        if mut != src:
            mut_ns = _load_audit_funcs(mut)
            mut_ns.update(MODULES=("pkg", "concurrency", "data"), BASE=str(td),
                          repo_dir=lambda repo, base: work,
                          remote_latest=ns["remote_latest"])
            mut_issues: list[str] = []
            mut_ns["check"] = lambda cond, msg: mut_issues.append(msg) if not cond else None
            mut_ns["check_repo_go_mod_floors"]()
            check(bool(mut_issues) and "v0.31.0" in mut_issues[0],
                  f"O5: 数据源退回本地工作树 ⇒ 必须假红（证明原判据真的在读 origin/main）"
                  f"——实得 issues={mut_issues}")

    # ── O2：origin 读不到 ⇒ 返回 None（fail-loud，不许回退工作树、不许当零漂移）──
    with tempfile.TemporaryDirectory() as td_s:
        td = Path(td_s)
        work = _make_floor_fixture(td, lifted=False)
        subprocess.run(["git", "-C", str(work), "remote", "set-url", "origin",
                        str(td / "no-such-origin.git")], check=True)
        ns = _load_audit_funcs(src)
        tree = ns["_origin_main_tree"]("data", work)
        check(tree is None and "data" in ns["_origin_unreadable"],
              f"O2: origin 读不到必须返回 None 并记账原因——实得 tree={tree}, "
              f"unreadable={ns['_origin_unreadable']}")


if __name__ == "__main__":
    main()
