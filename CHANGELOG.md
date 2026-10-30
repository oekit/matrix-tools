# Changelog — oekit/matrix-tools

oekit 模块矩阵的自动化工具：生成器 + 全量审计 + 定时巡检。
本仓此前无版本化（工具仓，随用随改）；v0.1.0 起建立版本记录，
以便回执类流程能指向「哪个版本落地了哪次变更」。

## [v0.1.24] — 2026-10-30

### fix(tools)：父仓地板变了，嵌套模块的 go.sum 必须跟着 tidy——否则 CI 必红而工具说「已对齐」

**现象**：kitex#65（地板对齐 PR）的 CI 在 tidy 门禁**连续两次**判红：

```
diff current/go.sum tidy/go.sum
-github.com/oekit/bootstrap v0.6.5 h1:...
+github.com/oekit/bootstrap v0.6.6 h1:...
```

而工具自报「✓ kitex 已写入并 tidy」。

**根因**：嵌套模块对父仓有 `replace github.com/oekit/kitex => ../../../..`
⇒ 父仓 go.mod 的 require 一变，**子模块的依赖图随之变化**：即便子模块
**自己不直接 require** 那个依赖（kitexotel 不 require bootstrap），它的 go.sum
里也会换掉那个依赖的哈希（经父仓传递进来）。

⇒ 旧实现只对「计划里有 ups 的 go.mod」跑 tidy：kitexotel **压根不在计划里**
⇒ 它的 go.sum 永远滞后于父仓 ⇒ CI 的 `go mod tidy -diff` 门禁必红，
而工具自报「已写入并 tidy」——又一次**假绿**。

**修法**：抽出 `apply_repo`，改成
1. **先改完全部 go.mod**（tidy 要用改后的父仓内容解析，反序会算出旧依赖图）；
2. 再对仓内**每个** go.mod（根 + 全部嵌套模块，含没有 ups 的）跑 tidy。

**实测**：修好后 kitex 的改动从 2 个文件变成 3 个——根 go.mod/go.sum **+
observability/tracing/instrumentation/kitexotel/go.sum**（之前漏的就是它）；
kitex / kitexotel / istio 三个模块的 `go mod tidy -diff` 均干净。

**变异验证**（自测新增契约断言，两向都验）：
- tidy 循环退回「只遍历 plans」⇒ 自测红 ✅
- 顺序反了（先 tidy 后改 go.mod）⇒ 自测红 ✅

> ⚠️ 这是 align_floors 的**第三个**同族假绿坑（#129 漏扫深层嵌套模块、
> #130 抬不动 `// indirect` 行、本次嵌套模块 go.sum 不跟随父仓）。共同形状：
> **判定（plan）与执行（apply）共用同一套不完整的规则** ⇒ 两边一起漏 ⇒
> 工具报成功而实际没改。

### feat(tools)：巡检链加 flaky 扫描——CI 的 -count=1 永远看不见的那类缺陷

**背景**：矩阵 CI 的 test-script 默认 `go test -race -count=1` ⇒ **只跑一轮**。
而有一类缺陷**第一轮恒绿、第二轮起才红** ⇒ **CI 永远不会报**。2026-10-30
实测三例，全是同一形态（**进程级全局状态在测试间/轮次间未隔离**）：

- **logx**：caller 跳过注册表只增不删 ⇒ 用例自己注册的探针文件留在全局 ⇒
  第二轮起探针帧被自己跳过（logx#52 已修）
- **concurrency**：GetBreaker 的 sharedBreakers 全局 ⇒ 冲突回调只在「首次
  创建」时有对比对象 ⇒ 第二轮起指纹相同 ⇒ 回调不触发（concurrency#119 已修）
- **redisx**：兜底期断言把**后台探活**那次 EVAL 也算进业务计数 ⇒ 计数 +1
  ⇒ 断言误判（redisx#67 已修，实测 100 轮红 14 次）

**修法**：新增 `scripts/flake_scan.py`（全矩阵并发跑 `-race -count=N`，收集
`--- FAIL` 用例名），并接进 `maintenance.yml`：
- 扫描**不阻塞**维护轮主流程（它是"发现"不是"判红"）⇒ `continue-on-error`
  + 吞退出码，结果写进 `flake-report.txt` 供 issue 正文引用；
- 「关单」与「开单」的条件都加上「且无 flaky 用例」——**地板零漂移 ≠ 健康**；
- issue 正文新增独立一段，点明这类缺陷的特征与典型成因。

### fix(tools)：flaky 扫描在「没 Go」/「编译不过」时会静默失效

**背景**：上面把扫描接进巡检链后发现那个 workflow **只装了 Python、没装 Go**
⇒ 扫描步骤里的 `go test` 根本跑不起来。

**为什么这比报错危险**：扫描工具当时只按 `--- FAIL` 行收集失败用例
⇒ go 命令不存在 ⇒ 每个仓的 go test 立刻失败、输出里**没有** `--- FAIL` 行
⇒ 工具报「✓ 全矩阵无失败用例」⇒ **看起来扫过了，实际一个测试都没跑**。
（与 align_floors 的三个假绿同族：判据没覆盖真实失败面。）

**修法（三处）**：
1. `maintenance.yml` 加 `setup-go`（Go 地板口径：业务层 1.24/1.25/1.26 均可，取 1.26）；
2. `flake_scan.py` 启动即检查 go 是否可用（用 `shutil.which`——用
   `subprocess.run(["go", ...])` 会在 go 不存在时抛 FileNotFoundError ⇒
   检测本身崩掉、报 traceback，更看不出是环境问题），**不可用 ⇒ 退出码 2**
   并明说「不是『无失败用例』」；
3. **构建/编译失败与用例失败分开报**：`go test` 编译不过时退出码非 0 但
   没有 `--- FAIL` 行 ⇒ 旧逻辑把它记成「该仓无失败」⇒ 同样是把
   「没测成」说成「测过了」。现在单独报 `build failed` 并让整体退出码非 0。

## [v0.1.23] — 2026-10-30

### fix(tools)：gen_versions 的干净守卫收窄到文件级——别人的在途改动不该卡死发版链

**现象（2026-10-30 实测）**：`release_post.py` 的站点步骤失败在

```
gen_versions 失败：gen_versions: 工作树不干净，拒绝执行：oekit.github.io
```

而站点仓当时**只有一行未提交改动**，是 `docs/public/llms.txt`——与本生成器
**要写的文件毫无关系**（共享工作区里并行会话的在途产物）。

⇒ 整条发版链（版本行 PR → versions.json → BOM → audit）断在站点这一步，
而 git 的脏树状态本该只由**在途的人**处理。

**根因**：`gen_versions` 用的是 `ensure_clean_all([SITE])` —— 判**整棵树**。
但它只写 `docs/public/versions.json` 一个文件 ⇒ 别的文件脏，与它无关。

**修法**：判据收窄成**文件级**（新增 `matrix.ensure_file_clean`）——只看
「我自己要写的那个文件」有没有未提交改动：

- 别人的在途改动（llms.txt 等）⇒ **放行**（本生成器不碰它）；
- 我要写的 versions.json 自己有未提交改动 ⇒ **挡住**（生成会静默覆盖别人的内容
  ⇒ 那才是真事故）。

守卫**没有整体放松**：放松的是「无关文件的脏」，守住的是「自己要写的文件」。

**顺带**：`release_post` 的站点步骤原来静默 `git checkout --` 掉 versions.json
的未提交改动 ⇒ 现在会**说出来**。⚠️ 只重置 versions.json 这**一个生成物**——
站点的 llms.txt 是**手写文件**（`verify_site_copy.py` 明说它没有生成器），
它的未提交改动是别人的在途工作，**不许重置**。

**自测**：`toolchain_selftest.py` 加用例 4（临时 git 仓造两个场景）——
场景 A：别的文件脏 ⇒ 必须放行；场景 B：自己要写的文件脏 ⇒ 必须挡住。
**变异验证**：把判据退回整树检查 ⇒ 用例 4a 转红（实测退出码 1）。

## [v0.1.22] — 2026-10-30

### fix(tools)：merge_pr 的回退会在 checks 未绿时立即合——绕过「PR 必须 CI 全绿」

`merge_pr` 的 auto-merge 回退是**无条件立即合**：

```python
r = sh(["gh", "pr", "merge", ..., "--auto"], ...)   # 失败
r2 = sh(["gh", "pr", "merge", ...], ...)            # ← 无条件立即合
```

⇒ 在**没有分支保护**（或保护里没挂 required checks）的仓上，那条回退等于
「不等 CI 就合」⇒ 破坏本矩阵「PR 必须 CI 全绿才能合」的约定；而且它**不报错**
（命令成功退出）⇒ 事后才发现时已经合进去了。

修法：判据提成纯函数 `may_merge_without_checks(merge_state)`——只有
`mergeStateStatus == CLEAN`（**没有任何 required check 挂起**）才允许立即合；
`BLOCKED` / `BEHIND` / `DIRTY` / **状态读不到** ⇒ 明确返回「等 CI 绿后再合」，
合并动作留给人或下一次调用。「读不到 ⇒ 不许合」是刻意的：**判不了 ≠ 没问题**。

### fix(tools)：gen_changelog 从来没跑起来过，修好后又发现它没有闸门

**① 它从来没跑起来过。** 旧版第 3 行是

```python
sys.path.insert(0, str(pathlib.Path(__file__).rsplit("/", 2)[0]))
```

`pathlib.Path` **没有** `rsplit`（那是 `str` 的方法）⇒ 一执行就 AttributeError。
全仓 46 个脚本里只有它这么写（其余都是 `str(__file__).rsplit`）⇒ 而 README
一直把它列在「可用工具」里 ⇒ 照着跑只会拿到 AttributeError。

**② 跑起来也不能没有闸门。** 旧版一跑就把全矩阵 24 个仓的 CHANGELOG.md
**整文件覆写**，且覆写出来的是「每个 tag 的 subject 一行」⇒ **release_train
写的详实条目被替换掉**。实测损失：

| 仓 | 现有 | 生成后 | 丢了多少 |
|---|---|---|---|
| hertz | 218 行 / 10699 字符 | 100 行 / 1459 字符 | **87%** |
| kitex | 173 行 / 7015 字符 | 108 行 / 1737 字符 | 75% |
| pkg | 382 行 | 172 行 | 详实的 CLI 设计说明被换成 tag 首行 |

而且三个仓现在有**三种不同的抬头**（pkg 没有抬头、logx 是 ekit v0.34.0、
bootstrap 是 Keep a Changelog）⇒ 旧版一跑就统一成第四种，还给 pkg 补上一个
它没有的抬头。

修法：跟矩阵既有约定对齐（align_floors / doc_wave / pr_flow 都是「默认只报告，
`--apply` 才落地」）——缺省**只报告**哪些仓会被改、会不会丢内容、丢多少；
`--apply` 才真写；`--push` 还要求 `--apply`；顺带把可执行部分收进 `main()`，
模块可被 import。「丢内容」的判据用**字符数**而不是行数——详实条目是长句，
行数少不代表没丢。

### refactor(tools)：local_dir 上提共享层——两份逐字相同的实现

`handoff.py` 与 `preflight.py` 各有一份**逐字相同**的 `local_dir` 实现。
重复的代价不是「多几行」——是**改目录名规则时只改一处**，另一处悄悄落后。
事实层的东西（路径解析）就该有一份 ⇒ 上提到 `matrix.py`（与 `repo_dir` 并列）。

### test(tools)：工具链闸门自测

新增 `scripts/toolchain_selftest.py`，钉住本轮的三道闸门：

1. `may_merge_without_checks`：只有 CLEAN 放行；`BLOCKED` / `BEHIND` /
   `DIRTY` / **状态读不到** 都必须挡；
2. `changelog_write_allowed`：`--push` 不带 `--apply` 必须被拦；另外
   **import 它本身**就是一道判据——旧版那行崩溃在 import 期就炸，import 成功
   即证明崩溃行没了（不扫源码：docstring 里本就该写清这个坑的历史，扫源码会被
   文档误伤——2026-10-30 实测先写成扫源码，结果被自己的文档字符串判红）；
3. `local_dir`：行为（认 `<base>/<name>`、兼容 `ekit-<name>`、找不到返回
   None）+ **源码级**断言（handoff.py / preflight.py 里不许再自己定义一份）。

三处**变异验证**（改坏判据 ⇒ 自测必须转红，实测均转红、退出码 1）：
放宽 merge 判据 ⇒ 用例 1b 转红；去掉 `push and not apply_` ⇒ 用例 2b 转红；
在 handoff.py 塞回一份 local_dir 定义 ⇒ 用例 3b 转红。

### docs(tools)：脚本表补全到 46 个并分类

脚本表只列了 22/46 ⇒ 另外 23 个（含全部 `verify_*` 守卫与 12 个自测）在文档里
查不到，新人不知道它们是干什么的、该不该跑。现在按**什么时候跑**分四组：
日常 / 发版链 / 维护轮·跨仓文档 / 一次性·危险，另列自测与守卫库。
同时把「本地用法」对齐新闸门（原来写着 `gen_changelog.py --push`，现在会被拒）。

## [v0.1.21] — 2026-10-29

### fix(tools)：align_floors 看不见嵌套模块——第 4 层的 kitexotel 被漏扫

`align_floors.py` 扫 go.mod 用的是 `glob("*/go.mod")`，只覆盖**一层**子目录。
而嵌套模块的路径深度并不固定：`bootstrap/nacos`、`logx/sink` 是一层，
`kitex/observability/tracing/instrumentation/kitexotel` 在**第四层**。

⇒ 深层嵌套模块被漏 ⇒ 父仓地板抬升后，子模块 go.mod 仍指向旧版本。
子模块对父仓有 `replace ... => ../../..`，父仓一动，子模块的间接依赖集合
随之变化 ⇒ CI 的 `go mod tidy -diff` 门禁必然判红（kitex#58 的真实成因）。

修法：改用 `rglob` 递归扫描，排除 `.git` / `vendor` / `node_modules` /
`testdata`（vendor 下的 go.mod 属于第三方依赖，被扫到会写出错误的 require 行）。
新增 `align_floors_selftest.py` 钉住「深层嵌套模块必须扫得到、vendor 必须排除」，
并接入 audit.yml 的 selftest job。

### fix(tools)：align_floors 抬不动 indirect 行——工具说「零漂移」实际没对齐

`plan_repo` 与 `apply_edits` 用的 require 行正则**以 `$` 收尾、不匹配行尾的
`// indirect` 标记**。go.mod 的间接依赖行形如：

```
	github.com/oekit/concurrency v0.23.0 // indirect
```

而矩阵里**嵌套模块**的依赖几乎全是 indirect 形态（bootstrap/nacos、
bootstrap/obs、timertask/redis …）⇒ 嵌套模块的地板既扫不到、也抬不动。

**为什么这比报错更危险**：plan 阶段用同一个正则 ⇒ 嵌套模块根本不会进入计划
⇒ 工具自报「✓ 全矩阵地板零漂移」而实际没对齐 ⇒ **假绿**一路走到 CI 门禁
才炸（bootstrap#50 就是这么红的）。

修法：新增 `FLOOR_REQ_RE`（容忍 ` // indirect` 标记），plan 与 apply 两侧都用它；
`apply_edits` 改为按整行计数、逐行替换（同一模块可能同时有直接依赖行与 indirect
行，两行都要抬），并加断言：计划里有的行在文件里找不到 ⇒ 直接报错，不再静默跳过。

不改 `matrix.OEKIT_REQ_RE`：它的语义是「这个仓**直接**依赖哪些矩阵模块」
（dep_matrix 的依赖矩阵在用），地板扫描是另一件事——「所有 oekit 依赖行（含间接）」。

自测扩了两条断言，两向变异均验：正则退回 ⇒ 自测报「indirect 依赖行未扫到」；
apply_edits 退回 ⇒ 自测触发断言「计划与实际不一致」。

### 修好后的效果

工具第一次看见真实的全矩阵地板漂移——修这两处之前，四轮维护轮共 49 个地板 PR
里有一批**只抬了根模块、漏了嵌套模块**，被假绿掩盖；修好后首次暴露 5 仓真实漂移
（etcdx、httpx、nns-endpoint、bootstrap、kitex-examples）。

## [v0.1.20] — 2026-10-27

### fix(tools)：consumer 探针判据粒度对齐——kitex 父仓被判假阳性，深层嵌套模块排除失效

2026-10-27 跑消费面探针时，kitex 父仓被判出两条「父←子隔离」违规：

```
kitex: go.mod 仍直接 require 子模块 kitex/observability/tracing/instrumentation/kitexotel 扛的重依赖 […]
kitex: go.mod 仍直接 require 子模块 kitex/istio 扛的重依赖 […]
```

但 kitex 的 `go.mod` 是**对的** —— 父仓自己就 import 了 kitex 自己的包。

#### 根因（两处，都只在真实 org 数据上暴露，夹具看不出）

**一、判据粒度错配（假阳性）**

源码里写的是**子包路径** `github.com/cloudwego/kitex/server`，
go.mod 里 require 的是**模块路径** `github.com/cloudwego/kitex`。
判据拿**全等**去比这两个字符串 ⇒ 永远匹配不上 ⇒ 判定「父仓没 import 它」⇒ 报出假阳性。

取证：kitex 父仓根级文件（`bootstrap.go` / `config.go` / `retry.go` / `chainmw/` /
`kitexmeta/` / `nns/` / `registry/` / `internal/` 等共 80 处）确实 import 了
`github.com/cloudwego/kitex/server`。

**二、嵌套模块排除只覆盖第一层（假阴性）**

排除逻辑只看 `rel.parts[0]/go.mod`。kitexotel 在
`observability/tracing/instrumentation/kitexotel`（**深度 4**），`observability/` 没有 go.mod
⇒ kitexotel 的 import 被算成**父仓**的 ⇒ 父仓「看起来自己 import 了重依赖」
⇒ 这条判据对最深的嵌套模块**完全失效**（`istio/` 在第一层，被正确排除了）。

#### 改动

- 新增 `_imports_module(imports, mod)`：按**模块前缀**判（`ip == mod or ip.startswith(mod + "/")`）；
- 新增 `_under_nested_module(repo_dir, rel)`：逐级向上找 `go.mod`，覆盖**任意深度**；
- 顺带修 stdout 行缓冲：重定向/管道时 Python 默认块缓冲 ⇒ 34 个单元逐个
  `go list -m all` 的那几分钟里日志全是 0 字节 ⇒ 进程被杀/超时时一个字都留不下。

#### 自测（判据失效必须红）

新增两条**判据用例**，都自带反证样本：

- ⑦ 父仓 import 的是该模块的**子包**时不该报 ⇒ 若前缀比退回全等，**立刻变红**；
- ⑧ **深层**嵌套模块的 import 不该算成父仓的。

变异验证：前缀比退回全等 ⇒ ⑦ 红；还原 ⇒ 全绿。

实测修正后 kitex 仓的两条假阳性违规消失，真实违规仍报得出。

### fix(ci)：consumer job 跨仓写回 oekit.github.io 必须用 PAT——这个 job 从未绿过，真违规从未被报出

`Matrix audit` 的 `consumer` job 长期是红的，站点消费面画像**冻结在 2026-10-21**：

```
could not read Username for 'https://github.com'
fatal: could not read Username for 'https://github.com'
```

#### 根因

1. 该 job 用**无凭据**的 HTTPS URL clone `oekit.github.io` ⇒ `git push` 要凭据时无人应答；
2. `GH_TOKEN` / `GITHUB_TOKEN` 环境变量只对 **gh CLI** 生效，**git 不认这两个变量**；
3. token 来源选错：`secrets.GITHUB_TOKEN` **只对本仓（matrix-tools）有效** ⇒ 拿它推
   `oekit.github.io` 必然 403/无凭据。跨仓写回必须用 **classic PAT**
   （`OEKIT_PROJECT_TOKEN` 在本 org 各仓都有，带 repo scope）；
4. ⚠️ 更糟的一层：push 失败发生在 **"Open consumer issue" 之前** ⇒ 步骤失败即中断后续
   ⇒ **真违规从来没人被报过**（这个 job 看上去在跑，实际只跑到写回就死了）。

#### 改动

- token 取 `secrets.MATRIX_AUDIT_TOKEN || secrets.OEKIT_PROJECT_TOKEN || secrets.GITHUB_TOKEN`；
- push 显式塞 `http.extraheader`：

  ```bash
  AUTH_HEADER="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GH_TOKEN" | base64 | tr -d '\n')"
  git -c "http.https://github.com/.extraheader=$AUTH_HEADER" push --force origin chore/deps-profile
  ```

  用 extraheader 而不是改 remote URL ⇒ token 不落进 `.git/config`，也不进 `git remote -v` 的输出；
- push 失败时 `::error::` 给出**可执行诊断**（指明该用哪个 secret、为什么 GITHUB_TOKEN 不行）；
- `set -euo pipefail`：让管道里的失败不被吞。

#### 影响面

`consumer_probe` 的判据只喂给 CI 的 `consumer` job 与本地画像生成 ⇒
不影响发版链（`matrix.py` / `audit.py` / `release_post.py`）。

## [v0.1.19] — 2026-10-27

### fix(tools)：远端 tag 读取加重试——一次网络抖动曾打死整条发版后置链

2026-10-27 跑 gormx v0.6.2 的发版后置链（`release_post.py gormx`）时，链在**第三步**
（站点 `versions.json`）死掉：

```
gen_versions 失败：matrix.RemoteReadError: middleware: 远端 tag 读不到
（已试 https://github.com/oekit/middleware.git）——读不到 ≠ 没有，
不能用本地 tag 顶替（#79）
```

⇒ 版本行 PR 已经开了（gormx#33），而**站点与 BOM 都没开**，只能整条链重跑一遍。
实测手动 `git ls-remote` 同一个仓当场可读 ⇒ 是**瞬时**网络抖动，不是真读不到。

#### 根因

`matrix._ls_remote_tags()` 对每个 URL 只试**一次**，`remote_tags()` 再换 origin URL
兜底一次 ⇒ 两次都撞上瞬时抖动就抛 `RemoteReadError`。而「读不到 ≠ 没有」是 #79 的
核心口径（**不能**降级成本地 tag）⇒ 失败得抛，于是一次抖动就把整条链打断。

远端读失败**大多数是瞬时的**（代理抽风 / DNS 抖动 / 连接被重置），不该和
「这个仓真的读不到」用同一种处理。

#### 改动

- `_ls_remote_tags(url, timeout=60, attempts=3)`：每个 URL 最多试 3 次，
  退避 0.5s / 1s / 2s；
- ⚠️ **重试只吸收瞬时抖动，不改 #79 的口径**：全试完仍失败照样抛
  `RemoteReadError`，**绝不**降级成本地 tag（读到空 ≠ 读不到）。

`remote_tags` 是**所有**远端读的事实入口——审计（`audit.py`）、BOM 生成
（`gen_matrix_go.py`）、站点版本表（`gen_versions.py`）、Release 对账
（`gen_releases.py`）都走它 ⇒ 一次抖动原本能打断其中任何一条链。

#### 自测（判据失效必须红）

`scripts/tag_cache_selftest.py` 补 2 例（该文件已接进 CI 的 `selftest` job）：

- **反证 7**：远端读遇到瞬时失败必须重试，不能一次就死（前两次失败、第三次成功
  ⇒ 应读成功，尝试次数 == 3）；
- **反证 8**：全试完仍失败 ⇒ **仍然抛 `RemoteReadError`**（#79 口径不变），
  且尝试次数 == 2 URL × 3 次（证明重试真的发生了）。

**变异验证**：`attempts` 从 3 改成 1（等价于去掉重试）⇒ 反证 7、8 双双变红；
改回 3 ⇒ 7/7 全绿。

## [v0.1.18] — 2026-10-25

### fix(tools)：审计加实时进度行 + BOM「读不到」与「陈旧」分开报

修后台审计任务「Re-run audit for final truth」（`bg_19b83328`，900s 超时）暴露出来的
两个问题：**超时后日志里一个字都没有**，以及**「读不到」被当成「漂移」报出来**。

#### 一、超时后日志全丢

该任务的输出日志只有 **59 字节**——运行时那句
`Background bash failed: Command timed out after 900 seconds`，审计自己的输出全丢。
两个原因叠在一起：

1. **stdout 块缓冲**：Python 的 stdout 在重定向/管道时是块缓冲的。实测一次 48s 的
   审计写进日志文件时，**整个运行期间文件都是 0 字节**，只在进程退出那一刻一次性
   落盘 ⇒ 跑到一半被 kill/超时 ⇒ 什么都没有。
2. **命令形状**：`... | grep -E "..." | tail -15`（管道两端都缓冲，中间还要等进程
   结束才吐）+ 开头的 `sleep 90`（白等 90s 预算）。

改动：

- `scripts/audit.py`：stdout 改**行缓冲**（`sys.stdout.reconfigure(line_buffering=True)`）
  ⇒ 印一行落一行，进程被 kill 也留下已印的部分。
- `scripts/audit.py`：新增 `progress()`，**进度行写 stderr**（stderr 默认行缓冲）。
  写 stderr 而不是 stdout 是刻意的：CI 里 `audit.py > audit-report.txt` 收集的是
  stdout（那份文件会被贴进漂移 issue），进度进的是**日志**（CI 上实时可见），
  **报告文件格式一个字不变** ⇒ `grep -c '❌'` 的漂移计数不受影响。
  进度分 7 个阶段：预取远端事实 / 逐仓检查（24 仓逐个报）/ 站点仓 / 独立门禁 /
  Release 对账 / BOM 新鲜度 / 出报告。
- `scripts/gen_releases.py`：Release 对账是 24 次**串行** GitHub API 调用，是整条
  审计最长的阶段 ⇒ 逐仓进度写 stderr。

改前/改后实测（同一台机器、同一网络条件）：

| | 运行期间日志大小 | 进程被杀时留下什么 |
|---|---|---|
| 改前 | **0 字节**（T+10/20/30/40s 全是 0） | 什么都没有 |
| 改后 | **1346 字节**（T+10s 已有进度） | 「跑到哪一步」清清楚楚 |

#### 二、「读不到」不该混进「漂移」

兄弟会话那条同名任务（`bg_b4bfd6cf`，local 模式）报出 2 项漂移，其中一条：

```
❌ matrix.RemoteReadError: registry: 远端 tag 读不到（已试 https://github.com/oekit/registry.git）——读不到 ≠ 没有，不能用本地 tag 顶替（matrix-tools#79）
```

那是 **BOM 子进程**崩了、audit 取它输出的**最后一行**当漂移消息——读的人根本看不出
这是**读取失败**而不是**产物陈旧**（违反 #79 的口径）。

改动：

- `scripts/gen_matrix_go.py`：新增**退出码契约**（契约的**生产方**是子进程、
  **消费方**是 audit.py，口径只此一处）：`0 = 一致` / `1 = 真漂移（产物陈旧）` /
  `2 = 读不到远端 tag（判不了 ≠ 通过）`。`main()` 捕获 `RemoteReadError` ⇒ 打印干净、
  带修复步骤的消息并返回 2，**不再抛裸 traceback**。
- `scripts/gen_matrix_go.py`：新增 `classify_check(rc, out)`，把退出码翻译成
  (`ok` | `stale` | `unreadable`, 消息)；audit.py 直接调用它，**不靠猜消息文本**。
- `scripts/audit.py`：BOM 子进程退出码 2 ⇒ 报成「BOM 新鲜度**未核对**（读不到远端
  tag，判不了 ≠ 通过）」，不再混进「漂移」。
- `scripts/gen_matrix_go_selftest.py`：新增 2 例（现 7 例）——「读不到 ⇒ 退出码 2 +
  干净消息（不是裸 traceback）」与「退出码分类（0/1/2 ⇒ 一致/真漂移/未核对）」；
  已接线进 `.github/workflows/audit.yml` 的 selftest job。

#### 三、该任务重跑（修正命令形状）

去掉 `sleep 90`、去掉 `| grep | tail`，日志直接落盘：

```
[Re-run audit for final truth — 修正命令形状后重跑]
退出码=0 耗时=54s
[audit] 阶段 6/7 oekit BOM 新鲜度（子进程）
[audit] 阶段 7/7 出报告
远端 tag 读取：24/24 仓成功（0 读失败）
✓ 矩阵审计通过（24 模块 + 站点）
```

审计结论与报告格式**不变**；BOM 产物零变化（`--check` 仍报
`✓ oekit BOM 与远端 tag 一致（v0.1.44，35 模块）`）。
## [v0.1.17] — 2026-10-25

### perf(tools)：远端 tag 读取加进程内缓存 + 并发预取——审计 281s → 48s

后台审计任务（`Final audit run`）反复以 `Command timed out after 900 seconds` +
**输出为空**收场：活没干完，工具只看到「超时」两个字。

**根因不是审计逻辑变了，是同一份远端事实被重复读了几十遍。**

`audit.py` 对同一个仓的「远端最新 tag」问了**四个**地方（README 安装行 / AGENTS.md
当前版本行 / 站点 versions.json / 嵌套模块的父模块地板），而
`matrix.remote_tags()` **没有任何缓存**（每次都真的打一次 `git ls-remote`）⇒
24 仓 × 4 处 = **96 次** HTTPS 读；`gen_releases` 的 Release 对账还要**每个仓的完整
tag 列表**（再 24 次）；audit 把 BOM 生成器当**子进程**跑（再 48 次）⇒ 一次审计
合计 ≈ **168 次**远端读。

实测（同一台机器）：单次读 1.0–2.1s（网络拥塞时 5–10s）⇒ 审计要跑 **281s**，
网络一拥塞就直奔 900s。

改动：

- `matrix.py`：新增**默认关**的进程内远端 tag 缓存
  （`enable_tag_cache` / `disable_tag_cache` / `clear_tag_cache`）——同一进程内同仓
  只读一次；**失败态也缓存**（读不到的仓恰恰最慢：要依次试 canonical HTTPS 和
  origin，两条都失败才抛，重读多遍是最贵的一档），但重抛的是**同一个错误**，
  **不**把「读不到」说成「没有 tag」（#79 的口径不变）。
  **默认关是刻意的**：BOM / versions.json 的快照必须反映**生成那一刻**的远端事实，
  发布类脚本（release_train / gen_versions / gen_matrix_go）永远读最新；只有
  **一次性**的核对流程（audit）才显式开启。
- `matrix.py`：新增 `prefetch_remote_tags()`——**并发**预取各仓远端 tag
  （实测 24 仓串行 34.6s ⇒ 并发 **6.1s**）；失败按传入顺序回调 ⇒ 报告里漂移条目的
  顺序是确定的（并发下按完成顺序回调会让每次跑的报告顺序都不一样）。
- `scripts/audit.py`：开头 `enable_tag_cache()` + 并发预取，之后 4 个调用点与
  Release 对账全部命中缓存（≈168 次 ⇒ 24 次）；读失败**只记一条漂移**（同一仓被问
  四次，记 4 遍会让人以为有 4 个问题）。
- `scripts/gen_matrix_go.py`：每仓读**两次**远端 tag（`latest_version` +
  `_remote_tags`）⇒ 改成读一次、本轮复用；并复用上面的缓存 + 并发预取。
  读失败仍直接抛（fail-loud，#100 的口径）。
- `scripts/tag_cache_selftest.py`（新）：**反证怎么构造**——把缓存去掉 / 把并发
  改成串行，本自测必须失败。5 例覆盖：同仓连读 4 次只打 1 次网络 / 读失败重抛同一
  个错误且不重打网络、也不变成空列表 / 缓存默认关 / prefetch 必须并发
  （`threading.Barrier` 卡住，退化成串行必炸）/ 预取的失败回调顺序确定。
  变异实测：去掉缓存 ⇒ 3/5 例红；把 prefetch 改成串行 ⇒ 并发那例红。
  已接线进 `.github/workflows/audit.yml` 的 selftest job。

实测（同一台机器、同一网络条件，`audit.py --remote-tags`）：

| | 耗时 | 远端读次数 |
|---|---|---|
| 改前 | **281s** | ≈168 |
| 改后 | **48s / 49s**（连跑两次） | 24（并发） |

**5.8×**。审计结论**不变**：`远端 tag 读取：24/24 仓成功（0 读失败）` +
`✓ 矩阵审计通过（24 模块 + 站点）`。GitHub Actions 上的 audit job 耗时 **21s**。

**产物零变化**：`gen_matrix_go.py --check` 改前改后是同一句话——
`✓ oekit BOM 与远端 tag 一致（v0.1.44，35 模块）`。

## [v0.1.16] — 2026-10-25

### fix(tools)：BOM 生成器的 Go 地板取不到就报错——v0.1.42 曾把 hertz 的 1.26.0 写成 1.24.0

`gen_matrix_go.py` 生成 BOM 时**版本取远端 tag、Go 地板取本地 clone**，两路口径分裂：

- 版本：`gen_versions.latest_version` → `_remote_tags`（**远端**）
- 地板：`git -C <dir> show <tag>:go.mod`（**本地 clone 的 git 对象**）

本地 clone 还没 fetch 到那个 tag 时（多仓并行发版、clone 刚建、网络不稳），
`git show` 直接失败 ⇒ 落到旧实现的兜底分支 `return m.group(1) if m else "1.24.0"`。

**关键点**：兜底值 `1.24.0` 不是「未知」而是**一个具体的、看起来合法的地板**——它和
「真的是 1.24.0 的仓」在产物里长得一模一样，下游无从分辨。地板**低报比不报更危险**：
不报会让人去查，报一个具体数字会让人以为那就是事实（按 BOM 选型的消费方会以为
Go 1.24 装得下，拉下 hertz v0.8.5 构建直接失败）。

已污染一个发布物：BOM **v0.1.42** 把 hertz 写成 `Go: "1.24.0"`，而 hertz v0.8.4 /
v0.8.5 的 `go.mod` 都是 `go 1.26.0`（触发条件：生成时本地 hertz clone 尚未 fetch 到
`v0.8.5`）。v0.1.42 已发布、tag 不可撤，只能补发 v0.1.43 修正——**但根因仍在**，
下一个 BOM 版本只要再撞上「本地 clone 缺 tag」就会重犯。

改动：

- `scripts/gen_matrix_go.py`：`go_floor()` **fail-loud**——取不到 `go.mod`、或 `go.mod`
  里没有 `go` 指令 ⇒ 抛 SystemExit，错误消息里带可执行的修复步骤（`git fetch --tags`）
  与 issue 编号；**不再有任何默认地板**。这与 `matrix.remote_tags` 已有的口径一致
  （「读到空」≠「事实为空」，#79）。
- `scripts/gen_matrix_go.py`：新增 `ensure_local_tag()`——取地板前若本地缺该 tag，
  补一次 `git fetch --tags`（**只在真缺时做**，稳态零额外网络开销；fetch 失败不抛，
  真因仍由 `go_floor` 报，它的消息里带 git 的原始 stderr，更可诊断）。
- `scripts/gen_matrix_go_selftest.py`（新）：**反证怎么构造**——把「取不到就报错」
  改回「兜底」，自测必须失败。5 例覆盖：正常路径取到真地板 / 本地缺 tag 必须抛错
  （且消息里不含任何看起来合法的地板值）/ `go.mod` 无 `go` 指令同样抛错 /
  `ensure_local_tag` 能从 origin 补取 / 稳态不做额外 fetch。无副作用、不联网、
  不依赖工作区布局（临时目录造 fixture 仓，「远端」用本地 bare 仓当 origin）。
  已接线进 `.github/workflows/audit.yml` 的 selftest job。
- 兼容性：happy path **零行为变化**——`gen_matrix_go.py --check` 实跑仍报
  `✓ oekit BOM 与远端 tag 一致（v0.1.43，35 模块）`；产物格式零变化（仍是 `Go:` 字段），
  只有「取不到」时从「写假值」变成「报错退出」。

**变异验证（实跑）**：把兜底改回 `return m.group(1) if m else "1.24.0"` ⇒ 反证 1、2
两条用例立刻 FAIL（`应当抛 SystemExit，却正常返回了`）；还原后 5 例全绿。

关联 oekit/matrix-tools#100（产物侧已由 BOM v0.1.43 修正）。

## [v0.1.15] — 2026-10-24

### fix(tools)：维护轮门禁此前从未生效——读不到仓却报「零漂移」，且维护轮标签不存在

月度 workflow `maintenance.yml`（2026-10-19 加入，cron `0 4 5 * *`）**至今 0 次运行、
0 个维护轮 issue**，而**真实地板漂移是 20 仓**（阈值 8）——门禁到期不报，
且没人知道信号丢了。三个根因互相叠加：

1. **`maintenance_check.py` 不认 `MATRIX_BASE`**，CI 布局下 24 仓全部「读不到」。
   脚本硬编码 `BASE = parent.parent.parent`，而 `matrix.repo_dir(repo, base)` 的语义是
   `base or os.environ.get("MATRIX_BASE", ...)`——**base 非空就把环境变量短路掉**。
   于是 docstring 里写的 `MATRIX_BASE=repos python3 scripts/maintenance_check.py` 是无效的；
   CI 里仓克隆在 `$GITHUB_WORKSPACE/repos/<name>`，`repo_dir` 的三个候选一个都不命中
   ⇒ 24 仓全抛 `FileNotFoundError` ⇒ 被 `except Exception` 吞掉 ⇒ `MAINTENANCE_COUNT=0`。
2. **后果是反向的**：`count == '0'` 走的正是「**关闭**维护轮 issue」分支
   ⇒ 门禁方向反了——到期不报、反而关单。
3. **标签 `matrix-maintenance` 在本仓不存在** ⇒ 即使走对分支，`issues.create` 也会
   422 Validation Failed，issue 依然开不出来。（`matrix-drift` 存在，所以 `audit.yml`
   的漂移 issue 开得出来——这就是两个 workflow 表现不一致的原因。）

连带：`align_floors.py` 是同一行硬编码，而它是维护轮**唯一**的整改工具
⇒ 定位错 ⇒ 整改命令打到错误目录。

改动：

- `scripts/maintenance_check.py`：base 解析认 `MATRIX_BASE`（与 `audit.py` 同口径），
  `--base` 可显式覆盖，新增 `--repos` 单仓盘点；**「读不到」与「真的没漂移」分开**——
  读不到的仓计入 `MAINTENANCE_UNDETERMINED`，且**不再打印「✓ 全矩阵地板零漂移」**
  （与 audit 在 #79 对 remote tag 的处理同口径：读不到 ≠ 事实为空）。
- `scripts/align_floors.py`：同样认 `MATRIX_BASE` / 新增 `--base`。
- `.github/workflows/maintenance.yml`：显式传 `--base "$PWD/repos"`；
  `set -o pipefail` 让脚本崩溃传导为步骤失败（而不是被 `tee` 吞成 0）；
  关闭分支收紧为 `count == '0' && undetermined == '0'`——**只有确定没有漂移才关单**；
  计数缺失（脚本崩了）⇒ 明确失败、**不**改动任何维护轮 issue；
  开 issue 前确保标签存在（自愈，不依赖仓外的带外设置）。
- `scripts/maintenance_check_selftest.py`（新）：一道没人验证过的门禁，很可能自己就是
  个假绿。自测用**临时目录 + 临时 git 仓**（不联网、不碰真仓）复刻 CI 布局，证明仓能
  被认出、漂移能被算出来、仓读不到时**不许**输出「零漂移」、`align_floors.py` 同样认
  `MATRIX_BASE`。**改前红 4/5、改后绿 5/5**（同一命令行，失败原因只能是 bug 本身）。
- `.github/workflows/audit.yml`：自测接入既有 `selftest` job。
- `README.md`：补维护轮 workflow 说明与两个工具的 base 口径。

### feat(tools)：维护轮盘点新增 `--repos` 单仓盘点

`maintenance_check.py` 新增 `--repos`，只盘点指定仓（逗号分隔，缺省全矩阵）。
与 `align_floors.py` 的 `--repos` 同款用法——维护轮整改通常是逐仓推进，
全矩阵盘点每次都要重跑 24 仓。

## [v0.1.14] — 2026-10-24

### fix(tools)：嵌套模块支持多层子目录路径——五处「单层目录」假设在 kitex v0.7.0 后全部算错

kitex v0.7.0 把 `observability/tracing/instrumentation/kitexotel` 与 `istio` 拆成嵌套模块（kitex#37）。
Go 的子目录模块 tag 规则要求 tag 带**完整子路径** ⇒ 这类嵌套模块的 tag 是
`observability/tracing/instrumentation/kitexotel/v0.1.0`；而工具链有五处解析按「单层目录」写，
在多层时**算错**：

| 位置 | 旧假设 | 修法 |
|---|---|---|
| `gen_versions.py` | `nested_versions` 用 `tag.split("/", 1)` | 改 `rsplit`（split 会把多层路径切成 path=observability / ver=tracing/…/v0.1.0） |
| `gen_matrix_go.py` | `NESTED_ONLY_TAGS` 正则只放行单层路径 | 正则放宽为允许多层，版本号改 `rsplit` 取最后一段 |
| `release_train.py` | 嵌套模块的 CHANGELOG 取 `module_dir.parent/CHANGELOG.md`（假定在仓根下一层） | 上溯到仓根（含 `.git` 的那层）——多层时原指向不存在的目录，发版被凭空拦下 |
| `verify_release_units.py` | llms.txt 嵌套条目正则只放行单层路径 | 名字与链接正则同步放宽 |
| `audit.py` | 嵌套模块父仓地板门禁的 glob 只扫「仓根下一层」 | 放宽为递归（`**/go.mod`，排除仓根本身） |

对既有 9 个单层嵌套模块**零行为变化**：`gen_matrix_go --check` 与 `gen_versions` 重跑后产物逐字节一致。

### fix(tools)：count_units 数嵌套模块改为递归扫（多层子目录路径不再漏数）

`count_units` 用 `r.glob("*/go.mod")` 只扫「仓根下一层」⇒ 多层嵌套模块被漏数：
实测文件系统里有 **11** 个嵌套模块，单层 glob 只数出 **10** ⇒ 站点文案据此判漂移
（真实 11 / 36，误判 10 / 35）。

改为递归扫 `**/go.mod`，排除仓根本身与 `.git` / `vendor` / `third_party` / `testdata` / `examples`
（与共享 CI 的嵌套模块发现口径一致）。口径数字同步更新：**嵌套模块 11、发版单元 36**（24 根 + 11 嵌套 + 1 伞仓）。

### feat(tools)：NESTED_MODULES 登记表补齐 kitex v0.7.0 拆出的两个嵌套模块

`matrix.py` 的 `NESTED_MODULES` 登记表是「应该有什么」的唯一声明。缺登记会让三类判据失真：

| 判据 | 缺登记时的表现 |
|---|---|
| `verify_release_units.py` 发版单元口径 | 门禁口径 34 ≠ 文件系统推导 36 ⇒ 判「口径分裂」红 |
| `check_section_consistency` llms.txt 逐字节比对 | 规范渲染少两条 ⇒ 站点段漂移红 |
| `check_llms_units` 嵌套模块安装行 | kitexotel / istio 无 `go get` 安装行 ⇒ Agent 装不上 |

补登记 2 条（`kitexotel`、`istio`，含 module 路径与 desc），计数口径同步：嵌套模块 9 → 11、发版单元 34 → 36。

### docs(matrix)：DESC 同步——pkg 补 PeekGetter/stale-if-error 契约、data 门面同步壳转发面

真相源 `matrix.py` 的 `DESC` 与已发布的矩阵表对齐（两处均为**已发布事实的登记**，非新增宣称）：

- **pkg**：补 v0.23.0 起 Peek 非删除式读（可选接口 `PeekGetter` 外置——读到过期条目只算不命中、不删除它，
  stale-if-error 兜底需要那份过期真值留在表内；`SingleFlightCache` 包装 `LocalCache` 时曾因前置检查走
  `Get` 惰性删除而静默废掉 `StaleIfError`，pkg#72）；
- **data**：补 v0.18.0 起 cache 壳 `PeekGetter` 别名 + cachetest 壳 2 个 stale-if-error 契约入口（套件 13 → 15 场景）。

### fix(tools)：登记表的伞仓计数注释去掉过期绝对数

PR #94 把嵌套模块 9 → 11、发版单元 34 → 36，但 `BOM_RELEASE_UNIT` 旁的注释仍写着
「裁决定死 34」——裁决本身（**伞仓必须计入发版单元口径**）没变，变的是总数随嵌套模块增减。
注释里的绝对数会再次过期且误导（读者会以为 34 是定值）⇒ 改为只写裁决、不写绝对数。

## [v0.1.13] — 2026-10-23

### feat(tools)：新增「发布序列倒挂」门禁——版本号回退的旁支发布进不了矩阵也撤不掉

全矩阵 24 个模块仓里，**4 个仓有 5 个已发布版本的 tag 发布时刻晚于同一版本线上
更高的版本**——版本号在时间线上**回退**了：

| 仓 | 旁支版本 | 发布时刻 | 此前已发布的更高版本 | 其发布时刻 | 间隔 |
|---|---|---|---|---|---|
| kitex | `v0.3.9` | 10-15 19:32 | `v0.4.1` | 10-15 16:48 | +2h43m |
| kitex | `v0.3.10` | 10-16 08:39 | `v0.4.1` | 10-15 16:48 | +15h50m |
| redisx | `v0.3.1` | 10-16 10:20 | `v0.4.0` | 10-16 10:17 | **+3m** |
| middleware | `v0.5.1` | 10-17 16:52 | `v0.6.0` | 10-17 16:08 | +44m |
| network | `v0.1.1` | 10-17 16:53 | `v0.5.1` | 10-17 16:51 | **+2m** |

这些版本**都已发布**（tag 在远端、GitHub Release 在、Go module proxy 上可解析）
⇒ 删不得也挪不得（重推 = 已缓存消费方集体中毒，.github#5）。矩阵
（versions.json / llms.txt）只登记 `latest` 与 `breakingIndex`，**不登记它们**
⇒ 对外口径与远端事实不一致。

#### 现有门禁为什么放行

共享 CI 的 `changelog_check.py` 规则 1 查的是「**CHANGELOG 小节**按 semver 严格
递减」——那是**文档序**；倒挂是**发布序**（事实序）。network 的小节顺序
`v0.5.5 … v0.5.0 → v0.1.1` 逐节递减 ⇒ **门禁绿**，而事实序是 `v0.5.1` 先发布、
`v0.1.1` 后发布。**实测 4/4 仓的 changelog_check 全绿**。

#### 新增

- `scripts/verify_version_line.py`——独立 oracle：自己解析 semver、自己读 git
  原始输出（`git for-each-ref --sort=creatordate`），刻意不复用任何现有解析
  逻辑（共享逻辑就有共同盲区，matrix-tools#31 复审原则）；**版本线 =
  (路径前缀, module 路径)**——module 路径变更是「新模块」，新旧两条线各自单调
  （redisx 活例：旧路径 `github.com/oekit/redis` 与新路径 `…/redisx`）；
  某仓一个 tag 都没有 ⇒ 判不了，不当通过。
- `scripts/version_line_known.json`——已登记的 5 处旁支版本，逐条写明事实与处置；
  **未登记的新倒挂仍然判红**（例外表是显式人工放行口，不是漏报口子）。
- `scripts/verify_version_line_selftest.py`——变异自测 **7 例**：版本号回退必须
  抓到且参照是此前已发布的最高版本 / 正常递增不误报 / 嵌套版本线独立 /
  module 路径变更后新线起点不误判 / 同一路径内的回退仍抓到 / 例外表只放行登记的
  那一处 / 无 tag 判不了。

接入 `audit.py`（聚合层 ⇒ 随之进 CI 的 PR 门禁与周巡检）；`audit.yml` 的
selftest job 加入新自测。

#### 自测

```
✅ 自测1：版本号回退被抓到（得到 ['v0.1.5']，参照 v0.3.0）
✅ 自测2：正常递增不误报（得到 []）
✅ 自测3：嵌套版本线独立（得到 []）
✅ 自测3b：module 路径变更后新线起点不误判（得到 []）
✅ 自测3c：同一路径内的回退仍被抓到（得到 ['v0.1.0']）
✅ 自测4：例外表只放行登记的那一处（known=['v0.1.5'] unknown=['v0.1.6']）
✅ 自测5：无 tag ⇒ 判不了（unreadable=1）
```

#### 明确不做

- **不删、不重推任何已发布 tag**（= 已缓存消费方集体中毒）
- 不改已发布版本的内容语义（各仓只补事实声明：network#24 / kitex#33 /
  middleware#46 / redisx#31）

关：#88

## [v0.1.12] — 2026-10-23

### fix(tools)：「远端 tag 是否存在」三条读路径语义统一——发版查重读失败即当「不存在」

#79 把「读到空 ≠ 事实为空」在 GitHub API 侧（ghsafe）与 git 传输侧
（matrix.remote_tags）都做实了，但「远端有没有这个 tag」这条**事实**还有另外
两条读取路径——发版查重与关单守卫——仍是旧语义：只看 `git ls-remote` 的 stdout
非空 ⇒ 读失败 = 「不存在」。

#### 变异实测（修复前）

同一个**确实存在于远端**的 tag，只因**读法**不同就从「存在」翻成「不存在」：

| 读法 | 读成功 | 读失败 |
|---|---|---|
| `release_train.tag_exists_remote` | `True` | **`False`** |
| `matrix.tag_exists` | `True` | **`False`** |

#### 三处漏洞

1. **发版查重 fail-open**：`release_train` 检查面 a「同名 tag 查重」在读失败时
   返回 False ⇒ 「这个 tag 不存在」⇒ **门禁静默放行**。docstring 第 11 行写着
   这条检查的理由：「重推 = 代理/sumdb 缓存集体中毒，.github#5」——恰在最需要
   它的时刻（SSH 掉线 / 网络抖动）放行。
   真正拦住重推的是 `git push` 自身（实测同名 tag 无 `--force` 推送被拒：
   `! [rejected] v1.0.0 -> v1.0.0 (already exists)`，远端 tag 指向不变）⇒
   **不会真的重推成功**，但脚本会在 `git tag -a` 之后崩在 push、留下半截本地
   tag，报错是「already exists」而不是「查重时读不到远端」——排查被带偏。
2. **关单守卫误报**：`close_issue` 的「tag 必须已存在于远端」守卫 fail-closed
   （不会凭空发回执），但报的是「tag 不在远端」——**事实说反了**。这条信息会把人
   推向 `--force`（脚本文案里就写着「确实要引用未发版本需 --force」），而真实
   原因可能只是网络抖了一下 ⇒ 守卫生效的路径被误诊成「刻意的越权」。
3. **第三种语义留在公共函数里**：`matrix.tag_exists` 同样只看 stdout 非空
   （当前无调用方，`release_post.py` 只是 import 未用）。

#### 改法

- `matrix.remote_tag_exists`：这条事实的**唯一**读法（走 `matrix.remote_tags`：
  canonical HTTPS 优先 → origin 兜底 → 失败抛 `RemoteReadError`）；
  `matrix.tag_exists` 收口到它，不再自带语义。
- `release_train.tag_exists_remote` 改走它；`check_module` 接住 `RemoteReadError`
  ⇒ **fail-closed**（不进打 tag 环节），错误信息写明「读不到 ≠ 不存在」。
- `close_issue.tag_on_remote` 改走它；读不到 ⇒ 报「读不到远端 tag（不是不存在）」
  并明确不要用 `--force` 绕过；`--force` 是刻意的越权 ⇒ 显式跳过核对、不发读
  请求（保持自测离线）。

#### 自测与变异测试

- `scripts/remote_reads_selftest.py` 加 6 组用例（I/J/K/L/M/N）：三条读路径各自
  的「读失败必抛」与「读成功必判存在」。该脚本已接进 `audit.yml` 的 selftest job
  ⇒ 自动进 CI 覆盖。
- 变异测试：M1 release_train 退回旧语义 ⇒ **K 转红**；M2 close_issue 退回旧语义
  ⇒ **M 转红**；M3 matrix 退回旧语义 ⇒ **I 转红**；3/3 转红，还原后 **14/14 全绿**。
- 其余自测（close_issue / dependabot_dirs / ghsafe / verify_release_units）全绿；
  `audit.py --remote-tags` 通过（24/24 仓远端 tag 读取成功）。

影响面：只改**读取层**，不碰任何生成器输出（versions.json / BOM 内容不变）⇒
不需要「先落产物再合生成器」的顺序。

## [v0.1.11] — 2026-10-23

### fix(tools)：发版单元门禁漏检伞仓 BOM 仓——llms.txt 覆盖 34 个发版单元的承诺无人守

站点承诺 llms.txt 是「**34 个发版单元**」的机器可读索引（24 根 + 9 嵌套 + 1 伞仓
`oekit`），canonical 口径 `gen_tables.count_units` 也这么算。但发版单元门禁
`verify_release_units` 只校验 24 + 9 = 33，**伞仓压根没进判据**。

伞仓 `oekit` 是货真价实的独立发版单元：有独立 tag（latest v0.1.29）、独立
GitHub Release，下游 `go get github.com/oekit/oekit@latest` 是一次独立的版本获取。

#### 变异实测（修复前）

把站点 `docs/public/llms.txt` 里伞仓那段（`go get github.com/oekit/oekit@latest`）
整段删掉后重跑门禁：

    ✅ 发版单元门禁通过（33 个发版单元 = 24 根 + 9 嵌套；9 条嵌套安装行齐全）
    exit=0

同样的变异对嵌套模块是**红的** ⇒ 门禁的牙没长在伞仓上。而站点
`docs/guide/ai-agent.md` 正对外承诺「llms.txt 覆盖 34 个发版单元（… + **1 个 BOM
伞仓 `oekit`**）」⇒ 这条承诺**无人守**（llms.txt 是 AI Agent 唯一会抓的机器可读索引）。

#### 改法

- `check_llms_units` 增加**伞仓 oracle**：llms.txt 必须含
  `go get github.com/oekit/oekit@latest` 安装行，与嵌套模块同等要求。该函数已被
  `audit.py` 导入（`scripts/audit.py:23`）⇒ 改动自动进 CI 覆盖面（audit.yml 的
  audit job：PR 门禁 + 周巡检），无需新增 CI 接线。
- `check_filesystem_oracle` 增加伞仓的物理证明：目录在 ⇒ 必须真有独立 `vX.Y.Z`
  tag（与嵌套模块同款判据）；目录没检出不报（本地少 clone 一个不该假红）。
- 口径统一到 canonical：新增 `release_unit_count()` = 24 根 + 9 嵌套 + 1 伞仓 = 34。
  伞仓数不写死常量（判定依据是 `BOM_REPO` 不在矩阵登记表里，它若哪天被收进矩阵
  模块，口径自动变 0，不会两处各说各话）。
- 门禁额外做**口径交叉核对**：完整检出时拿文件系统推导（`count_units`）与
  canonical 口径对比，不一致即判红——同一个术语不许对外报出两个数。

#### 自测与变异测试

- 新增 `scripts/verify_release_units_selftest.py`（纯内存 fixture；不联网、不依赖
  工作区布局、无副作用），4 组用例：合法 fixture 零问题、**删掉伞仓安装行必须判红**
  （原始形态）、删掉嵌套安装行必须判红、删掉根模块条目必须判红；另钉住 34 这个口径。
- 变异回归：修复前同一变异 `exit=0`（绿）→ 修复后 `exit=1` 并点名伞仓。
- 接入 `audit.yml` 的 selftest job（与 close_issue / dependabot_dirs / ghsafe /
  remote_reads 四个既有 selftest 并列）。

## [v0.1.10] — 2026-10-23

### fix(tools)：git 传输侧「读到空 ≠ 事实为空」——ls-remote 失败不再静默降级

#75 把「读到空 ≠ 事实为空」在 **GitHub API 侧**做实了（`ghsafe.py`），但矩阵的
「版本事实」还有**第二条**读取路径——**git 传输**（`git ls-remote --tags`），
它此前是裸 `except`，失败即静默降级，且没有任何告警。

两处漏洞：

1. **`gen_versions._remote_tags`**：读失败 ⇒ 直接返回**本地 clone 的 tag**。
   而这个函数的 docstring 写明它存在的理由正是「本地 clone 的 tag 可能没 fetch，
   生成 generatedAt 很新但内容陈旧的 versions.json——比明显陈旧更有害」
   ⇒ 回退到本地 tag 恰好把它要防的 bug 原样放回来。产物 `versions.json`
   （站点对外的版本事实）与 `oekit` BOM（`go get github.com/oekit/oekit@latest`
   一次拉齐的 MVS 地板）都由它喂数据 ⇒ 降级会直接**流向消费方**。
2. **`matrix.latest_tag(remote=True)`**：读失败 ⇒ 返回 `None`，而四个调用方一律
   `if lt:` 跳过 ⇒ 该仓整条检查**静默消失**，报告照样印「✓ 矩阵审计通过」。
   这正是 `audit.yml` 注释里记的 #26 形态：23/24 仓被跳过，报告仍显示通过。

#### 改法

- 新增 `matrix.remote_tags`：canonical HTTPS **优先**、clone origin URL 兜底
  ——两条路协议独立（SSH 抽风不影响结论；实测 SSH ≈6.5s/仓、HTTPS ≈1.2s/仓，
  优先 HTTPS 顺带把审计跑快约 5 倍）；两条都失败 ⇒ 抛 `RemoteReadError`。
- `matrix.latest_tag(remote=True)`：读不到 ⇒ 抛错，不再返回 `None`。
  `None` 只保留给「该仓真的一个 tag 都没有」。
- `gen_versions._remote_tags`：统一走 `matrix.remote_tags`，删掉静默本地回退
  （`_local_tags` 随之退役）。
- audit：四个调用点改走 `remote_latest()`，读失败**记为漂移**（该仓这一版没被
  核对过，不能算通过），不再静默跳过；报告加一行覆盖证据——
  「远端 tag 读取：N/24 仓成功（M 读失败）」。按 #75 建立的「零发现必须自带
  覆盖证据」口径：结论行「✓ 矩阵审计通过」在读不到时长得一模一样，不能自证
  「真的读了远端事实」。

#### 加剧因素：origin 协议混用

`_remote_tags` 用各 clone 自己的 origin URL。本机 27 个仓：SSH 22 / HTTPS 5。
实测 25 个含 go.mod 的仓：**SSH ≈6.5s/仓、HTTPS ≈1.2s/仓（慢 5.4 倍）**；
更要紧的是**可用性不对称**（HTTPS 走 gh 的 token，SSH 依赖本机 key/agent）
⇒ 一条 SSH 链路抽风时，**21/25 个仓的版本事实静默退化成陈旧本地 tag，
audit 照样报绿**。本轮实测未触发降级（25/25 仓 `ls-remote` 全部成功，
远端 tag 数 == 本地 tag 数）⇒ 本轮 audit 的绿是可信的——**但那是运气，不是机制**。

#### 自测与变异测试

- 新增 `scripts/remote_reads_selftest.py`（假 git + 临时目录；不联网、不依赖工作区
  布局、无副作用），8 组用例：origin 失败走 HTTPS、两条都失败必抛错、
  exit 0 + 空 stdout 是合法空事实、`latest_tag` 取最高版本、canonical 优先于
  origin、HTTPS 失败回落 origin、`gen_versions._remote_tags` 不回退本地 tag。
- 变异测试（红在**断言**层而非编译/导入层）：M1 `remote_tags` 退回「失败回退本地
  tag」⇒ B 转红；M2 `latest_tag` 退回「失败返回 None」⇒ D 转红；M3
  `gen_versions._remote_tags` 退回「失败回退本地 tag」⇒ F 转红。**3/3 转红**，
  还原后 8/8 全绿。
- 已接进 `audit.yml` 的 `selftest` job（与 `ghsafe_selftest.py` 同层）。

#### 影响面

只改**读取层**，不碰任何生成器输出——实测 `gen_versions.py --stdout` 与已发布
`versions.json` **完全一致**（忽略 `generatedAt`），`gen_matrix_go.py --check`
亦一致 ⇒ **不需要「先落产物再合生成器」的顺序**。但它改的是**门禁自己**，
所以必须带自测 + 变异测试，否则等于给门禁换了个没牙齿的实现。

详见 PR #80 / #81。

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
