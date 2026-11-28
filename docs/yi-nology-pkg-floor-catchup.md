# yi-nology 侧 pkg 地板追平工程报告

> **面向**：yi-nology 侧 18 个消费 `github.com/oekit/pkg` 的仓的维护者。
> **出处**：oekit/matrix-tools 的跨 org 地板机制——[#333](https://github.com/oekit/matrix-tools/pull/333)
> 把 yi-nology 登记进 `scripts/cross_org_consumers.json` 之后，这一面才第一次可见。
> **数据日期**：2026-11-26。**事实源**：各仓 HEAD 的 `go.mod` + HEAD 源码的 import 实测
> （不是依赖声明，是真读源码），外加逐仓实跑 `go build` 的编译结论。
> **目标版本**：`v0.61.5`（全矩阵最新已发布版；它同时携带了本轮 pkg 侧的清单修订）。

## 一、结论

**18 个仓可以抬地板到 `v0.61.5`——但不是「只改一行 go.mod」就完事。**

上一轮的判断是「区间内有 3 个真 breaking ⇒ 不能一刀切抬」——**这个判断已被证伪**：
它是基于「区间内**存在** breaking」推的，不是基于「**实际影响到谁**」推的。
逐仓实测的结论是：

### 1.1 各仓**自身源码**：编译期 breaking 影响面 0 / 18

| breaking（pkg CHANGELOG 实测） | 受影响仓数 | 判据 |
|---|---|---|
| v0.45.0 / v0.48.0 删除 `errors.Errorf` 兼容层 | **0 / 18** | 别名感知匹配：只认「本文件 import 的是本仓 `pkg/errors`」且调用形如 `<别名>.Errorf(` |
| v0.50.0 摘除 `licensx` 包 | **0 / 18** | 源码无 `github.com/oekit/pkg/licensx` import |
| v0.50.0 摘除 `webhook` IM 载荷 | **0 / 18** | 四个被摘符号 `BuildIMPayload` / `DingtalkMarkdownPayload` / `FeishuTextPayload` / `WecomMarkdownPayload` 逐个点名，零命中 |
| v0.59.0 删除 `toolname` 包 | **0 / 18** | 源码无 `github.com/oekit/pkg/toolname` import |
| v0.43.0 裸 `Option` 改名（`cache.Option`→`CheckerOption` / `pack.Option`→`WriterOption` / `urlguard.Option`→`GuardOption`） | **0 / 18** | 三个旧名逐个点名，零命中 |

### 1.2 但**间接依赖**会被编译穿（这是本轮实测踩到的真洞）

MVS 选中的 pkg 版本一旦 ≥ v0.45，**依赖图里任何还在用 `errors.Errorf` 的模块**都会编译不过，
哪怕本仓自己一行都没改。实测命中：

| 间接依赖 | 出问题的版本 | 已迁移的版本 | 影响 |
|---|---|---|---|
| `github.com/oagentkit/guardx` | v0.24.4（egress 包 2 处） | **v0.25.0 起** | yi-nology/git-manage-service |
| `github.com/yi-nology/go-git-platform` | v0.124.0（pkg/credential、gitbackend、provider） | **v0.128.0 起** | yi-nology/git-manage-service |

⇒ **抬地板前必须先扫依赖图里所有 require 了本包的模块**，不能只看本仓源码。
（下一版 pkg 侧的门禁也该把这一维纳进去：地板抬升判据要含「间接依赖的源码兼容性」。）

### 1.3 行为变更影响面

| 行为变更 | 受影响仓数 | 说明 |
|---|---|---|
| v0.46.0 `pagination` 快照深拷贝改走 `reflectx.DeepCopy`（含未导出字段的 struct，其导出引用字段由**共享**变**深拷贝**） | **8 / 18** 用 pagination | 语义只紧不松（隔离更强），但**值不再共享** |
| v0.56.0 `pagination` 五个 sentinel 由 `BizError(1)`→HTTP400 改为 `errors.Is` 身份 | **0 / 18** | 8 个用 pagination 的仓里，**无一引用**这五个 sentinel（`ErrBadPageSize` / `ErrNilFetcher` / `ErrTooManyPages` / `ErrBudgetExceeded` / `ErrNilYielder`），也**无一用 `WalkEach`**（`budget<=0` 兜底口径变更点） |
| v0.56.0 `encoding` 五入口分族（`Unmarshal`/`FromString`/`FromReader` 回归 float64） | **0 / 18** | 三个入口零命中 |
| v0.56.0 `urlguard.WithAllowedSchemes` 改替换语义 | **0 / 18** | 零命中 |
| v0.54.0 `webhook.SignerWithClock(nil)` 由 panic 改回退默认时钟 | **0 / 18** | 零命中 |
| v0.55.0~v0.55.1 三处 nil-ctx 兜底（`pagination.Walk` / `cache.Checker.Check` / `procx.MemGroup.Close`） | panic → 正常 | 只可能**变好**，不会变坏 |

⇒ 唯一需要专门回归的是用 `pagination.Walk` 的仓（本轮实测只有 `yi-nology/zentao-mini`，
4 个文件）——**该仓已实测 `go test ./...` 全绿**（抬升后无回归）。

## 二、18 个仓现状（抬升前）

| 仓 | 当前 pkg 地板 | 落后 | 消费的 pkg 子包（import 文件数） |
|---|---|---|---|
| yi-nology/jumpserver-mgmt | v0.42.0 | 19 版 | convert(23)、slicex(10)、cli(4) |
| yi-nology/zentao-max | v0.42.0 | 19 版 | httpjson(2)、secretx(2)、slicex(1) |
| yi-nology/zentao-mini | v0.42.0 | 19 版 | pagination(4)、httpjson(1)、secretx(1) |
| yi-nology/git-manage-service | v0.44.0 | 17 版 | pagination(3)、httpjson(2)、shellx(2) |
| yi-nology/jumpserver-kit | v0.44.0 | 17 版 | convert(4)、stringx(2)、slicex(2)、cache(1)、cli(1)、pagination(1) |
| yi-nology/agent-memory | v0.48.0 | 13 版 | jsonrepair(4)、container(3)、timex(3)、pack(2)、cache(2)、stringx(2)、encoding(1) |
| yi-nology/go-gongfeng | v0.48.0 | 13 版 | errors(4) |
| yi-nology/gopkg | v0.50.0 | 11 版 | webhook(2) |
| yi-nology/open-voice-bridge | v0.51.0 | 10 版 | errors(40)、ptrx(3)、cli(1) |
| yi-nology/git-ferry-core | v0.54.0 | 7 版 | errors(75)、stringx(9)、pathx(8)、ptrx(4)、convert(3)、timex(3)、procx(2)、timex/cron(2)、contextx(2)、tarx(1)、secretx(1)、encoding(1)、httpjson(1)、cache(1)、slicex(1) |
| yi-nology/go-git-platform | v0.54.0 | 7 版 | errors(48)、container(8)、shellx(6)、stringx(5)、cache(4)、encoding(3)、slicex(3)、timex(2)、webhook(2)、bufiox(1)、secretx(1)、pagination(1)、cache/cachetest(1)、httpjson(1)、pathx(1) |
| yi-nology/git-ferry | v0.57.0 | 4 版 | errors(53)、ptrx(16)、convert(6)、stringx(6)、pathx(5)、ctxscope(4)、pagination(2)、mathx(1) |
| yi-nology/code-review-service | v0.59.0 | 2 版 | slicex(19)、ptrx(16)、convert(15)、stringx(13)、errors(4)、pagination(4)、pathx(4)、mathx(4)、encoding(3)、cache(3)、jsonrepair(2)、httpjson(1) |
| yi-nology/goagent-kernel | v0.59.0 | 2 版 | errors(287)、slicex(8)、encoding(8)、stringx(7)、jsonrepair(4)、subhub(3)、ptrx(1)、cache(1)、pathx(1)、pagination(1) |
| yi-nology/goagent-runtime | v0.59.0 | 2 版 | errors(105) |
| yi-nology/argus-service | v0.60.0 | 1 版 | jsonrepair(8)、stringx(6)、pii(4)、shellx(4)、httpjson(3)、cli(2)、pathx(2)、ptrx(2)、errors(1)、mathx(1)、container(1) |
| yi-nology/bianque | v0.60.0 | 1 版 | errors(48)、encoding(10)、stringx(9)、cache(5)、slicex(4)、jsonrepair(4)、timex(4)、convert(3)、ptrx(1)、shellx(1)、container(1)、auditchain(1)、timex/cron(1)、pagination(1) |
| yi-nology/jumpserver-exec | v0.61.1 | 0 版 | shellx(7)、cli(4)、slicex(3)、encoding(2)、mathx(2)、stringx(2)、pathx(1)、tarx(1) |

## 三、编译级验证（地板最旧的 5 个仓）

上面的表给的是「已知 breaking 的影响面」；这一节给的是**真实编译结论**——
把地板最旧（跨度最大 ⇒ 风险最高）的 5 个仓 clone 下来，抬到目标版本实跑
`go get` → `go mod tidy` → `go build ./...`：

| 仓 | 原地板 | 抬升后 | 结论 |
|---|---|---|---|
| jumpserver-mgmt | v0.42.0 | `go build ./...` 通过 | ✅ 零编译期破坏 |
| zentao-mini | v0.42.0 | `go build ./...` 通过 | ✅ 零编译期破坏 |
| jumpserver-kit | v0.44.0 | `go build ./...` 通过 | ✅ 零编译期破坏 |
| zentao-max | v0.42.0 | 报 `backend/main.go:43: pattern static/*: no matching files found` | ⚠ 与 pkg 无关 |
| git-manage-service | v0.44.0 | 报 `pkg/embed/embed.go:8: pattern all:public: no matching files found` | ⚠ 与 pkg 无关 |

后两条做了**基线对照**：把 go.mod 回退到原地板（v0.42.0 / v0.44.0）重编，报错
**逐字相同** ⇒ 是这两个仓 checkout 里缺 `//go:embed` 资源目录的既有问题，
与 pkg 版本无关（两仓 main 的 CI 都是绿的；CI 侧有资源生成步骤）。
（git-manage-service 的嵌套模块 `third_party/gongfeng-sdk-go` 编译通过。）

## 四、执行记录（本轮实际落地）

18 个仓逐一抬到 `v0.61.5`，共开 16 个 PR（另 2 个仓当时已在目标版本）：

| 仓 | 抬前地板 | 抬后 | PR | 结果 |
|---|---|---|---|---|
| yi-nology/agent-memory | v0.48.0 | v0.61.5 | #69 | ✅ 合入 |
| yi-nology/argus-service | v0.61.4 | v0.61.5 | #118 | ✅ 合入 |
| yi-nology/bianque | v0.60.0 | v0.61.5 | #7 | ✅ 合入 |
| yi-nology/code-review-service | v0.61.4 | v0.61.5 | #11 | ✅ 合入（见下「既有红」） |
| yi-nology/git-ferry | v0.57.0 | v0.61.5 | #52 | ✅ 合入 |
| yi-nology/git-ferry-core | v0.54.0 | v0.61.5 | #49 | ✅ 合入 |
| yi-nology/git-manage-service | v0.44.0 | v0.61.5 | #27 | ✅ 合入（见下「间接依赖」） |
| yi-nology/go-git-platform | v0.54.0 | v0.61.5 | #176 | ✅ 合入 |
| yi-nology/go-gongfeng | v0.48.0 | v0.61.5 | #14 | ✅ 合入 |
| yi-nology/gopkg | v0.50.0 | v0.61.5 | #32 | ✅ 合入 |
| yi-nology/jumpserver-exec | v0.61.1 | v0.61.5 | #35 | ✅ 合入（见下「恢复清单」） |
| yi-nology/jumpserver-kit | v0.44.0 | v0.61.5 | #40 | ✅ 合入 |
| yi-nology/jumpserver-mgmt | v0.42.0 | v0.61.5 | #16 | ✅ 合入（见下「恢复清单」） |
| yi-nology/open-voice-bridge | v0.51.0 | v0.61.5 | #22 | ✅ 合入 |
| yi-nology/zentao-max | v0.42.0 | v0.61.5 | #14 | ✅ 合入 |
| yi-nology/zentao-mini | v0.42.0 | v0.61.5 | #37 | ✅ 合入 |
| yi-nology/goagent-kernel | v0.61.5 | v0.61.5 | — | 已在目标版本 |
| yi-nology/goagent-runtime | v0.61.5 | v0.61.5 | — | 已在目标版本 |

### 四个需要特殊处理的仓（都是「抬地板不只是改一行 go.mod」）

1. **git-manage-service —— 间接依赖被编译穿**（§1.2）：
   连带把 `github.com/oagentkit/guardx` 抬到 v0.30.0（v0.25.0 起已迁移 `errors.Errorf`）、
   `github.com/yi-nology/go-git-platform` 抬到 **v0.140.0**（v0.128.0 起已迁移，且仍保留
   `pkg/credential` / `githubapp` / `backends/all`；**不能跳 v1.x**——v1.2.0 已移除这些包，
   本仓与 git-ferry-core 的 import 全断）。
2. **jumpserver-exec / jumpserver-mgmt —— 模块缓存恢复清单要同步**：
   这两个仓有一套自订的依赖钉版机制：`script/restore-go-modcache.sh` 的 `MODULES` 清单
   必须与 `go.mod` 的 require **一一对应**，且每个 require 都被 `go.sum` 钉住
   （守卫测试 `TestTheDependenciesArePinned`）。抬了 require 不同步清单 ⇒ 守卫判红
   （错误原文：`github.com/oekit/pkg@v0.61.5 is in go.mod but not in script/restore-go-modcache.sh`）。
   ⇒ 抬地板必须同时改清单（jumpserver-exec 还需移除已不在 require 的 `logx/kitexlog`）。
3. **argus-service —— 一次时序 flaky**：首轮 CI 挂在
   `TestScopedHeartbeatUnblocksStaleRecovery`（心跳/复位时序），main 上此前两次同项均绿
   ⇒ 空提交触发重跑后通过。与依赖抬升无关。
4. **code-review-service —— 既有红**（非本次引入）：Quality Gate 的
   `Architecture` 与 `Backend` 两项在 **main 上已经红**（Architecture 跑
   `go run github.com/yi-nology/goagent-kernel/cmd/archcheck`，而 go.mod 里 require 的
   是 `github.com/oagentkit/goagent-kernel` ⇒ 引用了已迁走的模块路径；Backend 是 gofmt）。
   本 PR 与这两个红无关（diff 只有 pkg 一行），故照常合入。

## 五、给后来者的落地动作

地板抬升本身是机械操作（每个仓一条命令）：

```bash
cd <仓>
go get github.com/oekit/pkg@v0.61.5
go mod tidy
go build ./... && go test ./...
```

但**顺序与配套**有讲究：

1. **先扫间接依赖**：`go mod graph` 里所有 require 了本包的模块，逐个确认它们
   不用已删除的符号（§1.2）——否则 MVS 会替你选出一个编译不过的组合；
2. **先跑回归的仓**：`zentao-mini`（pagination.Walk 深拷贝语义）——它验证语义假设对不对；
3. **有自定义依赖机制的仓，先看它有没有「清单/守卫」**：改了 require 就同步清单；
4. **抬完后重跑 `go build ./...`**（含嵌套模块：`bootstrap` / `registry` / `timertask`
   等有嵌套 go.mod 的仓要逐个模块验）。

## 六、这面为什么会漏掉（机制缺口，已修）

本次追平不是「临时补一次作业」，而是机制侧补了两处：

1. **覆盖面**：[#333](https://github.com/oekit/matrix-tools/pull/333) 把 yi-nology 18 仓登记进
   `cross_org_consumers.json`。此前登记表里只有 oagentkit ⇒ yi-nology 整侧 18 仓
   **不在任何地板门禁的覆盖内**，oekit 侧发了新版 pkg，对侧落后多少**一条都看不见**。
2. **事实源**：yi-nology/bianque 是**私有仓**，`raw.githubusercontent.com` 对它任何路径都 404
   （匿名不可见），而跨 org 地板的 go.mod 读取走的正是匿名 raw ⇒ 它的 404 被判成
   「HEAD 上没有 go.mod」⇒ 私有消费方永远进不了地板视野。
   现在 404 会先走**认证读法**兜底，两条路都 404 才判「没有 go.mod」。

机制修好后，这面的落后由 audit 自动点名（⚠ 可见不阻塞，**追平动作属对侧 org**——
本侧报告是协调通道，不是对侧的执行器，#267）。

**还没修的一处**：跨 org 地板机制现在能看见「谁落后」，但**看不见「抬了会不会编译穿」**——
§1.2 的间接依赖维度目前靠人扫。地板抬升判据若要自动化，得把「依赖图里所有 require 了
本包的模块，其源码是否用已删除符号」纳入门禁。

## 七、复算方法

本文所有数字都可复算，方法写在 `oekit/pkg` 的 `api-lifecycle.yaml` 头部：

> 候选集 = go.mod 里 require 了 `github.com/oekit/pkg` 的仓（import 的必要条件，
> 列举用 GitHub API 且 `type=all` **含私有仓**）⇒ 逐仓扫 HEAD 源码里
> `github.com/oekit/pkg/<子包>` 的 import ⇒ 记「消费方仓数 + import 文件数」两个数。

⚠ **列举口径必须含私有仓**：只列公开仓会把私有消费方整体漏掉——
本轮 pkg 侧的 API 生命周期清单就是这么误把 3 个包记成「无下游消费方」的
（`sse` / `webhook` / `bufiox`，已在 [pkg#612](https://github.com/oekit/pkg/pull/612) 修正）。
