#!/usr/bin/env python3
"""历史回执「变更内容」重渲染（#163）。

为什么要有这个工具
------------------
#153 给回执加了「按 issue 号在版本节里定位本条修复」，但**只对今后新发的回执生效**。
#153 之前发的那批（pre-#153 形态）把**整段版本节**当「变更内容」⇒ 版本节捆绑
多条修复时，读者分不清哪一条是自己的（不精确 ≠ 说错，见 #158 的更正）。

本工具把一条**已有**回执的「变更内容」段按 #153 的定位器**重新渲染**，并
**编辑原评论**（`PATCH /repos/{o}/{r}/issues/comments/{id}`）——
**不新发评论、不关单、不改 issue 状态**。

用法
----
  python3 scripts/rerender_receipt.py <repo>#<N> [...] [--tag vX.Y.Z]
                                      [--apply] [--force] [--backup-dir DIR]

- 默认**只预览**（不动任何东西）；要真改加 `--apply`
  （改的是**已发布**的评论 ⇒ 先看清预览再动手）。
- `--tag`：一条 issue 上可能有多条回执 ⇒ 用它指明要重渲染哪一条。
- `--force`：已重渲染过（含动作痕迹标记）也重渲。
- 可一次给多个 `<repo>#<N>`（批量重渲染）。

为什么默认只预览
----------------
编辑**已发布**的评论是**不可静默**的操作：读者看到的旧内容会被替换掉。
⇒ 默认预览 + 显式 `--apply`，并把旧正文**备份**落盘（可回滚）。

幂等
----
**已重渲染过**的回执 ⇒ 直接跳过（不重复改写）⇒ **重跑安全（两个分支都安全）**。

⚠️ 判据是**动作痕迹**（正文末尾的 `<!-- rerendered-by matrix-tools -->` 标记，
GitHub 不渲染、读者看不见），**不是内容形态**：

- 旧判据是「正文含 `**本条修复**`」⇒ 它**与「定位结果」耦合**：
  **定位不到**时产物压根不含这四个字 ⇒ 重跑不跳过 ⇒ 同一评论被**重复 PATCH**
  （内容相同，但批量重跑会重复写、还会连带覆盖备份）；
- 且旧判据是**子串**匹配 ⇒ 旧回执**引用的版本节正文里**出现这四个字时
  ⇒ 被误判成「已重渲染」⇒ **静默跳过** ⇒ **该改的没改**。

⇒ 两个方向的坑在同一处判据上 ⇒ 换成与「定位结果」无关的动作痕迹（#170）。
⚠️ 也不许拿「有没有人提过这个版本号」这种弱特征当判据（#154 的教训）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from matrix import ORG, repo_dir  # noqa: E402
import ghsafe  # noqa: E402
from close_issue import build_change_content, release_mentions_issue  # noqa: E402
from gen_releases import changelog_section  # noqa: E402

# 回执首行：`已在 \`<tag>\` 修复并发布 ✅`（与 close_issue.py 写的形态一致）
RECEIPT_HEAD_RE = re.compile(r"^\s*已在\s*`([^`]+)`\s*修复并发布\s*✅")
CHANGE_HEADING = "## 变更内容"

# **动作痕迹标记**（幂等判据的唯一依据）：重渲染成功后写在正文末尾。
# GitHub 不渲染 HTML 注释 ⇒ 读者看不见；判据**不依赖内容形态** ⇒
# 「定位得到」与「定位不到」两个分支**都幂等**（#170）。
RERENDER_MARK = "<!-- rerendered-by matrix-tools -->"
MARK_LINE_RE = re.compile(r"(?m)^[ \t]*<!--\s*rerendered-by matrix-tools\s*-->[ \t]*$")


def already_rendered(body: str) -> bool:
    """这条回执**是否已被本工具重渲染过**。

    判据只认**动作痕迹**（独立的「已重渲染」标记），**不认内容形态**：

    - 旧判据是「正文含 `**本条修复**`」⇒ 它**与「定位结果」耦合**：
      **定位不到**时产物压根不含这四个字 ⇒ 重跑不跳过 ⇒ 同一评论被**重复 PATCH**
      （内容相同，但批量重跑会重复写、还会连带覆盖备份）；
    - 而且旧判据是**子串**匹配 ⇒ 旧回执**引用的版本节正文里**出现这四个字时
      ⇒ 被误判成「已重渲染」⇒ **静默跳过** ⇒ **该改的没改**（方向相反的坑）。
    ⇒ 两个方向的坑都在同一处判据上 ⇒ 换成动作痕迹。
    """
    return bool(MARK_LINE_RE.search(body))


def _strip_marks(text: str) -> str:
    """剥掉已有的动作痕迹标记行（重渲时先剥 ⇒ 标记永远恰好一份）。"""
    return MARK_LINE_RE.sub("", text)


def receipt_tag(body: str) -> str | None:
    """这条评论是不是**回执形态**？是 ⇒ 返回它引用的 tag，否则 None。

    只认**首行**的完整形态——issue 正文里引用旧文案、补充说明里提到版本号，
    都不算回执（#163 的复现口径：按「最后一条评论」扫会假红）。
    """
    first = next((l for l in body.splitlines() if l.strip()), "")
    m = RECEIPT_HEAD_RE.match(first.strip())
    return m.group(1) if m else None


def split_receipt(body: str, repo: str, tag: str) -> tuple[str, str, str]:
    """把回执正文切成 (前导, 变更内容段, 尾部)。

    尾部锚点优先用**本仓本 tag 的 Release 行**（回执结构里唯一的确定锚点）；
    找不到 ⇒ 退化到「下一个顶层标题 / `Release:` / `**合入**` / `**CI**` 行」；
    再找不到 ⇒ 变更内容段取到文末。
    """
    lines = body.split("\n")
    idx = next((i for i, l in enumerate(lines) if l.strip() == CHANGE_HEADING), None)
    if idx is None:
        return body.rstrip("\n"), "", ""
    head = "\n".join(lines[:idx]).rstrip("\n")
    marker = f"Release: https://github.com/{ORG}/{repo}/releases/tag/{tag}"
    end: int | None = None
    for j in range(idx + 1, len(lines)):
        if marker and marker in lines[j]:
            end = j
            break
    if end is None:
        for j in range(idx + 1, len(lines)):
            l = lines[j]
            if (l.startswith("## ") or l.startswith("Release: ")
                    or l.startswith("**合入**") or l.startswith("**CI**")):
                end = j
                break
    if end is None:
        end = len(lines)
    mid = "\n".join(lines[idx:end]).rstrip("\n")
    tail = "\n".join(lines[end:]).rstrip("\n")
    return head, mid, tail


def rebuild_body(body: str, sec: str | None, repo: str, num: str, tag: str,
                 evidence: bool | None) -> str:
    """用 #153 的渲染器重写「变更内容」段，**其余部分原样保留**。

    其余部分 = 首行 / Release 行 / 合入与 CI 证据行 / 附注 —— 一律不动。

    末尾追加**动作痕迹标记**（幂等判据的唯一依据，#170）⇒ 标记恰好一份
    （重渲时先剥掉旧的）。
    """
    head, _mid, tail = split_receipt(body, repo, tag)
    new_mid = CHANGE_HEADING + "\n\n" + build_change_content(sec, repo, num, tag, evidence)
    parts = [head, new_mid]
    if tail:
        parts.append(tail)
    out = _strip_marks("\n\n".join(parts)).rstrip("\n")
    return out + "\n\n" + RERENDER_MARK + "\n"


def list_comments(repo: str, num: str) -> list[dict]:
    """读该 issue 的全部评论。

    用 **REST**（要 comment id 才能 PATCH）；空集合按「读不到」重试几轮
    （ghsafe 的口径：读到空 ≠ 事实为空），仍为空才算真的没有评论。
    """
    def read() -> list[dict]:
        data = ghsafe.gh_json(
            ["api", f"repos/{ORG}/{repo}/issues/{num}/comments?per_page=100"],
            allow_empty_collection=True)
        return data if isinstance(data, list) else []

    data = ghsafe.read_until_nonempty(read) or []
    return [c for c in data if isinstance(c, dict)]


def patch_comment(repo: str, cid: int, body: str) -> None:
    """把评论正文**整条替换**（PATCH）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump({"body": body}, f, ensure_ascii=False)
        path = f.name
    try:
        ghsafe.run_gh(["api", "--method", "PATCH",
                       f"repos/{ORG}/{repo}/issues/comments/{cid}",
                       "--input", path])
    finally:
        os.unlink(path)


def backup_body(backup_dir: Path, repo: str, num: str, cid: int, body: str) -> Path:
    """旧正文落盘（可回滚）——改已发布的评论必须有后路。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    p = backup_dir / f"{repo}#{num}-{cid}.md"
    p.write_text(body, encoding="utf-8")
    return p


def rerender(repo: str, num: str, tag: str | None = None, *, apply: bool = False,
             force: bool = False, backup_dir: Path | None = None) -> tuple[bool, str]:
    """重渲染一条历史回执。返回 (成功?, 给人看的说明)。"""
    comments = list_comments(repo, num)
    receipts = [(c, receipt_tag(c.get("body") or "")) for c in comments]
    receipts = [(c, t) for c, t in receipts if t]
    if tag:
        receipts = [(c, t) for c, t in receipts if t == tag]
    if not receipts:
        return False, (f"{repo}#{num}: 没找到回执形态的评论"
                       f"（首行形如「已在 `<tag>` 修复并发布 ✅」）")
    if len(receipts) > 1:
        cands = ", ".join(sorted({t for _c, t in receipts}))
        return False, (f"{repo}#{num}: 有多条回执（{cands}）⇒ 用 --tag 指明要重渲染哪一条")
    comment, rtag = receipts[0]
    cid = int(comment["id"])
    body = comment.get("body") or ""

    if already_rendered(body) and not force:
        return True, (f"{repo}#{num}: 回执已重渲染过（含动作痕迹标记）"
                      f" ⇒ 跳过（--force 可重渲）")

    nested = rtag.rsplit("/", 1)[0] if "/" in rtag else None
    version = rtag.rsplit("/", 1)[-1]
    # 版本节读**本地工作树**（与 close_issue.py 同源）⇒ 操作前必须把本地
    # checkout 拉到含写回的那次提交，否则渲染出来的是改前的节。
    sec = changelog_section(str(repo_dir(repo)), version, nested)
    evidence = release_mentions_issue(repo, rtag, num)
    new_body = rebuild_body(body, sec, repo, num, rtag, evidence)

    if not apply:
        return True, (f"{repo}#{num}: 预览（未改动）——{rtag} 的回执将重渲染为：\n"
                      f"{new_body}")

    bdir = backup_dir or (Path(tempfile.gettempdir()) / "rerender-backup")
    bpath = backup_body(bdir, repo, num, cid, body)
    patch_comment(repo, cid, new_body)

    # 复核：**回读**评论，正文必须已经是新的（不信命令返回成功）。
    after = [c for c in list_comments(repo, num) if int(c.get("id", -1)) == cid]
    if not after:
        return False, f"{repo}#{num}: 回读不到刚编辑的评论 {cid} ⇒ 未确认改动"
    if (after[0].get("body") or "") != new_body:
        return False, (f"{repo}#{num}: 回读到的正文与预期不一致 ⇒ 未确认改动"
                       f"（旧正文已备份在 {bpath}）")
    return True, (f"{repo}#{num}: 已重渲染回执 {cid}（{rtag}）"
                  f"；旧正文备份：{bpath}")


def main() -> None:
    args = sys.argv[1:]
    apply = "--apply" in args
    force = "--force" in args
    tag = args[args.index("--tag") + 1] if "--tag" in args and args.index("--tag") + 1 < len(args) else None
    backup_dir: Path | None = None
    if "--backup-dir" in args and args.index("--backup-dir") + 1 < len(args):
        backup_dir = Path(args[args.index("--backup-dir") + 1])

    refs = [a for a in args if "#" in a and not a.startswith("--")]
    if not refs:
        print(__doc__)
        raise SystemExit(2)

    failed = 0
    for ref in refs:
        m = re.fullmatch(r"([a-z0-9.-]+)#(\d+)", ref)
        if not m:
            print(f"✗ issue 引用格式应为 <repo>#<N>：{ref!r}")
            failed += 1
            continue
        ok, msg = rerender(m.group(1), m.group(2), tag, apply=apply, force=force,
                            backup_dir=backup_dir)
        print(("✓ " if ok else "✗ ") + msg)
        if not ok:
            failed += 1
    if failed:
        raise SystemExit(failed)


if __name__ == "__main__":
    main()
