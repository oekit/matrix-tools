#!/usr/bin/env python3
"""BOM 生成器 Go 地板口径的自测：证明「取不到就报错」这道判据抓得住自己要防的东西。

**为什么要有这个自测**（2026-10-25 实测，issue #100）：`gen_matrix_go.py` 的
`go_floor()` 原来在取不到 `go.mod` 时**静默兜底**成默认地板 `1.24.0`——那不是
「未知」而是**一个具体的、看起来合法的地板**，与「真的是 1.24.0 的仓」在产物里
长得一模一样，下游无从分辨。

实测已污染一个发布物：BOM **v0.1.42** 把 hertz（真实 `go 1.26.0`）写成
`1.24.0`；触发条件只是生成时本地 clone 尚未 fetch 到那个 tag。

**反证怎么构造**：把「取不到就报错」改回「兜底」，本自测必须**失败**：
  1. 本地 clone 缺该 tag ⇒ 必须抛错（且错误消息给出可执行的修复步骤），
     而不是返回某个看起来合法的地板。
  2. 该 tag 的 `go.mod` 里没有 `go` 指令 ⇒ 同样必须抛错（不能兜底）。
  3. 本地 clone 缺该 tag 但**远端有** ⇒ `ensure_local_tag` 补取后能取到真地板
     （证明 fail-loud 之外还给出了可用路径，而不是只会拦人）。

**无副作用、不联网、不依赖工作区布局**：全部在临时目录里造 fixture 仓
（`git init` + commit + tag；「远端」用本地 bare 仓当 origin，fetch 不出网）。

用法：python3 scripts/gen_matrix_go_selftest.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # matrix.py 在仓根，不在 scripts/ 下

from gen_matrix_go import ensure_local_tag, go_floor  # noqa: E402


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{r.stderr.strip()[:200]}")
    return r.stdout.strip()


def make_repo(root: Path, name: str, go_directive: str = "go 1.26.0") -> Path:
    """造一个带 go.mod 的 fixture 仓，打 tag v0.1.0。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "selftest@example.invalid")
    git(d, "config", "user.name", "selftest")
    (d / "go.mod").write_text(
        f"module example.invalid/{name}\n\n{go_directive}\n", encoding="utf-8")
    git(d, "add", "go.mod")
    git(d, "commit", "-q", "-m", "init")
    git(d, "tag", "v0.1.0")
    return d


def expect_raises(fn, *args) -> str:
    """调用 fn，断言它抛 SystemExit，并返回错误消息文本。"""
    try:
        fn(*args)
    except SystemExit as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{args} 应当抛 SystemExit，却正常返回了")


def case_local_tag_present() -> None:
    """正常路径：本地有该 tag ⇒ 取到真地板。"""
    with tempfile.TemporaryDirectory() as td:
        d = make_repo(Path(td), "normal")
        got = go_floor(str(d), "v0.1.0")
        assert got == "1.26.0", f"正常路径地板应为 1.26.0，实得 {got}"


def case_missing_tag_must_fail() -> None:
    """反证 1：本地缺该 tag ⇒ 必须抛错，且不能吐出一个「看起来合法的地板」。

    旧实现在这里返回 "1.24.0"——把这个兜底改回去，本用例必须失败。
    """
    with tempfile.TemporaryDirectory() as td:
        d = make_repo(Path(td), "missing")
        msg = expect_raises(go_floor, str(d), "v0.9.9")
        assert "fetch" in msg, f"错误消息应给出可执行的修复步骤（fetch --tags），实得：{msg[:200]}"
        assert "#100" in msg, f"错误消息应指向本 issue，便于追溯，实得：{msg[:200]}"
        for bogus in ("1.24.0", "1.26.0"):
            assert bogus not in msg, f"错误消息里不应出现任何看起来合法的地板值 {bogus}：{msg[:200]}"


def case_no_go_directive_must_fail() -> None:
    """反证 2：该 tag 的 go.mod 里没有 go 指令 ⇒ 同样必须抛错（不能兜底）。"""
    with tempfile.TemporaryDirectory() as td:
        d = make_repo(Path(td), "nogo", go_directive="// no go directive here")
        msg = expect_raises(go_floor, str(d), "v0.1.0")
        assert "#100" in msg, f"错误消息应指向本 issue，实得：{msg[:200]}"


def case_ensure_local_tag_fetches_from_origin() -> None:
    """反证 3：本地缺 tag 但 origin 有 ⇒ ensure_local_tag 补取后能取到真地板。

    「远端」用本地 bare 仓当 origin（不出网）。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        origin = root / "origin.git"
        src = make_repo(root, "src")
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        git(src, "remote", "add", "origin", str(origin))
        git(src, "push", "-q", "origin", "main", "--tags")

        clone = root / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        git(clone, "config", "user.email", "selftest@example.invalid")
        git(clone, "config", "user.name", "selftest")
        # 造出「本地缺 tag、远端还在」的现场
        git(clone, "tag", "-d", "v0.1.0")

        ensure_local_tag(str(clone), "v0.1.0")
        got = go_floor(str(clone), "v0.1.0")
        assert got == "1.26.0", f"补取后应取到真地板 1.26.0，实得 {got}"


def case_ensure_local_tag_noop_when_present() -> None:
    """本地已有该 tag ⇒ ensure_local_tag 不动网络（稳态零额外开销）。"""
    with tempfile.TemporaryDirectory() as td:
        d = make_repo(Path(td), "noop")
        # 把 origin 指到一个不存在的路径：若实现真的去 fetch，这里会失败
        git(d, "remote", "add", "origin", str(Path(td) / "does-not-exist.git"))
        ensure_local_tag(str(d), "v0.1.0")
        assert go_floor(str(d), "v0.1.0") == "1.26.0"


def case_unreadable_exits_with_code_2() -> None:
    """反证 4：读不到远端 tag ⇒ **退出码 2 + 干净消息**，不是裸 traceback。

    「读不到 ≠ 没有」（#79）：这**不是**「BOM 陈旧」，是「判不了」。
    以前这里抛裸异常，audit 只取子进程输出的最后一行当漂移消息 ⇒
    报告里出现「❌ matrix.RemoteReadError: registry: 远端 tag 读不到…」，
    读的人看不出这是读取失败而不是产物陈旧（实测 2026-10-25）。
    """
    import gen_matrix_go

    orig = gen_matrix_go.prefetch_remote_tags

    def boom(*_args, **_kwargs):
        raise gen_matrix_go.RemoteReadError(
            "registry: 远端 tag 读不到（已试 https://github.com/oekit/registry.git）"
            "——读不到 ≠ 没有，不能用本地 tag 顶替（matrix-tools#79）")

    gen_matrix_go.prefetch_remote_tags = boom
    old_argv = sys.argv
    sys.argv = ["gen_matrix_go.py", "--check"]
    try:
        rc = gen_matrix_go.main()
    finally:
        gen_matrix_go.prefetch_remote_tags = orig
        sys.argv = old_argv
    assert rc == gen_matrix_go.EXIT_UNREADABLE == 2, \
        f"读不到应退出码 2（EXIT_UNREADABLE），实得 {rc}"


def case_classify_check_kinds() -> None:
    """退出码分类：0=一致 / 1=真漂移 / 2=未核对（读不到）。"""
    import gen_matrix_go

    kind, msg = gen_matrix_go.classify_check(gen_matrix_go.EXIT_OK, "✓ 一致")
    assert kind == "ok", f"退出码 0 应判为一致，实得 {kind}"

    kind, msg = gen_matrix_go.classify_check(gen_matrix_go.EXIT_STALE, "❌ oekit BOM 陈旧：go.mod")
    assert kind == "stale", f"退出码 1 应判为真漂移，实得 {kind}"
    assert "BOM 陈旧" in msg, f"漂移消息应保留子进程原话，实得 {msg}"

    kind, msg = gen_matrix_go.classify_check(gen_matrix_go.EXIT_UNREADABLE,
                                             "读不到远端 tag ⇒ 无法核对 BOM 新鲜度")
    assert kind == "unreadable", f"退出码 2 应判为未核对，实得 {kind}"
    assert "未核对" in msg and "判不了" in msg, \
        f"未核对消息必须说清「判不了 ≠ 通过」，实得 {msg}"


CASES = [
    ("正常路径：本地有 tag ⇒ 取到真地板", case_local_tag_present),
    ("反证1：本地缺 tag ⇒ 必须抛错（不得兜底）", case_missing_tag_must_fail),
    ("反证2：go.mod 无 go 指令 ⇒ 必须抛错", case_no_go_directive_must_fail),
    ("反证3：ensure_local_tag 能从 origin 补取", case_ensure_local_tag_fetches_from_origin),
    ("稳态：tag 已在本地 ⇒ 不做额外 fetch", case_ensure_local_tag_noop_when_present),
    ("反证4：读不到远端 tag ⇒ 退出码 2 + 干净消息（非裸 traceback）", case_unreadable_exits_with_code_2),
    ("反证5：退出码分类（0/1/2 ⇒ 一致/真漂移/未核对）", case_classify_check_kinds),
]


def main() -> int:
    failed = 0
    for title, fn in CASES:
        try:
            fn()
            print(f"  ✓ {title}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {title}：{e}")
    if failed:
        print(f"\n❌ selftest 失败 {failed}/{len(CASES)}")
        return 1
    print(f"\n✓ gen_matrix_go selftest 全绿（{len(CASES)} 例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
