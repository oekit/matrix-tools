#!/usr/bin/env python3
"""dependabot 目录门禁的自测：证明这道判据「抓得住自己要防的东西」，且不假绿。

**为什么要有这个自测**：audit.yml 的 selftest job 已经有这个原则——
「判据脚本自己先得能证明『它抓得住自己要防的东西』」。data#39 的教训尤其
适用：那个残留条目在 matrix.py、README、生成器里都被同步过了，**唯独
dependabot.yml 没人看**，而它造成的后果是「每周一个红 run、永远不自愈」。
一道没人验证过的门禁，很可能自己就是个假绿。

**反证怎么构造**：把判据里的两处关键逻辑分别掐掉，本自测必须**失败**：
  1. 「只认 gomod ecosystem」被掐 ⇒ 用例 3（github-actions 生态指向不存在的目录）
     应当误报 → 自测必须抓到。
  2. 「目录必须有 go.mod」被掐 ⇒ 用例 2 应当漏报 → 自测必须抓到。

**无副作用、不联网、不依赖工作区布局**：全部用临时目录造 fixture。

用法：python3 scripts/dependabot_dirs_selftest.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dependabot_dirs import check_dirs, gomod_dirs  # noqa: E402

# 有效配置：根模块 + 一个真实存在的嵌套模块
VALID = """version: 2
updates:
  - package-ecosystem: gomod
    directory: /
    schedule:
      interval: weekly
  - package-ecosystem: gomod
    directory: /sink
    schedule:
      interval: weekly
"""

# data#39 的原始形态：all/ 已删目录，但配置里还留着条目
STALE = """version: 2
updates:
  - package-ecosystem: gomod
    directory: /
    schedule:
      interval: weekly
  - package-ecosystem: gomod
    directory: /all
    schedule:
      interval: weekly
"""

# 目录在，但没有 go.mod（路径写错）
NO_GOMOD = """version: 2
updates:
  - package-ecosystem: gomod
    directory: /notamodule
    schedule:
      interval: weekly
"""

# 非 gomod 生态：目录不存在也**不该**判红（否则 .github / matrix-tools / 站点仓全假红）
OTHER_ECO = """version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
  - package-ecosystem: npm
    directory: /
    schedule:
      interval: weekly
"""


def make_repo(root: Path, dep_yaml: str, dirs: tuple[str, ...] = ("sink",)) -> Path:
    """造一个最小仓：根 go.mod + .github/dependabot.yml + 指定目录。

    `dirs` 里的目录会被建出来；其中 `sink` 额外放一个 go.mod（当嵌套模块），
    其余目录**故意不放** go.mod——用来造「目录在但不是模块」的形态。
    """
    repo = root / "fakerepo"
    (repo / ".github").mkdir(parents=True, exist_ok=True)
    (repo / "go.mod").write_text("module github.com/oekit/fakerepo\n", encoding="utf-8")
    (repo / ".github" / "dependabot.yml").write_text(dep_yaml, encoding="utf-8")
    for d in dirs:
        (repo / d).mkdir(parents=True, exist_ok=True)
    if "sink" in dirs:
        (repo / "sink" / "go.mod").write_text(
            "module github.com/oekit/fakerepo/sink\n", encoding="utf-8")
    return repo


def main() -> None:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 用例 1：有效配置（根 + 真实嵌套模块）⇒ 必须零问题
        repo = make_repo(tmp / "c1", VALID)
        problems = check_dirs(repo, VALID)
        if problems:
            failures.append(f"用例1 期望零问题，实得 {problems}")

        # 用例 2：目录存在但没有 go.mod ⇒ 必须抓到
        repo = make_repo(tmp / "c2", NO_GOMOD, dirs=("notamodule",))
        problems = check_dirs(repo, NO_GOMOD)
        if not problems:
            failures.append("用例2 期望抓到「目录存在但无 go.mod」，实得零问题")
        elif "go.mod" not in problems[0]:
            failures.append(f"用例2 报错未点名 go.mod：{problems[0]}")

        # 用例 3：非 gomod 生态 ⇒ 必须**不**判红（防全矩阵假红）
        repo = make_repo(tmp / "c3", OTHER_ECO, dirs=())
        problems = check_dirs(repo, OTHER_ECO)
        if problems:
            failures.append(f"用例3 期望零问题（非 gomod 生态不该判红），实得 {problems}")

        # 用例 4（关键反证）：data#39 的原始形态 ⇒ 必须抓到
        repo = make_repo(tmp / "c4", STALE, dirs=())
        problems = check_dirs(repo, STALE)
        if not problems:
            failures.append("用例4 期望抓到 data#39 的残留目录条目，实得零问题")
        elif "/all" not in problems[0]:
            failures.append(f"用例4 报错未点名 /all：{problems[0]}")
        elif "不存在的目录" not in problems[0]:
            # 必须断言**具体措辞**而非只断言点名了 /all：目录不存在时，
            # 「没有 go.mod」那条分支也会同时成立（目录都没有，go.mod 自然也没有）
            # ⇒ 只断言 /all 的话，掐掉「目录存在性检查」这条分支自测照样绿
            # ⇒ 门禁自己就成了它要防的那种假绿（2026-10-22 变异测试实测）
            failures.append(f"用例4 报错未区分「目录不存在」与「目录无 go.mod」：{problems[0]}")

        # 用例 5：解析层单独验——只取 gomod 的 directory 条目
        if gomod_dirs(OTHER_ECO) != []:
            failures.append(f"用例5 非 gomod 生态被误收：{gomod_dirs(OTHER_ECO)}")
        if gomod_dirs(STALE) != ["/", "/all"]:
            failures.append(f"用例5 解析结果不对：{gomod_dirs(STALE)}")

    if failures:
        print("✗ dependabot 目录门禁自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ dependabot 目录门禁自测通过：残留条目/无 go.mod 都抓得到；非 gomod 生态不误报")


if __name__ == "__main__":
    main()
