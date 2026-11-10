#!/usr/bin/env python3
"""反向依赖地板核查：「我发的新版本还没被谁跟上」（matrix-tools#267）。

## 为什么要有这个

本矩阵的地板治理**只管「我依赖谁」**（align_floors / audit 扫本 org 各仓的
go.mod），**不管「谁依赖我」**。但本 org 的模块正被**别的 org** 大量依赖
（实测 2026-11-09：oagentkit 15 个仓里 13 个 require github.com/oekit/*）——
`pkg v0.41.0` 发布之后 12 个消费仓里只跟了 2 个，而 oekit 侧的任何工具
**完全看不到这 10 处**。跨 org 的地板落后落在治理盲区：对侧有判据
（verify_dep_floors）但默认 report 模式且与本侧零联动，本侧根本没有这一面。

⇒ 本脚本把「谁依赖我」变成**本侧的可见事实**（#267 的第①条）：
  对每个 oekit 发版单元，列出跨 org 消费方 go.mod 里 require 的版本，
  与该单元**远端最新 tag** 比——落后的消费方按**集合**判一条漂移
  （#267 的第②条：地板要按依赖集合整体判定，不是一个仓一个仓地改；
  「llm/tools 跟上、其余 11 个没跟上」的半对齐形态当场判红）。

## 消费方清单与发现

登记在 `scripts/cross_org_consumers.json`（org → 仓列表）。清单是**可验证的**：
- 某个登记仓不再 require github.com/oekit/*（或仓已删/改名）⇒ 报
  「登记已过期」⚠——清单要随之维护，否则这一面会烂掉；
- 要**发现**新消费方：`--discover` 跑 GitHub code search
  （`github.com/oekit` filename:go.mod，按 org），把命中仓与登记清单比对，
  建议增补。code search 有配额，所以只做成显式旗子，不进默认跑。

## 事实源与读不到的口径（#79 同族纪律）

- 消费方 go.mod 读 **raw.githubusercontent.com/<org>/<repo>/HEAD/go.mod**
  （HEAD = 默认分支最新提交 ⇒ 不吃本地 clone 陈旧的亏，#269 的教训）；
- oekit 侧最新版本读**远端 tag**（与 audit #269 同源）；
- 消费方 go.mod 读不到 ⇒ ⚠ 报「未核对」，**不静默跳过、也不当成「没依赖」**
  ——读不到 ≠ 没有；但**不**判 ❌（跨 org 私有仓/网络抖动是环境事实，
  判 ❌ 会把本侧 CI 永久钉死在对侧的可达性上）。**读得到的**消费方落后
  才判 ❌（那是可行动的事实：跨 org 的地板追平有对侧工具
  oagentkit/matrix-tools 的 verify_dep_floors / bump_dep_floors 负责，
  本侧报告是协调通道）。

## 协作口径（#267 的第③条，两边对同一批依赖给同一份结论）

- 本侧（oekit）：audit / 本脚本——「我的新版本还没被谁跟上」；
- 对侧（oagentkit）：verify_dep_floors.py（--prefix 默认含 github.com/oekit/）
  「我依赖的谁又发新版了」；
- 两边读的是**同一批远端事实**（tag + go.mod），结论应当一致；
  不一致时先怀疑本地 clone 陈旧（#246/#269 同族），再用 --remote 复核。
- **发版流程接入**（#267 的第④条）：release_train 打完 tag 会提示跑本脚本
  ——「我发新版 ⇒ 核查下游」从自觉变成流程里的一行输出。

用法：
    python3 scripts/reverse_floors.py                # 核查 + 打印报告
    python3 scripts/reverse_floors.py --json out.json
    python3 scripts/reverse_floors.py --discover     # 另跑 code search 找新消费方
    python3 scripts/reverse_floors.py --selftest     # 离线自测（fixture 注入）

退出码：0 = 无 ❌；1 = 有落后/过期（漂移清单逐条打印）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from matrix import (  # noqa: E402
    MODULES, NESTED_MODULES, ORG, latest_tag, enable_tag_cache,
    prefetch_remote_tags, nested_tag_prefix,
)

CONSUMERS_JSON = HERE / "cross_org_consumers.json"

# require 行抓取：direct 与 indirect 块都算（嵌套模块的依赖几乎全是 indirect 形态
# ——align_floors 的既有教训）。模块路径小写，tag 严格 semver。
OEKIT_REQUIRE_RE = re.compile(
    r"github\.com/oekit/([A-Za-z0-9._/-]+)\s+(v\d+\.\d+\.\d+)(?:[-+][^\s]*)?")

RAW_TIMEOUT = 20


def load_consumers(path: Path = CONSUMERS_JSON) -> dict[str, list[str]]:
    if not path.exists():
        raise SystemExit(f"✗ 消费方清单不存在：{path}（登记跨 org 消费方：org → 仓列表）")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(v, list) for v in data.values()):
        raise SystemExit(f"✗ 消费方清单形状不对（应为 {{org: [repo, ...]}}）：{path}")
    return data


def save_consumers(consumers: dict[str, list[str]], path: Path = CONSUMERS_JSON) -> None:
    path.write_text(json.dumps(consumers, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def fetch_gomod(org: str, repo: str, fetcher=None) -> str:
    """消费方默认分支 HEAD 的 go.mod 正文。

    读不到抛 RuntimeError，**消息带形态**：
      - `HTTP 404`：HEAD 上没有 go.mod——可能不是 Go 模块（纯工具仓）或仓已删
        ⇒ 调用方按「登记待核实」处理（提示维护清单，不是网络故障）；
      - 其它（超时/连接错）：环境故障 ⇒ 调用方按「未核对」处理（读不到 ≠ 没有）。
    带一次重试（raw.githubusercontent 偶发超时，实测一次重试足够）。
    """
    if fetcher is not None:
        return fetcher(org, repo)
    url = f"https://raw.githubusercontent.com/{org}/{repo}/HEAD/go.mod"
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(url, timeout=RAW_TIMEOUT) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}") from None   # 404 不重试（确定无文件）
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
    raise RuntimeError(f"{last_err}") from None


def unit_latest_versions() -> dict[str, str]:
    """oekit 侧每个发版单元（根 + 嵌套）→ 远端最新 tag。"""
    enable_tag_cache()
    prefetch_remote_tags(MODULES)
    out: dict[str, str] = {}
    for repo in MODULES:
        lt = latest_tag(repo, remote=True)
        if lt:
            out[f"github.com/{ORG}/{repo}"] = lt
    for nm in NESTED_MODULES:
        prefix = nested_tag_prefix(nm["module"])
        if not prefix:
            continue
        tags = [t for t in _remote_tags_cached(nm["repo"])
                if t.startswith(f"{prefix}/")]
        semver = [t[len(prefix) + 1:] for t in tags
                  if re.fullmatch(re.escape(prefix) + r"/v\d+\.\d+\.\d+", t)]
        if semver:
            out[nm["module"]] = _max_semver(semver)
    return out


def _remote_tags_cached(repo: str) -> list[str]:
    from matrix import remote_tags  # noqa: 延迟导入保持缓存口径一致
    return remote_tags(repo)


def _max_semver(versions: list[str]) -> str:
    def key(v: str):
        try:
            return tuple(int(x) for x in v[1:].split("."))
        except ValueError:
            return (0, 0, 0)
    return sorted(set(versions), key=key, reverse=True)[0]


def parse_consumer_requires(text: str) -> dict[str, str]:
    """go.mod 正文 → {github.com/oekit/<module>: require 的版本}。"""
    return {f"github.com/oekit/{m}": v
            for m, v in OEKIT_REQUIRE_RE.findall(text)}


def cross_org_floor_drifts(consumers: dict[str, list[str]], latest: dict[str, str],
                           fetcher=None) -> tuple[list[str], list[str]]:
    """核查全部消费方 ⇒ (漂移列表, 警告列表)。

    漂移（❌，可行动）：
      - 某发版单元的最新版本**没被全部**消费方跟上 ⇒ 按**集合**判一条，
        一次列出全部落后者与它们的版本（#267 第②条）。
    警告（⚠，环境/清单卫生，不阻塞）：
      - 消费方 go.mod 读不到 ⇒ 「未核对」（读不到 ≠ 没有，#79 同族）；
      - 登记仓不再依赖 oekit（清单过期）。
    """
    drifts: list[str] = []
    warns: list[str] = []
    # consumer → require 表；读不到的消费方记为 None
    per_consumer: dict[str, dict[str, str] | None] = {}
    for org, repos in consumers.items():
        for repo in repos:
            name = f"{org}/{repo}"
            try:
                text = fetch_gomod(org, repo, fetcher=fetcher)
            except RuntimeError as e:
                per_consumer[name] = None
                if "HTTP 404" in str(e):
                    # 404 = HEAD 上没有 go.mod：可能不是 Go 模块（纯工具仓）或已删
                    # ⇒ 登记**待核实**（维护清单），不是网络故障
                    warns.append(f"{name}：HEAD 上没有 go.mod（可能不是 Go 模块或仓已删）"
                                 f"⇒ 登记待核实：核实后从 cross_org_consumers.json "
                                 f"移除或保留观察")
                else:
                    warns.append(f"{name}：go.mod 读不到（{e}）⇒ 消费面**未核对**"
                                 f"（读不到 ≠ 没有；私有仓/网络问题时先修读取，别当干净放行）")
                continue
            req = parse_consumer_requires(text)
            per_consumer[name] = req
            if not req:
                warns.append(f"{name}：登记在消费方清单里但 go.mod 不再 require "
                             f"github.com/oekit/* ⇒ 登记已过期，请从 "
                             f"cross_org_consumers.json 移除（清单过期会让这一面烂掉）")

    # 按发版单元做**集合判定**（#267 第②条）
    for module, newest in sorted(latest.items()):
        lagging: list[str] = []
        checked = 0
        for name, req in per_consumer.items():
            if req is None:
                continue                     # 未核对的消费方已在 warns 里显式报过
            if module not in req:
                continue                     # 它不依赖这个单元
            checked += 1
            if req[module] != newest:
                lagging.append(f"{name} {req[module]}")
        if lagging:
            drifts.append(
                f"{module} v{newest[1:] if newest.startswith('v') else newest}："
                f"跨 org 消费方 {checked} 个中 {len(lagging)} 个落后——"
                f"{', '.join(sorted(lagging))}；"
                f"地板按**集合**判定：要跟就全跟（对侧 bump_dep_floors 一条命令），"
                f"确要钉旧版的在 cross_org_consumers.json 写明 pin（matrix-tools#267）")
    return drifts, warns


def discover_candidates() -> list[str]:
    """GitHub code search：org 里 go.mod 引到 github.com/oekit 的仓 ⇒ 登记清单的增补建议。

    code search 有配额且需要认证（gh）⇒ 只做显式旗子（--discover），不进默认跑。
    """
    drifts: list[str] = []
    consumers = load_consumers()
    for org, repos in consumers.items():
        known = set(repos)
        query = f'"github.com/oekit" filename:go.mod org:{org}'
        try:
            raw = subprocess.run(
                ["gh", "api", "-X", "GET", "search/code", "-f", f"q={query}",
                 "-f", "per_page=100"],
                capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            drifts.append(f"{org}：--discover code search 失败（{e}）⇒ 发现面未核对")
            continue
        if raw.returncode != 0:
            drifts.append(f"{org}：--discover code search 失败"
                          f"（{raw.stderr.strip()[:200]}）⇒ 发现面未核对（读不到 ≠ 没有）")
            continue
        try:
            items = json.loads(raw.stdout).get("items", [])
        except json.JSONDecodeError:
            drifts.append(f"{org}：--discover code search 返回不可解析 ⇒ 发现面未核对")
            continue
        found = {it.get("repository", {}).get("name", "") for it in items}
        found.discard("")
        new = sorted(found - known - {ORG, "oekit.github.io"})
        if new:
            drifts.append(f"{org}：code search 发现未登记的消费方仓 {new}"
                          f"⇒ 请核实后补进 cross_org_consumers.json（#267：清单要能长大）")
    return drifts


def cross_org_floor_warnings(latest: dict[str, str] | None = None,
                             fetcher=None) -> tuple[list[str], str | None]:
    """audit 用的**报告形态**：返回 (⚠ 行列表, 不可用原因)。

    与 standalone（exit 1）的区别：跨 org 地板落后在 audit 里是 **⚠ 可见不阻塞**
    ——追平动作发生在对侧 org（bump_dep_floors），❌ 会把本侧 CI 钉死在对侧的
    行动上（#267：本侧报告是**协调通道**，不是对侧的执行器）。清单卫生与
    「未核对」也一并显式可见（#79 同族：读不到 ≠ 没有，判不了 ≠ 通过）。
    latest=None 时自查取远端事实（audit 调用时已开 tag 缓存，零额外网络读）。
    """
    try:
        consumers = load_consumers()
        if latest is None:
            latest = unit_latest_versions()
        drifts, warns = cross_org_floor_drifts(consumers, latest, fetcher=fetcher)
    except Exception as e:  # noqa: BLE001 —— 判不了必须显式说，不当通过
        return [], f"反向依赖核查不可用：{e}——跨 org 消费面**未核对**（判不了 ≠ 通过）"
    lines = list(warns)
    for d in drifts:
        lines.append(f"{d}（audit 中可见不阻塞：追平由对侧 bump_dep_floors 负责，"
                     f"standalone reverse_floors.py 才以退出码拦截）")
    return lines, None


def emit_upgrade_commands(gomod_path: str, diff_only: bool = False,
                          latest: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """消费方 go.mod → 精确的 `go get` 升级命令（matrix-tools#271）。

    输入任意 go.mod 路径（下游仓工作树），输出**单行可复制**的 go get——
    每个模块钉到远端最新 tag。下游升级轮的「人工 grep go.mod × 17 模块
    × N 仓、版本号手滑整单回滚」（zeus+fuxi 14 仓实弹）由这一行消灭。

    返回 (命令, 警告)。tag 事实读不到的模块**不进命令**（拼一个错版本号
    进 go get 是整单回滚的根因）⇒ 但必须显式警告（读不到 ≠ 没有）。
    diff_only=True 只钉有落差的模块；缺省全量钉最新（幂等重跑安全）。
    """
    text = Path(gomod_path).read_text(encoding="utf-8")
    req = parse_consumer_requires(text)
    if not req:
        return "", ["该 go.mod 不 require 任何 github.com/oekit/* 模块 ⇒ 无需升级命令"]
    if latest is None:
        latest = unit_latest_versions()
    missing = [m for m in req if m not in latest]
    gets: list[str] = []
    for m, cur in req.items():
        if m not in latest:
            continue
        if diff_only and cur == latest[m]:
            continue
        gets.append(f"{m}@{latest[m]}")
    cmd = f"go get {' '.join(gets)}" if gets else ""
    warns = [f"{m}：远端最新 tag 读不到 ⇒ 不进升级命令（先修网络/代理再重跑）"
             for m in missing]
    return cmd, warns


def main() -> int:
    p = argparse.ArgumentParser(description="反向依赖地板核查（matrix-tools#267）")
    p.add_argument("--json", help="把报告落成 JSON（站点/面板可用）")
    p.add_argument("--discover", action="store_true",
                   help="另跑 GitHub code search 发现未登记的消费方（有配额，显式才跑）")
    p.add_argument("--emit", metavar="GOMOD_PATH",
                   help="消费方 go.mod 路径 ⇒ 输出精确 go get 升级命令（matrix-tools#271）")
    p.add_argument("--diff", action="store_true",
                   help="配合 --emit：只输出有落差的模块（缺省全量钉最新）")
    p.add_argument("--selftest", action="store_true", help="离线自测")
    args = p.parse_args()
    if args.selftest:
        return _selftest()
    if args.emit:
        cmd, warns = emit_upgrade_commands(args.emit, diff_only=args.diff)
        for w in warns:
            print(f"⚠ {w}", file=sys.stderr)
        if cmd:
            print(cmd)
            return 0
        print("✓ 无需升级（全部 oekit 依赖已在最新）", file=sys.stderr)
        return 0

    consumers = load_consumers()
    latest = unit_latest_versions()
    drifts, warns = cross_org_floor_drifts(consumers, latest)
    if args.discover:
        drifts.extend(discover_candidates())

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"drifts": drifts, "warnings": warns, "latest": latest},
            ensure_ascii=False, indent=2), encoding="utf-8")

    total_consumers = sum(len(v) for v in consumers.values())
    print(f"反向依赖核查：{len(consumers)} org / {total_consumers} 登记消费方，"
          f"{len(latest)} 个发版单元按集合判定")
    for w in warns:
        print(f"  ⚠ {w}")
    if drifts:
        print(f"发现 {len(drifts)} 项跨 org 地板落后：")
        for d in drifts:
            print(f"  ❌ {d}")
        return 1
    print("✓ 全部发版单元的最新版本已被所有可见消费方跟上")
    return 0


def _selftest() -> int:
    def ns_main_emit(path: str, diff_only: bool = False, latest=None):
        return emit_upgrade_commands(path, diff_only=diff_only, latest=latest)

    """离线自测：fetcher/tag 源全注入，判据有牙齿（变异可证）。"""
    from matrix import RemoteReadError  # noqa: F401  — 保持 import 面与主流程一致
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    latest = {"github.com/oekit/pkg": "v0.42.0", "github.com/oekit/logx": "v0.8.12"}

    def mk_fetcher(table: dict[str, str]):
        def f(org: str, repo: str) -> str:
            v = table.get(f"{org}/{repo}")
            if v is None:
                # 缺 key = 环境故障语义（超时），与 404（无文件）区分
                raise RuntimeError("The read operation timed out")
            return v
        return f

    def gomod(reqs: dict[str, str]) -> str:
        lines = "\n".join(f"\t{m} {v}" for m, v in reqs.items())
        return f"module demo\n\nrequire (\n{lines}\n)\n"

    ok_side = {"oagentkit/textx": gomod({"github.com/oekit/pkg": "v0.42.0"}),
               "oagentkit/llm": gomod({"github.com/oekit/pkg": "v0.42.0",
                                       "github.com/oekit/logx": "v0.8.12"})}
    lag = {"oagentkit/runtime": gomod({"github.com/oekit/pkg": "v0.40.0"})}
    stale = {"oagentkit/queue": gomod({})}   # 登记过期：不再依赖 oekit

    consumers = {"oagentkit": ["textx", "llm", "runtime", "agent", "queue"]}

    # ── 1：全部跟上 ⇒ 零漂移 ──
    d, w = cross_org_floor_drifts(consumers, latest, fetcher=mk_fetcher(ok_side))
    check(not d, f"1: 全部跟上必须零漂移——实得 {d}")

    # ── 2：半对齐形态（部分落后）⇒ **按集合判一条**，列出全部落后者 ──
    table = dict(ok_side); table.update(lag)
    d, w = cross_org_floor_drifts(consumers, latest, fetcher=mk_fetcher(table))
    check(len(d) == 1 and "pkg" in d[0] and "runtime v0.40.0" in d[0]
          and "落后" in d[0] and "llm" not in d[0],
          f"2: 半对齐必须按集合判一条且点名落后者——实得 {d}")

    # ── 3：读不到的消费方（环境故障）⇒ ⚠ 未核对，不判 ❌、也不当干净 ──
    # （fixture：agent 不在表里 ⇒ mk_fetcher 走超时，与真读取失败同路）
    table = dict(ok_side)
    d, w = cross_org_floor_drifts(consumers, latest, fetcher=mk_fetcher(table))
    check(not d and any("/agent：" in x and "未核对" in x for x in w),
          f"3: 读不到必须显式「未核对」且不判漂移——实得 d={d}, w={w}")

    # ── 3b：404（HEAD 上没有 go.mod）⇒ 「登记待核实」，不是网络故障 ──
    def f404(org: str, repo: str) -> str:
        if repo == "agent":
            raise RuntimeError("HTTP 404")
        v = ok_side.get(f"{org}/{repo}")
        if v is None:
            raise RuntimeError("The read operation timed out")
        return v
    d, w = cross_org_floor_drifts(consumers, latest, fetcher=f404)
    # 注意锚定写法：「agent」是 oagentkit 的子串（substring 陷阱）⇒ 用 "/agent："
    check(not d and any("/agent：" in x and "待核实" in x for x in w)
          and not any("/agent：" in x and "未核对" in x for x in w),
          f"3b: 404 必须报「登记待核实」而非未核对——实得 d={d}, w={w}")

    # ── 4：登记过期（不再依赖 oekit）⇒ ⚠ 提示维护清单 ──
    table = dict(ok_side); table.update(stale)
    d, w = cross_org_floor_drifts(consumers, latest, fetcher=mk_fetcher(table))
    check(any("queue" in x and "过期" in x for x in w),
          f"4: 登记过期必须提示维护清单——实得 w={w}")

    # ── 5：嵌套单元也参与集合判定（require registry/nacos 对 nacos 最新）──
    latest5 = {"github.com/oekit/registry/nacos": "v0.1.11"}
    table5 = {"oagentkit/textx": gomod({"github.com/oekit/registry/nacos": "v0.1.10"}),
              "oagentkit/llm": gomod({"github.com/oekit/registry/nacos": "v0.1.11"})}
    d, w = cross_org_floor_drifts({"oagentkit": ["textx", "llm"]}, latest5,
                                  fetcher=mk_fetcher(table5))
    check(len(d) == 1 and "nacos" in d[0] and "textx v0.1.10" in d[0],
          f"5: 嵌套单元的落后也要判——实得 {d}")

    # ── 5b：--emit 升级命令生成（#271）——全量钉最新、--diff 只钉落差、
    #        tag 读不到的模块**不进命令**但显式警告 ──
    with tempfile.TemporaryDirectory() as td2:
        gm = Path(td2) / "go.mod"
        gm.write_text(gomod({"github.com/oekit/pkg": "v0.40.0",
                             "github.com/oekit/logx": "v0.8.12"}))
        cmd, warns = ns_main_emit(str(gm), diff_only=False, latest=latest)
        check(cmd == "go get github.com/oekit/pkg@v0.42.0 github.com/oekit/logx@v0.8.12",
              f"5b: 全量模式钉最新（含已齐平的）——实得 {cmd!r}")
        cmd2, _ = ns_main_emit(str(gm), diff_only=True, latest=latest)
        check(cmd2 == "go get github.com/oekit/pkg@v0.42.0",
              f"5b--diff: 只钉有落差的——实得 {cmd2!r}")
        ns_emit = None  # 缺 tag 形态：latest 缺一个模块
        cmd3, warns3 = ns_main_emit(str(gm), diff_only=False,
                                    latest={"github.com/oekit/pkg": "v0.42.0"})
        check("logx" not in cmd3 and any("logx" in w and "读不到" in w for w in warns3),
              f"5b--missing: tag 读不到的模块不进命令且显式警告——实得 {cmd3!r} / {warns3}")


    #    （集合判定是本修法的核心：变异后半对齐形态只报一个落后者，
    #      「其余 11 个没跟上」会漏——fixture 用**两个**落后消费方才能分辨。）
    src = (HERE / "reverse_floors.py").read_text(encoding="utf-8")
    mutated = src.replace(
        "            if req[module] != newest:\n                lagging.append(f\"{name} {req[module]}\")",
        "            if req[module] != newest and len(lagging) == 0:\n                lagging.append(f\"{name} {req[module]}\")")
    check(mutated != src, "6: 变异体没生效（源码形态变了？）")
    if mutated != src:
        ns: dict = {"__file__": str(HERE / "reverse_floors.py"), "__name__": "reverse_floors_mut"}
        exec(compile(mutated.replace('if __name__ == "__main__":\n    sys.exit(main())', ''),
                     "<mut>", "exec"), ns)
        two_lag = {"oagentkit/textx": gomod({"github.com/oekit/pkg": "v0.42.0"}),
                   "oagentkit/runtime": gomod({"github.com/oekit/pkg": "v0.40.0"}),
                   "oagentkit/agent": gomod({"github.com/oekit/pkg": "v0.39.0"})}
        d, _ = ns["cross_org_floor_drifts"](
            {"oagentkit": ["textx", "runtime", "agent"]}, latest,
            fetcher=mk_fetcher(two_lag))
        # 变异后必须**丢人**（只剩第一个落后者）⇒ 证明原判据「列全部」真的在生效
        check(len(d) == 1 and "runtime v0.40.0" in d[0] and "agent v0.39.0" not in d[0],
              f"6: 变异后必须丢掉第二个落后者（证明集合判定有牙齿）——实得 {d}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("✓ reverse_floors 自测通过（集合判定/未核对口径/清单过期/嵌套单元）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
