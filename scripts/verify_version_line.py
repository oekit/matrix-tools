#!/usr/bin/env python3
"""发布序列倒挂门禁：**同一版本线内，后发布的 tag 版本号必须更高**。

**为什么要有这条**（2026-10-23 实测）：全矩阵 24 个模块仓里有 **4 个仓、5 个
已发布版本**的 tag 发布时刻**晚于**同一版本线上更高的版本——版本号在时间线上
**回退**了：

| 仓 | 旁支版本 | 发布时刻 | 此前已发布的更高版本 | 其发布时刻 | 间隔 |
|---|---|---|---|---|---|
| kitex | v0.3.9  | 10-15 19:32 | v0.4.1 | 10-15 16:48 | +2h43m |
| kitex | v0.3.10 | 10-16 08:39 | v0.4.1 | 10-15 16:48 | +15h50m |
| redisx | v0.3.1  | 10-16 10:20 | v0.4.0 | 10-16 10:17 | **+3m** |
| middleware | v0.5.1 | 10-17 16:52 | v0.6.0 | 10-17 16:08 | +44m |
| network | v0.1.1  | 10-17 16:53 | v0.5.1 | 10-17 16:51 | **+2m** |

这些版本**都已发布**（tag 在远端、GitHub Release 在、Go module proxy 上可解析），
因此**删不掉也挪不动**（重推 = 已缓存消费方集体中毒，.github#5）。矩阵
（versions.json / llms.txt）只登记 `latest` 与 `breakingIndex`，**不登记它们**
⇒ 对外口径与远端事实不一致：矩阵说 network 最新 v0.5.5，但 v0.1.1 也是个
可安装的已发布版本。

**现有门禁为什么放行**：共享 CI 的 `changelog_check.py` 规则 1 查的是
「**CHANGELOG 小节**按 semver 严格递减」——那是**文档序**。倒挂是**发布序**
（事实序）。两者不是一回事：network 的 CHANGELOG 小节顺序是
v0.5.5 … v0.5.0 → v0.1.1，**逐节递减 ⇒ 门禁绿**，而事实序是 v0.5.1 先发布、
v0.1.1 后发布。**实测 4/4 仓的 changelog_check 全绿**。

**判据（客观事实，不复用生成器逻辑）**：
1. 读每个仓的 `git for-each-ref --sort=creatordate refs/tags`（tag 的**发布
   时刻**按 creatordate，即 annotated tag 的 tagger 时间）；
2. 按「版本线」分开：根模块 tag 走一条线，嵌套模块 tag（`<子目录>/vX.Y.Z`）
   各走一条线——嵌套模块有独立版本节奏，不能混在一条线上比；
3. 同一版本线内按发布顺序扫，**某 tag 的版本号低于此前已发布的最高版本**
   ⇒ 倒挂（regression）。

**为什么判据不复用任何现有解析逻辑**：共享逻辑就会有共同盲区——解析器漏收时
两边一起漏、门禁全绿而事实仍在（这正是 matrix-tools#31 复审定下的原则）。
本脚本自己解析 semver、自己读 git 原始输出。

**已登记的例外**（`scripts/version_line_known.json`）：现存 5 处倒挂**撤不掉**
（tag 已发布），登记为「已知旁支」并写明事实与处置；**新增任何一处倒挂 ⇒
报红**。例外表是显式的人工放行口，不是漏报的口子——每条都要写理由。

用法：
  python3 scripts/verify_version_line.py --base <工作区> [--known <例外表.json>]
  python3 scripts/verify_version_line.py --selftest

退出码：0 = 无未登记的倒挂；1 = 有未登记的倒挂 / 有仓的版本事实读不到（判不了）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from matrix import MODULES, ORG, repo_dir  # noqa: E402

# 严格的 vX.Y.Z（不含预发布/构建元数据——那些另有语义，不参与本门禁）
SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# 伞仓 BOM 仓与本仓（matrix-tools）不在 MODULES 里，但同样有版本线
EXTRA_LINE_REPOS = ["oekit", "matrix-tools"]

KNOWN_PATH = HERE / "version_line_known.json"


def _parse_tag(tag: str) -> tuple[str, str] | None:
    """tag 名 → (版本线, 版本号文本)；不是 semver 则返回 None。

    版本线：根模块 tag = ""；嵌套模块 tag = "<子目录>"（如 logx/sink 的 "sink"）。
    """
    if "/" in tag:
        line, name = tag.rsplit("/", 1)
        # 嵌套模块只认单层目录（多层的 tag 不在本仓约定内）
        if "/" in line:
            return None
    else:
        line, name = "", tag
    if not SEMVER_RE.match(name):
        return None
    return line, name


def _semver(name: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(name)
    assert m, name
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def git_tag_events(repo_dir: Path) -> list[tuple[str, str, tuple[int, int, int], str]] | None:
    """读该仓全部 tag 的发布时刻 → [(tag, 版本线, 版本号, 发布时刻)]。

    **返回 None = 读不到**（不是「没有 tag」）——调用方必须把两者分开：
    读不到 ⇒ 判不了（不能算通过）；读到但为空 ⇒ 该仓确实一个 tag 都没有。
    """
    if not (repo_dir / ".git").exists():
        return None
    r = subprocess.run(
        ["git", "-C", str(repo_dir), "for-each-ref", "--sort=creatordate",
         "--format=%(refname:short)|%(creatordate:iso8601)", "refs/tags"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None
    out: list[tuple[str, str, tuple[int, int, int], str]] = []
    for raw in r.stdout.splitlines():
        if "|" not in raw:
            continue
        tag, date = raw.split("|", 1)
        parsed = _parse_tag(tag.strip())
        if parsed is None:
            continue
        line, name = parsed
        out.append((tag.strip(), line, _semver(name), date.strip()))
    return out


def find_regressions(events: list[tuple[str, str, tuple[int, int, int], str]]) -> list[dict]:
    """同一版本线内，后发布却版本更低的 tag。

    events 已按发布时刻升序（git for-each-ref --sort=creatordate）。
    """
    out: list[dict] = []
    best: dict[str, tuple[tuple[int, int, int], str, str]] = {}   # 版本线 → (版本号, tag, 时刻)
    for tag, line, ver, date in events:
        prev = best.get(line)
        if prev is not None and ver < prev[0]:
            out.append({
                "tag": tag,
                "line": line,
                "version": ver,
                "date": date,
                "higher_tag": prev[1],
                "higher_version": prev[0],
                "higher_date": prev[2],
            })
        elif prev is None or ver > prev[0]:
            best[line] = (ver, tag, date)
    return out


def scan_repo(repo: str, base: Path) -> tuple[list[dict], str | None]:
    """→ (倒挂清单, 读不到的原因)；读不到时倒挂清单恒为空。"""
    try:
        d = repo_dir(repo, str(base))
    except FileNotFoundError:
        return [], f"仓不在工作区（{base}）"
    events = git_tag_events(d)
    if events is None:
        return [], f"{repo}: 读不到 tag（git for-each-ref 失败或不是 git 仓）"
    if not events:
        return [], f"{repo}: 一个 tag 都没有——版本事实读不到，不能算通过"
    return find_regressions(events), None


def load_known(path: Path | None = None) -> dict[tuple[str, str], dict]:
    """已登记的旁支版本 → {(仓, tag): 记录}。"""
    p = path or KNOWN_PATH
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], dict] = {}
    for item in data.get("known", []):
        out[(item["repo"], item["tag"])] = item
    return out


def scan(base: Path, repos: list[str]) -> dict:
    """扫全矩阵 → {known, unknown, unreadable, repos_scanned}。"""
    known_map = load_known()
    known: list[dict] = []
    unknown: list[dict] = []
    unreadable: list[str] = []
    scanned = 0
    for repo in repos:
        try:
            repo_dir(repo, str(base))
        except FileNotFoundError:
            continue                     # 不在本工作区（如 CI 布局里没有 matrix-tools）→ 不适用
        scanned += 1
        regs, err = scan_repo(repo, base)
        if err:
            unreadable.append(err)
            continue
        for r in regs:
            r["repo"] = repo
            rec = known_map.get((repo, r["tag"]))
            if rec:
                r["reason"] = rec.get("reason", "")
                known.append(r)
            else:
                unknown.append(r)
    return {"known": known, "unknown": unknown, "unreadable": unreadable,
            "repos_scanned": scanned}


def _fmt(v: tuple[int, int, int]) -> str:
    return "v" + ".".join(str(x) for x in v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("MATRIX_BASE"))
    ap.add_argument("--known", default=None, help="已登记旁支版本表（默认 scripts/version_line_known.json）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        from verify_version_line_selftest import selftest  # 延迟导入，避免循环
        return selftest()

    base = Path(args.base or HERE.parent.parent).resolve()
    repos = list(MODULES) + [r for r in EXTRA_LINE_REPOS if r not in MODULES]
    known_path = Path(args.known) if args.known else None
    if known_path:
        global KNOWN_PATH
        KNOWN_PATH = known_path

    res = scan(base, repos)

    for item in res["known"]:
        print(f"  ⚠ 已知旁支版本（已登记，tag 已发布撤不掉）：{item['repo']} {item['tag']}"
              f"（{_fmt(item['version'])}，发布于 {item['date']}，晚于已发布的 "
              f"{item['higher_tag']}）——{item.get('reason', '')}")

    if res["unreadable"]:
        print(f"  ❌ 有 {len(res['unreadable'])} 个仓的版本事实读不到（判不了，不能当成通过）：")
        for e in res["unreadable"]:
            print(f"     {e}")

    if res["unknown"]:
        print(f"❌ 有 {len(res['unknown'])} 处未登记的发布序列倒挂"
              f"（同一版本线内，后发布的 tag 版本号反而更低）：")
        for item in res["unknown"]:
            line = f"/{item['line']}" if item["line"] else ""
            print(f"   {item['repo']} {item['tag']}{line}（{_fmt(item['version'])}，发布于 {item['date']}）"
                  f" 晚于已发布的 {item['higher_tag']}（{_fmt(item['higher_version'])}，{item['higher_date']}）")
        print("\n修法：新版本号必须高于该仓已发布的最高版本；若该版本已发布（撤不掉），"
              "把它登记进 scripts/version_line_known.json 并写明事实与处置。")
        return 1

    if res["unreadable"]:
        return 1

    print(f"✅ 发布序列无倒挂（扫了 {res['repos_scanned']} 个仓；"
          f"已登记旁支 {len(res['known'])} 处，见 scripts/version_line_known.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
