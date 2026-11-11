---
name: oekit-release
description: >-
  oekit 矩阵发版/tag/CHANGELOG/依赖地板 bump 操作手册。触发场景：给 oekit 各仓发新版、打 tag、
  补 CHANGELOG 版本节、抬依赖地板、发版后同步站点/BOM/矩阵表，或排查发版门禁失败、tag 冲突、
  版本被抢发。仅适用 oekit org；oagentkit/yi-nology 各有自己的发版工具。
---

# oekit 发版操作手册

> 检查面与门禁的权威定义在 `matrix-tools/scripts/release_train.py` 的 docstring——本 skill 是
> 提炼不是替代，两者冲突以脚本为准。所有命令在 matrix-tools 仓根执行。
> 「用 oekit 包写代码」类问题用 **oekit** skill；issue 清零用 **oekit-sweep**。

## 一条命令流（单仓）

```bash
python3 scripts/release_train.py <repo> main=vX.Y.Z --apply --changelog "<版本节内容>"
# 关联 issue 必须点名进版本节（回执按 issue 号在版本节定位；Release 正文它读不到）：
python3 scripts/release_train.py <repo> main=vX.Y.Z --apply --changelog "修 xxx" --issue 123
```

- 缺省 dry-run 只跑检查；`--changelog` 缺小节时 dry-run 只提示不落 PR
- 嵌套同 train（父模块地板须已就位）：
  `python3 scripts/release_train.py bootstrap main=v0.6.4 nacos=v0.3.4 obs=v0.2.9 --apply --changelog "…"`

## 三纪律（幽灵 tag 事故实录）

1. **一仓一命令**：绝不在一条命令里给多个仓打 tag
2. **pwd 验目录**：每次执行前确认当前目录就是要发的仓
3. **merge 拒合即止步**：PR 被拒/冲突不要绕过，解决后重走流程——tag 只打在已合入 main 的 commit 上
   （否则 `go get` 拉到没进过 CI 的代码）

## 检查面（全过才打 tag，任一失败原子中止）

1. ls-remote 同名 tag 查重（重推 = 代理/sumdb 集体中毒）
2. `go mod tidy -diff` 干净（地板指向已删 tag 直接红）
3. `go build`（GOWORK=off，外部用户视角）
4. CHANGELOG 含本 tag 版本节
5. gofmt + golangci-lint（本地可用时）
6. apidiff 相邻 tag：导出面不兼容变更而版本节无 **BREAKING** 标注 ⇒ 红；
   `--skip-apidiff "理由"` 显式逃生（理由留痕）
7. 依赖可解析：oekit require 必须已发布或本 train 一起发（嵌套 replace 对下游无效）
8. 依赖地板 = 该依赖当前最新已发布版本（旧地板会被焊进已发布产物，不可重推只能欠一版）

## 版本号选择

- SemVer；0.x 阶段 minor 允许 breaking
- apidiff 有不兼容 ⇒ minor/breaking + BREAKING 标注，不许 patch 偷渡
- 版本号被并行会话抢发 ⇒ **改发下一版**，绝不重推（tag 不可删改 ruleset）

## 地板跑步机（发版后的连带义务）

新 tag 发布后，旧地板即过期：

```bash
python3 scripts/align_floors.py --apply   # 一条命令追平全矩阵依赖地板
```

- 分层顺序：基础库（pkg/concurrency/logx/core/kitx/metrics）→ 业务层
- 脏 go.mod 要收尾：tidy -diff 干净才算完

## 发版后同步（不跑就是欠账，audit 周巡检会抓）

1. 站点 versions.json：`make fix-versions`
2. 各仓 README 矩阵表：`make fix-tables`
3. 伞仓 BOM：重跑 `gen_matrix_go.py` → oekit 仓发新 tag（audit 有新鲜度门禁）
4. 硬规则/场景表若有变更：`make gen-skill`（AI 入口 skill 同步）

## 撞车与并发会话

- release_train 自带 preflight：工作树不干净 / main 落后自动 ff / 分叉即拒 / 被并行会话登记占用即拒
- 动手前先查远端在途：`gh pr list` + `git ls-remote --tags origin`
- 本地多会话用 worktree 隔离车道；**有子代理活跃的仓禁 `reset --hard`**

## 跨 org 提醒

- oagentkit / yi-nology **有自己的发版工具**（oagentkit matrix 的 release.py：prewrite→tag→sync-matrix），
  不要把 release_train 用到别的 org
- CHANGELOG replace 锚文本各仓形态不统一——replace 后必须验证 + tag 快照回验
