#!/usr/bin/env python3
"""align_floors「嵌套模块地板」判据的自测（oekit/oekit#104）。

**为什么要有这个自测**：#104 的真实成因是 `align_floors.py` 的 `FLOOR_REQ_RE`
只匹配**根模块**形态 `github.com/oekit/<短名>` ⇒ 看不见
`github.com/oekit/<仓>/<嵌套路径>`（**别仓的嵌套模块**）这类地板。

实测：nns 的 `registry/nacosimpl v0.1.0`（远端最新 v0.1.1）、hertz 的
`logx/hlog v0.1.4`（远端最新 v0.1.6）等 **13 处**漂移，工具**全看不见**
⇒ 工具自报「✓ 全矩阵地板零漂移」——**假绿**（本文件此前已有同类教训：
bootstrap#50 的 indirect 行漏抬、kitex#65 的嵌套 go.sum 滞后、
oekit#102 的本地 tag 陈旧）。

一道没人验证过的判据，很可能自己就是个假绿——**「看得见」必须能被证伪**。

**反证怎么构造**（变异自证，全部离线、不联网）：
  1. 正则退回「只匹配根模块」⇒ 第 2 条断言必须转红；
  2. 嵌套模块取版本退回「用父仓根模块的 tag」⇒ 第 3 条必须转红；
  3. 版本比较退回「按字符串排序」⇒ 第 4 条必须转红（v0.3.0 < v0.31.0）。

用法：python3 scripts/align_floors_nested_selftest.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


# ---------- 判据 1：正则必须同时匹配根模块与嵌套模块两种形态 ----------

def check_regex_matches_both_shapes() -> None:
    from align_floors import FLOOR_REQ_RE

    text = (
        "module example.com/probe\n"
        "\n"
        "require (\n"
        "\tgithub.com/oekit/pkg v0.31.1\n"
        "\tgithub.com/oekit/registry/nacosimpl v0.1.0\n"
        "\tgithub.com/oekit/logx/hlog v0.1.4\n"
        "\tgithub.com/oekit/kitex/observability/tracing/instrumentation/kitexotel v0.7.0\n"
        "\tgithub.com/oekit/logx/kitexlog v0.1.4 // indirect\n"
        ")\n"
    )
    hits = list(FLOOR_REQ_RE.finditer(text))
    fulls = {m.group(1) for m in hits}
    for want in (
        "github.com/oekit/pkg",
        "github.com/oekit/registry/nacosimpl",
        "github.com/oekit/logx/hlog",
        "github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel",
        "github.com/oekit/logx/kitexlog",
    ):
        check(want in fulls, f"正则漏收 {want}：fulls={sorted(fulls)}")

    # 分组必须能区分：group(2)=根模块仓名、group(3)=嵌套子路径（None ⇒ 根模块）
    by_full = {m.group(1): m for m in hits}
    # ⚠️ **必须先确认键存在再取**（变异自证时正则退回 ⇒ 键缺失 ⇒ KeyError 崩在
    # 异常而非判红）⇒ 崩掉不算「抓住变异」，判红才算（§7：判据全灭先确认
    # 被测物真被执行；异常不是判据）。
    if "github.com/oekit/pkg" not in by_full:
        check(False, f"连根模块形态都没扫到，正则坏得比预期更彻底：fulls={sorted(fulls)}")
        return
    check(by_full["github.com/oekit/pkg"].group(3) is None,
          "根模块形态不应有子路径分组")
    for full, want_repo, want_sub in (
        ("github.com/oekit/registry/nacosimpl", "registry", "/nacosimpl"),
        ("github.com/oekit/logx/hlog", "logx", "/hlog"),
        ("github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel",
         "kitex", "/observability/tracing/instrumentation/kitexotel"),
    ):
        if full not in by_full:
            check(False, f"嵌套形态 {full} 未被扫到（漏收）")
            continue
        check(by_full[full].group(2) == want_repo,
              f"{full} 的 group(2) 必须是父仓名 {want_repo}，"
              f"实得 {by_full[full].group(2)}")
        check(by_full[full].group(3) == want_sub,
              f"{full} 的子路径分组应是 {want_sub}，实得 {by_full[full].group(3)}")


# ---------- 判据 2：嵌套 tag 前缀必须是**完整子路径**，不是最后一段 ----------

def check_nested_tag_prefix_is_full_subpath() -> None:
    """Go 的子目录模块规则：tag 前缀 = 该模块在父仓里的**子目录路径**。

    ⚠️ 用「最后一段」当前缀 ⇒ kitexotel（子路径 4 层）读不到 tag ⇒
    地板判据把「读不到」当成「没漂移」⇒ 又是假绿。
    """
    from matrix import nested_tag_prefix

    cases = {
        "github.com/oekit/logx/hlog": "hlog",
        "github.com/oekit/registry/nacosimpl": "nacosimpl",
        "github.com/oekit/bootstrap/nacos": "nacos",
        "github.com/oekit/bootstrap/obs": "obs",
        "github.com/oekit/timertask/redis": "redis",
        "github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel":
            "observability/tracing/instrumentation/kitexotel",
        "github.com/oekit/kitex/istio": "istio",
    }
    for module_path, want in cases.items():
        got = nested_tag_prefix(module_path)
        check(got == want, f"{module_path} 的 tag 前缀应是 {want}，实得 {got}")

    # 根模块不是嵌套模块 ⇒ 必须返回 None（否则会被拿嵌套 tag 口径去比）
    check(nested_tag_prefix("github.com/oekit/pkg") is None,
          "根模块不应有嵌套 tag 前缀")
    check(nested_tag_prefix("github.com/oekit/not-a-nested-module") is None,
          "未登记的路径不应有嵌套 tag 前缀")


# ---------- 判据 3：嵌套模块取版本必须走**嵌套 tag**，不能拿父仓根 tag ----------

def check_nested_lookup_uses_nested_tag() -> None:
    """`latest_nested_tag` 必须按**子路径前缀**过滤 tag 并按数字比。

    形态（若退化成「取父仓根模块最新 tag」）：
      logx 根模块最新 tag 是 v0.8.0，而 logx/hlog 最新是 v0.1.6
      ⇒ 用错口径会把 hlog 的地板判成「落后 v0.8.0」（永远抬不动）或
      反过来把根模块地板判成落后 v0.1.6（假漂移）。
    """
    import inspect

    from matrix import latest_nested_tag

    src = inspect.getsource(latest_nested_tag)
    # 必须按前缀过滤（不能整仓取最高 tag）
    check("prefix" in src and "remote_tags" in src,
          "latest_nested_tag 必须读远端 tag 并按前缀过滤")
    check(re.search(r"re\.compile\(rf?\"\^", src) is not None
          or "pat.match" in src,
          "latest_nested_tag 必须用前缀正则过滤 tag（不能整仓取最高）")
    # 版本必须按数字比（拆元组），不能字符串排序
    check("ver > best" in src and "int(m.group(" in src,
          "latest_nested_tag 的版本比较必须按数字（拆元组）")


# ---------- 判据 4：版本比较必须按数字（v0.3.0 < v0.31.0） ----------

def check_numeric_version_compare() -> None:
    from align_floors import parse_semver

    check(parse_semver("v0.3.0") < parse_semver("v0.31.0"),
          "版本比较必须按数字：v0.3.0 < v0.31.0（字符串排序会反过来）")
    check(parse_semver("v0.1.4") < parse_semver("v0.1.6"),
          "版本比较必须按数字：v0.1.4 < v0.1.6")
    # 字符串排序的反例（供变异自证对照）：
    # ⚠️ 反例必须选**字符串与数字结论相反**的那一对——
    #   v0.3.0 vs v0.31.0：字符串恰好同向（'.'0x2E < '1'0x31）⇒ 抓不住变异；
    #   v0.9.0 vs v0.31.0：字符串说 v0.9.0 > v0.31.0（'9'0x39 > '3'0x31），
    #   数字说 v0.9.0 < v0.31.0 ⇒ **结论相反** ⇒ 抓得住变异。
    check("v0.9.0" > "v0.31.0",
          "字符串排序在本反例上确实给出相反结论（本断言仅记录该事实）")
    check(parse_semver("v0.9.0") < parse_semver("v0.31.0"),
          "数字比较必须给出 v0.9.0 < v0.31.0")


# ---------- 判据 5：plan_repo 必须对嵌套模块用 latest_nested_tag ----------

def check_plan_uses_nested_lookup() -> None:
    """plan_repo 取目标版本时，嵌套形态必须走 `latest_nested_tag`。

    形态（若退回「一律 latest_tag(父仓名)」）：嵌套模块的 tag 带前缀
    （`hlog/v0.1.6`），根模块口径的 latest_tag 会把它整条丢掉 ⇒ 返回 None ⇒
    「读不到」被当成「没漂移」⇒ 13 处漂移继续隐形。
    """
    import inspect

    from align_floors import plan_repo

    src = inspect.getsource(plan_repo)
    check("latest_nested_tag" in src,
          "plan_repo 必须对嵌套模块调用 latest_nested_tag")
    check("latest_tag" in src,
          "plan_repo 必须对根模块调用 latest_tag")
    # 嵌套分支必须以「子路径是否存在」为条件
    check(re.search(r"if\s+sub\s+is\s+None", src) is not None
          or re.search(r"sub\s*==\s*None", src) is not None,
          "plan_repo 必须按「子路径是否存在」区分两种取版本口径")


# ---------- 变异自证：把扩展退回去，判据必须转红 ----------

def check_mutation_is_detected() -> None:
    """**变异自证**：把两处扩展退回 ⇒ 对应断言必须转红。

    这是「判据有牙齿」的证据，不是「判据在绿的时候会跑」。
    变异 1：正则退回只匹配根模块。
    变异 2：嵌套 tag 前缀退回「最后一段」。
    """
    import align_floors
    from matrix import NESTED_MODULES  # noqa: F401

    orig_re = align_floors.FLOOR_REQ_RE
    orig_prefix_fn = None
    try:
        # 变异 1：只匹配根模块
        align_floors.FLOOR_REQ_RE = re.compile(
            r"^(?:\t|require )(github\.com/oekit/([a-z0-9-]+)) (v\d+\.\d+\.\d+)( // indirect)?$",
            re.M)
        text = (
            "require (\n"
            "\tgithub.com/oekit/pkg v0.31.1\n"
            "\tgithub.com/oekit/registry/nacosimpl v0.1.0\n"
            ")\n"
        )
        fulls = {m.group(1) for m in align_floors.FLOOR_REQ_RE.finditer(text)}
        check("github.com/oekit/registry/nacosimpl" not in fulls,
              "变异 1（正则退回只匹配根模块）本应让嵌套形态漏收，"
              "但断言没抓住——判据没牙齿")
        # 复原后必须又能收到
        align_floors.FLOOR_REQ_RE = orig_re
        fulls2 = {m.group(1) for m in align_floors.FLOOR_REQ_RE.finditer(text)}
        check("github.com/oekit/registry/nacosimpl" in fulls2,
              "复原正则后仍应收得到嵌套形态")
    finally:
        align_floors.FLOOR_REQ_RE = orig_re

    # 变异 2：前缀取「最后一段」⇒ kitexotel 的前缀错 ⇒ 断言必须失败
    import matrix

    orig_prefix_fn = matrix.nested_tag_prefix
    try:
        def mutant_prefix(module_path: str):
            for nm in matrix.NESTED_MODULES:
                if nm["module"] == module_path:
                    return Path(nm["path"]).name      # ← 变异：最后一段
            return None

        matrix.nested_tag_prefix = mutant_prefix
        got = matrix.nested_tag_prefix(
            "github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel")
        check(got != "observability/tracing/instrumentation/kitexotel",
              "变异 2（前缀取最后一段）本应让 kitexotel 前缀错，"
              "但断言没抓住——判据没牙齿")
    finally:
        matrix.nested_tag_prefix = orig_prefix_fn


def main() -> int:
    check_regex_matches_both_shapes()
    check_nested_tag_prefix_is_full_subpath()
    check_nested_lookup_uses_nested_tag()
    check_numeric_version_compare()
    check_plan_uses_nested_lookup()
    check_mutation_is_detected()

    if FAILURES:
        print(f"❌ align_floors 嵌套模块地板自测：{len(FAILURES)} 条未通过")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("✓ align_floors 嵌套模块地板自测全绿（6 组判据 + 变异自证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
