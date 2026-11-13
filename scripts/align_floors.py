#!/usr/bin/env python3
"""矩阵内依赖地板对齐（不动点）：go.mod 的 github.com/oekit/* 地板对齐各依赖最新 tag。

**定位（2026-10-19 起）：维护轮工具，非常规跟版动作。**
发版模型是按需的（pull-based）：底层依赖（pkg/concurrency 等）发新版后，
下游地板不动、不强制发版；下游仓改代码消费到新 API 时 go get 抬地板、
随本仓版本自然发出——谁用新版本谁升级。跨仓地板漂移不是漂移，audit 不检查
（只查嵌套模块对父模块的地板，那是发版正确性要求）。本工具只在显式维护轮
（低频，如月度/大版本对齐后）全量跑，或 --repo 单仓按需对齐。

用法：
  python3 scripts/align_floors.py --plan              # 默认：列出将发生的地板抬升（只读）
  python3 scripts/align_floors.py --repo data --plan  # 单仓按需对齐预览
  python3 scripts/align_floors.py --apply             # 写入 go.mod + GOWORK=off tidy（仅 main+干净仓）
  python3 scripts/align_floors.py --apply --pr        # 变更仓逐仓开 PR（分支 docs/align-floors）
  --repos a,b,c                                       # 子集（缺省全矩阵）
  --base <矩阵检出根目录>                              # 显式指定检出根（默认取 MATRIX_BASE，其次本仓上级）

注意：地板抬升只是本地变更——对齐完成的定义是这些仓以 patch 版发版
（release_train）且下游可见。--apply 输出末尾会列出待发版仓。

**BOM 仓（伞仓 oekit）不在本工具的处理集**（matrix-tools#241）：它的 go.mod
require 是无 import 的地板快照，tidy 会整块清空；显式 `--repos oekit` /
`--repo oekit` 会被拒绝，并指向 gen_matrix_go.py（BOM 的地板与 matrix.go 是
同一生成器的两个产物，必须整体重生成）。apply 阶段另有 require 骤降护栏：
tidy 前后 oekit 地板 require 行数只增不减，减少即回滚报错。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import (BOM_REPO, MODULES, OEKIT_REQ_RE, RemoteReadError,  # noqa: E402
                    apply_sumdb_policy, enable_tag_cache, git_branch, is_dirty,
                    go_toolchain_from_gomod, latest_nested_tag, latest_tag, repo_dir, sh)
from prkit import land_pr  # noqa: E402

# 地板扫描用的 require 行正则：**容忍行尾的 `// indirect` 标记**。
#
# 为什么不直接用 matrix.OEKIT_REQ_RE：它以 `$` 收尾、不匹配 indirect 行
# （`\tgithub.com/oekit/concurrency v0.23.0 // indirect`）。而矩阵里**嵌套
# 模块**的依赖几乎全是 indirect 形态（bootstrap/nacos、bootstrap/obs、
# timertask/redis …）⇒ 用它扫描等于**看不见嵌套模块的地板**。
#
# 后果实测（2026-10-29）：bootstrap#50 只抬了根模块的 concurrency，
# 嵌套模块 nacos/obs 里的 concurrency 仍停在 v0.23.0 ⇒ CI 的
# `go mod tidy -diff` 门禁判红；而工具自报「✓ 全矩阵地板零漂移」——
# **工具说对齐了，实际没对齐**，这类假绿比报错更危险。
#
# 独立定义而不改 matrix.OEKIT_REQ_RE：后者的语义是「这个仓**直接**依赖哪些
# 矩阵模块」（dep_matrix 的依赖矩阵用），改动会影响那张表的语义。
#
# ---- 2026-11-02 扩：能匹配**嵌套模块**形态（oekit/oekit#104）----
#
# 原正则只匹配 `github.com/oekit/<短名>`（**根模块**）⇒ 看不见
# `github.com/oekit/<仓>/<嵌套路径>`（**别仓的嵌套模块**）这类地板。
# 实测（#104）：nns 的 `registry/nacosimpl v0.1.0`（远端最新 v0.1.1）、
# hertz 的 `logx/hlog v0.1.4`（远端最新 v0.1.6）等 13 处漂移，工具**全看不见**
# ⇒ 「✓ 全矩阵地板零漂移」的假绿又复现了一次。
#
# 现在匹配两种形态（**贪心的路径段**）：
#   `github.com/oekit/<短名>`                 → 根模块
#   `github.com/oekit/<短名>/<路径…>`          → 嵌套模块（可能是别的仓的）
#
# ⚠️ 路径段的字符集必须**排除 `.` 之外的所有路径分隔与空白**，且允许 `-`（仓名
# 带连字符，如 kitex-examples）⇒ 用 `[a-z0-9._/-]+` 但**禁止以 `/` 开头/结尾**、
# 禁止 `//`。
FLOOR_REQ_RE = re.compile(
    r"^(?:\t|require )(github\.com/oekit/([a-z0-9-]+)(/[a-z0-9._-]+(?:/[a-z0-9._-]+)*)?)"
    r" (v\d+\.\d+\.\d+(?:-0\.\d{14}-[0-9a-f]{12}|-\d{14}-[0-9a-f]{12})?)( // indirect)?$", re.M)

def parse_semver(v: str) -> tuple[int, int, int]:
    # 伪版本（#306）：基础三段之后跟随 `-0.<时间戳>-<sha>`——此前 strict unpack
    # 在伪版本行上直接 ValueError（CRASH 比「隐身」好，但抬升计划根本没法跑）。
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-0\.\d{14}-[0-9a-f]{12}|-\d{14}-[0-9a-f]{12})?$", v)
    if not m:
        raise ValueError(f"not a semver/pseudo-version: {v!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def find_gomods(root: Path) -> list[Path]:
    """仓内所有 go.mod（根 + 任意深度的嵌套模块），排除 vendor/.git。

    **必须递归**：嵌套模块的路径深度不固定——bootstrap/nacos、logx/sink 是
    一层，而 kitex 的 observability/tracing/instrumentation/kitexotel 在**第 4 层**。
    只扫一层（`glob("*/go.mod")`）会漏掉深层嵌套模块 ⇒ 父仓地板抬升后
    子模块 go.mod 仍指向旧版本 ⇒ CI 的 `go mod tidy -diff` 门禁判红
    （kitex#58 的真实成因：kitexotel 的 require 停在 pkg v0.15.1，
    而父仓已抬到 v0.30.0）。

    嵌套模块之间有 `replace ... => ../../..` 指向父仓源码，父仓地板一动，
    子模块的间接依赖集合随之变化 ⇒ 子模块必须同步抬，否则 tidy 不一致。
    """
    out = [root / "go.mod"]
    for gm in root.rglob("go.mod"):
        rel = gm.relative_to(root)
        if rel == Path("go.mod"):
            continue
        parts = rel.parts
        if any(p in (".git", "vendor", "node_modules", "testdata") for p in parts):
            continue
        out.append(gm)
    return sorted(out)


def plan_repo(repo: str, base: str) -> dict[Path, list[tuple[str, str, str]]]:
    """{go.mod 路径: [(完整模块路径, 现地板, 目标)]}——只含需要抬升的行。"""
    root = repo_dir(repo, base)
    plans: dict[Path, list[tuple[str, str, str]]] = {}
    gomods = find_gomods(root)
    latest_cache: dict[str, str | None] = {}
    for gm in gomods:
        if not gm.exists():
            continue
        text = gm.read_text()
        ups = []
        for m in FLOOR_REQ_RE.finditer(text):
            full, dep, sub, ver = m.group(1), m.group(2), m.group(3), m.group(4)
            # `dep` = 根模块仓名；`sub` = 嵌套子路径（None ⇒ 这是根模块形态）。
            # 完整模块路径 = full（github.com/oekit/<仓>[/<子路径>]）。
            key = full
            if key not in latest_cache:
                # **必须读远端 tag，不能读本地 tag**（2026-11-02 修）。
                #
                # 形态：本地 clone 的 tag 陈旧（`git fetch` 没跑 / 没带 --tags）⇒
                # 按本地 tag 判 ⇒ **抬不动**，而工具自报「✓ 全矩阵地板零漂移」——
                # **假绿比报错更危险**（本文件此前已有两处同类教训：bootstrap#50
                # 的 indirect 行漏抬、kitex#65 的嵌套 go.sum 滞后）。
                #
                # 实测（2026-11-02，oekit#102 这一轮）：middleware v0.13.6 已发远端，
                # 而本地 clone 尚无该 tag ⇒ plan **漏掉** hertz 的
                # `middleware v0.13.5 → v0.13.6`（以及 BOM 那处）——
                # 清单因此少 1 处，工具还报「零漂移」。
                #
                # 口径与 issue 的「核对口径」一致：按各仓**远端** tag 比对
                # （与 `gen_versions._remote_tags` 同源）。
                #
                # remote=True 时 matrix.latest_tag 走 matrix.remote_tags：
                # 读不到就抛 RemoteReadError（**fail-closed**，绝不静默降级本地 tag）——
                # main() 捕获它并以退出码 2 报「判不了」，不与「没有漂移」混为一谈。
                #
                # ---- 2026-11-02 扩：嵌套模块走 latest_nested_tag ----
                # 嵌套模块的 tag 形如 `hlog/v0.1.6`，**带前缀**⇒ 根模块的
                # latest_tag（只认 `vX.Y.Z` 形态）会把它整条丢掉 ⇒ 必须
                # 按前缀过滤后再比（且**按数字**比，字符串排序会把 v0.3.0
                # 排到 v0.31.0 之后）。
                if sub is None:
                    latest_cache[key] = latest_tag(dep, base, remote=True)
                else:
                    latest_cache[key] = latest_nested_tag(full, base)
            target = latest_cache[key]
            if not target:
                continue
            if parse_semver(ver) < parse_semver(target):
                ups.append((full, ver, target))
        if ups:
            plans[gm] = ups
    return plans


def apply_edits(gm: Path, ups: list[tuple[str, str, str]]) -> None:
    text = gm.read_text()
    for full, ver, target in ups:
        # 保留原行前缀（块内 tab / 单行 require）——logx/bootstrap 是单行形态。
        #
        # **必须容忍行尾的 `// indirect` 标记**（2026-10-29 修）：go.mod 的间接
        # 依赖行形如 `\tgithub.com/oekit/concurrency v0.23.0 // indirect`。
        # 原正则以 `$` 收尾且不匹配该标记 ⇒ 嵌套模块（bootstrap/nacos、
        # bootstrap/obs 等）的间接依赖行**抬不动**——而 plan 阶段
        # OEKIT_REQ_RE 同样以 `$` 收尾、也匹配不到……
        # ⇒ plan 报「要抬」、apply 静默跳过 ⇒ 工具自称对齐、实际没动，
        # CI 的 `go mod tidy -diff` 门禁随后判红（bootstrap#50 的真实成因）。
        #
        # 匹配要**唯一**：同一模块可能同时有直接依赖行与 indirect 行，
        # 两行都要抬——所以按「整行」计数，允许多行命中但逐行替换。
        pat = re.compile(
            rf"^(\t|require )({re.escape(full)}) {re.escape(ver)}( // indirect)?$", re.M)
        hits = pat.findall(text)
        assert hits, f"{gm}: {full} {ver} 行未找到（计划与实际不一致？）"
        text = pat.sub(rf"\g<1>{full} {target}\g<3>", text)
    gm.write_text(text)


def apply_repo(repo: str, base: str, plans: dict[Path, list[tuple[str, str, str]]]) -> list[str]:
    """把一个仓的地板抬到位，并返回变更文件清单（相对仓根）。

    **关键：改完父仓后，该仓的**所有**嵌套模块都要重跑一次 tidy**——
    哪怕它自身没有 ups（2026-10-30 实测，kitex#65 的真实成因）。

    嵌套模块对父仓有 `replace github.com/oekit/kitex => ../../../..` ⇒ 父仓
    go.mod 的 require 一变，**子模块的依赖图随之变化**：即便子模块自己不直接
    require 那个依赖（如 kitexotel 不 require bootstrap），它的 go.sum 里也会
    多出/换掉那个依赖的哈希（经父仓传递进来）。

    ⇒ 只对「计划里有 ups 的 go.mod」跑 tidy 是不够的：kitexotel 压根不在
    计划里（它没直接 require bootstrap）⇒ 它的 go.sum 永远滞后 ⇒
    CI 的 `go mod tidy -diff` 门禁必红。而工具自报「✓ 已写入并 tidy」——
    又一次**假绿**。

    顺序：**先改全部 go.mod，再统一 tidy**（tidy 会用改后的父仓内容解析）。

    **require 骤降护栏（matrix-tools#241）**：tidy 的语义是「只保留被 import
    到的 require」⇒ 对「require 无 import 的地板快照」仓（伞仓 BOM 即此形态，
    36 条 require 没有任何一条被 import）tidy 必然**整块清空**，而工具一路
    报「✓ 已写入并 tidy」。现在 tidy 前后各数一遍 oekit 地板 require 行：
    只增不减是本工具的正常形态（align 只抬版本号、从不删 require），**减少
    超过 tidy 前 ⇒ 判定为破坏 ⇒ 回滚全部 go.mod/go.sum 并报错**，绝不允许
    报「✓ 已写入并 tidy」。护栏兜住的是「跳过名单被绕过」（直接以子仓形态
    加进 BOM、或未来新增同类仓）时的破坏。
    """
    d = repo_dir(repo, base)
    files: list[str] = []
    for gm, ups in plans.items():
        apply_edits(gm, ups)
    all_gomods = [gm for gm in find_gomods(d) if gm.exists()]
    floors_before = {gm: len(FLOOR_REQ_RE.findall(gm.read_text())) for gm in all_gomods}
    # 父仓改完后，仓内**每个**模块都要 tidy（根 + 全部嵌套模块）
    for gm in all_gomods:
        if run_tidy(gm.parent) != 0:
            raise SystemExit(f"{repo}/{gm.parent.relative_to(d)}: go mod tidy 失败")
    shrunk = []
    for gm in all_gomods:
        after = len(FLOOR_REQ_RE.findall(gm.read_text()))
        if after < floors_before[gm]:
            shrunk.append(f"{gm.relative_to(d)}: 地板 require {floors_before[gm]} → {after} 行")
    if shrunk:
        # 回滚 tidy 对整个仓 go.mod/go.sum 的破坏（含本次抬升——半截产物不能留），
        # 然后显式报错并指路。**不得**报「✓ 已写入并 tidy」。
        for gm in all_gomods:
            rel = str(gm.relative_to(d))
            sh(["git", "checkout", "--", rel], d, check=False)
            rel_sum = rel.replace("go.mod", "go.sum")
            if (d / rel_sum).exists():
                sh(["git", "checkout", "--", rel_sum], d, check=False)
        raise SystemExit(
            f"❌ {repo}: go mod tidy 把 oekit 地板 require 清掉了（matrix-tools#241 护栏）：\n"
            + "".join(f"    - {s}\n" for s in shrunk)
            + "  该模块的 require 是**无 import 的地板快照**（伞仓 BOM 即此形态），tidy "
              "的语义（只保留被 import 的）必然清空它。\n"
              "  ⇒ 正确做法：这类仓由 gen_matrix_go.py **整体重生成**（go.mod 与 "
              "matrix.go 是同一生成器的两个产物）：\n"
              "      python3 scripts/gen_matrix_go.py --version vX.Y.Z\n"
              "  本次抬升已随回滚撤销；处理完成因后重跑本工具。")
    for gm in find_gomods(d):
        rel = str(gm.relative_to(d))
        if (d / rel).exists() and sh(["git", "status", "--porcelain", rel], d).stdout.strip():
            files.append(rel)
        rel_sum = rel.replace("go.mod", "go.sum")
        if (d / rel_sum).exists() and sh(["git", "status", "--porcelain", rel_sum], d).stdout.strip():
            files.append(rel_sum)
    return files


def main() -> None:
    args = sys.argv[1:]
    do_apply = "--apply" in args
    do_pr = "--pr" in args
    repos = list(MODULES)
    if "--repos" in args:
        repos = args[args.index("--repos") + 1].split(",")
    if "--repo" in args:
        repos = [args[args.index("--repo") + 1]]
    # base 定位认 MATRIX_BASE（CI 的 repos/<repo> 布局）与 --base 显式覆盖。
    # 此前硬编码 parent.parent.parent：matrix.repo_dir 的语义是
    # `base or os.environ.get("MATRIX_BASE", ...)`——base 非空就把环境变量短路掉，
    # 于是 README 承诺的 MATRIX_BASE 对本工具无效，整改命令会打到错误目录（#96）。
    base = str(Path(__file__).resolve().parent.parent.parent)
    if "--base" in args:
        base = args[args.index("--base") + 1]
    else:
        base = os.environ.get("MATRIX_BASE") or base
    base = os.path.abspath(base)

    # BOM 仓（伞仓 oekit）**不进本工具的处理集**（matrix-tools#241）：它的
    # go.mod require 是「无 import 的地板快照」，go mod tidy 会把整块 36 行
    # require 清空，而工具一路报「✓ 已写入并 tidy」——报成功、实际是破坏
    # （2026-11-08 维护轮实测：`--apply --repos oekit` ⇒ go.mod 40 deletions）。
    # BOM 的地板与 matrix.go 是**同一生成器的两个产物**，必须整体重生成
    # （gen_matrix_go.py），不能单独 tidy。缺省枚举（MODULES）本来就不含
    # 伞仓；这里防的是显式 `--repos oekit` / `--repo oekit` 绕过。
    if BOM_REPO in repos:
        explicit = "--repos" in args or "--repo" in args
        msg = (
            f"❌ align_floors 不处理 BOM 仓 {BOM_REPO}（matrix-tools#241）："
            f"它的 go.mod require 是无 import 的地板快照，go mod tidy 会整块清空"
            f"（工具却会报「✓ 已写入并 tidy」）。BOM 由 gen_matrix_go.py 整体重生成：\n"
            f"    python3 scripts/gen_matrix_go.py --version vX.Y.Z")
        if explicit:
            raise SystemExit(msg)
        print(f"[skip] {msg}")
        repos = [r for r in repos if r != BOM_REPO]

    print("align_floors：依赖地板对齐（维护轮工具 / --repo 按需，非常规跟版）")
    plans_all: dict[str, dict[Path, list[tuple[str, str, str]]]] = {}
    skipped: list[str] = []
    # 远端 tag 缓存：全矩阵 24 仓 × 多个依赖，同一依赖只读一次远端
    # （不开缓存 ⇒ 每次 ls-remote 都要走 HTTPS，慢一个量级）。
    enable_tag_cache()
    try:
        for repo in repos:
            plans = plan_repo(repo, base)
            if plans:
                plans_all[repo] = plans
                for gm, ups in plans.items():
                    rel = gm.relative_to(repo_dir(repo, base))
                    for full, ver, target in ups:
                        print(f"  {repo:<14} {rel}: {full.split('/')[-1]} {ver} → {target}")
    except RemoteReadError as e:
        # **读不到 ≠ 没有**（matrix-tools#79 的口径）：判不了就明确报「判不了」，
        # 绝不把它当成「零漂移」——否则工具会在读失败时静默放过整轮地板对齐。
        print(f"❌ 读不到远端 tag ⇒ 无法判定地板漂移（**不是「零漂移」**）：{e}",
              file=sys.stderr)
        print("先修好网络/凭证再重试。", file=sys.stderr)
        raise SystemExit(2)
    if not plans_all:
        print("\n✓ 全矩阵地板零漂移")
        return

    changed = list(plans_all)
    if not do_apply:
        print(f"\n{len(changed)} 仓需抬升地板（plan 只读，加 --apply 生效；--pr 直接逐仓开 PR）")
        return

    if do_pr:
        print(f"\n== {len(changed)} 仓逐仓开 PR（分支 docs/align-floors，写入在分支上执行）==")
        for repo in changed:
            d = repo_dir(repo, base)
            if is_dirty(d) or git_branch(d) != "main":
                skipped.append(repo)
                continue
            tags = sorted({t for ups in plans_all[repo].values() for _, _, t in ups})
            url = land_pr(d, repo, "docs/align-floors",
                          f"build: 矩阵依赖地板对齐（{'、'.join(tags)}）",
                          f"build: 矩阵依赖地板对齐（{'、'.join(tags)}）",
                          "align_floors.py 不动点对齐。合入后需 patch 发版（release_train）。",
                          edit=lambda repo=repo: re_apply_for_pr(repo, base), auto_merge=True)
            print(f"  ✓ {repo}: {url or '无变更'}")
    else:
        for repo in changed:
            d = repo_dir(repo, base)
            if is_dirty(d) or git_branch(d) != "main":
                skipped.append(repo)
                continue
            apply_repo(repo, base, plans_all[repo])
            print(f"  ✓ {repo} 已写入并 tidy")
        print(f"\n下一步：{', '.join(changed)}\n"
              "  → 逐仓开 PR 可加 --pr，或手工处理；合入后 release_train 打 patch tag 才算对齐完成")
    if skipped:
        print(f"跳过 {len(skipped)} 仓（非 main 或脏）：{', '.join(skipped)}")


def re_apply_for_pr(repo: str, base: str) -> list[str]:
    """land_pr 的 edit：在分支上重跑本仓的抬升（plan_repo 结果与主流程一致），返回变更文件。"""
    return apply_repo(repo, base, plan_repo(repo, base))


# sumdb 收录窗口的特征（matrix-tools#245）：新版本**已在模块代理上**
# （.info/.zip 200）但 checksum DB 还没收录 ⇒ lookup 404 / "temporarily
# unavailable"。这不是网络故障，是「刚发布的版本还没进校验库」。
SUMDB_WINDOW_RE = re.compile(
    r"sum\.golang\.org|sumdb|not found: temporarily unavailable", re.I)
TIDY_ATTEMPTS = 3
TIDY_BACKOFF = (2, 5, 15)   # 秒；sumdb 收录通常分钟级，这里先退避三轮，
                            # 仍失败就把真实原因写进报错（见 run_tidy）。


def go_env_for(module_dir) -> dict:
    """go 子进程环境（matrix-tools#249 / #245，tidy 与 build 同一套）：

    - ``GOWORK=off``：外部用户视角的地板必须可解析（既有口径）；
    - ``GOTOOLCHAIN=<go.mod 的 go 指令换算>``：与 org 共享 CI 同面——本地门禁
      不钉工具链时，判定取决于发版机器上装的 Go（gormx v0.11.6 实测：同一份
      代码 go1.27 判 3 issues、CI 的 go1.24 判 0 ⇒ 本地更严挡发版、更松门禁
      形同虚设）。GOTOOLCHAIN 显式给了版本 ⇒ go **不会**静默回退本机 Go
      （缺工具链时自动下载）；
    - ``GONOSUMDB=github.com/oekit/*``（调用方未自设校验策略时）：矩阵内路径
      跳过 checksum DB——级联发版「刚发完版就抬下游地板」必然撞 sumdb 收录
      窗口（#245）。
    """
    env = dict(os.environ, GOWORK="off")
    apply_sumdb_policy(env)
    gm = Path(module_dir) / "go.mod"
    if gm.exists():
        tc = go_toolchain_from_gomod(gm.read_text(encoding="utf-8", errors="replace"))
        if tc:
            env["GOTOOLCHAIN"] = tc
    return env


def run_tidy(module_dir, runner=None, sleep_fn=None) -> int:
    """GOWORK=off tidy（外部用户视角的地板必须可解析）。

    #245：即便 GONOSUMDB 已把矩阵内路径挡在 sumdb 之外（go_env_for），仍可能
    撞收录窗口（校验策略被人覆盖 / 第三方依赖同窗）⇒ 把「sumdb lookup 404 /
    temporarily unavailable」识别成**可重试的收录窗口**，按 2/5/15s 退避重试；
    重试耗尽 ⇒ 报错文案必须点名真实原因（「刚发布的版本尚未被 sumdb 收录」），
    **不许**原样吐 go 的 404 把排查带向「换代理/查网络」。

    ``runner`` / ``sleep_fn``：可注入（自测用，不真跑 go、不真睡）。
    """
    env = go_env_for(module_dir)
    if runner is None:
        def runner():  # noqa: E306
            return subprocess.run(["go", "mod", "tidy"], cwd=module_dir, env=env,
                                  capture_output=True, text=True)
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep
    last = None
    for attempt in range(TIDY_ATTEMPTS):
        last = runner()
        if last.returncode == 0:
            return 0
        out = (last.stdout or "") + (last.stderr or "")
        if not SUMDB_WINDOW_RE.search(out):
            break                       # 不是收录窗口 ⇒ 不重试（真错误别拖）
        if attempt + 1 < TIDY_ATTEMPTS:
            print(f"  [tidy] sumdb 收录窗口（刚发布的版本尚未被 checksum DB 收录）"
                  f"⇒ 退避 {TIDY_BACKOFF[attempt]}s 重试 {attempt + 1}/{TIDY_ATTEMPTS - 1} …")
            sleep_fn(TIDY_BACKOFF[attempt])
    if last.returncode != 0:
        out = (last.stdout or "") + (last.stderr or "")
        if SUMDB_WINDOW_RE.search(out):
            print("[tidy] ❌ go mod tidy 失败：疑似**刚发布的版本尚未被 sumdb 收录**"
                  "（matrix-tools#245）——模块内容已在代理上（.info/.zip 可取），"
                  "缺的只是校验记录；\n"
                  "  ⇒ 等收录（通常分钟级）后重试；或显式设 GONOSUMDB/GOSUMDB=off"
                  "（本工具默认已对 github.com/oekit/* 跳过 sumdb）。")
        print((last.stdout or "")[-400:] + (last.stderr or "")[-400:])
    return last.returncode


if __name__ == "__main__":
    main()
