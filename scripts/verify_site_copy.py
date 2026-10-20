#!/usr/bin/env python3
"""站点内容门禁：文档站的「模块计数」与「llms.txt 模块覆盖」必须与矩阵一致。

## 为什么要有这个（2026-10-20 实测）

`oekit.github.io` 的 CI 只有一个 job：`npm ci` + `npm run build`（VitePress 构建内建
死链检查）。**站点内容正确性零门禁**——矩阵说 24 个模块、llms.txt 覆盖 24 个模块，
都只是「碰巧是对的」：

  - 把 `docs/index.md` 的「24 个独立发版的 Go 模块」改成 23 → CI 全绿；
  - 从 `docs/public/llms.txt` 删掉一个模块条目 → CI 全绿（audit.py 对 llms.txt
    只做**存在性**检查，不查内容）。

本脚本就是补这两条。

## 判据必须独立（关键）

被检对象 = 站点仓里**手写**的文案与 llms.txt（它们没有生成器）。
期望值来自两处**与被检对象无关**的事实：

  - 矩阵的模块登记表（`matrix.py` 的 `MODULES`）——决定「哪些模块该被提到」；
  - **文件系统里的真实 go.mod**（给了 `--repos-dir` 就扫）——决定「实际存在几个
    根模块 / 几个嵌套模块 / 是不是有矩阵没登记的幽灵根模块」。

判据不复用被检对象自己的口径：站点文案没有任何生成器，所以这是纯漂移检查；
真正的独立 oracle 在 `--repos-dir` 那一路（matrix-tools 的 audit.yml 会跑）。

## 三个口径不一样，别选一个当「唯一正确值」

| 口径 | 实测值 | 含义 |
|---|---|---|
| 根矩阵模块数 | 24 | `len(MODULES)`；站点文案用的就是这个，与「矩阵总览（24 活跃模块）」同口径 |
| 根 go.mod 数 | 25 | 物理事实：24 个矩阵模块仓 + 门面 BOM 仓 `oekit` |
| 嵌套模块数 | 7 | 深度 ≥2 的 go.mod（bootstrap/nacos、logx/hlog …），各自独立发版 |
| 发版单元总数 | 32 | 根矩阵模块 + 嵌套模块 + **伞仓 oekit**（伞仓有独立 tag 与 GitHub Release，`go get github.com/oekit/oekit@latest` 是一次独立的版本获取——按「可独立 go get 的发版单元」口径必须计入） |

报错文案会把这几个数一起打出来，读者不必再猜哪个是哪个。

用法:
    verify_site_copy.py --site <站点仓目录> [--matrix modules.json] [--repos-dir <各仓父目录>]

退出码: 0 = 通过；1 = 有漂移。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from matrix import MODULES  # noqa: E402
from gen_tables import BOM_REPO, COUNT_UNITS, count_units  # noqa: E402  口径单一真相

_COUNT_RE = re.compile(
    r"(\d+)\s*个\s*(" + "|".join(re.escape(u) for u in
                                 sorted(COUNT_UNITS, key=len, reverse=True)) + r")")

# 泛用单位「模块」的上下文锚：这一行在谈「矩阵规模」而不是普通行文。
_MATRIX_CTX = re.compile(r"矩阵|独立发版|全矩阵|全部模块")

# llms.txt 的模块条目形态：`- [name](https://github.com/oekit/name): 描述`
_LLMS_ENTRY_RE = re.compile(r"^-\s*\[([A-Za-z0-9_.\-]+)\]\(https://github\.com/oekit/[^)]+\)\s*[:：]",
                             re.M)

# 站点仓里要扫的目录/文件后缀。dist（构建产物）、node_modules、cache 一律排除——
# 里面是同一份文案的编译副本，扫了会重复报错。
_SKIP_DIRS = {"dist", "node_modules", "cache", ".git"}


def scan_site_files(site: Path) -> list[Path]:
    """站点仓里所有参与计数检查的 .md/.txt（排除构建产物与依赖目录）。"""
    out = []
    for f in sorted(site.rglob("*")):
        if not f.is_file() or f.suffix not in (".md", ".txt"):
            continue
        if any(p in _SKIP_DIRS for p in f.parts):
            continue
        out.append(f)
    return out


def _fmt_counts(counts: dict) -> str:
    parts = [f"根矩阵模块 {counts['matrix_modules']}"]
    if counts.get("root_gomods") is not None:
        parts.append(f"根 go.mod {counts['root_gomods']}（含门面 BOM 仓 {BOM_REPO}）")
    if counts.get("nested_modules") is not None:
        parts.append(f"嵌套模块 {counts['nested_modules']}")
    if counts.get("release_units") is not None:
        parts.append(f"发版单元 {counts['release_units']}")
    return " / ".join(parts)


def check_counts(site: Path, counts: dict) -> list[str]:
    """站点文案里的每个模块计数都必须等于对应口径。"""
    problems: list[str] = []
    for f in scan_site_files(site):
        text = f.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            for m in _COUNT_RE.finditer(line):
                num = int(m.group(1))
                unit = m.group(2)
                key = COUNT_UNITS[unit]
                if key == "matrix_modules" and unit == "模块" and not _MATRIX_CTX.search(line):
                    # 泛用单位「模块」只在明确谈矩阵时才校验：指南里写
                    # 「这 3 个模块」是普通行文，不是矩阵规模，误报会让人绕过门禁。
                    continue
                want = counts.get(key)
                if want is None:
                    # 口径没算出来（没给 --repos-dir）→ 判不了 ≠ 不对，不红。
                    continue
                if num != want:
                    problems.append(
                        f"{f.relative_to(site)}:{i} 「{num} 个{unit}」与矩阵不符"
                        f"（应为 {want}；{_fmt_counts(counts)}）")
    return problems


def check_llms_coverage(site: Path) -> list[str]:
    """llms.txt 的模块条目集合必须与矩阵登记的模块集合**完全相等**。

    这条补的是 audit.py 的缺口：它对 llms.txt 只做存在性检查，删掉一个条目没人管。
    """
    problems: list[str] = []
    llms = site / "docs" / "public" / "llms.txt"
    if not llms.is_file():
        return [f"缺 {llms.relative_to(site)}（llms.txt 是机器可读入口，必须在）"]
    text = llms.read_text(encoding="utf-8", errors="ignore")
    # 只取「## 模块」段里的条目：文档段里也有 github 链接，不是模块清单。
    m = re.search(r"^##\s+模块\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return ["llms.txt 里找不到「## 模块」段（模块清单段被改名或删了）——"
                "机器按 llms.txt 装包会漏整个矩阵"]
    section = m.group(1)
    found = set(_LLMS_ENTRY_RE.findall(section))
    want = set(MODULES)
    missing = sorted(want - found)
    extra = sorted(found - want)
    if missing:
        problems.append(f"llms.txt 的「## 模块」段缺 {len(missing)} 个模块条目：{missing}"
                        f"（消费方按 llms.txt 装包会漏）")
    if extra:
        problems.append(f"llms.txt 的「## 模块」段有 {len(extra)} 个矩阵里没有的条目：{extra}")
    return problems


def check_ghost_roots(repos_dir: Path, counts: dict) -> list[str]:
    """独立 oracle：根 go.mod 必须都能在矩阵里找到名字（否则是矩阵外的幽灵模块）。"""
    problems: list[str] = []
    for p in sorted(repos_dir.iterdir()):
        if not p.is_dir() or not (p / "go.mod").is_file():
            continue
        if p.name in MODULES or p.name == BOM_REPO:
            continue
        problems.append(f"{p.name}/ 有根 go.mod 但不在矩阵 MODULES 里"
                        f"（矩阵外的幽灵根模块；{_fmt_counts(counts)}）")
    for m in MODULES:
        if not (repos_dir / m / "go.mod").is_file():
            if (repos_dir / m).is_dir():
                problems.append(f"{m}: 目录在但 go.mod 不存在（模块仓坏了；{_fmt_counts(counts)}）")
            # 目录都没检出 ≠ 漂移，不报（否则本地少 clone 一个就假红）
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="站点内容门禁：模块计数口径 + llms.txt 模块覆盖")
    ap.add_argument("--site", required=True, help="站点仓目录（oekit.github.io）")
    ap.add_argument("--repos-dir", default="",
                    help="各模块仓的父目录（CI 里各仓 checkout 到 repos/）；"
                         "给了才做「真实 go.mod 数」的独立 oracle")
    args = ap.parse_args()

    site = Path(args.site).resolve()
    if not site.is_dir():
        print(f"❌ 站点目录不存在: {site}")
        return 1

    repos_dir = Path(args.repos_dir).resolve() if args.repos_dir else None
    counts = count_units(repos_dir)

    problems: list[str] = []
    problems += check_counts(site, counts)
    problems += check_llms_coverage(site)
    if repos_dir is not None:
        problems += check_ghost_roots(repos_dir, counts)

    if problems:
        print(f"❌ 站点内容门禁失败：{len(problems)} 处\n")
        for p in problems:
            print(f"  - {p}")
        print(f"\n口径：{_fmt_counts(counts)}")
        print("修法：改站点文案里的数字到上面的口径值；或跑 matrix-tools 的生成器重生成。")
        return 1

    print(f"✅ 站点内容门禁通过（口径：{_fmt_counts(counts)}；llms.txt 覆盖 {len(MODULES)} 个模块）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
