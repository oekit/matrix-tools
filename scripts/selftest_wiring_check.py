#!/usr/bin/env python3
"""自测接线漂移门禁（matrix-tools#260）：`scripts/*selftest.py` 的文件集合必须与
workflows 里实际调用的集合**完全相等**——「加了自测忘了接线」这一类漏接由机器发现。

为什么要有这道门禁：自测是判据脚本的唯一回归保护，而 CI 里唯一跑自测的地方就是
audit.yml 的 selftest job。#260 实测：`changelog_tag_snapshot_selftest.py`（#234
刚加的 13 个回归用例，含「判据失效即红」的命门面）与 `gen_versions_selftest.py`
在 4 条 workflow 里的引用数是 **0**——它们不是「跑着但红」，而是**根本没被调用**。
「文件数 31 vs 被引用 29」这种差值靠人记必漏，集合相等判定（本仓既有口径）不会漏。

判据（两条都红才算尽）：
  A. 每个自测文件都至少被一条 workflow 引用（漏接线 ⇒ 红）；
  B. 每条 workflow 里的自测引用都对应真实存在的文件（删文件忘删行 ⇒ 红跑空转）；
  C. 两个数（文件数 / 被引用数）打进日志，差值一眼可见。

变异自证（--mutate，合成夹具、不碰真仓库）：
  M1  合成 workflow 漏引一个自测文件 ⇒ 判据必须红；
  M2  合成 workflow 引用不存在的自测文件（幽灵）⇒ 判据必须红；
  M3  合成完全一致的夹具 ⇒ 判据必须放行。

用法：
    python3 scripts/selftest_wiring_check.py             # 对本仓真跑
    python3 scripts/selftest_wiring_check.py --mutate    # 变异自证
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SELFTEST_RE = re.compile(r"scripts/([A-Za-z0-9_]+selftest\.py)")


def wiring_diff(scripts_dir: Path, workflows_dir: Path) -> tuple[list[str], list[str]]:
    """返回 (漏接线的自测文件, 引用了不存在文件的幽灵行)。

    ⚠️ 读不到 workflow 目录 ⇒ 当「全部漏接」处理（fail-loud，#79：读不到 ≠ 没有）
    ——目录布局变了必须被人看见，不许静默判绿。"""
    workflow_files = sorted(workflows_dir.glob("*.yml")) if workflows_dir.is_dir() else []
    texts = {w.name: w.read_text(encoding="utf-8", errors="ignore")
             for w in workflow_files}
    if not texts:
        unwired = sorted(f.name for f in scripts_dir.glob("*selftest.py"))
        return unwired, []
    referenced: dict[str, list[str]] = {}
    for name, text in texts.items():
        for m in SELFTEST_RE.finditer(text):
            referenced.setdefault(m.group(1), []).append(name)
    unwired = sorted(f.name for f in scripts_dir.glob("*selftest.py")
                     if f.name not in referenced)
    phantom = sorted(f"{name} ← {', '.join(sorted(set(where)))}"
                     for name, where in referenced.items()
                     if not (scripts_dir / name).is_file())
    return unwired, phantom


def main() -> int:
    if "--mutate" in sys.argv:
        return _mutate()
    unwired, phantom = wiring_diff(REPO / "scripts", REPO / ".github" / "workflows")
    n_files = len(list((REPO / "scripts").glob("*selftest.py")))
    n_referenced = len(set(SELFTEST_RE.findall(
        "".join(p.read_text(encoding="utf-8", errors="ignore")
                for p in sorted((REPO / ".github" / "workflows").glob("*.yml"))))))
    print(f"自测接线读数：scripts/*selftest.py {n_files} 个；"
          f"workflow 引用 {n_referenced} 个（差值必须为 0）")
    if unwired or phantom:
        print("✗ 自测接线漂移：")
        for f in unwired:
            print(f"  - 自测文件未被任何 workflow 调用（CI 里零回归保护）：scripts/{f}")
        for p in phantom:
            print(f"  - workflow 引用了不存在的自测（幽灵引用/删文件忘删行）：{p}")
        print("修法：把漏接的自测补进 .github/workflows/audit.yml 的 selftest job；"
              "幽灵引用删掉对应步骤。")
        return 1
    print("✓ 自测接线门禁通过：自测文件集合 == workflow 调用集合（无漏接、无幽灵）")
    return 0


def _mutate() -> int:
    """变异自证：合成夹具上验「漏接必红 / 幽灵必红 / 完整必绿」三向。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        scripts = root / "scripts"
        scripts.mkdir()
        for name in ("a_selftest.py", "b_selftest.py"):
            (scripts / name).write_text("# fixture\n", encoding="utf-8")
        wf = root / ".github" / "workflows"

        # M1：漏接线——workflow 只调用了 a，b 躺在 scripts/ 里没人跑
        wf.mkdir(parents=True)
        (wf / "audit.yml").write_text(
            "jobs:\n  selftest:\n    steps:\n      - run: python3 scripts/a_selftest.py\n",
            encoding="utf-8")
        unwired, phantom = wiring_diff(scripts, wf)
        m1 = unwired == ["b_selftest.py"] and not phantom
        print(f"  M1 {'✓' if m1 else '✗'} 漏接线必须点名 b_selftest.py："
              f"unwired={unwired} phantom={phantom}")

        # M2：幽灵引用——workflow 调了不存在的 c_selftest.py
        (wf / "audit.yml").write_text(
            "jobs:\n  selftest:\n    steps:\n"
            "      - run: python3 scripts/a_selftest.py\n"
            "      - run: python3 scripts/c_selftest.py\n", encoding="utf-8")
        unwired2, phantom2 = wiring_diff(scripts, wf)
        m2 = (unwired2 == ["b_selftest.py"]
              and any("c_selftest.py" in p for p in phantom2))
        print(f"  M2 {'✓' if m2 else '✗'} 幽灵引用必须点名 c_selftest.py："
              f"unwired={unwired2} phantom={phantom2}")

        # M3：完全一致 ⇒ 放行
        (scripts / "c_selftest.py").write_text("# fixture\n", encoding="utf-8")
        (wf / "audit.yml").write_text(
            "jobs:\n  selftest:\n    steps:\n"
            "      - run: python3 scripts/a_selftest.py\n"
            "      - run: python3 scripts/b_selftest.py\n"
            "      - run: python3 scripts/c_selftest.py\n", encoding="utf-8")
        unwired3, phantom3 = wiring_diff(scripts, wf)
        m3 = not unwired3 and not phantom3
        print(f"  M3 {'✓' if m3 else '✗'} 集合一致必须放行："
              f"unwired={unwired3} phantom={phantom3}")

        # M4：workflow 目录整个读不到 ⇒ fail-loud（全部算漏接，不许静默绿）
        unwired4, phantom4 = wiring_diff(scripts, root / "no-such-dir")
        m4 = len(unwired4) == 3 and not phantom4
        print(f"  M4 {'✓' if m4 else '✗'} workflows 目录缺失必须按「全漏接」红："
              f"unwired={unwired4}")

    if m1 and m2 and m3 and m4:
        print("✓ 变异自证 4/4：漏接/幽灵/完整/目录缺失 四向判定全部正确")
        return 0
    print("✗ 变异自证失败（见上）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
