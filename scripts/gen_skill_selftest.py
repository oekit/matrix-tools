#!/usr/bin/env python3
"""gen_skill.py 数据与渲染逻辑自测（**无副作用、不联网**）。

覆盖四件事（skill 是 AI 的选型入口，数据错 = AI 全矩阵走错路，值得独立自测）：
  1. 渲染确定性：五个 render_* 各渲染两遍逐字节相等（数据区有随机成分即当场爆）
  2. 场景表死键：SCENARIOS 每行 mods ∈ MODULES（模块改名/删仓后表里留死键在这拦）
  3. LAYERS 恰好覆盖 MODULES：新增模块忘分层在这拦（audit 同款检查，双保险）
  4. REDLINES_SHORT 出处：前五条判据的关键 token 必须能在 HARD_RULES_MD 找到
     ——红线精华与单源漂移（改了 rules.py 没同步精华）在这拦
  5. 场景安装行（#341 P2）：每场景组合行三出口在场（skill/site/llms 规范段）、
     行格式 go get + 全 oekit 前缀、计数文案派生自 len()（不许硬编码 24）
  6. 场景覆盖（#344 P4 准入三问问2）：MODULES ⊆ SCENARIOS.mods，missing 必须空；
     准入三问文本投影在 rules.md

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

# 1b) 嵌套模块安装行正确性：module 字段已是全路径，模板不得再拼 org 前缀
# （端到端测试实测抓到 go get github.com/oekit/github.com/oekit/... 双前缀 → 照抄必 404）
import re as _re
sys.path.insert(0, str(HERE.parent))
from matrix import NESTED_MODULES  # noqa: E402
_mods_md = gen_skill.render_modules()
for n in NESTED_MODULES:
    want = f"go get {n['module']}@latest"
    expect(want in _mods_md, f"modules.md 缺正确安装行：{want}")
expect("github.com/oekit/github.com/oekit" not in _mods_md,
       "modules.md 安装行含双 org 前缀（模板重复拼接）")
expect("github.com/oekit/github.com/oekit" not in gen_skill.render_site(),
       "站点选型指南安装行含双 org 前缀")

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

# 4b) 场景安装行三出口在场 + 行格式（#341 P2 单源纪律）
_skill_md = gen_skill.render_skill()
_site_md = gen_skill.render_site()
_llms_sec = gen_skill.render_llms_scenario_section()
expect("### 场景安装行（复制即用）" in _skill_md, "SKILL.md 缺场景安装行块")
expect("### 场景安装行（复制即用）" in _site_md, "selection.md 缺场景安装行块")
expect(gen_skill.check_llms_scenarios(_llms_sec) == [], "规范 llms 场景段自检不通过")
expect(gen_skill.check_llms_scenarios("## 文档\nx") != [], "llms 缺场景段未判红")
_n_scenes = 0
for _g, _rows in gen_skill.SCENARIOS:
    for _r in _rows:
        _n_scenes += 1
        _line = gen_skill.scenario_install_line(_r["mods"])
        expect(_line in _skill_md, f"SKILL.md 缺场景行：{_line}")
        expect(_line in _site_md, f"selection.md 缺场景行：{_line}")
        expect(_line.split()[:2] == ["go", "get"], f"场景行非 go get：{_line}")
        expect(all(x.startswith("github.com/oekit/") for x in _line.split()[2:]),
               f"场景行含非 oekit 路径：{_line}")
expect(_n_scenes > 0, "SCENARIOS 为空")

# 4c) 计数文案派生（P0 教训：AI 面硬编码计数必漂）——渲染产物里不许出现
#     「写死的模块数」与 len(MODULES) 不一致的情况
# 扫描产物里所有「N 独立模块」声明，必须全部 == len(MODULES)——
# 不写死当前值（写死 23 在模块数变 24 时又漂，P0 的老路）
import re as _re2
for _m in _re2.finditer(r"(\d+) 独立模块", _skill_md):
    expect(int(_m.group(1)) == len(MODULES),
           f"SKILL.md 计数 {_m.group(1)} ≠ len(MODULES)={len(MODULES)}")
expect(f"{len(MODULES)} 独立模块" in _skill_md, "SKILL.md 计数未派生自 len(MODULES)")

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

# 7) 场景覆盖机判（#344 准入三问问2）：MODULES ⊆ SCENARIOS.mods，missing 必须空
_cov_ok, _cov_miss = gen_skill.scenario_coverage(sorted(MODULES))
expect(not _cov_miss, f"场景覆盖缺口（准入三问问2，#344）：{_cov_miss}")
expect("network" in _cov_ok, "network 未入任何场景（#344 首个覆盖缺口应已被补上）")
# 合成夹具：未覆盖的模块必须进 missing（函数不是恒空的摆设）
_c1, _m1 = gen_skill.scenario_coverage(["pkg", "不存在的仓"])
expect(_c1 == ["pkg"] and _m1 == ["不存在的仓"],
       f"scenario_coverage 合成夹具行为异常：covered={_c1} missing={_m1}")
# 准入三问投影在 rules.md（AI 提新模块时看得到判据）
from rules import MODULE_ADMISSION_3Q_MD  # noqa: E402
expect(MODULE_ADMISSION_3Q_MD in gen_skill.render_rules(),
       "准入三问未投影进 rules.md（render_rules 漏拼）")

if failures:
    print(f"❌ gen_skill 自测失败 {len(failures)} 项：")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("✓ gen_skill 自测通过（渲染确定性 / 死键 / LAYERS 覆盖 / 红线出处 / frontmatter / 场景覆盖 #344）")
