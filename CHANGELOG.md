# Changelog — oekit/matrix-tools

oekit 模块矩阵的自动化工具：生成器 + 全量审计 + 定时巡检。
本仓此前无版本化（工具仓，随用随改）；v0.1.0 起建立版本记录，
以便回执类流程能指向「哪个版本落地了哪次变更」。

## [v0.1.9] — 2026-10-22

### fix(tools)：读到空 ≠ 事实为空——GitHub 偶发返回空集合，导致 audit 假漂移与 PR 被误关

2026-10-22 巡检实测：**GitHub API 会间歇性返回空集合**（20~30% 的请求）——
不是限速、不是权限、不是客户端解析错，服务端这一次就返回了空：

- `GET /repos/{o}/{r}/releases` ⇒ 实测 10 次里 2 次返回 `200` + `[]`（5 字节）；
  **同一 URL 用 curl 带 token 直连同样复现** ⇒ 排除 gh 客户端与本地网络；
- `POST /graphql`（ProjectV2 items）⇒ 实测 10 次里 2 次返回 `totalCount=0, nodes=[]`；
- `gh api --paginate ... --jq '.[].tag_name'` ⇒ 读到 `[]` ⇒ jq 过滤后**零行输出**
  ⇒ 调用方看到的就是「exit 0 + 空输出」。

「读到空」被当成「事实为空」，三处都会得出**完全相反**的结论：

- **audit 报假漂移**：旧 `existing_releases` 把空当成「一个 Release 都没有」
  ⇒ 实测 11 个仓被报「N 个 tag 缺 Release」，而逐仓用可靠路径复核的真实 parity 是
  **24/24 全绿**（每个仓 tags == releases，missing 全空）；
- **PR 被当成 issue 关掉**：`target_is_pr` 读空 ⇒ JSON 解析失败 ⇒ 判成「不是 PR」
  ⇒ 对**未合入**的 PR 调 `gh issue close`——v0.1.8 刚修掉的坑从这条缝复活；
- **分支保护被当成没有**：`current_protection` 读空 ⇒ 也返回 None
  ⇒ 把「读不到现状」当成「这仓没有保护」⇒ 按错误现状去改配置。

#### 新增 `scripts/ghsafe.py`：远端读取层

- **命令非零** 与 **正文为空/不可解析** 分开表达（`GhCommandError` / `GhReadError`）
  ——调用方不能再一起 `except` 成「没有」；
- **空结果不采信**：要读到空得**连续 4 轮都是空**才认，中途任何一轮拿到非空就立刻返回；
- **读不到就报错**，不猜：宁可脚本失败，也不把「读不到」当成「没有」。

#### 配套改动

- `gen_releases.existing_releases` ⇒ 空则**换条独立路径重读**（GraphQL ↔ REST 交叉验证），
  连续 4 轮都空才认「事实为空」——真没建过 Release 的仓仍会被**如实报成真漂移**
  （不能因为修假红而漏真红）；
- `gen_releases._has_token` ⇒ 改用 `gh auth status` 判认证：旧实现只看 env 变量，
  看不见「gh 已登录（keyring / hosts.yml）」这种常态 ⇒ 本机明明有认证却被当匿名，
  于是走了最容易抖的 REST 路径，而实测 10/10 稳定的 GraphQL 路径没被用上；
- `close_issue.target_is_pr` ⇒ 读不到就**中止**（绝不当成「不是 PR」）；
- `pr_flow.current_protection` ⇒ 404 才当「无保护」，读不到就显式失败。

#### 自测与变异测试

新增 `scripts/ghsafe_selftest.py`（假 gh；12 组用例 + 3 条重试次数断言），
已接进 `audit.yml` 的 selftest job。假 gh 复刻真实故障的三种形态：
「前 N 次 exit 0 + 空正文、之后才有数据」「永远空」「命令失败（403/404）」。

变异测试（红在**断言**层而非编译/导入层）：

- `gh_json` 的空正文退回旧语义（直接返回 `{}`）⇒ A/B/H 转红，
  其中 H 红在 `IS_ISSUE`（正是「PR 被当 issue 关掉」那个面）→ 12/15；
- `existing_releases` 退回「读一次，空就是空」⇒ E/F 的重试次数断言转红 → 11/15；
- 还原 ⇒ 15/15（exit 0）。

#### 效果

`python3 scripts/audit.py` ⇒ **✓ 矩阵审计通过（24 模块 + 站点）**（exit 0），
与逐仓复核的真实 parity（24/24）一致；假漂移 11 项消失。

## [v0.1.8] — 2026-10-22

### fix(tools)：回执链三个硬缺陷——无发布的改动没有回执通道、PR 被当成 issue 去 close、重跑重复发

三个缺陷同源于一件事：**回执是巡检链的最后一环，却只在「发了版」这一条路径上存在**。
2026-10-22 实测：上一轮一次性处理 25 个条目（各仓一条入板并发组修复），
合入后 25 个条目**全部零回执**、看板却已 Done ⇒ 回执静默丢失、无任何门禁会报。

- **新增 `--no-release` 模式**（不发 SemVer 的改动也能发回执）
  - 此前脚本硬要求 `--tag`，回执内容围绕「版本 + CHANGELOG 小节 + Release 链接」组织
    ⇒ CI 基础设施 / 文档 / 治理配置这类**不构成发版单元**的改动
    **没有任何可用的回执通道** ⇒ 巡检链走到最后一步只能跳过
  - 回执形态：变更内容（`--note`）+ 合入证据（PR 链接 / merge commit / 合入时间 / CI 结论）
    + 显式声明「无 SemVer 发布」并说明**为什么**不发
  - `--tag` 与 `--no-release` **恰好二选一**（都传或都不传都判红）
- **目标是 PR 时只发回执、绝不 close**
  - GitHub 的 `issues` 端点与 `gh issue view` 对 **PR 编号**都能解析，
    而 PR 的 state 是 `MERGED`/`OPEN`（不是 issue 的 `CLOSED`/`OPEN`）
  - ⇒ 旧逻辑会对 PR 调 `gh issue close`：**对未合入的 PR 就是「不合并直接关掉」**；
    且关单后校验 `state == "closed"` 对 PR 永远不成立 ⇒ 脚本必然误判红
  - 实测：上一轮那 25 个「issue」**实际都是 PR**（`is_pr=true`）
  - PR 尚未合入时回执写「已提交修复（PR 尚未合入）」而**不**写「已合入修复」——不做假声明
- **幂等**：回执已在 ⇒ 跳过投递直接收工，避免重跑在评论区堆一串重复回执
- **回执判据收紧**：从「提到该 tag」升级为「**同时**提到全部 needle」
  （发版模式 = tag；无发布模式 = 「无 SemVer 发布」+ PR 号）
  ——「issue 上有任何评论就算数」在旧回执残留 / 评论发到别的 issue 时都会假绿
- **合入证据自动取证**：`gh pr view` + `gh pr checks` 取 PR 链接 / merge commit /
  合入时间 / CI 结论；**取不到证据不让整条回执失败**——投递是硬要求，证据完整是软要求
- **自测扩到 10 组用例**，并用**变异测试**证明自身有效：12 个变异点
  （判据退化 / 删掉无发布分支 / 跳过投递 / 丢标记 / 丢证据行 / needle 变弱 /
  PR 被 close / 去掉幂等 / 未合入却声明「已合入」…）**全部红在断言层**，无一漏网
  - 首轮自测有 **2 个变异点漏网**：「PR #70 出现在回执里」这条弱断言被默认正文
    「见 PR #70。」满足了 ⇒ 改传 `--note` 并把断言卡在「**合入**」证据行上
  - 另修一处**用例间串扰**：临时记录文件名由参数派生，用例4 与用例10 参数相同
    ⇒ 共享记录文件 ⇒ 用例10 误判成「重复发布了评论」⇒ 临时文件名加**唯一 case 标签**

## [v0.1.7] — 2026-10-22

### feat(audit)：dependabot 目录存在性门禁——已 retract 的嵌套模块残留条目会让 dependabot 每周必失败

- 判据：各仓 `.github/dependabot.yml` 里 **gomod ecosystem** 的 `directory:` 条目，
  指向的目录必须**存在**且含 `go.mod`；两种失败分开报（目录不存在 = 残留登记该删 /
  目录在但无 go.mod = 路径写错该改）
- 背景见 [oekit/data#39](https://github.com/oekit/data/issues/39)：`all/` 聚合嵌套
  模块在 v0.5.0 门面化时已 retract 并删目录，但 dependabot.yml 里的 `directory: /all`
  残留登记让该条 job **每周必失败**（`dependency_file_not_found: No go.mod files found`）
  且**不会自愈**——它不报 build 错（不在 go 编译链路上），只在 dependabot 自己的
  run 里红，且每周一次
- 判据抽到 `scripts/dependabot_dirs.py`（单一事实源，行解析而非 yaml 库——CI 的
  python 环境是裸装的，不该为这一个门禁引入 pyyaml 依赖），配
  `scripts/dependabot_dirs_selftest.py` 自测并挂进 `audit.yml` 的 selftest job
- 自测用**变异测试**证明自身有效：分别掐掉「gomod 生态过滤」「go.mod 存在性检查」
  「目录存在性检查」三处逻辑，自测各自必须失败（实测发现：只断言「报错点名了 /all」
  时，掐掉「目录存在性检查」自测照样绿——目录都不存在时「没有 go.mod」那条分支也
  同时成立 ⇒ 报错措辞必须断言到「不存在的目录」这一层）

### fix(tools)：发版收尾链三处硬失败——自动归位 main、lease 强推、--help 守卫

- `prkit.sync_main`：现场不在 main 且工作树干净 ⇒ 自动归位 main（原来直接
  `SystemExit`，整条收尾链退出 1——2026-10-22 实测站点仓停在 `docs/version-bump`）
- `prkit.push_branch`：远端同名分支含不在 main 上的提交 ⇒ 按 lease 强推复位
  （原来普通 push 报 `non-fast-forward`，整条链退出 1；lease 失配则拒绝，不覆盖他人工作）
- `release_post --help`：只打印不动作（原来会真跑一条收尾链——2026-10-22 实测误触发
  生成了一份陈旧的站点 PR 与 BOM PR）
- `matrix.tag_exists` / `matrix.declared_version`：版本号事实核对的两个事实源
  （查**远端** tag / 仓内**声明**版本；两者不一致 = 版本号断点，消费方永久拉不到
  且无门禁会报）

## [v0.1.6] — 2026-10-21

### fix(close_issue)：回执投递路径的大小写 bug——state 归一化为小写

- 上一轮（v0.1.5）补了「回执没落地要判红」，**判据这半边是对的**：本轮实测确实
  抓到了回执丢失（脚本如实报「回执未落地」）。但**投递路径那半边仍然是坏的**——
  所以判据每轮都在报同一个失败，而回执依然没落地
- 根因：真实 `gh issue view --json state --jq .state` 返回的是**大写** `CLOSED` /
  `OPEN`，而脚本比较的是小写 `"closed"` ⇒「issue 已关闭 ⇒ 改走 `gh issue comment`
  单独补发」这条分支**永远走不到** ⇒ 一律调 `gh issue close --comment` ⇒ 真实 gh
  对已关闭 issue 打印 `! Issue ... is already closed`、**以 0 退出、且不贴评论**
  ⇒ 回执静默丢失
- 本轮实测：pkg#46 / pkg#48 / concurrency#56 三条回执全部没落地（三条 issue 上
  评论数均为 0）——**判据红得对，投递是坏的**
- 修法：`issue_state` 读取时归一化成小写（`.lower()`），比较口径不变

### test(close_issue)：自测 fixture 改用真实 gh 的取值形态

- `close_issue_selftest.py` 的假 gh 把 state 取值写成小写 `closed`，而真实 gh 返回
  大写 ⇒ **自测在一组现实里不存在的取值上通过**，真实调用里却失效——**自测本身
  成了盲区**（与「门禁与被检对象共享逻辑」同类错误：验证的是假想输入下的行为，
  不是现实输入下的行为）
- fixture 改用真实取值 `CLOSED` / `OPEN`；另加一条小写容错用例
- 反证自检实测：掐掉 `.lower()` 后自测必然失败（用例1 报「回执未落地」、退出码非 0），
  恢复后通过

## [v0.1.5] — 2026-10-21

### fix(close_issue)：回执必须真落地——已关闭 issue 走 comment 补发，判据不信退出码

- 现象：issue 往往已被 PR 正文的 `Closes #N` 在合入时**自动关闭**；随后
  `gh issue close --comment` 对已关闭 issue 只打印「! Issue ... is already closed」
  并**以 0 退出**，评论**根本不会发**——而旧脚本只看退出码，于是每轮都打印
  「✓ 已回执并关闭」，**回执一条都没落**（本轮 3 个 issue 的回执全丢，已手工补发）
- 修法：先读 issue 状态——已关闭走 `gh issue comment` 单独补回执，未关闭才走
  close+comment；收尾**复核**回执真的落在 issue 上（存在提到该 tag 的评论），
  否则判红；关单同样复核状态确实变成 closed
- 判据是「回执真的落地」，不是「命令返回成功」——这是本条与旧实现的根本差别
- 自测：`scripts/close_issue_selftest.py`（带反证）——用假 gh 复刻真实 gh 的静默
  失败，断言①已关闭 issue 也会补发回执 ②回执未落地时判红不假绿；反证已验：
  把「已关闭 → 改走 `gh issue comment`」回退成旧写法，自测必失败
- 接线：并入 `.github/workflows/audit.yml` 的 `selftest` job（pull_request 触发）——
  共享的 `python-ci.yml` 刻意不执行脚本本体，自测放本仓 workflow 才算数；
  不接线的话这就是一条「没人跑的承诺」

## [v0.1.4] — 2026-10-20

### fix(audit)：breakingIndex oracle 不再要求覆盖「未发布」内容

- 现象：audit 报「hertz: CHANGELOG 有破坏性标记但 breakingIndex 未覆盖（bullet，
  Unreleased）」——但那**不是**矩阵漏收：hertz 的 `## [Unreleased]` 段尚未发布
  （最新 tag v0.7.1），矩阵描述的是**已发布**版本线，未发布内容进矩阵反而是提前剧透
- 根因：oracle 的 `_oracle_markers()` 无条件收下所有版本段里的标记，只有「段标题」
  分支才跳过非 semver 版本头，`bullet` / `standalone` 分支**没跳** → 误报
- 修法：新增 `ORACLE_PUBLISHED_RE`（只认 `vX.Y.Z`），oracle 只收**已发布版本段**
  里的标记；生成器侧口径本就正确（`_normalize_version` 对 Unreleased 返回 None）
- 实测：全矩阵 oracle 标记 33 → 32，**只少 hertz 那 1 条未发布的**，其余覆盖未丢
- 这条误报会**卡住全组织的 matrix-tools PR 门禁**（PR 模式下 drift≠0 即红）

## [v0.1.3] — 2026-10-20

### feat(nested)：嵌套模块加 `hostVersion`——与父仓的版本对应关系（#37 ① 口径落地）

- 背景：嵌套模块**不与父仓同号**（口径裁决见 issue #37 ①）——0.x semver 里 minor
  号就是破坏性轴，强行同号等于对下游说「这里可能不兼容」而实际零内容变更；
  且同号不构成兼容性保证（`logx v0.7.1` 与 `logx/sink v0.7.1` 是两个独立 module，
  Go 不做任何检查）。改为**显式登记对应关系**
- `gen_versions.py`：`nested[]` 新增 `hostVersion` = 该嵌套发布所处的**父仓版本区间**
  （判据：**commit 祖先关系**——父仓根 tag 的 commit 是该嵌套发布 commit 的祖先
  ⇒ 该发布落在那个父仓版本之后、下一个父仓版本之前）。客观 git 事实，不依赖
  CHANGELOG 措辞
- `verify_release_units.py`：门禁**自带**一套推导（**tag 日期序**——父仓根 tag
  里创建时间不晚于该嵌套 tag 的取最新者），与生成器**不同源**；两者交叉比对：
  共享判断逻辑就会一起错、门禁照样全绿（#31 的教训）
- 门禁新增 oracle C：已发布 `versions.json` 的 `nested[]` 必须与门禁推导一致
  （版本 + hostVersion 双向）；且产物里登记的嵌套模块**目录必须还活着**
- 顺带修掉一个真 bug：`versions.json` 一直登记着 `data/all v0.1.1`——该嵌套模块
  已由 data v0.5.0 声明 **retract**、目录已删除，矩阵却仍把它当可按需取用的模块
  报给下游。生成器现在**跳过目录已不存在的嵌套模块**（撤回≠删除 tag，已发布物不动）

## [v0.1.2] — 2026-10-20

### fix(release-train)：CHANGELOG 门禁预校验必须在 CHANGELOG 所属仓的目录里跑

- `changelog_check.py` 用 `git describe --exact-match --tags HEAD` 判断「HEAD 处于 tag」，
  但 `release_train.py` 调用它时**没切目录** → 拿到的是**调用方所在仓**的 tag
- 实测踩过：从 matrix-tools 目录校验 data 的 CHANGELOG 时，matrix-tools 的 HEAD 恰好
  有 tag `v0.1.1` → 误报「data 的 HEAD 处于 tag v0.1.1，但无同名小节」——
  data 根本没有 v0.1.1 这个 tag，**报错指向的仓和版本号都是错的**，发版被凭空拦下
- 修法：`validate_changelog` 传 `cwd=` CHANGELOG 所属仓根目录（从 CHANGELOG 路径向上
  找 `.git`），嵌套模块的 CHANGELOG 同样正确定位到自己的仓

## [v0.1.1] — 2026-10-19

### feat(land-order)：跨仓生成器变更的落地顺序工具化（#34）

- 新增 `scripts/land_generator_change.py`：跑生成器 → diff 产物 → 按序开产物 PR
  （站点 `versions.json` → oekit BOM）→ 打印「产物 PR 已开，可合生成器 PR 了」。
  默认只检查（跑生成器 + diff + 列待落地产物），`--apply` 才推分支开 PR
- 产物里**只有时间戳变化**时不算实质变更——否则脚本每次都报「产物有变化」，
  排查者会学会忽略这个输出，工具就废了
- `audit.py`：门禁的「漏收/多出」报错**带上落地顺序提示**（指向 #34 与新脚本）
- `README.md`：新增「跨仓生成器变更的落地顺序」章节——三种顺序的后果对照表
  （最易漏的是 B：用旧生成器落产物）、四步正确顺序、脚本用法

### feat(breaking-index)：补路径级移除信号 + 归属校验门禁（#36）

- `refactor` 段里的**路径级移除**（如「`github.com/oekit/x/y` → `github.com/oekit/x`」）
  是真 breaking，但条目正文未必带关键词——补信号识别这类路径迁移
- 补**归属校验门禁**：条目声明的版本必须归属到正确的 module（防止嵌套模块的
  条目被错记到父模块）

### chore

- 产物同步至 82 条 breakingIndex（含嵌套模块 CHANGELOG 与路径级移除信号）

## [v0.1.0] — 2026-10-19

### feat(breaking-index)：breakingIndex 改以各仓 CHANGELOG 的 Breaking 段为源

- 旧实现只做**行内关键词匹配**（`breaking|不兼容|破坏性`），漏掉绝大多数已标注条目——
  关键词在**段标题**里，条目行内没有（典型如 registry v0.4.0 的
  `- **SPI 包提升至模块根**`）。实测 24 模块中 5 模块 15 条 Breaking 未收录，
  registry/nns/tracing 的 breakingIndex 曾是**空数组**，而这正是下游必断的变更
- 解析口径改为五路**确定性信号**并用：
  - A) 段标题即语义信号（`### Breaking` / `### BREAKING` / `### 不兼容` / `### 破坏性`），
     段内条目一律收录；conventional 破坏性标记段（标题以 `!` 结尾）同理
  - B) 条目正文含 breaking/不兼容/破坏性（覆盖无 Breaking 段的模块）
  - C) 条目**加粗前缀**同时含「提升」与「模块根」（本仓自身 import 路径变更；
     判据卡在加粗前缀，避免把「依赖升级：registry v0.4.0（SPI 包提升至模块根）」
     这类别仓变更收进来）
  - D) 加粗前缀含 `!`（`**feat!**` 等 conventional 破坏性标记）
  - E) **顶格独立成行**的破坏性标记（`**BREAKING**: ...`，不带 `- ` bullet 前缀）——
     gormx v0.6.0 与 hertz v0.7.0 的主键类型泛型化正是这种写法，旧解析器按
     `startswith("-")` 过滤 → 标记行与其后**全部**条目一起漏收
- 嵌套模块的 CHANGELOG 一并扫描（版本号带路径前缀，如 `redis/v0.2.0`）——
  嵌套模块的破坏性变更同样是下游必断的
- 条目边界含多行续行与缩进子条目（跨行条目只取首行会把变更说明截断）

### feat(audit)：新增 breakingIndex 完整性门禁 + 独立 oracle

- 条目级**全量双向比对**：CHANGELOG 标注的破坏性条目必须在 versions.json 里逐条在位；
  versions.json 里的条目必须能在 CHANGELOG 里找到（反向防手改漂移）
- 刻意**不按已发布 tag 过滤**——那会在「本地没 fetch 到那些 tag」时把条目误判为
  未发布而跳过，是**门禁假通过**的典型形态（规则照常在报告里，一件坏事不判）
- 另加一路**独立 oracle**：判据**刻意不复用解析器逻辑**——上面的双向门禁两边共享
  同一套判断，解析器一旦有盲区两边一起漏、门禁照样全绿。oracle 直接扫 CHANGELOG
  原文找肉眼可见的标记形态，再要求 breakingIndex 必须覆盖到，验的是
  「原文里写着的破坏性，矩阵有没有如实告诉下游」
- 机器判不了的疑似破坏性（`**refactor(...)**` / `### Changed` 段）打成 ⚠ 提示，
  不阻塞 CI，也不静默吞掉

### fix(diagram)：pkg 芯片微库清单补齐并校正计数

- pkg 芯片此前停在「20 合 1」——漏了 v0.15.0 新增的 `urlguard`
- 补齐 `urlguard` / `cli` / `pack`，计数校正为 **23 合 1**；明暗两版 SVG 同步重生成
  （oekit 主仓 + 站点仓共 4 份产物）

### chore(desc)：matrix.py 的 DESC 口径同步

- `pkg`：23 合 1 + `cli`/`pack` 新子包 + `stringx.Masker` + `LocalCache` 可选形状
- `timertask`：Redis 后端迁嵌套模块 `timertask/redis` + 新增 `local` cron 调度器子包
- `data`：门面壳跟随源仓演进（cache 壳补 LocalCache 可选形状、cachetest 壳补 5 个
  契约套件入口、timertask 壳的 Redis 形态随源仓迁出）
