#!/usr/bin/env python3
"""工具链闸门自测：证明这几道闸门「抓得住自己要防的东西」，且不假绿。

覆盖本轮（2026-10-30）新增/收紧的三处：

1. `prkit.may_merge_without_checks` —— auto-merge 不可用时，**只有** CLEAN
   （没有任何 required check 挂起）才允许立即合。
   旧实现是「auto 失败 ⇒ 无条件立即合」⇒ 在无分支保护的仓上等于不等 CI 就合，
   且命令成功退出、不报错 ⇒ 事后才发现时已经合进去了。
2. `gen_changelog.changelog_write_allowed` —— `--push` 不带 `--apply` ⇒ 拒绝。
   旧实现没有这层：`--push` 直接把**覆写后**的 CHANGELOG 推上 main。
3. `matrix.local_dir` —— 事实层只有一份；handoff.py / preflight.py 不再各自定义。
   旧形态是两份**逐字相同**的实现（重复的代价不是多几行，是改规则时只改一处）。

**反证怎么构造**：
  - 用例 1b/1c：把判据里的「状态必须等于 CLEAN」放宽成「非空即可」，
    或把 BLOCKED 也放行 ⇒ 用例必须转红；
  - 用例 2b：去掉 `push and not apply_` 这条 ⇒ 用例必须转红；
  - 用例 3b：在 handoff.py 里塞回一份 local_dir 定义 ⇒ 用例必须转红。

**无副作用、不联网、不依赖工作区布局**：全部用临时目录造 fixture。

用法：python3 scripts/toolchain_selftest.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from prkit import may_merge_without_checks  # noqa: E402
from matrix import local_dir  # noqa: E402


def main() -> None:
    failures: list[str] = []

    # ---------- 1) merge 的回退闸门 ----------
    if not may_merge_without_checks("CLEAN"):
        failures.append("用例1a 期望 CLEAN 可直接合（无 required check 挂起），实得不可合")
    for bad in ("BLOCKED", "BEHIND", "DIRTY", "UNKNOWN", "", "   ", None):
        if may_merge_without_checks(bad):
            failures.append(f"用例1b 期望 {bad!r} 不许直接合（checks 未绿/读不到），实得允许")

    # ---------- 2) gen_changelog 的写闸门 ----------
    # import 本身就是一道行为判据：旧版那行 `pathlib.Path(__file__).rsplit`
    # 在 **import 期**就 AttributeError ⇒ 这里的 import 必须成功。
    try:
        from gen_changelog import changelog_write_allowed  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        failures.append(f"用例4 gen_changelog import 期就炸（{exc!r}）⇒ 模块级代码不可执行")
        changelog_write_allowed = None  # type: ignore[assignment]

    if changelog_write_allowed is not None:
        try:
            got = changelog_write_allowed([])
            if got != (False, False):
                failures.append(f"用例2a 无参期望 (apply=False, push=False)，实得 {got}")
        except SystemExit:
            failures.append("用例2a 无参不该被拦")

        try:
            got = changelog_write_allowed(["--apply"])
            if got != (True, False):
                failures.append(f"用例2a --apply 期望 (True, False)，实得 {got}")
        except SystemExit:
            failures.append("用例2a --apply 不该被拦")

        try:
            got = changelog_write_allowed(["--apply", "--push"])
            if got != (True, True):
                failures.append(f"用例2a --apply --push 期望 (True, True)，实得 {got}")
        except SystemExit:
            failures.append("用例2a --apply --push 不该被拦")

        try:
            changelog_write_allowed(["--push"])
            failures.append("用例2b --push 不带 --apply 必须被拦（会推覆写后的文件），实得放行")
        except SystemExit:
            pass

    # ---------- 3) local_dir 只此一份 ----------
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "demo").mkdir()
        (base / "demo" / ".git").mkdir()
        (base / "ekit-old").mkdir()
        (base / "ekit-old" / ".git").mkdir()
        if local_dir("demo", str(base)) != base / "demo":
            failures.append("用例3a local_dir 应认 <base>/<name>")
        if local_dir("old", str(base)) != base / "ekit-old":
            failures.append("用例3a local_dir 应兼容旧目录名 ekit-<name>")
        if local_dir("missing", str(base)) is not None:
            failures.append("用例3a 找不到的仓必须返回 None，不能瞎返回一个路径")

    for name in ("handoff.py", "preflight.py"):
        src = (HERE / name).read_text(encoding="utf-8")
        if "def local_dir(" in src:
            failures.append(f"用例3b {name} 又自己定义了一份 local_dir ⇒ 事实层出现第二份实现")

    # ---------- 4) gen_changelog 的崩溃行不许回来 ----------
    # 判据在上面（import 期必须不炸）——**不扫源码**：docstring 里本来就该写清
    # 这个坑的历史，扫源码会被注释/文档误伤（2026-10-30 实测：先写成扫源码，
    # 结果被自己的文档字符串判红）。

    if failures:
        print("✗ 工具链闸门自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ 工具链闸门自测通过：合并回退只在 CLEAN 放行 / CHANGELOG 覆写要 --apply / local_dir 只此一份")


if __name__ == "__main__":
    main()
