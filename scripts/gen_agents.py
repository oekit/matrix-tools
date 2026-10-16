#!/usr/bin/env python3
"""为 21 个活跃仓生成 oekit 适配版 AGENTS.md（共享规范 + 本仓信息）"""
import os
import pathlib
import re
import subprocess

import sys
sys.path.insert(0, str(pathlib.Path(__file__).rsplit("/", 2)[0]))
from matrix import MODULES
BASE = pathlib.Path(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
def repo_path(r): return BASE / "pkg" if r == "pkg" else BASE / f"ekit-{r}"

DESC = {  # 各仓一句话定位（matrix.py 亦有副本）
    "pkg": "微工具集 21 合 1（bloomx/collectionx/contextx/errors/validator/stringx/slicex/enumx/convert/encoding/jsonrepair/httpjson/mathx/pathx/pagination/ptrx/timex/uidgen/cache/bufiox/strmap）",
    "concurrency": "并发与流控原语（async GoSafe 系列 / gopool 协程池 / group / singleflight / progress / syncx / ratelimit / retry）",
    "logx": "统一日志门面（接口分层 / 多后端 / Sink / caller 归属）+ shutdown 优雅关停",
    "core": "应用基建件：governance 治理 schema、lifecycle 契约、network IP 工具、version",
    "istio": "Istio Proxyless——Kitex xDS 动态熔断（InitXDS / NewXDSClientOption）",
    "metrics": "Prometheus 门面（Registry / Init）+ metrics/resilience 弹性指标族",
    "tracing": "OpenTelemetry Init + trace_id 真相源（traceid 包）+ httpotel / redisotel 插桩",
    "registry": "注册发现 SPI（Registrar/Discoverer/Subscriber/ConfigStore）+ Nacos 实现 + nacos 门面 + config 配置加载 + sdbridge + registrymocks",
    "timertask": "分片定时任务（LocalBackend / RedisBackend，FNV-1a 分片）",
    "data": "缓存检查器（NewChecker：TTL/failTTL/inflight 合并）+ LocalCache + SingleFlightCache",
    "gorm": "gormx 连接池增强 + crud 框架无关 CRUD Service（TxRepository 事务防丢更新）+ gormrepo 仓储",
    "redis": "redisx 客户端封装 + distlock 分布式锁 + data/cache 的 Redis 后端",
    "httpx": "Hertz Client 高层封装——服务发现/熔断/指数退避重试/弹性指标/OTel 透传 + loadbalance",
    "middleware": "自研标准中间件栈（包名 mw：Recovery/Response/AccessLog/RateLimit/Auth/I18n/Fail）+ contrib 桥接 + hmw",
    "nns": "NNS 服务治理门面——nns.New 一行创建（mock 模式 / ServiceClient 逃生口）",
    "nns-endpoint": "端点工厂——HTTPEndpointFactory/GRPCEndpointFactory：per-service 熔断、换实例重试、gRPC 池",
    "bootstrap": "bootstrap.Run 应用骨架（App ctx + AddResource + GoBackground）+ healthz 健康检查",
    "handler": "crudh 通用 CRUD HTTP Handler（泛型）+ validatorh 参数绑定 + versionh",
    "hertz": "Hertz 运行时集成——hlog 桥接、RunServer 生命周期、Shutdown Hook + health/metrics/sse/render/adaptor + registry/hertz + nns/hertz + hertzotel/hertztracer + governance/hertz",
    "kitex": "Kitex 生态——klog 桥接、GovernanceConfig 治理热更、RunServer、WithBizErrorBridge、kitexmeta 透传、registry/kitex + nns/kitex + kitexprom + kitexotel",
    "workflow": "组合式流程编排——PipeFlow/ParallelFlow/AsyncFlow/SwitchFlow + Hook AOP",
    "kitex-examples": "Kitex echo 示例——client/server + IDL + 治理配置样例",
}

BODY = """
## 硬规则速览（全矩阵适用）

### 分层与依赖
- 依赖严格自底向上，禁止反向依赖；本仓的矩阵内依赖见 README「矩阵总览」表
- 微工具集统一在 `github.com/oekit/pkg`；本仓新增基础工具前先确认 pkg 里没有

### 错误处理
- 一律使用 `github.com/oekit/pkg/errors`：`errors.Wrap(err, "ctx", kv...)`（nil-safe）、
  `errors.Cause`；业务错误用 BizError（BizCode 与 HTTPCode 双轨，命名工厂 NewNotFoundError 等）
- **类型同一性红线**：同一进程内只允许一套 errors 包。混用旧路径会把 BizError 的 As/Is
  判定打断（Not Found 变 500）
- handler 层统一 `mw.Fail(ctx, c, err)` 出口；所有 error 禁止 `_` 忽略

### 日志
- 门面 `github.com/oekit/logx`：业务服务 `logx.WithService("svc")`；
  **SDK/基础包必须 `logx.Delegating("组件名")` 懒解析**（禁止 Default()/WithService 声明包级 logger）
- kv 结构化风格为默认；`Ctx*` 方法自动携带 trace_id；禁止 printf 风格 `logx.Infof` 作为主要手段
- 后端经 `logx.SetDefault` 替换；`LOG_LEVEL` 环境变量统一生效

### 并发安全
- 禁止裸 `go func`：用 `github.com/oekit/concurrency/async` 的 GoSafe 系列（panic 被 recover）
- 共享状态 mutex / atomic 保护；包级全局可变状态用 `atomic.Pointer[T]`

### 生命周期
- 组件 Close 统一 `Close(ctx) error` 签名 + sync.Once 幂等
- 应用骨架用 `github.com/oekit/bootstrap/bootstrap.Run`；**userFn 返回即服务结束，
  长驻服务必须 `<-app.Context().Done()` 阻塞**

### Option 与命名
- 函数选项模式 `XxxOption`（禁止裸 `Option`，多包 co-import 冲突）
- 导出符号 PascalCase + godoc 注释；包文档放 doc.go

### 版本与发版
- SemVer；0.x 阶段 minor 允许 breaking；发版 = 打 annotated tag 推远端（CI 自动跑 lint+test）
- module 路径 = `github.com/oekit/<name>`；**改 module 路径必须同步**：
  全部 import、go.mod、README 安装行、logx caller 启发式（若涉及 logx）

### 文档
- 本仓 README 的「矩阵总览」表由脚本生成，结构变化后需重新生成
- 历史完整规范见 ekit 单体仓（git.enjoye.top/enjoydream/ekit，冻结维护）的 AGENTS.md 与 docs/

## 本仓范围
"""

def pkg_list(repo):
    d = BASE / f"ekit-{repo}"
    pkgs = []
    for p in sorted(d.rglob("*.go")):
        if ".git" in p.parts or "_test.go" in p.name or "/testdata/" in str(p):
            continue
        rel = p.parent.relative_to(d)
        if str(rel) not in pkgs:
            pkgs.append(str(rel))
    return "、".join(f"`{p}/`" for p in sorted(set(pkgs)))

count = 0
for repo in MODULES:
    d = repo_path(repo)
    lt = subprocess.run(["git", "-C", str(d), "tag", "-l", "--sort=-v:refname"],
                        capture_output=True, text=True).stdout.split()[0]
    content = f"""# AGENTS.md — oekit/{repo}

> {DESC[repo]}
> 当前版本：`{lt}` · module：`github.com/oekit/{repo}` · org：[oekit = Open Easy Kit](https://oekit.github.io)

## 本仓包含
{pkg_list(repo)}
{BODY}
"""
    (d / "AGENTS.md").write_text(content)
    count += 1
    print(f"✓ oekit/{repo}/AGENTS.md", flush=True)
print(f"=== {count} 份 AGENTS.md ===")
