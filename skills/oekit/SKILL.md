---
name: oekit
description: >-
  在 Go 业务代码中使用/重构时选用 oekit 框架包的选型与用法入口（24 独立模块 + 12 嵌套 + 1 BOM 伞仓）。
  触发场景：限流/熔断/降载、分布式锁、缓存、日志、配置、出站 HTTP、RPC 服务、定时任务、错误处理、
  SSRF 防护、重试、指标追踪、ID 校验、CRUD、服务启动引导，或不确定该用哪个 oekit 模块/子包时。
  含场景选型表、全矩阵硬红线、pkg 子包速查索引。发版/tag/CHANGELOG 用 oekit-release；
  issue 扫描/清零用 oekit-sweep。
---

# oekit 包选型与用法入口

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

### 服务构建

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| 起 HTTP 服务（生产全栈） | `bootstrap` + `hertz` + `middleware`（注册发现加 `nns`） | `hertz.RunServer` 的 `WithOnReady` 里触发 `app.Ready()`（就绪门，NNS 才注册）；标准栈 `mw.DefaultMiddleware` | userFn 返回即服务结束：长驻必须 `<-app.Context().Done()`；无 Recovery 则 panic 直接断连 |
| 起 RPC 服务（Kitex） | `kitex` + `bootstrap`（+`nns`） | `RunServer(WithOnReady/WithReadyAddr)`；`SetDefaultPanicHandler` 兜底；治理热更 `NewGovernanceConfig` | 就绪门不触发 ⇒ 服务永不进注册中心；治理配置是全量同步，勿混用代码级 retry |
| handler 错误出口 | `pkg/errors` + `middleware` | BizError 命名工厂（NewNotFoundError 等）；`mw.Fail(ctx, c, err)` 统一出口（NotFound→404、兜底 500） | BizError 载体唯一——混两套 errors 包 404 断裂成 500；error 禁 `_` 忽略 |
| 响应体自管的服务 | `middleware` | `StackConfig{DisableResponseWrap: true}` 一键（TraceID→Recovery→AccessLog）或 `MinimalMiddleware` 手拼 | Recovery 仍硬性要求（Hertz 无默认 PanicHandler） |
| 健康检查 / K8s 就绪 | `bootstrap/healthz` + `hertz` | liveness/readiness 多 check 并发、panic 隔离；业务健康门过了再 `app.Ready()` | TCP 探活只证端口可建连，不证业务健康 |
| K8s 客户端 / Lease 选主 | `k8s`（+`k8s/leaderelection`） | kubeconfig→KUBECONFIG→in-cluster 逐级回退；主备单活 OnStarted/StoppedLeading 回调 | 失权与 ctx 取消并存时失权优先上抛 ErrLostLeadership |
| 组合式流程编排 | `workflow` | 串行/并行/异步/条件分支 + Hook AOP | — |

### 出站调用与弹性

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| 内部服务 HTTP 调用（发现+熔断+重试+LB） | `httpx`（+`nns`） | 存量 net/http 用 `NewRoundTripper`/`StdClient` 一行接入治理；LB 用 loadbalance Selector（p2c/一致性哈希） | 重试缺省关、显式开；非 2xx 非错误才计失败 |
| 出站 SSRF 防护 | `pkg/urlguard` + `httpx` | 一行组合 `httpx.WithSafeDial`；豁免只走构造态 `WithAllow*` | 拨号层预解析 + RemoteAddr 核对闭合 TOCTOU——别在调用方自己写解析 |
| RPC 依赖的 HTTP 端点（熔断+换实例重试） | `nns-endpoint` | `NewHTTPEndpointFactory(nnsc.ServiceClient()).MakeEndpoint("dep-svc")` | 4xx 不计熔断失败 |
| 重试（带返回值/退避抖动） | `concurrency/retry` | `DoValue[T]`（`Do` 已委托给它，只有一份退避实现）；服务端 Retry-After 优先（DelayHint） | 别手写退避——封顶/抖动/可重试判定都在 retry 里 |
| 中间件一份逻辑两端跑 | `core/chain` + `core/transport` → `middleware/chainmw` | kitex 经 `kitex/chainmw.ToKitex` 复用同一实现（Recovery/AccessLog/MetadataPropagate） | 监控上报钩子内 panic 已隔离，别在钩子里再 recover 吞语义 |
| RPC 业务异常透传 | `kitex` `WithBizErrorBridge` | 对端 BizStatusError → 本地 BizError，handler `mw.Fail` 无感处理 | timeout→50400/504、no-instance→50300/503——映射是框架契约，别自己猜 |

### 数据访问

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| CRUD 缓存编排 | `data/cached`（多实例加 `data/twolevel`） | 三行接入 `cached.NewRepository`：写后失效/事务提交后失效/查询缓存版本号失效/GetByIDs 批量 | 模式是 Cache-Aside 不是双写；TTL 必须有限（失效失败唯一安全网）；返回值只读（改写开 CloneOnRead） |
| 本地缓存 | `pkg/cache` | `LocalCache` + `SingleFlightCache` 防击穿；确定性 TTL 测试用 `cachetest.RunWithClock` | 命中返回零拷贝引用；`GetEx` 区分 miss 与值为 nil |
| 分布式缓存 | `data/rediscache` / `redisx` | CacheAside 的 Redis 实现，管道化 MGet | `redis.Nil` 是未命中不是错误 |
| 分布式锁 / 信号量 | `redisx/distlock` 或 `etcdx/distlock` | Locker 接口同形可互换（换后端不改调用方）；自动续期 `WithAutoRenew`；Semaphore ZSET 槽位 | `Renew` 返回 false = 锁丢失；Semaphore 无 Redis fail-fast 不降级 |
| 泛型 CRUD / 防丢更新 | `gormx/crud`（HTTP 层 `hertz/crudh`） | `NewBaseService[T, ID, Req]`；repo 实现 `TxRepository` 则 Update 自动事务 + FOR UPDATE 行锁读 | afterUpdate 钩子失败整体回滚 |
| 分片定时任务 | `timertask`（分布式加嵌套 `timertask/redis`） | 单机 `Scheduler`（cron 表达式复用 pkg/timex/cron，FakeClock 确定性断言）；分布式 RedisBackend + 可靠投递 | 可靠投递要接入生命周期才真正生效 |

### 流控与防护

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| 进程内限流 / 熔断 / 降载 | `concurrency`（ratelimit / breaker / shedding / window / batch） | breaker 三态：open 不放流量（仅 forcePass + 冷却期满半开探测）；连续失败 `WithConsecutiveFailures`；LLM 等长尾用 `WithCooldown` 与 `WithProbeTimeout` 分开配 | 规范哨兵 `ErrCircuitOpen`；自实现 Breaker 接口须补 `State()` |
| 分布式限流 | `redisx/ratelimit` | Lua 三态返回；故障自动降级本地令牌桶并探活回切 | — |
| HTTP 服务端过载保护 | `middleware` SheddingMiddleware | CPU 自适应降载，opt-in 显式挂载 | — |
| 防爆破锁 | `concurrency/ratelimit` Lockout（跨实例 `redisx/distlock` Lockout） | fail2ban 形态：滑窗失败计数→阈值锁定→成功 Reset/到期自解 | 多实例共享计数才用 Redis 版 |
| 防击穿 / 防穿透 | `pkg/cache` SingleFlightCache / null marker / `data` bloom | SingleFlight 包 LocalCache；bloom 用 `cached.WithBloomFilter`（可选件） | bloom 慎用：全写走装饰器 + 存量预热 + ID 单调，违反即「数据消失」窗口 |

### 可观测

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| 日志 | `logx` | 业务 `WithService`；kv 风格默认；`Ctx*` 自动带 trace_id；后端 `SetDefault` 整体替换；LOG_LEVEL 统一生效 | SDK/基础包包级 logger 必须 `Delegating`；错误字段用 `logx.ErrKey` 常量 |
| 脱敏 / 审计日志 | `logx` WithFilter / NewAuditLogger | `FilterKey` 凭证打 ***；审计级别锁 Info、不受 LOG_LEVEL 降级；slog 双向桥 + TraceHandler | — |
| 指标 | `metrics`（挂载 `hertz/metrics`） | Prometheus Registry + `/metrics` handler；Prometheus 中间件从默认栈拆出（v0.2.0 起），要就显式挂 | — |
| 追踪 / trace_id 串联 | `tracing`（+`logx` TraceHandler） | `traceid.Middleware` 注入 ctx 后 `Ctx*` 自动提取；OTLP gRPC/HTTP 双协议（v0.3.0 起） | 框架挂载二选一勿叠加 |

### 配置 · 生命周期 · 基础设施

| 我要做什么 | 用什么 | 组合提示 | 红线 |
|---|---|---|---|
| 应用配置 | `core/config` | `LoadBootstrap`（etc/ 约定）/ `LoadFile`（任意路径）/ `Unmarshal`（字节源）共享同一条解码线；严格模式 `WithStrict()` 未知键拒载 | `Unmarshal` 是 yaml 优先于 env——要 12-factor 再调 `ApplyEnvOverlay` |
| 零依赖嵌入配置 | `pkg/config` | yaml 子集 parser + env 覆盖 + 必填汇总校验 + 敏感字段 Redact dump | 需要完整 yaml/三方能力选 core/config（pkg 是零依赖仓） |
| 优雅关停 | `core/lifecycle`（`bootstrap` 组合根） | Manager：closer 注册 / Drain 引用计数 / per-job 超时 / panic 隔离；关停序 nns 反注册 → server.Shutdown | 组件 Close 统一 `Close(ctx) error` + sync.Once 幂等 |
| ID 生成 / 校验 / 哈希 | `kitx` | uidgen（Snowflake 单例 + Extract 元数据 / RFC4122 v4）；validator 校验失败 Unwrap 即 BizError 400；hash 非密码学缓存键 | — |
| 日志后端桥接 / 文件轮转 | `logx/sink` · `logx/hlog` · `logx/hertzlog` · `logx/kitexlog`（嵌套模块按需引入） | 文件/轮转/多目标 Sink；logrus/zap/zerolog/slog 四后端桥 hlog；Kitex klog 桥接 | 防递归：hlog 后端别再传给框架桥（kitexlog panic 防御） |
| 注册发现 / Nacos 配置 | `registry`（+嵌套 `registry/nacos`·`registry/nacosimpl`），客户端门面 `nns` | SPI 无关：Registrar/Discoverer/ConfigStore + registrymocks 测试替身；Nacos 重依赖在嵌套模块 | — |


## 红线速览（完整版 rules.md；各仓 AGENTS.md 同源）

- 业务错误载体唯一：`pkg/errors` BizError + handler 统一 `mw.Fail`——混两套 errors 包，404 断裂成 500
- SDK/基础包包级 logger 必须 `logx.Delegating("组件名")`——禁 `Default()`/`WithService`
- 禁裸 `go func`：用 `concurrency/async` GoSafe（panic 被 recover）
- Close 统一 `Close(ctx) error` + sync.Once；bootstrap userFn 返回即服务结束（长驻 `<-app.Context().Done()`）
- Option 命名 `XxxOption` 禁裸 `Option`；构造期非法值 fail-fast
- 缓存命中返回零拷贝引用——改写需 `CloneOnRead`；TTL 必须有限（失效失败时的唯一安全网）

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
