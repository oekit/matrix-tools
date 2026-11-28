#!/usr/bin/env python3
"""全矩阵硬规则速览——**唯一手写面**（2026-11-12 自 gen_agents.py BODY 提取为共享常量）。

为什么要单独一个模块：BODY 原先只住在 gen_agents.py 里，而 skills/oekit（gen_skill.py
渲染的 AI 入口）需要同一段红线——复制一份就是第二个漂移面（DESC 本地副本
2026-10-16 漂移清理的同型教训）。提取后：

  - gen_agents.py  ：BODY = 硬规则速览 + 本仓范围头（冷启动模板用）
  - gen_skill.py   ：rules.md（skill 参考文件）整段渲染 + SKILL.md 红线精华
  - 提取本身做过**字节级比对**（旧 BODY 与新组合逐字节相等）

内容变更纪律：改这里 = 全矩阵 AGENTS.md 冷启动口径 + skill 红线一起变；
本工具对已生成各仓 AGENTS.md **不做覆写**（gen_agents 仅新仓冷启动可用），
因此对存量仓的影响只在「下次冷启动」——日常修订硬规则后，如需同步存量仓
请走人工/批量编辑，不要指望重跑生成器。
"""

# 逐字提取自 gen_agents.py 旧 BODY（去掉「## 本仓范围」段头与首尾空行）。
# 首行无缩进顶格开始、末行以换行结束——组合方按需自行包前后缀。
HARD_RULES_MD = """## 硬规则速览（全矩阵适用）

### 分层与依赖
- 依赖严格自底向上，禁止反向依赖；本仓的矩阵内依赖见 README「矩阵总览」表
- 微工具集统一在 `github.com/oekit/pkg`；本仓新增基础工具前先确认 pkg 里没有
- `pkg` 是**零第三方依赖仓**（go.mod 零 require，仅标准库，CI 强制）——需要三方能力的基础工具拆独立仓（先例：kitx）

### 错误处理
- 业务错误（需 BizCode/HTTPCode 双轨、可被 As/Is 判定）统一用
  `github.com/oekit/pkg/errors` 的 BizError（命名工厂 NewNotFoundError 等）
- 普通错误包装允许 stdlib：`fmt.Errorf("...: %w", err)` / `errors.New`
- BizError 载体同一进程只此一套：混入其他 BizError 实现会打断 As/Is 判定（Not Found 变 500）
- handler 层统一 `mw.Fail(ctx, c, err)` 出口；所有 error 禁止 `_` 忽略

### 日志
- 门面 `github.com/oekit/logx`：业务服务 `logx.WithService("svc")`；
  **SDK/基础包必须 `logx.Delegating("组件名")` 懒解析**（禁止 Default()/WithService 声明包级 logger）
- kv 结构化风格为默认；`Ctx*` 方法自动携带 trace_id；禁止 printf 风格 `logx.Infof` 作为主要手段
- 错误字段名统一用 `logx.ErrKey` 常量（值 "err"），禁止手写 "err"/"error" 字面量（两矩阵落同一 Loki/ES）
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
- Option 非法值总则：① 构造期非法值 fail-fast（panic 或返回 error，同仓内保持一致）；
  ② nil 参数 = 显式回退默认值，不算非法；③ 运行期不可恢复错误显式上抛，不静默

### 超时默认值
- 探测类 ≤10s、治理类 ≤60s、后台任务 ≤10min；跨区间取值须在包文档声明理由

### 版本与发版
- SemVer；0.x 阶段 minor 允许 breaking
- **main 受分支保护，改动一律走 PR，管理员也不例外**（`enforce_admins=true`，
  直推会被 `GH006 Changes must be made through a pull request` 拒掉）
- 发版流程：建分支 → 提交 → 开 PR → CI（`Lint & Test / Lint` + `Lint & Test / Test`）
  绿 → 合入 main → **合入之后**再打 annotated tag 推远端
- tag 不走 PR（`protect-release-tags` ruleset 管的是不可删/不可改，不是审批）；
  tag 必须落在已合入 main 的 commit 上，否则 `go get` 拉到的是没进过 CI 的代码
- module 路径 = `github.com/oekit/<name>`；**改 module 路径必须同步**：
  全部 import、go.mod、README 安装行、logx caller 启发式（若涉及 logx）

### 文档
- 本仓 README 的「矩阵总览」表由脚本生成，结构变化后需重新生成
- 历史完整规范见 ekit 单体仓（git.enjoye.top/enjoydream/ekit，冻结维护）的 AGENTS.md 与 docs/
"""

# 模块准入三问（2026-11-28 演进评审 P4 / matrix-tools#344）。
# 独立于 HARD_RULES_MD：硬规则管「仓内怎么写」，三问管「要不要有这个仓」——
# 投影面也不同：HARD_RULES_MD 进每个仓的 AGENTS.md/红线，三问进
# ① gen_agents --new-repo 过闸（缺答案即拒）② 冷启动 AGENTS.md 留档段
# ③ gen_skill rules.md（AI 提新模块时看得到判据）。
MODULE_ADMISSION_3Q_MD = """## 模块准入三问（新增/改独立发版单元前必答）

进度的尺子是**场景覆盖数与承诺门禁数**，不是模块数（2026-11-27 演进评审 P4 换轨）。
新增独立仓前三问答不出，就不独立——并 pkg / 走嵌套（matrix.py NESTED_MODULES，
#37 裁决不换位置）/ 不做：

1. **独立生命周期**：有独立的发版节奏与矩阵外消费方吗？只是某仓的重依赖隔离壳
   ⇒ 嵌套模块；通用微工具 ⇒ 先查 `github.com/oekit/pkg` 有没有。
2. **使用场景**：场景选型表（gen_skill.SCENARIOS）写得出至少一条它的场景吗？
   写不出 = 没人知道何时用它 = 不该独立成仓。audit 红级机判：MODULES 每仓必须
   被 ≥1 场景 mods 引用（#344 起）。
3. **稳定 API**：能承诺导出面的稳定性分级吗？清单住本仓 `api-lifecycle.yaml`
   （每生产包定级+理由），门禁住 matrix-tools（api_lifecycle_gate 清单↔包注释↔
   生产包三处一致）——pkg 双面设计是范式，冷启动至少给出首版定级计划。

过闸：`gen_agents.py --new-repo <name>` 要求三个 `--admit-*` 显式答案，缺一即拒；
答案嵌入新仓 AGENTS.md「准入三问」段留档（谁凭什么是独立仓，白纸黑字）。
"""
