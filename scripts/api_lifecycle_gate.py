#!/usr/bin/env python3
"""API 生命周期门禁：清单 ↔ 包注释标记 ↔ 生产包集合，三处一致才绿。

为什么门禁住治理仓（本脚本）而不是 pkg 仓
-------------------------------------------------
pkg 是**零第三方依赖仓**（CI 强制 `go list -m all` 只允许本模块一行），
解析 YAML 需要 pyyaml ⇒ 门禁若住在 pkg 仓就破这条红线。
⇒ 判定逻辑放本仓（matrix-tools），pkg 仓只留声明面：
   - `api-lifecycle.yaml`（定级 + 理由 + 定级版本）
   - 各包注释里的 `// 生命周期: <级别>` 标记
   - `docs/api-compat.md`（口径书）
消费方的依赖图不受任何影响。这与 oekit/oekit 的 BOM 门禁同范式：
「判据脚本住在治理仓，本仓只有产物」。

为什么用行解析而不是 yaml 库
-----------------------------
matrix-tools **刻意不引 pyyaml**（见 dependabot_dirs.py 的同款纪律：
「不该为这一个门禁引入 pyyaml 依赖」）。本清单的 YAML 子集极简且结构固定
（`packages:` 下每条 `- pkg:` + 缩进的 `level:`/`since:`/`reason:`），
行解析足够，且**拼错字段名会直接判红**（行解析找不到该字段即报缺字段），
比 yaml 库的静默忽略更安全。

判据（三类集合级判定）
----------------------
1. 覆盖：清单 ↔ 生产包集合，双向判差（漏登记 / 幽灵登记都不许）；
2. 标记：清单 level ↔ 包注释标记，级别必须一致且**恰好一处**；
3. 自洽：每条登记的 reason 必填、since 形如 vX.Y.Z、level 在词表内；
4. 替代指向：replacement 指向的符号必须真实存在（对齐 kernel 的 checkReplacements）。

用法：
  python3 scripts/api_lifecycle_gate.py --repo <pkg 仓路径>
  退出码：0=一致 / 1=真漂移 / 2=读不到事实（判不了 ≠ 通过）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

LEVELS = {"Experimental", "Stable", "Deprecated"}
MANIFEST_NAME = "api-lifecycle.yaml"
# 本仓 module 路径：replacement 只能指向本仓内的符号（对齐 kernel 的 selfModule）
SELF_MODULE = "github.com/oekit/pkg"
MARKER_RE = re.compile(r"^生命周期[:：]\s*(Experimental|Stable|Deprecated)\b")
SINCE_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# 生产包排除项（每条都要写明理由，不允许调用方传参放宽）
SKIP_DIRS = {"testdata", "vendor", "node_modules", ".git"}


class Entry:
    __slots__ = ("pkg", "level", "since", "reason", "replacement")

    def __init__(self, pkg: str):
        self.pkg = pkg
        self.level = ""
        self.since = ""
        self.reason = ""
        self.replacement = ""


def parse_manifest(path: Path):
    """行解析 api-lifecycle.yaml。返回 (version, entries)；解析失败返回 None。

    行解析的一个刻意特性：**字段名拼错会判红**（找不到该字段 ⇒ 缺字段），
    而不是被静默忽略后让整条判据空转。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    version = 0
    entries = []
    cur = None
    in_packages = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" "):
            in_packages = stripped == "packages:"
            if stripped.startswith("version:"):
                try:
                    version = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    return None
            cur = None
            continue
        if not in_packages:
            continue
        m = re.match(r"^-\s*pkg:\s*(\S+)\s*$", stripped)
        if m:
            cur = Entry(m.group(1))
            entries.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"^(level|since|reason|replacement):\s*(.*)$", stripped)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key == "level":
                cur.level = val
            elif key == "since":
                cur.since = val
            elif key == "reason":
                cur.reason = val
            elif key == "replacement":
                cur.replacement = val
            else:
                return None  # 未知字段：清单拼错字段名必须判红
            continue
        # 续行（reason 的多行折叠写法）
        if cur.reason and stripped and not stripped.startswith("-"):
            cur.reason += " " + stripped
    return version, entries


def discover_packages(root: Path) -> list:
    """枚举全部生产包目录（相对路径，/ 分隔，字典序）。

    口径：每个**含生产 .go 文件**的目录都算（含 internal/、嵌套子包）。
    排除 testdata/vendor 等非源码目录与 .git。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel = Path(dirpath).relative_to(root).as_posix()
        if rel == ".":
            continue
        if any(f.endswith(".go") and not f.endswith("_test.go") for f in filenames):
            out.append(rel)
    return sorted(out)


def scan_markers(root: Path, pkg: str):
    """扫描一个包的包注释标记。

    返回 (标记列表 [(级别, 文件名)], import 路径列表)。
    标记只认**紧邻 package 声明之前的连续注释块**（即真正的 package doc）——
    散落在文件中间的同型注释不算标记（那不是 godoc 承载处）。
    """
    d = root / pkg
    hits = []
    imports = []
    for f in sorted(d.glob("*.go")):
        if f.name.endswith("_test.go"):
            continue
        try:
            src = f.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = src.splitlines()
        pkg_idx = None
        for i, ln in enumerate(lines):
            if re.match(r"^package\s+\w+", ln):
                pkg_idx = i
                break
        if pkg_idx is None:
            continue
        # 往上收集紧邻 package 的连续注释行
        j = pkg_idx - 1
        block = []
        while j >= 0 and lines[j].lstrip().startswith("//"):
            block.append(lines[j])
            j -= 1
        block.reverse()
        for c in block:
            t = c.lstrip().lstrip("/").strip()
            m = MARKER_RE.match(t)
            if m:
                hits.append((m.group(1), f.name))
        for m2 in re.finditer(r'"(github\.com/oekit/pkg[^"]*)"', src):
            imports.append(m2.group(1))
    return hits, imports


def split_replacement(repl):
    """拆「import 路径.符号名」⇒ (包相对路径, 符号名)；形态非法返回 None。

    对齐 kernel 的 splitReplacement：末段取符号名（用 rfind 而非 split——
    包路径本身可含点），import 路径必须落在本仓 module 之下，符号必须是导出名。
    """
    i = repl.rfind(".")
    if i <= 0 or i == len(repl) - 1:
        return None
    import_path = repl[:i]
    name = repl[i + 1:]
    if not import_path.startswith(SELF_MODULE + "/"):
        return None
    head = name[:1]
    if not head.isalpha() or not head.isupper():
        return None  # 导出符号：首字母大写（未导出 / 下划线开头一律不算）
    rel = import_path[len(SELF_MODULE) + 1:]
    if not rel or ".." in rel:
        return None
    return rel, name


def _strip_code(src):
    """去注释与字符串字面量 ⇒ 只留代码骨架（注释里的「type Foo」不算声明）。"""
    out = []
    in_block = False
    bs = chr(92)
    for raw in src.splitlines():
        buf = []
        i = 0
        n = len(raw)
        while i < n:
            ch = raw[i]
            if in_block:
                if ch == "*" and i + 1 < n and raw[i + 1] == "/":
                    in_block = False
                    i += 2
                    continue
                i += 1
                continue
            if ch == "/" and i + 1 < n and raw[i + 1] == "/":
                break
            if ch == "/" and i + 1 < n and raw[i + 1] == "*":
                in_block = True
                i += 2
                continue
            if ch == '"' or ch == "'" or ch == "`":
                quote = ch
                i += 1
                while i < n:
                    if quote != "`" and raw[i] == bs:
                        i += 2
                        continue
                    if raw[i] == quote:
                        i += 1
                        break
                    i += 1
                buf.append(" ")
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


def _first_word(text):
    """取首个标识符（泛型参数 [T] 与其后的类型都切掉）。"""
    head = text.split("[")[0].strip()
    parts = head.split()
    return parts[0] if parts else ""


def declared_names(pkg_dir):
    """包目录内**顶层**导出声明名（func / type / var / const）⇒ (名字集合, 读不到的文件)。

    与 kernel 的 go/ast 判据的差别：本实现是行解析——gofmt 保证顶层声明在第 0 列，
    括号分组（type (…)、var (…)、const (…）按「分组内首条的缩进」取条目名。
    读不到 .go 文件 ⇒ 记进 unreadable（判不了 ≠ 通过，不静默放过）。
    """
    names = set()
    unreadable = []
    for f in sorted(pkg_dir.glob("*.go")):
        if f.name.endswith("_test.go"):
            continue
        try:
            src = f.read_text(encoding="utf-8")
        except OSError:
            unreadable.append(f.name)
            continue
        in_group = False
        group_indent = -1
        for raw in _strip_code(src):
            if not raw.strip():
                continue
            indent = len(raw) - len(raw.lstrip())
            line = raw.strip()
            if in_group:
                if line.startswith(")"):
                    in_group = False
                    continue
                if group_indent < 0:
                    group_indent = indent
                if indent != group_indent:
                    continue  # 分组内的嵌套字面量 / 结构体字段，不是声明名
                body = line.split("=")[0]
                for part in body.split(","):
                    nm = _first_word(part)
                    if nm and nm[0].isalpha():
                        names.add(nm)
                continue
            if indent != 0:
                continue
            if line.startswith("func "):
                rest = line[5:].strip()
                if rest.startswith("("):
                    close = rest.find(")")
                    if close < 0:
                        continue
                    rest = rest[close + 1:].strip()
                nm = _first_word(rest.split("(")[0])
                if nm and nm[0].isalpha():
                    names.add(nm)
                continue
            if line in ("type (", "var (", "const ("):
                in_group = True
                group_indent = -1
                continue
            hit = False
            for kw in ("type ", "var ", "const "):
                if line.startswith(kw):
                    nm = _first_word(line[len(kw):])
                    if nm and nm[0].isalpha():
                        names.add(nm)
                    hit = True
                    break
            if hit:
                continue
    return names, unreadable


def check_replacements(repo, entries):
    """Deprecated 条目的替代指向必须真实存在（对齐 kernel checkReplacements）。

    判据：形态是「<selfModule>/<包>.<导出符号>」，包目录存在，且该包生产代码里
    确有同名导出顶层声明。指到不存在的符号 = 把调用方引向悬崖，比不给替代更糟。
    """
    probs = []
    for e in entries:
        if not e.replacement:
            continue
        parts = split_replacement(e.replacement)
        if parts is None:
            probs.append("%s: replacement %r 不是「%s/<包>.<导出符号>」形态"
                         % (e.pkg, e.replacement, SELF_MODULE))
            continue
        rel, sym = parts
        d = repo / rel
        if not d.is_dir():
            probs.append("%s: replacement 指向的包目录 %s 不存在——替代指向必须指到真的符号"
                         % (e.pkg, rel))
            continue
        names, unreadable = declared_names(d)
        for fn in unreadable:
            probs.append("%s: 包目录 %s 里的 %s 读不到——符号存在性**未核对**（判不了 ≠ 通过）"
                         % (e.pkg, rel, fn))
        if sym not in names:
            probs.append("%s: replacement 指向的符号 %s 不存在——替代指向必须指到真的符号"
                         % (e.pkg, e.replacement))
    return probs


def check(repo: Path) -> list:
    probs = []
    manifest_path = repo / MANIFEST_NAME
    if not manifest_path.exists():
        return ["读不到 %s：pkg 仓里没有 API 生命周期清单" % MANIFEST_NAME]
    parsed = parse_manifest(manifest_path)
    if parsed is None:
        return ["%s 解析失败（清单格式或字段名有误）" % MANIFEST_NAME]
    version, entries = parsed
    if version != 1:
        probs.append("清单 version = %d，本门禁只认 1" % version)
    # 自洽性
    seen = set()
    for e in entries:
        if not e.pkg:
            probs.append("清单里有一条登记没有 pkg")
            continue
        if e.pkg in seen:
            probs.append("%s: 清单里重复登记" % e.pkg)
        seen.add(e.pkg)
        if e.level not in LEVELS:
            probs.append("%s: level %r 不在词表（Experimental/Stable/Deprecated）" % (e.pkg, e.level))
        if not SINCE_RE.match(e.since):
            probs.append("%s: since %r 不是 vX.Y.Z 形态" % (e.pkg, e.since))
        if not e.reason:
            probs.append("%s: reason 为空——定级理由每条必填，没有理由的承诺无从复核" % e.pkg)
        if e.level == "Deprecated" and not e.replacement:
            probs.append("%s: Deprecated 必须给 replacement，否则调用方没有迁移路径" % e.pkg)
        if e.level != "Deprecated" and e.replacement:
            probs.append("%s: 只有 Deprecated 条目才能带 replacement" % e.pkg)
    # 覆盖：清单 ↔ 生产包集合（双向）
    actual = discover_packages(repo)
    manifest_set = {e.pkg for e in entries}
    actual_set = set(actual)
    for d in actual:
        if d not in manifest_set:
            probs.append("%s: 生产包未登记进 %s——新增包必须显式定级" % (d, MANIFEST_NAME))
    for e in entries:
        if e.pkg not in actual_set:
            probs.append("%s: 清单登记的包不存在（或已无生产 .go 文件）——删包要同步删登记" % e.pkg)
    # 标记：清单 level ↔ 包注释标记
    for e in entries:
        if e.pkg not in actual_set:
            continue
        hits, _ = scan_markers(repo, e.pkg)
        if len(hits) == 0:
            probs.append("%s: 包注释里没有「生命周期: <级别>」标记——在 package doc 里加一行，与清单 level 一致" % e.pkg)
        elif len(hits) > 1:
            files = "、".join(f for _, f in hits)
            probs.append("%s: 生命周期标记出现 %d 次（%s）——一个包只允许一处" % (e.pkg, len(hits), files))
        elif hits[0][0] != e.level:
            probs.append("%s: 包注释标记 %s 与清单 level %s 不一致（判据面在 %s）" % (e.pkg, hits[0][0], e.level, hits[0][1]))
    # 替代指向：符号必须真实存在（对齐 kernel checkReplacements）
    probs.extend(check_replacements(repo, entries))
    return probs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="pkg API 生命周期门禁")
    ap.add_argument("--repo", required=True, help="pkg 仓路径（仓根，含 api-lifecycle.yaml）")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print("读不到仓：%s" % repo, file=sys.stderr)
        return 2
    if not (repo / MANIFEST_NAME).exists():
        print("读不到 %s（%s）——判不了 ≠ 通过" % (MANIFEST_NAME, repo), file=sys.stderr)
        return 2
    probs = check(repo)
    if not probs:
        print("✅ API 生命周期门禁通过：%s" % repo)
        return 0
    print("❌ API 生命周期门禁不通过（%d 项）" % len(probs), file=sys.stderr)
    for p in probs:
        print("  - %s" % p, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
