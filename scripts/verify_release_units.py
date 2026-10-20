#!/usr/bin/env python3
"""发版单元门禁：llms.txt 必须覆盖全部发版单元（根模块 + 嵌套模块），且嵌套模块各自有 go get 安装行。

## 为什么要有这个（2026-10-20 实测）

对外口径分裂成 21 / 24 / 32 三种，而 llms.txt —— **AI Agent 唯一会抓的机器可读索引** ——
里 7 个嵌套模块**既没有独立条目、也没有 `go get` 安装行**。Agent 读到
「Sink 在嵌套模块 logx/sink」却找不到安装行，就装不上。

而嵌套模块恰是**零依赖隔离**的载体（logx/sink 隔离 lumberjack、bootstrap/nacos 隔离
约 50 个阿里云 SDK）。它们的价值就是「可选装」——llms.txt 不给安装行 = 这个价值对
Agent 不可见。

verify_site_copy.py 补的是**根模块条目集合**（llms.txt「## 模块」段 == MODULES）；
本脚本补的是**发版单元全集**（根 24 + 嵌套 7）与**嵌套模块的可安装性**。

## 判据必须独立（关键）

被检对象 = 站点仓里**手写**的 llms.txt（它**没有生成器**——已全量核实：
matrix-tools/scripts/ 下无任何写 llms.txt 的脚本，其历史全是手写提交）。
期望值来自三处与被检对象无关的事实：

  1. **文件系统**：每个嵌套模块必须在 `<repo>/<path>/go.mod` 真实存在，且有真实 tag
     （独立发版的物理证据）——这是独立 oracle；
  2. **矩阵登记表**（matrix.py 的 MODULES / NESTED_MODULES）——声明「应该有什么」；
  3. **本脚本自己渲染的规范段落**——与站点里的段落逐字节比对，锁住格式漂移。

第 1 条是真正独立的 oracle：哪怕 matrix.py 的 NESTED_MODULES 写错了，
文件系统这一路仍会红。判据**不复用被检对象自己的口径**。

## 两条 oracle 都要有（缺一即有共同盲区）

- **oracle A（正确性）**：解析 llms.txt 全文，抽出全部条目形态，
  断言 24 个根模块名 + 7 个嵌套模块名**各至少出现一次**，且 7 个嵌套模块
  **各自都有一行 `go get github.com/oekit/<module>`**。不依赖渲染结果。
- **oracle B（一致性）**：渲染规范「## 嵌套模块」段，与站点里的该段逐字节比对。
  抓的是格式漂移（少个安装行、描述改了、链接写错）。

用法:
    verify_release_units.py --site <站点仓目录> [--repos-dir <各仓父目录>]
    verify_release_units.py --emit          # 打印规范段落（供写回 llms.txt）

退出码: 0 = 通过；1 = 有漂移。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from matrix import MODULES, NESTED_MODULES  # noqa: E402

# 根模块条目形态：`- [name](https://github.com/oekit/name): 描述`
_ROOT_ENTRY_RE = re.compile(r"^-\s*\[([A-Za-z0-9_.\-]+)\]\(https://github\.com/oekit/[^)]+\)\s*[:：]",
                            re.M)
# 嵌套模块条目形态：`- [repo/path](https://github.com/oekit/repo/tree/main/path): 描述`
_NESTED_ENTRY_RE = re.compile(
    r"^-\s*\[([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\]"
    r"\(https://github\.com/oekit/[A-Za-z0-9_.\-]+/tree/main/[A-Za-z0-9_.\-]+\)\s*[:：]",
    re.M)
# 安装行：`go get github.com/oekit/logx/sink@latest`
# 抓取**完整 module 路径**（含 github.com/oekit/ 前缀），与 NESTED_MODULES 的
# `module` 字段同形态——两边必须能直接比对，中间不做前缀剥离。
_GO_GET_RE = re.compile(r"go\s+get\s+(github\.com/oekit/[A-Za-z0-9_.\-/]+?)(?:@|`)")

# 嵌套模块段落标题——渲染与比对都锚在这一行，段落被改名即漂移。
_NESTED_HEADING = "## 嵌套模块（可选装 · 依赖隔离）"


def _semver_key(v: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _remote_tags(repo: str) -> list[str]:
    """远端 tag 列表。

    必须走 **远端**（ls-remote）而不是本地 `git tag`：CI 里各仓是 fresh clone，
    本地没有 tag——用本地 tag 会让规范渲染在 CI 与本地**得出不同结果**，
    门禁就成了「本地绿、CI 红」。
    """
    r = subprocess.run(["git", "ls-remote", "--tags", f"https://github.com/oekit/{repo}.git"],
                       capture_output=True, text=True)
    return [ln.split("refs/tags/")[-1].strip() for ln in r.stdout.splitlines()
            if ln.strip() and not ln.rstrip().endswith("^{}")]


def _latest_root_tag(repo: str) -> str | None:
    """仓的最新根 tag（形如 v0.7.1）。"""
    tags = [t for t in _remote_tags(repo)
            if "/" not in t and re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return max(tags, key=_semver_key) if tags else None


def _latest_nested_tag(repo: str, path: str) -> str | None:
    """嵌套模块的最新 tag（形如 nacos/v0.3.5 → v0.3.5）。"""
    pre = f"{path}/"
    tags = [t.split("/", 1)[1] for t in _remote_tags(repo)
            if t.startswith(pre) and re.fullmatch(r"v\d+\.\d+\.\d+", t.split("/", 1)[1])]
    return max(tags, key=_semver_key) if tags else None


def render_nested_section() -> str:
    """渲染规范的「## 嵌套模块」段。

    这是 llms.txt 那一段的**唯一产出源**：站点里的段落必须与它逐字节一致
    （oracle B）。写成生成器而不是手改产物——llms.txt 整体没有生成器，
    但这一段是机械的，机械的就不该手写。

    版本信息**从 git tag 推导**，不手写：
      - `版本`：该嵌套模块自己的最新 tag；
      - `对应父仓`：父仓的**最新** tag（= 当前与哪个父仓版本对应）。
    ⚠️ 曾手写过「随父仓 X 拆出」的历史对应关系，实测**不可靠**：7 条里 2 条与
    父仓 CHANGELOG 对不上、2 条根本没有 CHANGELOG 依据——手挑值不可复核。
    故只保留两项**可从 tag 客观推导**的信息。
    """
    lines = [
        _NESTED_HEADING,
        "",
        "> 嵌套模块是**独立发版**的 Go module（各自有 tag），职责是**依赖隔离**而非目录组织：",
        "> 只 import 根模块的消费者**不会**被拉入这些重依赖；需要时才单独 `go get`。",
        "",
    ]
    for m in NESTED_MODULES:
        name = f"{m['repo']}/{m['path']}"
        url = f"https://github.com/oekit/{m['repo']}/tree/main/{m['path']}"
        lines.append(f"- [{name}]({url}): {m['desc']}")
        lines.append(f"  - 安装：`go get {m['module']}@latest`")
        ntag = _latest_nested_tag(m["repo"], m["path"])
        ptag = _latest_root_tag(m["repo"])
        if ntag and ptag:
            lines.append(f"  - 版本：`{ntag}` · 对应父仓 `{m['repo']} {ptag}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _nested_units() -> list[tuple[str, str]]:
    """(条目名, import 路径) 列表，名字用 `repo/path` 形态。"""
    return [(f"{m['repo']}/{m['path']}", m["module"]) for m in NESTED_MODULES]


def check_filesystem_oracle(repos_dir: Path | None) -> list[str]:
    """oracle A-1（独立 oracle）：嵌套模块必须在文件系统里真实存在且有真实 tag。

    哪怕 matrix.py 的 NESTED_MODULES 写错了，这一条仍会红——它不依赖任何声明。
    """
    if repos_dir is None:
        return []
    problems: list[str] = []
    for name, _mod in _nested_units():
        repo, path = name.split("/", 1)
        gomod = repos_dir / repo / path / "go.mod"
        if not gomod.is_file():
            problems.append(f"嵌套模块 {name} 在文件系统里不存在（{gomod}）——"
                            f"声明了但实际没有这个 go.mod")
            continue
        try:
            tags = subprocess.run(["git", "tag", "--list", f"{path}/v*"], cwd=repos_dir / repo,
                                  capture_output=True, text=True, timeout=30).stdout.split()
        except Exception:  # noqa: BLE001 — git 不可用时不该让门禁崩
            continue
        if not tags:
            problems.append(f"嵌套模块 {name} 没有任何 {path}/vX.Y.Z tag——"
                            f"「独立发版」的说法不成立")
    return problems


def check_llms_units(llms_text: str) -> list[str]:
    """oracle A-2：llms.txt 必须覆盖全部发版单元，嵌套模块各自有 go get 安装行。

    不依赖 render_nested_section() 的输出——自己解析 llms.txt。
    """
    problems: list[str] = []
    root_found = set(_ROOT_ENTRY_RE.findall(llms_text))
    nested_found = set(_NESTED_ENTRY_RE.findall(llms_text))
    all_found = root_found | nested_found

    missing_root = sorted(set(MODULES) - all_found)
    if missing_root:
        problems.append(f"llms.txt 缺 {len(missing_root)} 个根模块条目：{missing_root}"
                        f"（Agent 按 llms.txt 装包会漏）")
    missing_nested = sorted({n for n, _ in _nested_units()} - all_found)
    if missing_nested:
        problems.append(f"llms.txt 缺 {len(missing_nested)} 个嵌套模块条目：{missing_nested}"
                        f"（嵌套模块的「可选装」价值对 Agent 不可见）")

    # 嵌套模块各自的 go get 安装行——**逐个**要求，不接受「有一段提过就算」
    install_lines = {m.group(1) for m in _GO_GET_RE.finditer(llms_text)}
    for name, mod in _nested_units():
        if mod not in install_lines:
            problems.append(f"llms.txt 缺嵌套模块 {name} 的 go get 安装行"
                            f"（`go get {mod}@latest`）——Agent 读到条目也装不上")
    return problems


def check_section_consistency(llms_text: str) -> list[str]:
    """oracle B：站点的嵌套模块段必须与渲染结果逐字节一致。"""
    want = render_nested_section()
    if _NESTED_HEADING not in llms_text:
        return [f"llms.txt 缺「{_NESTED_HEADING}」段——"
                f"7 个嵌套模块没有独立条目段（跑 --emit 取规范文本）"]
    m = re.search(re.escape(_NESTED_HEADING) + r"\n(.*?)(?=^##\s|\Z)", llms_text, re.M | re.S)
    got = _NESTED_HEADING + "\n" + (m.group(1) if m else "")
    if got.rstrip() != want.rstrip():
        return ["llms.txt 的「嵌套模块」段与规范渲染不一致（描述/安装行/链接漂移）"
                "——跑 `verify_release_units.py --emit` 取规范文本逐字节替换"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="发版单元门禁：llms.txt 覆盖全部发版单元 + 嵌套模块可安装")
    ap.add_argument("--site", help="站点仓目录（用于读 docs/public/llms.txt）")
    ap.add_argument("--repos-dir", help="各仓父目录（用于文件系统独立 oracle）")
    ap.add_argument("--emit", action="store_true", help="打印规范「## 嵌套模块」段")
    args = ap.parse_args()

    if args.emit:
        print(render_nested_section(), end="")
        return 0

    if not args.site:
        ap.error("需要 --site <站点仓目录>")

    site = Path(args.site).resolve()
    llms = site / "docs" / "public" / "llms.txt"
    if not llms.is_file():
        print(f"❌ 缺 {llms}（llms.txt 是机器可读入口，必须在）")
        return 1
    text = llms.read_text(encoding="utf-8", errors="ignore")

    problems: list[str] = []
    repos_dir = Path(args.repos_dir).resolve() if args.repos_dir else None
    problems += check_filesystem_oracle(repos_dir)
    problems += check_llms_units(text)
    problems += check_section_consistency(text)

    total = len(MODULES) + len(NESTED_MODULES)
    if problems:
        print(f"❌ 发版单元门禁未通过（应覆盖 {total} 个发版单元"
              f"= {len(MODULES)} 根模块 + {len(NESTED_MODULES)} 嵌套模块）：")
        for p in problems:
            print(f"   - {p}")
        return 1
    print(f"✅ 发版单元门禁通过（{total} 个发版单元 = {len(MODULES)} 根 + "
          f"{len(NESTED_MODULES)} 嵌套；{len(NESTED_MODULES)} 条嵌套安装行齐全）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
