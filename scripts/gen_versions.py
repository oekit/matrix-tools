#!/usr/bin/env python3
"""生成模块版本矩阵 versions.json（最新 tag / 矩阵内依赖地板 / 破坏性前缀索引）。

用法：
  python3 scripts/gen_versions.py            # 写 docs/public/versions.json（需 SITE）
  python3 scripts/gen_versions.py --stdout   # 打到标准输出
  python3 scripts/gen_versions.py --md       # 同时渲染 markdown 片段到 stdout

输出结构（每模块）：
  {
    "module": "logx",
    "latest": "v0.5.0",
    "nested": [{"path": "hlog", "latest": "v0.1.1"}, ...],
    "matrixDeps": ["core"],
    "breakingIndex": [{"version": "v0.4.0", "line": "…"}, ...]
  }
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, ORG, repo_dir, sibling_deps  # noqa: E402

BASE = os.path.abspath(
    os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", ".."))
)
SITE = os.environ.get("MATRIX_SITE", str(Path(BASE) / "oekit.github.io"))

BREAK_RE = re.compile(r"(?m)^- \*\*breaking\*\*", re.I)
BREAK_ANY = re.compile(r"(?i)breaking|不兼容|破坏性")


def _git_tags(d: Path) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(d), "tag", "-l", "v*", "--sort=-v:refname"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    return [t.strip() for t in out.splitlines() if t.strip()]


def _nested_tags(d: Path) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(d), "tag", "-l", "*/*", "--sort=-v:refname"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    return [t.strip() for t in out.splitlines() if t.strip()]


def latest_version(repo: str) -> str | None:
    tags = _git_tags(repo_dir(repo, BASE))
    return tags[0] if tags else None


def nested_versions(repo: str) -> list[dict]:
    d = repo_dir(repo, BASE)
    by_path: dict[str, str] = {}
    for tag in _nested_tags(d):
        # 格式: hlog/v0.1.1
        if "/" not in tag:
            continue
        path, ver = tag.split("/", 1)
        if path not in by_path:
            by_path[path] = ver
    return [{"path": p, "latest": v} for p, v in sorted(by_path.items())]


def breaking_index(repo: str) -> list[dict]:
    """扫 CHANGELOG.md，收集 breaking 事件 → {version, line}。"""
    d = repo_dir(repo, BASE)
    cl = d / "CHANGELOG.md"
    if not cl.exists():
        return []
    text = cl.read_text(encoding="utf-8")
    cur = None
    items: list[dict] = []
    for line in text.splitlines():
        m = re.match(r"## \[([^\]]+)\]", line)
        if m:
            cur = m.group(1)
            continue
        if cur and BREAK_ANY.search(line) and line.strip().startswith("-"):
            items.append({"version": cur, "line": line.strip()[:200]})
    return items


def build() -> dict:
    modules = []
    for r in MODULES:
        try:
            d = repo_dir(r, BASE)
        except FileNotFoundError:
            continue
        if not d.exists():
            continue
        try:
            deps = sibling_deps(r, BASE)
        except Exception:
            deps = []
        modules.append(
            {
                "module": r,
                "latest": latest_version(r),
                "nested": nested_versions(r),
                "matrixDeps": deps,
                "breakingIndex": breaking_index(r),
            }
        )
    return {
        "generatedAt": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "org": ORG,
        "modules": modules,
    }


def to_md(data: dict) -> str:
    lines = [
        "| 模块 | 最新版本 | 嵌套模块 | 矩阵内依赖 |",
        "|---|---|---|---|",
    ]
    for m in data["modules"]:
        nested = "、".join(f"{n['path']}@{n['latest']}" for n in m["nested"]) or "—"
        deps = "、".join(m["matrixDeps"]) or "—"
        lines.append(f"| {m['module']} | `{m['latest'] or '—'}` | {nested} | {deps} |")
    return "\n".join(lines)


def main() -> int:
    data = build()
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if "--stdout" in sys.argv:
        sys.stdout.write(text)
    else:
        out = Path(SITE) / "docs" / "public" / "versions.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"✓ wrote {out}")
    if "--md" in sys.argv:
        sys.stdout.write(to_md(data) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
