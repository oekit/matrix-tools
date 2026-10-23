#!/usr/bin/env python3
"""oekit 模块矩阵——唯一事实源与共享工具。

仓库名解析支持三种本地布局（按序探测）：
  <base>/<name>          # 规范布局（Actions / 新检出）
  <base>/ekit-<name>     # 历史布局（本会话遗留的本地检出）
  <base>/repos/<name>    # Actions 批量克隆布局
"""
from __future__ import annotations
import os
import re
import subprocess
from pathlib import Path

# 规范模块清单（24 个，依赖自底向上的顺序）
MODULES = [
    "pkg", "concurrency", "logx", "core", "kitx",
    "metrics", "tracing", "registry",
    "data", "gormx", "redisx", "etcdx", "httpx", "network", "timertask", "middleware", "k8s",
    "nns", "nns-endpoint",
    "bootstrap", "hertz", "kitex", "workflow", "kitex-examples",
]

# 嵌套模块清单（7 个，matrix-tools#37）
#
# 嵌套的职责是**依赖隔离**，不是「目录组织」——把重依赖（hertz / kitex / go-redis /
# 阿里云 Nacos SDK / prometheus+otel）关进子模块，父仓根包保持零第三方依赖。
# issue #37 的架构裁决：**不给它们换位置**（换独立仓隔离效果完全等价，却要付
# 「全部 import 变更 + 仓库数 24→32」；合并回父仓不可行——Go 的 import 即依赖，
# 会毁掉零捆绑定位）。
#
# 为什么与 MODULES **分开**：MODULES 决定 versions.json 的 latest/matrixDeps 与
# 依赖地板统计，扩进去会污染那些口径（裁决明令「不动 MODULES 语义」）。
# NESTED_MODULES 只影响两件事——**对外计数**与**llms.txt 的可安装条目**。
#
# 形状：repo=父仓名 / path=子目录 / module=import 路径 / desc=llms.txt 一行说明。
# 真相源纪律同 MODULES：这里只声明「有什么」，版本号从 git tag 推导（不手写）。
NESTED_MODULES = [
    {"repo": "bootstrap", "path": "nacos", "module": "github.com/oekit/bootstrap/nacos",
     "desc": "bootstrap 的 Nacos/NNS 服务治理接入——以 BootstrapperOption 注入，父仓保持零 Nacos 依赖（v0.3.0 起注册延迟到 App.Ready 之后）"},
    {"repo": "bootstrap", "path": "obs", "module": "github.com/oekit/bootstrap/obs",
     "desc": "bootstrap 的可观测性接入——以 BootstrapperOption 注入 Prometheus 指标（oekit/metrics）与 OpenTelemetry 追踪（oekit/tracing），父仓保持零 prometheus/otel 依赖"},
    {"repo": "logx", "path": "sink", "module": "github.com/oekit/logx/sink",
     "desc": "logx 的输出目标实现——Sink 抽象 + MultiSink/FileSink/RotatingSink 三个内置实现，重依赖隔离在此，根包纯 stdlib"},
    {"repo": "logx", "path": "hlog", "module": "github.com/oekit/logx/hlog",
     "desc": "基于 Hertz hlog 的 logx.Logger 实现（结构化 kv 降级为字符串拼接，输出格式与 Hertz hlog 对齐）"},
    {"repo": "logx", "path": "hertzlog", "module": "github.com/oekit/logx/hertzlog",
     "desc": "Hertz 日志后端桥接——logrus/zap/zerolog/slog 四后端各自实现 hlog.FullLogger（依赖隔离，logx 根包不引三方日志库）"},
    {"repo": "logx", "path": "kitexlog", "module": "github.com/oekit/logx/kitexlog",
     "desc": "Kitex 日志后端桥接——把 logx.Logger 适配为 Kitex klog.FullLogger（依赖隔离，logx 根包不引 kitex）"},
    {"repo": "timertask", "path": "redis", "module": "github.com/oekit/timertask/redis",
     "desc": "timertask 的 Redis 后端——RedisBackend/ShardManager 与可靠投递，go-redis 隔离在此，根包零 redis 依赖（v0.2.0 起）"},
    {"repo": "registry", "path": "nacosimpl", "module": "github.com/oekit/registry/nacosimpl",
     "desc": "Nacos SPI 实现——Discoverer/ConfigStore/Registrar 基于 nacos-sdk-go v2（nacos-sdk 重依赖隔离在此，registry 根仓零 nacos-sdk）"},
    {"repo": "registry", "path": "nacos", "module": "github.com/oekit/registry/nacos",
     "desc": "Nacos 客户端门面——Bootstrap 配置、client 工厂与 config 加载（实现 core/config.ConfigSource，配 ConfigWatcher 热更新）"},
]

# 对外计数口径（唯一出处，站点文案与审计门禁都以此为准）
MODULE_COUNT = len(MODULES)                       # 24 个独立发版的模块仓
NESTED_MODULE_COUNT = len(NESTED_MODULES)         # 9 个嵌套模块
# 伞仓 oekit（BOM）**是独立发版单元**——它有自己的 tag 与 GitHub Release
# （v0.1.11 等），下游 `go get github.com/oekit/oekit@latest` 是一次独立的
# 版本获取。按「可独立 go get 的发版单元」口径，它必须计入（裁决定死 34）。
BOM_RELEASE_UNIT = 1
GO_MODULE_COUNT = MODULE_COUNT + NESTED_MODULE_COUNT + BOM_RELEASE_UNIT  # 34 个 Go module
# 工作区里的 go.mod 总数 = 34（24 根 + 9 嵌套 + 1 伞仓），与 GO_MODULE_COUNT 相等

ORG = "oekit"
SITE_REPO = "oekit.github.io"
STALE_RE = re.compile(r"yi-nology|openekit|github\.com/oekit/ekit-")

# ---------- Go 版本地板三层口径（matrix-tools#33，负责人裁决） ----------
#
# 裁决原文：「我接受有些包依赖高版本的 golang。基础包需要依赖低版本的 go1.24，
# 业务包依赖 1.24 / 1.25 / 1.26 都行。」
#
# 因此门禁**只放宽「地板值」，不放宽「成对」**：
#   · 基础层：go.mod 的 go 指令必须 ≤ 1.24（基础包是所有服务的地基，
#     地板抬高会传染到全部下游）
#   · tracing：豁免——go.opentelemetry.io/otel v1.47.0 的 go.mod 明写 go 1.26.0，
#     属「接受高版本依赖」那一类，门禁不得报它
#   · 业务层：1.24 / 1.25 / 1.26 均可，不报
#   · **全矩阵通用**：go.mod 的 go 指令与 CI 的 go-version 必须成对——
#     CI 用 1.26 构建、go.mod 声明 1.24，等于最低支持版本从来没被验证过
GO_FLOOR_BASE_MAX = (1, 24)
GO_FLOOR_BUSINESS_OK = {(1, 24), (1, 25), (1, 26)}
GO_FLOOR_BASE_LAYER = {"pkg", "concurrency", "logx", "core", "kitx", "metrics"}
GO_FLOOR_EXEMPT = {"tracing"}

DESC = {
    "pkg": "微工具集 23 合 1（errors/bloomx/contextx/container/stringx/slicex/enumx/convert/encoding/jsonrepair/httpjson/mathx/pathx/pagination/ptrx/timex/cache/bufiox/strmap/singleflighttest/urlguard/cli/pack；v0.10.0 起 uidgen/validator 拆至 kitx）；errors（v0.12.0 起）New/As/Is/Unwrap 与 stdlib 同型透传——下游哨兵/As 判定单 import 即可，类型同一性红线可完整执行；cache（v0.4.0 起）含缓存接口 CacheAside + BatchGetter 批量能力（MGet）+ LocalCache/SingleFlightCache + cachetest 契约（Run 真实时间 + RunWithClock 假时钟确定性 TTL 边界）+ Checker；LocalCache 时钟可注入（LocalConfig.Now——TTL/NullTTL 全路径确定性测试，v0.13.0 起）；v0.16.0 起 LocalCache 可选形状——NoBackgroundSweep 关闭后台清扫 goroutine（嵌入式/短命进程，公开 Sweep 手动清扫口）、CloneOnRead 读命中返回副本（调用方可改写不污染缓存）、EvictPolicy 可选 LRU（默认 FIFO 不变）、GetEx 区分未命中与值为 nil（新增能力以可选接口 Sweeper/HitGetter 外置，不破坏其它后端）；v0.23.0 起补 Peek 非删除式读（可选接口 PeekGetter 外置——读到过期条目只算不命中、不删除它，stale-if-error 兜底需要那份过期真值留在表内；SingleFlightCache 包装 LocalCache 时曾因前置检查走 Get 惰性删除而静默废掉 StaleIfError，#72）；container（v0.9.0 起，收编原 collectionx）泛型容器 Set/Ring/Queue/SafeMap + Map（sync.Map 背书）/Slice（并发安全）；mathx/jsonrepair（v0.11.0）Unstable 单边向上抖动 Upward / 宽松解析 UnmarshalLenient / 抽对象与抽数组对称原语 ExtractObject·ExtractArray（v0.18.0 起——多段数组与散文含 '{' 的数组文本不再整段作废，取首个口径与对象面一致）；timex/stringx（v0.7.0 起）时间段边界 BeginningOf/EndOf 七对（分/时/日/周/月/季/年，周起始显式参数）+ trie 多模式替换器 Replacer（最长匹配）/ NewMask 敏感词遮罩；stringx Masker（v0.16.0 起）正则脱敏——按形态分类打码（形态标签进掩码供审计读）+ 保留首尾各 ≤4 字符定位线索且不可复原（保留位封顶不随长度变化），规则表由调用方完全提供；singleflighttest（v0.8.0 起）singleflight 共享契约测试套件（internal 与 concurrency 双侧引用）；urlguard（v0.15.0 起）出站 SSRF 防护原语——CheckURL 字面量校验（私网/loopback/链路本地含云元数据 169.254.169.254/CGNAT/ULA/组播保留、.internal·.local·localhost 域名、scheme 白名单、IPv4-mapped IPv6 归一）+ DialFunc 拨号层预解析校验与实际 RemoteAddr 端点核对闭合 TOCTOU（Happy-Eyeballs 覆盖），构造态豁免 WithAllowPrivate/WithAllowCIDRs/WithAllowDomains，与 httpx WithDialFunc 一行组合（httpx#11）；cli（v0.16.0 起）框架无关可单测的子命令分发原语——App/Command + 退出码 0/1/2 写死在包里、usage 按 Summary 自动生成、不读 os.Args 不调 os.Exit（输出流全由调用方给）；pack（v0.16.0 起）迁移包容器原语——zip + manifest（格式版本/条数/每文件 SHA-256）+ JSONL 流式读写 + Inspect 体检一次列全问题并区分包损坏与版本不认识",
    "kitx": "标识与校验——uidgen（Snowflake 全局单例+Extract 元数据、手写 RFC 4122 v4 UUID 零第三方依赖、RandomHex/RandomString 拒绝采样随机串）+ validator（go-playground/validator v10 接口化门面：struct tag 校验/自定义规则注册/ValidationError Unwrap 为 BizError 400 对接 middleware.Fail）+ hash（v0.2.0 起非密码学哈希助手 Md5Hex/Md5Bytes——缓存键/幂等键/短唯一 code，格式稳定承诺，pkg 2026-09-15 治理移除弱哈希后 kitx 侧收编 pkg#10），自 pkg v0.10.0 拆出",
    "concurrency": "并发与流控原语（async/gopool/group/singleflight/progress/syncx/ratelimit/retry）+ window 滑动窗口 / breaker 自适应熔断（规范哨兵 ErrCircuitOpen）/ shedding CPU 自适应降载 / batch 攒批执行器（满条数/满字节/窗口去重）/ timewheel 单级时间轮；v0.5.0 起 retry 指数退避+抖动（MaxDelay 封顶/溢出饱和）、loop 周期原语（忙时丢 tick/单轮 panic 收编/FirstDelay 可打断）、async labeled RunSafe（panic 按任务标签上报）、syncx.FanInErr 逐槽错误（panic 槽显式标记）、KeyedLimiter 增强（容量护栏/RetryAfter/速率热更）并接入 pkg/singleflighttest 契约测试；v0.8.0 起 singleflight DoShared 折叠可观测（Result.Waiters）、async panic 统一收编 typed *PanicError（errors.As 可辨、携带栈）+ 实例级 Runner panic 路由、retry DelayHint（服务端 Retry-After 建议优先）+ JitterRatio 比例抖动 + retry/httpx Retryable 可重试性判定；v0.9.0 起 breaker 显式三态状态机 closed/open/half-open——冷却（WithProbeTimeout 缺省 1m）期满首个放行即半开探测、探测成功关闸清窗/失败重开、失联超时自动回收 + AbandonGen 显式放弃，GenBreaker 代绑定（AllowGen/SuccessGen/FailureGen/AbandonGen：代仅在探测放行自增、陈旧代回报整体作废、开放期失败不后移冷却）；v0.10.0 起 WithCooldown 独立业务冷却期（缺省回落 probeTimeout）+ WithConsecutiveFailures 连续失败计数触发（与速率型并联，补低 QPS 信号，对齐 Envoy consecutive_5xx）；v0.11.0 起 ratelimit Lockout 防爆破锁（fail2ban 形态：滑窗失败计数→阈值锁定拒绝 LockDuration→成功 Reset 清零/到期自解，CheckLocked 返回剩余时长）；v0.12.0 起 retry/httpx RetryableWithHeaders（403 + Retry-After/X-RateLimit-Remaining 头感知可重试判定，HeaderSource 零依赖抽象）、syncx FanInLimit 受限并发扇出（固定 worker 防下游过载）+ FanInLimitCtx（v0.14.0，ctx 感知：ctx 已取消未启动项 fn 一次不调、剩余槽快速排空 Canceled、在飞项照常落槽）、ratelimit WithTokenClock 时钟注入；v0.13.0 起 breaker 熔断期收敛到探测级（forcePass 强制探活 + 冷却期满半开探测，其余拒绝——SRE 公式退役为 closed→open 触发器，WithConsecutiveFailures 触发后流量真停）+ State()/State 枚举可观测出口；v0.16.0 起 ratelimit NewKeyedLimiterByFunc[K]——factory func(K) Limiter 按 key 定配额（一个管理器按接口维度分档位，NewKeyedLimiterOf 零破坏随迁；未登记 key 拿 factory 兜底档的坑已在 doc 点名，#31）；v0.17.0 起 KeyedLimiter.Create 护栏内原子登记（表满先回收空闲与 Allow 同路、仍满拒绝计 OverCapHits、已存在不覆盖——热更走 Replace；解决 ByFunc factory 库锁内调用致消费方锁序倒置，#33）；v0.18.0 起 retry.DoValue 泛型伴生——**带返回值的重试**（`DoValue[T](ctx, cfg, fn func(attempt int) (T, error))`；`Do` 改为**委托给** `DoValue`——退避序列/抖动/MaxDelay 封顶/DelayHint 优先/Retryable 门控/ctx 取消**只有一份实现**，杜绝「同一个仓里两份实现」；失败返回**零值**与最后一次错误）",
    "logx": "统一日志门面（多后端可替换 + caller 归属；Sink 已迁嵌套模块 logx/sink，根包纯 stdlib）；WithFilter 脱敏/丢弃包装（FilterKey 命中键替换 ***/FilterFunc 丢整条，子 logger 保持规则）、Slogger/SlogHandler 正向桥（三方 slog 直接消费 logx 生态）、TraceHandler 任意 slog.Handler 自动注入 trace_id、NewAuditLogger 审计留痕（级别锁 Info，不受 LOG_LEVEL 降级）、LevelEnabler 热路径提前判级、ParseLogLevel 返回 (Level, error) + ParseLogLevelOr 配错退默认（v0.7.0）；优雅关停正身在 core/lifecycle.Manager（logx/shutdown 仅余 Deprecated 别名）；桥接子包 hlog/hertzlog/kitexlog 为嵌套模块按需引入",
    "core": "应用基建件——governance 治理 schema、config 配置加载（LoadBootstrap/LoadDotEnv/Get* 系、default/env/range/options tag 校验、OverlaySource 多源叠加热更、ApplyEnvOverlay env 优先 12-factor 形态）、transport 协议无关传输上下文（Transporter/Header/Server·Client ctx 存取，仅标准库）、chain 中间件链（Handler/Middleware/Chain/Recovery/Timeout，与 nns-endpoint.Endpoint 同形）、lifecycle 契约 + Manager 优雅关停（自 logx/shutdown 迁入，日志 *slog.Logger——core 不再依赖 logx）、network IP 工具、version；v0.9.0 起 config FieldSet 显式提供集合（yaml 显式 port:0 不再被 default/env 覆盖——LoadBootstrap 默认接线，免全量指针化）+ require tag（必须 yaml/env 提供，缺失列全并附 env 提示）+ v0.9.1 修 yaml:,inline 字段在 FieldSet 下漏标记（inline 叶子显式零值曾被 default 覆盖）；v0.9.2 env 卫生改纯静态（仅格式/重名，移除依赖进程环境的拼写冒烟检查——误报 PATH/HOME、漏无人设置的真拼错，core#8），命名一致性走 LintEnvNaming 诊断 / WithEnvNamingLint 可选 fail-closed / ignorederived=\"true\" 豁免；v0.10.0 起 config **严格模式**——`LoadBootstrap[T](envFlag string, opts ...LoadOption)` 传 `config.WithStrict()` 开启：未知键**拒载**（不再静默忽略），错误带**嵌套路径** + **该层合法键清单** + **近似拼写建议**（`UnknownKeyError`）；默认行为不变，严格模式显式开启；v0.11.0 起 config 加载入口按来源拆开——`LoadFile`（**任意路径**：CLI -config 路径、容器内固定挂载位）+ `Unmarshal`（**字节源**：API 下发 / 模板渲染 / 测试夹具），与 `LoadBootstrap`（etc/ 约定）共享**同一条解码线**，因此严格模式诊断面（嵌套路径 / 合法键清单 / 拼写建议）**与来源无关**；`LoadBootstrap` 行为与错误文案逐字不变",
    "metrics": "Prometheus 门面 + 弹性指标族",
    "tracing": "OpenTelemetry 追踪 + trace_id 真相源（v0.3.0 起 OTLP 双协议导出：gRPC 缺省 / HTTP，Config.Protocol + Headers，切换协议不丢传播器/指标/旧 provider 关闭）",
    "registry": "注册发现核心（provider 无关 SPI：Registrar/Discoverer/Subscriber/ConfigStore + 实例模型 + netutil）+ sdbridge + registrymocks——**根仓零第三方依赖**；Nacos 实现与 Nacos 配置源已拆成嵌套模块（v0.5.0 起：registry/nacosimpl、registry/nacos——nacos-sdk-go 重依赖隔离在此，实测单独引入即拖 63 个第三方模块，不隔离时只要 SPI 接口的消费方也会被连带拖入）",
    "timertask": "分片定时任务（FNV-1a、心跳续约）；v0.1.5 起可选可靠投递 DeliveryAtLeastOnce（租约 + Ack + 可见性超时；过期租约回扫重投/死信、停机 best-effort 收租，v0.1.7 接入生命周期真正生效）；v0.2.0 起 **Redis 后端迁至嵌套模块 timertask/redis**（根包不再引 go-redis/logx/concurrency，require 仅 pkg——import timertask 与 import timertask/local 的模块依赖面均无 redis，纯 stdlib 嵌入式场景可用）+ **新增 local 子包 cron 调度器 Scheduler**（表达式复用 pkg/timex/cron 不另写一份；并发度/CatchUp 补跑/退出语义明确，FakeClock 假时钟驱动、触发点序列可确定性断言）",
    "data": "数据访问门面——复刻 ekit data/ 能力面：cache/rediscache/crud/distlock/gormx/redisx/timertask/cachetest 八子包 re-export（类型同一，实现住 pkg 与各子仓）+ cached 缓存读写编排（写后失效/事务提交后失效/查询缓存版本号失效/GetByIDs 批量点读 MGet 快路径/bloom 防穿透）+ twolevel 两级缓存与跨实例失效广播（v0.7.0 起本仓唯二非 re-export 实现）；v0.15.0 起壳跟随源仓演进——cache 壳补 LocalCache 可选形状（EvictPolicy/Sweeper/HitGetter）、cachetest 壳补 5 个契约套件入口（套件 8 → 13 场景）、v0.18.0 起 cache 壳补 PeekGetter 别名 + cachetest 壳补 2 个 stale-if-error 契约入口（套件 13 → 15 场景）、timertask 壳的 Redis 形态随源仓 v0.2.0 迁至嵌套模块 timertask/redis（壳不再转发 RedisBackend/ShardManager，Redis 形态直接依赖该嵌套模块）；all/ 聚合模块已废弃 retract",
    "gormx": "SQL/GORM 全家——连接池增强、框架无关 CRUD Service、仓储（原 oekit/gorm，v0.3.0 更名）",
    "redisx": "Redis 全家——客户端封装（根包）、分布式缓存 cache/、分布式锁 distlock（Locker 同形可互换；Renew 手动续期（false=锁丢失，不进 Locker 接口保 etcdx 互换）+ WithAutoRenew 覆盖 TryLock 自动续期（v0.8.0 feat!：此前组合不续期）；Semaphore ZSET 槽位信号量（v0.8.0：Acquire 同 identifier 幂等刷新 + Lua 内原子清过期槽崩溃自愈、Renew 仅未过期槽可续、Cleanup 返条数、无 Redis 降级 fail-fast））、分布式限流 ratelimit（Lua 三态 + 故障本地兜底）；v0.9.0 起 distlock Lockout 分布式防爆破锁（Redis 侧 fail2ban：滑窗失败计数→阈值锁定 Penalty→Reset 清零，与 concurrency/ratelimit.Lockout 进程内孪生——多实例共享计数是存在理由；惩罚只按首次触发计算、nil client fail-fast 不降级，#6）",
    "etcdx": "etcd 全家——客户端封装（根包：生产默认值/Health 探活/Close(ctx) 幂等）、registry 注册发现实现（SPI 对接 registry/nacos 链路：Lease KeepAlive 注册 + 丢约指数退避自动重注册、prefix Watch 订阅推送、ConfigStore KV 配置监听）、distlock 分布式锁（Session+concurrency.Mutex 公平排队，接口与 redisx/distlock.Locker 同形可互换，可选无 etcd 降级放行）",
    "httpx": "HTTP 客户端 + 负载均衡——服务发现/熔断（规范哨兵 ErrCircuitOpen）/重试/指标/OTel 透传；loadbalance Selector 形态（RoundRobin/Random/Weighted/一致性哈希权重感知虚拟副本/p2c 自适应——EWMA 经 DoneFunc 回写建立、Err 计 1s 惩罚；WithFilter 按元数据过滤；v0.4.0 起 Balancer 全家族删除无兼容路径）；codec 注册制编解码器（json 内置 + form 表单，DoCodec 请求按 codec 序列化、响应按 Content-Type 反查回退请求 codec）；v0.6.0 起 net/http 桥接层 NewRoundTripper/StdClient——存量标准库外呼一行 Transport 接入熔断/重试/指标/链路（重试缺省关显式开、非 2xx 非错误、hop-by-hop 隔离，#8）；v0.8.0 起 SafeDialFunc/WithSafeDial 出站 SSRF 防护胶水（判定单源 pkg/urlguard v0.15.0：解析 IP 全量预检 + 实际 RemoteAddr 端点核对闭合重解析 TOCTOU、Happy-Eyeballs 覆盖，构造态豁免；v0.7.0 内置的独立 httpx/urlguard 实现收敛删除，#11）",
    "middleware": "Hertz 标准中间件栈（模块根 mw，双层目录已收平）+ contrib 桥接 + hmw + SheddingMiddleware 自适应降载 + chainmw 子包（transport 感知框架无关中间件，基于 core/chain+transport：Recovery 带 panic 隔离上报钩子 WithRecoveryReporter、AccessLog skip+redact+自定义字段、MetadataPropagate——kitex 经 kitex/chainmw.ToKitex 复用同一实现，根包 Recovery/AccessLog 已薄壳化复用 chainmw 内核、下游 handler 可经 transport.FromServerContext 读协议无关传输信息）；v0.6.0 起 accesslog 凭证路径脱敏（WithAccessLogRedactPathPrefixes）、ratelimit 默认 KeyFunc 去端口（连接桶→地址桶）；v0.11.0 起 WithAccessLogSink 落库 sink（AccessRecord{Method,Path,Status,Latency,ClientIP,UserAgent,TraceID} 与日志同源、c 同传可读业务键、error/panic 只记 Warn 不影响主链路、SkipPaths 命中不触发）+ chainmw.WithSink 内核同构（kitex 随 ToKitex 获得）+ StackConfig.DisableResponseWrap 响应自管一键形态（TraceID→Recovery→AccessLog）与 AccessLogOptions 透传 + doc/README「适用边界」段（响应契约统一 vs handler 自管的推荐组合，#18）；v0.12.0 起 AccessLog panic 恢复点产出记录（AccessRecord.Panic 标记 + Err 摘要，Hertz status 按 Recovery 缺省 500）后按原值重抛——panic 请求不再零访问记录，sink/kitex 桥接同步留痕，#20；v0.13.0 起 accesslog 内建日志可关闭/改道（WithAccessLogSilent 只采不写、WithAccessLogLogger 注入宿主 logger 避免双写；静默下 skip/redact/sink/panic 记录照常），内核 chainmw.WithSilent 同构（kitex 随 ToKitex 获得），#38",
    "k8s": "Kubernetes 客户端封装——RESTConfig/Clientset/DynamicClient 构建（显式 kubeconfig → in-cluster 自动 → KUBECONFIG/~/.kube/config 逐级回退，QPS/Burst 限流默认 50/100）+ leaderelection 子包 Lease 选主简化封装（OnStartedLeading/OnStoppedLeading/OnNewLeader 回调，主备单活，仅要求 Lease CRUD 权限）",
    "network": "轻量网络客户端——WebSocket/NATS/MQ 抽象、连接与流工具",
    "nns": "NNS 服务治理客户端——provider 无关发现/配置/实例缓存",
    "nns-endpoint": "NNS 端点工厂——per-service 熔断、换实例重试、gRPC 池",
    "bootstrap": "应用生命周期引导——New(WithServiceName/WithShutdownTimeout/WithBeforeServe) + Run(fn) + healthz；App.Ready/ReadyChan/IsReady 就绪门（幂等并发安全）——嵌套 bootstrap/nacos 自 v0.3.0 起 NNS 注册延迟到 Ready 之后（server 就绪 → userFn 触发 Ready → 才进注册中心）、关停序 nns 反注册先于 server 关闭；就绪门漏接时可观测——nacos 看门狗超时打 ERROR 点明「服务不会注册到注册中心」（WithReadyTimeout 可调）+ NNSComponent.RegistrationState() 暴露注册阶段（pending/registered/failed/skipped/shutdown）；Metrics/Tracing 在嵌套模块 bootstrap/obs",
    "hertz": "Hertz 运行时集成（模块根扁平化）——日志桥接、生命周期、SSE/health/metrics、适配器、OTel、crudh/validatorh（基于 kitx/validator）/versionh（原 handler 微库收编）；RunServer 变参就绪门 WithOnReady（全部 HostPort TCP 探活可建连后回调，探活超时按启动失败优雅关机；缺省超时 5s 经 WithReadyProbeTimeout 调整——探活目标是 server 自动取到的全部 HostPort，**无探活地址覆盖选项**（kitex 侧才有 WithReadyAddr）），配合 bootstrap Ready 实现 NNS 延迟注册；transportx（hertz→core/transport 同构适配器）+ chainmw 桥接",
    "kitex": "Kitex 生态——日志桥接、治理热更、生命周期、BizError 桥接、kitexotel、istio（xDS 动态熔断，原 istio 微库收编）；RunServer 变参就绪门（WithOnReady/WithReadyAddr/WithReadyProbeTimeout）+ chainmw.ToKitex（core/chain.Middleware → kitex 中间件，与 hertz 共享 middleware/chainmw 实现）+ transportx",
    "workflow": "组合式流程编排——串行/并行/异步/条件分支 + Hook AOP",
    "kitex-examples": "Kitex echo 示例——client/server + IDL + 治理配置",
}

DEFAULT_BASE = os.environ.get("MATRIX_BASE", "/Users/zhangyi/my_project/ekit-split")


def repo_dir(repo: str, base: str | None = None) -> Path:
    base = Path(base or os.environ.get("MATRIX_BASE", DEFAULT_BASE))
    for cand in (base / repo, base / f"ekit-{repo}", base / "repos" / repo):
        if (cand / "go.mod").exists() or (cand / "README.md").exists():
            return cand
    raise FileNotFoundError(f"repo {repo} 不存在于 {base} 的任何已知布局中")


def go_mod(repo: str, base: str | None = None) -> str:
    return (repo_dir(repo, base) / "go.mod").read_text()


def module_line(repo: str, base: str | None = None) -> str:
    return go_mod(repo, base).splitlines()[0].strip()


# go.mod 的 oekit 依赖行两种形态：require( 块内 tab 前缀 + 单行 require。
# 此前只匹配 tab 形态——logx/bootstrap 的单行 require core 被解析为无依赖
# （矩阵表依赖列错、地板对齐漏仓），logx#22 揭示。
OEKIT_REQ_RE = re.compile(
    r"^(?:\t|require )(github\.com/oekit/([a-z0-9-]+)) (v\d+\.\d+\.\d+)$", re.M)


def sibling_deps(repo: str, base: str | None = None) -> list[str]:
    """go.mod 里 require 的其他矩阵模块（排序去重，块内与单行 require 均识别）。"""
    out = set()
    for m in OEKIT_REQ_RE.finditer(go_mod(repo, base)):
        if m.group(2) != repo:
            out.add(m.group(2))
    return sorted(out)


class RemoteReadError(RuntimeError):
    """远端事实**读不到**（不是「读到空」）。

    matrix-tools#79：「读到空」与「读不到」必须分开表达。
    读到空 = 事实为空（可以据此下结论）；读不到 = 不知道（必须报错）。
    两者混为一谈 ⇒ 门禁静默降级 ⇒ 假绿（#26 的形态：23/24 仓被跳过，报告仍显示通过）。
    """


# 远端 tag 读取台账（#79）：repo -> "ok" | "failed"。
#
# 供 audit 报告「远端事实真的读到了几个」——让「零降级」自带**覆盖证据**，
# 而不是只印一句「✓ 矩阵审计通过」（读不到时这句话长得一模一样）。
REMOTE_READ_LOG: dict[str, str] = {}


def _ls_remote_tags(url: str, timeout: int = 60) -> tuple[list[str], bool]:
    """(tags, ok)。

    ok=False ⇒ **读失败**（网络/认证/超时/非零退出）；
    ok=True 且 tags=[] ⇒ 真的一个 tag 都没有（这是合法事实，不是故障）。
    这两者的区分是 #79 的核心——混在一起就会把「不知道」当成「没有」。
    """
    try:
        r = subprocess.run(["git", "ls-remote", "--tags", url],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return [], False
    if r.returncode != 0:
        return [], False
    tags = []
    for line in r.stdout.splitlines():
        ref = line.split("\t")[-1].strip()
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags.append(ref[len("refs/tags/"):])
    return tags, True


def remote_tags(repo: str, base: str | None = None) -> list[str]:
    """读某仓**远端**全部 tag（含嵌套前缀，形如 hlog/v0.1.4）。

    口径（matrix-tools#79）：

    1. **读失败不静默降级为本地 tag。** 降级会把「远端已发新版、本地 clone 没 fetch」
       这个 gen_versions docstring 明说要防的形态原样放回来，且无任何告警
       ——「generatedAt 很新但内容陈旧」比明显陈旧更有害。
    2. **canonical HTTPS 优先**，clone 的 origin URL 兜底：
       - HTTPS 走 gh 的 token，不依赖本机 SSH key/agent ⇒ SSH 抽风不影响结论；
       - 实测 SSH ≈6.5s/仓、HTTPS ≈1.2s/仓 ⇒ 优先 HTTPS 顺带把审计跑快约 5 倍；
       - origin 兜底覆盖「HTTPS 被代理挡了、只有 SSH 通」的场景。
    3. 两条都失败 ⇒ 抛 :class:`RemoteReadError`：宁可脚本失败，也不把「读不到」
       伪装成「没有」。
    """
    d = repo_dir(repo, base)
    origin = None
    try:
        origin = subprocess.check_output(
            ["git", "-C", str(d), "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        origin = None
    urls: list[str] = []
    canonical = f"https://github.com/{ORG}/{repo}.git"
    urls.append(canonical)
    if origin and origin not in urls:
        urls.append(origin)
    for url in urls:
        tags, ok = _ls_remote_tags(url)
        if ok:
            REMOTE_READ_LOG[repo] = "ok"
            return tags
    REMOTE_READ_LOG[repo] = "failed"
    raise RemoteReadError(
        f"{repo}: 远端 tag 读不到（已试 {'、'.join(urls)}）——"
        f"读不到 ≠ 没有，不能用本地 tag 顶替（matrix-tools#79）")


def latest_tag(repo: str, base: str | None = None, remote: bool = False) -> str | None:
    d = repo_dir(repo, base)
    if remote:
        # 读不到 ⇒ 抛 RemoteReadError（**不再**返回 None）。
        # 返回 None 只表达「该仓真的一个 tag 都没有」；此前 None 同时表达
        # 「读失败」，而调用方一律 `if lt:` 跳过 ⇒ 整条检查静默消失（#79）。
        tags = remote_tags(repo, base)

        def key(v):
            try:
                return tuple(int(x) for x in v[1:].split("."))
            except ValueError:
                return (0, 0, 0)
        tags = sorted(set(tags), key=key, reverse=True)
    else:
        r = subprocess.run(["git", "-C", str(d), "tag", "-l", "--sort=-v:refname"],
                           capture_output=True, text=True)
        tags = r.stdout.split()
    semver = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return semver[0] if semver else None


def bump_patch(v: str) -> str:
    a, b, c = v[1:].split(".")
    return f"v{a}.{b}.{int(c) + 1}"


def remote_tag_exists(repo: str, tag: str, base: str | None = None) -> bool:
    """远端是否**真的**有这个 tag。

    「读到空」≠「读不到」：远端读失败 ⇒ 抛 :class:`RemoteReadError`，
    **不得**当成「这个 tag 不存在」（#85）。

    本函数是「远端 tag 是否存在」这条事实的**唯一**读法——发版查重
    （release_train）与关单守卫（close_issue）都走它。同一个事实有多条
    读取路径时，语义必须一致，否则一条路径 fail-open、另一条误报。
    """
    return tag in set(remote_tags(repo, base))


def tag_exists(repo: str, tag: str, base: str | None = None) -> bool:
    """该仓**远端**是否有这个 tag（发布事实核对用：版本号必须真的发出去过）。

    查远端而不是本地：本地 clone 的 tag ref 常陈旧（浅克隆 / 长期不 fetch），
    用本地判据会把「已发布」误判成「未发布」（反之亦然）。

    读不到 ⇒ 抛 RemoteReadError（#85）：此前只看 ls-remote 的 stdout 非空，
    读失败（SSH 掉线 / 网络抖动）会返回 False ⇒ 把「不知道」说成「没有」。
    """
    return remote_tag_exists(repo, tag, base)


def declared_version(repo: str, base: str | None = None) -> str | None:
    """仓内**声明**的当前版本（oekit 门面 matrix.go 的 const Version）。

    与 tag 的区别：声明值先于 tag 落到 main。两者不一致 = 版本号断点
    （main 说 vX.Y.Z、远端最高 tag 是 vX.Y.(Z-1)）⇒ 该版本号对消费方永久缺失
    （`go get <mod>@vX.Y.Z` 拉不到），且没有任何门禁会报它。
    """
    p = repo_dir(repo, base) / "matrix.go"
    if not p.exists():
        return None
    m = re.search(r'const Version = "([^"]+)"', p.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def sh(args, cwd, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr[-400:]}")
    return r


# ---------- 工作树状态守卫（写入型脚本共用） ----------

def git_branch(d) -> str:
    r = subprocess.run(["git", "-C", str(d), "branch", "--show-current"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def is_dirty(d) -> bool:
    r = subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def ensure_clean_all(dirs, what: str) -> None:
    """写入型生成器/发版守卫：任一仓工作树不干净即整体拒绝（先查后做，不产生半截产物）。
    生成器产物必须落在已提交的基线上——checkout -f 冲掉未提交产物、tag 打在旧内容上
    的事故类别的硬约束（2026-10-19 一日三踩后收口）。"""
    dirty = []
    for d in dirs:
        d = Path(d)
        if d.exists() and is_dirty(d):
            dirty.append(d.name)
    if dirty:
        raise SystemExit(f"{what}: 工作树不干净，拒绝执行：{', '.join(dirty)}"
                         f"（先提交或 stash——生成器产物必须在干净基线上生成）")
