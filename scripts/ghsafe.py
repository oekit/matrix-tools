#!/usr/bin/env python3
"""gh 远端读取层：把「这次读到空」与「事实为空」分开。

**实测（2026-10-22 oekit 巡检）**：GitHub API 会**间歇性返回空集合**——
不是限速、不是权限、不是客户端解析错，而是服务端这一次就返回了空：

- `GET /repos/oekit/core/releases` ⇒ 实测 10 次里 2 次返回 `200` + `[]`（5 字节）；
  同一 URL 用 `curl` 带 token 直连**同样能复现** ⇒ 排除了 gh 客户端与本地网络；
- `POST /graphql`（ProjectV2 items）⇒ 实测 10 次里 2 次返回 `totalCount=0, nodes=[]`；
- `gh api --paginate ... --jq '.[].tag_name'` ⇒ 读到 `[]` ⇒ jq 过滤后**零行输出**，
  于是调用方看到的是「exit 0 + 空输出」。

一旦把这个「空」当成事实，就会得出**完全相反**的结论：

| 读什么 | 空被当成 | 后果（实测） |
|---|---|---|
| Release 列表 | 「这仓一个 Release 都没有」 | audit 报**假漂移**：11 个仓被报「N 个 tag 缺 Release」，而真实 parity 是 **24/24 全绿** |
| issue 元数据 | 「这不是 PR」 | `close_issue` 对**未合入的 PR** 调 `gh issue close`（v0.1.8 刚修掉的坑从这条缝复活） |
| 分支保护 | 「这仓没有保护」 | 按「无保护」去改配置 |

⇒ 本模块的统一口径：

1. **命令非零** 与 **正文为空/不可解析** 是两回事，分别用 `GhCommandError`
   与 `GhReadError` 表达——调用方不能再把两者一起 `except` 成「没有」。
2. **空结果不采信**：要读到空，得**连续 N 轮都是空**才认（`read_until_nonempty`）；
   中间任何一轮拿到非空就立刻返回。
3. **读不到就报错**，不猜：宁可脚本失败，也不把「读不到」当成「没有」。

配套自测：`python3 scripts/ghsafe_selftest.py`（假 gh，无副作用、不联网）。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable

# 像「瞬时错误」的特征：限速 / 5xx / 超时 / 连接被掐。
# 命中这些才值得重试；404、权限不足这类**确定性**失败要立刻抛。
#
# ⚠️ EOF 的两种真实格式都要覆盖（2026-10-30 实测）：
#   - `failed to get run: Get "https://api.github.com/...": EOF` ← **真实 gh**
#   - `unexpected EOF`
# 原先只写 `unexpected EOF` ⇒ **真实 gh 的报错格式匹配不上** ⇒ 读取层遇到
# GitHub API 的间歇性 EOF 时**不重试、直接抛** ⇒ 整条回执/巡检链被打死
# （本次实测：close_issue 读 issue 元数据的 `gh api` 直接 EOF 失败）。
# ⇒ 写成「可选的 unexpected + EOF」，两种都匹配。
TRANSIENT_RE = re.compile(
    r"rate limit|abuse detection|secondary rate|abuse|"
    r"\b5\d\d\b|timed out|timeout|connection reset|connection refused|"
    r"temporarily unavailable|(?:\bunexpected\s+)?\bEOF\b",
    re.I,
)

# 「连续几轮都空才认空」的默认轮数。
# 实测单次读的空率约 20~30% ⇒ 4 轮全空的概率 ≈ 0.2^4 ≈ 0.16%，
# 两条独立路径（GraphQL / REST）交叉后更低。
EMPTY_ROUNDS = 4


class GhCommandError(RuntimeError):
    """gh 命令返回**非零**退出码：404 / 权限不足 / 限速……

    调用方自己判别：404 类「东西不存在」可以当 None 处理；
    其它错误应当显式失败，而不是当成「没有」。
    """

    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str):
        self.cmd_args = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = (stderr or stdout or "").strip()[:300]
        super().__init__(f"gh {' '.join(args)} 退出码 {returncode}：{detail or '（无输出）'}")

    @property
    def is_not_found(self) -> bool:
        """是不是 404（东西不存在）——可以当「没有」处理的**唯一**确定性失败。"""
        blob = f"{self.stderr}\n{self.stdout}"
        return "404" in blob or "Not Found" in blob


class GhReadError(RuntimeError):
    """命令成功但**正文不可用**（空正文 / 不可解析 / 空集合）。

    这类错误**必须**与「远端确实没有这个东西」区分开——
    GitHub 会偶发返回空集合（见模块文档），当成事实就是假红 / 误判。
    """


def _is_empty_container(data: Any) -> bool:
    return data is None or data == [] or data == {} or data == ""


def run_gh(args: list[str], *, tries: int = 3, backoff: float = 0.4) -> tuple[str, str]:
    """跑 `gh <args>`，返回 (stdout, stderr)。

    - 退出码 0 ⇒ 直接返回（正文可能为空，由调用方的上层判读）；
    - 退出码非 0 且**像瞬时错误**（限速/5xx/超时）⇒ 重试；
    - 退出码非 0 且是确定性失败（404/权限）⇒ 立刻抛 `GhCommandError`。
    """
    last: GhCommandError | None = None
    for i in range(max(1, tries)):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout, p.stderr
        last = GhCommandError(args, p.returncode, p.stdout, p.stderr)
        detail = f"{p.stderr}\n{p.stdout}"
        if not TRANSIENT_RE.search(detail):
            raise last
        time.sleep(backoff * (i + 1))
    raise last if last else GhCommandError(args, -1, "", "（未执行）")


def gh_json(args: list[str], *, tries: int = 3, backoff: float = 0.4,
            allow_empty_collection: bool = False) -> Any:
    """跑 gh 取 JSON。

    - 命令非零 ⇒ `GhCommandError`（瞬时错误已在内部重试）
    - 正文为空 / 不可解析 ⇒ `GhReadError`（内部重试后仍如此）
    - `allow_empty_collection=False`（默认）时，**空容器**（`[]` / `{}` / `""`）
      也算「读不到」⇒ 重试 ⇒ 最终 `GhReadError`。
      查「某个东西在不在」时**不要**传 True：空正是要防的误判来源。
    """
    last: Exception | None = None
    for i in range(max(1, tries)):
        try:
            out, _err = run_gh(args, tries=1, backoff=backoff)
        except GhCommandError as e:
            if not TRANSIENT_RE.search(f"{e.stderr}\n{e.stdout}"):
                raise
            last = e
            time.sleep(backoff * (i + 1))
            continue
        body = out.strip()
        if not body:
            last = GhReadError(f"gh {' '.join(args)} 正文为空")
            time.sleep(backoff * (i + 1))
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            last = GhReadError(f"gh {' '.join(args)} 正文不可解析：{e}")
            time.sleep(backoff * (i + 1))
            continue
        if not allow_empty_collection and _is_empty_container(data):
            last = GhReadError(f"gh {' '.join(args)} 读到空集合")
            time.sleep(backoff * (i + 1))
            continue
        return data
    raise last if last else GhReadError(f"gh {' '.join(args)} 读不到内容")


_AUTH_CACHE: bool | None = None


def gh_authenticated() -> bool:
    """gh 是否已认证（env token **或** gh 配置里的账号）。

    为什么要有这个：旧实现只看 env 里的 `GH_TOKEN`/`GITHUB_TOKEN`，
    看不见「gh 已登录（keyring / hosts.yml）」这种**常态**
    ⇒ 本机明明有认证却被当成匿名，于是走了最容易抖的那条 REST 路径，
    且本可以用的 GraphQL 路径（实测 10/10 稳定）根本没被用上。

    结果进程内缓存：认证状态在一次运行里不会变。
    """
    global _AUTH_CACHE
    if _AUTH_CACHE is None:
        p = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        _AUTH_CACHE = p.returncode == 0
    return _AUTH_CACHE


def read_until_nonempty(read_fn: Callable[[], Any], *, rounds: int = EMPTY_ROUNDS,
                        backoff: float = 0.4) -> Any:
    """反复调用 `read_fn()`，直到拿到**非空**结果。

    连续 `rounds` 轮都是空 ⇒ 认定「事实为空」，返回最后一次的空结果。

    这是区分「GitHub 偶发返回空集合」与「这仓真的什么都没有」的唯一办法：
    - 真的没有 ⇒ 空是稳定的 ⇒ rounds 轮后如实返回空 ⇒ 门禁照常报漂移；
    - 偶发空 ⇒ 中途某轮就拿到数据 ⇒ 立即返回 ⇒ 门禁不会假红。
    """
    result: Any = None
    for i in range(max(1, rounds)):
        try:
            result = read_fn()
        except GhReadError:
            result = None
        if result:
            return result
        time.sleep(backoff * (i + 1))
    return result
