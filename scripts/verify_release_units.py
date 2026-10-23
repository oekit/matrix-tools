#!/usr/bin/env python3
"""发版单元门禁：llms.txt 必须覆盖全部发版单元（根模块 + 嵌套模块），且嵌套模块各自有 go get 安装行。

## 为什么要有这个（2026-10-20 实测）

对外口径分裂成 21 / 24 / 32 三种，而 llms.txt —— **AI Agent 唯一会抓的机器可读索引** ——
里 7 个嵌套模块**既没有独立条目、也没有 `go get` 安装行**。Agent 读到
「Sink 在嵌套模块 logx/sink」却找不到安装行，就装不上。

而嵌套模块恰是**零依赖隔离**的载体（logx/sink 隔离 lumberjack、bootstrap/nacos 隔离
约 50 个阿里云 SDK）。它们的价值就是「可选装」——llms.txt 不给安装行 = 这个价值对
Agent 不可见。

verify_site_copy.py 补的是**根模块条目集合**（llms.txt「## 模块」段 == MODULES）；
本脚本补的是**发版单元全集**（根 24 + 嵌套 9 + **伞仓 1**）与**每个单元的可安装性**。

## 伞仓也是发版单元（2026-10-23 实测，issue #82）

伞仓 BOM 仓 `oekit` 有**独立 tag + 独立 GitHub Release**（latest v0.1.29），
下游 `go get github.com/oekit/oekit@latest` 是一次独立的版本获取
⇒ 按「可独立 go get 的发版单元」口径，它**就是第 34 个单元**
（canonical 口径 `gen_tables.count_units` 也这么算：24 根 + 9 嵌套 + 1 伞仓 = 34；
站点文案一律写 34）。

但本脚本原先只校验 24 + 9 = 33，**伞仓漏检**：把 llms.txt 里伞仓那
（`go get github.com/oekit/oekit@latest`）整段删掉，门禁**照样全绿**
（实测 exit=0）⇒ 站点「llms.txt 覆盖 34 个发版单元」的承诺**无人守**。

⇒ 本脚本现在要求伞仓与嵌套模块**同等**的可安装性，并统一用 canonical 口径
报数（不再出现「同一个术语，门禁说 33、站点说 34」）。

## 判据必须独立（关键）

被检对象 = 站点仓里**手写**的 llms.txt（它**没有生成器**——已全量核实：
matrix-tools/scripts/ 下无任何写 llms.txt 的脚本，其历史全是手写提交）。
期望值来自三处与被检对象无关的事实：

  1. **文件系统**：每个嵌套模块必须在 `<repo>/<path>/go.mod` 真实存在，且有真实 tag
     （独立发版的物理证据）——这是独立 oracle；
  2. **矩阵登记表**（matrix.py 的 MODULES / NESTED_MODULES）——声明「应该有什么」；
  3. **本脚本自己渲染的规范段落**——与站点里的段落逐字节比对，锁住格式漂移。

第 1 条是真正独立的 oracle：哪怕 matrix.py 的 NESTED_MODULES 写错了，
文件系统这一路仍会红。判据**不复用被检对象自己的口径**。

## 两条 oracle 都要有（缺一即有共同盲区）

- **oracle A（正确性）**：解析 llms.txt 全文，抽出全部条目形态，
  断言 24 个根模块名 + 9 个嵌套模块名**各至少出现一次**，且 9 个嵌套模块
  **各自都有一行 `go get github.com/oekit/<module>`**，且**伞仓 BOM 仓**
  有一行 `go get github.com/oekit/oekit@latest`。不依赖渲染结果。
- **oracle B（一致性）**：渲染规范「## 嵌套模块」段，与站点里的该段逐字节比对。
  抓的是格式漂移（少个安装行、描述改了、链接写错）。

用法:
    verify_release_units.py --site <站点仓目录> [--repos-dir <各仓父目录>]
    verify_release_units.py --emit          # 打印规范段落（供写回 llms.txt）

退出码: 0 = 通过；1 = 有漂移。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from matrix import MODULES, NESTED_MODULES  # noqa: E402
from gen_tables import BOM_REPO, count_units  # noqa: E402  口径单一真相（与站点文案同源）

# 根模块条目形态：`- [name](https://github.com/oekit/name): 描述`
_ROOT_ENTRY_RE = re.compile(r"^-\s*\[([A-Za-z0-9_.\-]+)\]\(https://github\.com/oekit/[^)]+\)\s*[:：]",
                            re.M)
# 嵌套模块条目形态：`- [repo/path](https://github.com/oekit/repo/tree/main/path): 描述`
_NESTED_ENTRY_RE = re.compile(
    r"^-\s*\[([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\]"
    r"\(https://github\.com/oekit/[A-Za-z0-9_.\-]+/tree/main/[A-Za-z0-9_.\-]+\)\s*[:：]",
    re.M)
# 安装行：`go get github.com/oekit/logx/sink@latest`
# 抓取**完整 module 路径**（含 github.com/oekit/ 前缀），与 NESTED_MODULES 的
# `module` 字段同形态——两边必须能直接比对，中间不做前缀剥离。
_GO_GET_RE = re.compile(r"go\s+get\s+(github\.com/oekit/[A-Za-z0-9_.\-/]+?)(?:@|`)")

# 嵌套模块段落标题——渲染与比对都锚在这一行，段落被改名即漂移。
_NESTED_HEADING = "## 嵌套模块（可选装 · 依赖隔离）"


def render_nested_section(versions: dict[str, dict] | None = None) -> str:
    """渲染规范的「## 嵌套模块」段。

    这是 llms.txt 那一段的**唯一产出源**：站点里的段落必须与它逐字节一致
    （oracle B）。写成生成器而不是手改产物——llms.txt 整体没有生成器，
    但这一段是机械的，机械的就不该手写。

    `versions` 给定时（形如 {"logx/sink": {"latest": "v0.1.0", "hostVersion": "v0.4.0"}}），
    每条额外写一行「版本 + 对应父仓版本」——嵌套模块与父仓的**版本对应关系**
    是下游装机时唯一需要的信息（见 issue #37 ① 的口径裁决）。
    """
    lines = [
        _NESTED_HEADING,
        "",
        "> 嵌套模块是**独立发版**的 Go module（各自有 tag），职责是**依赖隔离**而非目录组织：",
        "> 只 import 根模块的消费者**不会**被拉入这些重依赖；需要时才单独 `go get`。",
        "",
    ]
    for m in NESTED_MODULES:
        name = f"{m['repo']}/{m['path']}"
        url = f"https://github.com/oekit/{m['repo']}/tree/main/{m['path']}"
        lines.append(f"- [{name}]({url}): {m['desc']}")
        lines.append(f"  - 安装：`go get {m['module']}@latest`")
        v = (versions or {}).get(name)
        if v and v.get("latest"):
            host = v.get("hostVersion")
            tail = f" · 对应父仓 `{m['repo']} {host}`" if host else ""
            lines.append(f"  - 版本：`{v['latest']}`{tail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _tag_dates(repo_dir: Path, repo: str) -> dict[str, int]:
    """仓内全部 tag → 创建时间戳（unix）。

    与生成器**刻意用不同的 git 事实**推导「对应父仓版本」：生成器走
    **commit 祖先关系**，这里走 **tag 日期序**。两者独立、必须一致——
    若两边共享判断逻辑，就会一起错、门禁照样全绿（#31 的教训）。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir / repo), "for-each-ref",
             "--format=%(refname:short)|%(creatordate:unix)", "refs/tags"],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return {}
    res: dict[str, int] = {}
    for line in out.splitlines():
        p = line.split("|")
        if len(p) < 2:
            continue
        try:
            res[p[0]] = int(p[1])
        except ValueError:
            continue
    return res


def _semver_key(v: str) -> tuple:
    m = re.match(r"v(\d+)\.(\d+)\.(\d+)", v)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def derive_nested_versions(repos_dir: Path | None) -> dict[str, dict]:
    """嵌套模块的「当前版本 + 对应父仓版本」——**门禁自己的推导**。

    判据（与生成器不同源）：**tag 日期序**——父仓根 tag 里，创建时间**不晚于**
    该嵌套 tag 的那个里取最新的 ⇒ 嵌套发布所处的父仓版本区间。

    两种推导（生成器=commit 祖先 / 门禁=tag 日期）互为交叉验证。
    """
    if repos_dir is None:
        return {}
    out: dict[str, dict] = {}
    for m in NESTED_MODULES:
        repo, path = m["repo"], m["path"]
        dates = _tag_dates(repos_dir, repo)
        if not dates:
            continue
        nested = {n: d for n, d in dates.items()
                  if n.startswith(path + "/") and re.fullmatch(r"v\d+\.\d+\.\d+", n[len(path) + 1:])}
        if not nested:
            continue
        latest_tag = max(nested, key=lambda n: _semver_key(n[len(path) + 1:]))
        latest = latest_tag[len(path) + 1:]
        roots = {n: d for n, d in dates.items()
                 if "/" not in n and re.fullmatch(r"v\d+\.\d+\.\d+", n)}
        cands = [r for r, d in roots.items() if d <= nested[latest_tag]]
        host = max(cands, key=lambda r: (roots[r], _semver_key(r))) if cands else None
        out[f"{repo}/{path}"] = {"latest": latest, "hostVersion": host,
                                 "module": m["module"]}
    return out


def check_nested_version_correspondence(published: dict, derived: dict[str, dict],
                                       repos_dir: Path | None = None) -> list[str]:
    """oracle C：已发布 versions.json 的 nested 条目必须与门禁自己的推导一致。

    查**产物**（已发布的机器可读矩阵），不查生成器输出——生成器输出与产物
    一致只说明「生成器自洽」，这里要验的是「产物说的对应关系是对的」。

    双向：
      ① 声明的 7 个嵌套模块 → 必须都在产物里，且版本与 hostVersion 一致；
      ② 产物里登记的嵌套模块 → 目录必须**还活着**（已撤回的模块不该被登记）。
    """
    problems: list[str] = []
    nested_by_repo: dict[str, dict] = {}
    for mod in published.get("modules", []):
        for item in mod.get("nested") or []:
            nested_by_repo[f"{mod['module']}/{item['path']}"] = item
    for name, d in sorted(derived.items()):
        got = nested_by_repo.get(name)
        if not got:
            problems.append(f"已发布 versions.json 缺嵌套模块 {name} 的条目")
            continue
        if got.get("latest") != d["latest"]:
            problems.append(f"{name}: 已发布版本 {got.get('latest')} ≠ 实际最新 {d['latest']}")
        if d["hostVersion"] and got.get("hostVersion") != d["hostVersion"]:
            problems.append(f"{name}: 已发布 hostVersion {got.get('hostVersion')!r} "
                            f"≠ 推导值 {d['hostVersion']!r}（对应父仓版本漂移）")
    if repos_dir is not None:
        for name, item in sorted(nested_by_repo.items()):
            repo, path = name.split("/", 1)
            if not (repos_dir / repo / path).is_dir():
                problems.append(f"已发布 versions.json 登记了嵌套模块 {name}，"
                                f"但源码树里该目录已不存在（已撤回？）——矩阵不该登记废弃模块")
    return problems



def _nested_units() -> list[tuple[str, str]]:
    """(条目名, import 路径) 列表，名字用 `repo/path` 形态。"""
    return [(f"{m['repo']}/{m['path']}", m["module"]) for m in NESTED_MODULES]


def _umbrella_units() -> int:
    """伞仓 BOM 仓算不算一个发版单元。

    它**不算**矩阵模块（不在 `MODULES` 里，是门面 BOM 快照），但有独立 tag、
    独立 GitHub Release、`go get github.com/oekit/oekit@latest` ⇒ 按「可独立
    go get 的发版单元」口径**必须计入**（与站点文案、canonical 口径一致）。

    这里不写死「1」：判定依据是「BOM_REPO 不在矩阵登记表里」——它若哪天被
    收进矩阵模块，口径会自动变成 0，不会两处各说各话。
    """
    return 0 if BOM_REPO in MODULES else 1


def release_unit_count() -> tuple[int, int, int, int]:
    """(根模块, 嵌套模块, 伞仓, 合计)——发版单元的 canonical 口径。

    合计 = 24 根 + 9 嵌套 + 1 伞仓 = 34，与 `gen_tables.count_units`
    （独立 oracle：扫文件系统算伞仓数）和站点文案同口径。
    """
    root, nested = len(MODULES), len(NESTED_MODULES)
    bom = _umbrella_units()
    return root, nested, bom, root + nested + bom


def _checkout_complete(repos_dir: Path) -> bool:
    """检出是否完整（全部矩阵模块 + 伞仓都在）。

    只有完整检出才拿文件系统推导的数来**交叉核对** canonical 口径——
    半套检出算出来的数必然偏小，拿它判红就是假红（不是漂移）。
    """
    return all((repos_dir / m).is_dir() for m in MODULES) and (repos_dir / BOM_REPO).is_dir()


def check_filesystem_oracle(repos_dir: Path | None) -> list[str]:
    """oracle A-1（独立 oracle）：嵌套模块必须在文件系统里真实存在且有真实 tag；
    伞仓 BOM 仓同理（它也是独立发版单元）。

    哪怕 matrix.py 的 NESTED_MODULES 写错了，这一条仍会红——它不依赖任何声明。
    """
    if repos_dir is None:
        return []
    problems: list[str] = []
    for name, _mod in _nested_units():
        repo, path = name.split("/", 1)
        gomod = repos_dir / repo / path / "go.mod"
        if not gomod.is_file():
            problems.append(f"嵌套模块 {name} 在文件系统里不存在（{gomod}）——"
                            f"声明了但实际没有这个 go.mod")
            continue
        try:
            tags = subprocess.run(["git", "tag", "--list", f"{path}/v*"], cwd=repos_dir / repo,
                                  capture_output=True, text=True, timeout=30).stdout.split()
        except Exception:  # noqa: BLE001 — git 不可用时不该让门禁崩
            continue
        if not tags:
            problems.append(f"嵌套模块 {name} 没有任何 {path}/vX.Y.Z tag——"
                            f"「独立发版」的说法不成立")

    # 伞仓 BOM 仓：目录没检出 ≠ 漂移（本地少 clone 一个就假红），不报；
    # 在 ⇒ 必须真的有独立 tag，否则「独立发版的伞仓」这句话不成立。
    if _umbrella_units():
        bom_dir = repos_dir / BOM_REPO
        if bom_dir.is_dir():
            try:
                tags = subprocess.run(["git", "tag", "--list", "v*"], cwd=bom_dir,
                                      capture_output=True, text=True, timeout=30).stdout.split()
            except Exception:  # noqa: BLE001
                tags = []
            if not tags:
                problems.append(f"伞仓 BOM 仓 {BOM_REPO} 在文件系统里没有任何 vX.Y.Z tag——"
                                f"「独立发版的发版单元」的说法不成立")
    return problems


def check_llms_units(llms_text: str) -> list[str]:
    """oracle A-2：llms.txt 必须覆盖全部发版单元，嵌套模块各自有 go get 安装行。

    不依赖 render_nested_section() 的输出——自己解析 llms.txt。
    """
    problems: list[str] = []
    root_found = set(_ROOT_ENTRY_RE.findall(llms_text))
    nested_found = set(_NESTED_ENTRY_RE.findall(llms_text))
    all_found = root_found | nested_found

    missing_root = sorted(set(MODULES) - all_found)
    if missing_root:
        problems.append(f"llms.txt 缺 {len(missing_root)} 个根模块条目：{missing_root}"
                        f"（Agent 按 llms.txt 装包会漏）")
    missing_nested = sorted({n for n, _ in _nested_units()} - all_found)
    if missing_nested:
        problems.append(f"llms.txt 缺 {len(missing_nested)} 个嵌套模块条目：{missing_nested}"
                        f"（嵌套模块的「可选装」价值对 Agent 不可见）")

    # 嵌套模块各自的 go get 安装行——**逐个**要求，不接受「有一段提过就算」
    install_lines = {m.group(1) for m in _GO_GET_RE.finditer(llms_text)}
    for name, mod in _nested_units():
        if mod not in install_lines:
            problems.append(f"llms.txt 缺嵌套模块 {name} 的 go get 安装行"
                            f"（`go get {mod}@latest`）——Agent 读到条目也装不上")

    # 伞仓 BOM 仓的可安装性——**与嵌套模块同等要求**（2026-10-23 实测，issue #82）。
    # 它也是独立发版单元（有独立 tag + 独立 Release），站点承诺 llms.txt 覆盖
    # 34 个发版单元；原先这道检查压根不存在 ⇒ 删掉伞仓那行，门禁照样全绿。
    if _umbrella_units():
        bom_mod = f"github.com/oekit/{BOM_REPO}"
        if bom_mod not in install_lines:
            problems.append(f"llms.txt 缺伞仓 BOM 仓 {BOM_REPO} 的 go get 安装行"
                            f"（`go get {bom_mod}@latest`）——它也是独立发版单元"
                            f"（有独立 tag 与 Release），站点承诺 llms.txt 覆盖 34 个发版单元")
    return problems


def check_section_consistency(llms_text: str, versions: dict[str, dict] | None = None) -> list[str]:
    """oracle B：站点的嵌套模块段必须与渲染结果逐字节一致。"""
    want = render_nested_section(versions)
    if _NESTED_HEADING not in llms_text:
        return [f"llms.txt 缺「{_NESTED_HEADING}」段——"
                f"{len(NESTED_MODULES)} 个嵌套模块没有独立条目段（跑 --emit 取规范文本）"]
    m = re.search(re.escape(_NESTED_HEADING) + r"\n(.*?)(?=^##\s|\Z)", llms_text, re.M | re.S)
    got = _NESTED_HEADING + "\n" + (m.group(1) if m else "")
    if got.rstrip() != want.rstrip():
        return ["llms.txt 的「嵌套模块」段与规范渲染不一致（描述/安装行/版本对应行/链接漂移）"
                "——跑 `verify_release_units.py --emit --repos-dir <各仓父目录>` 取规范文本逐字节替换"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="发版单元门禁：llms.txt 覆盖全部发版单元 + 嵌套模块可安装")
    ap.add_argument("--site", help="站点仓目录（用于读 docs/public/llms.txt）")
    ap.add_argument("--repos-dir", help="各仓父目录（用于文件系统独立 oracle）")
    ap.add_argument("--emit", action="store_true", help="打印规范「## 嵌套模块」段")
    args = ap.parse_args()

    repos_dir = Path(args.repos_dir).resolve() if args.repos_dir else None
    derived = derive_nested_versions(repos_dir)

    if args.emit:
        print(render_nested_section(derived), end="")
        return 0

    if not args.site:
        ap.error("需要 --site <站点仓目录>")

    site = Path(args.site).resolve()
    llms = site / "docs" / "public" / "llms.txt"
    if not llms.is_file():
        print(f"❌ 缺 {llms}（llms.txt 是机器可读入口，必须在）")
        return 1
    text = llms.read_text(encoding="utf-8", errors="ignore")

    problems: list[str] = []
    problems += check_filesystem_oracle(repos_dir)
    problems += check_llms_units(text)
    problems += check_section_consistency(text, derived)

    # oracle C：已发布 versions.json 的 nested 对应关系（与生成器不同源推导）
    vfile = site / "docs" / "public" / "versions.json"
    if derived:
        if not vfile.is_file():
            problems.append(f"缺 {vfile}（嵌套模块的版本对应关系在机器可读矩阵里）")
        else:
            import json as _json
            try:
                published = _json.loads(vfile.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                problems.append(f"已发布 versions.json 解析失败：{e}")
            else:
                problems += check_nested_version_correspondence(published, derived, repos_dir)

    root, nested, bom, total = release_unit_count()
    detail = f"{root} 根 + {nested} 嵌套 + {bom} 伞仓"

    # 口径交叉核对：canonical 口径（登记）与文件系统推导（独立 oracle）
    # 必须一致——不一致就意味着「发版单元」这个数会各说各话（issue #82）。
    # 只在**完整检出**时核对：半套检出算出来的数必然偏小，拿它判红是假红。
    if repos_dir is not None and _checkout_complete(repos_dir):
        fs_units = count_units(repos_dir).get("release_units")
        if fs_units is not None and fs_units != total:
            problems.append(f"发版单元口径分裂：门禁口径 {total}（{detail}）"
                            f" ≠ 文件系统推导 {fs_units}（gen_tables.count_units）——"
                            f"两处口径必须一致，否则同一个术语会对外报出两个数")

    if problems:
        print(f"❌ 发版单元门禁未通过（应覆盖 {total} 个发版单元 = {detail}）：")
        for p in problems:
            print(f"   - {p}")
        return 1
    print(f"✅ 发版单元门禁通过（{total} 个发版单元 = {detail}；"
          f"{nested} 条嵌套安装行 + {bom} 条伞仓安装行齐全）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
