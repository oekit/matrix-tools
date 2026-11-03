#!/usr/bin/env python3
"""git 网络子进程「有界化」自测（超时 + SSH keepalive，2026-11-03）。

被测对象：`matrix.git_env` / `matrix.sh(timeout=…)` / `matrix._ls_remote_tags(env=…)`
/ `gen_matrix_go.ensure_local_tag(timeout=…)`。

为什么要有这个自测（2026-11-03 实录）：到 GitHub 的链路抖动时（本机 ssh 走
ssh.github.com:443，链路在「通 / 黑洞」间反复），已建立的 SSH 连接会静默挂死：
`git pull`/`git fetch` 无输出地无限等 ⇒ 三个后台任务（gen_matrix_go / BOM 发版
后置 / audit 门禁）全部超时被杀，release_train 的 `git fetch` 实录卡 10 分钟。
根因是 git 网络子进程**无超时、无 keepalive**——挂死不是「慢」，是「无界」。

| 形态 | 期望 |
|---|---|
| git_env() 未设 GIT_SSH_COMMAND | 注入 keepalive（ConnectTimeout/ServerAlive/BatchMode）|
| 用户已设 GIT_SSH_COMMAND / GIT_TERMINAL_PROMPT | **原样尊重**，不覆盖 |
| sh() 打到挂死子进程 | 有界失败：抛 RuntimeError（可诊断文案），不无限等 |
| sh() 的子进程环境 | 能看到 keepalive（证明 env 真传进了子进程，不是只改了自己）|
| _ls_remote_tags 打到挂死远端 | 快速返回 ([], False)（读失败，#79 口径不许混淆）|
| ensure_local_tag 的 fetch 挂死 | 有界返回且**不抛**（best-effort 语义不变，真因由 go_floor 报）|

**反证怎么构造**（各用例内建精确反证，不需要 --mutate）：
- 去掉 timeout 接线 ⇒ 假 git 睡 8s 后 exit 0 ⇒ sh()「正常返回」⇒
  RuntimeError 断言精确转红（**不会挂死自测本身**：假 git 的 sleep 是有界的）；
- 去掉 env 接线 ⇒ 假 git 落盘的 env 里没有 GIT_Ssh_COMMAND 的 keepalive ⇒
  环境断言精确转红。

**不联网、不依赖工作区布局、无副作用**：全程假 git（PATH 前置）+ 临时目录。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import matrix  # noqa: E402

FAKE_GIT = r"""#!/bin/sh
# 假 git：把 git_env 注入的环境落盘（供断言），再按环境变量注入「挂死」形态。
[ -n "$FAKE_ENV_DIR" ] || { echo "FAKE_ENV_DIR 未设" >&2; exit 99; }
printf '%s' "${GIT_SSH_COMMAND-__UNSET__}" > "$FAKE_ENV_DIR/git_ssh_command"
printf '%s' "${GIT_TERMINAL_PROMPT-__UNSET__}" > "$FAKE_ENV_DIR/git_terminal_prompt"
case "$*" in
  *"rev-parse --verify"*) exit 1 ;;   # 恒报「缺该 tag」⇒ 必走 fetch 分支
  *"fetch"*)
    [ -n "$FAKE_SLEEP_FETCH" ] && sleep "$FAKE_SLEEP_FETCH"
    exit 0 ;;
  *"ls-remote"*)
    [ -n "$FAKE_SLEEP_LSREMOTE" ] && sleep "$FAKE_SLEEP_LSREMOTE"
    exit 0 ;;
esac
[ -n "$FAKE_SLEEP_ALL" ] && sleep "$FAKE_SLEEP_ALL"
exit 0
"""

# 有界 sleep：比断言阈值（5s）长、比任何合理等待短 ⇒
# 「无 timeout 接线」时假 git 8s 后正常退出 ⇒ 断言精确转红，自测本身不挂死。
SLEEP = "8"

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


class Bench:
    """一次性的假 git + 环境交换（os.environ 换上换回）。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        bindir = tmp / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        g = bindir / "git"
        g.write_text(FAKE_GIT, encoding="utf-8")
        g.chmod(0o755)
        self.envdir = tmp / "envdump"
        self.envdir.mkdir(parents=True, exist_ok=True)
        self._saved = dict(os.environ)
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_ENV_DIR"] = str(self.envdir)
        # 用例里注入挂死形态的开关（默认全关）
        for k in ("FAKE_SLEEP_ALL", "FAKE_SLEEP_FETCH", "FAKE_SLEEP_LSREMOTE"):
            os.environ.pop(k, None)
        # git_env 的 setdefault 判据要「用户没设」⇒ 清掉宿主可能带着的值
        os.environ.pop("GIT_SSH_COMMAND", None)
        os.environ.pop("GIT_TERMINAL_PROMPT", None)

    def restore(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def envdump(self, name: str) -> str:
        p = self.envdir / name
        return p.read_text(encoding="utf-8") if p.exists() else "__MISSING__"


@case("A1. git_env() 未设时注入 keepalive（ConnectTimeout/ServerAlive/BatchMode）")
def case_a1(b: Bench):
    env = matrix.git_env()
    cmd = env.get("GIT_SSH_COMMAND", "")
    for opt in ("ConnectTimeout=", "ServerAliveInterval=", "ServerAliveCountMax=",
                "BatchMode=yes"):
        assert opt in cmd, f"GIT_SSH_COMMAND 缺 {opt}：{cmd!r}"
    assert env.get("GIT_TERMINAL_PROMPT") == "0", "未禁交互提示：" + repr(env.get("GIT_TERMINAL_PROMPT"))


@case("A2. 用户已设 GIT_SSH_COMMAND / GIT_TERMINAL_PROMPT ⇒ 原样尊重，不覆盖")
def case_a2(b: Bench):
    os.environ["GIT_SSH_COMMAND"] = "ssh -o ProxyJump=jump.example"
    os.environ["GIT_TERMINAL_PROMPT"] = "1"
    env = matrix.git_env()
    assert env["GIT_SSH_COMMAND"] == "ssh -o ProxyJump=jump.example", \
        "覆盖了用户的 GIT_SSH_COMMAND（代理跳板场景会被弄坏）：" + repr(env["GIT_SSH_COMMAND"])
    assert env["GIT_TERMINAL_PROMPT"] == "1", "覆盖了用户的 GIT_TERMINAL_PROMPT"


@case("B1. sh() 打到挂死子进程 ⇒ 有界失败（RuntimeError），不无限等")
def case_b1(b: Bench):
    os.environ["FAKE_SLEEP_ALL"] = SLEEP
    t0 = time.monotonic()
    try:
        matrix.sh(["git", "fake-op"], b.tmp, timeout=1)
    except RuntimeError as e:
        elapsed = time.monotonic() - t0
        assert elapsed < 5, f"有界失败但太慢（{elapsed:.1f}s）：{e}"
        assert "未完成" in str(e), "错误文案不可诊断：" + str(e)
    else:
        raise AssertionError(
            "sh() 对挂死子进程正常返回——timeout 接线丢了？"
            f"（耗时 {time.monotonic() - t0:.1f}s，假 git {SLEEP}s 后 exit 0）")


@case("B2. sh() 常规语义不变：exit 0 返回 / 非零抛 RuntimeError / check=False 不抛")
def case_b2(b: Bench):
    r = matrix.sh(["git", "ok-op"], b.tmp)
    assert r.returncode == 0
    r2 = matrix.sh(["sh", "-c", "echo boom >&2; exit 3"], b.tmp, check=False)
    assert r2.returncode == 3
    try:
        matrix.sh(["sh", "-c", "exit 3"], b.tmp)
    except RuntimeError:
        pass
    else:
        raise AssertionError("check=True 下非零退出应抛 RuntimeError")


@case("B3. sh() 的子进程真拿到了 keepalive env（不是只改了自己）")
def case_b3(b: Bench):
    matrix.sh(["git", "ok-op"], b.tmp)
    cmd = b.envdump("git_ssh_command")
    assert "ConnectTimeout=" in cmd and "BatchMode=yes" in cmd, \
        "子进程没看到 GIT_SSH_COMMAND keepalive：" + repr(cmd)
    assert b.envdump("git_terminal_prompt") == "0", "子进程没看到 GIT_TERMINAL_PROMPT=0"


@case("C. _ls_remote_tags 打到挂死远端 ⇒ 快速 ([], False)，且 env 带 keepalive")
def case_c(b: Bench):
    os.environ["FAKE_SLEEP_LSREMOTE"] = SLEEP
    t0 = time.monotonic()
    tags, ok = matrix._ls_remote_tags("https://example.invalid/repo.git",
                                      timeout=1, attempts=1)
    elapsed = time.monotonic() - t0
    assert (tags, ok) == ([], False), \
        f"挂死应判「读失败」([], False)（#79：读不到 ≠ 没有）：{(tags, ok)}"
    assert elapsed < 5, f"读失败有界但太慢（{elapsed:.1f}s）"
    cmd = b.envdump("git_ssh_command")
    assert "ConnectTimeout=" in cmd, "ls-remote 子进程没拿到 keepalive env：" + repr(cmd)


@case("D. ensure_local_tag 的 fetch 挂死 ⇒ 有界返回且不抛（best-effort 语义不变）")
def case_d(b: Bench):
    from gen_matrix_go import ensure_local_tag
    os.environ["FAKE_SLEEP_FETCH"] = SLEEP  # rev-parse 恒 exit 1 ⇒ 必走 fetch
    t0 = time.monotonic()
    ensure_local_tag(str(b.tmp), "v0.0.1", timeout=1)  # 不抛即通过
    elapsed = time.monotonic() - t0
    assert elapsed < 5, \
        f"fetch 无界（{elapsed:.1f}s）——timeout 接线丢了？挂死会拖死整个 BOM 生成"
    cmd = b.envdump("git_ssh_command")
    assert "ConnectTimeout=" in cmd, "fetch 子进程没拿到 keepalive env：" + repr(cmd)


def main() -> int:
    print("git_env_selftest：git 网络子进程有界化自测（超时 + SSH keepalive）")
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
