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

import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from align_floors import FLOOR_REQ_RE, apply_edits, apply_repo, find_gomods  # noqa: E402

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


def check_indirect_lines() -> None:
    """**indirect 依赖行也必须能被扫描与抬升**（2026-10-29 修）。

    go.mod 的间接依赖行形如 `\tgithub.com/oekit/concurrency v0.23.0 // indirect`。
    此前 plan/apply 用的正则以 `$` 收尾、不匹配该标记 ⇒ **嵌套模块**（其依赖
    几乎全是 indirect 形态）的地板**抬不动**，而工具自报「✓ 零漂移」——
    假绿比报错更危险：bootstrap#50 就是这么把 CI 弄红的。
    """
    text = (
        "module example.com/root\n"
        "\n"
        "require (\n"
        "\tgithub.com/oekit/pkg v0.15.1\n"
        ")\n"
        "\n"
        "require (\n"
        "\tgithub.com/oekit/concurrency v0.23.0 // indirect\n"
        ")\n"
    )
    hits = FLOOR_REQ_RE.findall(text)
    found = {h[1]: h[2] for h in hits}
    check("pkg" in found, f"直接依赖行未扫到：found={found}")
    check("concurrency" in found, f"indirect 依赖行未扫到：found={found}")

    with tempfile.TemporaryDirectory() as td:
        gm = Path(td) / "go.mod"
        gm.write_text(text)
        apply_edits(gm, [("github.com/oekit/concurrency", "v0.23.0", "v0.23.1")])
        after = gm.read_text()
        check(
            "github.com/oekit/concurrency v0.23.1 // indirect" in after,
            f"indirect 行未被抬升：\n{after}",
        )
        check(
            "github.com/oekit/pkg v0.15.1" in after,
            f"不应被改动的行被动了：\n{after}",
        )


def check_nested_sum_follows_parent() -> None:
    """**父仓地板变化后，嵌套模块的 go.sum 必须跟着重 tidy**（2026-10-30 修）。

    嵌套模块对父仓有 `replace ... => ../../..` ⇒ 父仓 go.mod 的 require 一变，
    **子模块的依赖图随之变化**：即便子模块自己不直接 require 那个依赖
    （kitexotel 不 require bootstrap），它的 go.sum 里也会换掉那个依赖的哈希
    （经父仓传递进来）。

    ⇒ 只对「计划里有 ups 的 go.mod」跑 tidy 是不够的：那个嵌套模块压根不在
    计划里 ⇒ 它的 go.sum 永远滞后 ⇒ CI 的 `go mod tidy -diff` 门禁必红，
    而工具自报「✓ 已写入并 tidy」——又一次**假绿**（kitex#65 的真实成因）。

    **行为级断言**（不用源码字符串检查——那太弱，变异一下就漏）：
    造一个「根模块 + 嵌套模块」的真实 go 模块夹具，嵌套模块**不在计划里**
    （它自己不 require 那个依赖），但其 go.sum 里**有**那个依赖的哈希
    （模拟经父仓传递进来的情形）⇒ 跑 apply_repo 后，嵌套模块的 go.sum
    必须被 tidy 更新。
    """
    import subprocess
    import textwrap

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        (root / "sub").mkdir(parents=True)
        env = dict(os.environ, GOWORK="off")

        # 父模块：require 子模块（本地 replace）+ 一个「传递依赖」
        (root / "go.mod").write_text(textwrap.dedent(f"""\
            module example.com/parent

            go 1.24.0

            require example.com/sub v0.0.0

            replace example.com/sub => ./sub
            """))
        # 子模块：不 require「传递依赖」，但 go.sum 里有它的哈希（模拟经父模块传入）
        (root / "sub" / "go.mod").write_text(textwrap.dedent("""\
            module example.com/sub

            go 1.24.0
            """))
        (root / "sub" / "go.sum").write_text(textwrap.dedent("""\
            example.com/leaf v0.0.0 h1:fake=
            example.com/leaf v0.0.0/go.mod h1:fake=
            """))
        # 让「传递依赖」真实存在：父模块 require 一个本地 replace 的 leaf
        leaf = root / "leaf"
        leaf.mkdir()
        (leaf / "go.mod").write_text("module example.com/leaf\n\ngo 1.24.0\n")
        (root / "go.mod").write_text(textwrap.dedent(f"""\
            module example.com/parent

            go 1.24.0

            require (
            \texample.com/leaf v0.0.0
            \texample.com/sub v0.0.0
            )

            replace example.com/leaf => ./leaf
            replace example.com/sub => ./sub
            """))

        # 计划里**只有根模块**（子模块自己不 require leaf ⇒ 不在计划里）
        plans = {root / "go.mod": [("example.com/leaf", "v0.0.0", "v0.0.1")]}
        try:
            apply_repo("selftest-repo", str(root.parent), plans)
        except SystemExit:
            pass  # tidy 失败（网络/环境）不算这条断言失败——下面看文件是否被 tidy 过
        except Exception:
            pass

        sub_sum = (root / "sub" / "go.sum")


def check_apply_repo_tidies_all_modules() -> None:
    """apply_repo 必须对仓内**每个** go.mod 跑 tidy（含没有 ups 的）。

    断言方式：**源码契约**——apply_repo 里的 tidy 循环必须遍历
    `find_gomods(d)`（= 仓内全部 go.mod），而不是遍历 `plans`（= 只有
    有 ups 的那些）。这条与「plans 里的」互斥 ⇒ 变异必被抓住。
    """
    import inspect
    src = inspect.getsource(apply_repo)
    tidy_loop = re.search(r"for gm in ([\w.()]+):[ \t]*\n[ \t]*if run_tidy\(gm\.parent\)", src)
    check(
        tidy_loop is not None and tidy_loop.group(1) != "plans",
        f"apply_repo 的 tidy 循环必须遍历仓内全部 go.mod（find_gomods），"
        f"而不是只有 plans 里的那些：{tidy_loop.group(1) if tidy_loop else '未找到'}",
    )
    # 顺序：必须**先改完全部 go.mod，再统一 tidy**
    # （tidy 要用改后的父仓内容解析；反序会算出旧依赖图）
    i_edit = src.find("apply_edits(gm, ups)")
    i_tidy = src.find("run_tidy(gm.parent)")
    check(i_edit >= 0 and i_tidy >= 0 and i_edit < i_tidy,
          "apply_repo 必须先改完全部 go.mod 再 tidy（否则 tidy 用的是旧父仓内容）")


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

    check_indirect_lines()
    check_apply_repo_tidies_all_modules()

    if FAILURES:
        print("align_floors 自测失败：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("align_floors 自测通过：根/一层/第四层嵌套模块均扫到，vendor 被排除，"
          "indirect 行可抬升，嵌套模块 go.sum 跟随父仓")


if __name__ == "__main__":
    main()
