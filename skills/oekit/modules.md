# oekit 模块索引（层分组）

> 由 gen_skill.py 从 matrix.py（MODULES/NESTED_MODULES/DESC）渲染——模块集合以 matrix.py 为真相源，本文件勿手改。深入阅读 = 该仓 AGENTS.md（首段 DESC + 硬规则速览）→ 包 doc.go → 站点指南。

安装：按需 `go get github.com/oekit/<mod>@latest`；一次拉齐兼容快照 `go get github.com/oekit/oekit@latest`（BOM 含 breaking 索引）。查最新 tag 用 `GOPROXY=direct`（proxy 缓存滞后）。

## 应用引导 · 框架生态

| 模块 | 一句话 | 深入 |
|---|---|---|
| [bootstrap](https://github.com/oekit/bootstrap) | 应用生命周期引导——New(WithServiceName/WithShutdownTimeout/WithBeforeServe) + Run(fn) + healthz | `bootstrap/AGENTS.md` |
| [hertz](https://github.com/oekit/hertz) | Hertz 运行时集成（模块根扁平化）——日志桥接、生命周期、SSE/health/metrics、适配器、OTel、crudh/validatorh（基于 kitx… | `hertz/AGENTS.md` |
| [kitex](https://github.com/oekit/kitex) | Kitex 生态——日志桥接、治理热更、生命周期、BizError 桥接、kitexotel、istio（xDS 动态熔断，原 istio 微库收编） | `kitex/AGENTS.md` |
| [middleware](https://github.com/oekit/middleware) | Hertz 标准中间件栈（模块根 mw，双层目录已收平）+ contrib 桥接 + hmw + SheddingMiddleware 自适应降载 + chainmw… | `middleware/AGENTS.md` |
| [workflow](https://github.com/oekit/workflow) | 组合式流程编排——串行/并行/异步/条件分支 + Hook AOP | `workflow/AGENTS.md` |
| [kitex-examples](https://github.com/oekit/kitex-examples) | Kitex echo 示例——client/server + IDL + 治理配置 | `kitex-examples/AGENTS.md` |
| [k8s](https://github.com/oekit/k8s) | Kubernetes 客户端封装——RESTConfig/Clientset/DynamicClient 构建（显式 kubeconfig → KUBECONFIG 显式意图… | `k8s/AGENTS.md` |

## 通信 · 服务治理

| 模块 | 一句话 | 深入 |
|---|---|---|
| [httpx](https://github.com/oekit/httpx) | HTTP 客户端 + 负载均衡——服务发现/熔断（规范哨兵 ErrCircuitOpen）/重试/指标/OTel 透传 | `httpx/AGENTS.md` |
| [nns-endpoint](https://github.com/oekit/nns-endpoint) | NNS 端点工厂——per-service 熔断、换实例重试、gRPC 池 | `nns-endpoint/AGENTS.md` |
| [network](https://github.com/oekit/network) | 轻量网络客户端——WebSocket/NATS/MQ 抽象、连接与流工具 | `network/AGENTS.md` |
| [nns](https://github.com/oekit/nns) | NNS 服务治理客户端——provider 无关发现/配置/实例缓存 | `nns/AGENTS.md` |
| [registry](https://github.com/oekit/registry) | 注册发现核心（provider 无关 SPI：Registrar/Discoverer/Subscriber… | `registry/AGENTS.md` |

## 数据访问

| 模块 | 一句话 | 深入 |
|---|---|---|
| [gormx](https://github.com/oekit/gormx) | SQL/GORM 全家——连接池增强、框架无关 CRUD Service、仓储（原 oekit/gorm，v0.3.0 更名） | `gormx/AGENTS.md` |
| [redisx](https://github.com/oekit/redisx) | Redis 全家——客户端封装（根包）、分布式缓存 cache/、分布式锁 distlock（Locker 同形可互换；Renew 手动续期（false=锁丢失，不进… | `redisx/AGENTS.md` |
| [etcdx](https://github.com/oekit/etcdx) | etcd 全家——客户端封装（根包：生产默认值/Health 探活/Close(ctx) 幂等）、registry 注册发现实现（SPI 对接 registry… | `etcdx/AGENTS.md` |
| [data](https://github.com/oekit/data) | 数据访问门面——复刻 ekit data/ 能力面：cache/rediscache/crud/distlock/gormx/redisx/timertask… | `data/AGENTS.md` |
| [timertask](https://github.com/oekit/timertask) | 分片定时任务（FNV-1a、心跳续约） | `timertask/AGENTS.md` |

## 可观测性

| 模块 | 一句话 | 深入 |
|---|---|---|
| [logx](https://github.com/oekit/logx) | 统一日志门面（多后端可替换 + caller 归属；Sink 已迁嵌套模块 logx/sink，根包纯 stdlib） | `logx/AGENTS.md` |
| [tracing](https://github.com/oekit/tracing) | OpenTelemetry 追踪 + trace_id 真相源（v0.3.0 起 OTLP 双协议导出：gRPC 缺省… | `tracing/AGENTS.md` |
| [metrics](https://github.com/oekit/metrics) | Prometheus 门面 + 弹性指标族 | `metrics/AGENTS.md` |

## 基础库

| 模块 | 一句话 | 深入 |
|---|---|---|
| [pkg](https://github.com/oekit/pkg) | 微工具集 33 合 1（errors/bloomx/contextx/ctxscope/container/stringx/slicex/enumx/convert… | `pkg/AGENTS.md` |
| [concurrency](https://github.com/oekit/concurrency) | 并发与流控原语（async/gopool/group/singleflight/progress/syncx/ratelimit/retry）+ window 滑动窗口… | `concurrency/AGENTS.md` |
| [core](https://github.com/oekit/core) | 应用基建件——governance 治理 schema、config 配置加载（LoadBootstrap/LoadDotEnv/Get* 系、default/env… | `core/AGENTS.md` |
| [kitx](https://github.com/oekit/kitx) | 标识与校验——uidgen（Snowflake 全局单例+Extract 元数据、手写 RFC 4122 v4 UUID 零第三方依赖、RandomHex… | `kitx/AGENTS.md` |

## 嵌套模块（12 个可选装——重依赖隔离载体，父仓保持零第三方依赖）

| 父仓 | 模块 | 一句话 | 安装 |
|---|---|---|---|
| bootstrap | `nacos` | bootstrap 的 Nacos/NNS 服务治理接入——以 BootstrapperOption 注入，父仓保持零 Nacos 依赖（v0.3.0 起注册延迟到 App.Ready 之后） | `go get github.com/oekit/bootstrap/nacos@latest` |
| bootstrap | `obs` | bootstrap 的可观测性接入——以 BootstrapperOption 注入 Prometheus 指标（oekit/metrics）与 OpenTelemetry 追踪（oekit… | `go get github.com/oekit/bootstrap/obs@latest` |
| logx | `sink` | logx 的输出目标实现——Sink 抽象 + MultiSink/FileSink/RotatingSink 三个内置实现，重依赖隔离在此，根包纯 stdlib | `go get github.com/oekit/logx/sink@latest` |
| logx | `hlog` | 基于 Hertz hlog 的 logx.Logger 实现（结构化 kv 降级为字符串拼接，输出格式与 Hertz hlog 对齐） | `go get github.com/oekit/logx/hlog@latest` |
| logx | `hertzlog` | Hertz 日志后端桥接——logrus/zap/zerolog/slog 四后端各自实现 hlog.FullLogger（依赖隔离，logx 根包不引三方日志库） | `go get github.com/oekit/logx/hertzlog@latest` |
| logx | `kitexlog` | Kitex 日志后端桥接——把 logx.Logger 适配为 Kitex klog.FullLogger（依赖隔离，logx 根包不引 kitex） | `go get github.com/oekit/logx/kitexlog@latest` |
| timertask | `redis` | timertask 的 Redis 后端——RedisBackend/ShardManager 与可靠投递，go-redis 隔离在此，根包零 redis 依赖（v0.2.0 起） | `go get github.com/oekit/timertask/redis@latest` |
| registry | `nacosimpl` | Nacos SPI 实现——Discoverer/ConfigStore/Registrar 基于 nacos-sdk-go v2（nacos-sdk 重依赖隔离在此，registry 根仓零 nacos-sdk） | `go get github.com/oekit/registry/nacosimpl@latest` |
| registry | `nacos` | Nacos 客户端门面——Bootstrap 配置、client 工厂与 config 加载（实现 core/config.ConfigSource，配 ConfigWatcher 热更新） | `go get github.com/oekit/registry/nacos@latest` |
| kitex | `observability/tracing/instrumentation/kitexotel` | Kitex 的 OpenTelemetry tracing 接入——client/server suite + metainfo 透传（obs-opentelemetry 重依赖隔离在此，kitex 根仓回归零… | `go get github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel@latest` |
| kitex | `istio` | Kitex 的 xDS / Proxyless / Istio 集成桥接——xDS 资源管理器 + 动态熔断（kitex-contrib/xds 重依赖隔离在此，kitex 根仓回归零 apache/thrift） | `go get github.com/oekit/kitex/istio@latest` |
| kitex | `cmd/thrift2openapi` | thrift IDL（api.* 注解体系）→ OpenAPI 3.0 文档生成器（thriftgo 真 parser，go install 即用）——替换各服务手搓 regex… | `go get github.com/oekit/kitex/cmd/thrift2openapi@latest` |
