#!/usr/bin/env python3
"""各仓 README 安装行 + AGENTS.md「当前版本」行刷新到最新 tag。
用法：python3 scripts/refresh_versions.py [--push]

sync_repo 供 doc_wave/release_post import 复用（写入逻辑不在模块顶层）。"""
import os, pathlib, re, sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, repo_dir, ensure_clean_all, latest_tag, sh

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
PUSH = "--push" in sys.argv


def sync_repo(repo: str, base: str | None = None, write: bool = True) -> list[str]:
    """单仓版本行对齐最新 tag。write=False 只算不写（plan 用）。返回变更文件名清单。"""
    d = repo_dir(repo, base)
    lt = latest_tag(repo, base)
    if lt is None:  # 无 tag 的新仓跳过，避免写入 @None
        return []
    changed: list[str] = []
    readme = d / "README.md"
    t = readme.read_text()
    t2 = re.sub(r"(go get github\.com/oekit/%s@)v[0-9.]+" % re.escape(repo), rf"\g<1>{lt}", t)
    if t2 != t:
        changed.append("README.md")
        if write:
            readme.write_text(t2)
    agents = d / "AGENTS.md"
    if agents.exists():
        ta = agents.read_text()
        ta2 = re.sub(r"(当前版本：`)v[0-9.]+(`)", rf"\g<1>{lt}\g<2>", ta, count=1)
        if ta2 != ta:
            changed.append("AGENTS.md")
            if write:
                agents.write_text(ta2)
    return changed


def main() -> None:
    ensure_clean_all([repo_dir(r, BASE) for r in MODULES], "refresh_versions")
    for repo in MODULES:
        changed = sync_repo(repo, BASE)
        if changed:
            lt = latest_tag(repo, BASE)
            print(f"✓ {repo} → {lt}（{'、'.join(changed)}）")
            if PUSH:
                d = repo_dir(repo, BASE)
                sh(["git", "add"] + changed, d)
                sh(["git", "commit", "-q", "-m", f"docs: 版本引用对齐 {lt}"], d)
                sh(["git", "push", "-q"], d)
        else:
            print(f"☑ {repo} 已对齐")


if __name__ == "__main__":
    main()
