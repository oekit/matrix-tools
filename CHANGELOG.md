# Changelog — oekit/matrix-tools

oekit 模块矩阵的自动化工具：生成器 + 全量审计 + 定时巡检。
本仓此前无版本化（工具仓，随用随改）；v0.1.0 起建立版本记录，
以便回执类流程能指向「哪个版本落地了哪次变更」。

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
