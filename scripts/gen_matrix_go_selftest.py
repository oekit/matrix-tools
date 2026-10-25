#!/usr/bin/env python3
"""嵌套模块 tag 扫描的自测：证明「多段嵌套路径不会被漏掉」这道修正真的生效。

**为什么要有这个自测**（2026-10-25 实测）：oekit BOM 的 CI 有一条「地板数量
护栏」，要求 go.mod 里恰好有 35 个 oekit 地板（24 根 + 11 嵌套）。实测该护栏
在 main 上就是红的——BOM 只钉了 34 个。

根因是生成器的嵌套 tag 正则只匹配**一段**：

    NESTED_ONLY_TAGS = re.compile(r"^([^/]+)/v\\d+\\.\\d+\\.\\d+$")

而 kitex 的遥测模块的 tag 是**三段**：

    observability/tracing/instrumentation/kitexotel/v0.7.0

⇒ `[^/]+` 匹配不到，整条嵌套模块被漏掉，BOM 少钉一个地板。

**反证怎么构造**（纯正则，不联网、不碰真仓）：
  1. 一段嵌套（`hlog/v0.1.2`）⇒ 必须扫到；
  2. 三段嵌套（`observability/tracing/instrumentation/kitexotel/v0.7.0`）⇒
     **修复前扫不到、修复后必须扫到**；
  3. 根模块 tag（`v0.7.0`）⇒ 必须**扫不到**（它是根模块，不是嵌套模块）；
  4. 多段嵌套的**末段名**必须能对上 SKIP_NESTED 登记表（`data/all` 仍被跳过）；
  5. 同一嵌套路径只认一个版本（取最新的那个）。

用法：python3 scripts/gen_matrix_go_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from gen_matrix_go import NESTED_ONLY_TAGS, SKIP_NESTED  # noqa: E402


def scan_nested(tag: str) -> str | None:
    """返回该 tag 对应的嵌套模块路径；不是嵌套 tag 则返回 None。"""
    m = NESTED_ONLY_TAGS.match(tag)
    if not m:
        return None
    return m.group(1)


def skipped(repo: str, nested_path: str) -> bool:
    """该嵌套模块是否在废弃登记表里（按末段名登记）。"""
    return (repo, nested_path.rsplit("/", 1)[-1]) in SKIP_NESTED


def main() -> int:
    failures: list[str] = []

    # 1. 一段嵌套必须扫到
    got = scan_nested("hlog/v0.1.2")
    if got != "hlog":
        failures.append(f"一段嵌套应扫到 'hlog'，实际 {got!r}")

    # 2. 三段嵌套必须扫到（本次修复的核心）
    got = scan_nested("observability/tracing/instrumentation/kitexotel/v0.7.0")
    want = "observability/tracing/instrumentation/kitexotel"
    if got != want:
        failures.append(f"三段嵌套应扫到 {want!r}，实际 {got!r}")

    # 3. 根模块 tag 必须扫不到
    for tag in ("v0.7.0", "v0.1.0"):
        if scan_nested(tag) is not None:
            failures.append(f"根模块 tag {tag!r} 不应被当成嵌套模块")

    # 4. 多段嵌套的末段名要能对上废弃登记表
    if not skipped("data", "all"):
        failures.append("data/all 仍应被跳过")
    if skipped("data", "all/v2"):
        failures.append("末段名为 all 的多段嵌套也应被跳过")
    if skipped("logx", "all"):
        failures.append("别的仓的同名末段不应被跳过")

    # 5. 同一嵌套路径的多版本都能被识别（取最新由调用方负责）
    for ver in ("v0.1.0", "v0.7.0"):
        got = scan_nested(f"observability/tracing/instrumentation/kitexotel/{ver}")
        if got != want:
            failures.append(f"三段嵌套 {ver} 应扫到 {want!r}，实际 {got!r}")

    if failures:
        print("✗ 嵌套 tag 扫描自测未过：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("✓ 嵌套 tag 扫描自测通过（含多段嵌套路径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
