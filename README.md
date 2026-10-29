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

46 个脚本按**什么时候跑**分四组。新会话先看「日常」那组；「一次性」那组
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

### 发版链（顺序不可反）

| 脚本 | 作用 |
|---|---|
| `scripts/release_train.py` | **单仓发版一条命令**：`--changelog "描述"` 自动补版本节（置顶+预校验）→ PR → 等合并 → 检查（tag 查重/tidy/build/CHANGELOG/gofmt/golangci-lint）→ 按序 tag → Release；train 前自动拉齐 main，要求无在途登记 |
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
`desc_count_selftest.py` / `gen_matrix_go_selftest.py` / `ghsafe_selftest.py` /
`maintenance_check_selftest.py` / `release_train_selftest.py` /
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

**发版模型：按需（pull-based，2026-10-19 起）**——默认只发改动的仓。底层依赖发新版后下游
地板不动、不强制跟版；下游改代码消费新 API 时 go get 抬地板、随本仓版本自然发出。跨仓
地板漂移不是漂移（audit 不检查）；全量对齐（align_floors.py）只在显式维护轮跑。BOM
（github.com/oekit/oekit）是验证过的推荐组合锚点，release_post 自动更新，`go get oekit@latest`
一次拉齐。

写入型生成器（gen_tables/refresh_versions/gen_changelog/gen_versions/gen_diagram/
gen_matrix_go/regen_readme）一律带干净树守卫：工作树脏直接拒绝执行——生成器产物
必须落在已提交基线上（checkout -f 冲产物事故的硬约束）。
