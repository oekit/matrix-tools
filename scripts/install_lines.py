#!/usr/bin/env python3
"""嵌套发版单元 README 安装行的判据层（matrix-tools#250）。

**背景**：「版本行纪律」此前只覆盖根模块——refresh_versions 的正则按
「仓库名 + @」定位，对嵌套安装行（`go get github.com/oekit/logx/sink@v0.1.0`）
永不匹配；audit 的安装行检查同款正则 ⇒ 12 个嵌套发版单元的安装行**既没人
刷新、也没人核对**（2026-11-08 实测：7 行带版本号的嵌套安装行 7/7 陈旧，
最坏落后 15 个 patch）。本模块把 audit 侧的判据收成**可注入的纯函数**
（与 scan_repo_floors / scan_repo_nested_floors 同款手法），audit.py 接线。

**两种漂移（互为反面）**：

1. **正向**：README 里本仓嵌套单元的带版本安装行 ≠ 该嵌套单元的远端最新
   tag ⇒ 落后（照着装会拿到旧版本）。
2. **反向**：README 里出现带版本号的 `go get github.com/oekit/<path>@vX.Y.Z`，
   但 `<path>` 既不是本仓根模块、也不是本仓任何嵌套模块 ⇒ **登记外路径**
   ——刷新与核对都只认登记内的路径，登记外的出了事没人知道
   （oekit#165「登记表漏登记」的同族；这条反向判据正是漏登记的探测器）。

**嵌套单元集合从哪来**：从**仓内嵌套 go.mod 的 module 行**推导（文件系统
事实），**不依赖 NESTED_MODULES 登记表**——判据不得与被检对象共享判断逻辑
（登记表漏登记时判据照样能看见；这正是反向判据存在的意义）。

纯函数（不碰网络）：`latest_of(module_path) -> str | None` 由调用方注入；
读不到返回 None ⇒ 正向判据跳过该行（「读不到」由调用方按 #79 口径显式记，
这里不猜）。

自测：scripts/install_lines_selftest.py（夹具 + 变异自证）。
"""
from __future__ import annotations

import re
from pathlib import Path

# 带版本号的安装行：`go get github.com/oekit/logx/sink@v0.1.0`。
# ⚠️ 只认全三段 vX.Y.Z：`@latest` 这类不带版本号的行没有「落后」可言，
# 不在本判据范围（refresh_versions 同样只改带版本号的行）。
INSTALL_LINE_RE = re.compile(
    r"go get github\.com/oekit/([A-Za-z0-9][A-Za-z0-9._/-]*)@(v\d+\.\d+\.\d+)")


def nested_units_of(root: Path) -> dict[str, str]:
    """本仓全部嵌套发版单元 {模块路径（不含 github.com/oekit/ 前缀）: tag 前缀}。

    从仓内**嵌套 go.mod 的 module 行**推导（文件系统事实，不依赖登记表）：
    - 键 = 模块路径去掉 `github.com/oekit/` 前缀（如 `logx/sink`）；
    - 值 = **tag 前缀 = 相对仓根的完整子路径**（Go 的子目录模块规则：
      `github.com/oekit/logx/sink` 的 tag 是 `sink/v0.1.6`——前缀里**没有**
      仓名那段；⚠️ 不是模块路径本身：拿 `logx/sink/` 当过滤前缀会一个 tag
      都匹配不到 ⇒ 正向判据静默失效。多层嵌套同理：kitexotel 的前缀是
      `observability/tracing/instrumentation/kitexotel`，不是最后一段）。
    vendor / .git / testdata 下的 go.mod 不是本仓模块，排除。
    """
    units: dict[str, str] = {}
    root = Path(root)
    for gm in sorted(root.glob("**/go.mod")):
        if gm.parent == root:
            continue  # 仓根本身是根模块，不是嵌套单元
        rel = gm.relative_to(root)
        if any(p in (".git", "vendor", "node_modules", "testdata") for p in rel.parts):
            continue
        try:
            first = gm.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        m = re.match(r"module\s+github\.com/oekit/(.+?)\s*$", first)
        if not m:
            continue
        rest = m.group(1)
        units[rest] = gm.parent.relative_to(root).as_posix()  # tag 前缀 = 相对仓根的完整子路径
    return units


def check_readme_install_lines(readme_text: str, repo: str, nested_units: set[str],
                               latest_of) -> list[str]:
    """README 安装行的正反向核对，返回漂移消息列表（空 = 无漂移）。

    - `nested_units`：本仓嵌套单元路径集合（nested_units_of 的键集）；
    - `latest_of(module_path) -> str | None`：嵌套单元的远端最新版本号
      （注入 ⇒ 自测不联网）；None ⇒ 该行跳过（读不到由调用方显式记，#79）。
    """
    problems: list[str] = []
    for m in INSTALL_LINE_RE.finditer(readme_text):
        path, ver = m.group(1), m.group(2)
        if path == repo:
            continue  # 根安装行：audit 逐仓判据 6 已核对（同一套纪律，不重复报）
        if path not in nested_units:
            problems.append(
                f"{repo}: README 安装行指向**登记外路径** github.com/oekit/{path}@{ver}"
                f"（不是本仓根模块，也不是本仓任何嵌套模块）⇒ 刷新与核对都不覆盖它"
                f"（matrix-tools#250 反向判据；登记表漏登记即此形态）")
            continue
        lt = latest_of(path)
        if lt is None:
            continue
        if ver != lt:
            problems.append(
                f"{repo}: 嵌套安装行 github.com/oekit/{path}@{ver} ≠ 最新 {lt}"
                f"（跑 refresh_versions.py）")
    return problems
