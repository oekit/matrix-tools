#!/usr/bin/env python3
"""release_train「被 tag 模块的 oekit 依赖必须下游可解析」门禁自测（matrix-tools#148）。

**为什么要有这个自测**：这条门禁防的是**静默失效**——

  嵌套模块对父仓有 `replace github.com/oekit/<父仓> => ../`（指向本地源码）
  ⇒ 本仓 `go mod tidy -diff` / `go build` **全绿** ⇒ 门禁前的既有检查**全都不出声**
  ⇒ 而下游消费方解析该嵌套模块时，Go **忽略依赖里的 replace** ⇒ 会照着 go.mod
  去模块代理取父仓的 require 版本 ⇒ 该版本没发布 ⇒ 消费方直接解析失败。

  也就是说：**这条门禁一旦被改成恒绿，所有症状都消失**（本地全绿、CI 全绿），
  破坏只会在消费方那边显形 ⇒ 必须自证「它抓得住自己要防的东西」。

**反证怎么构造**（本自测必须能在下列变异下转红）：

  1. 门禁函数直接 return（恒绿）⇒ 用例 2/4/6 必须失败；
  2. `required_tag_form` 把嵌套路径当**根形态**（用最后一段或空子路径）⇒ 用例 5 必须失败；
  3. 远端读不到时 fail-open（当成「已发布」）⇒ 用例 6 必须失败；
  4. 把 replace 块里的条目当 require ⇒ 用例 8 必须失败。

**无副作用、不联网**：纯函数 + 临时目录造 fixture，远端事实用注入的假 tag_exists。

用法：python3 scripts/release_train_requires_selftest.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_train  # noqa: E402
from matrix import RemoteReadError  # noqa: E402


def fake_tags(published: dict[str, set[str]]):
    """假 tag_exists：published[repo] 是该仓已发布的 tag 集合。"""
    def _f(repo: str, tag: str) -> bool:
        return tag in published.get(repo, set())
    return _f


def fake_unreadable(repo: str, tag: str) -> bool:
    raise RemoteReadError(f"模拟远端读不到：{repo}@{tag}")


def write_gomod(tmp: Path, body: str) -> Path:
    d = tmp / "mod"
    d.mkdir(parents=True, exist_ok=True)
    (d / "go.mod").write_text(body, encoding="utf-8")
    return d


def run(body: str, planned: set[tuple[str, str]], published: dict[str, set[str]],
        tag_exists=None) -> list[str]:
    """跑一次门禁，返回 failures 列表。"""
    with tempfile.TemporaryDirectory() as td:
        mod = write_gomod(Path(td), body)
        failures: list[str] = []
        release_train.check_requires_resolvable(
            "demo", "main", mod, planned, failures,
            tag_exists=tag_exists if tag_exists is not None else fake_tags(published))
        return failures


def main() -> int:
    failures: list[str] = []

    # --- 用例 1：父仓地板**已发布** ⇒ 放行 ---
    body = ("module example.com/nested\n\ngo 1.21\n\n"
            "require github.com/oekit/demo v0.2.6\n\n"
            "replace github.com/oekit/demo => ../\n")
    f = run(body, {("demo", "v0.2.7")}, {"demo": {"v0.2.6"}})
    if f:
        failures.append(f"用例1：父仓地板已发布时应放行，实际判红={f}")

    # --- 用例 2：父仓地板**未发布**且不在本 train ⇒ 判红（核心用例）---
    f = run(body, {("demo", "v0.2.7")}, {"demo": {"v0.2.5"}})
    if not f:
        failures.append("用例2：父仓地板未发布且不在本 train 时必须判红（这是本门禁要防的那一刀）")
    else:
        msg = f[0]
        for token in ("v0.2.6", "demo"):
            if token not in msg:
                failures.append(f"用例2：判红消息里应点名 {token}（实际={msg!r}）")

    # --- 用例 3：父仓地板**就是本 train 要发的版本** ⇒ 放行 ---
    f = run(body, {("demo", "v0.2.6")}, {"demo": {"v0.2.5"}})
    if f:
        failures.append(f"用例3：父仓地板在本 train 内一起发版时应放行，实际判红={f}")

    # --- 用例 3b：planned_tags 必须**带仓名**（同名版本在别的仓是另一个东西）---
    f = run(body, {("other", "v0.2.6")}, {"demo": {"v0.2.5"}})
    if not f:
        failures.append("用例3b：planned 里的 tag 必须按 (仓, tag) 配对——"
                        "别的仓的同名 v0.2.6 不算本仓父仓已发版")

    # --- 用例 4：根模块依赖**另一个仓**的未发布版本 ⇒ 判红 ---
    body4 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require github.com/oekit/other v0.9.9\n")
    f = run(body4, {("demo", "v0.2.7")}, {"other": {"v0.9.8"}})
    if not f:
        failures.append("用例4：依赖另一个仓的未发布版本时必须判红")
    elif "other" not in f[0]:
        failures.append(f"用例4：判红消息应点名 other（实际={f[0]!r}）")

    # --- 用例 5：跨仓**嵌套模块**依赖 ⇒ tag 形态必须是「子路径/版本」---
    body5 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require github.com/oekit/logx/hlog v0.1.6\n")
    # 代理里只有根形态 v0.1.6（logx 根模块的 tag）⇒ 嵌套形态 hlog/v0.1.6 不存在 ⇒ 判红
    f = run(body5, set(), {"logx": {"v0.1.6"}})
    if not f:
        failures.append("用例5：跨仓嵌套依赖必须按「子路径/版本」形态找 tag——"
                        "只认根形态 v0.1.6 会把「读不到」当成「已发布」")
    # 正确的形态存在 ⇒ 放行
    f = run(body5, set(), {"logx": {"hlog/v0.1.6"}})
    if f:
        failures.append(f"用例5b：嵌套形态 hlog/v0.1.6 已发布时应放行，实际判红={f}")

    # --- 用例 6：远端读不到 ⇒ **fail closed**（读不到 ≠ 已发布）---
    f = run(body4, set(), {}, tag_exists=fake_unreadable)
    if not f:
        failures.append("用例6：远端读不到时必须判红（fail closed），不得当成「已发布」")

    # --- 用例 7：非 oekit 依赖 ⇒ 忽略 ---
    body7 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require golang.org/x/sync v0.7.0\n\n"
             "require github.com/stretchr/testify v1.9.0\n")
    f = run(body7, set(), {})
    if f:
        failures.append(f"用例7：非 oekit 依赖不该判红，实际={f}")

    # --- 用例 8：replace 块里的条目**不是** require ---
    # （require 块里的 other v0.9.8 是**已发布**的真依赖 ⇒ 不该判红；
    #   replace 块里的 phantom v8.8.8 未发布，但它根本不是 require ⇒ 更不该判红）
    body8 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require (\n\tgithub.com/oekit/other v0.9.8 // indirect\n)\n\n"
             "replace (\n\tgithub.com/oekit/phantom v8.8.8 => ../phantom\n)\n")
    f = run(body8, set(), {"other": {"v0.9.8"}})
    if f:
        failures.append(f"用例8：replace 块里的条目不该被当成 require，实际={f}")

    # --- 用例 9：块状 require + // indirect 注释 ⇒ 正确解析 ---
    body9 = ("module github.com/oekit/demo\n\ngo 1.21\n\n"
             "require (\n\tgithub.com/oekit/other v0.9.8 // indirect\n)\n")
    f = run(body9, set(), {"other": {"v0.9.8"}})
    if f:
        failures.append(f"用例9：块状 require（含 indirect 注释）应正确解析并放行，实际判红={f}")

    # --- 用例 10：required_tag_form 的形态对照 ---
    cases = [
        ("github.com/oekit/demo", "v0.2.6", ("demo", "v0.2.6")),
        ("github.com/oekit/logx/hlog", "v0.1.6", ("logx", "hlog/v0.1.6")),
        ("github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel",
         "v0.7.4",
         ("kitex", "observability/tracing/instrumentation/kitexotel/v0.7.4")),
    ]
    for path, ver, want in cases:
        got = release_train.required_tag_form(path, ver)
        if got != want:
            failures.append(f"用例10：required_tag_form({path!r}, {ver!r}) = {got}，期望 {want}")

    if failures:
        print("✗ release_train 依赖可解析性门禁自测未过：")
        for f_ in failures:
            print("  -", f_)
        raise SystemExit(1)
    print("✓ release_train 依赖可解析性门禁自测通过：未发布的父仓地板/跨仓依赖判红、"
          "本 train 同发放行、远端读不到 fail closed、replace 块不当 require")


if __name__ == "__main__":
    main()
