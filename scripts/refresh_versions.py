#!/usr/bin/env python3
"""各仓 README 安装行 + AGENTS.md「当前版本」行刷新到最新 tag。用法：python3 scripts/refresh_versions.py [--push]"""
import os, pathlib, re, sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, repo_dir, latest_tag, sh
BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
PUSH = "--push" in sys.argv
for repo in MODULES:
    d = repo_dir(repo, BASE)
    lt = latest_tag(repo, BASE)
    if lt is None:  # 无 tag 的新仓跳过，避免写入 @None
        continue
    changed = []
    readme = d / "README.md"
    t = readme.read_text()
    t2 = re.sub(r"(go get github\.com/oekit/%s@)v[0-9.]+" % re.escape(repo), rf"\g<1>{lt}", t)
    if t2 != t:
        readme.write_text(t2)
        changed.append("README.md")
    agents = d / "AGENTS.md"
    if agents.exists():
        ta = agents.read_text()
        ta2 = re.sub(r"(当前版本：`)v[0-9.]+(`)", rf"\g<1>{lt}\g<2>", ta, count=1)
        if ta2 != ta:
            agents.write_text(ta2)
            changed.append("AGENTS.md")
    if changed:
        print(f"✓ {repo} → {lt}（{'、'.join(changed)}）")
        if PUSH:
            sh(["git", "add"] + changed, d)
            sh(["git", "commit", "-q", "-m", f"docs: 版本引用对齐 {lt}"], d)
            sh(["git", "push", "-q"], d)
    else:
        print(f"☑ {repo} 已是 {lt}")
