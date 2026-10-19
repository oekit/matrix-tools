#!/usr/bin/env python3
"""从 tag 自动生成各仓 CHANGELOG.md。用法：python3 scripts/gen_changelog.py [--push]"""
import os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).rsplit("/", 2)[0]))
from matrix import MODULES, repo_dir, ensure_clean_all, sh
BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
PUSH = "--push" in sys.argv
ensure_clean_all([repo_dir(r, BASE) for r in MODULES], "gen_changelog")  # 产物必须落在已提交基线上
for repo in MODULES:
    d = repo_dir(repo, BASE)
    tags = sh(["git", "-C", str(d), "tag", "-l", "--sort=v:refname"], d).stdout.split()
    lines = [f"# Changelog — oekit/{repo}\n",
             "由 ekit v0.34.0（git.enjoye.top/enjoydream/ekit，冻结维护）拆分而来。所有 notable 变更记录如下。\n"]
    for tag in reversed(tags):
        msg = sh(["git", "-C", str(d), "tag", "-l", tag, "--format=%(contents)"], d).stdout.strip().splitlines()
        subject = next((l for l in msg if l and not l.startswith("v0.")), tag)
        date = sh(["git", "-C", str(d), "log", "-1", "--format=%ad", "--date=short", tag], d).stdout.strip()
        lines.append(f"\n## [{tag}] — {date}\n\n- {subject}")
    (d / "CHANGELOG.md").write_text("\n".join(lines) + "\n")
    print(f"✓ {repo}: {len(tags)} 版本")
if PUSH:
    for repo in MODULES:
        d = repo_dir(repo, BASE)
        if sh(["git", "status", "--porcelain", "CHANGELOG.md"], d).stdout.strip():
            sh(["git", "add", "CHANGELOG.md"], d)
            sh(["git", "commit", "-q", "-m", "docs: CHANGELOG 更新（matrix-tools）"], d)
            sh(["git", "push", "-q"], d)
            print(f"⬆ {repo}")
