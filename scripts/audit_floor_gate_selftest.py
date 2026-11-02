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

用法：python3 scripts/audit_floor_gate_selftest.py
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

    if FAILURES:
        print("audit 地板门禁自测失败：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("audit 地板门禁自测通过：落后判出、齐平不误报、数字比、indirect 行、"
          "嵌套模块 go.mod、自身/非矩阵模块不判，变异自证有效")


if __name__ == "__main__":
    main()
