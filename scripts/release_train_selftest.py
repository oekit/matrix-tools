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


def nested_binding_assertions() -> list[str]:
    """#208：嵌套模块的回执必须**定位得到条目**，且「只发嵌套」时不得写假陈述。

    三条判据（缺一条就是缺陷）：
      ① 嵌套面的 bullet **含 issue 号** ⇒ 定位器 entries=1（回执能讲清本条改了什么）；
      ② 只发嵌套时**不得**声称「父模块一并发布」（父仓本次并不发版 ⇒ 假陈述）；
      ③ fail-closed 门禁**逐面**校验 ⇒ 嵌套面没绑上 ⇒ 中止发版。

    ★ ③ 的构造方式：先用变异把嵌套面**退回旧行为**（issue 只写在「关联问题单」行），
      再看门禁拦不拦 ⇒ 拦 ⇒ 逐面校验有牙齿；不拦 ⇒ 门禁只验了根面（假保证）。
    """
    failures: list[str] = []
    bullets = ["build: 把父仓地板带进已发布产物（registry#44）"]
    issues = ["registry#44"]

    # 4g：嵌套面的 bullet 必须含 issue 号（回执按号在版本节里定位条目）
    nested = release_train.format_bullets(bullets, True, "registry", "", issues)
    first = nested.split("\n\n")[0]
    if "registry#44" not in first:
        failures.append(f"用例4g：嵌套版本的 bullet 里必须含 issue 号：{nested!r}")
    if release_train.unbound_issues("- " + nested, issues):
        failures.append(f"用例4g：嵌套版本节必须绑得上 issue（回执 entries=1）：{nested!r}")

    # 4h：只发嵌套时不得写「父模块一并发布」（进已发布产物 ⇒ 假陈述）
    if "父模块一并发布" in nested:
        failures.append(f"用例4h：只发嵌套时不得声称父模块一并发布（假陈述）：{nested!r}")

    # 4i：带 main= 时保持既有文案（跟随父仓发版）
    nested_main = release_train.format_bullets(bullets, True, "registry", "v0.5.4", issues)
    if "跟随 registry v0.5.4 发版" not in nested_main:
        failures.append(f"用例4i：带 main= 时应保持「跟随父仓发版」文案：{nested_main!r}")

    # 4j：★ 嵌套面没绑上 ⇒ 门禁必须 fail closed（逐面校验）
    saved = release_train.format_bullets
    release_train.format_bullets = _nested_face_old_behavior(saved)
    try:
        raised = False
        try:
            release_train.check_new_section_binding(bullets, "registry", "", issues)
        except SystemExit:
            raised = True
    finally:
        release_train.format_bullets = saved
    if not raised:
        failures.append("用例4j：嵌套面绑不上 issue 时必须中止发版（逐面校验，fail closed）")
    return failures


def _nested_face_old_behavior(saved):
    """造一个「嵌套面退回旧行为」的 format_bullets（issue 只写在「关联问题单」行）。"""
    def mutated(bullets, nested_self, repo, main_tag, issues=None):
        if nested_self:
            body = (f"跟随 {repo} {main_tag} 发版（release_train 嵌套 train）"
                    if main_tag else
                    "随本次 train 的父模块一并发布（release_train 嵌套 train）")
            if issues:
                body += "\n\n关联问题单：" + "、".join(issues) + "。"
            return body
        return saved(bullets, nested_self, repo, main_tag, issues)
    return mutated


def run_nested_mutation(mutate) -> list[str]:
    saved_fb = release_train.format_bullets
    saved_cs = release_train.check_new_section_binding
    try:
        mutate()
        return nested_binding_assertions()
    finally:
        release_train.format_bullets = saved_fb
        release_train.check_new_section_binding = saved_cs


def _mutate_nested_face_old():
    """嵌套面退回旧行为（issue 只写在「关联问题单」行）⇒ 用例4g/4h 必须转红。"""
    release_train.format_bullets = _nested_face_old_behavior(release_train.format_bullets)


def _mutate_root_face_only():
    """门禁只校验根面（不校验嵌套面）⇒ 用例4j 必须转红。"""
    def mutated(bullets, repo, main_tag, issues):
        body = "- " + release_train.format_bullets(bullets, False, repo, main_tag, issues)
        bad = release_train.unbound_issues(body, issues)
        if bad:
            raise SystemExit(release_train.issue_binding_msg(
                repo, main_tag or "（本次 train 的嵌套模块）", bad,
                "新建的根面版本节里 issue 号只出现在「关联问题单」行、没绑到具体条目"))
    release_train.check_new_section_binding = mutated


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

    # 3f：★ 只发嵌套模块（无 main=）⇒ **不得**被一刀切拦下
    #   ⚠️ **旧断言是陈旧的**（#203 起守卫改成**条件**守卫）：只有「存在未发布的
    #   require」才拦；require 全已发布 ⇒ **必须放行**。旧断言「应当被拦下」是
    #   #203 之前的旧行为，而且**靠无关失败蒙对**（fixture 不是 git 仓 ⇒ 命令因
    #   「须在 main 上」退出 1）⇒ **假绿**。
    #   ⇒ 现在只断言「不得出现一刀切的拦截文案」；放行/拒绝两分档由
    #   release_train_requires_selftest.py 的用例 11-14 覆盖（那层有牙齿）。
    #   ⚠️ 判据只认**拦截标识**「不许发」——放行提示里也有「没有父模块 main=」字样
    #   （那是说明文字，不是拦截）⇒ 拿它当拦截特征会把放行误判成拦截。
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
    if "不许发" in _out:
        failures.append(f"用例3f：require 全已发布时**不得**被一刀切拦下：{_out[:200]!r}")
    if "都已发布" not in _out:
        failures.append(f"用例3f：放行时应说明已核「require 都已发布」：{_out[:200]!r}")

    # 3g：★ 嵌套模块**没有**独立 CHANGELOG（logx 仓形态）⇒ 版本节只写根，
    #   且**不得**为它新建空文件（新建会造出「嵌套模块有独立 CHANGELOG」的假形态）。
    #   旧实现 `path.read_text()` ⇒ FileNotFoundError ⇒ logx 这种仓根本发不了版。
    with tempfile.TemporaryDirectory() as _td3:
        _missing = Path(_td3) / "hlog" / "CHANGELOG.md"
        try:
            _r = release_train.insert_top(_missing, "## [v0.1.7]", "- x")
        except Exception as _e:  # noqa: BLE001 - 旧实现会抛 FileNotFoundError
            failures.append(f"用例3g：文件不存在时**不得抛异常**（旧实现 read_text 会炸）：{_e!r}")
            _r = "raised"
        if _r is not False:
            failures.append(f"用例3g：文件不存在时应返回 False（跳过），实得 {_r!r}")
        if _missing.exists():
            failures.append(f"用例3g：**不得**为不存在的嵌套路径新建 CHANGELOG.md：{_missing}")

    failures.extend(issue_binding_assertions())
    failures.extend(nested_binding_assertions())

    if failures:
        print("✗ release_train CHANGELOG 环节自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ release_train CHANGELOG 环节自测通过：共享脚本定位得到（找不到会告警）、多条 bullet 不丢条目、"
          "--issue 绑不到条目不放行、嵌套面也绑号且只发嵌套不写假陈述")

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

    # 变异自证（#208）：嵌套面的两处修复**必须各有牙齿**
    missed2: list[str] = []
    for name, mutate in (("嵌套面退回旧行为（issue 只写在「关联问题单」行）",
                          _mutate_nested_face_old),
                         ("门禁只校验根面（不校验嵌套面）", _mutate_root_face_only)):
        f = run_nested_mutation(mutate)
        if not f:
            missed2.append(name)
            print(f"✗ 变异「{name}」没有被抓住——这份断言没有牙齿")
        else:
            print(f"✓ 变异「{name}」被抓住（{len(f)} 条断言转红）")
    if missed2:
        raise SystemExit(1)
    print("✓ 嵌套面绑定变异自证 2/2 全部转红")


if __name__ == "__main__":
    main()
