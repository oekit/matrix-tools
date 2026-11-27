#!/usr/bin/env python3
"""BOM 主仓 README 计数门禁：badge / prose / 架构总览表 / 合数 / 易变数，全部比对单源。

## 为什么要有这个（2026-11-27 演进评审 P0，matrix-tools#339）

`matrix.py` 宣称是计数唯一事实源，但 README 的**数字文案**此前不在任何生成器与
门禁管辖内——单源机制对它完全失效。实测漂移（kitex-examples 11-23 移出 MODULES、
kitx 改名 authx、pkg 子包 23→38 三轮变更后 README 全没跟上）：

  - badge `modules-24`（应 23）；
  - prose「24 个活跃模块仓库」（应 23）；
  - 架构总览表仍列 kitex-examples（非矩阵模块）、仍链 kitx（已改名 authx）；
  - pkg「23 合 1」（真相源 38）；
  - 「108 条 breaking 记录」——易变数硬编码，实测已漂到 199。

站点侧同类判据早已存在（verify_site_copy.check_counts 守站点文案），BOM 主仓
README 是最后一个没人守的对外计数面。

## 判据（每条锚在唯一事实上，不锚在猜测上）

  1. **badge 在场且正确**：`badge/modules-N-` 的 N == len(MODULES)。
     缺 badge 也红（承诺在场——计数声明不许静默消失，同 dep_gate 承诺句纪律）。
  2. **prose 在场且正确**：「N 个活跃模块仓库」的 N == len(MODULES)。
  3. **架构总览表集合**：`## 架构总览` 区块内表格行里的 `github.com/oekit/<name>`
     链接集合必须与 MODULES **完全相等**（多一个 kitex-examples、少一个 authx
     都判红）。只认表格行——兼容性表的旧路径链接不在该区块，不受影响。
  4. **合数**：README 里每个「（N 合 1）」按**同行最近的模块链接**归属（与站点
     check_subpackage_counts 同款「最近归属」纪律），N 必须等于提取层真相。
  5. **易变数禁写**：「N 条 breaking」这类**随发版漂移的数**不许硬编码在 README
     ——出现即红，提示改定性表述（数字由 versions.json / matrix.go 自带）。

## 判据必须独立（关键）

被检对象 = BOM 主仓 README（手写面，无生成器）。期望值来自两处与被检对象无关
的事实：`matrix.MODULES`（登记表）与 `subpkg_claims.truth()`（从各仓真实子包
提取）。判据不复用 README 自己的任何口径。

## 退出码契约（生产方定义，audit 按 classify 取用，同 dep_gate 模式）

    0 = EXIT_OK          全部计数与单源一致
    1 = EXIT_VIOLATION   真漂移（badge/prose/表集合/合数/易变数任一违例）
    2 = EXIT_UNREADABLE  判不了（README 或事实源读不到）——判不了 ≠ 通过（#79）

用法:
    python3 scripts/verify_bom_readme.py                 # 对真 README 跑（需 MATRIX_BASE 布局）
    python3 scripts/verify_bom_readme.py --json          # 机器可读输出（audit 集成用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # import matrix（唯一事实源）

import matrix  # noqa: E402

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNREADABLE = 2

# badge：shields.io 形态 modules-<N>-
_BADGE_RE = re.compile(r"badge/modules-(\d+)-")
# prose：「24 个活跃模块仓库」（COUNT_UNITS 的 matrix_modules 单位）
_PROSE_RE = re.compile(r"(\d+)\s*个\s*活跃模块仓库")
# 架构总览区块
_SECTION_RE = re.compile(r"^##\s+架构总览\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)
# 表格行里的模块链接
_LINK_RE = re.compile(r"https://github\.com/oekit/([A-Za-z0-9_-]+)")
# 合数声明
_SUBPKG_RE = re.compile(r"（(\d+)\s*合\s*1）")
# 易变数：N 条 breaking（README 不许硬编码）
_VOLATILE_RE = re.compile(r"\d+\s*条\s*breaking", re.I)


def _module_mentions(line: str) -> list[tuple[int, int, str]]:
    """这一行里所有模块链接提及 (链接起点, 链接终点, 模块名)——供「最近归属」用。

    区间必须是**整个 `[name](url)` 链接**的跨度：只用 URL 起点算距离会被长 URL
    干扰（实测：pkg 的合数被归给下一個 concurrency——URL 起点离声称更「近」）。
    """
    return [(m.start(), m.end(), m.group(1)) for m in _LINK_RE.finditer(line)
            if m.group(1) in set(matrix.MODULES)]


def _attribute_claim(line: str, c_start: int, c_end: int) -> str | None:
    """把「（N 合 1）」归属到同行**区间间隙最小**的模块链接。

    与站点 check_subpackage_counts._module_of_claim 同款纪律：唯一性判据会把
    「同行多模块」误判成无法归属；距离用区间间隙（不是起点差）避免链接 URL
    长度干扰——链接在声称前用 claim_start - link_end，声称后用 link_start - claim_end。
    """
    mentions = _module_mentions(line)
    best, best_gap = None, None
    for l_start, l_end, name in mentions:
        gap = c_start - l_end if l_end <= c_start else l_start - c_end
        if best_gap is None or gap < best_gap:
            best, best_gap = name, gap
    return best


def check_bom_readme(text: str, matrix_modules: int, subpkg_truth: dict) -> list[str]:
    """纯函数判据：README 文本 + 单源计数 + 提取层真相 → 问题列表（空=全绿）。"""
    problems: list[str] = []

    # 1. badge 在场且正确
    m = _BADGE_RE.search(text)
    if not m:
        problems.append("README 缺 modules badge（计数承诺不许静默消失，"
                        f"应为 badge/modules-{matrix_modules}-）")
    elif int(m.group(1)) != matrix_modules:
        problems.append(f"badge modules-{m.group(1)} ≠ 单源 {matrix_modules}"
                        f"（len(MODULES)，跑本仓 verify_bom_readme 前先对 matrix.py）")

    # 2. prose 在场且正确
    m = _PROSE_RE.search(text)
    if not m:
        problems.append("README 缺「N 个活跃模块仓库」计数句（承诺在场）")
    elif int(m.group(1)) != matrix_modules:
        problems.append(f"prose「{m.group(0)}」≠ 单源 {matrix_modules}（len(MODULES)）")

    # 3. 架构总览表集合 == MODULES（双向判差）
    sec = _SECTION_RE.search(text)
    if not sec:
        problems.append("README 缺「## 架构总览」区块（架构表是计数承诺的载体）")
    else:
        table_lines = [l for l in sec.group(1).splitlines() if l.startswith("|")]
        found: set[str] = set()
        for l in table_lines:
            found.update(_LINK_RE.findall(l))
        want = set(matrix.MODULES)
        extra = sorted(found - want)
        missing = sorted(want - found)
        if extra:
            problems.append(f"架构总览表多出非矩阵模块链接：{extra}"
                            f"（不在 matrix.MODULES——已删仓/改名前旧路径/矩阵外示例仓）")
        if missing:
            problems.append(f"架构总览表缺矩阵模块链接：{missing}")

    # 4. 合数 = 提取层真相
    for i, line in enumerate(text.splitlines(), 1):
        for m in _SUBPKG_RE.finditer(line):
            num = int(m.group(1))
            mod = _attribute_claim(line, m.start(), m.end())
            if mod is None:
                problems.append(f"README:{i} 「{m.group(0)}」同行无可归属模块"
                                f"（写法应紧挨模块链接，如 [pkg](…)（38 合 1））")
                continue
            t = subpkg_truth.get(mod)
            if t is None:
                problems.append(f"README:{i} 「{m.group(0)}」归属 {mod}，但提取层"
                                f"没有它的「N 合 1」事实（DESC 未声明？）")
            elif num != t["expected"]:
                problems.append(f"README:{i} 「{m.group(0)}」与事实不符：{mod} 应为"
                                f" {t['expected']} 合 1（出处：{t['source']}）")

    # 5. 易变数禁写（N 条 breaking 随发版漂移，不许硬编码）
    for i, line in enumerate(text.splitlines(), 1):
        m = _VOLATILE_RE.search(line)
        if m:
            problems.append(f"README:{i} 硬编码易变数「{m.group(0)}」——breaking "
                            f"条数随每次发版漂移（108→实测 199），改定性表述；"
                            f"数字由 versions.json / matrix.go 自带")

    return problems


def classify(rc: int, out: str) -> tuple[str, str]:
    """退出码 → (kind, message)。kind ∈ {ok, violation, unreadable}。"""
    if rc == EXIT_OK:
        return "ok", "BOM README 计数与单源一致"
    if rc == EXIT_VIOLATION:
        return "violation", "BOM README 计数漂移（matrix-tools#339）：" + out.strip()
    return "unreadable", out.strip() or "README 或事实源读不到（判不了 ≠ 通过）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BOM 主仓 README 计数门禁")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--readme", default=None, help="README 路径（缺省从 MATRIX_BASE 布局推）")
    args = ap.parse_args(argv)

    # 事实源：README 文件 + subpkg 提取层。读不到 ⇒ EXIT_UNREADABLE（#79）。
    try:
        if args.readme:
            readme = Path(args.readme)
        else:
            readme = matrix.repo_dir(matrix.BOM_REPO) / "README.md"
        text = readme.read_text(encoding="utf-8")
        import subpkg_claims  # noqa: E402  提取层（与站点/DESC 自测同一份事实）
        truth = subpkg_claims.truth(str(readme.parent.parent))
    except Exception as e:  # noqa: BLE001 — 读不到必须显式红，不许静默放过
        msg = f"读不到 README 或提取层事实：{e}"
        if args.json:
            print(json.dumps({"kind": "unreadable", "problems": [msg]},
                             ensure_ascii=False))
        else:
            print(f"❌ {msg}")
        return EXIT_UNREADABLE

    problems = check_bom_readme(text, len(matrix.MODULES), truth)
    if args.json:
        print(json.dumps({"kind": "violation" if problems else "ok",
                          "problems": problems}, ensure_ascii=False, indent=1))
    elif problems:
        print(f"❌ BOM README 计数漂移 {len(problems)} 项：")
        for p in problems:
            print(f"  · {p}")
    else:
        print("✓ BOM README 计数与单源一致（badge/prose/架构表/合数/无易变数）")
    return EXIT_VIOLATION if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
