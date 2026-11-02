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


def issue_binding_assertions() -> list[str]:
    """#162：--issue 的引用绑不到条目 ⇒ **不许发版**（fail closed）。

    旧实现只打印一行提示就继续走 ⇒ 版本节已存在时（同一版里修了两条 issue、
    或版本节被别的 PR 先建出来）引用被**静默丢弃** ⇒ 回执定位不到本条修复。
    """
    failures: list[str] = []
    sec_bound = "- fix(tools): 修好了（#162）\n\n关联问题单：matrix-tools#162。\n"
    sec_only_line = "- fix(tools): 修好了\n\n关联问题单：matrix-tools#162。\n"

    if release_train.unbound_issues(sec_bound, ["matrix-tools#162"]):
        failures.append("用例4a：号在 bullet 里 ⇒ 不该判成未绑定")
    if not release_train.unbound_issues(sec_only_line, ["matrix-tools#162"]):
        failures.append("用例4b：号只在「关联问题单」行 ⇒ 必须判成未绑定"
                        "（回执定位不到本条修复）")
    if release_train.unbound_issues(sec_bound, []):
        failures.append("用例4c：没给 --issue ⇒ 不该有任何未绑定")
    if release_train.unbound_issues(sec_bound, None):
        failures.append("用例4c2：--issue 为 None ⇒ 不该有任何未绑定")
    if not release_train.unbound_issues(sec_bound, ["不是这种形态"]):
        failures.append("用例4d：形态不认识的引用必须判成未绑定（fail closed）")
    # ⚠️ 定位器的**弱形态只认数字**（`#N`），不认仓名 ⇒ 别的仓的号在本节里
    # **也会**被判成绑定。这是 #153 已发布的行为（回执就是按号定位）⇒ 本判据
    # **必须与回执同口径** ⇒ 不能在这里另立一套更严的规则（否则两端口径分裂）。
    if release_train.unbound_issues(sec_bound, ["other-repo#162"]):
        failures.append("用例4e：定位器按**号**定位（弱形态只认数字）⇒ "
                        "别的仓的号在本节里也算绑定 ⇒ 判据必须与回执同口径")

    # 版本节**已存在**且号绑不到 ⇒ 中止（且**不落 PR**）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [v0.1.99] — 2026-11-02\n\n- fix(tools): 修好了\n\n"
            "关联问题单：matrix-tools#162。\n", encoding="utf-8")
        specs = [("main", root, "v0.1.99")]
        try:
            release_train.ensure_changelog("matrix-tools", root, specs, ["修好了"],
                                          True, ["matrix-tools#162"])
        except SystemExit as e:
            msg = str(e)
            if "没有把" not in msg or "修法" not in msg:
                failures.append(f"用例4f：中止信息不 actionable：{msg[:200]}")
        else:
            failures.append("用例4f：版本节已存在且号绑不到条目时**仍放行**"
                            "（#162 的静默丢弃没修掉）")

    # 版本节**已存在**且号绑得到 ⇒ 放行（不许误伤正常发版）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [v0.1.99] — 2026-11-02\n\n"
            "- fix(tools): 修好了（#162）\n\n关联问题单：matrix-tools#162。\n",
            encoding="utf-8")
        specs = [("main", root, "v0.1.99")]
        try:
            release_train.ensure_changelog("matrix-tools", root, specs, ["修好了"],
                                          True, ["matrix-tools#162"])
        except SystemExit as e:
            failures.append(f"用例4g：号绑得到却被误拦（不许误伤正常发版）：{str(e)[:200]}")
    # 建节路径：新建的版本节必须**绑得到**这些 issue（#162）
    # ⚠️ 这一条同时覆盖「送检正文要带 `- ` 前缀」这个坑：少了前缀第一条就不是
    #    bullet ⇒ 定位器看不到条目 ⇒ 会把**正常**的写法误判成没绑到（误伤发版）。
    try:
        release_train.check_new_section_binding(["修好了"], "matrix-tools", "v0.1.99",
                                                ["matrix-tools#162"])
    except SystemExit:
        pass
    else:
        failures.append("用例4h：新建版本节里号绑不到条目却放行（建节路径没判）")
    try:
        release_train.check_new_section_binding(["修好了（#162）"], "matrix-tools",
                                                "v0.1.99", ["matrix-tools#162"])
    except SystemExit as e:
        failures.append(f"用例4i：bullet 里带了号却被误拦（不许误伤正常发版）：{str(e)[:200]}")
    # dry-run 也要判（预览时就该看见这个问题，不能「dry-run 绿、apply 才炸」）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "CHANGELOG.md").write_text("# Changelog\n\n", encoding="utf-8")
        specs = [("main", root, "v0.1.99")]
        try:
            release_train.ensure_changelog("matrix-tools", root, specs, ["修好了"],
                                          False, ["matrix-tools#162"])
        except SystemExit:
            pass
        else:
            failures.append("用例4j：dry-run 时号绑不到条目却放行"
                            "（预览就该看见这个问题）")
    return failures


def run_binding_mutation(mutate) -> list[str]:
    saved = release_train.unbound_issues
    try:
        mutate()
        return issue_binding_assertions()
    finally:
        release_train.unbound_issues = saved


def _mutate_never_unbound():
    release_train.unbound_issues = lambda section, issues: []


def _mutate_always_unbound():
    release_train.unbound_issues = lambda section, issues: list(issues or [])


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

    # 3e：★ main_tag 为空（只发嵌套模块）⇒ **不得产出双空格文案**
    #   实测旧形态：f"跟随 {repo} {main_tag} 发版" 在 main_tag="" 时 ⇒
    #   "跟随 registry  发版" ⇒ 一句没人看得懂的文案进了版本节。
    got_empty = release_train.format_bullets([], True, "demo", "")
    if "  " in got_empty:
        failures.append(f"用例3e：main_tag 为空时产出双空格文案：{got_empty!r}")
    if "跟随 demo  发版" in got_empty:
        failures.append(f"用例3e：main_tag 为空时仍在写父仓版本号：{got_empty!r}")
    if "父模块" not in got_empty:
        failures.append(f"用例3e：main_tag 为空时应说明随父模块发布：{got_empty!r}")

    # 3f：★ 只发嵌套模块（无 main=）⇒ **必须被守卫拦下**并给出正确用法
    #   旧形态会一路漏下去：分支名变成 `chore/rel-`（无版本号）、
    #   嵌套模块脱离父仓单独发版 ⇒ 下游解析失败（matrix-tools#148）。
    import subprocess
    import sys as _sys
    with tempfile.TemporaryDirectory() as _td:
        _ws = Path(_td) / "registry"
        _ws.mkdir(parents=True)
        # repo_dir() 的布局要求：仓根有 go.mod（或 README.md）；嵌套模块在自己的子目录
        (_ws / "go.mod").write_text("module github.com/oekit/registry\n")
        (_ws / "nacos").mkdir()
        (_ws / "nacos" / "go.mod").write_text("module github.com/oekit/registry/nacos\n")
        _env = dict(os.environ, MATRIX_BASE=str(Path(_td)))
        _p = subprocess.run(
            [_sys.executable, str(Path(__file__).resolve().parent / "release_train.py"),
             "registry", "nacos=v0.1.2"],
            capture_output=True, text=True, env=_env, cwd=str(Path(__file__).resolve().parent),
        )
        _out = (_p.stdout or "") + (_p.stderr or "")
    if _p.returncode == 0:
        failures.append(f"用例3f：只发嵌套模块应当被拦下（returncode=0）：{_out[:200]!r}")
    if "main=" not in _out:
        failures.append(f"用例3f：报错里应给出正确用法（提到 main=）：{_out[:200]!r}")

    failures.extend(issue_binding_assertions())

    if failures:
        print("✗ release_train CHANGELOG 环节自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ release_train CHANGELOG 环节自测通过：共享脚本定位得到（找不到会告警）、多条 bullet 不丢条目、"
          "--issue 绑不到条目不放行")

    # 变异自证：绑定判据必须有牙齿（#162）
    missed: list[str] = []
    for name, mutate in (("绑定判据恒不报（永远放行）", _mutate_never_unbound),
                         ("绑定判据恒报（永远阻断）", _mutate_always_unbound)):
        f = run_binding_mutation(mutate)
        if not f:
            missed.append(name)
            print(f"✗ 变异「{name}」没有被抓住——这份断言没有牙齿")
        else:
            print(f"✓ 变异「{name}」被抓住（{len(f)} 条断言转红）")
    if missed:
        raise SystemExit(1)
    print("✓ 绑定判据变异自证 2/2 全部转红")


if __name__ == "__main__":
    main()
