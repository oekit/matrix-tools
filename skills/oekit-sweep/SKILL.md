---
name: oekit-sweep
description: >-
  oekit issue 存量扫描与清零操作手册。触发场景：盘点 org 的 open issue、做清零战役、
  分类分级战报、处理 audit drift 重报单、merged-pr 欠账（PR 合了没发版）、issue 关闭收尾，
  或并行会话抢仓时分工让道。
---

# oekit issue 清零操作手册

> 工具单源：`matrix-tools/scripts/issue_sweep.py`（只读）与 `close_issue.py`；
> 历史战法教训已程序化进脚本 docstring——遇到与脚本判据冲突的实测，以实测为准并回写脚本。
> 所有命令在 matrix-tools 仓根执行。修单后的发版动作走 **oekit-release** skill。

## 开场盘点（一条命令出分级战报，只读绝不关单）

```bash
python3 scripts/issue_sweep.py                            # org 默认取 matrix.ORG
python3 scripts/issue_sweep.py --org oekit --no-timeline  # 快扫（跳过逐单 timeline）
python3 scripts/issue_sweep.py --org oekit --repo core,hlog
```

工具内建的四条铁律（踩坑程序化）：

1. **双路枚举 + 逐单读定胜负**：gh search 索引有分片延迟会漏单，路径 A（REST 全仓分页）
   与路径 B（search 三排序并集）各自全量，差集逐条单读——404=幽灵单，200=search-only
2. **REST 全程不碰 GraphQL**：大批量扫描 GraphQL 限速先死（TLS 层限流要等恢复，
   403 不是失败信号，别重试风暴）
3. **issue 列表混着 PR**：按 `pull_request` 键剔除，否则计数虚高
4. **空页 ≠ 终点**：上一页满页 ⇒ 本页空按偶发重试；上一页不满 ⇒ 空才是真终点

## 分级打法

1. **重报单先聚类再动手**：巡检类 issue（audit drift / maintenance 提醒）每周重开、标题只差数字
   ——脚本已做归一化 + Jaccard 聚类，近半是重报单，逐单手修是浪费
2. **audit drift 单**：先本地跑 `make audit` 复现再修；漂移红卡全仓合并、无旁路，
   解锁 = 带再生产物的 PR（手改生成物不算）
3. **merged-pr 欠账单**（PR 合了没发版）：走 **oekit-release** 补 tag，版本节点名原 issue
4. **真缺陷/特性**：建分支 → 修 → PR → CI 绿 → merge → `release_train --issue N`
   把 issue 写进版本节（回执按它定位）

## 修单收尾自检（agent 尾巴四类）

- [ ] CI 双检查（Lint & Test / Lint + Test）全绿才 merge；merge 后 tag 落已合 commit
- [ ] issue 关闭走 `close_issue.py`（带定位与验证，不裸 `gh issue close`）
- [ ] CHANGELOG 版本节点名了 issue 号（`--issue` 参数，不是手写漏掉）
- [ ] 涉及 API 变更：apidiff / BREAKING 标注过门禁；门禁误报先验证是误报再豁免（豁免理由留痕）

## 并发会话车道

- 动手前查远端在途：`gh pr list` + `git ls-remote --tags origin`——别人已收编的仓**让道**，
  不重复修（实测 jumpserver 三仓被并发会话收编）
- 同仓并行用 worktree 隔离；**有子代理活跃的仓禁 `reset --hard`**
- 大战役节奏：先扫全量出总账 → 分层分批修 → 每批结束 bump 地板（`align_floors.py --apply`）

## 完成判定

- 盘点重跑 `issue_sweep.py`：open 归零（或剩单全部有 owner/在途 PR）
- 每仓欠账（发版/同步/BOM）为零，audit 本地全绿
