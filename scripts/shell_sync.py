#!/usr/bin/env python3
"""门面壳同步检查：diff 源包导出符号（type/func/var）与门面壳的 alias/var
转发，拦截「源包新增导出符号时本壳需同步」的跟版税漏项。
（2026-10-16 曾漏 pkg v0.6.0 的 BatchGetter——靠人记不如 audit 拦。）

检查范围 = data 门面的八个壳。规则：
  - 源侧导出符号（跳过 const——壳无法 alias 常量；跳过方法——随类型走）
    必须在壳侧出现（type alias / var 转发 / generic.go 包装函数均算）；
  - 壳侧多出的符号（源已删）为残留，一并报告（会直接编译失败）；
  - **版本地板**（data#6）：壳引用的每个符号必须存在于 data/go.mod 钉住的那个源版本里。
    只比对源仓 HEAD 会漏掉「符号在 HEAD 有、在钉住的 tag 没有」——本地 go.work 把
    data 与源仓拼在一起 `go build` 绿，CI（GOWORK=off）才 `undefined: src.X`。
    壳缺符号时同样先核对地板：钉住版本不含该符号，则直接给出最早含全部缺失符号的
    tag 与 `go get` 命令，不把「要先升 go.mod」留给人猜。

用法：
  python3 scripts/shell_sync.py             # 检查全部壳，漂移则 exit 1
  python3 scripts/shell_sync.py --selftest  # 内置用例自检（解析/差集/地板逻辑）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable

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
# 根模块 tag（嵌套模块 tag 形如 sub/vX.Y.Z，不参与根包地板判定）
ROOT_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def parse_exported_text(text: str) -> set[str]:
    """解析一份 .go 源码的顶层导出符号名（type/func/var，跳过 const 与方法）。"""
    names: set[str] = set()
    group: str | None = None  # 处于某分组声明块内时的种类
    for line in text.splitlines():
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


def parse_exported(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        return parse_exported_text(f.read())


def package_symbols(pkg_dir: str) -> set[str]:
    """包内全部非测试 .go 文件的导出符号并集。"""
    out: set[str] = set()
    for fn in sorted(os.listdir(pkg_dir)):
        if fn.endswith(".go") and not fn.endswith("_test.go"):
            out |= parse_exported(os.path.join(pkg_dir, fn))
    return out


# ---------- 版本地板（data#6） ----------

def pinned_version(go_mod_text: str, src_repo: str) -> str | None:
    """从 data/go.mod 文本里取 github.com/oekit/<src_repo> 钉住的版本（无则 None）。"""
    m = re.search(rf"^\s*github\.com/oekit/{re.escape(src_repo)}\s+(v\S+)", go_mod_text, re.M)
    return m.group(1) if m else None


def _git(cwd: str, *args: str) -> str | None:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def symbols_at_tag(src_repo_dir: str, tag: str, src_sub: str) -> set[str] | None:
    """源仓某 tag 下 src_sub 包的导出符号集；tag 本地不可解析时返回 None（调用方跳过）。"""
    prefix = f"{src_sub}/" if src_sub else ""
    listing = _git(src_repo_dir, "ls-tree", "--name-only", tag, prefix or ".")
    if listing is None:
        return None
    out: set[str] = set()
    for path in listing.split():
        fn = path.rsplit("/", 1)[-1]
        if not fn.endswith(".go") or fn.endswith("_test.go"):
            continue
        # ls-tree 对根包列出的是顶层条目（含子目录名）；非 .go 已被上面过滤
        text = _git(src_repo_dir, "show", f"{tag}:{path}")
        if text is not None:
            out |= parse_exported_text(text)
    return out


def root_tags_sorted(src_repo_dir: str) -> list[str]:
    """源仓本地根模块 tag，按 SemVer 升序。"""
    listing = _git(src_repo_dir, "tag", "-l") or ""
    tags = [t for t in listing.split() if ROOT_TAG_RE.match(t)]
    return sorted(tags, key=lambda t: tuple(int(x) for x in t[1:].split(".")))


def first_tag_with(tags: Iterable[str], syms_at: Callable[[str], set[str] | None], need: set[str]) -> str | None:
    """按升序找最早「包含 need 全部符号」的 tag（符号集不可读的 tag 跳过）。"""
    for t in tags:
        s = syms_at(t)
        if s is not None and need <= s:
            return t
    return None


def floor_hint(data_dir: str, src_repo: str, src_repo_dir: str, src_sub: str,
               shell_syms: set[str], missing: list[str]) -> list[str]:
    """地板核对：返回需追加的报告行（空 = 地板无异常或无法核对）。"""
    try:
        with open(os.path.join(data_dir, "go.mod"), encoding="utf-8") as f:
            pinned = pinned_version(f.read(), src_repo)
    except OSError:
        return []
    if not pinned:
        return []
    at_pinned = symbols_at_tag(src_repo_dir, pinned, src_sub)
    if at_pinned is None:
        print(f"  （地板核对跳过：{src_repo} 本地无 tag {pinned}，先 git fetch --tags）", file=sys.stderr)
        return []
    out: list[str] = []
    # ① 壳已引用、但钉住版本里没有的符号——本地 go.work 掩盖，CI GOWORK=off 必炸
    broken = sorted(shell_syms - at_pinned)
    if broken:
        out.append(f"壳引用符号 {broken} 在 go.mod 钉住的 {src_repo} {pinned} 中不存在"
                   f"（本地 go.work 掩盖，CI GOWORK=off 会 undefined）→ 先升地板")
    # ② 壳缺符号——若钉住版本也没有，补转发会编译不过：先给出要升到的 tag
    need = set(missing) - at_pinned
    if need:
        target = first_tag_with(root_tags_sorted(src_repo_dir),
                                lambda t: symbols_at_tag(src_repo_dir, t, src_sub), set(missing))
        where = f"最早含全部缺失符号的 tag = {target}" if target else "本地已有 tag 均不含全部缺失符号（源仓尚未发版？）"
        cmd = f"GOWORK=off go get github.com/oekit/{src_repo}@{target}" if target else "等源仓发版后再升地板"
        out.append(f"地板：data/go.mod 钉 {src_repo} {pinned} 不含 {sorted(need)}；{where} → 先 `{cmd}` 再补壳")
    return out


def check_sync(base: str = BASE) -> list[str]:
    """返回漂移报告列表（空 = 全部同步）。"""
    issues: list[str] = []
    data_dir = str(repo_dir("data", base))
    for sub, src_repo, src_sub in SHELLS:
        shell_dir = os.path.join(data_dir, sub)
        src_repo_dir = str(repo_dir(src_repo, base))
        src_dir = os.path.join(src_repo_dir, src_sub) if src_sub else src_repo_dir
        shell_syms = package_symbols(shell_dir)
        src_syms = package_symbols(src_dir)
        missing = sorted(src_syms - shell_syms)
        stale = sorted(shell_syms - src_syms)
        label = f"data/{sub} ← {src_repo}/{src_sub}" if src_sub else f"data/{sub} ← {src_repo}"
        if missing:
            issues.append(f"{label}: 壳缺导出符号 {missing}（源仓新增未同步壳，跑壳同步）")
        if stale:
            issues.append(f"{label}: 壳残留符号 {stale}（源仓已删除，壳需清理）")
        # 地板只核对壳真正转发的符号（残留符号已单列，排除后免重复报）
        for line in floor_hint(data_dir, src_repo, src_repo_dir, src_sub, shell_syms - set(stale), missing):
            issues.append(f"{label}: {line}")
    return issues


def _selftest() -> bool:
    """内置用例：解析规则、差集逻辑、go.mod 地板解析、最早 tag 选取自检。"""
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
    ok = True
    got_src = parse_exported_text(src_go)
    got_shell = parse_exported_text(shell_go)
    want_src = {"Foo", "Bar", "Baz", "Exported"}
    if got_src != want_src:
        print(f"✗ 源侧解析 {sorted(got_src)} ≠ 期望 {sorted(want_src)}")
        ok = False
    missing = sorted(got_src - got_shell)
    if missing != ["Bar"]:
        print(f"✗ 差集 {missing} ≠ ['Bar']（分组别名必须被捕获）")
        ok = False

    go_mod = """module github.com/oekit/data

go 1.24.0

require (
	github.com/oekit/pkg v0.14.0
	github.com/oekit/redisx v0.8.0 // indirect
)
"""
    if pinned_version(go_mod, "redisx") != "v0.8.0" or pinned_version(go_mod, "pkg") != "v0.14.0":
        print("✗ go.mod 地板解析失败")
        ok = False
    if pinned_version(go_mod, "gormx") is not None:
        print("✗ go.mod 未钉住的仓应返回 None")
        ok = False

    history = {"v0.7.0": {"A"}, "v0.8.0": {"A", "B"}, "v0.9.0": {"A", "B", "Lockout"}}
    tags = ["v0.7.0", "v0.8.0", "v0.9.0"]
    if first_tag_with(tags, history.get, {"Lockout"}) != "v0.9.0":
        print("✗ 最早含符号的 tag 应为 v0.9.0")
        ok = False
    if first_tag_with(tags, history.get, {"B"}) != "v0.8.0":
        print("✗ 最早含符号的 tag 应为 v0.8.0")
        ok = False
    if first_tag_with(tags, history.get, {"Nope"}) is not None:
        print("✗ 无任何 tag 含该符号时应返回 None")
        ok = False
    semver_key = lambda t: tuple(int(x) for x in t[1:].split("."))  # noqa: E731
    if sorted(["v0.10.0", "v0.9.0", "v0.2.1"], key=semver_key) != ["v0.2.1", "v0.9.0", "v0.10.0"]:
        print("✗ tag 应按 SemVer 数值序而非字典序")
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
    print(f"✓ 门面壳同步检查通过（{len(SHELLS)} 壳，含 go.mod 版本地板核对）")
