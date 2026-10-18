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
| `scripts/gen_tables.py` | 重新生成各仓 README「矩阵总览」表 |
| `scripts/refresh_versions.py` | 刷新 README 安装行版本号到最新 tag |
| `scripts/pr_flow.py` | 全 org 铺开 main 分支保护（要求 PR + `enforce_admins` + 讨论解决），`--apply` 生效，默认 dry-run |
| `scripts/gen_versions.py` | 生成 `versions.json`（最新 tag / 嵌套模块 / 依赖地板 / breaking 索引）供文档站与 llms.txt |
| `scripts/ekit2oekit.py` | ekit→oekit **import 面**改写（长路径优先，避免前缀互撞）；`--print-rules` 看 23 条种子规则 |
| `scripts/audit.py` / `gen_agents.py` / `gen_changelog.py` / `shell_sync.py` | 文档审计（含嵌套地板门禁）/ AGENTS 生成 / CHANGELOG / shell 同步 |
| `scripts/release_train.py` | 主模块 + 嵌套模块同 train 按序打 tag（查重/tidy/build/CHANGELOG 四道前置，matrix-tools#6） |
