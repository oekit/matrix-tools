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


def nested_note_assertions() -> list[str]:
    """#214：只发嵌套模块时，提示里的「模块=版本」**不得丢首段**。

    ★ **形态**（实测 2026-11-03）：``nested_only_gate`` 的 ``only`` 串有两个消费者 ——
      ① 给人读的文案；② **给人照抄的修法示例命令行**（``spec`` 形态 ``name=ver``）。
    ⇒ 旧实现 ``_t.split('/', 1)[1]`` 对**多段**嵌套模块是错的：
      tag ``observability/tracing/instrumentation/kitexotel/v0.7.6``
      ⇒ ``split('/', 1)[1]`` = ``tracing/instrumentation/kitexotel/v0.7.6``
      ⇒ **丢了首段 ``observability/``** ⇒ 文案给出**不存在的模块名**，
      且照抄示例命令会被 spec 解析的版本号格式校验**直接拒掉**（退出码 2）。

    ⇒ 正确取法 = **只取版本号**（``rsplit('/', 1)[-1]``）⇒ 单段/多段都对。

    ★ **判据不与被检对象共享逻辑**：期望值在这里**独立重算**（按 ``/`` 切分取最后一段），
      不复用 ``nested_only_gate`` 内部的表达式 ⇒ 否则改错实现会连判据一起改对（假绿）。
    """
    failures: list[str] = []

    # 夹具：三种形态 —— 单段嵌套 / 多段嵌套 / 多模块混排
    cases = [
        ("timertask", [("redis", "redis/v0.2.7")]),
        ("kitex", [("observability/tracing/instrumentation/kitexotel",
                    "observability/tracing/instrumentation/kitexotel/v0.7.6")]),
        ("bootstrap", [("nacos", "nacos/v0.3.10"), ("obs", "obs/v0.2.14")]),
    ]
    for repo, mods in cases:
        specs = [(name, None, tag) for name, tag in mods]
        # 放行侧文案：require 全已发布 ⇒ 走「放行」分支
        saved = release_train.check_requires_resolvable
        release_train.check_requires_resolvable = lambda *a, **k: None
        try:
            ok, msg = release_train.nested_only_gate(repo, specs)
        finally:
            release_train.check_requires_resolvable = saved
        if not ok:
            failures.append(f"用例5（{repo}）：夹具应走放行分支，却判成拒绝：{msg[:120]!r}")
            continue
        for name, tag in mods:
            # ★ 独立重算期望值：版本号 = tag 按 '/' 切分的最后一段
            want_ver = tag.split("/")[-1]
            want_pair = f"{name}={want_ver}"
            if want_pair not in msg:
                failures.append(
                    f"用例5（{repo}）：提示里必须出现「{want_pair}」"
                    f"（多段嵌套不得丢首段）���实际提示={msg.splitlines()[0]!r}")
            # ★ 旧实现的错形态必须**不出现**（这是本次缺陷的指纹）
            wrong = f"{name}={tag.split('/', 1)[1]}" if "/" in tag else None
            if wrong and wrong != want_pair and wrong in msg:
                failures.append(
                    f"用例5（{repo}）：提示里出现旧实现的错形态「{wrong}」"
                    f"（split('/',1) 丢首段）⇒ 实际提示={msg.splitlines()[0]!r}")

        # ★ **照抄的命令行必须能被 spec 解析接受**（`name=ver`，ver 须为 vX.Y.Z）
        saved = release_train.check_requires_resolvable
        release_train.check_requires_resolvable = (
            lambda repo_, name, d, planned, unresolved, tag_exists=None:
            unresolved.append("github.com/oekit/foo v1.2.3（未发布）"))
        try:
            ok2, msg2 = release_train.nested_only_gate(repo, specs)
        finally:
            release_train.check_requires_resolvable = saved
        if ok2:
            failures.append(f"用例5（{repo}）：夹具应走拒绝分支，却判成放行")
            continue
        # 从文案里抠出示例命令行的 spec 段（形如 `name=ver`）
        import re as _re
        m = _re.search(r"例：(python3 scripts/release_train\.py .*?--apply)", msg2)
        if not m:
            failures.append(f"用例5（{repo}）：拒绝文案里必须带「修法示例命令行」：{msg2[:200]!r}")
            continue
        for tok in m.group(1).split():
            if "=" not in tok or tok.startswith("main="):
                continue
            s_name, s_ver = tok.split("=", 1)
            if not _re.fullmatch(r"v\d+\.\d+\.\d+", s_ver):
                failures.append(
                    f"用例5（{repo}）：示例命令行里的 spec「{tok}」"
                    f"的版本号部分必须是 vX.Y.Z（照抄要被 spec 解析接受）")
            if s_name != name and s_name not in [n for n, _t in mods]:
                failures.append(
                    f"用例5（{repo}）：示例命令行里的模块名「{s_name}」不是本次 train 的模块")
    return failures


def _apply_note_mutation():
    """把 `only` / `only_specs` 退回**旧实现** ⇒ 返回 restore 回调。

    ★ 旧实现有两处错（实测 2026-11-03）：
      (a) `split('/', 1)[1]` ⇒ 多段嵌套**丢首段**；
      (b) 同一个串**当下标分隔（文案）与命令行参数用** ⇒ 多模块时照抄示例
          命令得到一个 token ⇒ 被 spec 格式校验拒掉。

    ★★ **为什么返回 restore 回调、而不是「改完就改回」**（实测踩过）：
      若在函数内部 `finally` 里立刻把源码改回 ⇒ **断言跑的时候代码已经复原**
      ⇒ 变异**根本没生效** ⇒ 判据报绿 ⇒ 「变异自证」变成**假证明**。
      ⇒ 必须让变异**在断言运行期间保持生效**，断言跑完再恢复。
      ⇒ 且变异后的源码必须**语法正确**（否则 reload 抛 SyntaxError ⇒ 同样静默失效）
        —— 故这里先 `ast.parse` 自检。

    ★ 变异体只改**被测函数源码里的那两行**（不替换整个函数）⇒ 只破坏
      「提示串的取值」这一处 ⇒ 断言必须**精确**转红。
    """
    import ast as _ast
    import importlib
    import re as _re
    path = Path(__file__).resolve().parent / "release_train.py"
    src = path.read_text(encoding="utf-8")
    mutated = src
    # (a)(b)：`only` 退回 split('/',1) ⇒ 多段丢首段
    mutated = _re.sub(
        r"^    only = \"、\"\.join\(f\"\{name\}=\{_t\.rsplit\('/', 1\)\[-1\]\}\" for name, _d, _t in specs\)$",
        "    only = \"、\".join(f\"{name}={_t.split('/', 1)[1]}\" for name, _d, _t in specs)",
        mutated, count=1, flags=_re.M)
    # (b)：`only_specs` 退回「与 only 同一个串」（旧实现没有这一行 ⇒ 用 only）
    mutated = _re.sub(
        r"^    only_specs = \" \"\.join\(f\"\{name\}=\{_t\.rsplit\('/', 1\)\[-1\]\}\" for name, _d, _t in specs\)$",
        "    only_specs = only",
        mutated, count=1, flags=_re.M)
    if mutated == src:
        raise SystemExit("变异自证失败：没找到 `only` / `only_specs` 的 rsplit 实现（代码形态已变？）")
    try:
        _ast.parse(mutated)
    except SyntaxError as _e:
        raise SystemExit(f"变异自证失败：变异后的源码语法错误（变异体自身有问题）：{_e}")
    path.write_text(mutated, encoding="utf-8")
    importlib.reload(release_train)

    def restore() -> None:
        path.write_text(src, encoding="utf-8")
        importlib.reload(release_train)

    return restore


def release_reconciliation_assertions() -> list[str]:
    """缺陷3（实测 2026-11-03）：**Release 创建失败仍打印「✓ train 完成」并 exit 0**
    ⇒ 「tag 已推但 Release 没建」在自动化口径下是**绿的**（发版链假绿）。

    旧实现：`create_releases` 失败只 ⚠（且**不返回失败数**）⇒ `main()` 随后
    **无条件**打印「✓ train 完成：N 个 tag 已推送」并 exit 0。

    三条判据（缺一条就是缺陷）：
      ① `create_releases` 必须**统计并返回失败数**（>0）；
      ② 收尾必须走 `report_train_result`：有失败 ⇒ **退出码非 0** 且
         **不许**打印「✓ train 完成」；无失败 ⇒ 照常打印并正常退出；
      ③ `main` 里**不许**再直接打印「✓ train 完成」（旧写法无条件打印）。

    ★ 无副作用、不联网：用**假 gh**（临时目录里的桩脚本）替掉真 gh。
    """
    failures: list[str] = []
    import types as _types

    def _fake_gh(td: str, rc: int) -> None:
        bindir = Path(td) / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        gh = bindir / "gh"
        gh.write_text(f"#!/bin/sh\necho 'gh: could not create release: HTTP 502' >&2\nexit {rc}\n",
                      encoding="utf-8")
        gh.chmod(0o755)
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")

    def _call_create_releases(gh_rc: int) -> object:
        fake_gr = _types.ModuleType("gen_releases")
        fake_gr.build_notes = lambda root, tag, repo: "notes"   # noqa: E731 - 桩
        saved_gr = sys.modules.get("gen_releases")
        sys.modules["gen_releases"] = fake_gr
        old_path = os.environ.get("PATH", "")
        try:
            with tempfile.TemporaryDirectory() as td:
                _fake_gh(td, gh_rc)
                root = Path(td) / "repo"
                root.mkdir()
                return release_train.create_releases(
                    "demo", root, [("main", root, "v0.1.58")])
        finally:
            os.environ["PATH"] = old_path
            if saved_gr is not None:
                sys.modules["gen_releases"] = saved_gr
            else:
                sys.modules.pop("gen_releases", None)

    # ① gh 失败 ⇒ 必须统计出失败数（=1）
    failed = _call_create_releases(1)
    if failed != 1:
        failures.append(f"用例6a：gh 创建失败时 create_releases 必须返回失败数 1（实得 {failed!r}）"
                        "——旧实现失败只 ⚠ 不计数 ⇒ main 照印「✓ train 完成」且 exit 0（假绿）")

    # ② gh 成功 ⇒ 失败数必须是 0（不许把成功也判成失败）
    ok = _call_create_releases(0)
    if ok != 0:
        failures.append(f"用例6b：gh 创建成功时 create_releases 必须返回 0（实得 {ok!r}）")

    # ③ 有失败 ⇒ 退出码非 0，且**不许**打印「✓ train 完成」
    buf = io.StringIO()
    code = None
    raised = False
    with redirect_stdout(buf):
        try:
            release_train.report_train_result([("main", None, "v0.1.58")], 1)
        except SystemExit as e:
            raised = True
            code = e.code
    out = buf.getvalue()
    if not raised or code not in (1, 2):
        failures.append(f"用例6c：Release 创建失败时收尾必须以**非 0 退出码**结束"
                        f"（实得 raised={raised} code={code!r}）——假绿的核心形态")
    if "✓ train 完成" in out:
        failures.append(f"用例6c：Release 创建失败时**不许**打印「✓ train 完成」：{out!r}")

    # ④ 无失败 ⇒ 打印「✓ train 完成」且**不**退出（不许误伤正常发版）
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            release_train.report_train_result([("main", None, "v0.1.58")], 0)
        except SystemExit as e:
            failures.append(f"用例6d：没有 Release 失败时不得退出（实得 code={e.code!r}）")
    if "✓ train 完成" not in buf.getvalue():
        failures.append(f"用例6d：无失败时必须打印「✓ train 完成」：{buf.getvalue()!r}")

    # ⑤ 结构：main 必须走 report_train_result，且「✓ train 完成」只在它里面
    tree = ast.parse(Path(release_train.__file__).read_text(encoding="utf-8"))
    main_fn = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main_fn is None:
        failures.append("用例6e：release_train.py 里找不到 main()")
        return failures
    calls_report = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "report_train_result"
                       for n in ast.walk(main_fn))
    if not calls_report:
        failures.append("用例6e：main 必须把 Release 失败数交给 report_train_result 判红")
    has_literal = any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and "✓ train 完成" in n.value for n in ast.walk(main_fn))
    if has_literal:
        failures.append("用例6e：main 里不许再直接打印「✓ train 完成」"
                        "（旧写法无条件打印 ⇒ Release 没建也是绿的）")
    return failures


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
    failures.extend(nested_note_assertions())
    failures.extend(release_reconciliation_assertions())

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

    # 变异自证（#214）：提示里的「模块=版本」必须**各有牙齿**
    #   ⇒ 退回旧实现（split('/',1) 丢首段 + 多模块串不可照抄）⇒ 用例5 必须转红。
    #   ⚠️ **变异必须在断言运行期间保持生效** ⇒ 用 restore 回调延后恢复
    #     （实测踩过：在变异函数内部 finally 里立刻恢复 ⇒ 断言跑时代码已复原
    #      ⇒ 变异「没生效」⇒ 判据报绿 ⇒ 变异自证变成**假证明**）。
    #   ⚠️ **精确性要求**：本次变异只动 `only` 两行 ⇒ 只应让**用例5**转红；
    #     若其它用例也红了 ⇒ 说明变异体改的范围比预期大（或用例5 与别处耦合）。
    saved_failures = list(failures)
    restore = _apply_note_mutation()
    try:
        mutated_failures = nested_note_assertions()
    finally:
        restore()
    if not mutated_failures:
        print("✗ 变异「only 退回 split('/',1)（多段嵌套丢首段）」没有被抓住——用例5 没有牙齿")
        raise SystemExit(1)
    print(f"✓ 变异「only 退回 split('/',1)（多段嵌套丢首段）」被抓住（{len(mutated_failures)} 条断言转红）")
    # 精确性：变异**只**应影响用例5 ⇒ 用例5 的失败条数须 > 0，
    #   且其余用例（1/2/3/4 系列）仍应全绿。
    _rest = [f for f in failures if not f.startswith("用例5")]
    if _rest != saved_failures:
        print("✗ 变异体影响了用例5 以外的断言（变异范围过大，精确性不达标）")
        for f in _rest:
            if f not in saved_failures:
                print("   -", f)
        raise SystemExit(1)
    print("✓ 变异精确性：只让用例5 转红，其余用例保持原状")


    # 变异自证（缺陷3）：**Release 创建失败必须判红** ⇒ 两条变异都要转红
    #   ⚠️ 变异必须在断言运行期间保持生效 ⇒ 断言跑完再恢复。
    missed3: list[str] = []
    saved_cr = release_train.create_releases
    saved_rt = release_train.report_train_result
    try:
        # 变异A：create_releases 退回旧行为（失败只告警，**不返回失败数**）
        release_train.create_releases = lambda repo, root, created: None   # noqa: E731
        f = release_reconciliation_assertions()
        if not f:
            missed3.append("create_releases 不统计失败数")
            print("✗ 变异「create_releases 不统计失败数」没有被抓住——用例6 没有牙齿")
        else:
            print(f"✓ 变异「create_releases 不统计失败数」被抓住（{len(f)} 条断言转红）")

        # 变异B：收尾退回旧行为（**无条件**打印「✓ train 完成」，不判红）
        release_train.create_releases = saved_cr
        release_train.report_train_result = (           # noqa: E731
            lambda created, rel_failed: print(f"\n✓ train 完成：{len(created)} 个 tag 已推送"))
        f = release_reconciliation_assertions()
        if not f:
            missed3.append("收尾无条件打印「✓ train 完成」")
            print("✗ 变异「收尾无条件打印「✓ train 完成」」没有被抓住——用例6 没有牙齿")
        else:
            print(f"✓ 变异「收尾无条件打印「✓ train 完成」」被抓住（{len(f)} 条断言转红）")
    finally:
        release_train.create_releases = saved_cr
        release_train.report_train_result = saved_rt
    if missed3:
        raise SystemExit(1)
    print("✓ Release 对账失败判红 变异自证 2/2 全部转红")


if __name__ == "__main__":
    main()
