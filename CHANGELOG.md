# Changelog — oekit/matrix-tools

oekit 模块矩阵的自动化工具：生成器 + 全量审计 + 定时巡检。
本仓此前无版本化（工具仓，随用随改）；v0.1.0 起建立版本记录，
以便回执类流程能指向「哪个版本落地了哪次变更」。

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
