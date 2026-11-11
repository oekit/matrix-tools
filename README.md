# matrix-tools

oekit 模块矩阵的自动化工具：生成器 + 全量审计 + 定时巡检。

## 本地用法

```bash
# 全量审计（module 行/残留引用/门面/AGENTS/CHANGELOG/安装行版本/矩阵表新鲜度/站点）
python3 scripts/audit.py

# 修复类生成器（多数默认只报告，--apply 才写）
python3 scripts/gen_tables.py            # 各仓 README 矩阵总览表
python3 scripts/release_train.py bootstrap main=v0.6.2 nacos=v0.3.2  # 嵌套发布 train（dry-run，--apply 打 tag 推送）
python3 scripts/refresh_versions.py --push  # README 安装行对齐最新 tag
python3 scripts/shell_sync.py            # 门面壳同步检查（已并入 audit 硬门禁；含 go.mod 版本地板核对）
python3 scripts/gen_releases.py          # GitHub Release 对账/回填：每 tag 一个 Release（--apply 创建，已并入 audit 门禁）
python3 scripts/gen_matrix_go.py         # oekit 仓 BOM 模块生成（go.mod 地板 + matrix.go 快照；--check 为 audit 门禁）

# ⚠️ 两个会**整文件覆写**各仓文件的工具——默认只报告，别顺手跑
python3 scripts/gen_agents.py            # 只认 --new-repo <name>（新仓冷启动）；全量生成已禁用
python3 scripts/gen_changelog.py          # 默认只报告；--apply 才写（会覆盖 release_train 的详实条目）

# issue 存量扫描（只读，绝不关单）：org 全仓 open issue 盘点 + 分级战报
python3 scripts/issue_sweep.py --org oekit --out .issue-sweep
# 分级：merged-pr（修复已合入未关，立即可清）→ re-report（重报单）→ pr-open（等合入）
#       → stale（陈年）→ work（真实待修）；清零动作走 scripts/close_issue.py
```

`MATRIX_BASE` 环境变量指定矩阵检出根目录（默认 `~/my_project/ekit-split`；
支持 `<base>/<name>`、`<base>/ekit-<name>`、`<base>/repos/<name>` 三种布局）。

## 自动巡检

`.github/workflows/audit.yml`：每周一 03:00 UTC + 手动触发。
Actions 环境下自动批量克隆全仓（`repos/<name>` 布局，tag 走 ls-remote），
发现漂移自动创建/更新 `matrix-drift` 标签的 issue 并 @ 维护者。

`.github/workflows/maintenance.yml`：每月 5 日 04:00 UTC + 手动触发。
同样在 `repos/<name>` 布局下盘点**依赖地板漂移**，漂移 ≥8 仓时创建/更新
`matrix-maintenance` 标签的 issue（维护轮到期提醒）。**只有确定没有漂移、
且没有仓读不到时才关闭该 issue**——「读不到」≠「零漂移」（matrix-tools#96）。

## 规范

- 模块清单与描述的唯一事实源在本仓 `matrix.py` 的 `MODULES`/`DESC`
- 新增模块：改 `matrix.py` → 跑 `gen_tables.py --push` → 跑 `audit.py` 确认全绿
- 发版后：跑 `refresh_versions.py --push` 对齐各仓安装行
- **合入方式：走 PR，不直推 main**——全 org main 已开 `enforce_admins`，直推会被
  `GH006 Changes must be made through a pull request` 拒掉；tag 打在合入 main 之后

## 跨仓生成器变更的落地顺序（matrix-tools#34）

audit 门禁在 **PR 模式**下比对的是「**当前生成器的输出** vs **已发布产物**」。
改了生成器（`gen_versions.py` / `gen_matrix_go.py`）之后，两侧只有一种组合能对上：

| 落地顺序 | 生成器 | 已发布产物 | 门禁结果 |
|---|---|---|---|
| A. 先合生成器 | 新 | 旧 | ❌ 漏收 N 条（产物缺新条目） |
| B. 用**旧**生成器落产物 | 旧 | 新 | ❌ 多出 N 条（产物有旧生成器产不出的条目） |
| C. **用新生成器落产物 → 再合生成器** | 新 | 新 | ✅ 通过 |

**最容易漏的是 B**：直觉上「先落产物再合生成器」听起来对，但产物必须由
**新**生成器产出——用旧生成器产出的产物落下去，门禁照样红。两种报错都只说
「漏收/多出 N 条」，完全不提落地顺序，排查成本高（#34 的实测记录）。

**正确顺序**：

1. 在本仓（matrix-tools）分支上改生成器 → 开 PR（**先不合**）
2. 跑 `python3 scripts/land_generator_change.py --apply`：它用**当前分支的**
   新生成器产出产物 → 按序开产物 PR（站点 `versions.json` → oekit BOM）
3. 等产物 PR 合并（站点 → BOM）
4. 再合生成器 PR

`land_generator_change.py` 默认是只检查模式（跑生成器 + diff + 列待落地产物），
加 `--apply` 才推分支开 PR。产物里只有时间戳变化时**不算实质变更**（否则每次
都报「产物有变化」，排查者会学会忽略这个输出）。

> 门禁报错本身也带这条提示：`check_breaking_index` 报「漏收/多出」时会附上
> 落地顺序说明，不必再回来查 issue。

## 脚本

47 个脚本按**什么时候跑**分四组。新会话先看「日常」那组；「一次性」那组
除非明确要做那件事，否则不要碰（其中两个会**整文件覆写**各仓文件）。

### 日常（会话内高频）

| 脚本 | 作用 |
|---|---|
| `matrix.py`（**仓根**） | **共享事实层**：MODULES/DESC 登记表、仓库目录解析（`repo_dir`/`local_dir`）、远端 tag 读取与缓存（`latest_tag`/`remote_tags`）、`sh`/`git_branch`/`is_dirty`。**判定不许 import 门禁脚本，事实只从这里取** |
| `scripts/inflight.py` | **在途登记**（并行会话互斥）：`claim <repo> 说明` / `release <repo>` / `list`；PID+4h TTL 判定持有 |
| `scripts/handoff.py` | **现场交接清单**：新会话开工先跑——在途登记 / 非 main·脏工作树 / org open PR 一屏可见 |
| `scripts/preflight.py` | **开工前检查**：`preflight.py <repo>`——登记占用/脏树/分叉即 BLOCK（exit 1），落后远端/在途 PR 给 WARN |
| `scripts/prkit.py` | PR 落地共用件（`doc_wave`/`release_post`/`align_floors` 内部复用）：sync main → 分支 → 提交 → 开 PR → update-branch/auto 合并 |
| `scripts/audit.py` | **全量审计**（module 行/残留引用/门面/AGENTS/CHANGELOG/安装行版本/矩阵表新鲜度/站点/嵌套地板/dependabot 目录）——发现漂移 exit 1 |
| `scripts/dep_matrix.py` | **依赖版本矩阵**：`dep_matrix.py pkg` 看谁在用哪个版本（★=最新）；全矩阵视图——按需发版模型的决策面 |
| `scripts/ledger_write.py` | **巡检台账写入器**（`.oekit-cron-state.json`）：`--new-run` 开新一轮 / `--issue repo#N [--pr N] [--tag vX.Y.Z] [--closed]` 记一条 / `--run-end` 收口。**事实一律从 GitHub 回读**（issue 状态、PR 是否已合并、tag 是否在远端）⇒ **与要记的内容不符就拒绝写入**（宁可缺一条，也不要假的）；`--run-end` 会把本轮每条事实**再回读一遍**⇒ 记录期间现实变了就报出漂移 |

### 发版链（顺序不可反）

**嵌套模块发版的四条约束**（2026-11-03 实测，踩过 ⇒ 写进规范，不只靠工具行为）：

1. **嵌套模块的 CHANGELOG 节名按形态记**：**仓根** CHANGELOG 记**全名**（`## [nacosimpl/v0.1.3]`），
   **嵌套模块自己的** CHANGELOG 记**短名**（`## [v0.1.3]`）。消费方按仓根的全名节定位发布说明。
   - ⇒ `changelog_check.py` 规则 3「HEAD 处于 tag 时必须有同名小节」**必须按形态分别判定**
     （根形态比根节名、嵌套形态比短名节名、**别的模块的文件不适用**该规则）
     ⇒ 这条**已实现**（`oekit/.github` #57，main `55d39ba`）。
   - ⚠️ **残留的洞**：#57 仍用 `git describe --exact-match` 取 tag ⇒ **同 commit 上有多个 tag 时
     只返回任意一个**（实测优先返回嵌套 tag 名）⇒ 另一个模块的短名节**从不被校验** ⇒ 缺了也判绿
     （**假绿**）。实测：registry HEAD 停在 `nacos/v0.1.3` + `nacosimpl/v0.1.4` 同 commit 上，
     删掉 `nacosimpl/CHANGELOG.md` 的 `## [v0.1.4]` ⇒ 判 ✓（假绿）。
     ⇒ 修法 = 改用 `git tag --points-at HEAD` 取**全部** tag 并逐个按形态判定。
   - ⚠️ **「同仓的 train 发成功后重跑会被拦下」是错的**（2026-11-03 实测，勿写进规范）：
     规则 3 要求同名节**存在**（不是「最新」）⇒ 已发布版本的短名节**留在文件里**
     ⇒ 重跑判**绿**。实测（#57 与 #58 两版口径**都绿**）：bootstrap HEAD 停在
     `nacos/v0.3.10`+`obs/v0.2.14` **两个 tag** 上 ⇒ `nacos/`、`obs/`、仓根**三个面全 ✓**；
     registry HEAD 停在 `nacos/v0.1.3`+`nacosimpl/v0.1.4` ⇒ 三个面全 ✓；
     timertask HEAD 不在任何 tag 上 ⇒ 规则 3 整段跳过 ✓；
     kitex HEAD 停在 `…/kitexotel/v0.7.7` ⇒ ✓。
     ⇒ 唯一会红的情形是**本模块刚发的版本节被删了**（那是**该红**，不是「凭空拦」）。
2. **同一仓的多个嵌套模块，仍建议放同一条 train**（例：
   `release_train.py bootstrap nacos=v0.3.10 obs=v0.2.14`）。
   - ⚠️ **这条在 #57 之后已不再是「硬拦」**：实测 bootstrap HEAD 停在 `nacos/v0.3.10`（另一个模块的 tag）上，
     查 `obs/CHANGELOG.md` ⇒ #57 判**绿**（修复前会判红：「HEAD 处于 tag nacos/v0.3.10，
     但无同名小节 ## [nacos/v0.3.10]」）⇒ 「分两条 train 必被拦」**已不成立**。
   - **仍建议同一条 train 的理由**（流程层面，非门禁层面）：① 一次发完 ⇒ 同一仓的嵌套模块
     **版本号同轨**、不会被分两次的发布节奏拉开；② 版本节一次写齐 ⇒ 仓根的全名节与各模块的短名节
     **同一次落盘**，不会短暂出现「一边有、一边无」；③ 少一轮 train ⇒ 少一轮 tag/查重/Release。
   - ⇒ **性质**：这是**流程约定**，不是工具强制 ⇒ 违反它**不会被门禁拦住**，但会让发布节奏与
     版本节对齐变差。
3. **同一仓多个嵌套模块发版时，注意分支名形态**（`release_train` 自动开 PR 时用）：
   - 分支名形如 `chore/rel-<tag>` ⇒ 深层嵌套会拼出**多层路径**
     （`chore/rel-observability/tracing/instrumentation/kitexotel/v0.7.7`）
     ⇒ 与同仓既有的 `chore/rel-<首段>` 短分支构成 **git 引用 D/F 冲突**（实测 `fatal: … exists;
     cannot create 'refs/heads/chore/rel-nacos'`）。
   - ⇒ 目标形态应是**单层且唯一**：`chore/rel-<模块名>-<版本号>`
     （仓内既有惯例：`chore/rel-nacosimpl-v0.1.1`、`chore/rel-redis-v0.2.2`）
     ⇒ **只取版本号不够**：`registry` 的 `nacos/v0.1.3` 与 `nacosimpl/v0.1.3` **同版本号**
     ⇒ 只取版本号会撞名 ⇒ 而 `open_pr` 是按**分支名**查已有 open PR ⇒ 撞名会**复用别的模块的 PR**。
4. **一轮闭合在有依赖链时不能一次发完，必须分阶段**：**被依赖方先发 → 依赖方抬地板 → 依赖方再发**，
   且**抬升必须晚于被依赖方的发布**。
   - **为什么必须分阶段**：tag **不可重推** ⇒ 地板落后只能靠**再发一个新 tag** 补
     ⇒ 一次发完 = 依赖方**生而带旧地板** = 消费方解析到旧地板 = **自造欠账**。
   - **为什么抬升必须晚于发布**：提前把 `require` 抬到**还没发布**的版本 ⇒ `go mod` **解析失败**
     ⇒ 「依赖可解析」门禁会 fail closed（读不到 ≠ 已发布，#85 口径）。
   - **本次实测的时序**（2026-11-03，取数 03:53Z）：`registry nacos/v0.1.3`+`nacosimpl/v0.1.4`
     @22:50:24Z → `bootstrap` 抬地板 `8635da9` @22:59:25Z → `bootstrap nacos/v0.3.10`+`obs/v0.2.14`
     @23:10:35Z（**tag 快照里已经是** `registry/nacos v0.1.3`+`registry/nacosimpl v0.1.4`）；
     `logx kitexlog/v0.1.8` @22:39:55Z → `kitex` 抬地板 `7cad224` @23:02:57Z
     → `kitex …/kitexotel/v0.7.6` @23:16:26Z（**tag 快照里已经是** `logx/kitexlog v0.1.8`）。
   - ⇒ **依赖链**：`registry/nacos`+`registry/nacosimpl` → `bootstrap/nacos`；
     `logx/kitexlog` → `kitex/…/kitexotel`。
   - ⚠️ **父仓冻结**：本波已发的仓（logx / registry / timertask）**本波不得再发新版**
     ⇒ 否则依赖链要重新算一轮。
   - ⚠️ **「闭合」是局部口径，不是全 org 清零**：它指**本轮这条依赖链上各模块的最新产物互相一致**
     ⇒ **不等于**「全 org 的已发布产物都不带旧地板」。
     - 实测（2026-11-03T04:2xZ）：25 个仓里 **19 个**根仓的**已发布产物**带旧地板、合计 **62 条**
       （零欠账 6 个：core / logx / oekit / pkg / registry / timertask；欠账最多：kitex-examples 10、
       hertz 8、kitex 8、nns-endpoint 6）—— 数字随每次发版滚大 ⇒ **引用须带基线**
       （哪次 run / 哪个 `origin/main` commit —— 同一条纪律见本仓各版本节的「口径」bullet）。
     - ⇒ 该指标是**滚动**的：**每发一次任何依赖的版就新增一批实例**，重发能清掉的是旧批次
       ⇒ **清零后仍会重新出现** ⇒ 全 org 口径的「闭合」**不可达**。
     - ⇒ ⇒ **本波目标应写成「按依赖链推进一轮 + 每轮重扫欠账清单」，不要写「闭合」** ——
       否则清单本身就是个达不成的承诺。
   - ★ **发布的拓扑序 = 叶子仓根模块 → 该仓嵌套模块 → 下游仓根产品**（通用形态；本波实测：
     `registry` 根 v0.5.3 @21:18Z → `registry` 一对 @22:50Z → `bootstrap` 抬地板 @22:59Z
     → `bootstrap` 一对 @23:10Z）。
     - **硬约束 = 「依赖可解析」门禁**（`release_train.py` 的 `check_requires_resolvable`）：
       被 tag 模块 go.mod 里的每条 `github.com/oekit/*` require 必须**已发布**或**本 train 一起发**。
     - ⚠️ **易漏的一条**：门禁用 `parse_go_mod_requires` ⇒ **只认 require、不认 replace**
       ⇒ 嵌套模块 go.mod 里那条**被 `replace ... => ../` 指向本地的父仓根版本，仍然要查**
       ⇒ **父仓根版本没发布就不能发嵌套模块** ⇒ 门禁 **fail closed**（下游消费方同样解析失败：
       Go 忽略依赖里的 replace，#148）。
     - ⇒ ⇒ 这就是「叶子根仓先发」那一跳的**机制来源**，不是经验规则。

| 脚本 | 作用 |
|---|---|
| `scripts/release_train.py` | **单仓发版一条命令**：`--changelog "描述"` 自动补版本节（置顶+预校验）→ PR → 等合并 → 检查（tag 查重/tidy/build/CHANGELOG/gofmt/golangci-lint/**依赖可解析**）→ 按序 tag → Release；train 前自动拉齐 main，要求无在途登记。**依赖可解析**：被 tag 模块的每条 `github.com/oekit/*` require 必须**已发布**或**本 train 一起发版**——嵌套模块的 `replace ... => ../` 只对本仓自身构建有效，Go 忽略依赖里的 replace ⇒ 父仓版本没发布就发嵌套模块，下游消费方会直接解析失败（#148） |
| `scripts/release_post.py` | **发版后一条命令**：目标仓版本行 PR → versions.json → oekit BOM 推荐组合 → audit → 手工待办（`release_train` 之后跑；`--bom-release` 自动打 BOM tag） |
| `scripts/gen_releases.py` | GitHub Release 对账/回填：notes = CHANGELOG 同版本小节 + 该 tag go.mod 的矩阵依赖地板，`releases/latest` 全矩阵可解析 |
| `scripts/gen_matrix_go.py` | 生成 `oekit` 仓 BOM 模块（`go get github.com/oekit/oekit@latest` 一次拉齐兼容组合）；`--check` 为 audit 门禁 |
| `scripts/close_issue.py` | **issue 回执关单**：`close_issue.py <repo>#<N> --tag vX.Y.Z` 发回执（CHANGELOG 小节 + Release 链接）并关闭；tag 不在远端拒绝 |

### 维护轮 / 跨仓文档

| 脚本 | 作用 |
|---|---|
| `scripts/align_floors.py` | **依赖地板对齐（维护轮工具，非常规跟版）**：`--repo data` 单仓按需 / 缺省全矩阵；`--plan`/`--apply`/`--apply --pr`；检出根目录取 `MATRIX_BASE` 或 `--base` |
| `scripts/maintenance_check.py` | **维护轮到期检查**：地板漂移盘点 + 阈值判定；月度 workflow（maintenance.yml）漂移 ≥8 仓自动开 matrix-maintenance issue。**读不到的仓计入 `MAINTENANCE_UNDETERMINED`，不算「零漂移」**（matrix-tools#96） |
| `scripts/doc_wave.py` | **多仓文档波次**：DESC 矩阵表、版本行一改全矩阵脏——`--plan` → `--pr` 批量开 PR → `--merge-all` |
| `scripts/land_generator_change.py` | 跨仓生成器变更的**落地顺序**工具（matrix-tools#34）：生成器与产物必须同版本 ⇒ 先用新生成器落产物、再合生成器 |
| `scripts/pr_flow.py` | 全 org 铺开 main 分支保护（要求 PR + `enforce_admins` + 讨论解决），`--apply` 生效，默认 dry-run |
| `scripts/gen_versions.py` | 生成 `versions.json`（最新 tag / 嵌套模块 / 依赖地板 / breaking 索引）供文档站与 llms.txt |
| `scripts/gen_tables.py` | 重新生成各仓 README「矩阵总览」表 |
| `scripts/gen_diagram.py` | 生成架构图（站点用） |
| `scripts/gen_skill.py` | 渲染 **AI 入口 skill**（`skills/oekit/`：场景选型表 + 红线 + pkg 速查 + 模块索引）与站点《选型速查》指南（`--site`）——同一份数据两种产物；`--check` 比对漂移（audit 同款新鲜度门禁）。硬规则单源在 `scripts/rules.py`（2026-11-12 自 gen_agents BODY 提取，字节级验证） |
| `scripts/gen_deps_doc.py` | 生成依赖文档 |
| `scripts/refresh_versions.py` | 刷新 README 安装行版本号到最新 tag |
| `scripts/refresh_agents_desc.py` | 各仓 AGENTS.md 首行 DESC 行同步到 `matrix.py` 的 DESC 真相源（audit 判红时按提示跑它） |
| `scripts/consumer_probe.py` | 消费方探针：核对各仓**实际 import** 与 go.mod 声明是否相容（按模块前缀比，覆盖任意深度嵌套） |
| `scripts/ekit2oekit.py` | ekit→oekit **import 面**改写（长路径优先，避免前缀互撞）；`--print-rules` 看 23 条种子规则 |
| `scripts/shell_sync.py` | 门面壳同步检查（已并入 audit 硬门禁；含 go.mod 版本地板核对） |

### 一次性 / 危险（默认有闸门，别顺手跑）

| 脚本 | 作用 | 闸门 |
|---|---|---|
| `scripts/gen_agents.py` | 为新仓冷启动生成 AGENTS.md | **只认 `--new-repo <name>`**；全量生成已禁用（会整文件覆写，抹掉人工增量段） |
| `scripts/gen_changelog.py` | 从 tag 重新生成各仓 CHANGELOG.md | **默认只报告**，`--apply` 才写；`--push` 还要再加 `--apply`。⚠️ 实测它会用 tag subject 覆盖 `release_train` 写的详实条目（hertz 会从 10699 字符掉到 1459）⇒ **除非明确要重生成，否则别跑** |

### 自测（CI 跑，证明判据抓得住自己要防的东西）

`align_floors_selftest.py` / `close_issue_selftest.py` / `dependabot_dirs_selftest.py` /
`desc_count_selftest.py` / `gen_matrix_go_selftest.py` / `gen_skill_selftest.py` / `ghsafe_selftest.py` /
`ledger_write_selftest.py` / `maintenance_check_selftest.py` / `release_train_selftest.py` /
`release_train_requires_selftest.py` /
`remote_reads_selftest.py` / `tag_cache_selftest.py` /
`verify_release_units_selftest.py` / `verify_version_line_selftest.py`

### 守卫库（被 audit 调用，不是独立入口）

`scripts/dependabot_dirs.py`（dependabot 目录**两侧**判据：登记了却不存在 /
存在却没登记）/ `scripts/ghsafe.py`（gh 远端读取层：把「这次读到空」与「事实为空」分开）/
`scripts/llms_api_check.py` / `scripts/verify_dead_paths.py` /
`scripts/verify_release_units.py` / `scripts/verify_site_copy.py` /
`scripts/verify_version_line.py`


## 并行会话协作纪律（2026-10-19 起）

所有并行会话共享同一份本地工作区，撞车（checkout -f 冲产物、tag 打在旧内容、
并行 sweep 连推在途改动）的根因是「新会话不知道现场」。纪律收口成四条命令：

1. **开工**：`handoff.py` 看现场 → 动某仓前 `preflight.py <repo>`（BLOCK 必须先处理）
2. **占位**：`inflight.py claim <repo> "在做的事"`（preflight 会查登记；release_train --apply 自带登记管理，发版前**不要**手工 claim 同一仓，否则被自己的登记挡住）
3. **跨仓文档变更**：不手工逐仓 checkout——`doc_wave.py --tables --pr` / `release_post.py` 批量走 PR
4. **收尾**：`inflight.py release <repo>`；发版 = `release_train.py --apply` + `release_post.py`

**发版模型：按需（pull-based，2026-10-19 起）**——默认只发改动的仓：底层依赖发新版后，
**下游不必跟着发新版**；下游改代码消费新 API 时 go get 抬地板、随本仓版本自然发出。

⚠️ **但「下游不必发新版」≠「地板不用管」**（2026-11-02 起，oekit/oekit#102 / #104）：
**工作树**里的依赖地板必须跟到最新，audit 有三道门禁判它——

1. 各仓对**别仓根模块**的地板（`audit.scan_repo_floors`）；
2. 嵌套模块对**父仓**的地板（`audit.check_nested_module_floors`）；
3. 各仓对**别仓嵌套模块**的地板（`audit.scan_repo_nested_floors`）。

⇒ 底层依赖发新版后，下游工作树会被判红 ⇒ 要跑一次 `align_floors.py --apply --pr` 跟上
（全量对齐仍在显式维护轮跑，只是不再是「可做可不做」）。

另一半事实：**已发布产物**的地板滞后**改不了**（tag 不可重推）⇒ 只能给那个模块发新 tag
⇒ 这就是「只发嵌套模块」（父仓本轮不发新版）这条路径的用途。

BOM（github.com/oekit/oekit）是验证过的推荐组合锚点，release_post 自动更新，
`go get oekit@latest` 一次拉齐。

写入型生成器（gen_tables/refresh_versions/gen_changelog/gen_versions/gen_diagram/
gen_matrix_go/gen_skill/regen_readme）一律带干净树守卫：工作树脏直接拒绝执行——生成器产物
必须落在已提交基线上（checkout -f 冲产物事故的硬约束）。
