#!/usr/bin/env python3
"""已发布 GitHub Release **正文**重渲染（#213）。

为什么要有这个工具
------------------
回执侧有 `rerender_receipt.py`（预览 / `--apply` / 只编辑原评论 / 幂等跳过），
**已发布的 Release 正文**侧却**没有**对应工具 ⇒ 读路径一改（#214：嵌套 tag 改读
根面），**已发布的 Release 正文就陈旧了**，却只能手工逐个 `gh release edit`。

实测（2026-11-02 那一波 8 条嵌套 tag）：嵌套 tag 的 Release 正文由
`gen_releases.build_notes()` 渲染 ⇒ 它读 `changelog_section()` ⇒ 旧读法
（先读嵌套文件）⇒ **6 个嵌套 tag 的 Release 正文里「变更记录」只有一句发布关系
说明**（内容为空）⇒ 读者在 Release 页面读不到这一版改了什么，而同一版的根
CHANGELOG 节里写着完整描述。

用法
----
  python3 scripts/rerender_release_body.py <repo> <tag> [...] [--apply] [--force]
                                                   [--backup-dir DIR]

- 默认**只预览**（打印「当前正文 vs 新正文」的差异）；要真改加 `--apply`
  （改的是**已发布产物** ⇒ 先看清预览再动手）。
- `--force`：正文已一致也重写（默认**幂等跳过**）。
- 可一次给多个 `<repo> <tag>`（批量重渲染）。

fail closed
-----------
**tag 不存在 / Release 不存在 / 读不到版本节 ⇒ 报错退出，不写**。

为什么：写空正文会把「读者读不到改了什么」升级成「读者读不到任何东西」——
比陈旧更糟。且已发布产物不可静默改写 ⇒ 三种「判不了」都必须显式报出。

正文来源
--------
**`gen_releases.build_notes()`**（与发版时**同一份代码**）⇒ 不另造一套渲染口径，
避免「工具渲染的」与「发版时渲染的」分叉。

⚠️ `build_notes` 读的是**本地工作树**（与发版时同源）⇒ 操作前必须把本地
checkout 拉到含写回的那次提交，否则渲染出来的是改前的节。

幂等
----
正文已一致 ⇒ 明确报「无需改动」并跳过（**不许**重复改写已发布内容）。

⚠️ 判据是**正文逐字节相等**，不是「有没有某句话」——弱特征判据会把「正文恰好
包含那句话但整体不同」误判成已一致（#154 的教训）。

只对**明确点名的 tag** 生效
--------------------------
**不允许**「全仓所有 Release 重刷」这种默认动作 ⇒ 必须逐个点名。
"""
from __future__ import annotations

import difflib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from matrix import ORG, repo_dir  # noqa: E402
import ghsafe  # noqa: E402
from gen_releases import build_notes  # noqa: E402

# 动作痕迹标记（幂等的辅助证据；**不作为唯一判据**——见模块文档）
RERENDER_MARK = "<!-- rerendered-by matrix-tools -->"
MARK_LINE = "\n\n" + RERENDER_MARK + "\n"


def release_exists(repo: str, tag: str) -> bool:
    """该 tag 是否**有** Release（区分「没有」与「读不到」）。

    ⚠️ 404（没有这个 Release）是**确定性失败** ⇒ `GhCommandError`；
    其它失败形态（限速/5xx）由 `ghsafe` 内部重试 ⇒ 仍失败则**抛**（判不了）。
    """
    try:
        ghsafe.gh_json(["api", f"repos/{ORG}/{repo}/releases/tags/{tag}"])
        return True
    except ghsafe.GhCommandError as e:
        if "404" in (f"{e.stderr}\n{e.stdout}") or "Not Found" in (f"{e.stderr}\n{e.stdout}"):
            return False
        raise
    except Exception:  # noqa: BLE001 —— 读不到 ≠ 没有 ⇒ 不当「没有」
        raise


def release_body(repo: str, tag: str) -> str:
    """读该 Release 的当前正文（读不到 ⇒ 抛错，不当空）。"""
    data = ghsafe.gh_json(["api", f"repos/{ORG}/{repo}/releases/tags/{tag}"])
    if not isinstance(data, dict):
        raise RuntimeError(f"{repo} {tag}: 读到 Release 但不是对象 ⇒ 判不了")
    body = data.get("body")
    if body is None:
        raise RuntimeError(f"{repo} {tag}: Release 无 body 字段 ⇒ 判不了")
    return body


def tag_exists(repo: str, tag: str) -> bool:
    """该 tag 是否存在（远端）。

    ⚠️ `ghsafe.run_gh` 返回的是 **(stdout, stderr)**，**不是** (rc, out) ⇒
    判据用「异常」而非返回码（404 ⇒ `GhCommandError`）。
    """
    try:
        ghsafe.run_gh(["api", f"repos/{ORG}/{repo}/git/ref/tags/{tag}"], tries=2)
        return True
    except ghsafe.GhCommandError:
        return False


def edit_release_body(repo: str, tag: str, body: str) -> None:
    """把 Release 正文**整条替换**（只改正文，不动 tag / 标题 / latest 标记）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as f:
        f.write(body)
        path = f.name
    try:
        ghsafe.run_gh(["release", "edit", tag, "--repo", f"{ORG}/{repo}",
                       "--notes-file", path])
    finally:
        os.unlink(path)


def backup_body(backup_dir: Path, repo: str, tag: str, body: str) -> Path:
    """旧正文落盘（可回滚）——改已发布产物必须有后路。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe = tag.replace("/", "_")
    p = backup_dir / f"{repo}-{safe}.md"
    p.write_text(body, encoding="utf-8")
    return p


def diff_summary(old: str, new: str) -> str:
    """「当前 vs 新」的差异摘要（逐行）。"""
    d = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="当前正文", tofile="新正文", lineterm="", n=1))
    if not d:
        return "（无差异）"
    return "\n".join(d[:60])


# ── 三个判据（**独立成函数**：便于自测逐个变异，不埋在流程里）────────────────

def body_is_renderable(new: str) -> bool:
    """渲染出的正文**是否可用**（读到了版本节）。

    ⚠️ 读不到版本节 ⇒ `build_notes` 给出的是**缺信息的正文** ⇒ 写出去等于
    「读者连『有改了什么』都看不到」⇒ 比陈旧更糟 ⇒ 判为不可用。
    """
    return "## 变更记录" in new


def body_is_same(old: str, new: str) -> bool:
    """新旧正文**是否已一致**（幂等判据）。

    ⚠️ 判据是**逐字节相等**（去首尾空白），**不是**「有没有某句话」——
    弱特征判据会把「正文恰好包含那句话但整体不同」误判成已一致（#154 的教训）。
    """
    return old.strip() == new.strip()


def should_write(apply: bool) -> bool:
    """是否**真的写**（默认只预览）。

    ⚠️ 改已发布产物**不可静默** ⇒ 写入必须由调用方显式 `--apply` 授权。
    """
    return bool(apply)


def rerender(repo: str, tag: str, *, apply: bool = False, force: bool = False,
             backup_dir: Path | None = None) -> tuple[bool, str]:
    """重渲染一个 Release 的正文。返回 (成功?, 给人看的说明)。"""
    if not tag_exists(repo, tag):
        return False, f"{repo} {tag}: tag 不存在 ⇒ 不写"
    if not release_exists(repo, tag):
        return False, f"{repo} {tag}: 没有 Release ⇒ 不写"

    old = release_body(repo, tag)

    d = repo_dir(repo)
    if not d or not os.path.isdir(str(d)):
        return False, f"{repo} {tag}: 本地没有该仓的 clone ⇒ 判不了 ⇒ 不写"

    # 渲染正文（与发版时同一份代码）。
    new = build_notes(str(d), tag, repo)
    if not body_is_renderable(new):
        return False, (f"{repo} {tag}: 渲染出的正文没有「变更记录」段"
                       f"（读不到版本节？）⇒ 判不了 ⇒ 不写")

    if body_is_same(old, new) and not force:
        return True, f"{repo} {tag}: 正文已一致 ⇒ 无需改动（--force 可重写）"

    if not should_write(apply):
        return True, (f"{repo} {tag}: 预览（未改动）——正文差异：\n"
                      f"{diff_summary(old, new)}\n"
                      f"（新正文全文）\n{new}")

    bk = backup_body(backup_dir, repo, tag, old) if backup_dir else None
    edit_release_body(repo, tag, new)
    msg = f"{repo} {tag}: 已重渲染 Release 正文"
    if bk:
        msg += f"；旧正文备份：{bk}"
    return True, msg


def main() -> int:
    args = sys.argv[1:]
    apply = "--apply" in args
    force = "--force" in args

    # ★ **参数解析**：先把「带值的选项」整体摘掉，剩下的才是 <repo> <tag> 对。
    #   否则 `--backup-dir DIR` 的 DIR 会被当成 repo ⇒ 整批打错地方。
    rest: list[str] = []
    i = 0
    backup_dir: Path | None = None
    while i < len(args):
        a = args[i]
        if a == "--backup-dir":
            if i + 1 >= len(args):
                print("--backup-dir 需要一个目录参数")
                return 2
            backup_dir = Path(args[i + 1])
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        rest.append(a)
        i += 1

    if not rest or len(rest) % 2 != 0:
        print("用法：python3 scripts/rerender_release_body.py <repo> <tag> [...] "
              "[--apply] [--force] [--backup-dir DIR]")
        return 2

    pairs = list(zip(rest[0::2], rest[1::2]))
    failed = 0
    for repo, tag in pairs:
        try:
            ok, msg = rerender(repo, tag, apply=apply, force=force,
                               backup_dir=backup_dir)
        except Exception as e:  # noqa: BLE001 —— 一条失败不许带崩整批
            print(f"✗ {repo} {tag}: {e}")
            failed += 1
            continue
        print(("✓ " if ok else "✗ ") + msg)
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
