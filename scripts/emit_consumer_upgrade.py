#!/usr/bin/env python3
"""下游消费方 oekit 地板升级命令生成器（matrix-tools#271）。

## 为什么要有这个

align_floors.py / maintenance_check.py 服务的是**矩阵内部**仓之间的地板对齐；
矩阵**下游**（zeus 7 仓 / fuxi 7 仓级别的外部消费方）的对齐没有工具面：
人工 grep go.mod 的 oekit 依赖、与远端最新 tag 对账、手工拼 go get——
2026-11-09 zeus+fuxi 14 仓升级实弹：17+ 模块对账全靠人工，go get 事务性
（一个版本号手滑全单回滚）三次实锤。

本脚本把「下游对齐」变成**可复制的命令**：

    python3 scripts/emit_consumer_upgrade.py --go-mod /path/to/go.mod
    python3 scripts/emit_consumer_upgrade.py --go-mod ... --diff   # 只列落差

- 读 go.mod 的 github.com/oekit/* require（根 + 嵌套都认）；
- 逐模块读远端最新 tag（gh api，事实源；不用 go list -m -versions——
  它读 proxy 缓存，会报已删 tag，见 verify_dep_floors 的判据来源注记）；
- 输出**单行可复制**的 go get 命令（含全部需升级模块），tag 存在性由
  远端读取本身保证；
- exit 0 = 无落差或已给出命令；exit 1 = 远端读取失败（基础设施错误）。

判据来源：matrix.py 共享事实层（MODULES/NESTED_MODULES/remote_tags）。
"""
import argparse
import re
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import (MODULES, NESTED_MODULES, RemoteReadError, latest_tag, nested_tag_prefix,  # noqa: E402
                   remote_tags)

GO_MOD_RE = re.compile(r"^\s*(github\.com/oekit/\S+)\s+(v\d+\.\d+\.\d+)", re.M)


def parse_go_mod(path):
    """读 go.mod，返回 {模块路径: 当前版本}。oekit 模块之外的行忽略。"""
    deps = {}
    with open(path, encoding="utf-8") as f:
        for m in GO_MOD_RE.finditer(f.read()):
            deps[m.group(1)] = m.group(2)
    return deps


def semver_key(v):
    try:
        return tuple(int(x) for x in v[1:].split("."))
    except ValueError:
        return (0, 0, 0)


def latest_for_module(module_path, base=None):
    """模块 import 路径 → 远端最新 tag。根模块 tag=裸 v；嵌套模块 tag=前缀/v。
    返回 (module_path, latest_tag)；查不到抛 RemoteReadError。"""
    short = module_path[len("github.com/oekit/"):]
    if short in MODULES:
        t = latest_tag(short, base=base, remote=True)
        if t:
            return t
        raise RemoteReadError(f"{module_path}: 根模块无 tag")
    for n in NESTED_MODULES:
        if n["module"] == module_path:
            prefix = nested_tag_prefix(module_path)
            tags = [t for t in remote_tags(n["repo"], base)
                    if t.startswith(prefix + "/")]
            if tags:
                return sorted(tags, key=semver_key)[-1]
            raise RemoteReadError(f"{module_path}: 嵌套模块无 tag")
    raise RemoteReadError(f"{module_path}: 不在 MODULES/NESTED_MODULES 注册表里")


def main():
    ap = argparse.ArgumentParser(description="下游消费方 oekit 升级命令生成器")
    ap.add_argument("--go-mod", required=True, help="消费方 go.mod 路径")
    ap.add_argument("--diff", action="store_true", help="只输出有落差的模块")
    ap.add_argument("--base", help="矩阵根目录（缺省 MATRIX_BASE/自动）")
    args = ap.parse_args()

    deps = parse_go_mod(args.go_mod)
    if not deps:
        print("# 该 go.mod 无 github.com/oekit/* 依赖")
        return 0
    rows, errors = [], []
    for mod, cur in sorted(deps.items()):
        try:
            latest = latest_for_module(mod, base=args.base)
        except RemoteReadError as e:
            errors.append(f"# {mod}: 远端读取失败（{e}）")
            continue
        lag = semver_key(latest) > semver_key(cur)
        if args.diff and not lag:
            continue
        mark = "→" if lag else "="
        rows.append((mod, cur, latest, mark))
    if errors:
        print("\n".join(errors))
        print("# ↑ 有模块远端读取失败，命令不完整，勿直接执行；修复后重跑")
        return 1
    if not rows:
        print("# 全部对齐，无落差")
        return 0
    print("# 当前 → 最新（每模块）")
    for mod, cur, latest, mark in rows:
        print(f"#   {mod} {cur} {mark} {latest}")
    to_get = [f"{mod}@{latest}" for mod, cur, latest, mark in rows if mark == "→"]
    if to_get:
        print("go get " + " ".join(to_get))
    else:
        print("# 无需升级命令")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
