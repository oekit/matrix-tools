#!/usr/bin/env python3
"""为 21 个活跃仓生成 oekit 适配版 AGENTS.md（共享规范 + 本仓信息）。

⚠️ 重跑会整文件覆写各仓 AGENTS.md——各仓文件生成后已在人工维护演进
（如 data 的 cached/twolevel 硬规则段、门面壳同步规则等，本工具模板不含），
**仅在新增仓冷启动或明确决定全量重生成时使用**；日常修订请直接改各仓文件。
"""
import os
import pathlib
import re
import subprocess

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from matrix import MODULES, DESC, ensure_clean_all  # 一句话定位唯一事实源在 matrix.py
from rules import HARD_RULES_MD  # 硬规则速览单源（2026-11-12 自本文件 BODY 提取，与 gen_skill 共享）
BASE = pathlib.Path(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
def repo_path(r): return BASE / r

# DESC 本地副本已删除（2026-10-16 发现漂移：istio/gorm/redis 死键 + 各仓口径过期）
# 硬规则速览同理已提取至 rules.py（2026-11-12，字节级比对验证）——本文件不再持有第二份

BODY = "\n" + HARD_RULES_MD + "\n## 本仓范围\n"

def pkg_list(repo):
    d = repo_path(repo)
    pkgs = []
    for p in sorted(d.rglob("*.go")):
        if ".git" in p.parts or "_test.go" in p.name or "/testdata/" in str(p):
            continue
        rel = p.parent.relative_to(d)
        if str(rel) not in pkgs:
            pkgs.append(str(rel))
    return "、".join(f"`{p}/`" for p in sorted(set(pkgs)))

if __name__ != "__main__":
    raise SystemExit("gen_agents 会整文件覆写 AGENTS.md——请显式运行，不要 import")

# 拆雷（2026-10-19）：本工具是整文件覆写型，而各仓 AGENTS.md 已大量人工演进
# （data 的 cached/twolevel 硬规则段、门面壳同步规则等，模板不含）。日常修订
# 一律直接改各仓文件；本工具仅保留一个用途：新仓冷启动，且必须显式点名：
#   python3 scripts/gen_agents.py --new-repo <name>
_targets = None
_argv = sys.argv[1:]
if "--new-repo" in _argv:
    _name = _argv[_argv.index("--new-repo") + 1]
    if _name not in MODULES:
        raise SystemExit(f"未知模块仓 {_name}（先加 matrix.py MODULES/DESC 再冷启动）")
    _targets = [_name]
else:
    raise SystemExit(
        "gen_agents 会整文件覆写 AGENTS.md，已禁用全量生成。\n"
        "  新仓冷启动：python3 scripts/gen_agents.py --new-repo <name>\n"
        "  日常修订：直接改各仓 AGENTS.md（人工增量段本工具模板不含，覆写即丢失）")

_dirs = [repo_path(_r) for _r in _targets if repo_path(_r).exists()]
ensure_clean_all(_dirs, "gen_agents")  # 覆写前必须无未提交改动——人工增量段丢了就是真丢

count = 0
for repo in _targets:
    d = repo_path(repo)
    tags = subprocess.run(["git", "-C", str(d), "tag", "-l", "--sort=-v:refname"],
                          capture_output=True, text=True).stdout.split()
    lt = tags[0] if tags else "v0.1.0"
    content = f"""# AGENTS.md — oekit/{repo}

> {DESC[repo]}
> 当前版本：`{lt}` · module：`github.com/oekit/{repo}` · org：[oekit = Open Easy Kit](https://oekit.github.io)

## 本仓包含
{pkg_list(repo)}
{BODY}
"""
    (d / "AGENTS.md").write_text(content)
    count += 1
    print(f"✓ oekit/{repo}/AGENTS.md（冷启动模板，人工增量段请自行补齐）", flush=True)
print(f"=== {count} 份 AGENTS.md ===")
