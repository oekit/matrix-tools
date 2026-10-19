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
from matrix import MODULES, ORG, repo_dir, ensure_clean_all, sibling_deps  # noqa: E402

BASE = os.path.abspath(
    os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", ".."))
)
SITE = os.environ.get("MATRIX_SITE", str(Path(BASE) / "oekit.github.io"))

# ---------- 破坏性变更抽取（matrix-tools#31） ----------
#
# 旧实现只做「行内含 breaking 关键词」匹配，于是漏掉了 CHANGELOG 里绝大多数
# 破坏性条目——典型如 registry v0.4.0 的
#   ### Breaking
#   - **SPI 包提升至模块根**（与 bootstrap/gormx/... 收平先例一致）：
# 条目行内根本没有 "breaking" 字样（关键词在段标题里），整条被漏收。
# 实测 24 个模块中 5 个模块 15 条 Breaking 记录未进矩阵，registry/nns 的
# breakingIndex 甚至是空数组——下游照矩阵升版直接构建断裂。
#
# 现在的口径是五路**确定性信号**并用：
#   A) 段标题即语义信号：`### Breaking` / `### BREAKING` / `### 不兼容` /
#      `### 破坏性`，以及 conventional commits 的破坏性标记段（标题以 `!` 结尾，
#      如 httpx 的 `### refactor(urlguard)!`）——段内条目一律是破坏性变更；
#   B) 行内信号：条目正文含 breaking / 不兼容 / 破坏性（`- **BREAKING** ...` 风格），
#      用于没有 Breaking 段的模块（pkg / concurrency / core 等）；
#   C) 本仓自身的包路径提升：条目**加粗前缀**同时含「提升」与「模块根」
#      （`**核心包提升到模块根**`）——跨仓一致的确定性措辞，覆盖 bootstrap/hertz/
#      httpx/middleware 等「结构重置但未标 Breaking 段」的仓；
#   D) 条目加粗前缀含 `!`（conventional commits 破坏性标记，如 `**feat!**`）。
#   E) **顶格独立成行的破坏性标记**（`**BREAKING**: ...`，不带 bullet 前缀）——
#      标记行本身即一条条目，其后条目同属该次破坏性变更。gormx v0.6.0 与
#      hertz v0.7.0 的主键类型泛型化正是这种写法，见下方 STANDALONE_BREAK_RE。
# 五路按 (version, 条目文本) 去重。
#
# 已知边界（机器判不了，不自动收录）：`**refactor(...)**` / `**feat(...)**` /
# `### Changed` 段里的破坏性变更——语义要看人。audit 会把这类「疑似未标注」
# 打成 ⚠ 提示（不阻塞），不静默吞掉。
BREAK_WORD_RE = re.compile(r"(?i)breaking|不兼容|破坏性")
VERSION_HEAD_RE = re.compile(r"^##\s*\[([^\]]+)\]")
VERSION_HEAD_PLAIN_RE = re.compile(r"^##\s+(v\d+\.\d+\.\d+)\b")
SECTION_HEAD_RE = re.compile(r"^###\s*(.*)$")
ITEM_RE = re.compile(r"^(\s*)-\s+(.*)$")
SEMVER_PATH_RE = re.compile(r"[A-Za-z0-9_.-]*/?v\d+\.\d+\.\d+")
# 段标题以 `!` 结尾 = conventional commits 的破坏性标记（feat!/refactor!/fix!）
BREAK_SECTION_BANG_RE = re.compile(r"!\s*$")
# 条目加粗前缀：`**核心包提升到模块根**` / `**主包提升至模块根**` / `**SPI 包提升至模块根**`
# ——本仓自身的 import 路径变更（下游构建必断），是跨仓一致的确定性措辞。
# 判据要求「加粗前缀」而非正文任意位置：`- 依赖升级：registry v0.4.0（SPI 包提升至模块根）`
# 讲的是**别仓**的破坏性变更，本仓并无破坏，收进来是噪音。
BOLD_PREFIX_RE = re.compile(r"^\*\*([^*]+)\*\*")
# 疑似破坏性但机器判不了的信号（仅用于 audit 提示，不进 breakingIndex）
# 排除 feat——新增功能本身不破坏兼容（`**feat!**` 已被破坏性标记单独收走）。
SUSPECT_ITEM_RE = re.compile(r"^\*\*[^*]*(refactor|fix|change|remove|rename|drop|migrat)[^*]*\*\*", re.I)
SUSPECT_SECTION_RE = re.compile(r"(?i)^(changed|变更|行为变更)\b")
SUSPECT_TEXT_RE = re.compile(r"(结构重置|目录正名|路径变更|历史与旧版本号)")

# ---------- 信号 E：独立成行的破坏性标记（matrix-tools#31 复审补漏） ----------
#
# 上面四路信号都建立在「条目是 `- ` 开头的 bullet」这个前提上，但 CHANGELOG 里
# 还有另一种同样常见的写法——**破坏性标记独占一行，不带 bullet 前缀**：
#
#     ## [v0.6.0] — 2026-10-19
#
#     **BREAKING**: crud/gormrepo 主键类型泛型化（#7 问题 1）——接口全链路加 `ID comparable`
#     泛型参数，文本主键（TEXT PRIMARY KEY）/UUID 等项目可直接实现 crud 契约，不再被写死 int64。
#
#     - crud：`Reader[T]`/`Writer[T]`/... → `[T, ID comparable]`；...
#     - gormrepo：`GormRepository[T]` → `[T, ID comparable]`；...
#
# gormx v0.6.0 与 hertz v0.7.0 的主键类型泛型化正是这种写法。旧实现按
# `line.strip().startswith("-")` 过滤，**整段（含标记行与其后全部条目）被漏收**——
# 而这两条恰恰是下游必断的泛型签名变更（`Reader[T]` → `Reader[T, ID comparable]`）。
# 漏收后 gormx 的 breakingIndex 是空数组，矩阵对下游说谎。
#
# 口径：标记行本身即一条破坏性条目；其后紧跟的条目同属该次破坏性变更，
# 沿用到下一个版本头 / `###` 段头为止（与显式 `### Breaking` 段同一生命周期）。
# 标记行必须**顶格**（不以空白开头）才作数——缩进的 `**breaking**：...` 多为
# 上一条 bullet 的续行（logx 即如此），由信号 B 随父条目一并收录。
STANDALONE_BREAK_RE = re.compile(r"^\*\*[^*]*(?:breaking|不兼容|破坏性)[^*]*\*\*", re.I)
# 隐式破坏性段里要排除的「别仓说明」类条目：讲的是依赖地板 / 消费方波次，
# 不是本仓自身的破坏性变更（与信号 C 同一判据）。
#   hertz v0.7.0: `- gormx 地板 v0.5.5 → **v0.6.0**`
#   gormx v0.6.0: `- 消费方波次：data v0.14.0（...）、hertz v0.7.0（...）`
DEP_NOTE_RE = re.compile(
    r"^(?:[A-Za-z0-9_./-]+\s+)?(?:地板\b|依赖|消费方波次|消费方|升级：)")


def _is_dep_note(body: str) -> bool:
    """条目是否只是「别仓的变更说明」（依赖地板 / 消费方波次）。"""
    head = body.strip()
    bold = BOLD_PREFIX_RE.match(head)
    prefix = bold.group(1) if bold else ""
    # 判据落在**加粗前缀或行首**上：`- 依赖升级：registry v0.4.0（...）` 讲别仓，
    # 但 `- 新增 WithIDParser` 讲本仓自身——不能只看正文里有没有「依赖」二字。
    if prefix and DEP_NOTE_RE.match(prefix):
        return True
    return bool(DEP_NOTE_RE.match(head)) and not bold


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


def _normalize_version(raw: str) -> str | None:
    """CHANGELOG 版本头 → 规范化版本串。

    `## [v0.4.2] — 2026-10-19` → "v0.4.2"
    `## [hlog/v0.1.3 + kitexlog/v0.1.3] — ...` → "hlog/v0.1.3 + kitexlog/v0.1.3"
    `## [Unreleased]` → None（未发布，不进索引）
    """
    raw = raw.strip()
    if not raw or raw.lower() in ("unreleased", "未发布", "unreleased/未发布"):
        return None
    parts = SEMVER_PATH_RE.findall(raw)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else " + ".join(parts)


def _is_break_section(title: str) -> bool:
    """段标题是否构成「该段条目一律是破坏性变更」的确定性信号。"""
    title = title.strip()
    if BREAK_WORD_RE.search(title):
        return True
    return bool(BREAK_SECTION_BANG_RE.search(title))


def parse_changelog_breaking(text: str) -> tuple[list[dict], list[dict]]:
    """解析 CHANGELOG 文本 → (破坏性条目, 疑似破坏性条目)。

    破坏性条目形如 {"version": "v0.4.0", "line": "…"}。
    疑似条目（`**refactor(...)**` / `### Changed` 段——机器判不了语义）单独返回，
    供 audit 打提示，不进 breakingIndex。
    """
    items: list[dict] = []
    suspects: list[dict] = []
    seen: set[tuple[str, str]] = set()
    seen_suspect: set[tuple[str, str]] = set()
    cur_version: str | None = None
    in_break = False
    implicit_break = False
    in_suspect_section = False
    buf: list[str] = []
    keep = False
    suspect = False

    def flush():
        nonlocal buf, keep, suspect
        if buf:
            line = re.sub(r"\s+", " ", " ".join(s.strip() for s in buf)).strip()
            if cur_version and keep:
                key = (cur_version, line)
                if key not in seen:
                    seen.add(key)
                    items.append({"version": cur_version, "line": line[:200]})
            elif cur_version and suspect:
                key = (cur_version, line)
                if key not in seen_suspect:
                    seen_suspect.add(key)
                    suspects.append({"version": cur_version, "line": line[:200]})
        buf = []
        keep = False
        suspect = False

    for raw_line in text.splitlines():
        m = VERSION_HEAD_RE.match(raw_line)
        if m:
            flush()
            cur_version = _normalize_version(m.group(1))
            in_break = implicit_break = in_suspect_section = False
            continue
        m = VERSION_HEAD_PLAIN_RE.match(raw_line)
        if m:
            flush()
            cur_version = m.group(1)
            in_break = implicit_break = in_suspect_section = False
            continue
        m = SECTION_HEAD_RE.match(raw_line)
        if m:
            flush()
            title = m.group(1)
            in_break = _is_break_section(title)
            # 显式 `### Breaking` 段内的一切条目都算数，包括「别仓说明」——
            # 是作者**刻意**放进 Breaking 段的。隐式段（信号 E）才排除。
            implicit_break = False
            in_suspect_section = bool(SUSPECT_SECTION_RE.match(title.strip()))
            continue
        # 信号 E：顶格独立成行的破坏性标记（`**BREAKING**: ...`）——标记行本身
        # 即一条条目，并开启隐式破坏性段（沿用到下一个版本头 / `###` 段头）。
        m = STANDALONE_BREAK_RE.match(raw_line)
        if m:
            flush()
            in_break = implicit_break = True
            in_suspect_section = False
            buf = [raw_line.strip()]
            keep = True
            continue
        m = ITEM_RE.match(raw_line)
        if m:
            flush()
            body = m.group(2)
            buf = [body]
            # 信号 A：处于 Breaking 段内；信号 B：条目正文自带破坏性关键词；
            # 信号 C：本仓自身的包路径提升（加粗前缀含「提升」+「模块根」）
            bold = BOLD_PREFIX_RE.match(body.strip())
            prefix = bold.group(1) if bold else ""
            keep = (
                in_break
                or bool(BREAK_WORD_RE.search(body))
                or ("提升" in prefix and "模块根" in prefix)
                # 信号 D：conventional commits 的破坏性标记（`**feat!**` / `**fix!**`）
                or ("!" in prefix)
            )
            # 隐式段（信号 E）里的「别仓说明」不算本仓的破坏性变更
            if keep and implicit_break and _is_dep_note(body):
                keep = False
            suspect = (not keep) and (
                in_suspect_section or bool(SUSPECT_ITEM_RE.match(body.strip()))
            )
            continue
        # 续行（缩进的非条目行）：并入当前条目
        if buf and raw_line.strip() and raw_line[:1] in (" ", "\t"):
            buf.append(raw_line)
            continue
        # 版本段首行的自由描述（如「仓库结构重置后的首个版本…」）——机器判不了
        # 是不是破坏性，记为疑似供 audit 提示
        if not buf and cur_version and raw_line.strip() and not raw_line.startswith("#") \
                and SUSPECT_TEXT_RE.search(raw_line):
            suspects.append({"version": cur_version, "line": re.sub(r"\s+", " ", raw_line.strip())[:200]})
        flush()

    flush()
    return items, suspects


def _prefix_version(v: str, prefix: str) -> str:
    """给版本号加嵌套模块前缀（`hlog/v0.1.3`）。已是 path/version 形式的不重复加。"""
    if not prefix:
        return v
    out = []
    for part in v.split(" + "):
        p = part.strip()
        out.append(p if "/" in p else f"{prefix}{p}")
    return " + ".join(out)


def _scan_changelog(path: Path, prefix: str = "") -> list[dict]:
    """扫一个 CHANGELOG.md 的破坏性条目（版本号按 prefix 加嵌套模块前缀）。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    items, _ = parse_changelog_breaking(text)
    for it in items:
        it["version"] = _prefix_version(it["version"], prefix)
    return items


def breaking_index(repo: str) -> list[dict]:
    """扫 CHANGELOG.md，收集 breaking 事件 → {version, line}。

    范围 = 根模块 CHANGELOG + 各嵌套模块的 CHANGELOG（子目录含 go.mod 者）。
    嵌套模块的版本号带路径前缀（如 `redis/v0.2.0`）——嵌套模块的破坏性变更
    同样是下游必断的（如 timertask v0.2.0 把 Redis 后端迁至嵌套模块），
    只扫根 CHANGELOG 会漏掉它们。
    """
    root = repo_dir(repo, BASE)
    items = _scan_changelog(root / "CHANGELOG.md")
    for gomod in sorted(root.glob("*/go.mod")):
        sub = gomod.parent
        items.extend(_scan_changelog(sub / "CHANGELOG.md", prefix=f"{sub.name}/"))
    return items


def breaking_suspects(repo: str) -> list[dict]:
    """扫 CHANGELOG.md，收集「疑似破坏性但机器判不了」的条目（供 audit 提示）。"""
    root = repo_dir(repo, BASE)
    out = []
    for path, prefix in [(root / "CHANGELOG.md", "")] + [
        (g.parent / "CHANGELOG.md", f"{g.parent.name}/") for g in sorted(root.glob("*/go.mod"))
    ]:
        if not path.exists():
            continue
        _, suspects = parse_changelog_breaking(path.read_text(encoding="utf-8"))
        for s in suspects:
            s["version"] = _prefix_version(s["version"], prefix)
        out.extend(suspects)
    return out


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
        ensure_clean_all([SITE], "gen_versions")  # 产物必须落在已提交基线上
        out = Path(SITE) / "docs" / "public" / "versions.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"✓ wrote {out}")
    if "--md" in sys.argv:
        sys.stdout.write(to_md(data) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
