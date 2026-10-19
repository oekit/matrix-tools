# matrix-tools

oekit 模块矩阵的自动化工具：生成器 + 全量审计 + 定时巡检。

## 本地用法

```bash
# 全量审计（module 行/残留引用/门面/AGENTS/CHANGELOG/安装行版本/矩阵表新鲜度/站点）
python3 scripts/audit.py

# 修复类生成器（--push 直接提交推送）
python3 scripts/gen_tables.py            # 各仓 README 矩阵总览表
python3 scripts/release_train.py bootstrap main=v0.6.2 nacos=v0.3.2  # 嵌套发布 train（dry-run，--apply 打 tag 推送）
python3 scripts/gen_agents.py            # 各仓 AGENTS.md
python3 scripts/gen_changelog.py --push  # 各仓 CHANGELOG
python3 scripts/refresh_versions.py --push  # README 安装行对齐最新 tag
python3 scripts/shell_sync.py            # 门面壳同步检查（已并入 audit 硬门禁；含 go.mod 版本地板核对）
python3 scripts/gen_releases.py          # GitHub Release 对账/回填：每 tag 一个 Release（--apply 创建，已并入 audit 门禁）
python3 scripts/gen_matrix_go.py         # oekit 仓 BOM 模块生成（go.mod 地板 + matrix.go 快照；--check 为 audit 门禁）
```

`MATRIX_BASE` 环境变量指定矩阵检出根目录（默认 `~/my_project/ekit-split`；
支持 `<base>/<name>`、`<base>/ekit-<name>`、`<base>/repos/<name>` 三种布局）。

## 自动巡检

`.github/workflows/audit.yml`：每周一 03:00 UTC + 手动触发。
Actions 环境下自动批量克隆全仓（`repos/<name>` 布局，tag 走 ls-remote），
发现漂移自动创建/更新 `matrix-drift` 标签的 issue 并 @ 维护者。

## 规范

- 模块清单与描述的唯一事实源在本仓 `matrix.py` 的 `MODULES`/`DESC`
- 新增模块：改 `matrix.py` → 跑 `gen_tables.py --push` → 跑 `audit.py` 确认全绿
- 发版后：跑 `refresh_versions.py --push` 对齐各仓安装行
- **合入方式：走 PR，不直推 main**——全 org main 已开 `enforce_admins`，直推会被
  `GH006 Changes must be made through a pull request` 拒掉；tag 打在合入 main 之后

## 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/inflight.py` | **在途登记**（并行会话互斥）：`claim <repo> 说明` / `release <repo>` / `list`；PID+4h TTL 判定持有 |
| `scripts/handoff.py` | **现场交接清单**：新会话开工先跑——在途登记 / 非 main·脏工作树 / org open PR 一屏可见 |
| `scripts/preflight.py` | **开工前检查**：`preflight.py <repo>`——登记占用/脏树/分叉即 BLOCK（exit 1），落后远端/在途 PR 给 WARN |
| `scripts/doc_wave.py` | **多仓文档波次**：DESC 矩阵表、版本行一改全矩阵脏——`--plan` → `--pr` 批量开 PR → `--merge-all` |
| `scripts/release_post.py` | **发版后一条命令**：版本行 PR → versions.json 重生成 → audit → 手工待办清单（`release_train` 之后跑） |
| `scripts/align_floors.py` | **依赖地板不动点对齐**：全仓 go.mod 的 oekit 依赖地板对齐最新 tag（`--plan`/`--apply`/`--apply --pr`） |
| `scripts/close_issue.py` | **issue 回执关单**：`close_issue.py <repo>#<N> --tag vX.Y.Z` 发回执（CHANGELOG 小节 + Release 链接）并关闭；tag 不在远端拒绝 |
| `scripts/prkit.py` | PR 落地共用件（doc_wave/release_post/align_floors 内部复用）：sync main → 分支 → 提交 → 开 PR → update-branch/auto 合并 |
| `scripts/gen_tables.py` | 重新生成各仓 README「矩阵总览」表 |
| `scripts/refresh_versions.py` | 刷新 README 安装行版本号到最新 tag |
| `scripts/pr_flow.py` | 全 org 铺开 main 分支保护（要求 PR + `enforce_admins` + 讨论解决），`--apply` 生效，默认 dry-run |
| `scripts/gen_versions.py` | 生成 `versions.json`（最新 tag / 嵌套模块 / 依赖地板 / breaking 索引）供文档站与 llms.txt |
| `scripts/ekit2oekit.py` | ekit→oekit **import 面**改写（长路径优先，避免前缀互撞）；`--print-rules` 看 23 条种子规则 |
| `scripts/audit.py` / `gen_agents.py` / `gen_changelog.py` / `shell_sync.py` | 文档审计（含嵌套地板门禁）/ AGENTS 生成 / CHANGELOG / shell 同步 |
| `scripts/release_train.py` | 主模块 + 嵌套模块同 train 按序打 tag（查重/tidy/build/CHANGELOG 四道前置，matrix-tools#6）；推完自动建 GitHub Release（.github#8）；--apply 要求 main+干净树+无在途登记 |
| `scripts/gen_releases.py` | GitHub Release 对账/回填：notes = CHANGELOG 同版本小节 + 该 tag go.mod 的矩阵依赖地板，`releases/latest` 全矩阵可解析 |
| `scripts/gen_matrix_go.py` | 生成 `oekit` 仓 BOM 模块（`go get github.com/oekit/oekit@latest` 一次拉齐兼容组合） |

## 并行会话协作纪律（2026-10-19 起）

所有并行会话共享同一份本地工作区，撞车（checkout -f 冲产物、tag 打在旧内容、
并行 sweep 连推在途改动）的根因是「新会话不知道现场」。纪律收口成四条命令：

1. **开工**：`handoff.py` 看现场 → 动某仓前 `preflight.py <repo>`（BLOCK 必须先处理）
2. **占位**：`inflight.py claim <repo> "在做的事"`（preflight 与 release_train --apply 都会查登记）
3. **跨仓文档变更**：不手工逐仓 checkout——`doc_wave.py --tables --pr` / `release_post.py` 批量走 PR
4. **收尾**：`inflight.py release <repo>`；发版 = `release_train.py --apply` + `release_post.py`

写入型生成器（gen_tables/refresh_versions/gen_changelog/gen_versions/gen_diagram/
gen_matrix_go/regen_readme）一律带干净树守卫：工作树脏直接拒绝执行——生成器产物
必须落在已提交基线上（checkout -f 冲产物事故的硬约束）。
