#!/usr/bin/env python3
"""gen_skill.py 数据与渲染逻辑自测（**无副作用、不联网**）。

覆盖四件事（skill 是 AI 的选型入口，数据错 = AI 全矩阵走错路，值得独立自测）：
  1. 渲染确定性：五个 render_* 各渲染两遍逐字节相等（数据区有随机成分即当场爆）
  2. 场景表死键：SCENARIOS 每行 mods ∈ MODULES（模块改名/删仓后表里留死键在这拦）
  3. LAYERS 恰好覆盖 MODULES：新增模块忘分层在这拦（audit 同款检查，双保险）
  4. REDLINES_SHORT 出处：前五条判据的关键 token 必须能在 HARD_RULES_MD 找到
     ——红线精华与单源漂移（改了 rules.py 没同步精华）在这拦

附：skills/*/SKILL.md 的 frontmatter name 与目录名一致（release/sweep 是手写件，
没有生成器兜底，拼错 name = ZCode 装载后触发不了）。

用法：python3 scripts/gen_skill_selftest.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import gen_skill  # noqa: E402
from matrix import MODULES  # noqa: E402
from rules import HARD_RULES_MD  # noqa: E402

failures: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


# 1) 渲染确定性
for name, fn in (("skill", gen_skill.render_skill), ("rules", gen_skill.render_rules),
                 ("modules", gen_skill.render_modules), ("pkg", gen_skill.render_pkg),
                 ("site", gen_skill.render_site)):
    expect(fn() == fn(), f"render_{name}() 两次渲染不一致（数据区含非确定成分）")

# 2) 场景表死键
mod_set = set(MODULES)
bad = sorted({m for _, rows in gen_skill.SCENARIOS for r in rows for m in r.get("mods", [])
              if m not in mod_set})
expect(not bad, f"SCENARIOS 引用了矩阵不存在的模块（死键）：{bad}")
empty_mods = [r["scene"] for _, rows in gen_skill.SCENARIOS for r in rows if not r.get("mods")]
expect(not empty_mods, f"SCENARIOS 这些行没填 mods（audit 校验靠它）：{empty_mods}")

# 3) LAYERS 恰好覆盖 MODULES
layered = {m for _, mods in gen_skill.LAYERS for m in mods}
expect(layered == mod_set,
       f"LAYERS 与 MODULES 不一致：缺 {sorted(mod_set - layered)} 多 {sorted(layered - mod_set)}")
dupes = [m for _, mods in gen_skill.LAYERS for m in mods
         if sum(m in ms for _, ms in gen_skill.LAYERS) > 1]
expect(not dupes, f"LAYERS 里重复登记的模块：{dupes}")

# 4) 红线精华与单源的从属关系（token 级，不做全等）
TOKENS = ("BizError", "mw.Fail", "Delegating", "GoSafe", "Close(ctx)", "userFn", "XxxOption")
missing = [t for t in TOKENS if t not in HARD_RULES_MD]
expect(not missing, f"rules.py 单源里找不到这些 token（提取漂移？）：{missing}")
for t in ("BizError", "Delegating", "GoSafe", "Close(ctx)", "XxxOption"):
    blob = "\n".join(gen_skill.REDLINES_SHORT)
    expect(t in blob, f"REDLINES_SHORT 缺关键判据 {t}（红线精华失守）")

# 5) frontmatter name 与目录名一致（三个 skill）
for s in ("oekit", "oekit-release", "oekit-sweep"):
    p = gen_skill.MT_ROOT / "skills" / s / "SKILL.md"
    if not p.exists():
        failures.append(f"skills/{s}/SKILL.md 不存在")
        continue
    head = p.read_text(encoding="utf-8").split("---", 2)
    expect(len(head) >= 3, f"skills/{s}/SKILL.md frontmatter 结构不完整")
    m = re.search(r"^name:\s*(\S+)\s*$", head[1], re.M) if len(head) >= 3 else None
    expect(bool(m) and m.group(1) == s, f"skills/{s}: name 与目录名不一致（{m and m.group(1)}）")

# 6) description 长度上限（skill 触发描述超长会被装载端截断）
blob = "\n".join(gen_skill.REDLINES_SHORT)
desc_m = re.search(r"description: >-\n((?:  .*\n)+)", gen_skill.render_skill())
expect(bool(desc_m), "SKILL.md frontmatter 缺 description")
if desc_m:
    desc = " ".join(ln.strip() for ln in desc_m.group(1).splitlines())
    expect(len(desc) <= 1024, f"SKILL description 超长（{len(desc)} > 1024）")

if failures:
    print(f"❌ gen_skill 自测失败 {len(failures)} 项：")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("✓ gen_skill 自测通过（渲染确定性 / 死键 / LAYERS 覆盖 / 红线出处 / frontmatter）")
