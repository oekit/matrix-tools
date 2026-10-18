#!/usr/bin/env python3
"""为 21 个活跃仓生成 oekit 适配版 AGENTS.md（共享规范 + 本仓信息）。

⚠️ 重跑会整文件覆写各仓 AGENTS.md——各仓文件生成后已在人工维护演进
（如 data 的 cached/twolevel 硬规则段、门面壳同步规则等，本工具模板不含），
**仅在新增仓冷启动或明确决定全量重生成时使用**；日常修订请直接改各仓文件。
"""
import os
import pathlib
import re
import subprocess

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from matrix import MODULES, DESC  # 一句话定位唯一事实源在 matrix.py
BASE = pathlib.Path(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
def repo_path(r): return BASE / r

# DESC 本地副本已删除（2026-10-16 发现漂移：istio/gorm/redis 死键 + 各仓口径过期）

BODY = """
## 硬规则速览（全矩阵适用）

### 分层与依赖
- 依赖严格自底向上，禁止反向依赖；本仓的矩阵内依赖见 README「矩阵总览」表
- 微工具集统一在 `github.com/oekit/pkg`；本仓新增基础工具前先确认 pkg 里没有
- `pkg` 是**零第三方依赖仓**（go.mod 零 require，仅标准库，CI 强制）——需要三方能力的基础工具拆独立仓（先例：kitx）

### 错误处理
- 一律使用 `github.com/oekit/pkg/errors`：`errors.Wrap(err, "ctx", kv...)`（nil-safe）、
  `errors.Cause`；业务错误用 BizError（BizCode 与 HTTPCode 双轨，命名工厂 NewNotFoundError 等）
- **类型同一性红线**：同一进程内只允许一套 errors 包。混用旧路径会把 BizError 的 As/Is
  判定打断（Not Found 变 500）
- handler 层统一 `mw.Fail(ctx, c, err)` 出口；所有 error 禁止 `_` 忽略

### 日志
- 门面 `github.com/oekit/logx`：业务服务 `logx.WithService("svc")`；
  **SDK/基础包必须 `logx.Delegating("组件名")` 懒解析**（禁止 Default()/WithService 声明包级 logger）
- kv 结构化风格为默认；`Ctx*` 方法自动携带 trace_id；禁止 printf 风格 `logx.Infof` 作为主要手段
- 后端经 `logx.SetDefault` 替换；`LOG_LEVEL` 环境变量统一生效

### 并发安全
- 禁止裸 `go func`：用 `github.com/oekit/concurrency/async` 的 GoSafe 系列（panic 被 recover）
- 共享状态 mutex / atomic 保护；包级全局可变状态用 `atomic.Pointer[T]`

### 生命周期
- 组件 Close 统一 `Close(ctx) error` 签名 + sync.Once 幂等
- 应用骨架用 `github.com/oekit/bootstrap/bootstrap.Run`；**userFn 返回即服务结束，
  长驻服务必须 `<-app.Context().Done()` 阻塞**

### Option 与命名
- 函数选项模式 `XxxOption`（禁止裸 `Option`，多包 co-import 冲突）
- 导出符号 PascalCase + godoc 注释；包文档放 doc.go

### 版本与发版
- SemVer；0.x 阶段 minor 允许 breaking
- **main 受分支保护，改动一律走 PR，管理员也不例外**（`enforce_admins=true`，
  直推会被 `GH006 Changes must be made through a pull request` 拒掉）
- 发版流程：建分支 → 提交 → 开 PR → CI（`Lint & Test / Lint` + `Lint & Test / Test`）
  绿 → 合入 main → **合入之后**再打 annotated tag 推远端
- tag 不走 PR（`protect-release-tags` ruleset 管的是不可删/不可改，不是审批）；
  tag 必须落在已合入 main 的 commit 上，否则 `go get` 拉到的是没进过 CI 的代码
- module 路径 = `github.com/oekit/<name>`；**改 module 路径必须同步**：
  全部 import、go.mod、README 安装行、logx caller 启发式（若涉及 logx）

### 文档
- 本仓 README 的「矩阵总览」表由脚本生成，结构变化后需重新生成
- 历史完整规范见 ekit 单体仓（git.enjoye.top/enjoydream/ekit，冻结维护）的 AGENTS.md 与 docs/

## 本仓范围
"""

def pkg_list(repo):
    d = repo_path(repo)
    pkgs = []
    for p in sorted(d.rglob("*.go")):
        if ".git" in p.parts or "_test.go" in p.name or "/testdata/" in str(p):
            continue
        rel = p.parent.relative_to(d)
        if str(rel) not in pkgs:
            pkgs.append(str(rel))
    return "、".join(f"`{p}/`" for p in sorted(set(pkgs)))

if __name__ != "__main__":
    raise SystemExit("gen_agents 会整文件覆写各仓 AGENTS.md——请显式运行，不要 import")

count = 0
for repo in MODULES:
    d = repo_path(repo)
    tags = subprocess.run(["git", "-C", str(d), "tag", "-l", "--sort=-v:refname"],
                          capture_output=True, text=True).stdout.split()
    lt = tags[0] if tags else "v0.1.0"
    content = f"""# AGENTS.md — oekit/{repo}

> {DESC[repo]}
> 当前版本：`{lt}` · module：`github.com/oekit/{repo}` · org：[oekit = Open Easy Kit](https://oekit.github.io)

## 本仓包含
{pkg_list(repo)}
{BODY}
"""
    (d / "AGENTS.md").write_text(content)
    count += 1
    print(f"✓ oekit/{repo}/AGENTS.md", flush=True)
print(f"=== {count} 份 AGENTS.md ===")
