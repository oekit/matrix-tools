#!/usr/bin/env python3
"""各仓 README 安装行版本刷新到最新 tag。用法：python3 scripts/refresh_versions.py [--push]"""
import os, pathlib, re, sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, repo_dir, latest_tag, sh
BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
PUSH = "--push" in sys.argv
for repo in MODULES:
    d = repo_dir(repo, BASE)
    readme = d / "README.md"
    t = readme.read_text()
    lt = latest_tag(repo, BASE)
    t2 = re.sub(r"(go get github\.com/oekit/%s@)v[0-9.]+" % re.escape(repo), rf"\g<1>{lt}", t)
    if t2 != t:
        readme.write_text(t2)
        print(f"✓ {repo} → {lt}")
        if PUSH:
            sh(["git", "add", "README.md"], d)
            sh(["git", "commit", "-q", "-m", f"docs: 安装行对齐 {lt}"], d)
            sh(["git", "push", "-q"], d)
    else:
        print(f"☑ {repo} 已是 {lt}")
