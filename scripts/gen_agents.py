#!/usr/bin/env python3
"""为活跃仓生成 oekit 适配版 AGENTS.md（共享规范 + 本仓信息）。

⚠️ 重跑会整文件覆写各仓 AGENTS.md——各仓文件生成后已在人工维护演进
（如 data 的 cached/twolevel 硬规则段、门面壳同步规则等，本工具模板不含），
**仅在新增仓冷启动或明确决定全量重生成时使用**；日常修订请直接改各仓文件。

准入过闸（2026-11-28 #344，演进评审 P4）：冷启动新仓前必须显式作答准入三问
（rules.MODULE_ADMISSION_3Q_MD 单源）——三个 `--admit-*` 旗缺一即拒（fail-closed），
答案嵌入新仓 AGENTS.md「准入三问」段留档。脚本流收在 main() 里，本模块可被
selftest 安全 import（纯函数在守卫之上，无副作用）。
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from matrix import MODULES, DESC, ensure_clean_all  # 一句话定位唯一事实源在 matrix.py
from rules import HARD_RULES_MD, MODULE_ADMISSION_3Q_MD  # 硬规则 + 准入三问双单源
BASE = pathlib.Path(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
def repo_path(r): return BASE / r

# DESC 本地副本已删除（2026-10-16 发现漂移：istio/gorm/redis 死键 + 各仓口径过期）
# 硬规则速览同理已提取至 rules.py（2026-11-12，字节级比对验证）——本文件不再持有第二份

BODY = "\n" + HARD_RULES_MD + "\n## 本仓范围\n"

# ---------- 准入三问过闸（#344；纯函数在 __main__ 守卫之上，供 selftest 导入） ----------

ADMISSION_FLAGS = {
    "--admit-lifecycle": "独立生命周期",
    "--admit-scenario": "使用场景",
    "--admit-api": "稳定 API",
}


def parse_admission(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    """解析三个 --admit-* 旗 → (answers 旗标→答案, missing 缺失的问题标签)。

    fail-closed 口径：旗缺失、值为空、值是下一个旗（串位）都算 missing——
    过闸只认「显式、非空、位置正确」的答案，不认占位。
    """
    answers: dict[str, str] = {}
    missing: list[str] = []
    for flag, label in ADMISSION_FLAGS.items():
        if flag not in argv:
            missing.append(label)
            continue
        i = argv.index(flag)
        val = argv[i + 1] if i + 1 < len(argv) else ""
        if val and not val.startswith("--"):
            answers[label] = val
        else:
            missing.append(label)  # 空值或吃到了下一个旗（串位）
    return answers, missing


def render_admission_section(answers: dict[str, str]) -> str:
    """冷启动 AGENTS.md 的留档段：谁凭什么是独立仓，白纸黑字。"""
    return (
        "\n## 准入三问（冷启动时作答，matrix-tools#344 留档）\n\n"
        f"1. **独立生命周期**：{answers['独立生命周期']}\n"
        f"2. **使用场景**：{answers['使用场景']}\n"
        f"3. **稳定 API**：{answers['稳定 API']}\n"
    )


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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # 拆雷（2026-10-19）：本工具是整文件覆写型，而各仓 AGENTS.md 已大量人工演进
    # （data 的 cached/twolevel 硬规则段、门面壳同步规则等，模板不含）。日常修订
    # 一律直接改各仓文件；本工具仅保留一个用途：新仓冷启动，且必须显式点名：
    #   python3 scripts/gen_agents.py --new-repo <name> --admit-* ×3
    if "--new-repo" not in argv:
        raise SystemExit(
            "gen_agents 会整文件覆写 AGENTS.md，已禁用全量生成。\n"
            "  新仓冷启动：python3 scripts/gen_agents.py --new-repo <name> \\\n"
            "      --admit-lifecycle \"<独立生命周期理由>\" \\\n"
            "      --admit-scenario \"<使用场景>\" --admit-api \"<稳定 API 承诺>\"\n"
            "  日常修订：直接改各仓 AGENTS.md（人工增量段本工具模板不含，覆写即丢失）")
    name = argv[argv.index("--new-repo") + 1] if argv.index("--new-repo") + 1 < len(argv) else ""
    if name not in MODULES:
        raise SystemExit(f"未知模块仓 {name}（先加 matrix.py MODULES/DESC 再冷启动）")

    # 准入三问过闸（#344）：缺一即拒，打印三问全文——答不出就不该有这个仓。
    answers, missing = parse_admission(argv)
    if missing:
        sys.stderr.write(MODULE_ADMISSION_3Q_MD + "\n")
        raise SystemExit(f"准入三问答不全（缺：{'、'.join(missing)}）"
                         f"——先答三问再冷启动（#344 fail-closed）")

    dirs = [repo_path(name)] if repo_path(name).exists() else []
    ensure_clean_all(dirs, "gen_agents")  # 覆写前必须无未提交改动——人工增量段丢了就是真丢

    d = repo_path(name)
    tags = subprocess.run(["git", "-C", str(d), "tag", "-l", "--sort=-v:refname"],
                          capture_output=True, text=True).stdout.split()
    lt = tags[0] if tags else "v0.1.0"
    content = f"""# AGENTS.md — oekit/{name}

> {DESC[name]}
> 当前版本：`{lt}` · module：`github.com/oekit/{name}` · org：[oekit = Open Easy Kit](https://oekit.github.io)

## 本仓包含
{pkg_list(name)}
{BODY}{render_admission_section(answers)}"""
    (d / "AGENTS.md").write_text(content)
    print(f"✓ oekit/{name}/AGENTS.md（冷启动模板，人工增量段请自行补齐；准入三问已留档）",
          flush=True)
    print("=== 1 份 AGENTS.md ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
