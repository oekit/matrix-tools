#!/usr/bin/env python3
"""flake_scan「判据不可用即红」自测（matrix-tools#313）。

被测对象：`flake_scan.scan_one` 的三类「没跑起来」与一类「没扫」上报，
以及 `flake_scan.main` 的退出码口径。

为什么要有这个自测（#313 实录）：`scan_one` 的三个 git 前置命令
（fetch / checkout / pull）**全部不看 returncode**，注释里自己点名了
「2026-10-30 陈旧 checkout ⇒ 假报」事故，却没有任何一行代码保证这三条
命令真的成功了。失效面逐条对应注释里的承诺：

| 形态 | 修复前 | 修复后 |
|---|---|---|
| fetch 失败（网络/限速）被吞 | 跑在落后提交的旧 main 上，结果冠以「全矩阵 flaky 扫描」 | 没跑起来 + git stderr 摘要，exit 1 |
| checkout 失败被吞 | 在**别的分支**上跑 -race -count=N | 同上 |
| pull --ff-only 失败被吞 | 同 fetch（本地分叉） | 同上 |
| 缺仓（repo_dir 抛 FileNotFoundError） | 异常穿过 fut.result() **打崩整轮**；原 `if not d.exists()` 是永不可达死分支 | 该仓没跑起来，整轮继续 |
| 在途登记持有该仓 | 直接 checkout/pull **切走对方现场**（inflight.py 文档点名要防的撞车） | 跳过并标注原因，不碰检出 |

三者修复前都会被记成「无失败用例」或整轮崩——与 #139「没跑起来 ≠ 无失败
用例」口径相反。本自测用**假 git + 假 go**（PATH 前置）伪造现场，断言
`main()` 的退出码与文案。

**不联网、不依赖工作区布局、无副作用**：全程临时目录 + PATH 前置假命令，
inflight 登记目录重定向到临时目录（不写真 `.in-flight/`）。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import flake_scan  # noqa: E402
import inflight  # noqa: E402

FAKE_GIT = r"""#!/bin/sh
# 假 git：记调用日志，按 FAKE_GIT_FAIL 指定的子命令失败。
if [ -n "$FAKE_GIT_LOG" ]; then printf '%s\n' "$*" >> "$FAKE_GIT_LOG"; fi
for a in "$@"; do
  case "$a" in
    fetch|checkout|pull)
      if [ "${FAKE_GIT_FAIL-}" = "$a" ]; then
        echo "fatal: simulated $a failure (network/auth/diverged)" >&2
        exit 1
      fi
      ;;
  esac
done
exit 0
"""

FAKE_GO = r"""#!/bin/sh
# 假 go：记调用日志，按 FAKE_GO_FAIL 给出「用例失败 / 构建失败 / 干净」三种输出。
if [ -n "$FAKE_GO_LOG" ]; then printf '%s\n' "$*" >> "$FAKE_GO_LOG"; fi
case "${FAKE_GO_FAIL-}" in
  test)
    echo "--- FAIL: TestFlakyProbe"
    echo "FAIL	example/pkg	0.01s"
    exit 1 ;;
  build)
    echo "# example/pkg"
    echo "./x.go:1:1: syntax error: unexpected token"
    exit 1 ;;
esac
echo "ok  	example/pkg	0.01s"
exit 0
"""


class Bench:
    """一次性的假命令 + 临时检出根 + inflight 目录重定向。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        bindir = tmp / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        for name, body in (("git", FAKE_GIT), ("go", FAKE_GO)):
            p = bindir / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)
        self.base = tmp / "matrix"
        self.base.mkdir(parents=True, exist_ok=True)
        self.gitlog = tmp / "git.log"
        self.golog = tmp / "go.log"
        self._saved = dict(os.environ)
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_GIT_LOG"] = str(self.gitlog)
        os.environ["FAKE_GO_LOG"] = str(self.golog)
        os.environ.pop("FAKE_GIT_FAIL", None)
        os.environ.pop("FAKE_GO_FAIL", None)
        # inflight.REGISTRY 指向临时目录：自测绝不写真 .in-flight/
        self._saved_registry = inflight.REGISTRY
        inflight.REGISTRY = tmp / "in-flight"
        self._saved_argv = sys.argv

    def restore(self):
        inflight.REGISTRY = self._saved_registry
        sys.argv = self._saved_argv
        os.environ.clear()
        os.environ.update(self._saved)

    def add_repo(self, name: str) -> Path:
        d = self.base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "go.mod").write_text(f"module example/{name}\n", encoding="utf-8")
        return d

    def git_calls(self) -> list[str]:
        if not self.gitlog.exists():
            return []
        return self.gitlog.read_text(encoding="utf-8").splitlines()

    def go_calls(self) -> list[str]:
        if not self.golog.exists():
            return []
        return self.golog.read_text(encoding="utf-8").splitlines()

    def claim(self, repo: str, note: str = "并行会话正在动这个仓"):
        inflight.REGISTRY.mkdir(parents=True, exist_ok=True)
        import json
        import time
        p = inflight.REGISTRY / f"{repo}.json"
        p.write_text(json.dumps({"repo": repo, "note": note,
                                 "pid": os.getpid(), "ts": time.time()}),
                     encoding="utf-8")

    def run_main(self, repos: list[str]) -> tuple[int, str]:
        """跑 flake_scan.main()，返回 (退出码, stdout)。"""
        sys.argv = ["flake_scan.py", "--base", str(self.base),
                    "--repos", ",".join(repos), "--count", "1", "--jobs", "2"]
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                flake_scan.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        return code, buf.getvalue()


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("A. fetch 失败 ⇒ 没跑起来（带 git stderr 摘要），exit 1，不报「无失败用例」")
def case_a(b: Bench):
    b.add_repo("pkg")
    os.environ["FAKE_GIT_FAIL"] = "fetch"
    code, out = b.run_main(["pkg"])
    assert code == 1, f"fetch 失败必须 exit 1，got {code}"
    assert "没跑起来" in out, f"必须标「没跑起来」：\n{out}"
    assert "git fetch 失败" in out, f"必须带 git fetch 失败摘要：\n{out}"
    assert "simulated fetch failure" in out, f"必须带 stderr 摘要：\n{out}"
    assert "✓ 全矩阵无失败用例" not in out, f"绝不能判绿（无失败用例）：\n{out}"
    assert b.go_calls() == [], \
        f"fetch 失败后不该继续跑 go test：{b.go_calls()}"


@case("B. checkout 失败 ⇒ 没跑起来（不在 main 上就不测），exit 1")
def case_b(b: Bench):
    b.add_repo("pkg")
    os.environ["FAKE_GIT_FAIL"] = "checkout"
    code, out = b.run_main(["pkg"])
    assert code == 1, f"checkout 失败必须 exit 1，got {code}"
    assert "git checkout 失败" in out, f"必须带 git checkout 失败摘要：\n{out}"
    assert "✓ 全矩阵无失败用例" not in out, f"绝不能判绿（无失败用例）：\n{out}"
    assert b.go_calls() == [], \
        f"checkout 失败后不该继续跑 go test：{b.go_calls()}"


@case("C. pull --ff-only 失败（本地分叉）⇒ 没跑起来，exit 1")
def case_c(b: Bench):
    b.add_repo("pkg")
    os.environ["FAKE_GIT_FAIL"] = "pull"
    code, out = b.run_main(["pkg"])
    assert code == 1, f"pull 失败必须 exit 1，got {code}"
    assert "git pull 失败" in out, f"必须带 git pull 失败摘要：\n{out}"
    assert "✓ 全矩阵无失败用例" not in out, f"绝不能判绿（无失败用例）：\n{out}"


@case("D. 缺仓（repo_dir 抛 FileNotFoundError）⇒ 该仓没跑起来，整轮不崩")
def case_d(b: Bench):
    # 缺仓仓 + 正常仓：单仓缺失不得打崩整轮，正常仓结果必须照常产出
    b.add_repo("good")
    code, out = b.run_main(["ghost", "good"])
    assert code == 1, f"有仓没跑起来必须 exit 1，got {code}"
    assert "ghost" in out and "仓检出缺失" in out, \
        f"缺仓必须按「没跑起来」上报：\n{out}"
    assert "✓ 全矩阵无失败用例" not in out, f"绝不能判绿（无失败用例）：\n{out}"
    # 整轮继续：正常仓被扫到（假 git 留下它的调用痕迹）
    assert any(str(b.base / "good") in c for c in b.git_calls()), \
        f"缺仓不该中断整轮，good 仓应被扫描：{b.git_calls()}"


@case("E. 在途登记持有 ⇒ 跳过并标注原因，**不碰对方检出**，exit 1")
def case_e(b: Bench):
    b.add_repo("held")
    b.claim("held")
    code, out = b.run_main(["held"])
    assert code == 1, f"没扫到必须 exit 1（不是无失败用例），got {code}"
    assert "跳过" in out and "在途" in out, f"必须标「跳过/在途」：\n{out}"
    assert "✓ 全矩阵无失败用例" not in out, f"绝不能判绿（无失败用例）：\n{out}"
    assert b.git_calls() == [], \
        f"在途仓一个 git 命令都不许跑（会切走对方现场）：{b.git_calls()}"


@case("F. 陈旧在途登记（pid 0 + 过期）⇒ 不拦，正常扫描")
def case_f(b: Bench):
    import json
    import time
    b.add_repo("stale")
    inflight.REGISTRY.mkdir(parents=True, exist_ok=True)
    p = inflight.REGISTRY / "stale.json"
    p.write_text(json.dumps({"repo": "stale", "note": "死进程遗留",
                             "pid": 0, "ts": time.time() - 5 * 3600}),
                 encoding="utf-8")
    code, out = b.run_main(["stale"])
    assert code == 0, f"陈旧登记不该拦扫描，got exit {code}:\n{out}"
    assert "全矩阵无失败用例" in out, f"干净仓应判绿：\n{out}"


@case("G. 干净仓 ⇒ exit 0 + 「✓ 全矩阵无失败用例」（正常路径不误伤）")
def case_g(b: Bench):
    b.add_repo("clean")
    code, out = b.run_main(["clean"])
    assert code == 0, f"干净仓必须 exit 0，got {code}:\n{out}"
    assert "全矩阵无失败用例" in out, f"干净仓应判绿：\n{out}"
    calls = b.git_calls()
    assert len([c for c in calls if "clean" in c]) == 3, \
        f"fetch/checkout/pull 三条都要跑：{calls}"


@case("H. 用例失败 ⇒ exit 1 + 列出 --- FAIL（既有口径不回退）")
def case_h(b: Bench):
    b.add_repo("flaky")
    os.environ["FAKE_GO_FAIL"] = "test"
    code, out = b.run_main(["flaky"])
    assert code == 1, f"用例失败必须 exit 1，got {code}"
    assert "--- FAIL: TestFlakyProbe" in out, f"必须列出失败用例：\n{out}"
    assert "✓ 全矩阵无失败用例" not in out, f"绝不能判绿（无失败用例）：\n{out}"


@case("I. 构建失败（go 非零且无 --- FAIL）⇒ 没跑起来，不记「无失败用例」")
def case_i(b: Bench):
    b.add_repo("broken")
    os.environ["FAKE_GO_FAIL"] = "build"
    code, out = b.run_main(["broken"])
    assert code == 1, f"构建失败必须 exit 1，got {code}"
    assert "没跑起来" in out, f"必须标「没跑起来」：\n{out}"
    assert "✓ 全矩阵无失败用例" not in out, f"绝不能判绿（无失败用例）：\n{out}"


@case("J. scan_one 返回四元组：在途仓给出跳过原因，git 三步全成功才进 go test")
def case_j(b: Bench):
    d = b.add_repo("ok")
    repo, names, not_run_err, skip_err = flake_scan.scan_one("ok", str(b.base), 1)
    assert (repo, names, not_run_err, skip_err) == ("ok", [], "", ""), \
        f"干净仓应全空：{(repo, names, not_run_err, skip_err)}"
    calls = b.git_calls()
    assert len(calls) == 3 and all(str(d) in c for c in calls), \
        f"三条 git 前置都要打到该仓：{calls}"
    assert len(b.go_calls()) == 1, f"go test 应在 git 全成功后跑一次：{b.go_calls()}"


def main() -> int:
    print("flake_scan_selftest：#313 判据不可用即红（git 前置 / 缺仓 / 在途）")
    failed = []
    for name, fn in CASES:
        with tempfile.TemporaryDirectory() as td:
            b = Bench(Path(td))
            try:
                fn(b)
                print(f"  ✓ {name}")
            except AssertionError as e:
                failed.append(name)
                print(f"  ❌ {name}\n     {e}")
            except Exception as e:  # noqa: BLE001
                failed.append(name)
                print(f"  ❌ {name}（异常）\n     {type(e).__name__}: {e}")
            finally:
                b.restore()
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
