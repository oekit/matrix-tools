#!/usr/bin/env python3
"""架构总览图生成器：从 matrix.py（MODULES/DESC 唯一事实源）渲染五能力层 SVG。

产物（light + dark 两主题，字节一致地写入两处仓）：
  <base>/oekit/docs/assets/architecture{-dark}.svg   # oekit 主仓（README <picture> 嵌入）
  <base>/<site>/docs/public/architecture{-dark}.svg  # 站点（/architecture{,-dark}.svg；暗色由站点 CSS 切换）

用法：
  python3 scripts/gen_diagram.py           # 写盘（不提交）
  python3 scripts/gen_diagram.py --push    # 写盘 + 两仓各自 commit & push
  python3 scripts/gen_diagram.py --check   # 只比对，漂移 exit 1（audit 用）

模块增删时本图随 matrix.py 自动更新；新增模块记得补 SHORT 短描述与 LAYERS 归层，
漏补会在生成时立即报错（fail-loud），不会产出缺模块的图。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, SITE_REPO, repo_dir  # noqa: E402

BASE = Path(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", ".."))).resolve()
HOMEPAGE_REPO = "oekit"

# ---------- 展示层配置（与 matrix.py 的 DESC 解耦：DESC 面向表格全量，SHORT 面向芯片一行） ----------
SHORT = {
    "bootstrap": "组合根 · App/Run · healthz",
    "hertz": "SSE/health · CRUD Handler · OTel",
    "kitex": "治理热更 · BizError 桥接 · xDS 熔断",
    "middleware": "标准栈 · contrib 桥接",
    "workflow": "串行/并行/异步编排 · Hook",
    "kitex-examples": "echo 示例 · 治理配置",
    "httpx": "LB · 熔断/重试 · OTel 透传",
    "nns-endpoint": "per-service 熔断 · 换实例重试",
    "network": "WS/NATS/MQ · 连接工具",
    "nns": "发现/配置/实例缓存 · 降级",
    "registry": "注册发现核心 + Nacos 实现",
    "gormx": "连接池增强 · CRUD Service",
    "redisx": "客户端 · 缓存后端 · 分布式锁",
    "data": "LocalCache · 缓存检查器",
    "timertask": "分片定时 · 心跳续约",
    "logx": "统一门面 · 多后端 · Sink",
    "tracing": "OpenTelemetry · trace_id",
    "metrics": "Prometheus · 弹性指标族",
    "pkg": "微工具集 16 合 1",
    "concurrency": "GoSafe · singleflight · retry",
    "core": "governance · lifecycle · IP 工具",
}

PKG_SUBS = [  # pkg 芯片内的两行微库清单（16 合 1；与 DESC/pkg 仓保持一致）
    "errors · validator · stringx · slicex · enumx · convert · encoding · jsonrepair · httpjson",
    "mathx · pathx · pagination · ptrx · timex · uidgen · cache",
]

# 双主题调色板：band/边框/层标题/芯片名/注释框填充/注释文字；另有全局色与箭头四色
THEMES = {
    "light": {
        "bg": "#FFFFFF", "frame": "#E9ECEF", "title": "#1B1F3B", "sub": "#66708A",
        "badge": "#98A2B3", "svc_fill": "#FFFFFF", "svc_border": "#868E96", "svc_text": "#495057",
        "desc": "#6B7280", "pkgsub": "#868E96", "chip_fill": "#FFFFFF", "emph_fill": "#DBE4FF", "emph_border": "#4C6EF5", "emph_name": "#2B3FA8",
        "arrows": {"gray": "#868E96", "blue": "#4C6EF5", "teal": "#0CA678", "amber": "#E67700"},
        "schemes": {
            "indigo": {"band": "#F5F8FF", "border": "#BAC8FF", "head": "#3B5BDB", "name": "#364FC7", "note": "#EDF2FF", "notec": "#3B5BDB"},
            "teal":   {"band": "#F0FBF7", "border": "#96F2D7", "head": "#087F5B", "name": "#087F5B", "note": "#E6FCF5", "notec": "#087F5B"},
            "amber":  {"band": "#FFF9E8", "border": "#FFD8A8", "head": "#D9480F", "name": "#D9480F", "note": "#FFF3D6", "notec": "#D9480F"},
            "violet": {"band": "#F7F3FF", "border": "#D0BFFF", "head": "#6741D9", "name": "#6741D9", "note": "#F3EEFF", "notec": "#845EF7"},
            "slate":  {"band": "#F4F6F8", "border": "#CED4DA", "head": "#343A40", "name": "#343A40", "note": "#EDF0F2", "notec": "#495057"},
        },
    },
    "dark": {
        "bg": "#0D1117", "frame": "#30363D", "title": "#E6EDF3", "sub": "#9DA7B3",
        "badge": "#7D8590", "svc_fill": "#0D1117", "svc_border": "#6E7681", "svc_text": "#C9D1D9",
        "desc": "#9DA7B3", "pkgsub": "#7D8590", "chip_fill": "#10151D", "emph_fill": "#24325A", "emph_border": "#7D94F7", "emph_name": "#D2DBFF",
        "arrows": {"gray": "#6E7681", "blue": "#7D94F7", "teal": "#4ADE9E", "amber": "#FFB558"},
        "schemes": {
            "indigo": {"band": "#141A29", "border": "#39466F", "head": "#8DA2FB", "name": "#A5B4FF", "note": "#1B2440", "notec": "#A5B4FF"},
            "teal":   {"band": "#0F1D18", "border": "#2B5245", "head": "#5EEAB4", "name": "#5EEAB4", "note": "#12251E", "notec": "#5EEAB4"},
            "amber":  {"band": "#211909", "border": "#6B5222", "head": "#FFB558", "name": "#FFC178", "note": "#2A2010", "notec": "#FFC178"},
            "violet": {"band": "#191430", "border": "#463A7A", "head": "#C4AFFF", "name": "#C4AFFF", "note": "#221C42", "notec": "#C9B8FF"},
            "slate":  {"band": "#151A20", "border": "#3D444D", "head": "#C9D1D9", "name": "#E6EDF3", "note": "#1B2129", "notec": "#C9D1D9"},
        },
    },
}

LAYERS = [  # 自顶向下；rows 显式分行（每行芯片从左到右），注释框固定驻留 note_row 行右侧
    {"name": "应用引导 · 框架生态", "scheme": "indigo",
     "rows": [["workflow", "kitex-examples"],
              ["hertz", "bootstrap", "middleware", "kitex"]],
     "note": ["hertz.RunServer / kitex.RunServer → bootstrap 组合根",
              "统一组合与优雅关停：排空连接 → 释放资源"],
     "emph": {"bootstrap"}, "dashed": {"kitex-examples"}},
    {"name": "通信 · 服务治理", "scheme": "teal",
     "rows": [["httpx", "nns-endpoint", "network"],
              ["nns", "registry"]],
     "note": ["hertz / kitex → nns → registry（服务发现链）",
              "nns-endpoint → httpx（端点级弹性）"]},
    {"name": "数据访问", "scheme": "amber",
     "rows": [["gormx", "redisx", "data", "timertask"]]},
    {"name": "可观测性", "scheme": "violet",
     "rows": [["logx", "tracing", "metrics"]],
     "plain_note": ["全矩阵统一可观测门面", "结构化日志 / OTel 链路 / 指标"]},
    {"name": "基础库", "scheme": "slate",
     "rows": [["pkg", "concurrency", "core"]]},
]

# 芯片间依赖箭头：(from, to, style, 颜色 kind)；h=同行相邻水平，arc=同行经行顶间隙的弧线
ARROWS = [("hertz", "bootstrap", "h", "blue"),
          ("kitex", "bootstrap", "arc", "blue"),
          ("nns", "registry", "h", "teal"),
          ("redisx", "data", "h", "amber")]

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"
W, BAND_X, BAND_W, CHIP_H, GAP_X, ROW_GAP = 1240, 32, 1176, 64, 20, 12
INNER_L, INNER_R = 48, 1192


def _fail(msg: str):
    raise SystemExit(f"gen_diagram: {msg}")


def _validate_config():
    seen = [m for ly in LAYERS for row in ly["rows"] for m in row]
    if sorted(seen) != sorted(MODULES):
        miss = sorted(set(MODULES) - set(seen))
        extra = sorted(set(seen) - set(MODULES))
        _fail(f"LAYERS 与 MODULES 不一致 缺={miss} 多={extra}")
    miss = [m for m in MODULES if m not in SHORT]
    if miss:
        _fail(f"SHORT 缺短描述: {miss}")


def tw(s: str, size: float, bold: bool = False) -> float:
    w = 0.0
    for ch in s:
        o = ord(ch)
        if o > 0x2E7F:
            w += size                      # CJK / 全角
        elif ch == " ":
            w += 0.30 * size
        else:
            w += (0.62 if bold else 0.55) * size
    return w


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chip_width(mid: str) -> float:
    if mid == "pkg":
        return max(420.0, max(tw(l, 10) for l in PKG_SUBS) + 32)
    name_w = tw(mid, 15, bold=True) + 32
    desc_w = tw(SHORT[mid], 11.5) + 32
    return max(150.0, name_w, desc_w)


def note_width(lines: list[str], boxed: bool) -> float:
    m = max(tw(l, 11.5) for l in lines)
    return m + (32 if boxed else 24)


def render_svg(theme: str = "light") -> bytes:
    _validate_config()
    if theme not in THEMES:
        _fail(f"未知主题 {theme}（可选 {sorted(THEMES)}）")
    P = THEMES[theme]
    out: list[str] = []
    n = len(MODULES)
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H_TOTAL}" viewBox="0 0 {W} {H_TOTAL}" font-family="{FONT}">')
    out.append("  <defs>")
    for kind, ac in P["arrows"].items():
        out.append(f'    <marker id="arr-{kind}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
                   f'<path d="M0 0 L10 5 L0 10 z" fill="{ac}"/></marker>')
    out.append("  </defs>")
    out.append(f'  <rect x="0" y="0" width="{W}" height="{H_TOTAL}" rx="16" fill="{P["bg"]}"/>')
    out.append(f'  <rect x="0.5" y="0.5" width="{W - 1}" height="{H_TOTAL - 1}" rx="16" fill="none" stroke="{P["frame"]}"/>')
    # 页眉
    out.append(f'  <text x="40" y="48" font-size="26" font-weight="700" fill="{P["title"]}">oekit 模块矩阵 · 架构总览</text>')
    out.append(f'  <text x="40" y="74" font-size="13" fill="{P["sub"]}">{n} 个独立发版的 Go 模块（SemVer + 独立 CI）· 依赖自底向上 · github.com/oekit</text>')
    out.append(f'  <text x="{W - 40}" y="48" font-size="12" fill="{P["badge"]}" text-anchor="end">Go 1.25+ · MIT</text>')
    # 你的服务
    out.append(f'  <rect x="{BAND_X}" y="96" width="{BAND_W}" height="56" rx="10" fill="{P["svc_fill"]}" stroke="{P["svc_border"]}" stroke-dasharray="6 4"/>')
    out.append(f'  <text x="620" y="130" font-size="15" font-weight="600" fill="{P["svc_text"]}" text-anchor="middle">你的服务 · 业务微服务（Hertz HTTP / Kitex RPC）</text>')
    out.append(f'  <line x1="620" y1="154" x2="620" y2="{LAY_Y0 - 4}" stroke="{P["arrows"]["gray"]}" stroke-width="1.5" marker-end="url(#arr-gray)"/>')
    out.append(f'  <text x="634" y="168" font-size="11" fill="{P["badge"]}">组合 · 引导</text>')

    pos: dict[str, tuple[float, float, float]] = {}  # id -> (x, y, w)
    for li, ly in enumerate(LAYERS):
        sc = P["schemes"][ly["scheme"]]
        by = LAY_TOPS[li]
        rows = _layout[li]["rows"]          # list[list[(id, x, w)]]
        nr = len(rows)
        bh = 36 + CHIP_H * nr + ROW_GAP * (nr - 1) + 14
        out.append(f'  <rect x="{BAND_X}" y="{by}" width="{BAND_W}" height="{bh}" rx="12" fill="{sc["band"]}" stroke="{sc["border"]}"/>')
        out.append(f'  <text x="48" y="{by + 24}" font-size="13" font-weight="700" fill="{sc["head"]}">{esc(ly["name"])}</text>')
        for r, row in enumerate(rows):
            ry = by + 36 + r * (CHIP_H + ROW_GAP)
            for mid, x, w in row:
                emph = mid in ly.get("emph", set())
                dash = mid in ly.get("dashed", set())
                rfill = P["emph_fill"] if emph else P["chip_fill"]
                rstk = P["emph_border"] if emph else sc["border"]
                swd = ' stroke-width="2"' if emph else ""
                out.append(f'  <rect x="{x:.0f}" y="{ry:.0f}" width="{w:.0f}" height="{CHIP_H}" rx="8" fill="{rfill}" stroke="{rstk}"{swd}'
                           + (' stroke-dasharray="5 4"' if dash else "") + "/>")
                nm = esc(mid)
                nfill = P["emph_name"] if emph else sc["name"]
                nwt = ' font-weight="700"' if emph else ' font-weight="600"'
                if mid == "pkg":
                    out.append(f'  <text x="{x + 16:.0f}" y="{ry + 22:.0f}" font-size="15"{nwt} fill="{nfill}">{nm}'
                               f'<tspan font-size="11.5" font-weight="400" fill="{P["pkgsub"]}"> {esc(SHORT[mid])}</tspan></text>')
                    out.append(f'  <text x="{x + 16:.0f}" y="{ry + 38:.0f}" font-size="10" fill="{P["pkgsub"]}">{esc(PKG_SUBS[0])}</text>')
                    out.append(f'  <text x="{x + 16:.0f}" y="{ry + 52:.0f}" font-size="10" fill="{P["pkgsub"]}">{esc(PKG_SUBS[1])}</text>')
                else:
                    out.append(f'  <text x="{x + 16:.0f}" y="{ry + 24:.0f}" font-size="15"{nwt} fill="{nfill}">{nm}</text>')
                    out.append(f'  <text x="{x + 16:.0f}" y="{ry + 46:.0f}" font-size="11.5" fill="{P["desc"]}">{esc(SHORT[mid])}</text>')
                pos[mid] = (x, ry, w)
        # 层内注释（框或裸文本），固定驻留 note_row 行右侧
        if ly.get("note"):
            lines = ly["note"]
            nw = note_width(lines, True)
            nx = INNER_R - nw
            nri = _layout[li]["note_row"] or 0
            ny = by + 36 + nri * (CHIP_H + ROW_GAP)
            out.append(f'  <rect x="{nx:.0f}" y="{ny:.0f}" width="{nw:.0f}" height="{CHIP_H}" rx="8" fill="{sc["note"]}" stroke="{sc["border"]}"/>')
            for i, l in enumerate(lines):
                out.append(f'  <text x="{nx + 16:.0f}" y="{ny + 26 + i * 20:.0f}" font-size="11.5" fill="{sc["notec"]}">{esc(l)}</text>')
        elif ly.get("plain_note"):
            lines = ly["plain_note"]
            nw = note_width(lines, False)
            nx = INNER_R - nw
            ry = by + 36
            for i, l in enumerate(lines):
                out.append(f'  <text x="{nx:.0f}" y="{ry + 26 + i * 20:.0f}" font-size="11.5" fill="{sc["notec"]}">{esc(l)}</text>')
        # 层间箭头
        if li < len(LAYERS) - 1:
            gy = by + bh + 2
            out.append(f'  <line x1="620" y1="{gy:.0f}" x2="620" y2="{gy + 18:.0f}" stroke="{P["arrows"]["gray"]}" stroke-width="1.5" marker-end="url(#arr-gray)"/>')

    # 层内箭头
    for src, dst, style, kind in ARROWS:
        if src not in pos or dst not in pos:
            _fail(f"箭头端点不存在: {src}→{dst}")
        sx, sy, sw = pos[src]
        dx, dy, dw = pos[dst]
        ac = P["arrows"][kind]
        if style == "h":
            if abs(sy - dy) > 1 or dx <= sx + sw:
                _fail(f"箭头 {src}→{dst} 不是同行相邻（检查 LAYERS 顺序）")
            out.append(f'  <line x1="{sx + sw + 1:.0f}" y1="{sy + CHIP_H / 2:.0f}" x2="{dx - 3:.0f}" y2="{dy + CHIP_H / 2:.0f}"'
                       f' stroke="{ac}" stroke-width="1.5" marker-end="url(#arr-{kind})"/>')
        elif style == "arc":
            if abs(sy - dy) > 1:
                _fail(f"箭头 {src}→{dst} 暂只支持同行弧线（源目标须同行）")
            gap_y = sy - ROW_GAP / 2          # 行顶上方的间隙中线
            sx_c, ex = sx + sw / 2, dx + dw * 0.35
            out.append(f'  <path d="M {sx_c:.0f} {sy - 2:.0f} C {sx_c - 100:.0f} {gap_y:.0f}, {ex + 140:.0f} {gap_y:.0f}, {ex:.0f} {dy - 2:.0f}"'
                       f' fill="none" stroke="{ac}" stroke-width="1.5" stroke-dasharray="5 4" marker-end="url(#arr-{kind})"/>')

    # 页脚
    out.append(f'  <text x="40" y="{H_TOTAL - 34:.0f}" font-size="11.5" fill="{P["badge"]}">依赖方向：上层依赖下层，全矩阵无环 · 每仓独立发版（SemVer）+ 独立 CI（Lint / Test）</text>')
    out.append(f'  <text x="40" y="{H_TOTAL - 16:.0f}" font-size="11.5" fill="{P["badge"]}">模块索引：https://github.com/oekit · 文档：https://oekit.github.io · llms.txt 供 AI Agent 检索</text>')
    out.append("</svg>")
    return ("\n".join(out) + "\n").encode()


# ---------- 两遍布局：先算各层行数得纵向坐标，再回填 ----------
def _layout_layers():
    layout = []
    for ly in LAYERS:
        note_w = note_width(ly["note"], True) if ly.get("note") else 0
        note_row = ly.get("note_row", 0)
        rows = []
        for ri, row_spec in enumerate(ly["rows"]):
            limit = INNER_R - (note_w + GAP_X) if (note_w and ri == note_row) else INNER_R
            row: list[tuple[str, float, float]] = []
            rx = float(INNER_L)
            for mid in row_spec:
                w = chip_width(mid)
                row.append((mid, rx, w))
                rx += w + GAP_X
            if rx - GAP_X > limit:
                _fail(f"层「{ly['name']}」第 {ri + 1} 行超宽（{rx - GAP_X:.0f} > {limit:.0f}），调整 rows 或 SHORT")
            rows.append(row)
        layout.append({"rows": rows, "note_row": note_row if note_w else None})
    return layout


_layout = _layout_layers()
_layer_h = [36 + CHIP_H * len(_layout[i]["rows"]) + ROW_GAP * (len(_layout[i]["rows"]) - 1) + 14 for i in range(len(LAYERS))]
LAY_Y0 = 178.0
_LAYER_GAP = 24
_layer_span = [_layer_h[i] + _LAYER_GAP for i in range(len(LAYERS))]     # 层高 + 层间隙
LAY_TOPS = [LAY_Y0 + sum(_layer_span[:i]) for i in range(len(LAYERS))]   # 各层纵向起点（前缀和）
H_TOTAL = LAY_TOPS[-1] + _layer_h[-1] + 62                               # 末层后接页脚

FILENAME = {"light": "architecture.svg", "dark": "architecture-dark.svg"}


def targets() -> list[tuple[str, Path, str]]:
    """全部产物 (标签, 绝对路径, 主题)。仓未检出时该仓条目缺席。"""
    out = []
    for repo_label, sub in ((HOMEPAGE_REPO, "docs/assets"), (SITE_REPO, "docs/public")):
        try:
            base = repo_dir(repo_label, str(BASE)) / sub
        except FileNotFoundError:
            continue
        for theme, fn in FILENAME.items():
            out.append((f"{repo_label}/{fn}", base / fn, theme))
    return out


def main() -> None:
    svg_by_theme = {t: render_svg(t) for t in FILENAME}
    tgts = targets()
    if not tgts:
        _fail("两处目标仓均未检出（需 oekit 与站点仓本地副本）")
    if "--check" in sys.argv:
        bad = [label for label, p, t in tgts if not p.exists() or p.read_bytes() != svg_by_theme[t]]
        if bad:
            print(f"架构图漂移：{', '.join(bad)}（跑 gen_diagram.py）")
            raise SystemExit(1)
        print(f"✓ 架构图与生成器一致（{len(tgts)} 处）")
        return
    import subprocess
    for label, p, t in tgts:
        old = p.read_bytes() if p.exists() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(svg_by_theme[t])
        print(f"{'=' if old == svg_by_theme[t] else '✎'} {label}")
    if "--push" in sys.argv:
        for label, p, _t in tgts:
            d = p.parent
            while not (d / ".git").exists():
                d = d.parent
            if not subprocess.run(["git", "status", "--porcelain", str(p.relative_to(d))],
                                  cwd=d, capture_output=True).stdout.strip():
                continue
            msg = "docs: architecture 图重新生成（matrix-tools gen_diagram，light+dark）"
            subprocess.run(["git", "add", str(p.relative_to(d))], cwd=d, check=True)
            subprocess.run(["git", "commit", "-q", "-m", msg], cwd=d, check=True)
            subprocess.run(["git", "push", "-q"], cwd=d, check=True)
            print(f"⬆ {label}")


if __name__ == "__main__":
    main()
