#!/usr/bin/env python3
"""发布序列倒挂门禁的自测：证明这道判据「抓得住自己要防的东西」，且不假绿。

**为什么要有这个自测**（2026-10-23 实测）：本仓 selftest job 的既有原则
（`close_issue_selftest` / `dependabot_dirs_selftest` / `ghsafe_selftest` /
`remote_reads_selftest` / `verify_release_units_selftest`）在这里同样适用——
一道没人验证过的门禁，很可能自己就是个假绿。

**反证怎么构造**（全部用临时 git 仓，不联网、不碰真仓、不依赖工作区布局）：
  1. 造一个「版本号回退」的仓（v0.1.0 → v0.2.0 → v0.1.5）⇒ 必须判红，
     且指出参照版本是 v0.2.0；
  2. 造一个「正常递增」的仓 ⇒ 必须判绿（不误报）；
  3. 造一个「嵌套模块独立版本线」的仓（根 v0.3.0 + sub/v0.1.0）⇒ 必须判绿
     （嵌套模块有自己的版本节奏，不能与根模块混在一条线上比）；
  4. 把倒挂登记进例外表 ⇒ 归入「已知」而非「未登记」，且**新出现的另一处倒挂
     仍然判红**（例外表不是漏报的口子）；
  5. 造一个「一个 tag 都没有」的仓 ⇒ 判**判不了**（读不到版本事实），
     不得当成通过。

用法：python3 scripts/verify_version_line_selftest.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from verify_version_line import find_regressions, git_tag_events, load_known, scan  # noqa: E402


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = None
    if date:
        import os
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()}")


def make_repo(path: Path, tags: list[tuple[str, str]]) -> Path:
    """造一个临时 git 仓，tags = [(tag 名, ISO 时刻)]，按给定时刻打 annotated tag。

    **必须自带 git 身份**：CI runner 没有全局 user.name/user.email
    （2026-10-23 实测：`git commit` 报 "Author identity unknown" ⇒ 自测红），
    而本地机器通常有 ⇒ **本地绿、CI 红**。身份只写进这个临时仓，不动全局配置。
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.name", "version-line selftest")
    _git(path, "config", "user.email", "selftest@example.invalid")
    (path / "README.md").write_text("probe\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init", date="2026-01-01T00:00:00Z")
    for name, date in tags:
        _git(path, "tag", "-a", name, "-m", f"tag {name}", date=date)
    return path


def selftest() -> int:
    ok_all: list[bool] = []

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # ── 自测1：版本号回退必须抓到，且参照是「此前已发布的最高版本」 ──
        d = make_repo(root / "r1", [
            ("v0.1.0", "2026-01-01T00:00:00Z"),
            ("v0.2.0", "2026-01-02T00:00:00Z"),
            ("v0.3.0", "2026-01-03T00:00:00Z"),
            ("v0.1.5", "2026-01-04T00:00:00Z"),
        ])
        regs = find_regressions(git_tag_events(d))
        ok = len(regs) == 1 and regs[0]["tag"] == "v0.1.5" and regs[0]["higher_tag"] == "v0.3.0"
        print(("✅" if ok else "❌"), f"自测1：版本号回退被抓到（得到 {[r['tag'] for r in regs]}，"
                                   f"参照 {regs[0]['higher_tag'] if regs else '—'}）")
        ok_all.append(ok)

        # ── 自测2：正常递增不得误报 ──
        d = make_repo(root / "r2", [
            ("v0.1.0", "2026-01-01T00:00:00Z"),
            ("v0.2.0", "2026-01-02T00:00:00Z"),
            ("v0.3.0", "2026-01-03T00:00:00Z"),
        ])
        regs = find_regressions(git_tag_events(d))
        ok = regs == []
        print(("✅" if ok else "❌"), f"自测2：正常递增不误报（得到 {[r['tag'] for r in regs]}）")
        ok_all.append(ok)

        # ── 自测3：嵌套模块是独立版本线，不得与根模块混比 ──
        d = make_repo(root / "r3", [
            ("v0.1.0", "2026-01-01T00:00:00Z"),
            ("v0.3.0", "2026-01-02T00:00:00Z"),
            ("sub/v0.1.0", "2026-01-03T00:00:00Z"),
            ("sub/v0.2.0", "2026-01-04T00:00:00Z"),
        ])
        regs = find_regressions(git_tag_events(d))
        ok = regs == []
        print(("✅" if ok else "❌"), f"自测3：嵌套版本线独立（得到 {[r['tag'] for r in regs]}）")
        ok_all.append(ok)

        # ── 自测4：例外表只放行登记的那一处，新出现的仍须判红 ──
        known_file = root / "known.json"
        known_file.write_text(json.dumps({
            "known": [{"repo": "r4", "tag": "v0.1.5", "reason": "自测登记"}]
        }), encoding="utf-8")
        import verify_version_line as vvl
        orig_known = vvl.KNOWN_PATH
        vvl.KNOWN_PATH = known_file
        try:
            d = make_repo(root / "r4", [
                ("v0.2.0", "2026-01-01T00:00:00Z"),
                ("v0.1.5", "2026-01-02T00:00:00Z"),
                ("v0.1.6", "2026-01-03T00:00:00Z"),
            ])
            res = scan(root, ["r4"])
            known_tags = [r["tag"] for r in res["known"]]
            unknown_tags = [r["tag"] for r in res["unknown"]]
            ok = known_tags == ["v0.1.5"] and unknown_tags == ["v0.1.6"]
            print(("✅" if ok else "❌"),
                  f"自测4：例外表只放行登记的那一处（known={known_tags} unknown={unknown_tags}）")
            ok_all.append(ok)
        finally:
            vvl.KNOWN_PATH = orig_known

        # ── 自测5：一个 tag 都没有 ⇒ 判不了，不得当成通过 ──
        d = make_repo(root / "r5", [])
        res = scan(root, ["r5"])
        ok = len(res["unreadable"]) == 1 and not res["unknown"] and not res["known"]
        print(("✅" if ok else "❌"),
              f"自测5：无 tag ⇒ 判不了（unreadable={len(res['unreadable'])}）")
        ok_all.append(ok)

    return 0 if all(ok_all) else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
