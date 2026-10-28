#!/usr/bin/env python3
"""dependabot.yml 的 gomod 目录条目：解析 + 存在性校验（单一事实源）。

**背景（data#39 实测，2026-10-22）**：data 的 `all/` 聚合嵌套模块在 v0.5.0
「门面化」时已 retract 并**删掉目录**，但 `.github/dependabot.yml` 仍为它注册着
`directory: /all` ⇒ dependabot weekly 的该条 job **每周必失败**
（`dependency_file_not_found: No go.mod files found`）。

这个失败形态特别难被发现，原因是它**同时满足三条**：
  1. **不报 build 错**——它根本不在 go 编译链路上，只在 dependabot 自己的 run 里红；
  2. **不会自愈**——判据（`/all/go.mod` 不存在）恒成立，重跑无用，也没有别的仓
     会推本仓 main 来触发重跑；
  3. **每周才出现一次**——两次巡检之间夹着整整一周。

而 retract 一个嵌套模块时的清理动作，散落在**好几个地方**：matrix.py 的
`NESTED_MODULES` 登记表、README 的说明、生成器（gen_versions.py /
gen_matrix_go.py）的显式跳过……都同步过了，**唯独这个 yaml 没人看**。
⇒ 本模块就是那道「没人看的地方」的门禁。

**判据只认 gomod ecosystem**：`github-actions` / `npm` 等 ecosystem 不需要
go.mod，按它们判会全矩阵假红（实测 `.github` / `matrix-tools` / 站点仓都是
`github-actions` 或 `npm` 生态）。

**为什么用行解析而不是 yaml 库**：dependabot.yml 的结构极简（`package-ecosystem`
与 `directory` 成对出现），而 matrix-tools 不该为这一个门禁引入 pyyaml 依赖
（CI 的 python 环境是裸装的）。`directories:`（复数，dependabot 的批量写法）
不参与判定——无法逐条核验，猜错会假红。
"""
from __future__ import annotations

import re
from pathlib import Path

# `- package-ecosystem: gomod`（含前导 `- `）与 `directory: /sink`
_ECOSYSTEM_RE = re.compile(r"^\s*-\s*package-ecosystem:\s*(\S+)")

# 扫嵌套模块时跳过的目录名（vendor/testdata 之类不是嵌套模块）
_SKIP_DIRS = {"vendor", "testdata", "third_party", "examples", "node_modules"}
_DIRECTORY_RE = re.compile(r"^\s*directory:\s*(\S+)")


def gomod_dirs(text: str) -> list[str]:
    """从 dependabot.yml 原文里取出 gomod ecosystem 的 directory 条目。

    返回值保留原样（带前导 `/`，如 `/`、`/sink`），由调用方决定怎么拼路径。
    """
    out: list[str] = []
    ecosystem: str | None = None
    for line in text.splitlines():
        m = _ECOSYSTEM_RE.match(line)
        if m:
            ecosystem = m.group(1)
            continue
        m = _DIRECTORY_RE.match(line)
        if m and ecosystem == "gomod":
            out.append(m.group(1).strip("\"'"))
    return out


def check_dirs(repo_root: Path, text: str) -> list[str]:
    """校验 dependabot.yml 里的 gomod 目录：必须存在，且目录里有 go.mod。

    返回**问题描述列表**（空列表 = 通过）——判定与报错措辞都放这里，
    调用方只负责把每条塞进自己的 issues 列表。

    三种失败分开报，因为修法不同：
      - 目录不存在 ⇒ 该嵌套模块已 retract/删目录 ⇒ 残留登记该删；
      - 目录在但没有 go.mod ⇒ 路径写错 ⇒ 该改；
      - **仓里有嵌套模块但 dependabot.yml 没登记** ⇒ 依赖被静默冻结（#118）。
    """
    problems = list(check_registered_dirs(repo_root, text))
    problems += check_nested_registered(repo_root, text)
    return problems


def check_registered_dirs(repo_root: Path, text: str) -> list[str]:
    """已登记的目录必须存在、且目录里有 go.mod（反向：登记了却已 retract）。"""
    problems: list[str] = []
    repo = repo_root.name
    for d in gomod_dirs(text):
        target = repo_root / d.lstrip("/")
        if not target.is_dir():
            problems.append(
                f"{repo}: dependabot.yml 注册了不存在的目录 {d}"
                f"（该嵌套模块已 retract/删目录？残留条目会让 dependabot 每周必失败 "
                f"dependency_file_not_found，且不会自愈）")
        elif not (target / "go.mod").exists():
            problems.append(
                f"{repo}: dependabot.yml 的 {d} 下没有 go.mod"
                f"（gomod ecosystem 扫不到模块，该条 dependabot job 必失败）")
    return problems


def check_nested_registered(repo_root: Path, text: str) -> list[str]:
    """**每个**嵌套模块都必须被登记 ⇒ 未登记 ⇒ 其依赖被静默冻结（#118）。

    为什么必须查这一侧：旧门禁只查「登记了却不存在」⇒ **「存在却没登记」零覆盖**
    ⇒ 嵌套模块的依赖**永远不会被 dependabot 提议升级**，而且**没有任何报错**
    （实测 2026-10-28：全矩阵 6 个嵌套模块（bootstrap/nacos、bootstrap/obs、
    registry/nacos、registry/nacosimpl、kitex/istio、kitex 的 kitexotel）
    未登记 ⇒ 共 130 项间接依赖冻结；形态是「直接依赖都是最新、落后全在间接依赖」
    ⇒ 冻结的影响落在**下游的构建产物**上，不是「go.mod 不好看」）。

    判据：仓内所有含 go.mod 的**子目录**（含多层路径）都必须在 gomod 条目里出现。
    排除：仓根（`/` 已隐含登记）、`vendor` / `testdata` / `third_party` / `examples`
    / 隐藏目录。
    """
    problems: list[str] = []
    repo = repo_root.name
    registered = {d.lstrip("/").rstrip("/") for d in gomod_dirs(text)}
    for mod in nested_module_dirs(repo_root):
        if mod in registered:
            continue
        problems.append(
            f"{repo}: 嵌套模块 {mod} 没在 dependabot.yml 里登记（gomod ecosystem）"
            f"⇒ 它的依赖**永远不会被提议升级**，且没有任何报错"
            f"（补一条 directory: /{mod} 即可）")
    return problems


def nested_module_dirs(repo_root: Path) -> list[str]:
    """仓内所有嵌套模块的相对路径（含多层，如 observability/tracing/x/kitexotel）。"""
    out: list[str] = []
    for go_mod in sorted(repo_root.rglob("go.mod")):
        rel = go_mod.parent.relative_to(repo_root)
        parts = rel.parts
        if not parts:
            continue  # 仓根（已由 directory: / 覆盖）
        if any(p in _SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        out.append(str(rel))
    return out
