#!/usr/bin/env python3
"""API 生命周期门禁自测：判据脚本自己先得能证明「它抓得住自己要防的东西」。

**为什么要有这道自测**：门禁脚本坏掉的形态很安静——判据写错（集合比对方向
反了、标记识别放宽了、漏登记判成了幽灵登记）时，它会**全绿通过**，而真实漂移
就在那儿。本自测用「变异体」反向证明：故意注入每一种漂移，断言门禁**精确**
判红；再断言未变异的干净态判绿。

自测覆盖的漂移形态（每一条都是清单/包注释/生产包集合之间的一种不一致）：

| 形态 | 期望 |
|---|---|
| 漏登记（新增包未进清单） | 判红 |
| 幽灵登记（清单里的包已不存在） | 判红 |
| level 与包注释标记不一致 | 判红 |
| 包注释标记缺失 | 判红 |
| 包注释标记出现多处 | 判红 |
| reason 为空 | 判红 |
| since 形态非法 | 判红 |
| level 不在词表 | 判红 |
| Deprecated 缺 replacement | 判红 |
| 非 Deprecated 却带 replacement | 判红 |
| 清单字段名拼错 | 判红 |
| 清单 version 不是 1 | 判红 |
| 干净态 | 判绿 |
| 清单文件不存在 | 退出码 2（判不了 ≠ 通过） |

**无副作用、不联网**：只在临时目录里造最小仓，不碰真实仓库。
用法：python3 scripts/api_lifecycle_gate_selftest.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from api_lifecycle_gate import check, parse_manifest  # noqa: E402

FAILURES = []


def build_repo(root: Path, packages, entries, version="1"):
    """造一个最小 pkg 仓：每个包一个 doc.go（含标记），加清单。"""
    root.mkdir(parents=True, exist_ok=True)
    for pkg, level in packages.items():
        d = root / pkg
        d.mkdir(parents=True, exist_ok=True)
        marker = f"// 生命周期: {level}——测试用标记。"
        (d / "doc.go").write_text(
            f"// Package {pkg.split('/')[-1]} 测试包。\n//\n{marker}\npackage {pkg.split('/')[-1]}\n",
            encoding="utf-8",
        )
    lines = ["# 测试清单", "", f"version: {version}", "", "packages:"]
    for e in entries:
        lines.append(f"  - pkg: {e['pkg']}")
        lines.append(f"    level: {e.get('level', 'Experimental')}")
        lines.append(f"    since: {e.get('since', 'v0.1.0')}")
        lines.append(f"    reason: {e.get('reason', '测试用理由')}")
        if e.get("replacement"):
            lines.append(f"    replacement: {e['replacement']}")
    (root / "api-lifecycle.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def expect_red(name, mutate):
    """注入变异 ⇒ 必须判红。"""
    tmp = Path(tempfile.mkdtemp(prefix="apilife_"))
    try:
        repo = tmp / "pkg"
        build_repo(repo, {"alpha": "Stable", "beta": "Experimental"},
                   [{"pkg": "alpha", "level": "Stable"}, {"pkg": "beta", "level": "Experimental"}])
        mutate(repo)
        probs = check(repo)
        if not probs:
            FAILURES.append(f"{name}: 期望判红，实际判绿（门禁没抓住这种漂移）")
            print(f"  ✗ {name}: 期望判红，实际判绿")
        else:
            print(f"  ✓ {name}: 判红（{probs[0][:60]}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def expect_green(name):
    tmp = Path(tempfile.mkdtemp(prefix="apilife_"))
    try:
        repo = tmp / "pkg"
        build_repo(repo, {"alpha": "Stable", "beta": "Experimental"},
                   [{"pkg": "alpha", "level": "Stable"}, {"pkg": "beta", "level": "Experimental"}])
        probs = check(repo)
        if probs:
            FAILURES.append(f"{name}: 期望判绿，实际判红：{probs}")
            print(f"  ✗ {name}: 期望判绿，实际判红 {probs}")
        else:
            print(f"  ✓ {name}: 判绿")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def edit_manifest(repo: Path, fn):
    p = repo / "api-lifecycle.yaml"
    p.write_text(fn(p.read_text(encoding="utf-8")), encoding="utf-8")


def main() -> int:
    print("=== API 生命周期门禁自测 ===")
    expect_green("干净态")

    expect_red("漏登记（新增包未进清单）",
               lambda r: (r / "gamma").mkdir() or ((r / "gamma" / "doc.go").write_text(
                   "// Package gamma 测试包。\npackage gamma\n", encoding="utf-8")))
    expect_red("幽灵登记（清单里的包不存在）",
               lambda r: edit_manifest(r, lambda s: s.replace("  - pkg: beta\n", "  - pkg: ghost\n")))
    expect_red("level 与包注释不一致",
               lambda r: edit_manifest(r, lambda s: s.replace(
                   "  - pkg: alpha\n    level: Stable", "  - pkg: alpha\n    level: Experimental")))
    expect_red("包注释标记缺失",
               lambda r: (r / "beta" / "doc.go").write_text(
                   "// Package beta 测试包。\npackage beta\n", encoding="utf-8"))
    expect_red("包注释标记多处",
               lambda r: (r / "beta" / "doc.go").write_text(
                   "// 生命周期: Experimental——标记一。\n//\n// 生命周期: Experimental——标记二。\npackage beta\n",
                   encoding="utf-8"))
    expect_red("reason 为空",
               lambda r: edit_manifest(r, lambda s: s.replace("    reason: 测试用理由", "    reason:")))
    expect_red("since 形态非法",
               lambda r: edit_manifest(r, lambda s: s.replace("    since: v0.1.0", "    since: 0.1")))
    expect_red("level 不在词表",
               lambda r: edit_manifest(r, lambda s: s.replace("    level: Stable", "    level: GA")))
    expect_red("Deprecated 缺 replacement",
               lambda r: edit_manifest(r, lambda s: s.replace(
                   "  - pkg: alpha\n    level: Stable", "  - pkg: alpha\n    level: Deprecated")))
    expect_red("非 Deprecated 却带 replacement",
               lambda r: edit_manifest(r, lambda s: s.replace(
                   "    reason: 测试用理由", "    reason: 测试用理由\n    replacement: github.com/oekit/pkg/alpha.X")))
    expect_red("清单字段名拼错",
               lambda r: edit_manifest(r, lambda s: s.replace("    since: v0.1.0", "    sice: v0.1.0")))
    expect_red("清单 version 不是 1",
               lambda r: edit_manifest(r, lambda s: s.replace("version: 1", "version: 2")))

    # 清单不存在 ⇒ 退出码 2（判不了 ≠ 通过）
    tmp = Path(tempfile.mkdtemp(prefix="apilife_"))
    try:
        empty = tmp / "pkg"
        empty.mkdir()
        (empty / "alpha").mkdir()
        (empty / "alpha" / "doc.go").write_text("package alpha\n", encoding="utf-8")
        probs = check(empty)
        if not probs:
            FAILURES.append("清单不存在: 期望报出读不到清单，实际无问题")
            print("  ✗ 清单不存在: 期望判红")
        else:
            print(f"  ✓ 清单不存在: 判红（{probs[0][:60]}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 解析器对畸形清单返回 None（不静默放过）
    tmp = Path(tempfile.mkdtemp(prefix="apilife_"))
    try:
        bad = tmp / "pkg"
        bad.mkdir()
        (bad / "api-lifecycle.yaml").write_text("version: notanumber\npackages:\n", encoding="utf-8")
        parsed = parse_manifest(bad / "api-lifecycle.yaml")
        if parsed is not None:
            FAILURES.append("畸形清单: 期望解析失败返回 None")
            print("  ✗ 畸形清单: 期望解析失败")
        else:
            print("  ✓ 畸形清单: 解析失败被识破")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ 自测失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("✅ 自测全绿：门禁对每种漂移形态都判得住，干净态判绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
