#!/usr/bin/env python3
"""gen_releases BOM（伞仓 oekit）覆盖与 Release 正文的自测（matrix-tools#251 / #252）。

**这两条防的是什么**：
  - #251：伞仓 BOM 是 37 个发版单元里的 1 个，但此前不在 gen_releases 的
    MODULES 枚举 ⇒ BOM 的 tag 永远不会被对账/补建 Release（存量：v0.2.7 有
    tag 无 Release，--apply 也修不上）；
  - #252：release_post 建的 BOM Release 正文是**写死的占位串** ⇒ 每版都不含
    「本次钉住了哪些版本」——而 BOM 这个产物的全部意义就是声明一组互相
    兼容的模块版本。修法：正文从**该 tag 的 go.mod 实际内容**渲染
    （钉住多少模块 + 相对上一版的地板 diff）。

断言（每条都要能判红）：
  1. release_repos：缺省枚举含 BOM_REPO、不含于 MODULES、单数 --repo 直通；
  2. 口径自检（#251 验收 4）：GO_MODULE_COUNT（37 个发版单元）与
     「MODULES + 嵌套 + BOM」一致 ⇒ 枚举少一个单元就能被抓住；
  3. bom_release_notes：正文含「N 个模块」+ 至少一个模块名 + 地板 diff
     （`旧 → 新`）+ 新增/移除标注；prev 缺失 ⇒ 无 diff 段（不编造）；
     go.mod 读不到 ⇒ 如实写「未核对」（不编造内容）；
  4. release_post 的 --bom-release 续跑幂等已接线（源码契约：按**远端事实**
     决定动作 + 正文走 bom_release_notes）。

**变异自证（#252 验收 4）**：把正文渲染退化回**固定串** ⇒ 本自测必须转红
⇒ 证明「正文必须含模块清单」的断言有牙齿（防再次退化成占位符）。

**无副作用、不联网**：read_at 注入（内存 go.mod 文本），不建 git 仓不打 API。

用法：python3 scripts/gen_releases_bom_selftest.py [--mutate]
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import matrix  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


CUR_GOMOD = """module github.com/oekit/oekit

go 1.26.0

// ---- oekit BOM：矩阵兼容快照的地板 require（勿手改/勿 tidy，见 README）----
require (
\tgithub.com/oekit/bootstrap v0.6.15
\tgithub.com/oekit/concurrency v0.28.4
\tgithub.com/oekit/pkg v0.37.4
)
"""

PREV_GOMOD = """module github.com/oekit/oekit

go 1.26.0

require (
\tgithub.com/oekit/bootstrap v0.6.15
\tgithub.com/oekit/concurrency v0.28.3
\tgithub.com/oekit/pkg v0.37.3
\tgithub.com/oekit/redisx v0.8.0
)
"""


def read_at_from(texts: dict[str, str | None]):
    def read_at(_dir, tag: str) -> str | None:
        return texts.get(tag)
    return read_at


def case_release_repos() -> None:
    import gen_releases
    repos = gen_releases.release_repos(None)
    check(matrix.BOM_REPO in repos, f"用例1 缺省枚举必须含伞仓 {matrix.BOM_REPO}（#251）：{repos[-3:]}")
    check(matrix.BOM_REPO not in matrix.MODULES,
          "伞仓不该混进 MODULES（那会污染 versions.json/地板统计的口径）")
    check(repos == list(matrix.MODULES) + [matrix.BOM_REPO],
          "用例1 枚举 = MODULES + BOM（缺一个单元就是 #251 的缺口形态）")
    check(gen_releases.release_repos("data") == ["data"], "用例1 --repo 单数直通")
    # 口径自检（#251 验收 4）：37 个发版单元 = 24 根 + 12 嵌套 + 1 BOM
    check(matrix.GO_MODULE_COUNT == len(matrix.MODULES) + len(matrix.NESTED_MODULES)
          + matrix.BOM_RELEASE_UNIT,
          f"用例2 口径自检失败：GO_MODULE_COUNT={matrix.GO_MODULE_COUNT} "
          f"≠ {len(matrix.MODULES)}+{len(matrix.NESTED_MODULES)}+1")


def case_bom_notes() -> None:
    import gen_releases
    read_at = read_at_from({"v0.2.8": CUR_GOMOD, "v0.2.7": PREV_GOMOD})
    notes = gen_releases.bom_release_notes("/fake/oekit", "v0.2.8", "v0.2.7", read_at)
    # a. 正文含模块数与**至少一个模块名**（#252 验收 1/4——防退化成固定串）
    check("3 个模块" in notes, f"用例3a 正文没有模块数：{notes[:200]}")
    check("github.com/oekit/pkg" in notes and "github.com/oekit/concurrency" in notes,
          f"用例3a 正文必须出现钉住的模块名（不许是占位符）：{notes[:300]}")
    # b. 地板 diff：新旧版本号成对出现
    check("v0.37.3 → v0.37.4" in notes, f"用例3b 缺 pkg 的地板 diff：{notes}")
    check("v0.28.3 → v0.28.4" in notes, f"用例3b 缺 concurrency 的地板 diff：{notes}")
    check("新增" in notes or "移除" in notes, f"用例3b 缺新增/移除标注：{notes}")
    check("github.com/oekit/redisx" in notes, "用例3b 移除的模块（redisx）该被列出")
    # c. prev 缺失 ⇒ 无 diff 段（不编造）
    notes_nodiff = gen_releases.bom_release_notes("/fake/oekit", "v0.2.8", None, read_at)
    check("相对上一版" not in notes_nodiff,
          "用例3c prev 缺失时不该有 diff 段（没事实就别写）")
    check("3 个模块" in notes_nodiff, "用例3c 钉住的组合段照常输出")
    # d. go.mod 读不到 ⇒ 如实「未核对」，不编造
    read_none = read_at_from({"v0.2.8": None})
    notes_none = gen_releases.bom_release_notes("/fake/oekit", "v0.2.8", None, read_none)
    check("未核对" in notes_none and "3 个模块" not in notes_none,
          f"用例3d go.mod 读不到必须如实说「未核对」：{notes_none[:300]}")


def case_release_post_wired() -> None:
    """源码契约：release_post 的 --bom-release 按**远端事实**续跑 + 正文走渲染。"""
    src = (ROOT / "scripts" / "release_post.py").read_text(encoding="utf-8")
    check("_remote_release_exists" in src,
          "release_post 缺 _remote_release_exists（续跑幂等的判据没接线，#252）")
    check("bom_release_notes" in src,
          "release_post 的 BOM Release 正文没走 bom_release_notes（#252）")
    check("BOM 推荐组合更新（release_post 自动跟进）。" not in src,
          "release_post 还有写死的 BOM 占位串（#252 的病灶还在）")
    check("--bom-release 可自动完成；PR 已合入而 tag 未打时" in src,
          "release_post 的「待办」提示没说明重跑的可执行性（#252 修法 3）")


def main() -> None:
    mutated = "--mutate" in sys.argv
    if mutated:
        # ★ 变异（#252 验收 4「把 diff 换成固定串」）：正文渲染退化成占位符
        #   ⇒ 用例 3a/3b 必须转红（防再次退化成「BOM 推荐组合更新。」）
        import gen_releases

        def fixed_notes(_oekit_dir, _tag, _prev_tag=None, _read_at=None):
            return "BOM 推荐组合更新（release_post 自动跟进）。\n"

        gen_releases.bom_release_notes = fixed_notes
        print("== 变异模式：BOM Release 正文退化成固定串 ==")
        case_bom_notes()
        if FAILURES:
            print(f"✓ 变异被抓住（{len(FAILURES)} 条断言转红）")
            return
        print("✗ 变异没有让任何断言失败 ⇒ 判据没牙齿")
        raise SystemExit(1)
    case_release_repos()
    case_bom_notes()
    case_release_post_wired()
    if FAILURES:
        print("✗ 自测失败：")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("✓ BOM 覆盖自测通过：gen_releases 枚举含伞仓（口径自检对得上 37）、"
          "BOM Release 正文从该 tag 的 go.mod 渲染（模块数 + 地板 diff）、"
          "release_post 续跑幂等已接线")


if __name__ == "__main__":
    main()
