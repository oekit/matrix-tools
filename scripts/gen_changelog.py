#!/usr/bin/env python3
"""从 tag 自动生成各仓 CHANGELOG.md。

用法：python3 scripts/gen_changelog.py [--apply] [--push]

**默认只报告**（不写任何文件）；`--apply` 才真写；`--push` 还要求 `--apply`。

## 为什么加这个闸门（2026-10-30 实测）

旧版**没有闸门**：跑一次就把全矩阵 24 个仓的 CHANGELOG.md **整文件覆写**，
而且覆写出来的是「每个 tag 的 subject 一行」⇒ **release_train 写的详实条目
被替换掉**。实测抽样：

- `pkg/CHANGELOG.md` 现在**没有抬头**，第一条就是 `## [v0.31.0] — 2026-10-29`，
  下面是一条讲清「CLI 别名为什么这么设计」的长条目（release_train 写的）；
- `logx/CHANGELOG.md` 有 ekit v0.34.0 的来源抬头；
- `bootstrap/CHANGELOG.md` 是第三种抬头（Keep a Changelog 口径）。

⇒ 三个仓三种格式，旧版一跑就把它们**统一成第四种**（还给 pkg 补上一个它没有的
抬头），内容还会被 tag subject 覆盖。实测损失：hertz 的 CHANGELOG 会从
10699 字符掉到 1459 字符（**丢 87%**）。**这是会毁掉全矩阵 CHANGELOG 的工具**，
却没有闸门、没有提示、退出码 0。

现在跟矩阵既有约定对齐（align_floors / doc_wave / pr_flow 都是「默认只报告，
`--apply` 才落地」）：默认只打印**哪些仓会被改、丢多少内容**。

## 另外修掉的一个真 bug

旧版第 3 行是 `pathlib.Path(__file__).rsplit("/", 2)[0]` —— **pathlib.Path 没有
rsplit**（那是 str 的方法）⇒ 一执行就 AttributeError ⇒ **这个脚本从来没跑起来过**，
而 README 一直把它列在「可用工具」里。其余脚本用的都是 `str(__file__).rsplit`。

## 还要注意

- `--push` 直推 main：main 受分支保护（enforce_admins）⇒ **推不上去**。
  本工具不改这个约定，只是把失败说清楚；改各仓 CHANGELOG 请走 PR。
"""
import os
import pathlib
import sys

# ⚠️ 旧写法是 `pathlib.Path(__file__).rsplit(...)` —— pathlib.Path 没有 rsplit
# ⇒ 一执行就 AttributeError ⇒ 这个脚本从来没跑起来过（见上文「另外修掉的一个真 bug」）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from matrix import MODULES, repo_dir, ensure_clean_all, sh  # noqa: E402

BASE = os.path.abspath(os.environ.get(
    "MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))


def changelog_write_allowed(argv: list[str]) -> tuple[bool, bool]:
    """(apply, push)——**判据提成纯函数是为了能自测**。

    `--push` 不带 `--apply` ⇒ 直接拒绝（推的前提是先写，而写的前提是显式
    `--apply`；旧版没有这层，`--push` 直接把覆写后的文件推上 main）。
    """
    apply_ = "--apply" in argv
    push = "--push" in argv
    if push and not apply_:
        raise SystemExit(
            "gen_changelog 会**整文件覆写**各仓 CHANGELOG.md（并用 tag subject 覆盖\n"
            "  release_train 写的详实条目）⇒ 必须显式 --apply 才写。\n"
            "  只看会改什么：python3 scripts/gen_changelog.py\n"
            "  真写：      python3 scripts/gen_changelog.py --apply\n"
            "  另：--push 直推 main 会被分支保护拒（enforce_admins）⇒ 改 CHANGELOG 请走 PR。")
    return apply_, push


def render(repo: str) -> str:
    """该仓 CHANGELOG 的生成内容（纯函数，不落盘）。"""
    d = repo_dir(repo, BASE)
    tags = sh(["git", "-C", str(d), "tag", "-l", "--sort=v:refname"], d).stdout.split()
    lines = [f"# Changelog — oekit/{repo}\n",
             "由 ekit v0.34.0（git.enjoye.top/enjoydream/ekit，冻结维护）拆分而来。所有 notable 变更记录如下。\n"]
    for tag in reversed(tags):
        msg = sh(["git", "-C", str(d), "tag", "-l", tag, "--format=%(contents)"], d).stdout.strip().splitlines()
        subject = next((l for l in msg if l and not l.startswith("v0.")), tag)
        date = sh(["git", "-C", str(d), "log", "-1", "--format=%ad", "--date=short", tag], d).stdout.strip()
        lines.append(f"\n## [{tag}] — {date}\n\n- {subject}")
    return "\n".join(lines) + "\n"


def main() -> None:
    apply_, push = changelog_write_allowed(sys.argv)

    # 产物必须落在已提交基线上（生成器产物不许压在工作树改动上）
    ensure_clean_all([repo_dir(r, BASE) for r in MODULES], "gen_changelog")

    changed: list[str] = []
    for repo in MODULES:
        d = repo_dir(repo, BASE)
        path = d / "CHANGELOG.md"
        content = render(repo)
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if old == content:
            continue
        changed.append(repo)
        old_lines = len(old.splitlines())
        new_lines = len(content.splitlines())
        # 「丢内容」的判据用**字符数**（详实条目是长句，行数少不代表没丢）
        # ——不能用行数比：metrics 现 27 行 → 生成 32 行（变长了）却按行数比不出来。
        loss = ""
        if old and len(old) > len(content):
            loss = f"，⚠️ 会**丢内容**（{len(old)} → {len(content)} 字符）"
        if apply_:
            path.write_text(content, encoding="utf-8")
            print(f"✓ {repo}: {new_lines} 版本（已覆写）")
        else:
            print(f"  {repo}: 会改（现 {old_lines} 行 → 生成 {new_lines} 行{loss}）")

    if not changed:
        print("✓ 全矩阵 CHANGELOG 与生成结果一致（无需改动）")
    elif not apply_:
        print(f"\n{len(changed)} 仓会被覆写：{', '.join(changed)}")
        print("（只报告模式；确认后加 --apply 真写）")

    if push:
        for repo in changed:
            d = repo_dir(repo, BASE)
            if sh(["git", "status", "--porcelain", "CHANGELOG.md"], d).stdout.strip():
                sh(["git", "add", "CHANGELOG.md"], d)
                sh(["git", "commit", "-q", "-m", "docs: CHANGELOG 更新（matrix-tools）"], d)
                sh(["git", "push", "-q"], d)
                print(f"⬆ {repo}")


if __name__ == "__main__":
    main()
