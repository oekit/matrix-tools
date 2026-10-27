#!/usr/bin/env python3
"""远端 tag 缓存（matrix.enable_tag_cache）与并发预取的自测。

**为什么要有这个自测**（2026-10-25 实测）：一次审计里，同一个仓的远端 tag
会被问很多次——audit.py 有 4 个调用点（README 安装行 / AGENTS 当前版本行 /
站点 versions.json / 嵌套模块父模块地板），gen_releases 的 Release 对账还要
**每个仓的完整 tag 列表**。`matrix.remote_tags()` 每次都真的打一次
`git ls-remote` ⇒ 同一份事实重复读 N 遍：实测一次审计 96 次 + BOM 子进程 48 次
⇒ 审计要 281s，本地/cron 的后台审计任务直接 **900s 超时**。

**反证怎么构造**：把缓存去掉（每次都真读），本自测必须**失败**：
  1. 同一仓连读 4 次 ⇒ 网络读必须只发生 **1** 次（去掉缓存 ⇒ 4 次 ⇒ FAIL）；
  2. 读失败 ⇒ **重抛同一个错误**，且后续再读**不再打网络**
     （失败仓恰恰最慢：要依次试 canonical HTTPS 和 origin，两条都失败才抛）；
  3. 读失败**不得**变成「这个仓没有 tag」（读到空 ≠ 读不到，#79）；
  4. 缓存**默认关** ⇒ 默认行为下每次都真读（发布类脚本靠这个保证产物新鲜）；
  5. `prefetch_remote_tags` 必须**并发**：用 `threading.Barrier` 卡住每个读——
     若退化成串行，barrier 会超时炸掉（确定性反证，不依赖墙钟阈值）；
  6. 预取的失败回调顺序必须**确定**（按传入 repos 的顺序，不按完成顺序）。

**无副作用、不联网、不依赖工作区布局**：`_ls_remote_tags` 被换成内存里的假实现，
仓库布局用临时目录造（`repo_dir` 需要 go.mod/README 才认这个仓）。

用法：python3 scripts/tag_cache_selftest.py
"""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # matrix.py 在仓根，不在 scripts/ 下

import matrix  # noqa: E402


class FakeRemote:
    """把 `_ls_remote_tags` 换成内存假实现，并计数。"""

    def __init__(self, tags=None, fail=()):
        self.tags = tags or {}
        self.fail = set(fail)
        self.calls: list[str] = []
        self.lock = threading.Lock()
        self._orig = matrix._ls_remote_tags

    def __enter__(self):
        def fake(url, timeout=60):
            with self.lock:
                self.calls.append(url)
            repo = url.rsplit("/", 1)[-1].removesuffix(".git")
            if repo in self.fail:
                return [], False
            return list(self.tags.get(repo, [])), True

        matrix._ls_remote_tags = fake
        return self

    def __exit__(self, *exc):
        matrix._ls_remote_tags = self._orig
        return False

    @property
    def n(self) -> int:
        return len(self.calls)


class FakeSubprocess:
    """把 `matrix.subprocess` 换成可控假实现：ls-remote 前 n 次失败、之后成功。

    用来验**重试**（2026-10-27 加）：重试逻辑住在 `_ls_remote_tags` 内部，
    而上一套用例把整个 `_ls_remote_tags` 换掉了 ⇒ 验不到重试本身。
    """

    def __init__(self, fail_times: int, *, tags=None):
        self.fail_times = fail_times
        self.tags = tags or ["v0.1.0"]
        self.calls = 0
        self._orig_subprocess = matrix.subprocess
        self._orig_sleep = matrix.time.sleep

    def __enter__(self):
        outer = self

        class _Result:
            def __init__(self, code, out):
                self.returncode = code
                self.stdout = out
                self.stderr = ""

        def run(cmd, **kwargs):
            outer.calls += 1
            if outer.calls <= outer.fail_times:
                return _Result(128, "")
            out = "".join(f"deadbeef\trefs/tags/{t}\n" for t in outer.tags)
            return _Result(0, out)

        def check_output(cmd, **kwargs):
            return "https://github.com/oekit/origin-fallback.git\n"

        matrix.subprocess = types.SimpleNamespace(
            run=run, check_output=check_output,
            # remote_tags 取 origin URL 时会引用 subprocess.DEVNULL ⇒ 假实现必须给全，
            # 否则 origin 兜底那条 URL 根本走不到（断言「2 URL × 3 次」会暴露这点）。
            DEVNULL=-3, PIPE=-1, STDOUT=-2,
        )
        matrix.time.sleep = lambda *_: None  # 退避别把自测拖慢
        return self

    def __exit__(self, *exc):
        matrix.subprocess = self._orig_subprocess
        matrix.time.sleep = self._orig_sleep
        return False


def make_base(root: Path, repos) -> Path:
    """造一个矩阵检出布局：每个仓至少有 go.mod，repo_dir 才认它。"""
    for repo in repos:
        d = root / repo
        d.mkdir(parents=True, exist_ok=True)
        (d / "go.mod").write_text(f"module github.com/oekit/{repo}\n", encoding="utf-8")
    return root


def case_cache_reads_once() -> None:
    """反证 1：开了缓存后，同一仓连读 4 次 ⇒ 只打 1 次网络。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = make_base(Path(td), ["pkg"])
        matrix.clear_tag_cache()
        matrix.enable_tag_cache()
        try:
            with FakeRemote({"pkg": ["v0.1.0"]}) as fr:
                for _ in range(4):
                    tags = matrix.remote_tags("pkg", str(base))
                    assert tags == ["v0.1.0"], f"取到的 tag 应一致，实得 {tags}"
                assert fr.n == 1, f"同一仓应只打 1 次网络，实打 {fr.n} 次（去掉缓存会退化成 4 次）"
        finally:
            matrix.disable_tag_cache()


def case_failure_reraised_not_empty() -> None:
    """反证 2+3：读失败 ⇒ 重抛错误（**不是**空列表），且不再打网络。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = make_base(Path(td), ["dead"])
        matrix.clear_tag_cache()
        matrix.enable_tag_cache()
        try:
            with FakeRemote(fail={"dead"}) as fr:
                for _ in range(3):
                    try:
                        got = matrix.remote_tags("dead", str(base))
                        raise AssertionError(f"读失败应抛 RemoteReadError，却返回了 {got!r}")
                    except matrix.RemoteReadError:
                        pass
                assert fr.n == 1, f"失败仓重读是最贵的一档（要试两条 URL），应只打 1 次，实打 {fr.n} 次"
        finally:
            matrix.disable_tag_cache()


def case_cache_off_by_default() -> None:
    """反证 4：缓存**默认关** ⇒ 默认行为下每次都真读（发布类脚本靠这个保新鲜）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = make_base(Path(td), ["pkg"])
        matrix.clear_tag_cache()
        matrix.disable_tag_cache()
        with FakeRemote({"pkg": ["v0.1.0"]}) as fr:
            matrix.remote_tags("pkg", str(base))
            matrix.remote_tags("pkg", str(base))
            assert fr.n == 2, f"默认（缓存关）应每次都真读，实打 {fr.n} 次"


def case_prefetch_is_concurrent() -> None:
    """反证 5：prefetch_remote_tags 必须**并发**（barrier 卡住 ⇒ 串行会炸）。

    ⚠️ barrier 只在**预取那一轮**生效：barrier 是可复用的（凑齐一轮就重置），
    后面再读若还去等它，主线程会自己把自己卡死 ⇒ 那测的是缓存不是并发。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repos = ["a", "b", "c"]
        base = make_base(Path(td), repos)
        gate = threading.Barrier(3, timeout=10)
        gate_on = threading.Event()
        gate_on.set()
        matrix.clear_tag_cache()
        matrix.enable_tag_cache()
        try:
            with FakeRemote({r: ["v0.1.0"] for r in repos}) as fr:
                orig = matrix._ls_remote_tags

                def gated(url, timeout=60):
                    if gate_on.is_set():
                        gate.wait()  # 三个读必须同时到达 ⇒ 串行必然 BrokenBarrierError
                    return orig(url, timeout)

                matrix._ls_remote_tags = gated
                matrix.prefetch_remote_tags(repos, str(base))
                gate_on.clear()  # 预取这一轮结束后不再参与 barrier
                assert fr.n == 3, f"预取应各读 1 次，实打 {fr.n} 次"
                for r in repos:
                    assert matrix.remote_tags(r, str(base)) == ["v0.1.0"]
                assert fr.n == 3, f"预取后再取应全是缓存命中，实打 {fr.n} 次"
        finally:
            matrix.disable_tag_cache()


def case_prefetch_fail_order_deterministic() -> None:
    """反证 6：预取的失败回调顺序 = 传入 repos 的顺序（不是完成顺序）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repos = ["bad1", "good", "bad2"]
        base = make_base(Path(td), repos)
        matrix.clear_tag_cache()
        matrix.enable_tag_cache()
        try:
            with FakeRemote({"good": ["v0.1.0"]}, fail={"bad1", "bad2"}) as fr:
                seen: list[str] = []
                matrix.prefetch_remote_tags(repos, str(base),
                                            on_fail=lambda repo, e: seen.append(repo))
                assert seen == ["bad1", "bad2"], f"失败回调顺序应按 repos 序，实得 {seen}"
                assert fr.n == 3, f"预取应各读 1 次，实打 {fr.n} 次"
        finally:
            matrix.disable_tag_cache()


def case_transient_failure_retried() -> None:
    """反证 7：远端读遇到**瞬时**失败必须重试，不能一次就死。

    2026-10-27 实测踩过：gormx 发版后置链跑到「站点 versions.json」那步时，
    middleware 的远端 tag 读不到（一次网络抖动）⇒ 整条链死掉，版本行 PR 开了
    而站点/BOM 都没开，只能整条重跑。
    """
    with FakeSubprocess(fail_times=2) as fs:
        tags, ok = matrix._ls_remote_tags("https://github.com/oekit/pkg.git")
        assert ok, "前两次失败、第三次成功 ⇒ 应读成功（一次就死会让整条发版链崩）"
        assert tags == ["v0.1.0"], f"应读到 tag，实得 {tags}"
        assert fs.calls == 3, f"应尝试 3 次，实试 {fs.calls} 次（去掉重试会退化成 1 次）"


def case_permanent_failure_still_raises() -> None:
    """反证 8：全试完仍失败 ⇒ **仍然抛 RemoteReadError**（#79 口径不变）。

    重试只吸收瞬时抖动，**不得**把「读不到」降级成「没有 tag」——
    降级会把「远端已发新版、本地没 fetch」这个 #79 明说要防的形态放回来，
    且无任何告警。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = make_base(Path(td), ["dead"])
        matrix.clear_tag_cache()
        try:
            with FakeSubprocess(fail_times=99) as fs:
                try:
                    got = matrix.remote_tags("dead", str(base))
                    raise AssertionError(f"读失败应抛 RemoteReadError，却返回了 {got!r}")
                except matrix.RemoteReadError:
                    pass
                # 两个 URL（canonical + origin 兜底）× 3 次 = 6 次
                assert fs.calls == 6, f"应试 2 URL × 3 次 = 6 次，实试 {fs.calls} 次"
        finally:
            matrix.clear_tag_cache()


CASES = [
    ("反证1：开了缓存 ⇒ 同一仓连读 4 次只打 1 次网络", case_cache_reads_once),
    ("反证2/3：读失败重抛同一个错误，不重打网络，也不变成空列表", case_failure_reraised_not_empty),
    ("反证4：缓存默认关 ⇒ 默认每次都真读", case_cache_off_by_default),
    ("反证5：prefetch_remote_tags 必须并发（barrier 卡住）", case_prefetch_is_concurrent),
    ("反证6：预取的失败回调顺序确定（按 repos 序）", case_prefetch_fail_order_deterministic),
    ("反证7：远端读遇到瞬时失败必须重试（不能一次就死）", case_transient_failure_retried),
    ("反证8：全试完仍失败 ⇒ 仍抛 RemoteReadError（#79 口径不变）", case_permanent_failure_still_raises),
]


def main() -> int:
    failed = 0
    for title, fn in CASES:
        try:
            fn()
            print(f"  ✓ {title}")
        except Exception as e:  # noqa: BLE001 - 自测用例里任何异常都是失败
            failed += 1
            print(f"  ✗ {title}：{type(e).__name__}: {e}")
    if failed:
        print(f"\n❌ selftest 失败 {failed}/{len(CASES)}")
        return 1
    print(f"\n✓ tag_cache selftest 全绿（{len(CASES)} 例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
