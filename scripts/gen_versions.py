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


def _remote_tags(repo: str) -> list[str]:
    """读远端仓库的全部 tag（#5 根因修复：本地 clone 的 tag 可能没 fetch，
    生成「generatedAt 很新但内容陈旧」的 versions.json——比明显陈旧更有害）。
    URL 用本地 clone 的 origin（失败回退 github.com/oekit/<repo>）。
    返回按版本降序的 tag 名（含嵌套前缀）。"""
    origin = None
    d = repo_dir(repo, BASE)
    try:
        origin = subprocess.check_output(
            ["git", "-C", str(d), "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        origin = None
    url = origin or f"https://github.com/oekit/{repo}.git"
    try:
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", url],
            text=True, stderr=subprocess.DEVNULL, timeout=60,
        )
    except Exception:
        # 远端不可达时回退本地 tag（降级而非失败）
        return _local_tags(d)
    tags = []
    for line in out.splitlines():
        # "<sha>\trefs/tags/v0.1.0" 或 "refs/tags/nacos/v0.3.0^{}"
        ref = line.split("\t")[-1].strip()
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags.append(ref[len("refs/tags/"):])
    return _sort_tags_desc(tags)


def _local_tags(d: Path) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(d), "tag", "-l", "--sort=-v:refname"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    return [t.strip() for t in out.splitlines() if t.strip()]


def _sort_tags_desc(tags: list[str]) -> list[str]:
    def key(t: str):
        # 取最后一段做 semver 排序（嵌套前缀 hlog/v0.1.2 → v0.1.2）
        ver = t.rsplit("/", 1)[-1]
        m = re.match(r"v(\d+)\.(\d+)\.(\d+)", ver)
        if not m:
            return (0, 0, 0, t)
        return (-int(m.group(1)), -int(m.group(2)), -int(m.group(3)), t)
    return sorted(tags, key=key)


def latest_version(repo: str) -> str | None:
    for t in _remote_tags(repo):
        if "/" not in t:  # 根模块 tag
            return t
    return None


def nested_versions(repo: str) -> list[dict]:
    by_path: dict[str, str] = {}
    for tag in _remote_tags(repo):
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
        "generatedAt": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
