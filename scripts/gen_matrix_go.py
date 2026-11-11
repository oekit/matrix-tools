#!/usr/bin/env python3
"""生成 oekit 仓的兼容矩阵 BOM 模块（oekit/.github#8 方案 2）：可被 go get 的版本快照。

产物（写入 oekit 仓根）：
  go.mod   module github.com/oekit/oekit；require 全部矩阵模块（含嵌套模块）的最新
           tag 作为 MVS 地板——下游 `go get github.com/oekit/oekit@latest` 一次拉齐
           一组经矩阵 CI 验证的兼容版本，不会再静默落到互不兼容的旧组合。
  matrix.go 版本快照数据，零 import：消费方可仅 import 本包把版本/breaking
           索引暴露给 /version 端点。
           · 版本：Matrix() []Module / Versions() map[string]string / VersionOf(path)
           · breaking：每个模块带自己的破坏性变更（嵌套只归自己）+ BreakingOf /
             HasBreaking / BreakingModules。
           ⚠️ v0.2.0 起 Matrix / Versions 由**导出的可变值**降级为**返回副本的
           函数**（旧写法 oekit.Versions["x"] → 新写法 oekit.Versions()["x"]）：
           旧的导出可变 map/切片可被消费方并发改写 ⇒ go run -race 实测 DATA RACE。

注意：**不要在 oekit 仓跑 go mod tidy**——BOM 的 require 是「无 import 的地板」，
tidy 会把它们清掉（pkg/network 等仓根目录无 Go 包，空导入保活的 tools.go 模式不可
用）。误跑后重跑本脚本即恢复；audit 的 --check 门禁会在漂移时报红。

口径与站点 versions.json 同源：各仓**远端** tag（gen_versions._remote_tags，防本地
clone 陈旧）。数据模块 `data/all` 已废弃 retract，跳过。

用法：
  python3 scripts/gen_matrix_go.py                    # 生成（Version 沿用现有文件，无则 v0.1.0）
  python3 scripts/gen_matrix_go.py --version v0.1.0   # 指定本快照版本
  python3 scripts/gen_matrix_go.py --check            # 门禁：重新生成并比对（audit 调用）
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import (  # noqa: E402
    MODULES, repo_dir, ensure_clean_all, enable_tag_cache, git_env,
    prefetch_remote_tags, RemoteReadError,
)
from gen_versions import _remote_tags, breaking_index_by_module  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
OEKIT_DIR = os.path.join(BASE, "oekit")
ORG = "oekit"
MODULE_PATH = f"github.com/{ORG}/{ORG}"
BOM_GO = "1.26.0"  # 矩阵最高 go 地板（hertz/kitex 等 1.26 生态豁免仓决定）

# 已废弃的嵌套模块（tag 还在、模块不再发版，不进 BOM）
SKIP_NESTED = {("data", "all")}
NESTED_ONLY_TAGS = re.compile(r"^((?:[^/]+/)*[^/]+)/v\d+\.\d+\.\d+$")

# ---------- 子进程退出码契约（audit.py 按这个分类，不靠猜消息） ----------
#
# 0 = 一致；1 = 真漂移（产物陈旧）；2 = **读不到远端 tag**（判不了 ≠ 通过，#79）。
# 2 单独一个码，是因为「读不到」与「有漂移」是两件事：混在一起报，
# 一次网络抖动就会变成一条假的矩阵漂移（实测 2026-10-25）。
EXIT_OK = 0
EXIT_STALE = 1
EXIT_UNREADABLE = 2


def classify_check(rc: int, out: str) -> tuple[str, str]:
    """把子进程退出码翻译成 (kind, message)。

    kind ∈ {"ok", "stale", "unreadable"}；message 是可直接进审计报告的一句话。
    契约的**生产方**是本脚本（子进程），消费方是 audit.py —— 口径只此一处。
    """
    last = out.splitlines()[-1].strip() if out.strip() else ""
    if rc == EXIT_OK:
        return "ok", last
    if rc == EXIT_UNREADABLE:
        return ("unreadable",
                f"oekit: BOM 新鲜度**未核对**（读不到远端 tag，判不了 ≠ 通过）："
                f"{last or '（子进程无输出）'}")
    return "stale", last or "oekit: BOM 新鲜度检查失败"


def ensure_local_tag(repo_dir_path: str, tag: str, timeout: float = 120) -> None:
    """本地 clone 缺该 tag 时补取一次（best-effort，失败不抛）。

    为什么需要：生成器的**版本取自远端 tag**（gen_versions.latest_version →
    `_remote_tags`），而 **Go 地板取自本地 clone**（`git show <tag>:go.mod`）——
    两路口径分叉。本地 clone 还没 fetch 到那个 tag 时（多仓并行发版 / clone 刚建 /
    网络不稳），`git show` 直接失败。

    这里在取地板前把缺的 tag 补齐，**减少**触发概率；**取不到仍由 go_floor 报错**
    ——fetch 本身可能因网络抖动失败，不能拿它兜底（那正是 #100 的成因）。

    只在 tag 确实缺失时才 fetch：稳态（clone 已是最新）零额外网络开销。

    **fetch 必须有界**（2026-11-03 实录）：SSH 黑洞下这个 fetch 无界挂死，
    整个 BOM 生成/audit 门禁跟着无限等（后台任务只能超时强杀）⇒ timeout
    默认 120s + git_env() keepalive；超时也走「失败不抛」的既有语义——
    真因仍由 go_floor 报，但挂死变成秒级可诊断的「fetch 没取到」。
    """
    have = subprocess.run(["git", "-C", repo_dir_path, "rev-parse", "--verify", "--quiet",
                           f"refs/tags/{tag}"], capture_output=True, text=True)
    if have.returncode == 0:
        return
    # 失败不抛：真因由 go_floor 报（它的消息里带 git 的原始 stderr，更可诊断）
    try:
        subprocess.run(["git", "-C", repo_dir_path, "fetch", "--quiet", "--tags", "origin"],
                       capture_output=True, text=True, timeout=timeout, env=git_env())
    except subprocess.TimeoutExpired:
        pass  # 同「fetch 失败」：交由 go_floor 报真因，绝不兜底假地板


def _tag_gomods(repo_dir_path: str, tag: str) -> str:
    """该 tag 下实际存在的 go.mod 列表（诊断用：取地板失败时告诉人文件在哪）。"""
    r = subprocess.run(["git", "-C", repo_dir_path, "ls-tree", "-r", "--name-only", tag],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return "（读不到该 tag 的文件树）"
    hits = [ln for ln in r.stdout.splitlines() if ln.endswith("go.mod")]
    return "、".join(hits[:10]) or "（该 tag 下没有任何 go.mod）"


def go_floor(repo_dir_path: str, tag: str) -> str:
    """取该 tag 的 go 指令（Go 地板）。

    ⚠️ **嵌套模块必须读它自己的 go.mod**（改造B，实测 2026-11-03）：
    嵌套 tag 形如 `obs/v0.2.14` ⇒ 读 `{tag}:obs/go.mod`。
    旧实现一律读 `{tag}:go.mod` ⇒ 对嵌套 tag 来说那是**宿主仓根目录**的
    go.mod（`module github.com/oekit/bootstrap`, `go 1.24.0`），
    **不是** `obs/go.mod`（`module github.com/oekit/bootstrap/obs`,
    `go 1.26.0`）⇒ 对外报的地板比真实**低报**（下游用 Go 1.25 构建会在
    该模块处直接失败）。全量 35 条里 bootstrap/obs、timertask/redis 两条不符
    （其余 9 个嵌套恰好宿主与嵌套同版本，掩盖了这个 bug）。

    **取不到就报错，绝不兜底**（matrix-tools#100）。

    旧实现取不到时静默返回默认地板 `1.24.0`——那不是「未知」而是**一个具体的、
    看起来合法的地板**，与「真的是 1.24.0 的仓」在产物里长得一模一样，下游无从分辨。
    实测已污染一个发布物：BOM v0.1.42 把 hertz（真实 `go 1.26.0`）写成 `1.24.0`，
    触发条件只是生成时本地 clone 尚未 fetch 到那个 tag。

    地板**低报比不报更危险**：不报会让人去查，报一个具体数字会让人以为那就是事实。
    ⇒ 宁可让生成失败（错误消息里带可执行的修复步骤），也不能写出一个假地板。
    """
    # 嵌套模块的 go.mod 在**它自己的子目录**里；子路径来自 tag 前缀
    # （Go 的子目录模块规则：tag 前缀 = 模块相对仓根的完整子路径）。
    subpath = tag.rsplit("/", 1)[0] if "/" in tag else ""
    rel = f"{subpath}/go.mod" if subpath else "go.mod"
    r = subprocess.run(["git", "-C", repo_dir_path, "show", f"{tag}:{rel}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            f"{repo_dir_path}: 取不到 {tag} 的 {rel}（本地 clone 缺该 tag？）——"
            f"先 `git -C {repo_dir_path} fetch --tags` 再生成。\n"
            f"**不兜底**：兜底值会被写进 BOM 产物，与真实地板无法分辨"
            f"（已因此污染过 BOM v0.1.42，见 matrix-tools#100）。\n"
            f"git 说：{(r.stderr or r.stdout).strip()[:300]}\n"
            f"该 tag 下实际存在的 go.mod：{_tag_gomods(repo_dir_path, tag)}")
    m = re.search(r"(?m)^go (\d+\.\d+(\.\d+)?)", r.stdout)
    if not m:
        raise SystemExit(
            f"{repo_dir_path}: {tag} 的 go.mod 里没有 go 指令——不兜底默认地板"
            f"（兜底值会写进 BOM 产物，与真实地板无法分辨，见 matrix-tools#100）")
    return m.group(1)


def tag_has_gomod(repo_dir_path: str, tag: str) -> bool:
    """该 tag 落点是否存在**本模块**的 go.mod（幽灵/误打 tag 判据，matrix-tools#294）。

    tag 名声明了 `<sub>/vX.Y.Z`，落点提交里却没有 `<sub>/go.mod` ⇒ 这个 tag
    不是该模块的发版点——连续发版命令 cd 断链、在错误仓目录打了别仓的 tag
    （实测 2026-11-11 两枚：`workflow:v0.2.12`、`tracing:redis/v0.2.19`，
    事故记录 matrix-tools#290）。这类 tag **跳过并告警**：跳过不是「兜底」
    ——#100 禁的是往产物里写**假地板**；幽灵 tag 本来就不是有效数据点，
    选它才是把假数据写进产物。

    tag 本身没 fetch 到时**不下结论**（返回 True）：「读不到」≠「不存在」
    （matrix-tools#79 口径），仍由 ensure_local_tag / go_floor 走既有报错。
    """
    subpath = tag.rsplit("/", 1)[0] if "/" in tag else ""
    rel = f"{subpath}/go.mod" if subpath else "go.mod"
    have = subprocess.run(["git", "-C", repo_dir_path, "rev-parse", "--verify", "--quiet",
                           f"refs/tags/{tag}"], capture_output=True, text=True)
    if have.returncode != 0:
        return True
    r = subprocess.run(["git", "-C", repo_dir_path, "cat-file", "-e", f"{tag}:{rel}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def select_nested_tags(repo: str, repo_dir_path: str, tags: list[str]) -> list[tuple[str, str]]:
    """[(子路径, tag)]：每个嵌套子路径选**最新的有效** tag（tags 为降序）。

    幽灵/误打 tag（`tag_has_gomod` 为 False，matrix-tools#294）跳过并告警，
    **不标记 seen** ⇒ 同子路径继续看更老的 tag（最新一个是幽灵时，
    不放弃整个子路径——更老的真发版点仍然有效）。SKIP_NESTED（已废弃
    嵌套模块，tag 还在、模块不再发版）语义不变。
    """
    seen: set[str] = set()
    picked: list[tuple[str, str]] = []
    for tag in tags:
        m = NESTED_ONLY_TAGS.match(tag)
        if not m or m.group(1) in seen or (repo, m.group(1)) in SKIP_NESTED:
            continue
        sub = m.group(1)
        ensure_local_tag(repo_dir_path, tag)
        if not tag_has_gomod(repo_dir_path, tag):
            print(f"[gen_matrix_go] ⚠ 跳过 {repo}:{tag}——落点没有 {sub}/go.mod"
                  f"（误打/幽灵 tag，matrix-tools#290 口径），继续看同子路径更老的 tag",
                  file=sys.stderr)
            continue
        seen.add(sub)
        picked.append((sub, tag))
    return picked


def _ver_key(v: str) -> tuple[int, int, int]:
    """版本串 → 可比较的 (major, minor, patch)（按数字比，不按字符串比）。"""
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def breaking_for(by_module: dict[str, list[dict]], subpath: str,
                 pinned_version: str) -> list[dict]:
    """该模块**自己**的 breaking 条目（嵌套只归自己，不折叠进宿主）+ 版本上界。

    纯函数（不碰网络/不读文件）⇒ 自测能直接驱动它。

    ⚠️ 归属键 = **相对仓根的完整子路径**（嵌套模块的版本号前缀）：
    根模块用 ""，嵌套模块用 "obs" / "observability/tracing/instrumentation/kitexotel"。
    """
    return _breaking_upto(list(by_module.get(subpath, [])), pinned_version)


def _breaking_upto(items: list[dict], pinned_version: str) -> list[dict]:
    """只保留**不晚于**钉住版本的破坏性条目。

    为什么：BOM 是**钉住版本**的快照，而 breaking 索引来自 CHANGELOG
    （可能已经写到下一个版本去了）⇒ 把「还没进快照」的破坏性变更写进
    快照，就是对下游说谎（「这个快照里有这条破坏性变更」——其实没有）。

    口径：条目的版本号（去掉子路径前缀后）≤ 快照钉住的版本号。
    """
    limit = _ver_key(pinned_version)
    out = []
    for it in items:
        v = str(it.get("version", "")).rsplit("/", 1)[-1]
        if _ver_key(v) <= limit:
            out.append(it)
    return out


# ---------- proxy 复核的事实源（matrix-tools#261） ----------
#
# 旧写法把 PROXY_BASE 写死 proxy.golang.org（无回退、无覆盖），且读不到 ⇒
# 「未核对」但**退出码仍是 0**（照印「✓ 一致」）⇒ 在只通镜像（goproxy.cn）
# 的网络里这道复核 100% 空转，报告照样全绿——audit.py 阶段 6 正是按退出码
# 判的 ⇒ 「BOM 新鲜度 ✓」下面可以是零覆盖（#79「判据不可用即红」的破口）。
# 现在：① 事实源可覆盖（OEKIT_GO_PROXY > GOPROXY 首段 > 官方源）；
#       ② 官方源读不到自动回退镜像（本矩阵实测两源都收录）；
#       ③ 复核没跑完 ⇒ 退出码 2（判不了 ≠ 一致，见 main() 的 check 分支）。
DEFAULT_PROXY_BASE = "https://proxy.golang.org"
MIRROR_PROXY_BASE = "https://goproxy.cn"


def proxy_bases() -> list[str]:
    """proxy 复核依次尝试的事实源（环境变量可指定镜像；逐源回退）。"""
    env = os.environ.get("OEKIT_GO_PROXY") or os.environ.get("GOPROXY") or ""
    out: list[str] = []
    first = env.split(",")[0].strip().rstrip("/")
    if first.startswith("http://") or first.startswith("https://"):
        out.append(first)
    for base in (DEFAULT_PROXY_BASE, MIRROR_PROXY_BASE):
        if base not in out:
            out.append(base)
    return out


def proxy_go_directive(module_path: str, version: str, timeout: float = 20.0) -> str | None:
    """读 Go proxy 上该模块该版本的 .mod → go 指令；**读不到返回 None**。

    为什么用 proxy 复核（改造B）：本地 clone 的 go.mod 与 **proxy 上发布出去的
    .mod** 是两件事——本地读错文件（嵌套模块读成宿主根）时，产物里的地板
    会**低报**，而 proxy 上的 .mod 是消费方真正拿到的东西（实测
    `bootstrap/obs@v0.2.14.mod` 是 `go 1.26.0`，而旧产物写 1.24.0）。

    读法沿用 gen_versions._remote_tags 的既有手法：**读失败不静默**——
    返回 None（= 读不到），由调用方区分「读不到」与「读到且不符」。

    逐源尝试（proxy_bases()）：OEKIT_GO_PROXY / GOPROXY 可指定镜像；官方源
    读不到自动回退 goproxy.cn ⇒ 只通镜像的网络里复核不再 100% 空转（#261）。
    """
    for base in proxy_bases():
        url = f"{base}/{module_path}/@v/{version}.mod"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        m = re.search(r"(?m)^go (\d+\.\d+(\.\d+)?)", text)
        if m:
            return m.group(1)
    return None


def check_go_against_proxy(modules: list[dict]) -> tuple[list[str], list[str]]:
    """用 proxy 的 .mod 复核每条 Go 字段 → (漂移消息列表, 未核对模块列表)。

    - **读到且不符** ⇒ 漂移（产物里的地板对外说谎，必须判红）；
    - **读不到** ⇒ 未核对（**不**判红：proxy 在部分网络环境不可达，
      拿网络抖动当漂移就是假红；但必须**显式列出**，不许静默）。
    """
    drift: list[str] = []
    unchecked: list[str] = []
    for m in modules:
        got = proxy_go_directive(m["path"], m["version"])
        if got is None:
            unchecked.append(f"{m['path']}@{m['version']}")
            continue
        if got != m["go"]:
            drift.append(f"{m['path']}@{m['version']}：产物写 Go {m['go']}，"
                         f"proxy 上的 .mod 是 {got}")
    return drift, unchecked


def collect() -> list[dict]:
    """[{repo, path, version, go, nested: bool, breaking: [...]}],
    按 MODULES 自底向上序 + 嵌套字母序。

    ⚠️ `breaking` 是**该模块自己**的破坏性变更（改造A）：嵌套模块的条目
    **只归自己**，不再折叠进宿主仓。归属判据 = gen_versions 的版本号前缀
    （= 该模块相对仓根的完整子路径）⇒ 与 Module.Path 一一对应。
    """
    # 远端 tag **一次性并发读完**（开了缓存，后面的读取全是命中）：
    # 原来每仓串行读一次（24 仓实测 ≈35s），而 audit 把本脚本当子进程跑
    # ⇒ 整条审计的耗时大头。并发 8 路实测 35s → 6s。
    #
    # 失败直接抛（fail-loud，#100 的口径）：BOM 的地板与版本必须来自
    # **真实读到的**远端 tag，读不到就不能生成产物。
    enable_tag_cache()
    prefetch_remote_tags(MODULES, BASE)

    out: list[dict] = []
    for repo in MODULES:
        d = str(repo_dir(repo, BASE))
        # **一仓只读一次**远端 tag 列表，本轮复用给「根模块最新版本」和「嵌套模块 tag」。
        # 原来这里是 latest_version(repo) + _remote_tags(repo) 各读一次 ⇒ 每仓 2 次
        # HTTPS 读（audit 的 --check 子进程因此要 48 次读，拖慢整条审计）。
        tags = _remote_tags(repo)
        ver = next((t for t in tags if "/" not in t), None)
        if not ver:
            continue
        ensure_local_tag(d, ver)
        # **breaking 索引按模块拆分**（改造A）：根模块拿无前缀的条目，
        # 嵌套模块拿「前缀 == 自己子路径」的条目 ⇒ 嵌套的 breaking 不再
        # 折叠进宿主（下游能直接定位到具体模块）。
        breaking_by_module = breaking_index_by_module(repo)
        out.append({"repo": repo, "path": f"github.com/{ORG}/{repo}", "version": ver,
                    "go": go_floor(d, ver), "nested": False,
                    "breaking": breaking_for(breaking_by_module, "", ver)})
        nested: list[dict] = []
        for sub, tag in select_nested_tags(repo, d, tags):
            nested.append({"repo": repo, "path": f"github.com/{ORG}/{repo}/{sub}",
                           "version": tag.rsplit("/", 1)[1], "go": go_floor(d, tag), "nested": True,
                           "breaking": breaking_for(breaking_by_module, sub,
                                                          tag.rsplit("/", 1)[1])})
        out.extend(sorted(nested, key=lambda x: x["path"]))
    return out


def next_version() -> str:
    r = subprocess.run(["git", "-C", OEKIT_DIR, "tag", "-l"], capture_output=True, text=True)
    tags = sorted(
        (t for t in r.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)),
        key=lambda t: tuple(int(x) for x in t[1:].split(".")),
    )
    if not tags:
        return "v0.1.0"
    major, minor, patch = (int(x) for x in tags[-1][1:].split("."))
    return f"v{major}.{minor}.{patch + 1}"


def _go_str(s: str) -> str:
    """Python 字符串 → Go 字符串字面量（含引号/反斜杠/控制字符的转义）。

    breaking 条目的原文里有中文、引号（backtick 包裹的 WithNNS）、反斜杠 ⇒ 必须转义，
    否则生成的 matrix.go 编译不过（gofmt 会报错，但错误信息难读）。
    JSON 的转义规则与 Go 字符串字面量兼容（\\" \\\\ \\n \\t \\uXXXX）⇒ 直接用。
    """
    return json.dumps(str(s), ensure_ascii=False)


def render(modules: list[dict], version: str, generated_at: str) -> dict[str, str]:
    # require 地板按模块路径排序（与 go list 输出序一致）。注意：本仓不可跑
    # go mod tidy——require 无对应 import，tidy 会清掉；见文件头说明。
    go_mod = [f"module {MODULE_PATH}", "", f"go {BOM_GO}", "",
              "// ---- oekit BOM：矩阵兼容快照的地板 require（勿手改/勿 tidy，见 README）----",
              "require ("]
    for m in sorted(modules, key=lambda x: x["path"]):
        go_mod.append(f"\t{m['path']} {m['version']}")
    go_mod.append(")")

    lines = [
        "// Code generated by matrix-tools/scripts/gen_matrix_go.py; DO NOT EDIT.",
        f"// 生成时间 {generated_at}；口径与 https://oekit.github.io/versions.json 同源（各仓远端 tag）。",
        "",
        "// Package oekit 声明 oekit 矩阵当前互相兼容的模块版本快照（BOM，bill of materials）",
        "// （[oekit/.github#8](https://github.com/oekit/.github/issues/8)）。",
        "//",
        "// go.mod 以 require 地板钉住全部模块（含嵌套模块）版本：",
        "//",
        "//\tgo get github.com/oekit/oekit@latest   // 一次拉齐一组经矩阵 CI 验证的兼容版本",
        "//",
        "// 下游 MVS 被抬到快照版本（已用的模块升到快照口径，不会静默落到互不兼容的旧组合）；",
        "// 不需要整组对齐时按需 go get 各模块即可，本包零强制。长期跟随快照的服务可在某处",
        "// import 本包（如 /version 端点读 Versions()），让 require 常驻 go.mod。",
        "//",
        "// 快照由 matrix-tools/scripts/gen_matrix_go.py 从各仓远端 tag 生成；矩阵版本变化后",
        "// 重跑生成并打新 tag（audit 有新鲜度门禁）。",
        "//",
        "// 本包自带 **breaking 索引**：每个模块带自己的破坏性变更条目",
        "//（嵌套模块的条目只归它自己，不折叠进宿主仓）⇒ 下游不必再查站点。",
        "//",
        "// ⚠️ **v0.2.0 起本包的导出全局被降级为不可变对外面**（Breaking 变更）：",
        "//   Matrix / Versions 由**导出的可变值**改成**返回副本的函数**。",
        "//   旧写法 oekit.Versions[\"x\"] / range oekit.Matrix → 新写法 oekit.Versions()[\"x\"] / range oekit.Matrix()。",
        "//   原因：旧的导出的可变 map/切片可被消费方并发改写 ⇒ go run -race 实测 DATA RACE，",
        "//   且可能触发不可 recover 的 fatal error: concurrent map read and map write。",
        "//",
        "// 本仓 go.mod 的 require 是无 import 的地板，勿跑 go mod tidy（会清掉；误跑后重跑生成器）。",
        "package oekit",
        "",
        "// Version 是本快照自身的版本（与仓库 git tag 一致）。",
        f'const Version = "{version}"',
        "",
        "// GeneratedAt 是快照生成时间（UTC）。",
        f'const GeneratedAt = "{generated_at}"',
        "",
        "// Breaking 是一条破坏性变更记录（来自各仓 CHANGELOG 的破坏性段）。",
        "type Breaking struct {",
        "\t// Version 是这次破坏性变更发生的版本：",
        "\t//   根模块   → \"v0.5.0\"",
        "\t//   嵌套模块 → \"<相对仓根的完整子路径>/v0.2.14\"（如 \"obs/v0.2.14\"）",
        "\t// 前缀 = 该模块相对宿主仓根的子路径，与嵌套 tag 前缀同形",
        "\t// （如 observability/tracing/instrumentation/kitexotel/v0.7.7）。",
        "\tVersion string",
        "\t// Line 是 CHANGELOG 里的原文条目。",
        "\tLine string",
        "}",
        "",
        "// Module 是矩阵中一个模块（根模块或嵌套模块）的版本声明。",
        "type Module struct {",
        "\t// Path 是 Go module 路径（github.com/oekit/pkg、github.com/oekit/logx/hlog）。",
        "\tPath string",
        "\t// Repo 是宿主仓库短名（pkg、logx）。",
        "\tRepo string",
        "\t// Version 是本快照钉住的版本。",
        "\tVersion string",
        "\t// Go 是该模块声明的最低 go 版本。",
        "\tGo string",
        "\t// Breaking 是**该模块自己**的破坏性变更条目。",
        "\t// 嵌套模块的破坏性变更**只**出现在它自己的条目里（不折叠进宿主仓）。",
        "\tBreaking []Breaking",
        "}",
        "",
        "// matrixSnapshot 是全部模块的版本声明（**包内私有**）：根模块按 matrix.py",
        "// 依赖自底向上序，嵌套模块（logx/hlog、bootstrap/nacos 等）跟随宿主仓按路径字母序。",
        "// 对外只给**副本**（见 Matrix()）——包内状态不可被消费方改写。",
        "var matrixSnapshot = []Module{",
    ]
    for m in modules:
        brk = m.get("breaking") or []
        brk_txt = ", ".join(f"{{Version: {_go_str(b['version'])}, Line: {_go_str(b['line'])}}}"
                            for b in brk)
        lines.append(
            f'\t{{Path: {_go_str(m["path"])}, Repo: {_go_str(m["repo"])}, '
            f'Version: {_go_str(m["version"])}, Go: {_go_str(m["go"])}, '
            f'Breaking: []Breaking{{{brk_txt}}}}},')
    lines += [
        "}",
        "",
        "// versionsSnapshot 是按 module 路径索引的版本（**包内私有**，对外只给副本）。",
        "var versionsSnapshot = map[string]string{",
    ]
    for m in sorted(modules, key=lambda x: x["path"]):
        lines.append(f'\t{_go_str(m["path"])}: {_go_str(m["version"])},')
    lines += [
        "}",
        "",
        "// breakingSnapshot 是按 module 路径索引的破坏性条目（**包内私有**，对外只给副本）。",
        "var breakingSnapshot = map[string][]Breaking{",
    ]
    for m in sorted(modules, key=lambda x: x["path"]):
        brk = m.get("breaking") or []
        if not brk:
            continue
        brk_txt = ", ".join(f"{{Version: {_go_str(b['version'])}, Line: {_go_str(b['line'])}}}"
                            for b in brk)
        lines.append(f'\t{_go_str(m["path"])}: []Breaking{{{brk_txt}}},')
    lines += [
        "}",
        "",
        "// copyBreaking 复制破坏性条目切片（返回副本 ⇒ 消费方改不到包内状态）。",
        "func copyBreaking(in []Breaking) []Breaking {",
        "\tif in == nil {",
        "\t\treturn nil",
        "\t}",
        "\tout := make([]Breaking, len(in))",
        "\tcopy(out, in)",
        "\treturn out",
        "}",
        "",
        "// Matrix 返回全部模块的版本声明（**副本**）。",
        "//",
        "// ⚠️ v0.2.0 起 Matrix 是**函数**（旧版是导出的可变切片）：",
        "// 迁移写法：range oekit.Matrix → range oekit.Matrix()；len(oekit.Matrix) → len(oekit.Matrix())。",
        "func Matrix() []Module {",
        "\tout := make([]Module, len(matrixSnapshot))",
        "\tfor i, m := range matrixSnapshot {",
        "\t\tout[i] = m",
        "\t\tout[i].Breaking = copyBreaking(m.Breaking)",
        "\t}",
        "\treturn out",
        "}",
        "",
        "// Versions 返回按 module 路径索引的版本（**副本**）。",
        "//",
        "// ⚠️ v0.2.0 起 Versions 是**函数**（旧版是导出的可变 map）：",
        "// 迁移写法：oekit.Versions[\"x\"] → oekit.Versions()[\"x\"]。",
        "func Versions() map[string]string {",
        "\tout := make(map[string]string, len(versionsSnapshot))",
        "\tfor k, v := range versionsSnapshot {",
        "\t\tout[k] = v",
        "\t}",
        "\treturn out",
        "}",
        "",
        "// VersionOf 返回模块路径对应的快照版本，未收录返回空串。",
        "//",
        "// 并发安全：versionsSnapshot 在包初始化后**只读** ⇒ 并发读无 race。",
        "func VersionOf(module string) string { return versionsSnapshot[module] }",
        "",
        "// BreakingOf 返回某模块**自己**的破坏性变更条目（**副本**）；未收录返回 nil。",
        "//",
        "// 嵌套模块的条目只在这里出现（不折叠进宿主仓）：",
        "//   BreakingOf(\"github.com/oekit/bootstrap/obs\") → 该嵌套模块自己的破坏性变更。",
        "func BreakingOf(module string) []Breaking {",
        "\treturn copyBreaking(breakingSnapshot[module])",
        "}",
        "",
        "// HasBreaking 报告某模块有没有破坏性变更。",
        "func HasBreaking(module string) bool { return len(breakingSnapshot[module]) > 0 }",
        "",
        "// BreakingModules 返回**有**破坏性变更的模块路径（**副本**，按矩阵序）。",
        "func BreakingModules() []string {",
        "\tout := []string{}",
        "\tfor _, m := range matrixSnapshot {",
        "\t\tif len(breakingSnapshot[m.Path]) > 0 {",
        "\t\t\tout = append(out, m.Path)",
        "\t\t}",
        "\t}",
        "\treturn out",
        "}",
        "",
    ]
    return {"go.mod": "\n".join(go_mod) + "\n",
            "matrix.go": "\n".join(lines)}


def gofmt_text(src: str) -> str:
    """渲染结果过一遍 gofmt（map 字面量对齐等），保证产物与 gofmt -w 一致。"""
    r = subprocess.run(["gofmt"], input=src, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gofmt 失败：{r.stderr[:300]}")
    return r.stdout


def existing_const(name: str, default: str) -> str:
    """读现有 matrix.go 里的字符串常量（Version / GeneratedAt），无则 default。
    --check 复用它们：只比对版本数据，时间戳与自身版本不算漂移。"""
    path = os.path.join(OEKIT_DIR, "matrix.go")
    if os.path.exists(path):
        m = re.search(r'const ' + name + r' = "([^"]+)"', open(path, encoding="utf-8").read())
        if m:
            return m.group(1)
    return default


def main() -> int:
    check = "--check" in sys.argv
    try:
        modules = collect()
    except RemoteReadError as e:
        # **读不到 ≠ 没有**（#79）：这不是「BOM 陈旧」，是「判不了」。
        # 用**独立的退出码 2** 表达，audit.py 据此把它归到「未核对」而不是「漂移」——
        # 混在一起报，一次网络抖动就会变成一条假的矩阵漂移。
        #
        # 以前这里是裸 traceback：audit 只取子进程输出的**最后一行**当漂移消息，
        # 于是报告里出现的是「❌ matrix.RemoteReadError: registry: 远端 tag 读不到…」，
        # 读的人看不出这是读取失败（实测 2026-10-25）。
        print(f"读不到远端 tag ⇒ 无法核对 BOM 新鲜度（**不是**「BOM 陈旧」）：{e}",
              file=sys.stderr)
        print("先修好网络/凭证再重试；若 HTTPS 被代理挡住，可用 clone 的 origin 兜底"
              "（matrix.remote_tags 会依次试 canonical HTTPS 与 origin）。", file=sys.stderr)
        return EXIT_UNREADABLE
    version = sys.argv[sys.argv.index("--version") + 1] if "--version" in sys.argv else existing_const("Version", "v0.1.0")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    generated_at = existing_const("GeneratedAt", now) if check else now
    files = render(modules, version, generated_at)
    files["matrix.go"] = gofmt_text(files["matrix.go"])
    if not check:
        ensure_clean_all([OEKIT_DIR], "gen_matrix_go")  # 产物必须落在已提交基线上
    changed = []
    for name, content in files.items():
        target = os.path.join(OEKIT_DIR, name)
        old = open(target, encoding="utf-8").read() if os.path.exists(target) else None
        if old != content:
            changed.append(name)
        if not check:
            open(target, "w", encoding="utf-8").write(content)

    if check:
        # **proxy 复核 Go 字段**（改造B）：产物里的地板必须与 proxy 上发布出去的
        # .mod 一致（嵌套模块读错文件时产物会低报，且没有任何其它门禁能看见）。
        drift, unchecked = check_go_against_proxy(modules)
        if drift:
            print(f"❌ oekit BOM 的 Go 地板与 proxy 上的 .mod 不符 {len(drift)} 条：")
            for d in drift[:8]:
                print(f"   - {d}")
            print("   （跑 gen_matrix_go.py 重生成并打新 tag）")
            return 1
        if unchecked:
            # ★ 复核没跑完 ⇒ 退出码 2（matrix-tools#261）：「未核对」不是「一致」。
            #   旧写法这里只 ⚠ 一句，然后照样「✓ 一致」+ exit 0 ⇒ proxy 全读不到时
            #   这道复核静默零覆盖，报告还是绿的（#79「判据不可用即红」的破口；
            #   audit.py 阶段 6 恰恰按退出码判「BOM 新鲜度 ✓」）。口径与上方
            #   collect() 的远端 tag 读不到一致：判不了 ⇒ EXIT_UNREADABLE(2)。
            print(f"⚠️ proxy 复核未完成：{len(unchecked)}/{len(modules)} 个模块未核对"
                  f"（读不到 ≠ 不符，但也**不是**一致 ⇒ 判不了）：{', '.join(unchecked[:6])}"
                  f"{' ……' if len(unchecked) > 6 else ''}")
            print("   先修网络/凭证，或用 OEKIT_GO_PROXY 指定可达的镜像后重跑。")
            return EXIT_UNREADABLE
        if changed:
            print(f"❌ oekit BOM 陈旧（跑 gen_matrix_go.py 重生成并打新 tag）：{', '.join(changed)}")
            return 1
        print(f"✓ oekit BOM 与远端 tag 一致（{version}，{len(modules)} 模块）")
        return 0

    d = subprocess.run(["git", "-C", OEKIT_DIR, "status", "--porcelain"],
                       capture_output=True, text=True).stdout.strip()
    print(f"✓ oekit BOM 已生成（{version}，{len(modules)} 模块）；变更文件：\n{d or '  （无变化）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
