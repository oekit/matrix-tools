#!/usr/bin/env python3
"""ekit2oekit —— ekit 单体 import 路径 → oekit 矩阵的 AST 级改写。

只改 import 面（含别名保持）；调用点级规则输出「需人工清单」。
规则可经 --rules=extra.json 扩展：[{"old": "...", "new": "...", "kind": "move"}]

用法：
  python3 scripts/ekit2oekit.py path/to/src
  python3 scripts/ekit2oekit.py --dry-run ./...
  python3 scripts/ekit2oekit.py --print-rules
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# 种子规则（扁鹊 23 条实测 + 收平）。长路径优先，避免前缀互撞。
# kind: move=路径迁移 | rename=包名变化 | flatten=目录收平
SEED_RULES: list[dict] = [
    # —— 第二轮目录收平（更长/更具体，必须先匹配）——
    {"old": "github.com/oekit/middleware/middleware", "new": "github.com/oekit/middleware", "kind": "flatten", "since": "2026-10-17"},
    {"old": "github.com/oekit/bootstrap/bootstrap", "new": "github.com/oekit/bootstrap", "kind": "flatten", "since": "2026-10-17"},
    {"old": "github.com/oekit/hertz/hertz/sse", "new": "github.com/oekit/hertz/sse", "kind": "flatten", "since": "2026-10-17"},
    {"old": "github.com/oekit/hertz/hertz/metrics", "new": "github.com/oekit/hertz/metrics", "kind": "flatten", "since": "2026-10-17"},
    {"old": "github.com/oekit/hertz/hertz/health", "new": "github.com/oekit/hertz/health", "kind": "flatten", "since": "2026-10-17"},
    {"old": "github.com/oekit/network/network/streamx", "new": "github.com/oekit/network/streamx", "kind": "flatten", "since": "2026-10-17"},
    {"old": "git.enjoye.top/enjoydream/ekit/middleware/traceid/hmw", "new": "github.com/oekit/middleware/hmw", "kind": "move", "since": "2026-10-17"},
    # —— 第一轮单体→矩阵——
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/errors", "new": "github.com/oekit/pkg/errors", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/stringx", "new": "github.com/oekit/pkg/stringx", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/slicex", "new": "github.com/oekit/pkg/slicex", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/uidgen", "new": "github.com/oekit/kitx/uidgen", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/jsonrepair", "new": "github.com/oekit/pkg/jsonrepair", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/ptrx", "new": "github.com/oekit/pkg/ptrx", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/cache", "new": "github.com/oekit/pkg/cache", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/timex/cron", "new": "github.com/oekit/pkg/timex/cron", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/retry", "new": "github.com/oekit/concurrency/retry", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/syncx", "new": "github.com/oekit/concurrency/syncx", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/pkg/ratelimit", "new": "github.com/oekit/concurrency/ratelimit", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/data/timertask", "new": "github.com/oekit/timertask", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/data/gormx", "new": "github.com/oekit/gormx", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/observability/logx", "new": "github.com/oekit/logx", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/observability/metrics", "new": "github.com/oekit/metrics", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/nacos", "new": "github.com/oekit/registry/nacos", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/middleware/contrib", "new": "github.com/oekit/middleware/contrib", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/bootstrap", "new": "github.com/oekit/bootstrap", "kind": "move", "since": "2026-10-16"},
    {"old": "git.enjoye.top/enjoydream/ekit/middleware", "new": "github.com/oekit/middleware", "kind": "move", "since": "2026-10-16"},
    # 常见包名（import alias 默认同 last segment）
    {"old": "git.enjoye.top/enjoydream/ekit", "new": "github.com/oekit/pkg", "kind": "move", "since": "2026-10-16", "note": "兜底，需人工核对"},
]

# 调用点级：机械改写覆盖不了，输出需人工清单
MANUAL = [
    "bootstrap.Run(Config{...}) → bootstrap.New(WithServiceName, WithShutdownTimeout) + b.Run(fn)",
    "middleware 零指标强制 StackConfig{ServiceName} → 显式 NewPrometheusMetrics",
]


def sorted_rules(rules: list[dict]) -> list[dict]:
    # 长路径优先，避免 ekit/middleware 吃掉 ekit/middleware/contrib
    return sorted(rules, key=lambda r: len(r["old"]), reverse=True)


def rewrite_import(path: str, rules: list[dict]) -> str | None:
    for r in rules:
        old, new = r["old"], r["new"]
        if path == old:
            return new
        if path.startswith(old + "/"):
            return new + path[len(old) :]
    return None


def rewrite_source(src: str, rules: list[dict]) -> tuple[str, int]:
    """改写 import 块/单行 import 中的路径（quote 锚定，不碰注释）。"""
    lines = src.splitlines(keepends=True)
    out_lines = []
    in_import = False
    n = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("import ("):
            in_import = True
            out_lines.append(line)
            continue
        if in_import and stripped == ")":
            in_import = False
            out_lines.append(line)
            continue
        m = None
        if in_import:
            m = re.match(r'^(\s*(?:[\w.]+\s+)?)(["`])([^"`]+)(["`])(.*)$', line)
        elif stripped.startswith("import "):
            m = re.match(r'^(\s*import\s+(?:[\w.]+\s+)?)(["`])([^"`]+)(["`])(.*)$', line)
        if m:
            prefix, q, path, q2, rest = m.groups()
            new = rewrite_import(path, rules)
            if new is not None:
                n += 1
                nl = "\n" if line.endswith("\n") else ""
                out_lines.append(f"{prefix}{q}{new}{q}{rest}{nl}")
                continue
        out_lines.append(line)
    return "".join(out_lines), n


def iter_go_files(root: Path):
    for p in root.rglob("*.go"):
        if "/.git/" in str(p):
            continue
        yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="源目录/文件")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rules", help="扩展规则 JSON 文件")
    ap.add_argument("--print-rules", action="store_true")
    args = ap.parse_args()

    rules = list(SEED_RULES)
    if args.rules:
        rules += json.loads(Path(args.rules).read_text(encoding="utf-8"))
    rules = sorted_rules(rules)

    if args.print_rules:
        print(json.dumps(rules, ensure_ascii=False, indent=2))
        print("\n# 需人工的调用点规则：")
        for x in MANUAL:
            print(" -", x)
        return 0

    if not args.paths:
        ap.error("需要至少一个路径，或用 --print-rules")

    total = 0
    for raw in args.paths:
        root = Path(raw)
        files = list(iter_go_files(root)) if root.is_dir() else [root]
        for f in files:
            src = f.read_text(encoding="utf-8")
            new, n = rewrite_source(src, rules)
            if n == 0:
                continue
            total += n
            print(f"{'~' if args.dry_run else '+'} {f}: {n} import(s)")
            if not args.dry_run:
                f.write_text(new, encoding="utf-8")
    print(f"done: {total} import rewrite(s)")
    print("\n# 仍需人工核对：")
    for x in MANUAL:
        print(" -", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
