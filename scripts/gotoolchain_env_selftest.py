#!/usr/bin/env python3
"""go 子进程环境与 tidy 收录窗口的自测（matrix-tools#249 / #245）。

**#249（GOTOOLCHAIN 钉住）**：release_train / align_floors 的本地门禁此前只设
GOWORK=off ⇒ 判定结论取决于发版机器上装的 Go（gormx v0.11.6 实测：同一份代码
本机 go1.27 判 3 issues、CI 钉的 go1.24 判 0 ⇒ 本地更严挡发版、更松门禁形同
虚设）。修法：按**仓 go.mod 的 go 指令**换算并钉 GOTOOLCHAIN（与 org 共享 CI
同一套换算，两段补 .0）。

**#245（sumdb 收录窗口）**：级联发版「刚发完版就抬下游地板」时，新版本往往
已在模块代理上但 checksum DB 还没收录 ⇒ tidy 的 sumdb lookup 404。修法两层：
GONOSUMDB=github.com/oekit/*（策略层）+ 命中窗口特征时退避重试、耗尽后报错
文案点名真实原因（语义层）。

断言（每条都要能判红）：
  1. go_toolchain_from_gomod：`go 1.24` → go1.24.0（两段补 .0）；`go 1.24.0`
     → go1.24.0；无 go 指令 ⇒ None（不钉）；
  2. align_floors.go_env_for / release_train.go_env：夹具 go.mod ⇒ env 带
     GOTOOLCHAIN=go1.24.0 + GOWORK=off + GONOSUMDB=github.com/oekit/*；
  3. 调用方已自设校验策略（GOSUMDB=off / GONOSUMDB 别的值 / GOPRIVATE）⇒
     原样尊重（不被覆盖）；
  4. run_tidy 重试：sumdb 窗口失败 → 重试后成功 ⇒ rc 0 且真的重试了；
     重试耗尽 ⇒ rc≠0 且报错文案含「尚未被 sumdb 收录」；
     非窗口错误 ⇒ 不重试（一次即败），文案不含 sumdb 字样。

**无副作用、不联网、不真跑 go、不真睡**：run_tidy 用注入 runner/sleep_fn。

用法：python3 scripts/gotoolchain_env_selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import align_floors  # noqa: E402
import release_train  # noqa: E402
from matrix import apply_sumdb_policy, go_toolchain_from_gomod, local_go_version  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


class FakeResult:
    def __init__(self, returncode: int, out: str = ""):
        self.returncode = returncode
        self.stdout = out
        self.stderr = ""


def case_toolchain_parsing() -> None:
    check(go_toolchain_from_gomod("module x\n\ngo 1.24\n") == "go1.24.0",
          "go 1.24 应换算成 go1.24.0（两段补 .0）")
    check(go_toolchain_from_gomod("module x\n\ngo 1.24.0\n") == "go1.24.0",
          "go 1.24.0 应换算成 go1.24.0")
    check(go_toolchain_from_gomod("module x\n\ngo 1.26\n\nrequire (\n)") == "go1.26.0",
          "go 1.26 应换算成 go1.26.0")
    check(go_toolchain_from_gomod("module x\nrequire y v1.0.0\n") is None,
          "无 go 指令应返回 None（不钉）")
    check(local_go_version().startswith("go") or "读不到" in local_go_version()
          or "无 go" in local_go_version(),
          f"local_go_version 应返回真实版本号或如实说读不到：{local_go_version()!r}")


def build_module(td: Path, directive: str = "go 1.24.0") -> Path:
    d = td / "mod"
    d.mkdir(exist_ok=True)
    lines = "module example.com/mod\n"
    if directive:
        lines += f"\n{directive}\n"
    (d / "go.mod").write_text(lines, encoding="utf-8")
    return d


def case_env_pinning() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = build_module(Path(td))
        env = align_floors.go_env_for(d)
        check(env.get("GOTOOLCHAIN") == "go1.24.0",
              f"align_floors.go_env_for 未钉 GOTOOLCHAIN=go1.24.0：{env.get('GOTOOLCHAIN')!r}")
        check(env.get("GOWORK") == "off", "go_env_for 必须保留 GOWORK=off")
        check(env.get("GONOSUMDB") == "github.com/oekit/*",
              f"align_floors.go_env_for 未设 GONOSUMDB=github.com/oekit/*：{env.get('GONOSUMDB')!r}")

        renv = release_train.go_env(d)
        check(renv.get("GOTOOLCHAIN") == "go1.24.0",
              f"release_train.go_env 未钉 GOTOOLCHAIN=go1.24.0：{renv.get('GOTOOLCHAIN')!r}")

        # 嵌套模块钉更高的 go（bootstrap/obs 1.26.0 vs 父仓 1.24.0 的真实形态）
        deep = build_module(Path(td), "go 1.26.0")
        deep2 = Path(td) / "mod2"
        deep.rename(deep2)
        check(align_floors.go_env_for(deep2).get("GOTOOLCHAIN") == "go1.26.0",
              "嵌套模块按**自己的** go.mod 钉（1.26.0），不用父仓的 1.24.0")

    # 无 go 指令 ⇒ 不钉
    with tempfile.TemporaryDirectory() as td:
        d = build_module(Path(td), directive="")
        check("GOTOOLCHAIN" not in align_floors.go_env_for(d),
              "无 go 指令的模块不该钉 GOTOOLCHAIN（返回 None ⇒ 保持本机工具链）")

    # 失败信息必须写明是哪个工具链判的（#249 验收）
    note = release_train.toolchain_note({"GOTOOLCHAIN": "go1.24.0"})
    check("go1.24.0" in note, f"toolchain_note 未写明钉住的工具链：{note!r}")
    check("本机" in note, f"toolchain_note 未写明本机默认版本：{note!r}")


def case_sumdb_policy() -> None:
    # 调用方已自设策略 ⇒ 原样尊重
    env = dict(os.environ, GOSUMDB="off")
    apply_sumdb_policy(env)
    check("GONOSUMDB" not in env or env["GONOSUMDB"] == os.environ.get("GONOSUMDB", ""),
          "已设 GOSUMDB=off ⇒ 不再注入 GONOSUMDB（尊重调用方策略）")
    env = dict(os.environ, GONOSUMDB="example.com/private/*")
    apply_sumdb_policy(env)
    check("github.com/oekit/*" in env["GONOSUMDB"],
          f"已有 GONOSUMDB 但没盖住矩阵路径 ⇒ 应追加：{env['GONOSUMDB']!r}")
    env = dict(os.environ, GOPRIVATE="github.com/oekit/*")
    apply_sumdb_policy(env)
    check(env.get("GONOSUMDB") in (None, os.environ.get("GONOSUMDB", "")),
          "已设 GOPRIVATE（已蕴含跳 sumdb）⇒ 不再注入")


def case_tidy_retry() -> None:
    window_err = ("example.com/dep: reading https://goproxy.cn/sumdb/sum.golang.org/"
                  "lookup/example.com/dep@v1.2.3: 404 Not Found\n"
                  "server response: not found: temporarily unavailable")
    with tempfile.TemporaryDirectory() as td:
        d = build_module(Path(td), directive="")  # 无 go 指令 ⇒ 不触发工具链下载

        # a. 窗口失败 → 重试后成功 ⇒ rc 0 且真的重试了
        calls = {"n": 0}
        sleeps: list[float] = []

        def flaky():
            calls["n"] += 1
            return FakeResult(0) if calls["n"] >= 2 else FakeResult(1, window_err)

        rc = align_floors.run_tidy(d, runner=flaky, sleep_fn=sleeps.append)
        check(rc == 0, f"用例4a 窗口失败重试后应成功：rc={rc}")
        check(calls["n"] == 2, f"用例4a 应重试一次（共 2 次调用），实际 {calls['n']}")
        check(sleeps == [align_floors.TIDY_BACKOFF[0]],
              f"用例4a 退避序列不对：{sleeps}")

        # b. 重试耗尽 ⇒ rc≠0 且报错文案点名真实原因（不是裸 404）
        import contextlib, io
        calls["n"] = 0

        def always_window():
            calls["n"] += 1
            return FakeResult(1, window_err)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = align_floors.run_tidy(d, runner=always_window, sleep_fn=sleeps.append)
        check(rc != 0, "用例4b 重试耗尽应返回非零")
        check(calls["n"] == align_floors.TIDY_ATTEMPTS,
              f"用例4b 应重试满 {align_floors.TIDY_ATTEMPTS} 次，实际 {calls['n']}")
        check("尚未被 sumdb 收录" in buf.getvalue(),
              f"用例4b 耗尽后的报错文案必须点名「刚发布的版本尚未被 sumdb 收录」"
              f"（不许原样吐 404 把人带去查网络）：{buf.getvalue()[-300:]}")

        # c. 非窗口错误 ⇒ 不重试、文案不含 sumdb 字样
        calls["n"] = 0
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = align_floors.run_tidy(d, runner=lambda: (calls.__setitem__(
                "n", calls["n"] + 1), FakeResult(1, "syntax error in go.mod file"))[1],
                sleep_fn=sleeps.append)
        check(rc != 0, "用例4c 真错误应返回非零")
        check(calls["n"] == 1, f"用例4c 非窗口错误不该重试（1 次即败），实际 {calls['n']}")
        check("sumdb 收录" not in buf.getvalue(),
              "用例4c 非窗口错误不该报「sumdb 收录」文案（会把排查带偏）")


def main() -> None:
    case_toolchain_parsing()
    case_env_pinning()
    case_sumdb_policy()
    case_tidy_retry()
    if FAILURES:
        print("✗ 自测失败：")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("✓ go 环境自测通过：GOTOOLCHAIN 按仓 go.mod 钉住（两段补 .0，本地与 CI 同面）、"
          "矩阵路径跳过 sumdb、收录窗口退避重试且报错文案点名真实原因")


if __name__ == "__main__":
    main()
