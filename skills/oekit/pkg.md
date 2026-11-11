# pkg 子包速查（31 合 1，零第三方依赖）

> 由 gen_skill.py 渲染——修订改 gen_skill.py 的 PKG_QUICK；audit 会拿 pkg 仓真实目录校验死键。API 以 `go doc github.com/oekit/pkg/<sub>` 为准。

| 想做什么 | 子包 | 说明 |
|---|---|---|
| 错误包装/业务错误 | `errors` | nil-safe Wrap/Cause + BizError 双码（服务形态唯一载体） |
| 字符串/脱敏/敏感词 | `stringx` | 工具集 + Replacer 最长匹配替换 + Masker 正则脱敏 |
| 切片/枚举/类型转换 | `slicex` / `enumx` / `convert` | — |
| 时间边界/cron | `timex` / `timex/cron` | BeginningOf/EndOf 七对 + cron 表达式 |
| 数学/路径/分页/指针 | `mathx` / `pathx` / `pagination` / `ptrx` | — |
| JSON 修复/宽松解析 | `jsonrepair` | UnmarshalLenient + ExtractObject/ExtractArray |
| HTTP + JSON（零依赖） | `httpjson` | 带解析的轻量请求 |
| 编码 | `encoding` | — |
| 本地缓存 | `cache` / `cache/cachetest` | CacheAside/LocalCache/SingleFlightCache + 契约套件（RunWithClock 假时钟；Comparable 仅 int/uint/string） |
| 泛型容器 | `container` | Set/Ring/Queue/SafeMap/Slice（并发安全） |
| 布隆过滤器 | `bloomx` | — |
| ctx 取值/作用域 | `contextx` / `ctxscope` | — |
| 缓冲 IO | `bufiox` | — |
| 并发安全 map | `strmap` | — |
| secrets 凭证 | `secretx` | — |
| shell 词法判据 | `shellx` | Analyze 事实面（重定向/动态词），判不了就报错 |
| 出站 SSRF 校验 | `urlguard` | CheckURL + DialFunc 预解析（与 httpx.WithSafeDial 组合） |
| webhook 回调 | `webhook` | — |
| CLI 子命令 | `cli` | 退出码 0/1/2 写死，不读 os.Args 不 os.Exit |
| 迁移包容器 | `pack` | zip + manifest + JSONL 流式 + Inspect 体检 |
| 目录 tar 传输 | `tarx` | Scan/Write/Extract 三件套，拒 Zip Slip |
| 断点续传/分片上传 | `resumable` | 会话状态机 + ChunkStore 接口 |
| 子进程治理 | `procx` | 内存上限 cgroup v2；与 shellx 配套 |
| 零依赖静态配置 | `config` | yaml 子集 + env 覆盖 + Redact |
| singleflight 契约测试 | `singleflighttest` | 共享契约套件 |
