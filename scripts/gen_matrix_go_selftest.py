#!/usr/bin/env python3
"""废弃嵌套模块登记的自测：证明「多层子路径下废弃模块仍被跳过」这道修正生效。

**为什么要有这个自测**（2026-10-25 实测）：matrix-tools#37 让嵌套模块的 tag
支持多层子目录路径（kitex 的 `observability/tracing/instrumentation/kitexotel`
是活例），随后 `gen_matrix_go` 的 `NESTED_ONLY_TAGS` 也跟着放宽成允许多层。

但 `SKIP_NESTED` 的匹配**没跟着泛化**：登记表按末段名登记
（`("data", "all")`），而放宽后 `m.group(1)` 是完整子路径（`"data/all"`），
拿它整体去比登记项就对不上 ⇒ **已废弃、不再发版的嵌套模块会被重新纳入 BOM**。

**反证怎么构造**（纯逻辑，不联网、不碰真仓）：
  1. `data/all`（单层嵌套、末段名 "all"）⇒ 必须被跳过；
  2. `data/all` 的多层形态 `a/data/all` ⇒ 也必须被跳过（末段名仍是 "all"）；
  3. 别的仓的同名末段（`logx` 的 `all`）⇒ **不得**被跳过（登记表是按仓+末段名）；
  4. 非废弃的多层嵌套 ⇒ 不得被跳过；
  5. 根模块 tag ⇒ 不参与嵌套判定。

用法：python3 scripts/gen_matrix_go_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from gen_matrix_go import NESTED_ONLY_TAGS, SKIP_NESTED, nested_module_of  # noqa: E402


def nested_path(tag: str) -> str | None:
    m = NESTED_ONLY_TAGS.match(tag)
    return m.group(1) if m else None


def is_skipped(repo: str, tag: str) -> bool:
    """该 tag 对应的嵌套模块是否被**排除**（废弃，或压根不是嵌套 tag）。

    直接调**被测代码**里的判定函数（nested_module_of），不在测试里重实现
    一遍——否则改了生成器测试照样绿，是一种假绿。
    """
    return nested_module_of(repo, tag) is None


def main() -> int:
    failures: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # 1. 单层嵌套的废弃模块必须被跳过
    expect(is_skipped("data", "data/all/v0.1.0"),
           "data/all 是已废弃的嵌套模块，应被跳过")

    # 2. 多层形态的废弃模块同样必须被跳过（末段名仍是 all）
    expect(is_skipped("data", "a/data/all/v0.1.0"),
           "多层路径下的 data/all 也应被跳过（登记表按末段名登记）")

    # 3. 别的仓的同名末段不得被跳过
    expect(not is_skipped("logx", "logx/all/v0.1.0"),
           "logx/all 不在废弃登记里，不应被跳过")

    # 4. 非废弃的多层嵌套不得被跳过
    expect(not is_skipped("kitex", "observability/tracing/instrumentation/kitexotel/v0.7.0"),
           "kitexotel 不是废弃模块，不应被跳过")

    # 5. 根模块 tag 不参与嵌套判定（不是嵌套，也不是废弃嵌套）
    expect(nested_path("v0.7.0") is None, "根模块 tag 不应被当成嵌套模块")
    expect(nested_module_of("data", "v0.1.0") is None, "根模块 tag 不应被判成嵌套模块")

    # 6. 非废弃的嵌套模块必须**被保留**（返回它的完整路径）——只断言"不是
    #    None"不够：判定函数若一律返回 None，上面几条废弃断言也会一起绿。
    expect(nested_module_of("kitex", "observability/tracing/instrumentation/kitexotel/v0.7.0")
           == "observability/tracing/instrumentation/kitexotel",
           "非废弃的多层嵌套模块应被保留并返回完整路径")
    expect(nested_module_of("logx", "hlog/v0.1.2") == "hlog",
           "单层嵌套模块应被保留并返回路径")

    if failures:
        print("✗ 废弃嵌套模块登记自测未过：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("✓ 废弃嵌套模块登记自测通过（多层子路径下仍能跳过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
