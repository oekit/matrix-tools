#!/usr/bin/env python3
"""远端读取层自测（matrix-tools#79）。

被测对象：`matrix.remote_tags` / `matrix.latest_tag(remote=True)`。

为什么要有这个自测（照 #75 的口径）：
「读到空 ≠ 事实为空」在 **GitHub API 侧**已由 ghsafe 覆盖（ghsafe_selftest.py），
但 **git 传输侧**（`git ls-remote --tags`）此前是裸 except ⇒ 读失败静默回退本地 tag。
本自测用**假 git** 复刻真实故障的四种形态，证明判据抓得住自己要防的东西：

| 形态 | 期望 |
|---|---|
| origin 失败、canonical HTTPS 成功 | 取到 HTTPS 的 tag（协议独立 ⇒ SSH 抽风不影响结论） |
| 两条都失败 | 抛 RemoteReadError（**不是**回退本地 tag） |
| exit 0 + 空 stdout | 返回空列表（真的没有 tag，不是故障） |
| exit 0 + 有 tag | latest_tag 取到版本号最高的那个；读失败时**抛错而非返回 None** |

**不联网、不依赖工作区布局、无副作用**：全程用临时目录 + 假 git（PATH 前置）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE_GIT = r"""#!/bin/sh
# 假 git：按环境变量复刻真实故障形态。
# 注意 ls-remote 的 URL 是第 3 个参数（git ls-remote --tags <url>）。
case "$*" in
  *"remote get-url origin"*)
    if [ -z "$FAKE_ORIGIN_URL" ]; then echo "no origin" >&2; exit 128; fi
    echo "$FAKE_ORIGIN_URL"; exit 0 ;;
  *"ls-remote --tags"*)
    url="$3"
    for bad in $(echo "$FAKE_FAIL_URLS" | tr ',' ' '); do
      [ -n "$bad" ] || continue
      case "$url" in *"$bad"*) echo "fatal: could not read from remote" >&2; exit 128 ;; esac
    done
    # 真实 ls-remote 的输出形态：<sha>\t<ref>；annotated tag 额外带 ^{} 一行
    tags="$FAKE_TAGS"
    case "$url" in *git@*) if [ -n "$FAKE_ORIGIN_TAGS" ]; then tags="$FAKE_ORIGIN_TAGS"; fi ;; esac
    if [ -n "$tags" ]; then
      for t in $tags; do
        printf 'deadbeef%s\trefs/tags/%s\n' "0000000000000000000000000000000000000000" "$t"
        printf 'deadbeef%s\trefs/tags/%s^{}\n' "0000000000000000000000000000000000000000" "$t"
      done
    fi
    exit 0 ;;
  *"tag -l"*)
    if [ -n "$FAKE_LOCAL_TAGS" ]; then printf '%s\n' "$FAKE_LOCAL_TAGS"; fi
    exit 0 ;;
esac
exit 0
"""

# 子进程里跑的片段：{ROOT} 与 {BASE} 在调用时替换。
INLINE_TAGS = """
import sys
sys.path.insert(0, {root!r})
from matrix import remote_tags
print("TAGS=" + repr(remote_tags("fakerepo", {base!r})))
"""

INLINE_TAGS_SAFE = """
import sys
sys.path.insert(0, {root!r})
from matrix import remote_tags, RemoteReadError
try:
    print("TAGS=" + repr(remote_tags("fakerepo", {base!r})))
except RemoteReadError as e:
    print("RAISED")
"""

INLINE_LATEST = """
import sys
sys.path.insert(0, {root!r})
from matrix import latest_tag, RemoteReadError
try:
    print("LATEST=" + str(latest_tag("fakerepo", {base!r}, remote=True)))
except RemoteReadError as e:
    print("RAISED")
"""

# #79 的直接受害者：gen_versions._remote_tags（旧实现读失败就回退本地 tag）
INLINE_GENVERSIONS = """
import sys
sys.path.insert(0, {root!r})
sys.path.insert(0, {root!r} + "/scripts")
from gen_versions import _remote_tags
from matrix import RemoteReadError
try:
    print("TAGS=" + repr(_remote_tags("fakerepo")))
except RemoteReadError as e:
    print("RAISED")
"""

# #85：「远端 tag 是否存在」这条事实的另外两条读路径（发版查重 / 关单守卫）
INLINE_TAG_EXISTS = """
import sys
sys.path.insert(0, {root!r})
sys.path.insert(0, {root!r} + "/scripts")
from matrix import remote_tag_exists, RemoteReadError
try:
    print("RESULT=" + repr(remote_tag_exists("fakerepo", "v1.0.0")))
except RemoteReadError:
    print("RAISED")
"""

INLINE_RELEASE_TRAIN = """
import sys
sys.path.insert(0, {root!r})
sys.path.insert(0, {root!r} + "/scripts")
from release_train import tag_exists_remote
from matrix import RemoteReadError
try:
    print("RESULT=" + repr(tag_exists_remote("fakerepo", "v1.0.0")))
except RemoteReadError:
    print("RAISED")
"""

INLINE_CLOSE_ISSUE = """
import sys
sys.path.insert(0, {root!r})
sys.path.insert(0, {root!r} + "/scripts")
from close_issue import tag_on_remote
from matrix import RemoteReadError
try:
    print("RESULT=" + repr(tag_on_remote("fakerepo", "v1.0.0")))
except RemoteReadError:
    print("RAISED")
"""


class Bench:
    """一次性的假 git + 假工作区。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        bindir = tmp / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        g = bindir / "git"
        g.write_text(FAKE_GIT, encoding="utf-8")
        g.chmod(0o755)
        self.base = tmp / "base"
        repo = self.base / "fakerepo"
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "go.mod").write_text("module github.com/oekit/fakerepo\n", encoding="utf-8")

    def env(self, **kw) -> dict:
        env = dict(os.environ)
        env["PATH"] = str(self.tmp / "bin") + os.pathsep + env.get("PATH", "")
        env.update(kw)
        return env

    def run(self, script: str, env: dict) -> str:
        code = script.format(root=str(ROOT), base=str(self.base))
        r = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f"子进程崩溃：\n{r.stdout}\n{r.stderr}")
        return r.stdout


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("A. origin 失败、canonical HTTPS 成功 ⇒ 取到 HTTPS 的 tag")
def case_a(b: Bench):
    out = b.run(INLINE_TAGS, b.env(
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="git@",
        FAKE_TAGS="v1.0.0\nv1.2.0"))
    assert "TAGS=" in out, out
    assert "'v1.2.0'" in out, "未取到 HTTPS 的 tag：" + out
    assert "'v1.0.0'" in out, out


@case("B. 两条都失败 ⇒ 抛 RemoteReadError，且**不是**本地 tag")
def case_b(b: Bench):
    out = b.run(INLINE_TAGS_SAFE, b.env(
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="git@,github.com",
        FAKE_TAGS="",
        FAKE_LOCAL_TAGS="v0.0.1-local-only"))
    assert "RAISED" in out, "未抛 RemoteReadError：" + out
    assert "v0.0.1-local-only" not in out, "回退到了本地 tag（这正是要防的坑）：" + out


@case("C. exit 0 + 空 stdout ⇒ 空列表（真的没有 tag，不是故障）")
def case_c(b: Bench):
    out = b.run(INLINE_TAGS, b.env(
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="",
        FAKE_TAGS=""))
    assert "TAGS=[]" in out, "空结果应被当成合法事实：" + out


@case("D. latest_tag(remote=True) 取最高版本；读失败时抛错而非返回 None")
def case_d(b: Bench):
    out = b.run(INLINE_LATEST, b.env(
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="",
        FAKE_TAGS="v1.0.0\nv1.10.0\nv1.2.0"))
    assert "LATEST=v1.10.0" in out, "未取到最高版本：" + out

    out2 = b.run(INLINE_LATEST, b.env(
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="git@,github.com",
        FAKE_TAGS=""))
    assert "RAISED" in out2, "读失败应抛错：" + out2
    assert "LATEST=None" not in out2, "读失败返回了 None（旧语义，必须已改）：" + out2


@case("E. 无 origin（读不到 origin URL）⇒ 仍能走 canonical HTTPS")
def case_e(b: Bench):
    out = b.run(INLINE_TAGS, b.env(
        FAKE_ORIGIN_URL="",
        FAKE_FAIL_URLS="",
        FAKE_TAGS="v2.0.0"))
    assert "'v2.0.0'" in out, "无 origin 时应回落到 canonical HTTPS：" + out


@case("F. gen_versions._remote_tags（#79 的直接受害者）读失败 ⇒ 抛错，不回退本地 tag")
def case_f(b: Bench):
    out = b.run(INLINE_GENVERSIONS, b.env(
        MATRIX_BASE=str(b.base),
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="git@,github.com",
        FAKE_TAGS="",
        FAKE_LOCAL_TAGS="v0.0.1-local-only"))
    assert "RAISED" in out, "gen_versions._remote_tags 未抛错（旧语义：静默回退本地 tag）：" + out
    assert "v0.0.1-local-only" not in out, "回退到了本地 tag（这正是要防的坑）：" + out


@case("G. canonical HTTPS 优先于 origin（两者都通但内容不同时，取 HTTPS 的）")
def case_g(b: Bench):
    out = b.run(INLINE_TAGS, b.env(
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="",
        FAKE_TAGS="v3.0.0-canonical",
        FAKE_ORIGIN_TAGS="v9.9.9-stale-origin"))
    assert "'v3.0.0-canonical'" in out, "未取 canonical HTTPS 的 tag：" + out
    assert "v9.9.9-stale-origin" not in out, "取了 origin 的 tag（canonical 应优先）：" + out


@case("H. canonical HTTPS 失败、origin 成功 ⇒ 回落 origin（两条路协议独立）")
def case_h(b: Bench):
    out = b.run(INLINE_TAGS, b.env(
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="https://",
        FAKE_TAGS="v3.0.0-canonical",
        FAKE_ORIGIN_TAGS="v4.0.0-via-origin"))
    assert "'v4.0.0-via-origin'" in out, "HTTPS 失败后未回落 origin：" + out


# ── #85：「远端 tag 是否存在」的三条读路径必须语义一致 ──
#
# 这三条读的是**同一个事实**（远端有没有这个 tag），此前却有两种语义：
# matrix.remote_tags 读失败抛错（#79），而发版查重 / 关单守卫只看 stdout 非空
# ⇒ 读失败 = 「不存在」⇒ 查重门禁静默放行、关单守卫误报「不在远端」。
# 变异实测：同一个确实存在的 tag，仅因读法不同就从 True 翻成 False。


@case("I. matrix.remote_tag_exists：读失败 ⇒ 抛错（读不到 ≠ 不存在）")
def case_i(b: Bench):
    out = b.run(INLINE_TAG_EXISTS, b.env(
        MATRIX_BASE=str(b.base),
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="git@,github.com",
        FAKE_TAGS=""))
    assert "RAISED" in out, "读失败未抛错（把「读不到」说成「没有」）：" + out
    assert "RESULT=False" not in out, "读失败返回了 False（旧语义，必须已改）：" + out


@case("J. matrix.remote_tag_exists：读成功且 tag 存在 ⇒ True")
def case_j(b: Bench):
    out = b.run(INLINE_TAG_EXISTS, b.env(
        MATRIX_BASE=str(b.base),
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="",
        FAKE_TAGS="v1.0.0"))
    assert "RESULT=True" in out, "未取到已存在的 tag：" + out


@case("K. release_train 查重：读失败 ⇒ 抛错（查重门禁不得静默放行）")
def case_k(b: Bench):
    out = b.run(INLINE_RELEASE_TRAIN, b.env(
        MATRIX_BASE=str(b.base),
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="git@,github.com",
        FAKE_TAGS=""))
    assert "RAISED" in out, "发版查重读失败未抛错（查重门禁静默失效）：" + out
    assert "RESULT=False" not in out, "发版查重读失败返回了 False（旧语义）：" + out


@case("L. release_train 查重：读成功且 tag 存在 ⇒ True")
def case_l(b: Bench):
    out = b.run(INLINE_RELEASE_TRAIN, b.env(
        MATRIX_BASE=str(b.base),
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="",
        FAKE_TAGS="v1.0.0"))
    assert "RESULT=True" in out, "未取到已存在的 tag：" + out


@case("M. close_issue 关单守卫：读失败 ⇒ 抛错（不得说「不在远端」）")
def case_m(b: Bench):
    out = b.run(INLINE_CLOSE_ISSUE, b.env(
        MATRIX_BASE=str(b.base),
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="git@,github.com",
        FAKE_TAGS=""))
    assert "RAISED" in out, "关单守卫读失败未抛错（把「读不到」报成「不在远端」）：" + out
    assert "RESULT=False" not in out, "关单守卫读失败返回了 False（旧语义）：" + out


@case("N. close_issue 关单守卫：读成功且 tag 存在 ⇒ True")
def case_n(b: Bench):
    out = b.run(INLINE_CLOSE_ISSUE, b.env(
        MATRIX_BASE=str(b.base),
        FAKE_ORIGIN_URL="git@github.com:oekit/fakerepo.git",
        FAKE_FAIL_URLS="",
        FAKE_TAGS="v1.0.0"))
    assert "RESULT=True" in out, "未取到已存在的 tag：" + out


def main() -> int:
    print("remote_reads_selftest：远端读取层判据自测（#79）")
    failed = []
    for name, fn in CASES:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Bench(Path(td)))
                print(f"  ✓ {name}")
            except AssertionError as e:
                failed.append(name)
                print(f"  ❌ {name}\n     {e}")
            except Exception as e:  # noqa: BLE001
                failed.append(name)
                print(f"  ❌ {name}（异常）\n     {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
