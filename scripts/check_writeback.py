#!/usr/bin/env python3
"""写回版本节前的**前置门禁**（#180）：落笔前先查「你要写的那条 bullet」。

为什么需要这个门禁
------------------
matrix-tools#169 的批量「把 issue 号写回 CHANGELOG 版本节」实测踩到过一个真实缺陷：

修复 PR 的标题为了关联/致敬，**带了别的仓的 issue 号**（实测：
`chore(tracing): 测试依赖的 sqlite 驱动换成纯 Go 实现…（关联 gormx#36）`）——
直接抄进版本节的 bullet 首行 ⇒ `gormx#36` 落在**别的**那条 bullet 里 ⇒
那个 issue 的回执**张冠李戴**（读者照着回执找，会以为 gormx#36 修的就是这条）。

⇒ 门禁的位置：**写回之前**（落笔 ⇒ 跑门禁 ⇒ 才允许提交/推送）。

三个告警类别
------------
1. `cross_repo`：bullet 里出现**别仓**的号（`otherrepo#M`）⇒ 张冠李戴风险。
   ⚠️ #172 之后定位器的弱形态已带左边界，`gormx#36` 不会再被当成本仓的号绑定；
   但它**仍然会被人读成**「这条讲的是 gormx#36」⇒ 仍要告警（防人工抄错）。
2. `same_repo_other`：bullet 里出现**本仓的其它号** M ≠ N（**强形态** `repo#M`）
   ⇒ 写 N 会让**两个 issue 共用一条目** ⇒ 归属歧义。
3. `bare_other`：bullet 里出现**裸弱形态** `#M`（M ≠ N、**无仓名前缀**）
   ⇒ 归属不可判定；按 #172 的口径它**会被当成本仓号绑定** ⇒ 必须告警。

⚠️ 类别 2 与 3 在「弱形态」上重叠（弱形态 `#M` 也就是「本仓的其它号」）。
   本门禁**一个告警只给一个类别** ⇒ 弱形态归入更具体的 `bare_other`
   （它的危害是「归属不可判定」，比「明确写了本仓其它号」更重）。

**不告警**
- `PR #M` / `PRs #M` / `PR #148、#149` / `pull/149` 这类 **PR 引用**：
  定位器会**先剥离 PR 引用再判**（PR 与 issue 共用编号空间）⇒ 这里也先剥离。
- **顶格段落 / 缩进非 bullet 段落**：不产生条目绑定 ⇒ 不构成张冠李戴。
  （matrix-tools 自己的惯例：血缘号**顶格**声明、**故意不放 bullet 里**。）

判据不许共享逻辑
----------------
本脚本**不 import** `gen_releases.locate_issue_entries` / `mentions_issue` /
`_pr_only_mentions` ⇒ 门禁**自己**扫、自己判形态。
否则就是「**用被检对象验被检对象**」：定位器改了口径、门禁跟着变 ⇒ 门禁形同虚设。

用法
----
  python3 scripts/check_writeback.py --repo matrix-tools --version v0.1.37 --num 180
  python3 scripts/check_writeback.py --repo matrix-tools --version v0.1.37 --num 180 --json
  python3 scripts/check_writeback.py --repo matrix-tools --all             # dry-run 扫全库
  python3 scripts/check_writeback.py --repo matrix-tools --all --write-allowlist <path>

退出码：0 干净；1 有告警（fail closed 友好）；2 用法/IO/解析错误（**读不到 ≠ 没有**）。

`--write-allowlist` 的归档口径
------------------------------
归档必须覆盖**所有视角**（`all_perspective_findings`）：每个绑定号都当一次
「自己的号」再取并集。`--all` 的**报告**只用一侧（自己的号 = bullet 绑定的
第一个号），但**归档**若也只用那一侧 ⇒ 多号 bullet（合法的历史形态：一条 bullet
覆盖本仓多个 issue）的**另一侧**盖不住 ⇒ 单条模式改以另一个号为目标时照样红
⇒「归档完了还是红的」。

去重按**约束键**（repo / version / num / category / bullet，缺一即通配），
**不含 reason** ⇒ 只差 reason 的同键条目不重复入库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from matrix import repo_dir  # noqa: E402  —— 事实层（路径解析）只从这里取

BASE = os.path.abspath(os.environ.get(
    "MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))

# 本仓自带的 allowlist（可配：--allowlist <path>）。首跑归完 allowlist 后，
# 后续跑只报**新增**的形态。
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "writeback_allowlist.json"

CATEGORIES = ("cross_repo", "same_repo_other", "bare_other")


# ── 号的形态（本脚本自己的判据，不与定位器共享实现）────────────────────

# bullet 行：`- ` / `* ` / `+ `（可缩进）。
BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")

# **PR 引用**：`PR #149` / `PRs #149` / `pull request #149` / `merged PR #149`
# / `PR #148、#149`（连号列举）/ `pull/149`。
# 定位器会**先把这些整段抹掉**再判 issue 引用（PR 与 issue 共用编号空间）⇒
# 这里**同样先剥离**（形态是**输入契约**，实现是本脚本自己的）。
PR_REF_RE = re.compile(
    r"(?i)\b(?:merged\s+pr|pull\s+requests?|pulls?|prs?)"
    r"\s*(?:#\d+(?:\s*[、,，/和]\s*#?\d+)*|/\d+)")

# **强形态**：`<repo>#<num>`。仓名取 `[A-Za-z0-9][A-Za-z0-9._-]*`；
# 左边界不吃 `[\w.#-]` ⇒ `oekit/data#39` 里的仓名是 `data`（owner 段不算仓名）。
#
# ⚠️ 强形态与弱形态**用同一个正则的两支**（而不是分两趟扫）：分两趟扫会
# 把「强形态」排在「弱形态」前面 ⇒ 一行里同时有 `#180` 与 `matrix-tools#181` 时，
# 「这条 bullet 自己的号」会被判成 181（**位置序**才是对的：自己的号在前面）。
# 弱形态那支带**左边界**（#172）：`#` 前面不能是词字符或 `#`
# ⇒ `pkg#46` / `concurrency#56` 里那个号**不是**裸号（它们走强形态分支），
# `##1620` 也不是（那是另一个号的一部分）。
TOKEN_RE = re.compile(
    r"(?<![\w.#-])([A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)\b"   # 强形态
    r"|(?<![\w#])#(\d+)\b")                                # 弱形态


class Hit(NamedTuple):
    """文本里一处号引用。"""
    kind: str        # "strong" | "weak"
    repo: str        # 强形态的仓名；弱形态为空串
    num: str
    text: str        # 命中的原文片段


class Block(NamedTuple):
    """一个块：bullet（含其缩进续行）或一个非 bullet 段落行。"""
    is_bullet: bool
    lines: list[str]
    line: int        # 首行在文件里的行号（1 起）


class Finding(NamedTuple):
    """一条告警。"""
    repo: str
    version: str
    num: str
    category: str
    matched: str
    bullet: str      # bullet 的首行（整行，strip 后）
    line: int
    allowed: bool
    reason: str


# ── 切块 / 扫号 ────────────────────────────────────────────────────────

def split_blocks(lines: list[str], offset: int = 0) -> list[Block]:
    """把一节切成块：bullet（含其缩进续行）成一块，其余各成一块。

    规则与定位器一致（`- `/`* `/`+ ` 起头 + 更深的缩进续行；空行断开），
    但**实现独立**（不共享定位器的代码）。
    """
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = BULLET_RE.match(line)
        if not m:
            blocks.append(Block(False, [line], offset + i + 1))
            i += 1
            continue
        indent = len(m.group(1))
        blk = [line]
        start = i
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip():
                break
            nm = BULLET_RE.match(nxt)
            cur = len(nm.group(1)) if nm else len(nxt) - len(nxt.lstrip())
            if cur <= indent:
                break
            blk.append(nxt)
            i += 1
        blocks.append(Block(True, blk, offset + start + 1))
    return blocks


def scan_line(line: str) -> list[Hit]:
    """一行里的所有号引用（**已剥离 PR 引用**），按**出现位置**排序。

    顺序：先剥 PR 引用（PR 与 issue 共用编号空间 ⇒ 定位器也是先剥再判），
    再用**同一个正则的两支**扫强形态 / 弱形态 ⇒ 命中天然按位置排序
    （一行里同时有 `#180` 与 `matrix-tools#181` 时，「自己的号」是**前面**那个）。

    强形态的片段被**整段吃掉**（正则一次匹配掉 `gormx#36` 整段）⇒
    里面的 `#36` 不会再被弱形态认第二次（一个号只报一遍）。
    """
    text = PR_REF_RE.sub(" ", line)
    hits: list[Hit] = []
    for m in TOKEN_RE.finditer(text):
        if m.group(1) is not None:
            hits.append(Hit("strong", m.group(1), m.group(2), m.group(0)))
        else:
            hits.append(Hit("weak", "", m.group(3), m.group(0)))
    return hits


def is_bound(hit: Hit, repo: str) -> bool:
    """这处引用会不会**绑定到本仓的某个号**（⇒ 会成为那条 bullet 的绑定项）。

    - 弱形态 `#N` ⇒ 会（#172 的口径：左边界干净 ⇒ 算本仓的号）；
    - 强形态 `repo#N` ⇒ 只有 `repo` 就是本仓时才会。
    """
    if hit.kind == "weak":
        return True
    return hit.repo.lower() == repo.lower()


def bullet_first_line(block: Block) -> str:
    """bullet 的首行（去掉行首的列表符号），用于报告与 allowlist 匹配。"""
    return BULLET_RE.sub("", block.lines[0]).strip() if block.lines else ""


# ── 版本节 ─────────────────────────────────────────────────────────────

SECTION_RE = re.compile(r"^## \[")


def section_ranges(lines: list[str]) -> list[tuple[str, int, int]]:
    """返回 [(head, body_start, body_end)]：head 是 `## [...]` 里的文本。

    分节规则与定位器一致：以 `## [` 开头的行开一节，节体到下一个 `## [` 或文末。
    """
    out: list[tuple[str, int, int]] = []
    starts: list[tuple[str, int]] = []
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if not m:
            continue
        head = line[line.index("[") + 1:line.index("]")]
        starts.append((head, i))
    for idx, (head, start) in enumerate(starts):
        end = starts[idx + 1][1] if idx + 1 < len(starts) else len(lines)
        body_start = start + 1
        out.append((head, body_start, end))
    return out


def find_section(lines: list[str], head: str) -> tuple[int, int] | None:
    """找 `## [head]` 那一节的节体范围；找不到返回 None。"""
    for h, s, e in section_ranges(lines):
        if h == head:
            return s, e
    return None


def resolve_changelog(
        repo: str, version: str, nested: str | None, file_override: str | None
) -> tuple[Path | None, str | None, str | None, int]:
    """定位「要检查的版本节」⇒ (文件, 节体文本, 实际节头)。

    取舍与 `changelog_section()` 同款：嵌套优先 `<sub>/CHANGELOG.md` 的
    `[vX.Y.Z]`，回退根 CHANGELOG 的 `[<sub>/vX.Y.Z]`。
    ⚠️ 找不到文件/节 ⇒ 返回 None（**读不到 ≠ 没有** ⇒ 调用方必须报错退出，
    不许当成「干净」）。
    """
    if file_override:
        p = Path(file_override)
        if not p.exists():
            return None, None, None, 0
        lines = p.read_text(encoding="utf-8").splitlines()
        heads = [version]
        if nested:
            heads.append(f"{nested}/{version}")
        for head in heads:
            rng = find_section(lines, head)
            if rng:
                s, e = rng
                return p, "\n".join(lines[s:e]), head, s
        return p, None, None, 0

    base = Path(BASE)
    try:
        rdir = repo_dir(repo, str(base))
    except FileNotFoundError:
        return None, None, None, 0

    if nested:
        sub_cl = rdir / nested / "CHANGELOG.md"
        if sub_cl.exists():
            lines = sub_cl.read_text(encoding="utf-8").splitlines()
            rng = find_section(lines, version)
            if rng:
                s, e = rng
                return sub_cl, "\n".join(lines[s:e]), version, s
        root_cl = rdir / "CHANGELOG.md"
        if not root_cl.exists():
            return None, None, None, 0
        lines = root_cl.read_text(encoding="utf-8").splitlines()
        head = f"{nested}/{version}"
        rng = find_section(lines, head)
        if rng:
            s, e = rng
            return root_cl, "\n".join(lines[s:e]), head, s
        return root_cl, None, None, 0

    root_cl = rdir / "CHANGELOG.md"
    if not root_cl.exists():
        return None, None, None, 0
    lines = root_cl.read_text(encoding="utf-8").splitlines()
    rng = find_section(lines, version)
    if rng:
        s, e = rng
        return root_cl, "\n".join(lines[s:e]), version, s
    return root_cl, None, None, 0


# ── 告警判定 ───────────────────────────────────────────────────────────

def judge_block(block: Block, repo: str, own_num: str | None, version: str) -> list[Finding]:
    """一个 bullet 块里混进了哪些不该有的号。

    `own_num` = 这条 bullet **自己的**号（单条模式下是 `--num` 给的 N；
    全库模式下是该块绑定的**第一个**号）。None = 这块没绑定任何本仓的号。
    """
    out: list[Finding] = []
    if not block.is_bullet:
        return out
    bullet = bullet_first_line(block)
    for off, line in enumerate(block.lines):
        for hit in scan_line(line):
            if hit.kind == "strong" and not is_bound(hit, repo):
                category = "cross_repo"
            elif own_num is not None and hit.num != own_num and is_bound(hit, repo):
                category = "same_repo_other" if hit.kind == "strong" else "bare_other"
            else:
                continue
            out.append(Finding(repo=repo, version=version, num=hit.num,
                               category=category, matched=hit.text,
                               bullet=bullet, line=block.line + off,
                               allowed=False, reason=""))
    return out


def bound_nums(block: Block, repo: str) -> list[str]:
    """这个块绑定到的本仓的号（按出现顺序；强形态与弱形态都算）。"""
    out: list[str] = []
    for line in block.lines:
        for hit in scan_line(line):
            if is_bound(hit, repo) and hit.num not in out:
                out.append(hit.num)
    return out


def all_perspective_findings(section_text: str, repo: str, version: str,
                             line_offset: int = 0) -> list[Finding]:
    """一个版本节里**所有视角**的告警（每个绑定号各当一次「自己的号」）。

    为什么 `--all` 那一侧不够（#181 的实测缺口）
    ------------------------------------------
    `--all` 模式把「自己的号」取成**该 bullet 绑定的第一个号** ⇒ 多号 bullet
    （合法的历史形态：一条 bullet 覆盖本仓多个 issue，例如 concurrency v0.5.0 的
    `FanInErr` 那条同时带 `#9` 与 `#7`）只会报出**一侧**。

    ⇒ 用 `--all` 的结果归档 allowlist，**盖不住另一侧**：单条模式改以另一个号
    为目标时（`--repo concurrency --version v0.5.0 --num 7` ⇒ 自己的号变成 7
    ⇒ 告警从「#7」变成「#9」），allowlist 里的 `num` 对不上 ⇒ 照样红
    ⇒ 「归档完了还是红的」。

    ⇒ 归档必须按**所有视角**取并集：每个绑定号都当一次「自己的号」，
    外加 `own=None`（只看跨仓号）那一路。
    """
    blocks = split_blocks(section_text.splitlines(), offset=line_offset)
    out: list[Finding] = []
    seen: set[tuple] = set()
    for block in blocks:
        if not block.is_bullet:
            continue
        nums = bound_nums(block, repo)
        for own in (nums + [None]):
            for f in judge_block(block, repo, own, version):
                key = (f.category, f.num, f.matched, f.bullet, f.line)
                if key in seen:
                    continue
                seen.add(key)
                out.append(f)
    return out


def check_section(section_text: str, repo: str, version: str, num: str | None,
                  line_offset: int = 0) -> tuple[list[Finding], bool]:
    """检查一节 ⇒ (告警列表, 是否找到目标 bullet)。

    - `num` 给定（单条模式）：只看**绑定到该号**的 bullet ⇒ 别的 bullet 不算。
    - `num` 为 None（全库模式）：每条 bullet 自己的号 = 它绑定的**第一个**号 ⇒
      块里多出来的号就是告警。
    """
    blocks = split_blocks(section_text.splitlines(), offset=line_offset)
    findings: list[Finding] = []
    target_found = False
    for block in blocks:
        if not block.is_bullet:
            continue
        if num is not None:
            if num not in bound_nums(block, repo):
                continue
            target_found = True
            own = num
        else:
            nums = bound_nums(block, repo)
            own = nums[0] if nums else None
        findings.extend(judge_block(block, repo, own, version))
    return findings, target_found


# ── allowlist ──────────────────────────────────────────────────────────

def load_allowlist(path: Path | None) -> list[dict]:
    """读 allowlist。文件不存在 ⇒ 没有豁免（**不是**读失败）。

    读到了但**结构不对** ⇒ 抛错（调用方退出码 2）：豁免表读不懂时静默放行
    = 门禁形同虚设 ⇒ 一律 fail closed。
    """
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"✗ allowlist 不是合法 JSON（{path}）：{e}")
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise SystemExit(f"✗ allowlist 结构不对（{path}）：缺 entries 数组")
    for e in entries:
        if not isinstance(e, dict):
            raise SystemExit(f"✗ allowlist 条目不是对象（{path}）：{e!r}")
        if not any(e.get(k) for k in ("repo", "version", "num", "category", "bullet")):
            raise SystemExit(f"✗ allowlist 条目没有任何约束字段（{path}）：{e!r}"
                             "——无约束的条目会豁免一切告警")
        if e.get("category") and e["category"] not in CATEGORIES:
            # 类别名写错 ⇒ 这条豁免**永远匹配不上**（静默失效）⇒ 宁可报错。
            raise SystemExit(f"✗ allowlist 条目的 category 不认识（{path}）："
                             f"{e['category']!r}（合法值：{list(CATEGORIES)}）")
    return entries


def finding_allowed(finding: Finding, entries: list[dict]) -> tuple[bool, str]:
    """这条告警是否被 allowlist 豁免。

    条目里**写了的字段都要对上**（没写的字段 = 通配）⇒ 豁免可以精确到
    「某仓 / 某版本 / 某个号 / 某条 bullet」。
    """
    for e in entries:
        if e.get("repo") and e["repo"] != finding.repo:
            continue
        if e.get("version") and e["version"] != finding.version:
            continue
        if e.get("num") and str(e["num"]) != finding.num:
            continue
        if e.get("category") and e["category"] != finding.category:
            continue
        if e.get("bullet") and e["bullet"] not in finding.bullet:
            continue
        return True, str(e.get("reason") or "")
    return False, ""


def apply_allowlist(findings: list[Finding], entries: list[dict]) -> list[Finding]:
    out: list[Finding] = []
    for f in findings:
        ok, reason = finding_allowed(f, entries)
        out.append(f._replace(allowed=ok, reason=reason))
    return out


def allowlist_entry(finding: Finding, reason: str) -> dict:
    """把一条告警渲染成 allowlist 条目（供 `--write-allowlist`）。"""
    return {"repo": finding.repo, "version": finding.version, "num": finding.num,
            "category": finding.category, "bullet": finding.bullet,
            "reason": reason}


def allowlist_key(entry: dict) -> tuple:
    """allowlist 条目的**约束键**（不含 reason）。

    约束键 = 匹配时真正参与判断的那些字段（缺一即通配）⇒
    同键的条目**互相等价** ⇒ 入库时按它去重，reason 不同不重复入库。
    """
    return (entry.get("repo"), entry.get("version"), str(entry.get("num")),
            entry.get("category"), entry.get("bullet"))


# ── CLI ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="写回版本节前的门禁：查目标 bullet 里有没有混入别仓/本仓其它的号")
    p.add_argument("--repo", required=True, help="本仓名（如 matrix-tools）")
    p.add_argument("--version", help="目标版本节（如 v0.1.37）")
    p.add_argument("--nested", help="嵌套模块子路径（如 bootstrap）")
    p.add_argument("--num", help="要写回的本条号 N（单条模式）")
    p.add_argument("--file", help="直接指定要读的 CHANGELOG 文件")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--allowlist", help="allowlist 文件路径（默认 scripts/writeback_allowlist.json）")
    p.add_argument("--all", action="store_true", help="dry-run：扫本仓所有版本节")
    p.add_argument("--require-target", action="store_true",
                   help="单条模式下目标 bullet 不存在 ⇒ 报错退出（防「没检查到就放行」）")
    p.add_argument("--write-allowlist", help="把本次发现写成 allowlist 条目（仅配合 --all）")
    p.add_argument("--reason", default="首跑归档：真实语料里已存在的形态",
                   help="--write-allowlist 写入的 reason")
    return p


def _run_all(repo: str, nested: str | None, file_override: str | None,
             entries: list[dict], want_perspectives: bool = False
             ) -> tuple[list[Finding], list[str], list[Finding]]:
    """全库 dry-run ⇒ (告警, 出错信息, 所有视角的告警)。

    第三项只在 `--write-allowlist` 时需要（归档必须覆盖**所有视角**，
    否则多号 bullet 的另一侧仍红）⇒ 由 `want_perspectives` 控制要不要算。
    """
    findings: list[Finding] = []
    errors: list[str] = []
    perspectives: list[Finding] = []
    # (文件, 节头, 节体, 节体首行的 0 基行号) —— 行号要落在**文件**坐标系里
    targets: list[tuple[Path, str, str, int]] = []
    if file_override:
        p = Path(file_override)
        if not p.exists():
            return [], [f"✗ 文件不存在：{p}"], []
        lines = p.read_text(encoding="utf-8").splitlines()
        for head, s, e in section_ranges(lines):
            targets.append((p, head, "\n".join(lines[s:e]), s))
    else:
        try:
            rdir = repo_dir(repo, BASE)
        except FileNotFoundError:
            return [], [f"✗ 仓 {repo} 不在 {BASE} 的任何已知布局里"], []
        if nested:
            sub_cl = rdir / nested / "CHANGELOG.md"
            if sub_cl.exists():
                lines = sub_cl.read_text(encoding="utf-8").splitlines()
                for head, s, e in section_ranges(lines):
                    targets.append((sub_cl, head, "\n".join(lines[s:e]), s))
            else:
                root_cl = rdir / "CHANGELOG.md"
                if not root_cl.exists():
                    return [], [f"✗ {root_cl} 不存在"], []
                lines = root_cl.read_text(encoding="utf-8").splitlines()
                for head, s, e in section_ranges(lines):
                    if head.startswith(f"{nested}/"):
                        targets.append((root_cl, head, "\n".join(lines[s:e]), s))
        else:
            root_cl = rdir / "CHANGELOG.md"
            if not root_cl.exists():
                return [], [f"✗ {root_cl} 不存在"], []
            lines = root_cl.read_text(encoding="utf-8").splitlines()
            for head, s, e in section_ranges(lines):
                targets.append((root_cl, head, "\n".join(lines[s:e]), s))

    for _path, head, body, body_start in targets:
        fs, _found = check_section(body, repo, head, None, line_offset=body_start)
        findings.extend(fs)
        if want_perspectives:
            perspectives.extend(
                all_perspective_findings(body, repo, head, line_offset=body_start))
    return apply_allowlist(findings, entries), errors, perspectives


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.all and args.num:
        print("✗ --all（全库 dry-run）与 --num（单条）只能二选一")
        return 2
    if not args.all and not args.version:
        print("✗ 必须给 --version（单条模式）或 --all（全库 dry-run）")
        return 2
    if not args.all and not args.num:
        print("✗ 单条模式必须给 --num（要写回的本条号 N）")
        return 2
    if args.write_allowlist and not args.all:
        print("✗ --write-allowlist 只在 --all（全库 dry-run）下可用")
        return 2

    allow_path = Path(args.allowlist) if args.allowlist else DEFAULT_ALLOWLIST
    entries = load_allowlist(allow_path)

    findings: list[Finding] = []
    perspectives: list[Finding] = []
    target_found = True
    file_used = None

    if args.all:
        findings, errors, perspectives = _run_all(
            args.repo, args.nested, args.file, entries,
            want_perspectives=bool(args.write_allowlist))
        if errors:
            for e in errors:
                print(e)
            return 2
    else:
        path, body, head, body_start = resolve_changelog(
            args.repo, args.version, args.nested, args.file)
        if path is None:
            print(f"✗ 读不到 {args.repo} 的 CHANGELOG（{BASE}）——**读不到 ≠ 没有**")
            return 2
        if body is None:
            print(f"✗ {path} 里没有 `## [{args.version}]` 这一节"
                  + (f"（也没试 `## [{args.nested}/{args.version}]`）" if args.nested else ""))
            return 2
        file_used = str(path)
        raw, target_found = check_section(body, args.repo, head, args.num,
                                          line_offset=body_start)
        findings = apply_allowlist(raw, entries)

    active = [f for f in findings if not f.allowed]
    allowed = [f for f in findings if f.allowed]

    # `--require-target`：目标 bullet 不存在 ⇒ **报错退出**（fail closed）。
    # ⚠️ 这一步必须在 JSON 分支**之前**：JSON 模式也是批量流程的消费者，
    # 「目标不存在」不能只印在人类输出里。
    target_missing = (not args.all) and (not target_found)
    if target_missing and args.require_target:
        msg = (f"✗ {args.repo} {args.version}：版本节里**没有**绑定到 #{args.num} 的 bullet"
               " ⇒ --require-target：没有可检查的项 ⇒ 报错退出（fail closed）")
        if args.json:
            print(json.dumps({
                "repo": args.repo, "mode": "single", "version": args.version,
                "num": args.num, "file": file_used, "target_found": False,
                "allowlist": str(allow_path), "findings": [],
                "count": 0, "allowed_count": 0, "exit_code": 2,
                "error": "target_not_found", "message": msg,
            }, ensure_ascii=False, indent=2))
            return 2
        print(msg)
        return 2

    if args.write_allowlist:
        wp = Path(args.write_allowlist)
        existing = []
        if wp.exists():
            try:
                data = json.loads(wp.read_text(encoding="utf-8"))
                existing = data.get("entries") if isinstance(data, dict) else data
                existing = existing or []
            except json.JSONDecodeError as e:
                print(f"✗ {wp} 不是合法 JSON：{e}")
                return 2
        new = [allowlist_entry(f, args.reason) for f in perspectives]
        # 按**约束键**去重（不是按整条 JSON）：同一条目若只是 reason 写得不一样，
        # 不该在表里出现两行 ⇒ 否则 allowlist 会越滚越大、越滚越难读。
        seen: set[tuple] = set()
        for e in existing:
            seen.add(allowlist_key(e))
        added = []
        for e in new:
            k = allowlist_key(e)
            if k in seen:
                continue
            seen.add(k)
            added.append(e)
        wp.parent.mkdir(parents=True, exist_ok=True)
        wp.write_text(json.dumps({"version": 1, "entries": existing + added},
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not args.json:
            print(f"→ 已写入 allowlist：{wp}（新增 {len(added)} 条，累计 {len(existing) + len(added)} 条）")

    if args.json:
        print(json.dumps({
            "repo": args.repo,
            "mode": "all" if args.all else "single",
            "version": None if args.all else args.version,
            "num": None if args.all else args.num,
            "file": file_used,
            "target_found": target_found,
            "allowlist": str(allow_path),
            "findings": [f._asdict() for f in findings],
            "count": len(active),
            "allowed_count": len(allowed),
            "exit_code": 1 if active else 0,
        }, ensure_ascii=False, indent=2))
        return 1 if active else 0

    if not args.all and target_missing:
        print(f"⚠️ {args.repo} {args.version}：版本节里**没有**绑定到 #{args.num} 的 bullet"
              " ⇒ 本次没有可检查的项（还没落笔？落了笔再跑一次）")

    if active:
        print(f"✗ {args.repo}"
              + ("" if args.all else f" {args.version} #{args.num}")
              + f"：目标 bullet 里混进了 {len(active)} 个不该有的号")
        for i, f in enumerate(active, 1):
            print(f"  {i}) {f.category}  {f.matched}（号 {f.num}）")
            print(f"     bullet（行 {f.line}）：{f.bullet[:160]}")
        print("⇒ 修法：把别仓的号/本仓的其它号从这条 bullet 里拿掉；"
              "血缘号请**顶格**声明（别放进 bullet 的首行或续行）。")
    elif args.all:
        print(f"✓ {args.repo}：全库 dry-run 干净（0 处告警）")
    else:
        print(f"✓ {args.repo} {args.version} #{args.num}：目标 bullet 干净（没有混入别仓/本仓其它的号）")

    if allowed:
        print(f"（另有 {len(allowed)} 处已被 allowlist 豁免）")
    return 1 if active else 0


if __name__ == "__main__":
    sys.exit(main())
