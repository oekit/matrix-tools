#!/usr/bin/env python3
"""release_train 的 CHANGELOG 环节自测：证明「预校验真的跑了」且「条目不丢」。

**为什么要有这个自测**（matrix-tools#113 / #114）：audit.yml 的 selftest job
有这条原则——「判据脚本自己先得能证明『它抓得住自己要防的东西』」。这两条
缺陷都属于**静默失效**：

  1. #113：预校验的脚本路径写的是旧 org 名（``profile/scripts/...``，而共享
     脚本仓早已改名为 ``.github``）⇒ 路径**恒不存在** ⇒ ``if not exists: return``
     ⇒ **预校验被静默跳过**，发版本地放行、CI 才拦——本地预校验形同虚设，
     而且**没有任何输出**说明它根本没跑。
  2. #114：``--changelog`` 可多次给出（每多一条就是一条 bullet），但版本节
     只写 ``bullets[0]`` ⇒ 其余**静默丢弃** ⇒ 发版说明缺条目。

**反证怎么构造**：本自测在「旧写法」下必须失败——
  1. 把 ``find_changelog_check`` 换回旧路径拼接 ⇒ 用例 1 必须失败；
  2. 把 ``format_bullets`` 换回只取 ``bullets[0]`` ⇒ 用例 3 必须失败。

**无副作用、不联网**：只测纯函数与路径解析（临时目录造 fixture）。

用法：python3 scripts/release_train_selftest.py
"""
from __future__ import annotations

import ast
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_train  # noqa: E402


def main() -> int:
    failures: list[str] = []

    # --- 用例 1：共享脚本定位（#113）---
    # 1a：环境变量显式指定，且文件存在 ⇒ 必须命中它
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "changelog_check.py"
        fake.write_text("# fake\n", encoding="utf-8")
        old_env = os.environ.get("CHANGELOG_CHECK")
        os.environ["CHANGELOG_CHECK"] = str(fake)
        try:
            got = release_train.find_changelog_check()
            if got is None or got.resolve() != fake.resolve():
                failures.append(f"用例1a：CHANGELOG_CHECK 指向存在的文件时未命中（got={got}）")
        finally:
            if old_env is None:
                os.environ.pop("CHANGELOG_CHECK", None)
            else:
                os.environ["CHANGELOG_CHECK"] = old_env

    # 1b：环境变量指向不存在的文件 ⇒ 返回 None（不得回落到别的路径）
    old_env = os.environ.get("CHANGELOG_CHECK")
    os.environ["CHANGELOG_CHECK"] = "/nonexistent/changelog_check.py"
    try:
        if release_train.find_changelog_check() is not None:
            failures.append("用例1b：CHANGELOG_CHECK 指向不存在的文件时应返回 None")
    finally:
        if old_env is None:
            os.environ.pop("CHANGELOG_CHECK", None)
        else:
            os.environ["CHANGELOG_CHECK"] = old_env

    # 1c：**旧路径**（profile/scripts）恒不存在 ⇒ 若代码仍指向它，预校验会被
    #     静默跳过。本自测断言：**代码**里不得再出现旧 org 名 "profile"
    #     （注释/文档里可以解释这个坑——只看代码，与 timertask 的源码护栏同口径）。
    src = (Path(__file__).resolve().parent / "release_train.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            val = body[0].value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                docstrings.add(id(val))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if "profile" in node.value:
                failures.append(
                    f"用例1c：代码里仍引用旧 org 名 'profile'（{node.value!r}）——"
                    "该路径恒不存在 ⇒ 预校验被静默跳过")

    # --- 用例 2：找不到脚本时**必须出声**（不许静默跳过）---
    old_env = os.environ.get("CHANGELOG_CHECK")
    os.environ["CHANGELOG_CHECK"] = "/nonexistent/changelog_check.py"
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            release_train.validate_changelog(Path("/tmp/does-not-matter/CHANGELOG.md"))
        out = buf.getvalue()
        if "未找到" not in out:
            failures.append(f"用例2：找不到 changelog_check.py 时必须明确告警，实际输出={out!r}")
    finally:
        if old_env is None:
            os.environ.pop("CHANGELOG_CHECK", None)
        else:
            os.environ["CHANGELOG_CHECK"] = old_env

    # --- 用例 3：多条 bullet 全部写入（#114）---
    got = release_train.format_bullets(["第一条", "第二条", "第三条"], False, "demo", "v1.2.3")
    for b in ("第一条", "第二条", "第三条"):
        if b not in got:
            failures.append(f"用例3：bullet「{b}」被丢弃了（got={got!r}）")
    if got.count("\n- ") != 2:
        failures.append(f"用例3：期望 3 条 bullet（2 个换行分隔），实际={got!r}")

    # 3b：只有一条 ⇒ 与旧行为一致（单条，不带换行）
    got1 = release_train.format_bullets(["只有一条"], False, "demo", "v1.2.3")
    if got1 != "只有一条":
        failures.append(f"用例3b：单条 bullet 的形态不对：{got1!r}")

    # 3c：嵌套模块的版本节仍是「跟随主模块发版」文案（不写主模块的 bullet）
    gotn = release_train.format_bullets(["主模块的 bullet"], True, "demo", "v1.2.3")
    if "主模块的 bullet" in gotn or "跟随" not in gotn:
        failures.append(f"用例3c：嵌套模块版本节文案不对：{gotn!r}")

    # 3d：没有 bullet ⇒ 跟随文案
    got0 = release_train.format_bullets([], False, "demo", "v1.2.3")
    if "跟随" not in got0:
        failures.append(f"用例3d：无 bullet 时应为跟随文案：{got0!r}")

    if failures:
        print("✗ release_train CHANGELOG 环节自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ release_train CHANGELOG 环节自测通过：共享脚本定位得到（找不到会告警）、多条 bullet 不丢条目")


if __name__ == "__main__":
    main()
