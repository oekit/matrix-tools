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
import re
import subprocess
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, repo_dir, sh  # noqa: E402


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

    # 按序打 annotated tag；先全部创建，再统一推送（推送失败可定位已建列表）
    created = []
    for name, d, tag in specs:
        msg = f"{repo} {tag}"
        subprocess.run(["git", "-C", str(d), "tag", "-a", tag, "-m", msg], check=True)
        created.append((name, d, tag))
        print(f"[tag] {tag}")
    for name, d, tag in created:
        subprocess.run(["git", "-C", str(d), "push", "origin", tag], check=True)
        print(f"[push] {tag}")
    print(f"\n✓ train 完成：{len(created)} 个 tag 已推送")


if __name__ == "__main__":
    main()
