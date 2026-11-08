#!/usr/bin/env python3
"""tag_reachability.py 的自测（matrix-tools#240）。

**这条判据防的是什么**：旧 tag 指向的提交被历史重写丢弃后，API 侧变更日志
提取（commits/{sha} / compare）对该 tag 全线 404/422，而 go mod download 照常
工作 ⇒ 缺口完全静默，直到消费方升级轮全线断链才被发现（2026-11-08 实测）。

断言（判定层与 CLI 层各一圈，全部可判红）：
  1. commit_exists：200 ⇒ True；404 / 422 / "No commit found" ⇒ False（悬空）；
     其它失败（限速）⇒ 向上抛（判不了 ≠ 可达，fail-loud）；
  2. inspect_repo：悬空 tag 进清单、可达 tag 不进；
  3. main：全可达 ⇒ 退出码 0；有悬空 ⇒ 退出码 1 且清单点名 tag；
     API 读不到 ⇒ 退出码 2（「判不了」不许伪装成「全部可达」）；
  4. ls-remote 解析：annotated tag 用 ^{} peeled 行、轻量 tag 用本行 sha。

**变异自证**：把判定点的「404/422 ⇒ 悬空」改成恒可达（fail-open）⇒ 本自测
必须转红 ⇒ 证明「悬空检出」有牙齿。

**无副作用、不联网**：tag 表与 API 判定全注入（每个用例打补丁、finally 还原），
不真跑 git/gh。

用法：python3 scripts/tag_reachability_selftest.py [--mutate]
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import ghsafe  # noqa: E402
import tag_reachability  # noqa: E402

FAILURES: list[str] = []
REAL_COMMIT_EXISTS = tag_reachability.commit_exists
REAL_TAG_SHAS = tag_reachability.tag_commit_shas

GOOD = "1111111111111111111111111111111111111111"
BAD = "2222222222222222222222222222222222222222"
PEELED = "3333333333333333333333333333333333333333"
LIGHT = "4444444444444444444444444444444444444444"


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def restore() -> None:
    tag_reachability.commit_exists = REAL_COMMIT_EXISTS
    tag_reachability.tag_commit_shas = REAL_TAG_SHAS


def not_found_api(org: str, repo: str, sha: str) -> None:
    """api_fn 替身：恒 422（提交不可达，#240 的线上形态）。"""
    raise ghsafe.GhCommandError(["gh", "api"], 1, "",
                                f"HTTP 422: No commit found for SHA: {sha}")


def rate_limit_api(org: str, repo: str, sha: str) -> None:
    """api_fn 替身：恒 403（限速 ⇒ 判不了，fail-loud）。"""
    raise ghsafe.GhCommandError(["gh", "api"], 1, "", "HTTP 403: rate limit exceeded")


def api_from_table(table: dict[str, dict[str, bool]]):
    """api_fn 替身：按表判定；不在表里的仓/sha ⇒ 403。"""
    def _api(org: str, repo: str, sha: str) -> None:
        row = table.get(repo)
        if row is None or sha not in row:
            raise ghsafe.GhCommandError(["gh", "api"], 1, "", "HTTP 403: rate limit")
        if not row[sha]:
            raise ghsafe.GhCommandError(
                ["gh", "api"], 1, "", f"HTTP 422: No commit found for SHA: {sha}")
    return _api


def case_commit_exists() -> None:
    check(tag_reachability.commit_exists("oekit", "logx", GOOD, api_from_table(
        {"logx": {GOOD: True}})) is True, "用例1 可达的 commit 应判 True")
    check(tag_reachability.commit_exists("oekit", "logx", BAD, not_found_api) is False,
          "用例1 404/422 应判 False（悬空）")
    try:
        tag_reachability.commit_exists("oekit", "logx", GOOD, rate_limit_api)
        check(False, "用例1 限速（非 404/422）应向上抛，不是判「不可达/可达」")
    except ghsafe.GhCommandError:
        pass


def case_inspect_repo() -> None:
    restore()
    tag_reachability.tag_commit_shas = lambda repo, base=None: {
        "v0.8.2": GOOD, "hlog/v0.1.9": BAD}
    try:
        dangling, _ = tag_reachability.inspect_repo(
            "logx", None, 0, api_fn=api_from_table({"logx": {GOOD: True, BAD: False}}))
    finally:
        restore()
    check(len(dangling) == 1 and "hlog/v0.1.9" in dangling[0],
          f"用例2 只有悬空 tag 进清单且点名 tag 名：{dangling}")
    check("v0.8.2" not in dangling[0], "用例2 可达 tag 不该进清单")


def case_ls_remote_parsing() -> None:
    """ls-remote 解析：annotated tag 取 ^{} peeled 行、轻量 tag 取本行 sha。"""
    restore()
    import subprocess

    class R:
        returncode = 0
        stdout = (f"{GOOD}\trefs/tags/v0.8.2\n"
                  f"{PEELED}\trefs/tags/v0.8.2^{{}}\n"
                  f"{LIGHT}\trefs/tags/hlog/v0.1.9\n")
        stderr = ""

    real_run = subprocess.run
    real_check = subprocess.check_output

    def fake_run(args, **kw):
        if args[:2] == ["git", "ls-remote"]:
            return R()
        return real_run(args, **kw)

    def fake_check_output(args, **kw):
        raise FileNotFoundError("no origin in fixture")   # origin 读不到 ⇒ 走 canonical

    subprocess.run = fake_run
    subprocess.check_output = fake_check_output
    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "logx"
            d.mkdir()
            (d / "go.mod").write_text("module github.com/oekit/logx\n", encoding="utf-8")
            tags = tag_reachability.tag_commit_shas("logx", td)
    finally:
        subprocess.run = real_run
        subprocess.check_output = real_check
    check(tags.get("v0.8.2") == PEELED,
          f"annotated tag 必须取 ^{{}} peeled 行的 commit sha：{tags.get('v0.8.2')!r}")
    check(tags.get("hlog/v0.1.9") == LIGHT,
          f"轻量 tag 取本行 sha：{tags.get('hlog/v0.1.9')!r}")
    check(not any(k.endswith("^{}") for k in tags), "peeled 行不该出现在结果里")


def case_main_exit_codes() -> None:
    # commit_exists 的打补丁方式：**包装当前值**（可能是变异体）——判定层
    # 只换「API 调用」这一步，分类逻辑留给被测函数/变异体自己。
    def with_api(table):
        cur = tag_reachability.commit_exists
        tag_reachability.commit_exists = lambda org, repo, sha, api_fn=None: \
            cur(org, repo, sha, api_from_table(table))

    # a. 全可达 ⇒ 0
    restore()
    tag_reachability.tag_commit_shas = lambda repo, base=None: {"v1.0.0": GOOD}
    with_api({"demo": {GOOD: True}})
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = tag_reachability.main(["--repo", "demo"])
    finally:
        restore()
    check(rc == tag_reachability.EXIT_OK, f"用例3a 全可达应退出码 0：{rc}")

    # b. 有悬空 ⇒ 1 且清单点名
    restore()
    tag_reachability.tag_commit_shas = lambda repo, base=None: {
        "v1.0.0": GOOD, "v1.0.1": BAD}
    with_api({"demo": {GOOD: True, BAD: False}})
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            rc = tag_reachability.main(["--repo", "demo"])
    finally:
        restore()
    check(rc == tag_reachability.EXIT_DANGLING, f"用例3b 有悬空应退出码 1：{rc}")
    check("demo@v1.0.1" in out.getvalue(),
          f"用例3b 悬空清单必须点名 tag：{out.getvalue()[-300:]}")

    # c. API 读不到 ⇒ 2（判不了 ≠ 可达）
    restore()
    tag_reachability.tag_commit_shas = lambda repo, base=None: {"v1.0.0": GOOD}
    with_api({"demo": {}})   # sha 不在表里 ⇒ 403（限速形态）
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = tag_reachability.main(["--repo", "demo"])
    finally:
        restore()
    check(rc == tag_reachability.EXIT_UNREADABLE,
          f"用例3c API 读不到应退出码 2（不许当「全部可达」）：{rc}")


def main() -> None:
    mutated = "--mutate" in sys.argv
    if mutated:
        # ★ 变异（fail-open）：把「API 读不到 ⇒ fail-loud（判不了 ≠ 可达）」
        #   改成「读不到也当可达」⇒ 限速/网络抖动时巡检静默放行——这正是
        #   #240 巡检要防的静默失效方向（判据只在出事时才见分晓的那类）。
        def fail_open(org, repo, sha, api_fn=None):
            try:
                return REAL_COMMIT_EXISTS(org, repo, sha, api_fn)
            except ghsafe.GhCommandError:
                return True
        tag_reachability.commit_exists = fail_open
        print("== 变异模式：API 读不到也当「可达」（fail-open）==")
    case_commit_exists()
    case_inspect_repo()
    if not mutated:
        case_ls_remote_parsing()
    case_main_exit_codes()
    restore()
    if mutated:
        if FAILURES:
            print(f"✓ 变异被抓住（{len(FAILURES)} 条断言转红）")
            return
        print("✗ 变异没有让任何断言失败 ⇒ 判据没牙齿")
        raise SystemExit(1)
    if FAILURES:
        print("✗ 自测失败：")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("✓ tag 可达性自测通过：404/422 ⇒ 悬空、限速 ⇒ 判不了（fail-loud）、"
          "退出码 0/1/2 语义正确、悬空清单点名 tag")


if __name__ == "__main__":
    main()
