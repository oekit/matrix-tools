#!/usr/bin/env python3
"""发布协同工具：主模块 + 嵌套模块同 train 按序打 tag（matrix-tools#6，承接 bootstrap#2）。

背景：bootstrap v0.6.0/nacos v0.3.0/obs v0.2.x 那轮全靠手工逐个打，易漏易错序；
bootstrap v0.5.1 还曾因地板指向已删 tag + 同名 tag 重推双重事故（.github#5）。
本工具把「查重 → tidy → build → CHANGELOG 核对 → 按序打 tag → 推送」收成一条命令。

用法（缺省 dry-run 只跑检查，--apply 才打 tag 并推送）：
  python3 scripts/release_train.py bootstrap main=v0.6.2 nacos=v0.3.2 obs=v0.2.7
  python3 scripts/release_train.py bootstrap main=v0.6.2 nacos=v0.3.2 --apply

规格说明：
  - main=<vX.Y.Z>   根模块，tag 名即版本本身
  - <sub>=<vX.Y.Z>  嵌套模块（子目录含 go.mod），tag 名 <sub>/<vX.Y.Z>
  - 模块顺序即打 tag 顺序（主模块在前）；全部模块的检查都通过才开始打 tag，
    任一检查失败即中止且不产生任何 tag（原子性靠先查后做保证）
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
sys.path.insert(0, str(__file__).rsplit("/", 1)[0])
from matrix import MODULES, git_branch, ensure_clean_all, repo_dir, sh  # noqa: E402
from inflight import read as inflight_read  # noqa: E402


def preflight_gate(repo_root, apply: bool) -> None:
    """发版现场门禁：工作树必须干净（tag 内容 = 已提交基线，2026-10-19 v0.1.2 事故）；
    --apply 还要求在 main 上（tag 打在合入 main 之后）且本仓未被并行会话登记占用。"""
    ensure_clean_all([repo_root], "release_train")
    if not apply:
        return
    if git_branch(repo_root) != "main":
        raise SystemExit(f"{repo_root.name}: --apply 须在 main 上（当前 {git_branch(repo_root)}）；"
                         f"tag 打在合入 main 之后")
    cur = inflight_read(repo_root.name)
    if cur:
        raise SystemExit(f"{repo_root.name}: 在途登记被占用（pid {cur.get('pid')}「{cur.get('note', '')}」）"
                         f"——确认对方收尾后 inflight.py release {repo_root.name} 再发版")


def tag_exists_remote(cwd: str, tag: str) -> bool:
    r = subprocess.run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"],
                       cwd=cwd, capture_output=True, text=True)
    return bool(r.stdout.strip())


def go_env():
    env = dict(os.environ)
    env["GOWORK"] = "off"
    return env


def check_module(name: str, module_dir, tag: str, failures: list[str]):
    """单模块四道前置检查（结果追加进 failures）。"""
    # a. ls-remote 同名 tag 查重（重推 = 代理/sumdb 缓存集体中毒，.github#5）
    if tag_exists_remote(str(module_dir), tag):
        failures.append(f"{name}: tag {tag} 已存在于远端（重推会使已缓存消费方集体中毒）")
        return
    # b. tidy 干净（地板指向已删 tag 时 tidy 直接红）
    r = subprocess.run(["go", "mod", "tidy", "-diff"], cwd=module_dir,
                       capture_output=True, text=True, env=go_env())
    if r.returncode != 0:
        failures.append(f"{name}: go mod tidy -diff 非空\n{r.stdout[-600:]}{r.stderr[-200:]}")
        return
    # c. 构建
    r = subprocess.run(["go", "build", "./..."], cwd=module_dir,
                       capture_output=True, text=True, env=go_env())
    if r.returncode != 0:
        failures.append(f"{name}: go build 失败\n{r.stderr[-600:]}")
        return
    # d. CHANGELOG 核对（同名小节或行内出现 tag 名）
    cl = module_dir.parent / "CHANGELOG.md" if name != "main" else module_dir / "CHANGELOG.md"
    if not cl.exists() or tag not in cl.read_text():
        failures.append(f"{name}: CHANGELOG.md 未包含 {tag}（版本号必须来自即将发布的内容）")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if len(args) < 2 or any("=" not in a for a in args[1:]):
        print(__doc__)
        sys.exit(2)
    repo = args[0]
    if repo not in MODULES:
        print(f"未知模块仓 {repo}（MODULES 无此项）")
        sys.exit(2)

    root = repo_dir(repo)
    preflight_gate(root, apply)
    specs = []  # (名称, 模块目录, tag 名)，主模块在前
    for spec in args[1:]:
        name, ver = spec.split("=", 1)
        if not re.fullmatch(r"v\d+\.\d+\.\d+", ver):
            print(f"{spec}: 版本号须为 vX.Y.Z 全三段")
            sys.exit(2)
        if name == "main":
            specs.append(("main", root, ver))
        else:
            sub = root / name
            if not (sub / "go.mod").exists():
                print(f"{repo}/{name}: 子目录无 go.mod，不是嵌套模块")
                sys.exit(2)
            specs.append((name, sub, f"{name}/{ver}"))

    print(f"release train: {repo}（{'apply' if apply else 'dry-run'}）")
    for name, d, tag in specs:
        print(f"  {tag}  <- {d}")

    # 先全部检查，任一失败即中止（不产生任何 tag）
    failures: list[str] = []
    for name, d, tag in specs:
        print(f"[check] {tag}")
        check_module(name, d, tag, failures)
    if failures:
        print(f"\n❌ {len(failures)} 项检查失败，未打任何 tag：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    if not apply:
        print("\n✓ 全部检查通过（dry-run，加 --apply 打 tag 并推送）")
        return

    # 按序打 annotated tag；先全部创建，再统一推送（推送失败可定位已建列表）。
    # 发版期间登记在途（防并行会话同仓抢发），收尾 finally 释放。
    import inflight
    inflight.claim(repo, "release_train " + " ".join(t for _, _, t in specs))
    created = []
    try:
        for name, d, tag in specs:
            msg = f"{repo} {tag}"
            subprocess.run(["git", "-C", str(d), "tag", "-a", tag, "-m", msg], check=True)
            created.append((name, d, tag))
            print(f"[tag] {tag}")
        for name, d, tag in created:
            subprocess.run(["git", "-C", str(d), "push", "origin", tag], check=True)
            print(f"[push] {tag}")
        create_releases(repo, root, created)
    finally:
        inflight.release(repo)
    print(f"\n✓ train 完成：{len(created)} 个 tag 已推送")


def create_releases(repo: str, root, created: list) -> None:
    """tag → GitHub Release 对账（.github#8）：notes=CHANGELOG 同版本小节 + 该 tag
    go.mod 的矩阵依赖地板。嵌套模块 tag 恒 --latest=false，根模块 tag 成为 latest。
    失败只告警不回滚（tag 已推送；gen_releases.py --apply 可幂等补齐）。"""
    import os
    import tempfile

    sys.path.insert(0, str(Path(__file__).parent))
    from gen_releases import build_notes  # noqa: E402

    for name, _d, tag in created:
        path = None
        try:
            notes = build_notes(str(root), tag, repo)
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write(notes)
                path = f.name
            args = ["release", "create", tag, "--repo", f"oekit/{repo}",
                    "--title", tag, "--notes-file", path, "--verify-tag"]
            if "/" in tag:  # 嵌套模块 tag 不抢根模块的 latest
                args.append("--latest=false")
            r = subprocess.run(["gh", *args], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[release] ⚠ {tag}: {(r.stderr or r.stdout).strip()[:200]}（gen_releases.py --apply 可补齐）")
            else:
                print(f"[release] {tag}")
        finally:
            if path:
                os.unlink(path)


if __name__ == "__main__":
    main()
