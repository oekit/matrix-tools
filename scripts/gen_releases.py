#!/usr/bin/env python3
"""GitHub Release 对账/回填（oekit/.github#8）：每个 tag 对应一个 Release。

#8 之前 oekit 全矩阵只有 tag、没有任何 Release（`releases/latest` 全部 404）——
下游没有 breaking change 的归档位，release-please / goreleaser 类工具无从消费，
「这一版验证过的模块组合」也没有地方声明。本脚本把缺口收成一条命令：

  - notes = CHANGELOG.md 里同版本小节（嵌套 tag 先找 `<sub>/CHANGELOG.md` 的
    `[vX.Y.Z]`，再回退根 CHANGELOG 的 `[<sub>/vX.Y.Z]`，如 bootstrap/nacos）；
  - notes 末尾附「矩阵内依赖地板」段：该 tag go.mod 里全部 github.com/oekit/*
    require 行——这正是「这一组是 CI 验证过的兼容组合」的可审计声明；
  - 除最新根模块 tag 外全部 `--latest=false`，保证 `releases/latest` 指向最新版；
  - 幂等：已存在的 Release 跳过，重复跑无副作用。

用法：
  python3 scripts/gen_releases.py                 # dry-run：列出缺 Release 的 tag
  python3 scripts/gen_releases.py --apply         # 创建缺失的 Release
  python3 scripts/gen_releases.py --check         # 门禁模式：有缺失则 exit 1（audit 调用）
  python3 scripts/gen_releases.py --repo data --apply
  python3 scripts/gen_releases.py --repo data --tag v0.9.0 --apply   # 只补一个
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, ORG, repo_dir  # noqa: E402
from gen_versions import _remote_tags  # noqa: E402
import ghsafe  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))

OEKIT_DEP_RE = re.compile(r"^\s*github\.com/oekit/\S+\s+v\S+", re.M)
VER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class ReleaseUnreachable(RuntimeError):
    """某仓的 Release 列表读不到（权限/限速/网络）。门禁按仓跳过，不红。

    rate_limited 标记供调用方做**熔断**：限速是全矩阵级的（配额被打满），
    继续逐仓重试只会把剩下的配额也耗光，且每一仓都会以同样的话失败——
    既慢又说不清真相。熔断后剩下���仓直接标注「未核对（限速熔断）」。
    """

    def __init__(self, msg: str, rate_limited: bool = False):
        super().__init__(msg)
        self.rate_limited = rate_limited


def gh(*args: str, check: bool = True) -> tuple[int, str, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} 失败：{(p.stderr or p.stdout).strip()[:300]}")
    return p.returncode, p.stdout, p.stderr


def semver_key(tag: str):
    ver = tag.rsplit("/", 1)[-1]
    m = VER_RE.match(ver)
    if not m:
        return (0, 0, 0, tag)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), tag)


RATE_LIMIT_RE = re.compile(r"rate limit|abuse detection|secondary rate|403 Forbidden", re.I)


def _has_token() -> bool:
    """本次进程能不能算「已认证」（env token **或** gh 配置里的账号）。

    旧实现只看 env 变量，看不见「gh 已登录（keyring / hosts.yml）」这种常态
    ⇒ 本机明明有认证却被当成匿名。认证与否决定走 5000/h 还是 60/h 的配额，
    判错会让匿名路径白烧配额、也会让可靠路径用不上 ⇒ 以 `gh auth status`
    为准（进程内缓存）。
    """
    return ghsafe.gh_authenticated()


def _read_releases_once(repo: str, attempts: list[str]) -> set[str]:
    """读一次远端 Release 列表。

    返回 tag 名集合；**读到空集合也返回空集合**（空不等于「没有」，
    由 `existing_releases` 决定要不要采信）；命令失败（权限/限速）⇒
    返回空集合并把失败原因记进 `attempts`。
    """
    out: set[str] = set()
    if _has_token():
        # GraphQL 路径：实测 10/10 稳定，一次到位（5000/h）。
        code, txt, err = gh("release", "list", "--repo", f"{ORG}/{repo}", "--limit", "300",
                            "--json", "tagName", "--jq", ".[].tagName", check=False)
        if code == 0:
            out |= set(txt.split())
        else:
            attempts.append(err or txt)
    if not out:
        # REST 路径：与 GraphQL 独立，可交叉验证；匿名也可读。
        # --paginate 是必需的：per_page 上限 100，单页会漏判「已存在的 Release」
        # 被误报成缺失（当前各仓最多 31 条，离上限还远，但那是会涨的量）。
        code, txt, err = gh("api", "--paginate", f"repos/{ORG}/{repo}/releases?per_page=100",
                            "--jq", ".[].tag_name", check=False)
        if code == 0:
            out |= set(txt.split())
        else:
            attempts.append(err or txt)
    return out


def existing_releases(repo: str) -> set[str]:
    """远端已存在的 Release tag 名（对抗 GitHub 偶发返回空集合）。

    这是本门禁最容易**静默失效**的一环，两次踩坑方向相反：

    - #26（漏报）：CI 里不注入 GH_TOKEN 时 `gh` 只能匿名跑，匿名 REST 60 次/小时，
      Actions runner 共享出口 IP，全矩阵逐仓一查就把它打满 ⇒ 所有仓一起
      「读不到」——门禁看起来绿，实际一仓没查；
    - 2026-10-22（**假报**）：GitHub API 偶发返回空集合（实测 20~30% 的请求，
      `GET /repos/{o}/{r}/releases` 返回 `200` + `[]`，curl 直连同样复现）
      ⇒ 旧实现「读到空 = 一个 Release 都没有」⇒ audit 报 11 个仓「N 个 tag
      缺 Release」，而真实 parity 是 **24/24 全绿**。

    ⇒ 现在的口径：
    - 读到空 ⇒ **换条独立路径重读**，连续 4 轮都空才认「事实为空」
      （真没建过 Release 的仓走这条路，仍然会被如实报成真漂移）；
    - 命令失败（权限/限速）⇒ 抛 `ReleaseUnreachable` 让调用方按「不可核对」跳过，
      跳过必须被显式渲染（#26 的教训：跳过不能渲染成通过）；
    - 报错必须带 **stderr**：gh 把失败原因写 stderr，只取 stdout 会得到
      `无法读取 Release 列表（）` 这种空括号，限速与权限完全分不开。
    """
    attempts: list[str] = []
    have: set[str] = set()
    for _ in range(ghsafe.EMPTY_ROUNDS):
        got = _read_releases_once(repo, attempts)
        if got:
            have |= got
            break
    if have:
        return have
    if attempts:
        reason = " | ".join(a.strip()[:120] for a in attempts if a.strip())[:240] or "（gh 未输出 stderr）"
        kind = "限速" if RATE_LIMIT_RE.search(reason) else "权限/网络"
        raise ReleaseUnreachable(f"{repo}: 无法读取 Release 列表（{kind}：{reason}）",
                                 rate_limited=(kind == "限速"))
    # 连续多轮都读到空（且没有任何命令失败）⇒ 事实为空：一个 Release 都没有。
    return set()


def changelog_section(repo_dir_path, version: str, nested: str | None) -> str | None:
    """取 CHANGELOG 同版本小节正文。嵌套 tag 优先 <sub>/CHANGELOG.md 的 [vX.Y.Z]，
    回退根 CHANGELOG 的 [<sub>/vX.Y.Z]（bootstrap/nacos 形态）；文件缺失返回 None
    （调用方回退 CHANGELOG 链接）。"""
    if nested:
        sub_cl = os.path.join(repo_dir_path, nested, "CHANGELOG.md")
        if os.path.exists(sub_cl):
            sec = _extract(open(sub_cl, encoding="utf-8").read(), version)
            if sec is not None:
                return sec
        root_cl = os.path.join(repo_dir_path, "CHANGELOG.md")
        if not os.path.exists(root_cl):
            return None
        return _extract(open(root_cl, encoding="utf-8").read(), f"{nested}/{version}")
    root_cl = os.path.join(repo_dir_path, "CHANGELOG.md")
    if not os.path.exists(root_cl):
        return None
    return _extract(open(root_cl, encoding="utf-8").read(), version)


def _extract(text: str, head: str) -> str | None:
    pat = re.compile(r"(?ms)^## \[" + re.escape(head) + r"\][^\n]*\n(.*?)(?=^## \[|\Z)")
    m = pat.search(text)
    return m.group(1).strip() if m else None


def go_mod_at(repo_dir_path, tag: str, nested: str | None) -> str | None:
    rel = os.path.join(nested, "go.mod") if nested else "go.mod"
    r = subprocess.run(["git", "-C", repo_dir_path, "show", f"{tag}:{rel}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def build_notes(repo_dir_path, tag: str, repo: str) -> str:
    nested = tag.rsplit("/", 1)[0] if "/" in tag else None
    version = tag.rsplit("/", 1)[-1]
    parts = [f"Release of `{ORG}/{repo}` `{tag}`（annotated tag，由 release_train 门禁后打点）。"]
    sec = changelog_section(repo_dir_path, version, nested)
    if sec:
        parts.append("## 变更记录\n\n" + sec)
    else:
        parts.append(f"变更记录见 [CHANGELOG.md](https://github.com/{ORG}/{repo}/blob/{tag}/CHANGELOG.md)。")
    gm = go_mod_at(repo_dir_path, tag, nested)
    if gm:
        deps = sorted(set(OEKIT_DEP_RE.findall(gm)))
        if deps:
            parts.append(
                "## 矩阵内依赖地板（该 tag 的 go.mod，CI 验证过的组合）\n\n```go.mod\n"
                + "\n".join(deps) + "\n```")
    return "\n\n".join(parts) + "\n"


def plan_repo(repo: str, only_tag: str | None = None) -> tuple[list[str], str | None, list[str]]:
    """返回 (缺失 tag 列表[升序], 最新根 tag, 该仓全部 tag)。"""
    tags = _remote_tags(repo)          # 降序
    tags_asc = sorted(tags, key=semver_key)
    if only_tag:
        tags_asc = [t for t in tags_asc if t == only_tag]
    latest_root = next((t for t in tags if "/" not in t), None)
    have = existing_releases(repo)
    missing = [t for t in tags_asc if t not in have]
    return missing, latest_root, tags_asc


def apply_repo(repo: str, missing: list[str], latest_root: str | None) -> int:
    d = str(repo_dir(repo, BASE))
    created = 0
    for tag in missing:
        notes = build_notes(d, tag, repo)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(notes)
            path = f.name
        try:
            args = ["release", "create", tag, "--repo", f"{ORG}/{repo}",
                    "--title", tag, "--notes-file", path, "--verify-tag"]
            if tag != latest_root:
                args.append("--latest=false")
            gh(*args)
            created += 1
            print(f"  ✓ {repo} {tag}" + (" (latest)" if tag == latest_root else ""))
        finally:
            os.unlink(path)
    return created


def missing_releases(repos: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """audit 门禁用：返回 ({repo: 缺失 tag}, 因权限/限速无法核对的仓)。

    第二项是「门禁没生效」的证据，调用方必须把它显式渲染出来——不能和
    「核对通过」渲染成同一种绿色对勾（#26 的教训：23/24 仓全跳过时报告仍显示通过）。
    """
    out: dict[str, list[str]] = {}
    skipped: list[str] = []
    tripped = False
    for i, repo in enumerate(repos, 1):
        # 进度写 stderr（实时落盘，不进 stdout 的报告）：这一步是 24 次**串行**
        # 的 GitHub API 调用，是整条审计里最长的阶段；没有进度行时，
        # 它一旦卡住就只能看到「进程还在、但不动了」（2026-10-25 实测：
        # 后台审计任务 900s 超时时，日志里一个字都没有）。
        print(f"[releases] 对账 {i}/{len(repos)} {repo}", file=sys.stderr, flush=True)
        if tripped:
            skipped.append(f"{repo}: 未核对（限速熔断：配额已耗尽，停止后续请求以免空转）")
            continue
        try:
            missing, _, _ = plan_repo(repo)
            if missing:
                out[repo] = missing
        except (RuntimeError, PermissionError) as e:
            skipped.append(str(e))  # 异常消息已自带仓名，不重复拼前缀
            if getattr(e, "rate_limited", False):
                tripped = True
    return out, skipped


def main() -> int:
    args = sys.argv[1:]
    do_apply = "--apply" in args
    do_check = "--check" in args
    only_repo = args[args.index("--repo") + 1] if "--repo" in args else None
    only_tag = args[args.index("--tag") + 1] if "--tag" in args else None
    repos = [only_repo] if only_repo else list(MODULES)

    mode = "APPLY" if do_apply else ("CHECK" if do_check else "DRY-RUN")
    print(f"GitHub Release 对账（{mode}，{len(repos)} 仓）\n")
    total_missing = 0
    skipped: list[str] = []
    tripped = False
    for repo in repos:
        if tripped:
            skipped.append(f"{repo}: 未核对（限速熔断：配额已耗尽，停止后续请求以免空转）")
            continue
        try:
            missing, latest_root, _ = plan_repo(repo, only_tag)
        except (RuntimeError, PermissionError) as e:
            skipped.append(str(e))  # 异常消息已自带仓名，不重复拼前缀
            if getattr(e, "rate_limited", False):
                tripped = True
            continue
        if not missing:
            continue
        total_missing += len(missing)
        root_extra = f"，latest={latest_root}" if latest_root else "（无根模块 tag，全部 --latest=false）"
        print(f"→ {repo}: 缺 {len(missing)} 个 Release（{missing[0]} … {missing[-1]}{root_extra}）")
        if do_apply:
            apply_repo(repo, missing, latest_root)

    if skipped:
        print(f"\n无法核对（权限/限速，门禁模式下跳过不红）：{len(skipped)}/{len(repos)} 仓")
        for s in skipped:
            print(f"  ⚠ {s}")
    if do_check:
        if total_missing:
            print(f"\n❌ {total_missing} 个 tag 缺 Release（跑 gen_releases.py --apply）")
            return 1
        # 「一个都没查」和「查过且都齐全」不能输出同一句话——#26 里 23/24 仓
        # 全部读不到 Release，报告却显示通过，等于门禁静默失效一整轮。
        if skipped:
            print(f"\n⚠ 已核对部分缺 0 个，但 {len(skipped)}/{len(repos)} 仓未取到 Release 列表："
                  f"这部分本门禁**没有结论**（CI 需注入 GH_TOKEN，见 audit.yml）")
            return 0
        print("✓ 全部 tag 均有 Release（已逐仓核对）")
        return 0
    if not do_apply:
        print(f"\n共 {total_missing} 个缺失。加 --apply 创建。")
    elif total_missing == 0:
        print("\n✓ 无缺失。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
