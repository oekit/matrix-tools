#!/usr/bin/env python3
"""审计「版本事实源 + 本地 tag 陈旧比对」的自测（matrix-tools#269）。

**为什么要有这个自测**：#269 之前，「最新版本」这个事实在本地跑时取自
本地 `git tag -l`——本地 tag 只在 `git fetch origin --tags` 时更新，日常
`git fetch origin main` 不动 tag ⇒ 实测一次维护轮**整片 69 项假红**。
改法是审计默认读远端、本地陈旧显式提示。这道判据若自己是个假绿
（比如比对函数被拆掉、默认值被改回本地），假红就原样回来。

**反证怎么构造**（全部临时目录造 fixture，bare 仓当 origin，**不联网**）：
  A. 本地 tag 落后远端 ⇒ 必须进返回值（且带两版号）；
  B. 两边一致 ⇒ 不许误报；
  C. 本地比远端新（旁支/远端被删）⇒ 同样要提示（方向不限）；
  D. 远端读不到 ⇒ 该仓不进返回值（漂移口径另有记账，这里不许崩）；
  E. 本地一个 tag 都没有（depth-1 clone 形态）⇒ 不算陈旧（噪音防线）；
  F. **真读路径**：远端 tag 走真实的 matrix.remote_tags（本地 origin 夹具，
     离线）⇒ 与 A 同形态必须判出——证明不是只对 stub 生效；
  W. **接线自证**：audit.py 源里必须 (1) 默认事实源=远端（"--local-tags" not in
     sys.argv），(2) 预取后真的调了 detect_stale_local_tags，(3) 结论区真的
     印「本地 tag 陈旧」——三处拆任何一处，本自测必须红。

用法：python3 scripts/audit_tag_source_selftest.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import matrix  # noqa: E402  （local_latest_tag / remote_tags 真读路径用）
from matrix import RemoteReadError  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def _load_detector(src: str):
    """按名取 audit.py 的 detect_stale_local_tags（不 import 整个 audit——那是跑全审计）。"""
    m = re.search(r"^def detect_stale_local_tags\(.*?(?=^\S)", src, re.S | re.M)
    if not m:
        raise AssertionError("audit.py 里找不到 detect_stale_local_tags（#269 修法被拆了？）")
    # 默认参数在 def 时求值 ⇒ 名字必须存在；自测全部显式传参，占位不会真被调用。
    ns: dict = {"RemoteReadError": RemoteReadError,
                "local_latest_tag": matrix.local_latest_tag,
                "remote_latest": lambda repo: None}
    exec(compile(m.group(0), "<audit-slice>", "exec"), ns)
    return ns["detect_stale_local_tags"]


def _git(*args: str, cwd: str | Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, check=True,
                   capture_output=True, text=True)


def _make_repo(td: Path, name: str, remote_tags: list[str], local_tags: list[str]) -> Path:
    """bare origin 带 remote_tags；clone 到 <name> 后删成 local_tags。"""
    origin = td / f"{name}-origin.git"
    _git("init", "--bare", "-q", str(origin))
    seed = td / f"{name}-seed"
    seed.mkdir()
    _git("init", "-q", cwd=seed)
    _git("config", "user.email", "t@t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    # repo_dir 认仓靠 go.mod/README.md 存在（fixture 必须像仓）
    (seed / "README.md").write_text(f"# {name}\n")
    (seed / "go.mod").write_text(f"module github.com/oekit/{name}\n\ngo 1.24\n")
    _git("add", ".", cwd=seed)
    _git("commit", "-qm", "init", cwd=seed)
    for t in remote_tags:
        _git("tag", t, cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "-q", "--all", "origin", cwd=seed)
    _git("push", "-q", "--tags", "origin", cwd=seed)
    work = td / name
    _git("clone", "-q", str(origin), str(work))
    for t in remote_tags:
        if t not in local_tags:
            _git("tag", "-d", t, cwd=work)
    for t in local_tags:
        if t not in remote_tags:
            _git("tag", t, cwd=work)   # 本地独有的 tag（旁支/远端被删形态）
    return work


def main() -> None:
    src = (HERE / "audit.py").read_text(encoding="utf-8")
    detect = _load_detector(src)

    # ── W：接线自证（拆任何一处 ⇒ 假红原样回来）──
    check('REMOTE_TAGS = "--local-tags" not in sys.argv' in src,
          "W1: 事实源默认必须是远端（--local-tags 才退回本地）——默认值被改回本地旗子 ⇒ 假红回来了")
    check("detect_stale_local_tags(_present, BASE)" in src,
          "W2: audit 主流程必须真的调用本地陈旧比对（不是只有函数没有接线）")
    check("本地 tag 陈旧" in src and "git fetch origin --tags --prune" in src,
          "W3: 结论区必须显式印本地陈旧提示与同步命令")
    check("`git fetch origin main` 不更新 tag" in src.replace("**不动 tag**", "不更新 tag")
          or "不动 tag" in src,
          "W4: docstring 必须写明「fetch origin main 不更新 tag」这一根因")

    with tempfile.TemporaryDirectory() as td_s:
        td = Path(td_s)
        base = str(td)
        work = _make_repo(td, "pkg", ["v0.1.0", "v0.2.0"], ["v0.1.0"])       # 落后 1 版
        fresh = _make_repo(td, "logx", ["v0.8.11", "v0.8.12"], ["v0.8.11", "v0.8.12"])  # 一致
        ahead = _make_repo(td, "core", ["v0.13.4"], ["v0.13.4", "v0.13.5"])  # 本地比远端新
        (work / ".git").exists()  # shape keeper

        remote_stub = {"pkg": "v0.2.0", "logx": "v0.8.12", "core": "v0.13.4"}

        # ── A/B/C：三种形态一次判 ──
        stale = detect(["pkg", "logx", "core"], base,
                       local_read=matrix.local_latest_tag,
                       remote_read=lambda repo: remote_stub[repo])
        check(stale.get("pkg") == ("v0.1.0", "v0.2.0"),
              f"A: 本地落后必须判出且带两版号——实得 {stale.get('pkg')}")
        check("logx" not in stale,
              f"B: 两边一致不许误报——实得 { {k: v for k, v in stale.items()} }")
        check(stale.get("core") == ("v0.13.5", "v0.13.4"),
              f"C: 本地比远端新（旁支/远端被删形态）也要提示——实得 {stale.get('core')}")

        # ── D：远端读不到 ⇒ 不进返回值、不崩 ──
        def bad_remote(repo: str) -> str:
            raise RemoteReadError(f"{repo}: 远端 tag 读不到（fixture）")
        stale_d = detect(["pkg"], base,
                         local_read=matrix.local_latest_tag, remote_read=bad_remote)
        check(stale_d == {},
              f"D: 远端读失败的仓必须跳过（另有漂移记账）不许崩——实得 {stale_d}")

        # ── E：本地无 tag（depth-1 clone 形态）⇒ 不算陈旧 ──
        notags = td / "notags"
        notags.mkdir()
        _git("init", "-q", cwd=notags)
        _git("config", "user.email", "t@t", cwd=notags)
        _git("config", "user.name", "t", cwd=notags)
        (notags / "README.md").write_text("# notags\n")
        (notags / "go.mod").write_text("module github.com/oekit/notags\n\ngo 1.24\n")
        (notags / "f.txt").write_text("x\n")
        _git("add", ".", cwd=notags)
        _git("commit", "-qm", "init", cwd=notags)
        stale_e = detect(["notags"], base,
                         local_read=matrix.local_latest_tag,
                         remote_read=lambda repo: "v9.9.9")
        check("notags" not in stale_e,
              f"E: 本地无 tag（CI depth-1 clone 形态）不许算陈旧（噪音防线）——实得 {stale_e}")

        # ── F：真读路径——远端事实走真实 remote_tags（本地 origin 夹具，离线）──
        matrix.disable_tag_cache()
        matrix.clear_tag_cache() if hasattr(matrix, "clear_tag_cache") else None
        stale_f = detect(["pkg"], base,
                         local_read=matrix.local_latest_tag,
                         remote_read=lambda repo: matrix.latest_tag(
                             repo, base, remote=True))
        check(stale_f.get("pkg") == ("v0.1.0", "v0.2.0"),
              f"F: 真读路径（local-origin 夹具）必须同样判出——实得 {stale_f.get('pkg')}")

    if FAILURES:
        for f in FAILURES:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("✓ audit 版本事实源/本地陈旧比对自测通过（matrix-tools#269）")


if __name__ == "__main__":
    main()
