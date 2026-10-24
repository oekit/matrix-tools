#!/usr/bin/env python3
"""维护轮门禁的自测：证明「维护轮到期」这道判据抓得住自己要防的东西，且不假绿。

**为什么要有这个自测**（2026-10-24 实测，matrix-tools#96）：
月度 workflow `maintenance.yml` 于 2026-10-19 加入，**至今 0 次运行、0 个维护轮
issue**——而真实地板漂移是 20 仓（阈值 8）。根因是三件事叠在一起：

  1. `maintenance_check.py` 的 base 定位硬编码 `parent.parent.parent`，**不认
     MATRIX_BASE**（`matrix.repo_dir` 的语义是 `base or os.environ.get(...)`，
     base 非空就把环境变量短路掉）⇒ CI 布局（仓克隆在 `$GITHUB_WORKSPACE/repos`）
     下 24 仓全部「读不到」；
  2. 「读不到」被静默 continue ⇒ 输出照旧是「✓ 全矩阵地板零漂移 /
     MAINTENANCE_COUNT=0」⇒ workflow 走进 `count==0 ⇒ 关闭维护轮 issue` 分支
     ⇒ **门禁方向反了**（到期不报、反而关单）；
  3. 标签 `matrix-maintenance` 在本仓不存在 ⇒ 即使走对分支，`issues.create`
     也会 422，issue 依然开不出来。

一道没人验证过的门禁，很可能自己就是个假绿——与本仓既有 selftest 的出发点
一致（`close_issue` / `ghsafe` / `remote_reads` / `verify_release_units` /
`verify_version_line`）。

**反证怎么构造**（全部用临时目录 + 临时 git 仓，不联网、不碰真仓）：
  1. 复刻 CI 布局（checkout 在 `X/`，仓克隆在 `X/repos/`）⇒ 仓**必须**被认出来
     （旧实现在这里报「解析失败 / 零漂移」——这就是本次修的那个 bug）；
  2. 只给 `MATRIX_BASE`、不给 `--base` ⇒ 同样必须被认出来（证明环境变量真的被读）；
  3. `--base` 显式指定 ⇒ 同样必须被认出来；
  4. 真的漂移必须被算出来（pkg v0.4.0 → v0.5.0 ⇒ 该仓计入 count）；
  5. 仓读不到时 ⇒ **不许**输出「零漂移」，且必须计入 `MAINTENANCE_UNDETERMINED`
     （旧实现这里就是假绿）；
  6. `--repos` 单仓盘点 ⇒ 同样认 base、只报该仓；
  7. `align_floors.py` 同样必须认 `MATRIX_BASE`（它是维护轮的整改工具，
     issue 正文里给出的补救命令就是它——定位错了整改就打到别的目录）。

**全矩阵的仓都用假仓造**（MODULES 里的每个名字都造一个），这样自测跑的是
**与旧代码完全相同的命令行**——旧代码的失败原因只能是 bug 本身，不能是
「参数不认」这种表层差异（否则「改前红」证明不了判据抓得住）。

用法：python3 scripts/maintenance_check_selftest.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
from matrix import MODULES  # noqa: E402

# 假仓里造出来的两条「真」依赖线：pkg 有新版本，core 还钉在旧版本 ⇒ core 漂移。
DEP = "pkg"
CONSUMER = "core"
DEP_OLD = "v0.4.0"
DEP_NEW = "v0.5.0"


def _git(repo: Path, *args: str) -> None:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()}")


def make_repo(path: Path, tags: list[str], requires: dict[str, str] | None = None) -> Path:
    """造一个临时 git 仓：给定 tag 列表，go.mod 里 require 指定的矩阵模块。

    **必须自带 git 身份**：CI runner 没有全局 user.name/user.email
    （2026-10-23 实测：本地绿、CI 红），身份只写进这个临时仓，不动全局配置。
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.name", "maintenance-check selftest")
    _git(path, "config", "user.email", "selftest@example.invalid")
    lines = ["module example.com/probe", "", "go 1.21", ""]
    if requires:
        lines.append("require (")
        for dep, ver in requires.items():
            lines.append(f"\tgithub.com/oekit/{dep} {ver}")
        lines.append(")")
    (path / "go.mod").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "README.md").write_text("probe\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    for name in tags:
        _git(path, "tag", "-a", name, "-m", f"tag {name}")
    return path


def make_ci_layout(root: Path) -> tuple[Path, Path]:
    """复刻 CI 布局：checkout 在 `X/`，仓克隆在 `X/repos/<name>/`。

    `X` 的上一级刻意**不放**仓——这正是旧实现踩空的地方（它找 `X/../<name>`
    与 `X/../../repos/<name>`，都不命中）。
    """
    checkout = root / "matrix-tools"
    repos = checkout / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "matrix.py", checkout / "matrix.py")
    shutil.copytree(REPO / "scripts", checkout / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return checkout, repos


def make_all_fake_repos(repos: Path) -> None:
    """MODULES 里每个名字都造一个假仓；其中 core 钉在 pkg 的旧版本 ⇒ 一处漂移。"""
    for mod in MODULES:
        if mod == DEP:
            make_repo(repos / mod, [DEP_OLD, DEP_NEW])
        elif mod == CONSUMER:
            make_repo(repos / mod, ["v0.1.0"], requires={DEP: DEP_OLD})
        else:
            make_repo(repos / mod, ["v0.1.0"])


def run_check(checkout: Path, extra_env: dict[str, str] | None = None,
              args: list[str] | None = None) -> str:
    env = dict(os.environ)
    env.pop("MATRIX_BASE", None)
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "scripts/maintenance_check.py", *(args or [])]
    r = subprocess.run(cmd, cwd=str(checkout), capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"maintenance_check.py 退出码 {r.returncode}: {r.stderr.strip()}")
    return r.stdout


def count_of(out: str, key: str) -> int:
    m = re.search(rf"^{key}=(\d+)$", out, re.M)
    if not m:
        raise AssertionError(f"输出里没有 {key}=… 这一行：\n{out}")
    return int(m.group(1))


def selftest() -> int:
    results: list[tuple[str, bool, str]] = []

    def case(name: str, fn) -> None:
        """跑一个形态；异常也算失败（不许因第一个崩了就跳过其余形态）。"""
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001 自测里任何异常都算该形态失败
            ok, detail = False, f"{type(e).__name__}: {e}".replace("\n", " ")[:160]
        results.append((name, ok, detail))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checkout, repos = make_ci_layout(root)
        make_all_fake_repos(repos)

        # ── 形态①：CI 布局 + 只给 MATRIX_BASE ⇒ 仓必须被认出来 ──
        # 旧实现在这里输出「解析失败 … ✓ 全矩阵地板零漂移 / MAINTENANCE_COUNT=0」。
        def case1() -> tuple[bool, str]:
            out = run_check(checkout, extra_env={"MATRIX_BASE": str(repos)})
            got_count = count_of(out, "MAINTENANCE_COUNT")
            got_undet = count_of(out, "MAINTENANCE_UNDETERMINED")
            ok = got_count == 1 and got_undet == 0 and "解析失败" not in out
            return ok, f"count={got_count} undetermined={got_undet}"

        case("CI 布局 + MATRIX_BASE ⇒ 仓被认出、漂移被算出来（不再「解析失败/零漂移」）", case1)

        # ── 形态②：--base 显式指定 ⇒ 同样被认出 ──
        def case2() -> tuple[bool, str]:
            out = run_check(checkout, args=["--base", str(repos)])
            got_count = count_of(out, "MAINTENANCE_COUNT")
            ok = got_count == 1 and "解析失败" not in out
            return ok, f"count={got_count}"

        case("--base 显式指定 ⇒ 仓被认出", case2)

        # ── 形态③：仓真的读不到 ⇒ 不许输出「零漂移」，且必须计入 undetermined ──
        def case3() -> tuple[bool, str]:
            missing = checkout / "empty-base"
            missing.mkdir()
            out = run_check(checkout, args=["--base", str(missing)])
            got_undet = count_of(out, "MAINTENANCE_UNDETERMINED")
            ok = (
                got_undet == len(MODULES)
                # 判据要精确：**那一行断言**不许出现。输出里「不能当零漂移」这类
                # 措辞含「零漂移」三字，用朴素子串判会误判自己。
                and "✓ 全矩阵地板零漂移" not in out
                and "漂移状态未知" in out
            )
            return ok, f"undetermined={got_undet}/{len(MODULES)}"

        case("仓读不到 ⇒ 不许输出「零漂移」，且计入 MAINTENANCE_UNDETERMINED", case3)

        # ── 形态④：--repos 单仓盘点 ⇒ 认 base、只报该仓 ──
        def case4() -> tuple[bool, str]:
            out = run_check(checkout, extra_env={"MATRIX_BASE": str(repos)},
                            args=["--repos", CONSUMER])
            got_count = count_of(out, "MAINTENANCE_COUNT")
            ok = got_count == 1 and "解析失败" not in out
            return ok, f"count={got_count}"

        case("--repos 单仓盘点 ⇒ 认 base、只报该仓", case4)

        # ── 形态⑤：align_floors 也必须认 MATRIX_BASE ──
        # 它是维护轮唯一的整改工具（issue 正文给出的补救命令就是它）。
        def case5() -> tuple[bool, str]:
            env = dict(os.environ)
            env["MATRIX_BASE"] = str(repos)
            r = subprocess.run(
                [sys.executable, "scripts/align_floors.py", "--plan", "--repo", CONSUMER],
                cwd=str(checkout), capture_output=True, text=True, env=env)
            ok = r.returncode == 0 and f"{DEP} {DEP_OLD} → {DEP_NEW}" in r.stdout
            detail = (r.stdout.strip().splitlines() or ["(无输出)"])[-1]
            if r.returncode != 0:
                detail = f"rc={r.returncode} {r.stderr.strip().splitlines()[-1:]}"
            return ok, detail[:160]

        case("align_floors.py 认 MATRIX_BASE（整改命令不会打到错误目录）", case5)

    print("维护轮门禁自测（matrix-tools#96）：")
    failed = 0
    for name, ok, detail in results:
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            failed += 1
    if failed:
        print(f"\n✗ {failed} 项自测失败——门禁抓不住自己要防的东西")
        return 1
    print("\n✓ 全部自测通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
