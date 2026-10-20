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
#   F) 本仓自身的路径级移除：公开符号整体迁出、删除重复层/废弃转发层、拆为
#      独立子模块、目录正名——**符号或路径在本仓消失**，下游 import 直接断。
#      覆盖 `**refactor(...)**` 段里那些正文不含 breaking 关键词、段标题也
#      不是 Breaking 的条目（logx v0.2.0 嵌套多模块、logx v0.4.0 Sink 迁至
#      logx/sink、logx v0.4.0 Manager 迁至 core/lifecycle、
#      middleware v0.5.0 删除 contrib/ 与 traceid/hmw/）。
#      F 路的两个护栏（缺一不可，否则会收进大量噪音）：
#        ① 路径归属：条目里出现的 oekit 路径标识（`github.com/oekit/<repo>`
#           或 `<repo>/`）必须命中**本仓**。`redisx v0.4.0 import 路径正名
#           data/data/cache → data/cache` 讲的是 data 仓，破坏归属 data，
#           收进 redisx 是错的；
#        ② 方向：只认「移除/迁出/删除/拆分/正名」，不认「迁入/新增」——
#           `timertask redis/v0.2.0 自根包迁入嵌套模块` 是新增位置，不是破坏
#           （根包侧的移除已由 timertask v0.2.0 那条收录）。
#      F 路判定在**合并续行之后**做——路径标识常在续行里（logx v0.2.0 的
#      `github.com/oekit/logx/hlog` 就在第二行），只看首行会漏。
# 六路按 (version, 条目文本) 去重。
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
# ---------- E 路：路径级移除 ----------
# 动作词只认「移除/迁出/删除/拆分/正名」——即符号或路径在**本仓**消失。
# 「迁入」「新增」是新位置而非破坏，不触发 E 路。
E_ACTION_RE = re.compile(
    r"(迁至|迁出|"
    r"删除重复层|删除废弃转发层|删除旧适配别名|"
    r"嵌套多模块|各自持有独立 go\.mod|拆为独立|"
    r"目录正名|import 路径正名)"
)
# 路径归属：条目里出现的 oekit 路径标识。`github.com/oekit/<name>` 或 `<name>/`。
E_PATH_RE = re.compile(r"github\.com/oekit/([a-z0-9-]+)|(?<![\w/])([a-z0-9-]+)/")
# 跟随依赖升级：本仓 API 未变，破坏性归属被依赖仓。
E_DEP_UPGRADE_RE = re.compile(r"^\**(依赖升级|build|fix\(deps\)|实现细节|docs|注|说明|doc)")
# 疑似破坏性但机器判不了的信号（仅用于 audit 提示，不进 breakingIndex）
# 排除 feat——新增功能本身不破坏兼容（`**feat!**` 已被破坏性标记单独收走）。
SUSPECT_ITEM_RE = re.compile(r"^\*\*[^*]*(refactor|fix|change|remove|rename|drop|migrat)[^*]*\*\*", re.I)
SUSPECT_SECTION_RE = re.compile(r"(?i)^(changed|变更|行为变更)\b")
SUSPECT_TEXT_RE = re.compile(r"(结构重置|目录正名|路径变更|历史与旧版本号)")

# ---------- 信号 G：死路径（客观判据，不靠动作词表） ----------
#
# 信号 F 靠**动作词表**判「这是路径移除」，而 CHANGELOG 的措辞不受控——
# 实测漏收 4 条真实死路径（措辞分别是「子包收成一级」「适配器扁平化」
# 「提升到模块根」「healthz 路径同步」，都不在词表里）。**词表穷举法注定漏**：
# 明天又出现一个新措辞，就又漏一条。
#
# 信号 G 换成**客观判据**：这个 import 路径**现在还在不在**？
#   1. 扫 CHANGELOG 原文抽本仓的路径引用（`github.com/oekit/<repo>/...`）；
#   2. 拿**文件系统**判生死——该路径在当前源码树里还在不在？
#   3. 不在了 = 死路径 = 下游 import 直接断。
#
# 方向性天然自带，**不需要额外维护「不认迁入」的词表**：
#   - 「A → B」的条目里 A 是**旧路径**（已死）→ 触发；B 是**新路径**（活的）→ 不触发；
#   - 迁移表里左右两列分别会被判生死，天然只命中被移除的那一侧；
#   - 路径若留有兼容别名（还活着）→ 不触发——**那确实不是破坏**。
#
# 与信号 F 是互补而非替代：F 抓的是「措辞说破了但路径还活着」的语义变更
# （如签名/行为变更），G 抓的是「措辞没提但路径真没了」的移除。
DEAD_PATH_RE = re.compile(r"github\.com/oekit/([a-z0-9-]+(?:/[a-z0-9-]+)*)")
# GitHub 页面路径不是 import 路径——`github.com/oekit/oekit/issues/1` 是 issue
# 链接，排除掉，否则会把它当成「死包」。
URLISH = {
    "issues", "pull", "pulls", "requests", "blob", "tree", "releases", "actions",
    "wiki", "compare", "discussions", "labels", "milestones", "projects",
    "security", "graphs", "network", "settings", "discussions",
}


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


def _is_path_removal(text: str, repo: str) -> bool:
    """E 路：本仓自身的路径级移除（符号或路径在本仓消失 → 下游 import 断）。

    `text` 必须是**合并续行后的完整条目**——路径标识常在续行里
    （logx v0.2.0 的 `github.com/oekit/logx/hlog` 就在第二行），
    只看首行会漏。

    两个护栏缺一不可：
      ① 路径归属——条目里出现的 oekit 路径标识必须命中**本仓**。
         `redisx v0.4.0 import 路径正名 data/data/cache → data/cache` 讲的是
         data 仓，破坏归属 data，收进 redisx 是错的；
      ② 方向——只认「移除/迁出/删除/拆分/正名」，不认「迁入/新增」。
         `timertask redis/v0.2.0 自根包迁入嵌套模块` 是新增位置，不是破坏
         （根包侧的移除已由 timertask v0.2.0 那条收录）。

    `repo` 为空（无法判定归属）时一律不收——宁可漏，不误收。
    """
    if not repo:
        return False
    if not E_ACTION_RE.search(text):
        return False
    if E_DEP_UPGRADE_RE.match(text.strip()):
        return False
    names = set()
    for m in E_PATH_RE.finditer(text):
        names.add(m.group(1) or m.group(2) or "")
    names.discard("")
    # 路径标识里必须出现本仓，否则是「跟随别仓」的变更
    return repo in names


def parse_changelog_breaking(text: str, repo: str = "") -> tuple[list[dict], list[dict]]:
    """解析 CHANGELOG 文本 → (破坏性条目, 疑似破坏性条目)。

    破坏性条目形如 {"version": "v0.4.0", "line": "…"}。
    疑似条目（`**refactor(...)**` / `### Changed` 段——机器判不了语义）单独返回，
    供 audit 打提示，不进 breakingIndex。
    `repo` 供 E 路判定路径归属（谁的仓）。
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
            # 信号 E 在**合并续行后**判定：路径标识常在续行里
            if cur_version and (keep or _is_path_removal(line, repo)):
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


def _scan_changelog(path: Path, prefix: str = "", repo: str = "") -> list[dict]:
    """扫一个 CHANGELOG.md 的破坏性条目（版本号按 prefix 加嵌套模块前缀）。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    items, _ = parse_changelog_breaking(text, repo=repo)
    for it in items:
        it["version"] = _prefix_version(it["version"], prefix)
    return items


def _path_alive(repo: str, rel: str, root: Path) -> bool:
    """`github.com/oekit/<repo>/<rel>` 在当前源码树里还活着吗？

    活的判据：路径在文件系统中存在，且（是目录时）含 .go 文件——Go 包的
    最小单位是含 .go 文件的目录。
    """
    parts = [p for p in rel.split("/") if p]
    if parts and parts[0] == repo:
        parts = parts[1:]          # 去掉仓名前缀，得到仓内相对路径
    if not parts:
        return True                # 仓根本身
    target = root
    for p in parts:
        target = target / p
    if target.is_dir():
        return any(target.glob("*.go"))
    if target.is_file():
        return True
    parent = target.parent
    return parent.is_dir() and (parent / target.name).is_file()


def _mentions_path(text: str, repo: str, full: str) -> bool:
    """text 里是否提到了这条路径。

    候选形态：全限定 `github.com/oekit/<repo>/<rest>`、仓内相对 `<rest>`、
    末段包名——CHANGELOG 里三种写法都出现过（迁移表用全限定，条目里常用相对）。
    """
    parts = full.split("/")
    rel = "/".join(parts[1:]) if len(parts) > 1 else ""
    cands = {full}
    if rel:
        cands.add(rel)
        if rel.startswith(repo + "/"):
            cands.add(rel[len(repo) + 1:])
        cands.add(rel.split("/")[-1])
    return any(c and c in text for c in cands)


def _dead_path_entries(text: str, repo: str, root: Path, prefix: str = "") -> list[dict]:
    """扫 CHANGELOG 原文找**死路径** → [{version, line, path}]。

    version 取该路径被提及时所在的版本节；line 取提及它的那一行原文
    （与其它信号同构，便于双向门禁逐条比对）。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    version: str | None = None
    for raw in text.splitlines():
        m = VERSION_HEAD_RE.match(raw)
        if m:
            version = _normalize_version(m.group(1))
            continue
        if version is None:
            continue
        for pm in DEAD_PATH_RE.finditer(raw):
            full = pm.group(1)
            parts = full.split("/")
            if parts[0] != repo:
                continue                    # 只看本仓的路径
            if len(parts) >= 2 and parts[1] in URLISH:
                continue                    # issue/pull 等链接不是 import 路径
            if _path_alive(repo, full, root):
                continue                    # 还活着 → 不是破坏
            line = re.sub(r"\s+", " ", raw.strip())[:200]
            ver = _prefix_version(version, prefix)
            key = (ver, full)
            if key in seen:
                continue
            seen.add(key)
            out.append({"version": ver, "line": line, "path": full})
    return out


def breaking_index(repo: str) -> list[dict]:
    """扫 CHANGELOG.md，收集 breaking 事件 → {version, line}。

    范围 = 根模块 CHANGELOG + 各嵌套模块的 CHANGELOG（子目录含 go.mod 者）。
    嵌套模块的版本号带路径前缀（如 `redis/v0.2.0`）——嵌套模块的破坏性变更
    同样是下游必断的（如 timertask v0.2.0 把 Redis 后端迁至嵌套模块），
    只扫根 CHANGELOG 会漏掉它们。

    三路来源并用（互相补盲区）：
      · A–E：措辞类信号（段标题 / 行内关键词 / 包路径提升 / conventional 标记 /
        顶格独立标记行）——抓「措辞说了」的破坏；
      · F：本仓路径级移除（动作词 + 归属护栏）——抓措辞说破的移除；
      · G：**死路径**（路径在当前源码树里已消失）——抓**措辞没提**的移除，
        客观判据，不需要动作词表。
    """
    root = repo_dir(repo, BASE)
    items = _scan_changelog(root / "CHANGELOG.md", repo=repo)
    for gomod in sorted(root.glob("*/go.mod")):
        sub = gomod.parent
        # 嵌套模块的路径标识是 `github.com/oekit/<root>/<sub>`，归属判据认根仓名
        items.extend(_scan_changelog(sub / "CHANGELOG.md", prefix=f"{sub.name}/", repo=repo))
    # 信号 G：死路径（对根 CHANGELOG 与各嵌套 CHANGELOG 一起扫）
    # **只补其他信号没提到的**——它是兜底，不是重复收集。已收集条目的文本
    # 里提到过该路径（全限定/相对/末段任一形态）就跳过。
    def _add_dead_paths(cl_path: Path, pfx: str = "") -> None:
        if not cl_path.exists():
            return
        blob = "\n".join(it.get("line", "") for it in items)
        for dp in _dead_path_entries(cl_path.read_text(encoding="utf-8"), repo, root, pfx):
            if _mentions_path(blob, repo, dp["path"]):
                continue
            items.append({"version": dp["version"], "line": dp["line"]})
            blob += "\n" + dp["line"]

    _add_dead_paths(root / "CHANGELOG.md")
    for gomod in sorted(root.glob("*/go.mod")):
        _add_dead_paths(gomod.parent / "CHANGELOG.md", f"{gomod.parent.name}/")
    # 按 (version, line) 去重（多路来源可能命中同一条）
    uniq: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for it in items:
        key = (it.get("version", ""), it.get("line", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq


def breaking_suspects(repo: str) -> list[dict]:
    """扫 CHANGELOG.md，收集「疑似破坏性但机器判不了」的条目（供 audit 提示）。"""
    root = repo_dir(repo, BASE)
    out = []
    for path, prefix in [(root / "CHANGELOG.md", "")] + [
        (g.parent / "CHANGELOG.md", f"{g.parent.name}/") for g in sorted(root.glob("*/go.mod"))
    ]:
        if not path.exists():
            continue
        _, suspects = parse_changelog_breaking(path.read_text(encoding="utf-8"), repo=repo)
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
