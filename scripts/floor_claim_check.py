#!/usr/bin/env python3
"""地板类 CHANGELOG 断言的字节级核验器（**只读**：只读仓、只跑 git 读命令）。

背景（真实发现，2026-11-03）：
    logx v0.8.1 版本节写「logx 唯一用到的 core/lifecycle **逐字节未变**，
    抬地板只带动修复传递」——**实测为假**：core v0.9.2 → v0.12.1 之间
    `lifecycle/manager.go` +5 行（补 `defer signal.Stop(ch)`）。
    根因是**照抄旧断言**：logx#22（core v0.6.0 → v0.9.2 区间）那句
    「逐字节未变」经核**成立**，被原句搬到了新区间 ⇒ 断言在新区间失效。

为什么需要这个工具：
    org 的 `changelog_check.py` 只查**结构**（重复小节 / 顺序递减 / tag 命中），
    **不查语义** ⇒ 「逐字节未变」「零行为风险」这类**可证伪**断言没有任何门禁。
    本工具补这一层：把断言**落到 git 对象层**去核。

判据（一个 bullet 里同时出现三条才算一条「地板类断言」）：
    ① 依赖地板迁移：`<dep> vA → vB`
    ② 依赖包路径：`<dep>/<pkgpath>`（首段必须等于 ① 的 dep）
    ③ 字节级断言词：逐字节未变 / 字节级未变 / 零行为风险

核验（在 <dep> 仓的本地 clone 上，**只看 tag 的树**）：
    IDENTICAL    两版本该包路径的「文件清单 + blob sha」全一致 ⇒ 断言成立
    DIFFERS      有任何差异 ⇒ **断言为假**，并给出 diff 统计
    UNVERIFIABLE tag 不存在 / 包路径不存在 / 本地无该仓 clone
                 ⇒ **fail-closed**：既不算成立也不算假，单列出来

★ 本工具的三条口径纪律（落地时逐条实测过）

  1. **否定语境不是断言**：断言词若出现在**被否定的引用**里（如「初版此处写「…逐字节
     未变…」**不成立**」）⇒ 不算待核断言。**不是可选优化** —— logx v0.8.1 更正后的
     bullet 里**原文引用了旧假句**并声明其不成立 ⇒ 不过滤的话，护栏上线第一件事就是
     把「刚更正对的那条」判成假（自己打自己）。见 `_disclaimed_spans`。
  2. **读不到 ≠ 成立**：tag 不存在 / 包路径不存在 ⇒ UNVERIFIABLE 单列，
     **不并入绿**。
  3. **「0 告警」不是证据，「变异能转红」才是** ⇒ 自测每条用例都写死期望判定。

★ CI 约束（**这一段原是假断言，已就地更正**）

  初版此处写「CI 只 checkout 本仓且 `fetch-tags: false` ⇒ 全 org 扫描会全部
  UNVERIFIABLE ⇒ 不进 PR 门禁」——**复核班实测推翻**：matrix-tools 的 workflow 里
  `fetch-tags: false` **grep 全 0**（**它根本不是 `actions/checkout` 的开关名** —— checkout
  没有这个参数），且 `audit.yml` 的 audit job **逐仓 plain `git clone` 全矩阵**（实测新克隆
  core 带 14 个 tag，含 v0.6.0 / v0.9.2 / v0.12.1）⇒ **核验层要的 tag 树在 CI 里就在**
  ⇒ 全 org 扫描**可以**进 PR 门禁（现已真挂上，见 audit.yml 的 audit job）。
  ⇒ 真障碍不是「读不到 tag」，而是「读不到 **Release 正文**」——旧实现硬编码本地
  脚本路径 ⇒ 已改走仓内标准读取层 `ghsafe.gh_json`（CI 里靠 GH_TOKEN）。

  ⚠️ **机制措辞订正（2026-11-03 二次复核）**：本段初版更正写「`actions/checkout@v7`
  **默认即带 tag**」——**这句本身不准确**：`actions/checkout` 的 `fetch-depth` **默认为 1
  = 浅克隆 ⇒ 不带 tag**（实测：`git clone --depth 1` ⇒ core **0 个 tag**；plain
  `git clone` ⇒ 14 个 tag）⇒ **checkout 本身确实拿不到别仓 tag**。真正让 tag 树在 CI 里
  存在的是 **audit job 逐仓 plain `git clone`**（绕开 checkout），**不是** checkout 的默认行为。
  ⇒ **结论方向不变**（全 org 扫描能进 PR 门禁），**变的是机制归因**。
  ⇒ 挂载形态的纪律：**要 tag 树 ⇒ 用 plain clone（或 checkout 加 `fetch-depth: 0`）**。

  ⇒ 门禁里仍保留**离线自测**（自造 fixture git 仓，不联网、不依赖工作区）：
  它的作用是证明**判据本身有牙齿**，与真实语料扫描是两件事。

★ 出处：初版由同伴会话写在共享状态目录（`.oekit-cron-state/`），本仓移植时补了
  ① `MATRIX_BASE` 口径 ② 否定语境过滤 ③ 离线 fixture 自测（CI 可跑）。

用法：
    python3 floor_claim_check.py                 # 全 org 扫描（只读）
    python3 floor_claim_check.py --repo logx     # 只扫某仓
    python3 floor_claim_check.py --json          # 机器可读输出
    python3 floor_claim_check.py --self-test     # 离线变异自证（CI 跑这个，不联网）
    python3 floor_claim_check.py --self-test-live  # 加跑工作区依赖的用例（本地）

⚠️ 口径纪律：
    * 「读不到」（UNVERIFIABLE）**不等于**「断言成立」⇒ 单独一列，不并入绿。
    * 覆盖面必须随结论一起给出（扫了多少仓 / 提取了多少条断言）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# 各仓父目录：认 MATRIX_BASE（与 matrix.py / audit.py 同口径）⇒ 判据一致
WORKSPACE = Path(os.environ.get("MATRIX_BASE")
                    or (Path(__file__).resolve().parent.parent.parent)).resolve()

# ── 断言识别 ──────────────────────────────────────────────────────────────
# ① 依赖地板迁移：<dep> vA → vB（也接受 -> 和 ～）
#    ⚠️ dep 与版本号之间允许**中文隔断**（实测存在「core 地板 v0.6.0 → v0.9.2」形态）
#    ⇒ 用 [\u4e00-\u9fff]* 吃掉隔断词，但要求 dep 后面**必须**紧跟版本号形态。
TRANS = re.compile(
    r"(?<![\w./-])([a-z0-9][a-z0-9._-]*)\s+[\u4e00-\u9fff]{0,6}\s*"
    r"v(\d+\.\d+\.\d+)\s*(?:→|->|~|～)\s*v(\d+\.\d+\.\d+)"
)
# ② 依赖包路径：<dep>/<pkgpath>
PKG = re.compile(r"(?<![\w./-])([a-z0-9][a-z0-9._-]*)/([a-z0-9_][a-z0-9_./-]*)")
# ③ 字节级断言词（**提取口径必须披露**——见 EXTRACTION_SCOPE）
CLAIM_WORDS = ("逐字节未变", "字节级未变", "零行为风险")

# ★ 提取口径披露（2026-11-03 自查，同伴提点后落地）：
#   本工具**只**认上面 3 个字面量 ⇒ **同类但换措辞**的断言**提取不到**
#   ⇒ 报「N 条断言」时若不披露口径，读者会误以为「全语料只有 N 条」。
#   ⇒ ⚠️ **下面的数字是快照，会过期**：每发一版 ⇒ 已发布 Release 正文面 +1
#      ⇒ 覆盖面与宽口径候选数**必然增长**（实测同一分钟内 587→588 来源面、
#      宽口径 21→23 条）⇒ **引用前必须重跑本工具取当前值，别直接抄这里的数字**。
#   ⇒ 实测（快照基线：2026-11-03，25 根 + 7 嵌套 CHANGELOG + 556 个 Release 正文）：
#      **宽口径**（含两个版本号 + 任何可证伪措辞）候选 **23 条**，
#      本工具口径**只抓到 2 条** ⇒ 其余 21 条属**别的断言类**
#      （发布范围 / import 路径 / 行为不变）⇒ **没有新的假断言**，但那是**人工判读**的结论。
#   ⇒ 「**可证伪 ≠ 可机检**」⇒ 工具判不了的类**单列**（与「读不到要单列」同源）。
EXTRACTION_SCOPE = {
    "tool_words": list(CLAIM_WORDS),
    "requires": "① 含上述字面量之一 ② 含「<dep> vA → vB」形态 ③ 含「<dep>/<pkgpath>」形态",
    "known_other_classes": [
        "发布范围断言（本次发布自身有没有改 X）——不能靠 tag 区间 diff 判（tag-to-tag diff 混进区间内其它提交）",
        "import 路径断言（依赖拆包后路径不变）",
        "行为不变断言（错误映射语义等）",
    ],
}

# 宽口径的可证伪措辞（**只用于统计与单列**，不用于判定）
BROAD_FALSE_WORDS = (
    "逐字节未变", "字节级未变", "零行为风险", "行为不变", "无变化",
    "路径不变", "行为无变化", "未变",
)
BROAD_VER = re.compile(r"v\d+\.\d+\.\d+")

# 版本小节标题：## [v0.8.1] / ## [hlog/v0.1.6]
# ⚠️ 已知**第四种形态**：`## [hlog/v0.1.3 + kitexlog/v0.1.3]`（一个节头含两个嵌套模块，
#    全语料 2 个，都在 logx）⇒ 本正则用 `[^]]+` 捕获**整串** ⇒ 不会漏读该节，
#    但节名**不是**单一版本号 ⇒ 凡按 `## [<sub>/<ver>]` **精确匹配**的工具会读不到。
SECTION = re.compile(r"^##\s+\[([^\]]+)\]\s*(.*)$")
BULLET = re.compile(r"^(\s*)-\s+(.*)$")


def broad_candidates(blk: str) -> bool:
    """宽口径：一条 bullet 是否「含两个版本号 + 任何可证伪措辞」。

    ⚠️ **只用于统计与单列**，不代表它属于本工具的目标断言类 ⇒ 判红必须人工判读。
    """
    if len(set(BROAD_VER.findall(blk))) < 2:
        return False
    return any(w in blk for w in BROAD_FALSE_WORDS)


# 否定语境：断言词若出现在**被明确否定的引用**里（如「初版此处写「…逐字节未变…」不成立」），
# 它**不是**本工具要核的断言 ⇒ 必须跳过，否则会把「已更正」的那条报成假断言（假阳性）。
# ⚠️ 这不是可选优化：logx v0.8.1 更正后的 bullet 里**原文引用了旧假句**并声明其不成立
# ⇒ 不过滤的话，护栏上线第一件事就是把「刚更正对的那条」判成假（自己打自己）。
NEG_MARKERS = ("不成立", "为假", "是假", "有误", "错了", "不准确", "不再适用", "误述", "并非")
CORRECTION_MARKERS = ("本条为更正", "初版", "旧版", "原文", "更正", "订正", "勘误", "原版")
CLAUSE_BREAKS = "；;。.!！?？\n"
QUOTE_PAIRS = (("「", "」"), ("“", "”"), ("『", "』"))


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    """文本里所有引用段（中文引号 / 弯引号）的 [起, 止) 区间。"""
    spans: list[tuple[int, int]] = []
    for a, b in QUOTE_PAIRS:
        i = 0
        while True:
            s0 = text.find(a, i)
            if s0 < 0:
                break
            e0 = text.find(b, s0 + 1)
            if e0 < 0:
                break
            spans.append((s0, e0 + len(b)))
            i = e0 + 1
    return spans


def _disclaimed_spans(text: str) -> set[int]:
    """返回**被否定**的断言词出现位置。

    两级判定（都要求「否定」与该断言词**同处一个语境**，避免过度过滤）：

    ① **引用语境**：断言词落在引用段（「…」）内，且该引用段**后面跟着否定标记**
       （「…**不成立**」）或**前面有更正标记**（「本条为更正：初版此处写「…」）
       ⇒ 这是**引用并否定**旧说法 ⇒ 不是待核断言。
    ② **同子句否定**：断言词之后、**下一个分句符之前**出现否定标记
       ⇒ 同一个子句里说「逐字节未变……不成立」⇒ 不是待核断言。

    ⚠️ 窗口**不得**跨到下一个断言词（否则同一 bullet 里「一条活的 + 一条被否定的」
    会被一起吃掉 ⇒ 漏报）。自测 M9b 专门盯这条。
    """
    out: set[int] = set()
    spans = _quoted_spans(text)
    for w in CLAIM_WORDS:
        for m in re.finditer(re.escape(w), text):
            span = next((sp for sp in spans if sp[0] <= m.start() < sp[1]), None)
            if span is not None:
                before = text[max(0, span[0] - 40):span[0]]
                after = text[span[1]:span[1] + 40]
                if (any(n in after for n in NEG_MARKERS)
                        or any(c in before for c in CORRECTION_MARKERS)):
                    out.add(m.start())
                    continue
            tail = text[m.end():]
            cut = len(tail)
            for ch in CLAUSE_BREAKS:
                k = tail.find(ch)
                if k >= 0:
                    cut = min(cut, k)
            if any(n in tail[:cut] for n in NEG_MARKERS):
                out.add(m.start())
    return out


def live_claim_words(text: str) -> list[str]:
    """文本里**未被否定**的断言词（按 CLAIM_WORDS 顺序）。"""
    if not any(w in text for w in CLAIM_WORDS):
        return []
    disclaimed = _disclaimed_spans(text)
    out = []
    for w in CLAIM_WORDS:
        for m in re.finditer(re.escape(w), text):
            if m.start() not in disclaimed:
                out.append(w)
                break
    return out


def extract_claims(bullet_text: str) -> list[dict]:
    """从一条 bullet 的全文里提取「地板类断言」。

    返回 [{dep, old, new, pkg, word}]；没有则返回 []。
    ⚠️ 只在**同一个 dep** 上配对（pkg 路径首段必须等于 dep）——
    否则「core v0.9.2 → v0.12.1」+「别的/x」会被错配。
    """
    words = live_claim_words(bullet_text)
    if not words:
        # 没有断言词，或断言词**全在否定语境里**（引用旧假句并声明其不成立）
        # ⇒ 本 bullet 没有待核断言 ⇒ 提取 0 条（不得把「已更正」判成假）。
        return []
    out = []
    for m in TRANS.finditer(bullet_text):
        dep, old, new = m.group(1), m.group(2), m.group(3)
        # 在该 dep 的所有出现处附近找 <dep>/<pkgpath>
        for pm in PKG.finditer(bullet_text):
            if pm.group(1) != dep:
                continue
            # 包路径必须紧跟在该 dep 的地板迁移表述之后（同一子句）
            if pm.start() < m.start():
                continue
            out.append({
                "dep": dep,
                "old": "v" + old,
                "new": "v" + new,
                "pkg": pm.group(2),
                "word": words[0],
            })
            break
    return out


# ── git 只读核验 ─────────────────────────────────────────────────────────
def _git(repo_dir: Path, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True, text=True, timeout=120,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"<exec error: {exc}>"


def _tree(repo_dir: Path, rev: str, pkgpath: str) -> str | None:
    """某 rev 下某包路径的「文件清单 + blob sha」；不存在则 None。"""
    rc, out = _git(repo_dir, "ls-tree", "-r", rev, "--", pkgpath)
    if rc != 0:
        return None
    return out.strip()


def verify_claim(dep_dir: Path | None, old: str, new: str, pkgpath: str) -> tuple[str, str]:
    """核一条断言 ⇒ (verdict, detail)。

    verdict ∈ {IDENTICAL, DIFFERS, UNVERIFIABLE}
    """
    if dep_dir is None or not dep_dir.is_dir():
        return "UNVERIFIABLE", f"本地无 {old} 的 clone"

    for rev in (old, new):
        rc, out = _git(dep_dir, "rev-parse", "--verify", f"{rev}^{{commit}}")
        if rc != 0:
            return "UNVERIFIABLE", f"{dep_dir.name}: tag {rev} 不存在"

    t_old = _tree(dep_dir, old, pkgpath)
    t_new = _tree(dep_dir, new, pkgpath)
    if t_old is None or t_new is None:
        return "UNVERIFIABLE", f"{dep_dir.name}: 包路径 {pkgpath} 在 {old}/{new} 下不存在"
    if not t_old and not t_new:
        return "UNVERIFIABLE", f"{dep_dir.name}: 包路径 {pkgpath} 在两版本下均为空"

    if t_old == t_new:
        return "IDENTICAL", f"{pkgpath} 在 {old}→{new} 逐字节一致"

    rc, stat = _git(dep_dir, "diff", "--shortstat", old, new, "--", pkgpath)
    stat = " ".join(stat.split())
    changed = []
    for line in t_old.splitlines():
        if line not in t_new.splitlines():
            changed.append("-" + line.split("\t")[-1])
    for line in t_new.splitlines():
        if line not in t_old.splitlines():
            changed.append("+" + line.split("\t")[-1])
    detail = f"{pkgpath} 在 {old}→{new} **有差异**：{stat or 'n/a'}"
    if changed:
        detail += "；变动文件 " + "、".join(changed[:6])
    return "DIFFERS", detail


# ── 扫描 ────────────────────────────────────────────────────────────────
def repo_dirs() -> list[Path]:
    out = []
    for d in sorted(WORKSPACE.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if (d / ".git").exists() and (d / "CHANGELOG.md").exists():
            out.append(d)
    return out


def nested_changelogs(repo_dir: Path) -> list[tuple[str, str]]:
    """列出该仓**嵌套模块**的 CHANGELOG（相对仓根的路径）。

    ⚠️ 为什么要扫嵌套（2026-11-03 自查发现，缺口来自同伴对 `check_writeback.py`
       `--all` 的同类审计）：嵌套模块的版本节**住在自己的文件里**，根 CHANGELOG
       里的 `## [nacos/v0.3.7]` 是**根视角**的记录、**内容可以完全不同**
       （实测 `bootstrap/nacos` v0.3.7：根 = 父模块地板变更、嵌套 = `#33` 补记）
       ⇒ **不能靠「根已镜像」免掉嵌套侧**。
    ⇒ 只扫根 ⇒ 报「干净」时嵌套侧**根本没见过** = **假干净**（违反「读不到 ≠ 没有」）。
    """
    rc, out = _git(repo_dir, "ls-tree", "-r", "--name-only", "origin/main")
    if rc != 0:
        return []
    return [
        (p, "") for p in out.splitlines()
        if p.endswith("CHANGELOG.md") and "/" in p
    ]


def read_changelog_at(repo_dir: Path, rel: str) -> tuple[str, str]:
    """读 origin/main 上某个路径的 CHANGELOG（权威面）；取不到则退回工作树文件。"""
    rc, out = _git(repo_dir, "show", f"origin/main:{rel}")
    if rc == 0 and out.strip():
        return out, "origin/main"
    try:
        return (repo_dir / rel).read_text(encoding="utf-8"), "worktree"
    except Exception:  # noqa: BLE001
        return "", "none"


# ── 已发布 Release 正文（**第二个发布面**，必须一起扫）────────────────────
def release_bodies(repo: str) -> list[tuple[str, str]]:
    """取该仓已发布 Release 的 (tag, 正文)。

    ⚠️ 为什么必须扫 Release 正文（2026-11-03 自查发现的第三个覆盖面缺口）：
       CHANGELOG 版本节与该版本的 **Release 正文是逐字复制**（实测 logx v0.8.1：
       两条 bullet 逐字一致）⇒ **同一句假断言有两个对外可见的落点**
       ⇒ 只扫 CHANGELOG ⇒ 「Release 页面上的假断言」根本没见过 = 假干净。
    ⇒ 且**修法必须两边同时改**（只改 CHANGELOG ⇒ 两份面向读者的文案不一致）。

    用 GitHub API（Release 正文不在 git 里）；取不到 ⇒ 记入 unreadable，不并入绿。
    """
    # ★ 必须走仓内标准读取层 `ghsafe.gh_json`（自带重试 + 「读到空 ≠ 事实为空」+ 错误语义）
    #   —— 旧实现硬编码本地脚本 `WORKSPACE/.oekit-cron-state/ghp.sh`，那个文件只存在于
    #   共享克隆 ⇒ CI 里 WORKSPACE=repos/ ⇒ **25 个仓的 Release 正文全部读不到**
    #   ⇒ 第二个发布面在 CI 里覆盖率为 0（虽不静默：会进 releases_unreadable 并打印 ⚠️）。
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ghsafe  # noqa: PLC0415 - 运行时导入，避免与仓内其它模块的导入顺序耦合
        data = ghsafe.gh_json(["api", f"repos/oekit/{repo}/releases?per_page=100"])
    except Exception:  # noqa: BLE001 - 读不到 ⇒ 由调用方记入 unreadable，不并入绿
        return []
    if not isinstance(data, list):
        return []
    return [(r.get("tag_name", "?"), r.get("body") or "") for r in data if isinstance(r, dict)]


def read_changelog(repo_dir: Path) -> tuple[str, str]:
    """读 origin/main 上的**根** CHANGELOG（权威面）；取不到则退回工作树文件。"""
    return read_changelog_at(repo_dir, "CHANGELOG.md")


def scan(repo_filter: str | None = None, include_releases: bool = True) -> dict:
    repos = repo_dirs()
    if repo_filter:
        repos = [r for r in repos if r.name == repo_filter]
    findings = []
    scanned_repos, repos_without_claims = 0, 0
    # ★ 仓级累加器：**一个仓只要有任一来源面含断言，就算「有此类断言」**
    #   （旧实现把文件级的 in_claims 在仓级读 ⇒ 读的是**最后一个来源面**留下的值
    #    ⇒ 一个仓只要最后一个来源面没断言就被算成「无断言」⇒ 全 org 报「25 个仓无断言」
    #    却同屏显示「提取断言 2 条」= **自相矛盾**的假干净。复核班实测发现。）
    repo_has_claims = False
    total_claims = 0
    scanned_files = 0
    root_files = 0
    nested_files = 0
    nested_unreadable = []
    release_files = 0
    releases_unreadable = []
    broad_total = 0
    broad_unmatched = []

    for repo_dir in repos:
        # ★ 每个仓**重置**仓级累加器（否则上一个仓的 True 会漏给下一个仓 ⇒
        #   「无断言的仓」一个都数不出来 —— M12 专盯这条）
        repo_has_claims = False
        # ── 根 CHANGELOG ──
        files = [("CHANGELOG.md", read_changelog(repo_dir))]
        # ── 嵌套 CHANGELOG（**必须扫**，否则是假干净）──
        for rel, _ in nested_changelogs(repo_dir):
            text, src = read_changelog_at(repo_dir, rel)
            if not text:
                nested_unreadable.append(f"{repo_dir.name}/{rel}")
                continue
            files.append((rel, (text, src)))
        # ── 已发布 Release 正文（**第二个发布面**，必须一起扫）──
        rel_bodies = release_bodies(repo_dir.name) if include_releases else []
        if not rel_bodies:
            releases_unreadable.append(repo_dir.name)
        for tag, body in rel_bodies:
            if not body.strip():
                continue
            files.append((f"release:{tag}", (body, "github-release")))

        for rel, (text, src) in files:
            if not text:
                continue
            scanned_files += 1
            is_release = rel.startswith("release:")
            if is_release:
                release_files += 1
            elif "/" in rel:
                nested_files += 1
            else:
                root_files += 1
            repo_key = repo_dir.name if rel == "CHANGELOG.md" else f"{repo_dir.name}/{rel}"
            section = rel.split(":", 1)[1] if is_release else "(preamble)"
            in_claims = 0
            # ★ 断言词**可能落在 bullet 的续行里**（实测：bootstrap 根 CHANGELOG 的
            #   #42 / #33 就在续行上）⇒ 必须把续行并入所属 bullet 再判定，
            #   否则「断言只在续行」会被整条漏掉（假干净）。
            bullet_parts: list[str] = []

            def flush_bullet() -> None:
                """把累积的 bullet（首行 + 续行）判一次。"""
                nonlocal in_claims, total_claims, broad_total, repo_has_claims
                if not bullet_parts:
                    return
                blk = "\n".join(bullet_parts)
                claims = extract_claims(blk)
                # ★ 宽口径候选：工具抓不到、但**看起来**可证伪的 bullet ⇒ **单列**
                #   （「可证伪 ≠ 可机检」⇒ 不能让它看起来像「没有这类断言」）
                if broad_candidates(blk):
                    broad_total += 1
                    if not claims:
                        broad_unmatched.append((repo_key, section, blk))
                if not claims:
                    bullet_parts.clear()
                    return
                in_claims += 1
                repo_has_claims = True
                total_claims += len(claims)
                for c in claims:
                    dep_dir = WORKSPACE / c["dep"]
                    verdict, detail = verify_claim(
                        dep_dir if dep_dir.is_dir() else None,
                        c["old"], c["new"], c["pkg"],
                    )
                    findings.append({
                        "repo": repo_key,
                        "section": section,
                        "src": src,
                        "dep": c["dep"],
                        "old": c["old"],
                        "new": c["new"],
                        "pkg": c["pkg"],
                        "word": c["word"],
                        "verdict": verdict,
                        "detail": detail,
                        "bullet": blk[:220],
                    })
                bullet_parts.clear()

            for raw in text.splitlines():
                sm = SECTION.match(raw)
                if sm:
                    flush_bullet()          # ★ 节边界：先封口上一条
                    section = sm.group(1)
                    continue
                bm = BULLET.match(raw)
                if bm:
                    flush_bullet()
                    bullet_parts = [bm.group(2)]
                    continue
                if bullet_parts and raw.strip():
                    bullet_parts.append(raw.strip())
            flush_bullet()
        if not repo_has_claims:
            repos_without_claims += 1

    scanned_repos = len(repos)
    return {
        "scanned_repos": scanned_repos,
        "scanned_files": scanned_files,
        "root_files": root_files,
        "nested_files": nested_files,
        "nested_unreadable": nested_unreadable,
        "release_files": release_files,
        "releases_unreadable": releases_unreadable,
        "broad_total": broad_total,
        "broad_unmatched": broad_unmatched,
        "repos_without_claims": repos_without_claims,
        "total_claims": total_claims,
        "findings": findings,
    }


# ── 变异自证 ────────────────────────────────────────────────────────────
def self_test(live: bool = False) -> int:
    """自测：**默认离线**（fixture 仓，不依赖工作区、不联网）⇒ 可挂 PR 的 CI。

    口径纪律：
      * 「0 告警」不是证据，「**变异能转红**」才是 ⇒ 自测里每条用例都写死了
        期望判定（IDENTICAL / DIFFERS / UNVERIFIABLE / 提取 0 条）。
      * 核验层必须**有牙齿**：读不到（tag 不存在 / 包路径不存在）⇒ UNVERIFIABLE，
        **不得**并入「成立」。
      * 「已被否定的引用」**不是**待核断言（见 `_disclaimed_spans`）⇒ 否则护栏
        会把「刚更正对的那条」判成假。
      * `--live` 才跑依赖工作区的用例（嵌套覆盖 / Release 正文）⇒ CI 不跑它
        （CI 只 checkout 本仓 ⇒ 默认浅克隆拿不到别仓 tag；真实语料扫描另由 audit
        job 逐仓 plain `git clone` 承担）。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import floor_claim_check_selftest as st  # noqa: PLC0415 - 避免与 selftest 循环导入
    return st.run(live=live)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-releases", action="store_true",
                    help="只扫 CHANGELOG（不读 Release 正文）⇒ 离线档，不联网")
    ap.add_argument("--self-test", action="store_true",
                    help="离线自测（fixture 仓，不联网、不依赖工作区）")
    ap.add_argument("--self-test-live", action="store_true",
                    help="自测 + 工作区依赖的用例（本地跑，CI 不跑）")
    args = ap.parse_args()

    if args.self_test or args.self_test_live:
        return self_test(live=args.self_test_live)

    res = scan(args.repo, include_releases=not args.no_releases)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        n_root = res['root_files']
        print(f"扫描 {res['scanned_repos']} 个仓 · **{res['scanned_files']} 个来源面**"
              f"（根 CHANGELOG {n_root} + 嵌套 CHANGELOG {res['nested_files']}"
              f" + **已发布 Release 正文 {res['release_files']}**）")
        if res.get("nested_unreadable"):
            print(f"⚠️ **读不到的嵌套 CHANGELOG {len(res['nested_unreadable'])} 份**"
                  f"（读不到 ≠ 干净 ⇒ 单列，不并入绿）：{res['nested_unreadable']}")
        if res.get("releases_unreadable"):
            print(f"⚠️ **Release 正文读不到 {len(res['releases_unreadable'])} 仓**"
                  f"（读不到 ≠ 干净 ⇒ 单列）：{res['releases_unreadable']}")
        print(f"其中 {res['repos_without_claims']} 个仓无此类断言；"
              f"提取断言 {res['total_claims']} 条\n")
        hdr = f"{'仓':<34}{'版本节':<16}{'依赖':<8}{'区间':<22}{'包路径':<16}判定"
        print(hdr)
        print("-" * len(hdr))
        for f in res["findings"]:
            rng = f"{f['old']} → {f['new']}"
            print(f"{f['repo']:<34}{f['section']:<16}{f['dep']:<8}{rng:<22}"
                  f"{f['dep']+'/'+f['pkg']:<16}{f['verdict']}")
            print(f"    └ {f['detail']}")
            if f["verdict"] == "DIFFERS":
                print(f"      原文：{f['bullet']}")
        n_diff = sum(1 for f in res["findings"] if f["verdict"] == "DIFFERS")
        n_unver = sum(1 for f in res["findings"] if f["verdict"] == "UNVERIFIABLE")
        n_ok = sum(1 for f in res["findings"] if f["verdict"] == "IDENTICAL")
        print(f"\n判定汇总：成立 {n_ok} / **为假 {n_diff}** / 读不到 {n_unver}"
              f"（共 {res['total_claims']} 条；读不到 ≠ 成立）")
        print(f"覆盖面：{res['scanned_repos']} 仓 / {res['scanned_files']} 来源面"
              f"（根 {n_root} + 嵌套 {res['nested_files']} + Release 正文 {res['release_files']}）"
              f"⇒ 结论覆盖全体，不是子集上的 0")
        # ★ 提取口径披露：不说清楚，读者会以为「N 条断言」= 全语料只有 N 条
        print("\n【提取口径（本工具只认这些，别把它当全语料）】")
        print(f"  断言词：{'、'.join(EXTRACTION_SCOPE['tool_words'])}")
        print(f"  另需同时满足：{EXTRACTION_SCOPE['requires']}")
        if res["broad_total"]:
            print(f"  ⚠️ **宽口径候选 {res['broad_total']} 条**（含两个版本号 + 任何可证伪措辞）"
                  f"⇒ 其中 **{len(res['broad_unmatched'])} 条本工具抓不到**"
                  f"（「可证伪 ≠ 可机检」⇒ **单列，不并入绿**，需人工判读）：")
            for repo_key, sec, blk in res["broad_unmatched"]:
                print(f"      - {repo_key} @ {sec}：{blk[:100]}")
        print(f"  已知**别的断言类**（人工判读结论：均非本工具目标类 ⇒ 未发现新的假断言）：")
        for k in EXTRACTION_SCOPE["known_other_classes"]:
            print(f"      · {k}")
        return 1 if n_diff else 0
    return 1 if any(f["verdict"] == "DIFFERS" for f in res["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
