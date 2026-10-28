#!/usr/bin/env python3
"""align_floors 的自测：证明「深层嵌套模块必须被扫到」这条判据抓得住自己要防的东西。

**为什么要有这个自测**：kitex#58 的真实成因就是 `align_floors.py` 只扫一层
（`glob("*/go.mod")`）⇒ 漏掉第 4 层的 `observability/tracing/instrumentation/kitexotel`
⇒ 父仓地板抬到 pkg v0.30.0、子模块仍停在 v0.15.1 ⇒ CI 的 `go mod tidy -diff`
门禁判红。

一道没人验证过的判据，很可能自己就是个假绿——**「扫到了」必须能被证伪**。

**反证怎么构造**：把 `find_gomods` 退回一层扫描（`glob("*/go.mod")`），
本自测必须**失败**。

**无副作用、不联网、不依赖工作区布局**：全部用临时目录造 fixture。

用法：python3 scripts/align_floors_selftest.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from align_floors import find_gomods  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def build_fixture(root: Path) -> None:
    """造一个「嵌套模块深度不一」的仓：根 + 一层 + 第四层。"""
    (root / "go.mod").write_text("module example.com/root\n")
    (root / "sink").mkdir()
    (root / "sink" / "go.mod").write_text("module example.com/root/sink\n")
    deep = root / "observability" / "tracing" / "instrumentation" / "kitexotel"
    deep.mkdir(parents=True)
    (deep / "go.mod").write_text("module example.com/root/observability/tracing/instrumentation/kitexotel\n")
    # 干扰项：vendor 与 .git 里的 go.mod 不该被扫到
    vend = root / "vendor" / "dep"
    vend.mkdir(parents=True)
    (vend / "go.mod").write_text("module example.com/vendor/dep\n")


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        build_fixture(root)

        found = {gm.relative_to(root).as_posix() for gm in find_gomods(root)}

        # 1. 根模块必须扫到
        check("go.mod" in found, f"根 go.mod 未扫到：found={sorted(found)}")
        # 2. 一层嵌套模块必须扫到
        check("sink/go.mod" in found, f"一层嵌套模块未扫到：found={sorted(found)}")
        # 3. 第四层嵌套模块必须扫到（kitexotel 的真实形态）
        deep = "observability/tracing/instrumentation/kitexotel/go.mod"
        check(deep in found, f"第四层嵌套模块未扫到：found={sorted(found)}")
        # 4. vendor 里的 go.mod 不该被扫到（不是本仓的模块）
        check(
            not any(p.startswith("vendor/") for p in found),
            f"vendor 下的 go.mod 被误扫：found={sorted(found)}",
        )
        # 5. 不得重复计入根模块
        check(len(found) == 3, f"扫描结果数量异常（期望 3）：found={sorted(found)}")

    if FAILURES:
        print("align_floors 自测失败：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("align_floors 自测通过：根/一层/第四层嵌套模块均扫到，vendor 被排除")


if __name__ == "__main__":
    main()
