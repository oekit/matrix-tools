#!/usr/bin/env python3
"""场景组合冒烟：SCENARIOS 的模块组合必须「装得上 + 编译得过」（消费方视角）。

## 为什么要有这个（matrix-tools#341，2026-11-27 演进评审 P2）

场景选型表（gen_skill.SCENARIOS）此前只有**叙述**没有**可执行面**：
- 单模块可编译：各仓自己的 CI 守（`go build ./...`）；
- 全矩阵可编译：kitex-examples 守（require 全套 + build）；
- **场景子集组合**（用户按安装行 go get 的那组）无人守——A/B 两个模块各自
  最新版能否放进同一个 go.mod 编译过，只有真跑才知道。

冒烟执行的是**发布行原文**（`gen_skill.scenario_install_line` 单源渲染）——
「文档给用户的行」与「被验证的行」是同一字符串，不是两份会漂的复制品。

## 判据（每条锚在可复算事实上）

  1. 数据在场：SCENARIOS 每行 mods ⊆ MODULES（死键双保险，audit 已有单保险）；
  2. 装得上：每个**去重组合**在独立 temp module 里跑发布行原文（go get）必须成功；
  3. 编译得过：`go build github.com/oekit/<mod>/...` 逐模块必须成功——
     消费方视角全包编译，**不需要根包**（pkg/concurrency/core/network 四个模块
     无根 Go 文件，2026-11-27 实测 23/23 模块模式化 build 可用）；
  4. 读不到 ≠ 通过（#79）：go 工具缺失 / SCENARIOS 为空 ⇒ EXIT_UNREADABLE。

组合按 mods 去重（frozenset）：34 个场景行共享约二十个唯一组合，去重后
每组合只跑一次；失败时点名**全部**使用该组合的场景。

## 退出码契约（生产方定义）

    0 = EXIT_OK          全部组合装得上且编译过
    1 = EXIT_VIOLATION   任一组合失败（go get / go build / 死键）
    2 = EXIT_UNREADABLE  go 缺失 / SCENARIOS 空——判不了 ≠ 通过

## 代价与定位

需要联网（module proxy）+ Go 工具链；冷缓存下首批组合要拉全依赖树。
定位 = audit.yml 的 **schedule/dispatch job**（同 consumer：测已发布世界，
**不进 PR 门禁**——PR 不该为网络波动红）。

用法：
    python3 scripts/scenario_smoke.py             # 全量冒烟
    python3 scripts/scenario_smoke.py --json      # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # import matrix / gen_skill（唯一事实源）
sys.path.insert(0, str(HERE))

import gen_skill  # noqa: E402  （scenario_install_line 单源——发布行=被验证行）
from matrix import MODULES  # noqa: E402

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNREADABLE = 2

GET_TIMEOUT = 300    # go get 单组合（冷缓存首批要拉全依赖树，给足余量）
BUILD_TIMEOUT = 600  # go build 单组合


def unique_combos(scenarios: list | None = None) -> list[tuple[tuple[str, ...], list[str]]]:
    """按 mods 去重组合 → [(sorted mods, [场景名...])]，保序（首见场景的顺序）。"""
    scenarios = gen_skill.SCENARIOS if scenarios is None else scenarios
    seen: dict[frozenset, tuple[list[str], list[str]]] = {}
    for _group, rows in scenarios:
        for r in rows:
            key = frozenset(r["mods"])
            if key not in seen:
                seen[key] = (list(r["mods"]), [])
            seen[key][1].append(r["scene"])
    return [(tuple(sorted(mods)), scenes) for mods, scenes in seen.values()]


def dead_keys(scenarios: list | None = None) -> list[str]:
    """mods ⊆ MODULES 死键检查（audit 单保险之外的双保险，数据错要在冒烟源头红）。"""
    scenarios = gen_skill.SCENARIOS if scenarios is None else scenarios
    mod_set = set(MODULES)
    bad = []
    for _group, rows in scenarios:
        for r in rows:
            for m in r.get("mods", []):
                if m not in mod_set:
                    bad.append(f"{r['scene']}: mods 含矩阵外模块 {m}")
    return sorted(set(bad))


def run_combo(mods: tuple[str, ...], workdir: Path) -> tuple[bool, str]:
    """单组合冒烟：独立 go.mod → 发布行原文 go get → 逐模块模式化 build。

    返回 (ok, errtail)。errtail = 失败时 stderr 尾部（够定位即可，不吞不全文）。

    GOFLAGS=-mod=mod（2026-11-27 首跑实录 23/30 假红后定）：go get 只记录
    **被命名包**所需 go.sum 条目，而 build 拉全包子树要更多条目 ⇒ 缺条目报
    「missing go.sum entry」。-mod=mod 让 build 自动补记账——这正是用户照抄
    发布行撞上同款报错时工具自己给的修复路径（报错原文 "to add: go get ..."），
    不掩盖真失败：MVS 解析不了 / 编译错 / 网络 404 照样红。对照实验：
    A 无 flag 23/30 红、B 带 flag 全绿（与 23 模块预探针同环境）。
    """
    env = {**os.environ, "GOFLAGS": "-mod=mod"}
    # 发布行原文——单源 scenario_install_line，不在此处重拼
    line = gen_skill.scenario_install_line(list(mods))
    try:
        subprocess.run(["go", "mod", "init", "smoke"], cwd=workdir,
                       capture_output=True, text=True, timeout=30, check=True,
                       env=env)
        get = subprocess.run(line.split(), cwd=workdir, capture_output=True,
                             text=True, timeout=GET_TIMEOUT, env=env)
        if get.returncode != 0:
            return False, f"go get 失败（{line}）：{_tail(get.stderr or get.stdout)}"
        for m in mods:
            b = subprocess.run(["go", "build", f"github.com/oekit/{m}/..."],
                               cwd=workdir, capture_output=True, text=True,
                               timeout=BUILD_TIMEOUT, env=env)
            if b.returncode != 0:
                return False, f"go build github.com/oekit/{m}/... 失败：{_tail(b.stderr or b.stdout)}"
        return True, ""
    except subprocess.TimeoutExpired as e:
        return False, f"超时（{e.cmd}）"
    except FileNotFoundError:
        raise  # go 缺失 → 上层判 EXIT_UNREADABLE，不许当组合失败
    except subprocess.CalledProcessError as e:
        return False, f"go mod init 失败：{_tail(e.stderr or e.stdout or '')}"


def _tail(s: str, n: int = 600) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else "…" + s[-n:]


def classify(rc: int, out: str) -> tuple[str, str]:
    """退出码 → (kind, message)。kind ∈ {ok, violation, unreadable}。"""
    if rc == EXIT_OK:
        return "ok", "全部场景组合装得上且编译过"
    if rc == EXIT_VIOLATION:
        return "violation", "场景组合冒烟失败（matrix-tools#341）：" + out.strip()
    return "unreadable", out.strip() or "go / SCENARIOS 读不到（判不了 ≠ 通过）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="场景组合冒烟（消费方视角 go get + build）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    # 事实源前置：读不到 = 判不了，不许当通过（#79）
    if not gen_skill.SCENARIOS:
        return _finish(args, EXIT_UNREADABLE, ["SCENARIOS 为空——判不了场景组合"], [])
    if shutil.which("go") is None:
        return _finish(args, EXIT_UNREADABLE, ["go 工具缺失——判不了编译"], [])

    dead = dead_keys()
    if dead:
        return _finish(args, EXIT_VIOLATION,
                       [f"死键：{d}（数据错，冒烟不许带病跑）" for d in dead], [])

    combos = unique_combos()
    # stdout 行缓冲：管道/重定向时进度不落地 = 进程被杀一个字不留（consumer_probe 同款坑）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001 — reconfigure 不可用不挡主流程
        pass

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="oekit-smoke-") as td:
        for i, (mods, scenes) in enumerate(combos, 1):
            sub = Path(td) / f"c{i}"
            sub.mkdir()
            try:
                ok, err = run_combo(mods, sub)
            except FileNotFoundError:
                return _finish(args, EXIT_UNREADABLE,
                               ["go 工具在跑动中消失——判不了编译"], failures)
            tag = "✓" if ok else "❌"
            print(f"{tag} [{i}/{len(combos)}] {' '.join(mods)}  ← {'；'.join(scenes)}")
            if not ok:
                failures.append(err)
                print(f"   {err}")

    return _finish(args, EXIT_VIOLATION if failures else EXIT_OK,
                   failures, combos)


def _finish(args, rc: int, problems: list, combos) -> int:
    if args.json:
        print(json.dumps({
            "kind": {EXIT_OK: "ok", EXIT_VIOLATION: "violation",
                     EXIT_UNREADABLE: "unreadable"}[rc],
            "combos": len(combos) if combos else 0,
            "problems": problems,
        }, ensure_ascii=False, indent=1))
    else:
        kind, msg = classify(rc, "\n".join(problems))
        if kind == "ok":
            print(f"✓ {msg}（{len(combos)} 个去重组合）")
        else:
            print(f"❌ {msg}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
