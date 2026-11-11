#!/usr/bin/env python3
"""渲染 oekit AI 入口 skill（skills/oekit）与站点《选型速查》指南——同一份数据两种产物。

定位：skill 回答的是「我要做 X，该用哪个 oekit 包、怎么组合、踩哪条红线」，
不是「oekit 有什么」（那是各仓 AGENTS.md / godoc 的职责）。因此：

  - 场景选型表（SCENARIOS）：**唯一手写新增知识**——跨模块选型判据
    （进程内 vs 分布式限流、redisx vs etcdx 锁、core/config vs pkg/config…）
    单仓文档里没有汇总，这里是它们的单点。先例：gen_agents.py 的硬规则 BODY
    就是手写模板。
  - 红线完整版：渲染自 rules.HARD_RULES_MD（与 gen_agents.py 单源，2026-11-12
    字节级比对验证过的提取）。
  - 模块索引：渲染自 matrix.py（MODULES / NESTED_MODULES / DESC）。

用法：
  python3 scripts/gen_skill.py           # 渲染 skills/oekit/{SKILL.md,rules.md,modules.md,pkg.md}
  python3 scripts/gen_skill.py --site    # 额外渲染站点仓 docs/guide/selection.md（同源）
  python3 scripts/gen_skill.py --check   # 只比对不写（漂移退出码 1，audit 同口径）

渲染产物**勿手改**：audit.py 会重渲染比对，漂移即红卡（BOM 新鲜度门禁同型）。
修订入口：场景表/pkg 速查改本文件数据区；硬规则改 rules.py；模块事实改 matrix.py。
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from matrix import DESC, MODULES, NESTED_MODULES, SITE_REPO, ensure_clean_all, repo_dir  # noqa: E402
from rules import HARD_RULES_MD  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
MT_ROOT = HERE.parent
SKILL_DIR = MT_ROOT / "skills" / "oekit"

# ---------------------------------------------------------------------------
# 手写数据区（修订场景判据/速查表改这里；mods 只填根模块名，audit 按矩阵校验死键）
# ---------------------------------------------------------------------------

# 能力层分组（口径与伞仓 README 架构总览一致，纯展示分组）
LAYERS: list[tuple[str, list[str]]] = [
    ("应用引导 · 框架生态", ["bootstrap", "hertz", "kitex", "middleware", "workflow", "kitex-examples", "k8s"]),
    ("通信 · 服务治理", ["httpx", "nns-endpoint", "network", "nns", "registry"]),
    ("数据访问", ["gormx", "redisx", "etcdx", "data", "timertask"]),
    ("可观测性", ["logx", "tracing", "metrics"]),
    ("基础库", ["pkg", "concurrency", "core", "kitx"]),
]

# 场景选型表：scene=我要做什么 / use=用什么（可含子包路径） / tips=组合与姿势 /
# redline=违反即返工的判据 / mods=涉及根模块（audit 校验 ∈ MODULES，防改名后留死键）
SCENARIOS: list[tuple[str, list[dict]]] = [
    ("服务构建", [
        {"scene": "起 HTTP 服务（生产全栈）",
         "use": "`bootstrap` + `hertz` + `middleware`（注册发现加 `nns`）",
         "tips": "`hertz.RunServer` 的 `WithOnReady` 里触发 `app.Ready()`（就绪门，NNS 才注册）；标准栈 `mw.DefaultMiddleware`",
         "redline": "userFn 返回即服务结束：长驻必须 `<-app.Context().Done()`；无 Recovery 则 panic 直接断连",
         "mods": ["bootstrap", "hertz", "middleware"]},
        {"scene": "起 RPC 服务（Kitex）",
         "use": "`kitex` + `bootstrap`（+`nns`）",
         "tips": "`RunServer(WithOnReady/WithReadyAddr)`；`SetDefaultPanicHandler` 兜底；治理热更 `NewGovernanceConfig`",
         "redline": "就绪门不触发 ⇒ 服务永不进注册中心；治理配置是全量同步，勿混用代码级 retry",
         "mods": ["kitex", "bootstrap", "nns"]},
        {"scene": "handler 错误出口",
         "use": "`pkg/errors` + `middleware`",
         "tips": "BizError 命名工厂（NewNotFoundError 等）；`mw.Fail(ctx, c, err)` 统一出口（NotFound→404、兜底 500）",
         "redline": "BizError 载体唯一——混两套 errors 包 404 断裂成 500；error 禁 `_` 忽略",
         "mods": ["pkg", "middleware"]},
        {"scene": "响应体自管的服务",
         "use": "`middleware`",
         "tips": "`StackConfig{DisableResponseWrap: true}` 一键（TraceID→Recovery→AccessLog）或 `MinimalMiddleware` 手拼",
         "redline": "Recovery 仍硬性要求（Hertz 无默认 PanicHandler）",
         "mods": ["middleware"]},
        {"scene": "健康检查 / K8s 就绪",
         "use": "`bootstrap/healthz` + `hertz`",
         "tips": "liveness/readiness 多 check 并发、panic 隔离；业务健康门过了再 `app.Ready()`",
         "redline": "TCP 探活只证端口可建连，不证业务健康",
         "mods": ["bootstrap", "hertz"]},
        {"scene": "K8s 客户端 / Lease 选主",
         "use": "`k8s`（+`k8s/leaderelection`）",
         "tips": "kubeconfig→KUBECONFIG→in-cluster 逐级回退；主备单活 OnStarted/StoppedLeading 回调",
         "redline": "失权与 ctx 取消并存时失权优先上抛 ErrLostLeadership",
         "mods": ["k8s"]},
        {"scene": "组合式流程编排",
         "use": "`workflow`",
         "tips": "串行/并行/异步/条件分支 + Hook AOP",
         "mods": ["workflow"]},
    ]),
    ("出站调用与弹性", [
        {"scene": "内部服务 HTTP 调用（发现+熔断+重试+LB）",
         "use": "`httpx`（+`nns`）",
         "tips": "存量 net/http 用 `NewRoundTripper`/`StdClient` 一行接入治理；LB 用 loadbalance Selector（p2c/一致性哈希）",
         "redline": "重试缺省关、显式开；非 2xx 非错误才计失败",
         "mods": ["httpx", "nns"]},
        {"scene": "出站 SSRF 防护",
         "use": "`pkg/urlguard` + `httpx`",
         "tips": "一行组合 `httpx.WithSafeDial`；豁免只走构造态 `WithAllow*`",
         "redline": "拨号层预解析 + RemoteAddr 核对闭合 TOCTOU——别在调用方自己写解析",
         "mods": ["pkg", "httpx"]},
        {"scene": "RPC 依赖的 HTTP 端点（熔断+换实例重试）",
         "use": "`nns-endpoint`",
         "tips": "`NewHTTPEndpointFactory(nnsc.ServiceClient()).MakeEndpoint(\"dep-svc\")`",
         "redline": "4xx 不计熔断失败",
         "mods": ["nns-endpoint", "nns"]},
        {"scene": "重试（带返回值/退避抖动）",
         "use": "`concurrency/retry`",
         "tips": "`DoValue[T]`（`Do` 已委托给它，只有一份退避实现）；服务端 Retry-After 优先（DelayHint）",
         "redline": "别手写退避——封顶/抖动/可重试判定都在 retry 里",
         "mods": ["concurrency"]},
        {"scene": "中间件一份逻辑两端跑",
         "use": "`core/chain` + `core/transport` → `middleware/chainmw`",
         "tips": "kitex 经 `kitex/chainmw.ToKitex` 复用同一实现（Recovery/AccessLog/MetadataPropagate）",
         "redline": "监控上报钩子内 panic 已隔离，别在钩子里再 recover 吞语义",
         "mods": ["core", "middleware", "kitex"]},
        {"scene": "RPC 业务异常透传",
         "use": "`kitex` `WithBizErrorBridge`",
         "tips": "对端 BizStatusError → 本地 BizError，handler `mw.Fail` 无感处理",
         "redline": "timeout→50400/504、no-instance→50300/503——映射是框架契约，别自己猜",
         "mods": ["kitex", "pkg"]},
    ]),
    ("数据访问", [
        {"scene": "CRUD 缓存编排",
         "use": "`data/cached`（多实例加 `data/twolevel`）",
         "tips": "三行接入 `cached.NewRepository`：写后失效/事务提交后失效/查询缓存版本号失效/GetByIDs 批量",
         "redline": "模式是 Cache-Aside 不是双写；TTL 必须有限（失效失败唯一安全网）；返回值只读（改写开 CloneOnRead）",
         "mods": ["data", "gormx"]},
        {"scene": "本地缓存",
         "use": "`pkg/cache`",
         "tips": "`LocalCache` + `SingleFlightCache` 防击穿；确定性 TTL 测试用 `cachetest.RunWithClock`",
         "redline": "命中返回零拷贝引用；`GetEx` 区分 miss 与值为 nil",
         "mods": ["pkg"]},
        {"scene": "分布式缓存",
         "use": "`data/rediscache` / `redisx`",
         "tips": "CacheAside 的 Redis 实现，管道化 MGet",
         "redline": "`redis.Nil` 是未命中不是错误",
         "mods": ["data", "redisx"]},
        {"scene": "分布式锁 / 信号量",
         "use": "`redisx/distlock` 或 `etcdx/distlock`",
         "tips": "Locker 接口同形可互换（换后端不改调用方）；自动续期 `WithAutoRenew`；Semaphore ZSET 槽位",
         "redline": "`Renew` 返回 false = 锁丢失；Semaphore 无 Redis fail-fast 不降级",
         "mods": ["redisx", "etcdx"]},
        {"scene": "泛型 CRUD / 防丢更新",
         "use": "`gormx/crud`（HTTP 层 `hertz/crudh`）",
         "tips": "`NewBaseService[T, ID, Req]`；repo 实现 `TxRepository` 则 Update 自动事务 + FOR UPDATE 行锁读",
         "redline": "afterUpdate 钩子失败整体回滚",
         "mods": ["gormx", "hertz"]},
        {"scene": "分片定时任务",
         "use": "`timertask`（分布式加嵌套 `timertask/redis`）",
         "tips": "单机 `Scheduler`（cron 表达式复用 pkg/timex/cron，FakeClock 确定性断言）；分布式 RedisBackend + 可靠投递",
         "redline": "可靠投递要接入生命周期才真正生效",
         "mods": ["timertask"]},
    ]),
    ("流控与防护", [
        {"scene": "进程内限流 / 熔断 / 降载",
         "use": "`concurrency`（ratelimit / breaker / shedding / window / batch）",
         "tips": "breaker 三态：open 不放流量（仅 forcePass + 冷却期满半开探测）；连续失败 `WithConsecutiveFailures`；LLM 等长尾用 `WithCooldown` 与 `WithProbeTimeout` 分开配",
         "redline": "规范哨兵 `ErrCircuitOpen`；自实现 Breaker 接口须补 `State()`",
         "mods": ["concurrency"]},
        {"scene": "分布式限流",
         "use": "`redisx/ratelimit`",
         "tips": "Lua 三态返回；故障自动降级本地令牌桶并探活回切",
         "mods": ["redisx"]},
        {"scene": "HTTP 服务端过载保护",
         "use": "`middleware` SheddingMiddleware",
         "tips": "CPU 自适应降载，opt-in 显式挂载",
         "mods": ["middleware"]},
        {"scene": "防爆破锁",
         "use": "`concurrency/ratelimit` Lockout（跨实例 `redisx/distlock` Lockout）",
         "tips": "fail2ban 形态：滑窗失败计数→阈值锁定→成功 Reset/到期自解",
         "redline": "多实例共享计数才用 Redis 版",
         "mods": ["concurrency", "redisx"]},
        {"scene": "防击穿 / 防穿透",
         "use": "`pkg/cache` SingleFlightCache / null marker / `data` bloom",
         "tips": "SingleFlight 包 LocalCache；bloom 用 `cached.WithBloomFilter`（可选件）",
         "redline": "bloom 慎用：全写走装饰器 + 存量预热 + ID 单调，违反即「数据消失」窗口",
         "mods": ["pkg", "data"]},
    ]),
    ("可观测", [
        {"scene": "日志",
         "use": "`logx`",
         "tips": "业务 `WithService`；kv 风格默认；`Ctx*` 自动带 trace_id；后端 `SetDefault` 整体替换；LOG_LEVEL 统一生效",
         "redline": "SDK/基础包包级 logger 必须 `Delegating`；错误字段用 `logx.ErrKey` 常量",
         "mods": ["logx"]},
        {"scene": "脱敏 / 审计日志",
         "use": "`logx` WithFilter / NewAuditLogger",
         "tips": "`FilterKey` 凭证打 ***；审计级别锁 Info、不受 LOG_LEVEL 降级；slog 双向桥 + TraceHandler",
         "mods": ["logx"]},
        {"scene": "指标",
         "use": "`metrics`（挂载 `hertz/metrics`）",
         "tips": "Prometheus Registry + `/metrics` handler；Prometheus 中间件从默认栈拆出（v0.2.0 起），要就显式挂",
         "mods": ["metrics", "hertz"]},
        {"scene": "追踪 / trace_id 串联",
         "use": "`tracing`（+`logx` TraceHandler）",
         "tips": "`traceid.Middleware` 注入 ctx 后 `Ctx*` 自动提取；OTLP gRPC/HTTP 双协议（v0.3.0 起）",
         "redline": "框架挂载二选一勿叠加",
         "mods": ["tracing", "logx"]},
    ]),
    ("配置 · 生命周期 · 基础设施", [
        {"scene": "应用配置",
         "use": "`core/config`",
         "tips": "`LoadBootstrap`（etc/ 约定）/ `LoadFile`（任意路径）/ `Unmarshal`（字节源）共享同一条解码线；严格模式 `WithStrict()` 未知键拒载",
         "redline": "`Unmarshal` 是 yaml 优先于 env——要 12-factor 再调 `ApplyEnvOverlay`",
         "mods": ["core"]},
        {"scene": "零依赖嵌入配置",
         "use": "`pkg/config`",
         "tips": "yaml 子集 parser + env 覆盖 + 必填汇总校验 + 敏感字段 Redact dump",
         "redline": "需要完整 yaml/三方能力选 core/config（pkg 是零依赖仓）",
         "mods": ["pkg", "core"]},
        {"scene": "优雅关停",
         "use": "`core/lifecycle`（`bootstrap` 组合根）",
         "tips": "Manager：closer 注册 / Drain 引用计数 / per-job 超时 / panic 隔离；关停序 nns 反注册 → server.Shutdown",
         "redline": "组件 Close 统一 `Close(ctx) error` + sync.Once 幂等",
         "mods": ["core", "bootstrap"]},
        {"scene": "ID 生成 / 校验 / 哈希",
         "use": "`kitx`",
         "tips": "uidgen（Snowflake 单例 + Extract 元数据 / RFC4122 v4）；validator 校验失败 Unwrap 即 BizError 400；hash 非密码学缓存键",
         "mods": ["kitx"]},
        {"scene": "日志后端桥接 / 文件轮转",
         "use": "`logx/sink` · `logx/hlog` · `logx/hertzlog` · `logx/kitexlog`（嵌套模块按需引入）",
         "tips": "文件/轮转/多目标 Sink；logrus/zap/zerolog/slog 四后端桥 hlog；Kitex klog 桥接",
         "redline": "防递归：hlog 后端别再传给框架桥（kitexlog panic 防御）",
         "mods": ["logx"]},
        {"scene": "注册发现 / Nacos 配置",
         "use": "`registry`（+嵌套 `registry/nacos`·`registry/nacosimpl`），客户端门面 `nns`",
         "tips": "SPI 无关：Registrar/Discoverer/ConfigStore + registrymocks 测试替身；Nacos 重依赖在嵌套模块",
         "mods": ["registry", "nns"]},
    ]),
]

# pkg 子包速查（31 合 1 的「想做什么 → 用哪个子包」）。
# 形状 (想做什么, [子包路径], 一句话)——audit 拿 pkg 仓真实目录校验死键（仓不在旁则跳过）
PKG_QUICK: list[tuple[str, list[str], str]] = [
    ("错误包装/业务错误", ["errors"], "nil-safe Wrap/Cause + BizError 双码（服务形态唯一载体）"),
    ("字符串/脱敏/敏感词", ["stringx"], "工具集 + Replacer 最长匹配替换 + Masker 正则脱敏"),
    ("切片/枚举/类型转换", ["slicex", "enumx", "convert"], ""),
    ("时间边界/cron", ["timex", "timex/cron"], "BeginningOf/EndOf 七对 + cron 表达式"),
    ("数学/路径/分页/指针", ["mathx", "pathx", "pagination", "ptrx"], ""),
    ("JSON 修复/宽松解析", ["jsonrepair"], "UnmarshalLenient + ExtractObject/ExtractArray"),
    ("HTTP + JSON（零依赖）", ["httpjson"], "带解析的轻量请求"),
    ("编码", ["encoding"], ""),
    ("本地缓存", ["cache", "cache/cachetest"], "CacheAside/LocalCache/SingleFlightCache + 契约套件（Clock 注入）"),
    ("泛型容器", ["container"], "Set/Ring/Queue/SafeMap/Slice（并发安全）"),
    ("布隆过滤器", ["bloomx"], ""),
    ("ctx 取值/作用域", ["contextx", "ctxscope"], ""),
    ("缓冲 IO", ["bufiox"], ""),
    ("并发安全 map", ["strmap"], ""),
    ("secrets 凭证", ["secretx"], ""),
    ("shell 词法判据", ["shellx"], "Analyze 事实面（重定向/动态词），判不了就报错"),
    ("出站 SSRF 校验", ["urlguard"], "CheckURL + DialFunc 预解析（与 httpx.WithSafeDial 组合）"),
    ("webhook 回调", ["webhook"], ""),
    ("CLI 子命令", ["cli"], "退出码 0/1/2 写死，不读 os.Args 不 os.Exit"),
    ("迁移包容器", ["pack"], "zip + manifest + JSONL 流式 + Inspect 体检"),
    ("目录 tar 传输", ["tarx"], "Scan/Write/Extract 三件套，拒 Zip Slip"),
    ("断点续传/分片上传", ["resumable"], "会话状态机 + ChunkStore 接口"),
    ("子进程治理", ["procx"], "内存上限 cgroup v2；与 shellx 配套"),
    ("零依赖静态配置", ["config"], "yaml 子集 + env 覆盖 + Redact"),
    ("singleflight 契约测试", ["singleflighttest"], "共享契约套件"),
]

# SKILL.md 红线精华（完整版在 rules.md；前五条判据必须能在 HARD_RULES_MD 里找到出处）
REDLINES_SHORT: list[str] = [
    "业务错误载体唯一：`pkg/errors` BizError + handler 统一 `mw.Fail`——混两套 errors 包，404 断裂成 500",
    "SDK/基础包包级 logger 必须 `logx.Delegating(\"组件名\")`——禁 `Default()`/`WithService`",
    "禁裸 `go func`：用 `concurrency/async` GoSafe（panic 被 recover）",
    "Close 统一 `Close(ctx) error` + sync.Once；bootstrap userFn 返回即服务结束（长驻 `<-app.Context().Done()`）",
    "Option 命名 `XxxOption` 禁裸 `Option`；构造期非法值 fail-fast",
    "缓存命中返回零拷贝引用——改写需 `CloneOnRead`；TTL 必须有限（失效失败时的唯一安全网）",
]

SKILL_DESCRIPTION = """\
在 Go 业务代码中使用/重构时选用 oekit 框架包的选型与用法入口（24 独立模块 + 12 嵌套 + 1 BOM 伞仓）。
触发场景：限流/熔断/降载、分布式锁、缓存、日志、配置、出站 HTTP、RPC 服务、定时任务、错误处理、
SSRF 防护、重试、指标追踪、ID 校验、CRUD、服务启动引导，或不确定该用哪个 oekit 模块/子包时。
含场景选型表、全矩阵硬红线、pkg 子包速查索引。发版/tag/CHANGELOG 用 oekit-release；
issue 扫描/清零用 oekit-sweep。"""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def desc_head(desc: str, limit: int = 90) -> str:
    """DESC 首句（括号外首个 ；/。 截断；超长再截断加 …）。

    pkg 的 DESC 首个「；」在包名清单括号**内**（desc_count_selftest 同款坑），
    所以按括号深度判——深度 0 的分隔符才算句界。
    """
    depth = 0
    cut = len(desc)
    for i, ch in enumerate(desc):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in "；。" and i > 0:
            cut = i
            break
    head = desc[:cut].rstrip("；、")
    if len(head) > limit:
        head = head[:limit - 1]
        # 回退到最近的词界（/ 、 空格），避免词中截断
        for sep in ("/", "、", " "):
            idx = head.rfind(sep)
            if idx > limit // 2:
                head = head[:idx]
                break
        head = head.rstrip() + "…"
    return head


def _scenario_tables() -> str:
    out = []
    for group, rows in SCENARIOS:
        out.append(f"### {group}\n")
        out.append("| 我要做什么 | 用什么 | 组合提示 | 红线 |")
        out.append("|---|---|---|---|")
        for r in rows:
            tips = r.get("tips", "—")
            redline = r.get("redline", "—")
            out.append(f"| {r['scene']} | {r['use']} | {tips} | {redline} |")
        out.append("")
    return "\n".join(out)


def _pkg_table() -> str:
    out = ["| 想做什么 | 子包 | 说明 |", "|---|---|---|"]
    for what, subs, desc in PKG_QUICK:
        cell = " / ".join(f"`{s}`" for s in subs)
        out.append(f"| {what} | {cell} | {desc or '—'} |")
    return "\n".join(out)


def render_rules() -> str:
    return (
        "# oekit 硬规则速览（全矩阵）\n\n"
        "> 由 matrix-tools/scripts/rules.py 单源渲染（与各仓 AGENTS.md 冷启动模板同源）——"
        "改硬规则改 rules.py，勿直接改本文件。\n\n"
        + HARD_RULES_MD
    )


def _root_module_table() -> str:
    out = []
    for layer, mods in LAYERS:
        out.append(f"## {layer}")
        out.append("")
        out.append("| 模块 | 一句话 | 深入 |")
        out.append("|---|---|---|")
        for m in mods:
            out.append(f"| [{m}](https://github.com/oekit/{m}) | {desc_head(DESC[m])} | `{m}/AGENTS.md` |")
        out.append("")
    return "\n".join(out)


def _nested_module_table() -> str:
    out = ["| 父仓 | 模块 | 一句话 | 安装 |", "|---|---|---|---|"]
    for n in NESTED_MODULES:
        out.append(f"| {n['repo']} | `{n['path']}` | {desc_head(n['desc'], 110)} | "
                   f"`go get github.com/oekit/{n['module']}@latest` |")
    return "\n".join(out)


def render_modules() -> str:
    return "\n".join([
        "# oekit 模块索引（层分组）",
        "",
        "> 由 gen_skill.py 从 matrix.py（MODULES/NESTED_MODULES/DESC）渲染——模块集合以 matrix.py 为真相源，"
        "本文件勿手改。深入阅读 = 该仓 AGENTS.md（首段 DESC + 硬规则速览）→ 包 doc.go → 站点指南。",
        "",
        "安装：按需 `go get github.com/oekit/<mod>@latest`；一次拉齐兼容快照 "
        "`go get github.com/oekit/oekit@latest`（BOM 含 breaking 索引）。"
        "查最新 tag 用 `GOPROXY=direct`（proxy 缓存滞后）。",
        "",
        _root_module_table(),
        "## 嵌套模块（12 个可选装——重依赖隔离载体，父仓保持零第三方依赖）",
        "",
        _nested_module_table(),
        "",
    ])


def render_skill() -> str:
    fm = (
        "---\n"
        "name: oekit\n"
        "description: >-\n"
        + "".join(f"  {ln}\n" for ln in SKILL_DESCRIPTION.splitlines())
        + "---\n\n"
    )
    body = f"""# oekit 包选型与用法入口

> oekit = Open Easy Kit：24 独立模块 + 12 嵌套模块 + 1 BOM 伞仓，依赖自底向上无环。
> 本 skill 由 `matrix-tools/scripts/gen_skill.py` 渲染——场景表/pkg 速查为手写数据、
> 红线与模块索引为生成产物；修订请改 gen_skill.py 数据区（或 rules.py / matrix.py），勿手改本文件。

## 使用流（四步）

1. **选型**：下面「场景选型表」定位模块/子包；基础工具类需求（字符串/时间/脱敏/CLI/子进程…）
   先查同目录 `pkg.md` 速查表
2. **拿准签名**：`go doc github.com/oekit/<pkg>` 或读包 `doc.go`——禁止凭记忆猜 API
3. **套红线**：下方速览；完整版同目录 `rules.md`（与各仓 AGENTS.md「硬规则速览」同源）
4. **深入**：同目录 `modules.md`（模块索引 + 嵌套模块安装行）→ 该仓 AGENTS.md → 站点指南

## 场景选型表

{_scenario_tables()}

## 红线速览（完整版 rules.md；各仓 AGENTS.md 同源）

{chr(10).join(f"- {r}" for r in REDLINES_SHORT)}

- 版本对齐：`go get github.com/oekit/oekit@latest` 一次拉齐兼容快照（BOM 自带 breaking 索引）；
  查最新 tag 务必 `GOPROXY=direct`（proxy 版本列表缓存滞后）

## 流程任务（另装的兄弟 skill）

- 发版 / tag / CHANGELOG / 地板 bump → **oekit-release**
- issue 扫描 / 清零 / 巡检 → **oekit-sweep**

## 深入入口

- 模块索引（层分组 + 嵌套模块安装行）：同目录 `modules.md`
- 下游/远端 AI：<https://oekit.github.io/llms.txt> · 站点选型指南：
  <https://oekit.github.io/guide/selection.html>
- oekit 各仓本地检出为平级目录（matrix.py 支持三种布局探测）；找不到检出根时问用户
"""
    return fm + body


def render_pkg() -> str:
    return (
        "# pkg 子包速查（31 合 1，零第三方依赖）\n\n"
        "> 由 gen_skill.py 渲染——修订改 gen_skill.py 的 PKG_QUICK；"
        "audit 会拿 pkg 仓真实目录校验死键。API 以 `go doc github.com/oekit/pkg/<sub>` 为准。\n\n"
        f"{_pkg_table()}\n"
    )


def render_site() -> str:
    return f"""---
title: 选型速查（场景 → 包）
---

用 oekit 重构/实现功能时的「我要做 X → 用什么包」速查。与 AI 入口 skill
（matrix-tools `skills/oekit`）**同源渲染**（`scripts/gen_skill.py`）——手改会被巡检覆盖；
模块事实以 [matrix.py](https://github.com/oekit/matrix-tools/blob/main/matrix.py) 为真相源。

## 使用流

1. 下面场景表定位模块/子包；基础工具需求查 [pkg 子包速查](#pkg-子包速查31-合-1零第三方依赖)
2. `go doc github.com/oekit/<pkg>` 或读包 `doc.go` 拿准签名
3. 对照[硬规则](#硬规则速览全矩阵适用)
4. 深入：该仓 AGENTS.md / 对应指南（[日志](/guide/logging) · [错误](/guide/errors) ·
   [缓存](/guide/caching) · [HTTP](/guide/http-service) · [RPC](/guide/rpc-service) ·
   [可观测](/guide/observability)）

## 场景选型表

{_scenario_tables()}

## pkg 子包速查（31 合 1，零第三方依赖）

{_pkg_table()}

{HARD_RULES_MD}

## 模块索引

{_root_module_table()}
### 嵌套模块（重依赖隔离载体）

{_nested_module_table()}

> 模块事实源：[matrix.py](https://github.com/oekit/matrix-tools/blob/main/matrix.py)；
> 机器可读索引：[llms.txt](/llms.txt) · 版本矩阵：[versions.json](/versions.json)
"""


# ---------------------------------------------------------------------------
# 写盘 / 校验
# ---------------------------------------------------------------------------

def write_all(site: bool = False) -> list[str]:
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in (("SKILL.md", render_skill()), ("rules.md", render_rules()),
                          ("modules.md", render_modules()), ("pkg.md", render_pkg())):
        (SKILL_DIR / name).write_text(content, encoding="utf-8")
        written.append(str(SKILL_DIR / name))
    if site:
        sel = repo_dir(SITE_REPO, os.environ.get("MATRIX_BASE", str(MT_ROOT.parent))) / "docs" / "guide" / "selection.md"
        sel.parent.mkdir(parents=True, exist_ok=True)
        sel.write_text(render_site(), encoding="utf-8")
        written.append(str(sel))
    return written


def check_drift() -> list[str]:
    """--check / audit 共用：返回漂移清单（空 = 新鲜）。"""
    drift = []
    expected = {"SKILL.md": render_skill(), "rules.md": render_rules(),
                "modules.md": render_modules(), "pkg.md": render_pkg()}
    for name, exp in expected.items():
        p = SKILL_DIR / name
        if not p.exists():
            drift.append(f"skills/oekit/{name} 缺失（跑 make gen-skill）")
            continue
        if p.read_text(encoding="utf-8") != exp:
            drift.append(f"skills/oekit/{name} 与 gen_skill.py 渲染不一致（跑 make gen-skill）")
    try:
        sel = repo_dir(SITE_REPO, os.environ.get("MATRIX_BASE", str(MT_ROOT.parent))) / "docs" / "guide" / "selection.md"
        if sel.exists() and sel.read_text(encoding="utf-8") != render_site():
            drift.append("site: guide/selection.md 与 gen_skill.py 渲染不一致（跑 make gen-skill --site）")
    except FileNotFoundError:
        pass  # 站点仓不在旁（本地部分检出）——站点新鲜度交给有站点仓的环境
    return drift


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--check" in argv:
        d = check_drift()
        for line in d:
            print(f"❌ {line}")
        raise SystemExit(1 if d else 0)
    # 写入型生成器家族纪律：产物必须落在已提交基线上（工作树脏先提交再生成）。
    # 首次落地例外：生成器与产物同一个 PR，落地后的后续运行即受守卫约束。
    _site = "--site" in argv
    _dirty_skip = os.environ.get("GEN_SKILL_ALLOW_DIRTY") == "1"
    if not _dirty_skip:
        _dirs = [MT_ROOT]
        if _site:
            try:
                _dirs.append(repo_dir(SITE_REPO, os.environ.get("MATRIX_BASE", str(MT_ROOT.parent))))
            except FileNotFoundError:
                pass
        ensure_clean_all(_dirs, "gen_skill")
    for p in write_all(site=_site):
        print(f"✓ {p}")
