#!/usr/bin/env python3
"""llms.txt 模块条目里的「反引号标识符」是否真的存在于**本仓**源码里。

## 为什么要有这个（2026-10-20 实测）

llms.txt 是 AI Agent 唯一会抓的机器可读索引。实测发现它的 hertz 条目写着
``WithReadyAddr``——**hertz 没有这个导出函数**：

- kitex 才有 ``WithReadyAddr(addr)``：kitex server 没有就绪 introspection，
  探活目标必须显式给出；
- hertz 的 ``RunServerOption`` 只有 ``WithOnReady`` 与 ``WithReadyProbeTimeout``，
  ``runServerConfig`` 里也只有 onReady / probeTimeout / probeEnabled 三个字段，
  探活目标固定是 server 自动取到的全部 HostPort。

照着 llms.txt 写代码的人会拿到 ``undefined: hertz.WithReadyAddr``。
这条 DESC 还同时进了 hertz/README.md、hertz/AGENTS.md 与全矩阵 24 仓的矩阵表。

这类错误的特点是：**机器判不了语义，但能判「这个名字在本仓源码里不存在」**。

## 判据

约定：llms.txt 的模块条目里，**反引号**标出的标识符指的是**本仓**的导出 API
（提到别仓的东西不加反引号）——靠这条约定把「本仓 API」与「散文里的词」分开。

- 期望值 = 本仓 Go 源码（**不含 _test.go**）里出现过的标识符；
- 判据只读**源码**（客观事实），不读任何生成物——与被检对象无共享逻辑，
  也不与被检对象共用任何解析代码。

命中分两类，都要人看一眼：

- **跨仓串味**：本仓没有，但别的矩阵仓有——多半是把别仓的 API 写进了本仓条目；
- **查无此物**：本仓与全矩阵都没有——多半是 API 已改名/移除而条目没跟上。

## 为什么是「提示」而不是硬门禁

机器判不了「这个标识符是不是别仓的、这里只是提到了它」。
实测误报率：全矩阵 11 个反引号标识符，命中 1（且是真 bug）。
所以只做**非阻塞提示**，由人确认——门禁要「少而致命」，误报多的门禁会被绕过。

用法:
    python3 scripts/llms_api_check.py --site <站点仓目录> [--base <各仓父目录>] [--selftest]

退出码: 0 = 无提示；1 = 有提示（命中时打印明细）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# llms.txt「## 模块」段里的条目：`- [core](https://github.com/oekit/core): 描述`
_ENTRY_RE = re.compile(r"^- \[([a-z0-9\-]+)\]\(https://github\.com/oekit/[a-z0-9\-]+\):\s*(.*)$", re.M)
# 反引号里的导出风格标识符（首字母大写、只含字母数字、长度 ≥2）
_TOKEN_RE = re.compile(r"`([A-Z][A-Za-z0-9]{1,})`")

_SRC_CACHE: dict[str, str] = {}


def _read_go_source(repo_dir: Path) -> str:
    """仓库的全部 Go 源码（非 _test.go）拼成一个大字符串。"""
    key = str(repo_dir)
    if key in _SRC_CACHE:
        return _SRC_CACHE[key]
    parts: list[str] = []
    if repo_dir.is_dir():
        for p in sorted(repo_dir.rglob("*.go")):
            if p.name.endswith("_test.go") or ".git" in p.parts:
                continue
            try:
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    src = "\n".join(parts)
    _SRC_CACHE[key] = src
    return src


def _entries(text: str) -> dict[str, str]:
    """llms.txt「## 模块」段的条目 → {模块名: 描述}。"""
    m = re.search(r"^##\s+模块\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return {}
    return {mm.group(1): mm.group(2) for mm in _ENTRY_RE.finditer(m.group(1))}


def check_llms_api_identifiers(site_dir: Path, base: Path, modules: list[str]) -> list[str]:
    """返回提示明细列表（空 = 无命中）。

    modules: 矩阵登记的根模块名（决定「本仓」是哪个目录，也决定跨仓比对的范围）。
    """
    llms = site_dir / "docs" / "public" / "llms.txt"
    if not llms.is_file():
        return [f"缺 {llms}（llms.txt 不在，指纹判据无从谈起）"]
    entries = _entries(llms.read_text(encoding="utf-8", errors="ignore"))
    if not entries:
        return ["llms.txt 里找不到「## 模块」段的条目（模块清单段被改名或删了）"]

    warnings: list[str] = []
    for mod, desc in sorted(entries.items()):
        tokens = sorted(set(_TOKEN_RE.findall(desc)))
        if not tokens:
            continue
        own = _read_go_source(base / mod)
        missing = [t for t in tokens if t not in own]
        if not missing:
            continue
        for tok in missing:
            owners = [m for m in modules if m != mod and tok in _read_go_source(base / m)]
            if owners:
                warnings.append(
                    f"{mod}: 反引号标识符 `{tok}` 在本仓源码里不存在，"
                    f"但 {', '.join(owners)} 里有——疑似**跨仓串味**"
                    f"（本仓条目写了别仓的 API，照抄会得到 undefined）")
            else:
                warnings.append(
                    f"{mod}: 反引号标识符 `{tok}` 在本仓与全矩阵源码里都查不到"
                    f"——疑似已改名/移除而条目没跟上")
    return warnings


def _selftest() -> int:
    """反证自测：判据被阉割（恒返回空 / 只判本仓不判跨仓）时**必须**失败。

    样本要**真的**带被防的形态——否则自测会假通过（2026-10-20 oagentkit
    的教训：自测第一版样本不带缺陷形态，移除修复后自测仍绿）。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "alpha").mkdir()
        (root / "alpha" / "a.go").write_text(
            "package alpha\n\nfunc WithOnReady() {}\nfunc WithReadyProbeTimeout() {}\n",
            encoding="utf-8")
        (root / "beta").mkdir()
        (root / "beta" / "b.go").write_text(
            "package beta\n\nfunc WithReadyAddr(addr string) {}\n", encoding="utf-8")
        site = root / "site"
        (site / "docs" / "public").mkdir(parents=True)

        # ① 三个标识符：本仓有两个（存在）、一个只在 beta（跨仓串味）
        (site / "docs" / "public" / "llms.txt").write_text(
            "## 模块\n\n"
            "- [alpha](https://github.com/oekit/alpha): `WithOnReady` `WithReadyProbeTimeout` `WithReadyAddr`\n"
            "- [beta](https://github.com/oekit/beta): `WithReadyAddr`\n"
            "\n## 其他\n\n- [gamma](https://github.com/oekit/gamma): `Nothing`\n",
            encoding="utf-8")
        w = check_llms_api_identifiers(site, root, ["alpha", "beta"])
        assert len(w) == 1, f"应恰好命中 1 条（alpha 的跨仓串味），实际 {w}"
        assert "WithReadyAddr" in w[0] and "跨仓串味" in w[0], f"命中分类不对：{w[0]}"
        assert "beta" in w[0], f"应指出该 API 属于 beta：{w[0]}"

        # ② 全矩阵都没有 → 另一类
        (site / "docs" / "public" / "llms.txt").write_text(
            "## 模块\n\n- [alpha](https://github.com/oekit/alpha): `TotallyGone`\n",
            encoding="utf-8")
        w2 = check_llms_api_identifiers(site, root, ["alpha", "beta"])
        assert len(w2) == 1 and "查不到" in w2[0], f"应报「查无此物」：{w2}"

        # ③ 全对 → 无提示（防止误报泛滥）
        (site / "docs" / "public" / "llms.txt").write_text(
            "## 模块\n\n- [alpha](https://github.com/oekit/alpha): `WithOnReady`\n",
            encoding="utf-8")
        w3 = check_llms_api_identifiers(site, root, ["alpha", "beta"])
        assert not w3, f"不该有提示：{w3}"

        # ④ 反证：把 beta 从「全矩阵」里拿掉 → 同一输入应从「跨仓串味」变成
        # 「查无此物」。若跨仓比对被阉割（恒返回无归属），① 的分类断言必失败。
        (site / "docs" / "public" / "llms.txt").write_text(
            "## 模块\n\n"
            "- [alpha](https://github.com/oekit/alpha): `WithOnReady` `WithReadyProbeTimeout` `WithReadyAddr`\n",
            encoding="utf-8")
        w4 = check_llms_api_identifiers(site, root, ["alpha"])
        assert len(w4) == 1 and "查不到" in w4[0], f"拿掉 beta 后应改判「查无此物」：{w4}"

    print("✅ llms_api_check 自测通过（含跨仓串味 / 查无此物 / 无误报 / 反证）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="llms.txt 反引号标识符：本仓源码里存在吗？")
    ap.add_argument("--site", required=False, help="站点仓目录")
    ap.add_argument("--base", default="", help="各模块仓的父目录（默认取 site 的上级）")
    ap.add_argument("--selftest", action="store_true", help="跑反证自测")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not args.site:
        ap.print_help()
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from matrix import MODULES  # noqa: E402

    site = Path(args.site).resolve()
    base = Path(args.base).resolve() if args.base else site.parent
    warnings = check_llms_api_identifiers(site, base, list(MODULES))
    if not warnings:
        print("✅ llms.txt 的反引号标识符在本仓源码里都存在")
        return 0
    print(f"⚠ llms.txt 反引号标识符存疑（{len(warnings)} 条，仅提示不阻塞）：")
    for w in warnings:
        print(f"  · {w}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
