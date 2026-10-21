#!/usr/bin/env python3
"""close_issue.py 的自测：证明「回执真的落地」这条判据不会假绿。

**为什么要有这个自测**：2026-10-21 实测——PR 正文带 `Closes #N` 会在合入时
自动关闭 issue；随后 `gh issue close --comment` 对**已关闭**的 issue 打印
「already closed」并**以 0 退出**，评论根本没发，而旧脚本只看退出码，
于是每轮都打印「✓ 已回执并关闭」——**回执一条都没落**。

自测用一个假 `gh` 复刻真实 gh 的这个行为：
  - `gh issue view --json state`      → closed
  - `gh issue view --json comments`   → 假 gh 记录里的评论
  - `gh issue close --comment <body>` → **什么都不做**（复刻真实 gh 的静默失败）
  - `gh issue comment <body>`         → 真的记一条评论

判据：跑 close_issue.py 后，**记录里必须真的有评论**且退出码为 0。
反证：把「已关闭 → 改走 gh issue comment」这段回退成旧写法（只调 close），
本自测必然失败（记录为空 ⇒ 脚本判「回执未落地」⇒ 非 0 退出）。

用法：python3 scripts/close_issue_selftest.py
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE

# 假 gh 的行为开关（用于反证：把评论记录功能掐掉 ⇒ 脚本必须失败而不是假绿）
FAKE_GH = r'''#!/bin/sh
# 假 gh：复刻真实 gh 在「issue 已关闭」时的静默失败。
REC="$FAKE_GH_RECORD"
if [ "$1" = "issue" ] && [ "$2" = "view" ]; then
  if echo "$*" | grep -q "state"; then
    echo "$FAKE_GH_STATE"
    exit 0
  fi
  if echo "$*" | grep -q "comments"; then
    [ -f "$REC" ] && cat "$REC"
    exit 0
  fi
fi
if [ "$1" = "issue" ] && [ "$2" = "close" ]; then
  # 真实 gh 对已关闭 issue：打印提示、退出 0、**不贴评论**
  echo "! Issue is already closed" >&2
  exit 0
fi
if [ "$1" = "issue" ] && [ "$2" = "comment" ]; then
  if [ "$FAKE_GH_COMMENT_WORKS" = "1" ]; then
    printf '%s\n' "$*" >> "$REC"
    echo "https://example.invalid/comment"
    exit 0
  fi
  exit 0   # 评论「发出去了」但没落地（模拟静默失败）
fi
exit 0
'''


def run_case(tmp: Path, state: str, comment_works: str) -> tuple[int, str, str]:
    """跑一遍 close_issue.py，返回 (退出码, 记录内容, 组合输出)。"""
    bindir = tmp / f"bin-{state}-{comment_works}"
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rec = tmp / f"rec-{state}-{comment_works}.txt"

    # 自带一个最小 MATRIX_BASE：repo_dir 只要求仓目录下有 go.mod 或 README.md。
    # ⇒ 自测不依赖本地工作区布局，CI 里也能跑（不 clone 全矩阵、不联网）。
    base = tmp / f"base-{state}-{comment_works}"
    (base / "pkg").mkdir(parents=True, exist_ok=True)
    (base / "pkg" / "README.md").write_text("# fake pkg\n", encoding="utf-8")
    (base / "pkg" / "CHANGELOG.md").write_text(
        "## [v0.19.0] — 2026-10-21\n\n- fake entry\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GH_RECORD"] = str(rec)
    env["FAKE_GH_STATE"] = state
    env["FAKE_GH_COMMENT_WORKS"] = comment_works
    env["MATRIX_BASE"] = str(base)

    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "close_issue.py"), "pkg#45", "--tag", "v0.19.0", "--force"],
        capture_output=True, text=True, env=env)
    recorded = rec.read_text(encoding="utf-8") if rec.exists() else ""
    return p.returncode, recorded, (p.stdout or "") + (p.stderr or "")


def main() -> None:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 用例 1：issue 已被 PR 的 Closes 自动关闭 ⇒ 必须补发回执（评论真的落地）
        rc, recorded, out = run_case(tmp, "closed", "1")
        if rc != 0:
            failures.append(f"用例1 期望退出码 0，实得 {rc}：{out.strip()[:200]}")
        if "v0.19.0" not in recorded:
            failures.append("用例1 回执未落地：假 gh 没收到 gh issue comment 调用")

        # 用例 2：issue 未关闭 ⇒ 走 close+comment；此时假 gh 的 close 不贴评论，
        # 但脚本必须**判定回执未落地并失败**（而不是假绿）
        rc2, recorded2, out2 = run_case(tmp, "open", "1")
        if rc2 == 0:
            failures.append("用例2 期望非 0 退出（回执未落地必须报错），实得 0")
        if "回执未落地" not in out2:
            failures.append(f"用例2 期望报「回执未落地」，实得：{out2.strip()[:200]}")

        # 用例 3：评论命令「成功」但没落地 ⇒ 同样必须失败（判据不信退出码）
        rc3, recorded3, out3 = run_case(tmp, "closed", "0")
        if rc3 == 0:
            failures.append("用例3 期望非 0 退出（回执未落地必须报错），实得 0")
        if "回执未落地" not in out3:
            failures.append(f"用例3 期望报「回执未落地」，实得：{out3.strip()[:200]}")

    if failures:
        print("✗ close_issue 自测未过：")
        for f in failures:
            print("  -", f)
        raise SystemExit(1)
    print("✓ close_issue 自测通过：已关闭 issue 也会补发回执；回执未落地时判红不假绿")


if __name__ == "__main__":
    main()
