#!/usr/bin/env python3
"""发布协同工具：主模块 + 嵌套模块同 train 按序打 tag（matrix-tools#6，承接 bootstrap#2）。

单仓发版一条命令（--changelog 自动补版本节 → PR → 等合并 → 检查 → tag → Release）：
  python3 scripts/release_train.py pkg main=v0.16.0 --apply --changelog "新增 xxx（#N）"
嵌套同 train（嵌套小节自动写入根与子 CHANGELOG，父模块地板须已就位）：
  python3 scripts/release_train.py bootstrap main=v0.6.4 nacos=v0.3.4 obs=v0.2.9 --apply --changelog "..."
缺省 dry-run 只跑检查；--changelog 缺小节时 dry-run 只提示不落 PR。

检查面（全过才打 tag，任一失败原子中止）：
  a. ls-remote 同名 tag 查重（重推 = 代理/sumdb 集体中毒，.github#5）
  b. go mod tidy -diff 干净（地板指向已删 tag 时直接红）
  c. go build（GOWORK=off，外部用户视角）
  d. CHANGELOG 含本 tag（版本号必须来自即将发布的内容）
  e. gofmt + golangci-lint（本地可用时；与 org CI 同面——发完才发现红的事不再有）
  0. train 前自动 fetch + ff 拉齐 main（tag 永远打在远端 main 最新基线上，
     不再依赖"记得先 pull"——并行会话推过 main 也不落空）
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
sys.path.insert(0, str(__file__).rsplit("/", 1)[0])
from matrix import MODULES, ORG, git_branch, ensure_clean_all, repo_dir, sh  # noqa: E402
from inflight import read as inflight_read  # noqa: E402

TODAY = time.strftime("%Y-%m-%d")


def preflight_gate(repo_root, apply: bool) -> None:
    """发版现场门禁：工作树干净；main 与远端一致（落后自动 ff 拉齐、分叉即拒）；
    --apply 还要求 main 分支 + 未被并行会话登记占用。"""
    ensure_clean_all([repo_root], "release_train")
    if git_branch(repo_root) != "main":
        raise SystemExit(f"{repo_root.name}: 须在 main 上（当前 {git_branch(repo_root)}）")
    sh(["git", "fetch", "origin", "main"], repo_root)
    behind = sh(["git", "rev-list", "--count", "main..origin/main"], repo_root).stdout.strip()
    ahead = sh(["git", "rev-list", "--count", "origin/main..main"], repo_root).stdout.strip()
    if behind != "0":
        sh(["git", "pull", "--ff-only", "origin", "main"], repo_root)
        print(f"[sync] main 落后 {behind}，已 ff 拉齐")
    ahead = sh(["git", "rev-list", "--count", "origin/main..main"], repo_root).stdout.strip()
    if ahead != "0":
        raise SystemExit(f"{repo_root.name}: main 领先远端 {ahead} 个提交（未推）——先落 PR 推 main 再 train")
    if apply:
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


def check_repo(root, failures: list[str]) -> None:
    """仓级检查（每 train 一次）：gofmt + golangci-lint（本地可用时）。"""
    r = subprocess.run(["gofmt", "-l", "."], cwd=root, capture_output=True, text=True)
    files = [l for l in r.stdout.split() if "/vendor/" not in l and not l.startswith("vendor/")]
    if files:
        failures.append(f"gofmt 未格式化：{files[:5]}")
        return
    if not shutil.which("golangci-lint"):
        print("[check] golangci-lint 本地不可用，跳过（CI 兜底）")
        return
    r = subprocess.run(["golangci-lint", "run", "--timeout", "5m"],
                       cwd=root, capture_output=True, text=True, env=go_env())
    if r.returncode != 0:
        failures.append(f"golangci-lint 失败\n{(r.stdout + r.stderr)[-600:]}")


def check_module(name: str, module_dir, tag: str, failures: list[str]):
    """单模块前置检查（结果追加进 failures）。"""
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


# ---------- --changelog：版本节自动生成 + PR + 等合并 ----------

def wanted_sections(root, specs) -> list[tuple[Path, str]]:
    """每个 spec 需要的 (CHANGELOG 文件, 标题行前缀)。嵌套 spec 需根 + 子双份。"""
    out = []
    main_tag = next((t for n, _d, t in specs if n == "main"), None)
    for name, d, tag in specs:
        if name == "main":
            out.append((root / "CHANGELOG.md", f"## [{tag}]"))
        else:
            out.append((root / "CHANGELOG.md", f"## [{tag}]"))
            ver = tag.rsplit("/", 1)[-1]
            out.append((d / "CHANGELOG.md", f"## [{ver}]"))
    return out


def insert_top(path: Path, heading: str, bullet: str) -> bool:
    """版本节插到文件最顶（严格递减门禁要求新版本在最上）；已存在返回 False。"""
    t = path.read_text()
    if any(l.startswith(heading) for l in t.splitlines()):
        return False
    block = f"{heading} — {TODAY}\n\n- {bullet}\n\n"
    m = re.search(r"(?m)^## \[", t)
    t2 = (t[:m.start()] + block + t[m.start():]) if m else t.rstrip() + "\n\n" + block
    path.write_text(t2)
    return True


def validate_changelog(path: Path) -> None:
    """本地跑 org 的 changelog_check.py（缺失则跳过——CI 兜底）。"""
    chk = Path(__file__).resolve().parent.parent.parent / "profile" / "scripts" / "changelog_check.py"
    if not chk.exists():
        return
    r = subprocess.run([sys.executable, str(chk), str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"CHANGELOG 门禁预校验失败（{path.name}）：\n{(r.stdout + r.stderr)[-400:]}")


def wait_pr_merged(repo: str, number: int, timeout: int = 900) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = sh(["gh", "pr", "view", str(number), "--repo", f"{ORG}/{repo}",
                 "--json", "state", "--jq", ".state"], None, check=False).stdout.strip()
        if st == "MERGED":
            print(f"[changelog] PR #{number} 已合并")
            return
        if st == "CLOSED":
            raise SystemExit(f"{repo}: CHANGELOG PR #{number} 被关闭，中止")
        time.sleep(15)
    raise SystemExit(f"{repo}: 等待 CHANGELOG PR #{number} 合并超时（CI 可能仍在跑）——稍后重跑 train")


def ensure_changelog(repo: str, root, specs, bullets: list[str], apply: bool) -> None:
    """缺版本节时：生成（置顶）→ 预校验 → PR → 排程合并 → 等合并 → 拉齐。
    dry-run 只报告不落 PR。bullets 来自 --changelog（可多次），第一条给主模块。"""
    wanted = wanted_sections(root, specs)
    todo = []
    for path, heading in wanted:
        t = path.read_text() if path.exists() else ""
        if not any(l.startswith(heading) for l in t.splitlines()):
            todo.append((path, heading))
    if not todo:
        return
    if not apply:
        for path, heading in todo:
            print(f"[changelog] dry-run：{path.name} 缺 {heading}（--apply 时自动生成并落 PR）")
        return
    main_tag = next((t for n, _d, t in specs if n == "main"), "")
    changed: set[Path] = set()
    for path, heading in todo:
        nested_self = path != root / "CHANGELOG.md"
        bullet = bullets[0] if (not nested_self and bullets) else \
            f"跟随 {repo} {main_tag} 发版（release_train 嵌套 train）"
        if insert_top(path, heading, bullet):
            changed.add(path)
    for path in sorted(changed):
        validate_changelog(path)
    files = [str(p.relative_to(root)) for p in sorted(changed)]
    from prkit import land_pr, merge_pr, open_pr
    url = land_pr(root, repo, f"chore/rel-{main_tag}",
                  f"chore: CHANGELOG 补 {main_tag}（release_train --changelog）",
                  f"chore: CHANGELOG 补 {main_tag}",
                  "release_train 自动生成（版本节置顶，过 changelog_check 预校验）。",
                  edit=lambda: files, auto_merge=True)
    print(f"[changelog] PR: {url or '无变更'}")
    if not url:
        return
    m = re.search(r"/pull/(\d+)", url)
    if m:
        pr = open_pr(repo, f"chore/rel-{main_tag}")
        if pr:  # 尚未自动合并则等待（auto-merge 等 CI）
            wait_pr_merged(repo, int(m.group(1)))
    sh(["git", "pull", "--ff-only", "origin", "main"], root)
    preflight_gate(root, apply=True)


def main():
    argv = sys.argv[1:]
    apply = "--apply" in argv
    bullets: list[str] = []
    args: list[str] = []       # 位置参数（--changelog 的值不属于此处）
    i = 0
    while i < len(argv):
        if argv[i] == "--changelog":
            if i + 1 >= len(argv):
                print("--changelog 需要跟描述文本")
                sys.exit(2)
            bullets.append(argv[i + 1])
            i += 2
        else:
            if not argv[i].startswith("--"):
                args.append(argv[i])
            i += 1
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

    if bullets:
        ensure_changelog(repo, root, specs, bullets, apply)
    elif apply:
        # apply 但没给 --changelog 且缺小节 → check d 会拦；提前提示
        missing = [(p, h) for p, h in wanted_sections(root, specs)
                   if not any(l.startswith(h) for l in (p.read_text().splitlines() if p.exists() else []))]
        if missing:
            print(f"[hint] 缺 {len(missing)} 个版本节，可用 --changelog \"描述\" 自动生成")

    # 先全部检查，任一失败即中止（不产生任何 tag）
    failures: list[str] = []
    print("[check] gofmt + golangci-lint（仓级）")
    check_repo(root, failures)
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
