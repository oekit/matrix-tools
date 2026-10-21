#!/usr/bin/env python3
"""把消费面依赖画像写成站点产物：docs/public/deps.json + 快速开始页的表格。

## 为什么要有这个（2026-10-20 实测）

矩阵对外说「按需取用，一个 import 即用」——但**代价**此前对外部团队是黑盒：
装 `github.com/oekit/registry` 会拖进 60 个第三方模块，装 `github.com/oekit/pkg`
一个都不拖。选型的人看不到这个差别，只能装完才发现。

本脚本把 `consumer_probe.py` 测出来的画像变成站点可读的事实：

- `docs/public/deps.json`——机器可读（AI Agent 一次抓取即知全矩阵的依赖代价）；
- 快速开始页的「装一个模块要拖多少东西」表——人读。

## 数据来源与刷新

数据来自**消费方的真实模块图**（`go get <unit>@latest` 后 `go list -m all`），
不是任何仓的 go.mod 声明。刷新由 matrix-tools 的周巡检（`audit.yml` 的
`consumer` job）产出 profile 后开 PR 回站点——**产物 PR 纪律**：生成器改的是
站点，站点 PR 与数据一起走，不直接推 main。

用法:
    python3 scripts/gen_deps_doc.py --profile consumer-profile.json --site <站点仓目录>

退出码: 0 = 成功；1 = 参数/数据不对。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

START = "<!-- deps-table:start -->"
END = "<!-- deps-table:end -->"

# 表格里的一句人话说明——按实测的第三方模块数分档，**不写死具体数字**
# （数字随矩阵演进，硬写会变成新的陈旧文案）。
def _tier(third: int) -> str:
    if third == 0:
        return "零第三方"
    if third <= 5:
        return "轻"
    if third <= 20:
        return "中"
    return "重"


def build_table(results: list[dict]) -> str:
    """渲染 markdown 表格（按第三方数升序——轻的在前，选型的人先看到代价小的）。"""
    rows = sorted(results, key=lambda r: (len(r.get("third_party") or []), r.get("unit", "")))
    lines = ["| 模块 | 装它会拉进 | 其中第三方 | 量级 |",
             "|---|---|---|---|"]
    for r in rows:
        unit = r.get("unit", "")
        tp = len(r.get("third_party") or [])
        total = len([p for p in (r.get("paths") or []) if not p.startswith("probe_")])
        lines.append(f"| [`{unit}`](https://github.com/oekit/{unit}) | {total} | {tp} | {_tier(tp)} |")
    return "\n".join(lines)


SECTION_HEADER = """## 装它要拖多少东西（消费方实测）

「按需取用」的另一面是**代价**：装一个模块会连带拉进多少东西。
下表是**实测值**——在空消费方模块里 `go get github.com/oekit/<单元>@latest`
之后数 `go list -m all` 得到的模块图（不含消费方自身）。
不是任何仓 `go.mod` 的声明，声明不算证据。

量级分档：零第三方 / 轻（≤5）/ 中（≤20）/ 重（>20）。
机器可读版：[deps.json](https://oekit.github.io/deps.json)。

由 `matrix-tools/scripts/consumer_probe.py` 测量、`gen_deps_doc.py` 生成，
随矩阵周巡检刷新。"""


def build_json(results: list[dict], generated_at: str) -> str:
    units = []
    for r in results:
        unit = r.get("unit", "")
        units.append({
            "unit": unit,
            "module": f"github.com/oekit/{unit}",
            "ok": bool(r.get("ok")),
            "consumerModules": len([p for p in (r.get("paths") or []) if not p.startswith("probe_")]),
            "thirdParty": len(r.get("third_party") or []),
            "thirdPartyList": sorted(r.get("third_party") or []),
            "ownModules": sorted(p for p in (r.get("own") or [])),
        })
    return json.dumps({"generatedAt": generated_at, "org": "oekit", "units": units},
                      ensure_ascii=False, indent=2) + "\n"


def splice_table(md: str, table: str) -> str:
    """把「小节 + 表格」写进标记块（幂等：重复跑结果一致）。"""
    block = f"{START}\n{SECTION_HEADER}\n\n{table}\n{END}"
    if START in md and END in md:
        pre = md.split(START)[0]
        post = md.split(END, 1)[1]
        return f"{pre}{block}{post}"
    return md.rstrip() + "\n\n" + block + "\n"


def _selftest() -> int:
    """幂等性自测：重复跑不得让表格重复堆积。"""
    sample = [
        {"unit": "pkg", "ok": True, "paths": ["probe_pkg", "github.com/oekit/pkg"],
         "third_party": [], "own": ["github.com/oekit/pkg"]},
        {"unit": "registry", "ok": True, "paths": ["probe_registry", "github.com/oekit/registry"]
         + [f"example.com/dep/{i}" for i in range(25)],
         "third_party": [f"example.com/dep/{i}" for i in range(25)],
         "own": ["github.com/oekit/registry"]},
    ]
    t = build_table(sample)
    assert "零第三方" in t and "重" in t, f"分档不对：{t}"
    assert t.index("| [`pkg`]") < t.index("| [`registry`]"), "轻的必须排在前面"
    once = splice_table("# 标题\n\n正文\n", t)
    twice = splice_table(once, t)
    assert once == twice, "重复跑必须幂等"
    assert twice.count("| [`pkg`]") == 1, "表格不能重复堆积"
    j = json.loads(build_json(sample, "2026-01-01T00:00:00Z"))
    assert j["units"][0]["unit"] == "pkg" and j["units"][1]["thirdParty"] == 25, f"JSON 形状不对：{j}"
    print("✅ gen_deps_doc 自测通过（分档 / 幂等 / JSON 形状）")
    return 0


def _strip_ts(text: str) -> str:
    """去掉 generatedAt 字段——**只有数据变了**才需要开 PR。

    时间戳每周都变；若把它算进差异，周巡检会每周开一个内容无实质变化的 PR。
    """
    try:
        d = json.loads(text)
    except Exception:
        return text
    d.pop("generatedAt", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成站点消费面依赖画像（deps.json + 表格）")
    ap.add_argument("--profile", default="", help="consumer_probe.py 产出的 JSON")
    ap.add_argument("--site", default="", help="站点仓目录")
    ap.add_argument("--generated-at", default="", help="生成时间戳（ISO8601）")
    ap.add_argument("--check", action="store_true",
                    help="只比对不写：数据（忽略时间戳）与站点现状一致则 exit 0，否则 exit 1")
    ap.add_argument("--selftest", action="store_true", help="幂等性自测（不联网）")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not args.profile or not args.site:
        ap.print_help()
        return 2

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    results = profile.get("results") or []
    if not results:
        print("profile 里没有 results，无法生成")
        return 1
    site = Path(args.site).resolve()
    table = build_table(results)
    generated_at = args.generated_at or "unknown"

    out_json = site / "docs" / "public" / "deps.json"
    guide = site / "docs" / "guide" / "getting-started.md"
    new_json = build_json(results, generated_at)
    new_guide = splice_table(guide.read_text(encoding="utf-8"), table) if guide.is_file() else None

    if args.check:
        changed = []
        if not out_json.is_file() or _strip_ts(out_json.read_text(encoding="utf-8")) != _strip_ts(new_json):
            changed.append(str(out_json))
        if new_guide is not None and guide.read_text(encoding="utf-8") != new_guide:
            changed.append(str(guide))
        if changed:
            print("⚠ 站点消费面画像与实测不一致：")
            for c in changed:
                print(f"  · {c}")
            return 1
        print("✓ 站点消费面画像与实测一致（忽略时间戳）")
        return 0

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(new_json, encoding="utf-8")
    print(f"✓ 已写 {out_json}")
    if guide.is_file():
        guide.write_text(new_guide, encoding="utf-8")
        print(f"✓ 已更新 {guide}")
    else:
        print(f"[warn] {guide} 不存在，跳过表格写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
