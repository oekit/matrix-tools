#!/usr/bin/env python3
"""门面壳同步检查：diff 源包导出符号（type/func/var）与门面壳的 alias/var
转发，拦截「源包新增导出符号时本壳需同步」的跟版税漏项。
（2026-10-16 曾漏 pkg v0.6.0 的 BatchGetter——靠人记不如 audit 拦。）

检查范围 = data 门面的八个壳。规则：
  - 源侧导出符号（跳过 const——壳无法 alias 常量；跳过方法——随类型走）
    必须在壳侧出现（type alias / var 转发 / generic.go 包装函数均算）；
  - 壳侧多出的符号（源已删）为残留，一并报告（会直接编译失败）。

用法：
  python3 scripts/shell_sync.py             # 检查全部壳，漂移则 exit 1
  python3 scripts/shell_sync.py --selftest  # 内置用例自检（解析/差集逻辑）
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import repo_dir  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))

# (壳子包, 源仓, 源子包)——源子包为空表示源仓根包
SHELLS = [
    ("cache", "pkg", "cache"),
    ("rediscache", "redisx", "cache"),
    ("crud", "gormx", "crud"),
    ("distlock", "redisx", "distlock"),
    ("gormx", "gormx", ""),
    ("redisx", "redisx", ""),
    ("timertask", "timertask", ""),
    ("cachetest", "pkg", "cache/cachetest"),
]

# 顶层声明：type/var/func + 首个标识符。方法行（func (recv) Name）在此之前
# 被 FUNC_METHOD_RE 拦截；const 组整体跳过（壳无法 alias 常量）。
FUNC_METHOD_RE = re.compile(r"^func\s*\(")
DECL_RE = re.compile(r"^(type|var|func)\s+([A-Za-z_][A-Za-z0-9_]*)")
GROUP_OPEN_RE = re.compile(r"^(type|var|const)\s*\(\s*$")
GROUP_ITEM_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)")


def parse_exported(path: str) -> set[str]:
    """解析单个 .go 文件的顶层导出符号名（type/func/var，跳过 const 与方法）。"""
    names: set[str] = set()
    group: str | None = None  # 处于某分组声明块内时的种类
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            if group:
                if stripped == ")":
                    group = None
                    continue
                m = GROUP_ITEM_RE.match(stripped)
                # 组内项：`Name ...`；const 组的隐式重复项（无标识符）自然跳过
                if m and m.group(1)[0].isupper() and group != "const":
                    names.add(m.group(1))
                continue
            if FUNC_METHOD_RE.match(stripped):
                continue
            m = DECL_RE.match(line)
            if m:
                kind, name = m.group(1), m.group(2)
                if name[0].isupper():
                    names.add(name)
                continue
            gm = GROUP_OPEN_RE.match(stripped)
            if gm and gm.group(1) != "const":
                group = gm.group(1)
    return names


def package_symbols(pkg_dir: str) -> set[str]:
    """包内全部非测试 .go 文件的导出符号并集。"""
    out: set[str] = set()
    for fn in sorted(os.listdir(pkg_dir)):
        if fn.endswith(".go") and not fn.endswith("_test.go"):
            out |= parse_exported(os.path.join(pkg_dir, fn))
    return out


def check_sync(base: str = BASE) -> list[str]:
    """返回漂移报告列表（空 = 全部同步）。"""
    issues: list[str] = []
    for sub, src_repo, src_sub in SHELLS:
        shell_dir = os.path.join(repo_dir("data", base), sub)
        src_dir = repo_dir(src_repo, base)
        if src_sub:
            src_dir = os.path.join(src_dir, src_sub)
        shell_syms = package_symbols(shell_dir)
        src_syms = package_symbols(src_dir)
        missing = sorted(src_syms - shell_syms)
        stale = sorted(shell_syms - src_syms)
        label = f"data/{sub} ← {src_repo}/{src_sub}" if src_sub else f"data/{sub} ← {src_repo}"
        if missing:
            issues.append(f"{label}: 壳缺导出符号 {missing}（源仓新增未同步壳，跑壳同步）")
        if stale:
            issues.append(f"{label}: 壳残留符号 {stale}（源仓已删除，壳需清理）")
    return issues


def _selftest() -> bool:
    """内置用例：解析规则与差集逻辑自检。"""
    import tempfile

    src_go = """package src

// Doc comment 里提到 type Ghost 不应计入
type Foo[T any] interface {
	Get()
}

type (
	Bar[T any] = src.Bar[T]
	baz        // 未导出，跳过
)

const Konst = 1

func Baz(ctx context.Context) error
func (f *Foo[T]) method() // 方法随类型走，不计
var qux = 1
var Exported = 2
"""
    shell_go = """package shell

import src "example.com/src"

type Foo[T any] = src.Foo[T]
var Exported = src.Exported
func Baz(ctx context.Context) error
"""
    with tempfile.TemporaryDirectory() as d:
        sp, hp = os.path.join(d, "s.go"), os.path.join(d, "h.go")
        open(sp, "w").write(src_go)
        open(hp, "w").write(shell_go)
        got_src = parse_exported(sp)
        got_shell = parse_exported(hp)
    want_src = {"Foo", "Bar", "Baz", "Exported"}
    ok = True
    if got_src != want_src:
        print(f"✗ 源侧解析 {sorted(got_src)} ≠ 期望 {sorted(want_src)}")
        ok = False
    missing = sorted(got_src - got_shell)
    if missing != ["Bar"]:
        print(f"✗ 差集 {missing} ≠ ['Bar']（分组别名必须被捕获）")
        ok = False
    if ok:
        print("✓ shell_sync selftest 通过")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    drift = check_sync(BASE)
    if drift:
        print(f"门面壳同步漂移 {len(drift)} 项：")
        for d in drift:
            print(f"  ❌ {d}")
        sys.exit(1)
    print(f"✓ 门面壳同步检查通过（{len(SHELLS)} 壳）")
