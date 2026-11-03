#!/usr/bin/env python3
"""发版分支名 slug 的自测（用例5）：分支名必须**单层、唯一、含版本号**，且**调用点真的走了它**。

**为什么要有这个自测**：分支名是发版批次的唯一标识。
  - 旧形态用**完整 tag** ⇒ 多段嵌套模块的分支名变成多层引用
    （``chore/rel-observability/tracing/.../v0.7.7``）⇒ 与同仓其它分支有
    **git 引用 D/F 冲突**风险（实测 fatal: ``cannot lock ref``）；
  - 另一种写法用「只有版本号」（``rsplit("/", 1)[-1]``）⇒ **丢掉模块身份**
    ⇒ 不同模块版本号相同就会**撞分支名** ⇒ ``open_pr`` 可能复用别的模块的 PR；
  - 仓内注释明确要求「分支名**不能退化成 ``chore/rel-``**」（没有版本号 ⇒
    无法区分批次）⇒ 修法必须同时满足**含版本号**与**单层**。

★ **为什么必须同时断言「调用点」**（本版新增，踩过的坑）：
  只测纯函数 ⇒ **「函数对、调用点错」这类退化抓不到** ⇒
  反例（实测）：把 ``rel_slug = branch_slug([...])`` 改回
  ``rel_slug = "+".join(t for _n, _d, t in specs)``（**`branch_slug` 函数本身保持正确**）
  ⇒ 纯函数自测**照样全绿**，而实际产出的分支名又变回**多层引用** ⇒ D/F 冲突故障**复活**。
  ⇒ 故本自测用 `ast` **解析源码**断言赋值目标与被调用函数 ⇒ 不依赖文本排版。

**反证怎么构造**（变异自证 3/3）：
  M1 分支名退回「完整 tag 拼接」（旧形态）
  M2 分支名退回「只有版本号」（`rsplit` 末段）
  M3 **调用点**退回旧写法（函数保持正确）★ 这一条是「纯函数自测的天然盲区」

**无副作用、不联网**：只测纯函数 + 静态解析源码。

用法：python3 scripts/release_train_slug_selftest.py
"""
from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_train  # noqa: E402


def slug_assertions() -> list[str]:
    """用例5：发版分支名 slug 的形态（单层 / 唯一 / 含版本号）。"""
    failures: list[str] = []
    cases = [
        (["v0.2.7"], "v0.2.7", "根 tag：只有版本号"),
        (["redis/v0.2.7"], "redis-v0.2.7", "单段嵌套：模块名+版本号"),
        (["observability/tracing/instrumentation/kitexotel/v0.7.7"],
         "kitexotel-v0.7.7", "深层嵌套：只取末两段"),
        (["nacos/v0.3.10", "obs/v0.2.14"], "nacos-v0.3.10+obs-v0.2.14",
         "多模块：+ 连接"),
    ]
    for tags, want, why in cases:
        got = release_train.branch_slug(tags)
        if got != want:
            failures.append(f"用例5a：{why} ⇒ 分支 slug 期望 {want!r}，实际 {got!r}")
        if "/" in got:
            failures.append(f"用例5b：{why} ⇒ 分支 slug 不得含 '/'（多层引用/D-F 冲突）：{got!r}")
    a = release_train.branch_slug(["nacos/v0.1.3"])
    b = release_train.branch_slug(["nacosimpl/v0.1.3"])
    if a == b:
        failures.append(f"用例5c：不同模块同版本号的 slug 必须不同（否则撞分支名）：{a!r}")
    if not release_train.branch_slug(["redis/v0.2.7"]).endswith("v0.2.7"):
        failures.append("用例5d：分支 slug 必须以版本号结尾（否则退化，批次不可区分）")
    return failures


# ---------------------------------------------------------------- 调用点断言
def module_source() -> str:
    return inspect.getsource(release_train)


def _find_func(tree: ast.AST, name: str):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def call_site_failures(src: str) -> list[str]:
    """★ 断言 `ensure_changelog` 的**调用点**：`rel_slug` 必须由 `branch_slug()` 生成。

    判定口径（不依赖源码排版，只看 AST）：
      · `rel_slug` 的每一次赋值，要么是 `= main_tag`（**根发版路径**，合法），
        要么是 **调用 `branch_slug(...)`** ⇒ 任何其它形态（例如退回 `"+".join(...)`）
        都会被判红；
      · `rel_title_tag` **不得由 `rel_slug` 派生** ⇒ 标题必须保留**完整 tag**。
    """
    failures: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"用例5e：源码无法解析（{e}）⇒ 调用点断言失效"]
    fn = _find_func(tree, "ensure_changelog")
    if fn is None:
        return ["用例5e：源码里找不到 `ensure_changelog` ⇒ 调用点断言失效（源码形态变了？）"]

    slug_vals, title_vals = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    if tgt.id == "rel_slug":
                        slug_vals.append(node.value)
                    elif tgt.id == "rel_title_tag":
                        title_vals.append(node.value)

    if not slug_vals:
        failures.append("用例5e：`ensure_changelog` 里找不到 `rel_slug` 赋值 ⇒ 调用点断言失效")
    for v in slug_vals:
        if isinstance(v, ast.Name) and v.id == "main_tag":
            continue  # 根发版路径：分支名本来就该是版本号
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
                and v.func.id == "branch_slug":
            continue
        failures.append(
            "用例5e：`rel_slug` 必须由 `branch_slug(...)` 生成（当前不是）"
            "⇒ 退回旧写法会复活多层引用/D-F 冲突")

    if not title_vals:
        failures.append("用例5f：找不到 `rel_title_tag` 赋值")
    for v in title_vals:
        if any(isinstance(n, ast.Name) and n.id == "rel_slug" for n in ast.walk(v)):
            failures.append(
                "用例5f：`rel_title_tag` 不得由 `rel_slug` 派生（标题必须保留完整 tag）")
    return failures


# ---------------------------------------------------------------- 变异自证
def run_slug_mutation(mutate) -> list[str]:
    saved = release_train.branch_slug
    try:
        mutate()
        return slug_assertions()
    finally:
        release_train.branch_slug = saved


def _mutate_slug_full_tag():
    """M1：分支名退回「完整 tag 拼接」（旧形态）⇒ 用例5 必须转红。"""
    release_train.branch_slug = lambda tags: "+".join(tags)


def _mutate_slug_version_only():
    """M2：分支名退回「只有版本号」（rsplit 末段）⇒ 用例5c 必须转红（丢模块身份）。"""
    release_train.branch_slug = lambda tags: "+".join(t.rsplit("/", 1)[-1] for t in tags)


def _mutate_call_site_source(src: str) -> str:
    """M3：**调用点**退回旧写法（`branch_slug` 函数保持正确）⇒ 用例5e 必须转红。

    ★ 这是纯函数自测的天然盲区 ⇒ 必须显式覆盖。
    """
    new = re.sub(r"rel_slug\s*=\s*branch_slug\(\[[^\]]*\]\)",
                 'rel_slug = "+".join(t for _n, _d, t in specs)', src)
    return new


def main() -> int:
    failures = slug_assertions() + call_site_failures(module_source())
    if failures:
        print("✗ 分支名 slug 自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ 分支名 slug 自测通过：单层（无 '/'）/ 模块身份不丢 / 含版本号 / 调用点已核")

    missed: list[str] = []
    for name, mutate in (("分支名退回完整 tag（多层引用/D-F 冲突）", _mutate_slug_full_tag),
                         ("分支名退回只有版本号（丢模块身份 ⇒ 撞名）", _mutate_slug_version_only)):
        f = run_slug_mutation(mutate)
        if not f:
            missed.append(name)
            print(f"✗ 变异「{name}」没有被抓住——这份断言没有牙齿")
        else:
            print(f"✓ 变异「{name}」被抓住（{len(f)} 条断言转红）")

    # M3：调用点退化（函数保持正确）⇒ 用源码级变异 + 静态解析判红
    src = module_source()
    mutated = _mutate_call_site_source(src)
    if mutated == src:
        missed.append("调用点退回旧写法")
        print("✗ 变异「调用点退回旧写法」**未能施加**（源码形态变了？）⇒ 变异自证无效")
    else:
        f = call_site_failures(mutated)
        if not f:
            missed.append("调用点退回旧写法")
            print("✗ 变异「调用点退回旧写法」没有被抓住——这份断言没有牙齿")
        else:
            print(f"✓ 变异「调用点退回旧写法」被抓住（{len(f)} 条断言转红）")

    if missed:
        raise SystemExit(1)
    print("✓ 分支名 slug 变异自证 3/3 全部转红（含**调用点退化**）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
