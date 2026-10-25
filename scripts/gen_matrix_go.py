#!/usr/bin/env python3
"""生成 oekit 仓的兼容矩阵 BOM 模块（oekit/.github#8 方案 2）：可被 go get 的版本快照。

产物（写入 oekit 仓根）：
  go.mod   module github.com/oekit/oekit；require 全部矩阵模块（含嵌套模块）的最新
           tag 作为 MVS 地板——下游 `go get github.com/oekit/oekit@latest` 一次拉齐
           一组经矩阵 CI 验证的兼容版本，不会再静默落到互不兼容的旧组合。
  matrix.go 版本快照数据（Matrix []Module / Versions map / VersionOf），零 import，
           消费方可仅 import 本包把 Versions 暴露给 /version 端点。

注意：**不要在 oekit 仓跑 go mod tidy**——BOM 的 require 是「无 import 的地板」，
tidy 会把它们清掉（pkg/network 等仓根目录无 Go 包，空导入保活的 tools.go 模式不可
用）。误跑后重跑本脚本即恢复；audit 的 --check 门禁会在漂移时报红。

口径与站点 versions.json 同源：各仓**远端** tag（gen_versions._remote_tags，防本地
clone 陈旧）。数据模块 `data/all` 已废弃 retract，跳过。

用法：
  python3 scripts/gen_matrix_go.py                    # 生成（Version 沿用现有文件，无则 v0.1.0）
  python3 scripts/gen_matrix_go.py --version v0.1.0   # 指定本快照版本
  python3 scripts/gen_matrix_go.py --check            # 门禁：重新生成并比对（audit 调用）
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from matrix import MODULES, repo_dir, ensure_clean_all  # noqa: E402
from gen_versions import _remote_tags, latest_version  # noqa: E402

BASE = os.path.abspath(os.environ.get("MATRIX_BASE", os.path.join(os.path.dirname(__file__), "..", "..")))
OEKIT_DIR = os.path.join(BASE, "oekit")
ORG = "oekit"
MODULE_PATH = f"github.com/{ORG}/{ORG}"
BOM_GO = "1.26.0"  # 矩阵最高 go 地板（hertz/kitex 等 1.26 生态豁免仓决定）

# 已废弃的嵌套模块（tag 还在、模块不再发版，不进 BOM）
SKIP_NESTED = {("data", "all")}
NESTED_ONLY_TAGS = re.compile(r"^((?:[^/]+/)*[^/]+)/v\d+\.\d+\.\d+$")


def ensure_local_tag(repo_dir_path: str, tag: str) -> None:
    """本地 clone 缺该 tag 时补取一次（best-effort，失败不抛）。

    为什么需要：生成器的**版本取自远端 tag**（gen_versions.latest_version →
    `_remote_tags`），而 **Go 地板取自本地 clone**（`git show <tag>:go.mod`）——
    两路口径分叉。本地 clone 还没 fetch 到那个 tag 时（多仓并行发版 / clone 刚建 /
    网络不稳），`git show` 直接失败。

    这里在取地板前把缺的 tag 补齐，**减少**触发概率；**取不到仍由 go_floor 报错**
    ——fetch 本身可能因网络抖动失败，不能拿它兜底（那正是 #100 的成因）。

    只在 tag 确实缺失时才 fetch：稳态（clone 已是最新）零额外网络开销。
    """
    have = subprocess.run(["git", "-C", repo_dir_path, "rev-parse", "--verify", "--quiet",
                           f"refs/tags/{tag}"], capture_output=True, text=True)
    if have.returncode == 0:
        return
    # 失败不抛：真因由 go_floor 报（它的消息里带 git 的原始 stderr，更可诊断）
    subprocess.run(["git", "-C", repo_dir_path, "fetch", "--quiet", "--tags", "origin"],
                   capture_output=True, text=True)


def go_floor(repo_dir_path: str, tag: str) -> str:
    """取该 tag 的 go 指令（Go 地板）。

    **取不到就报错，绝不兜底**（matrix-tools#100）。

    旧实现取不到时静默返回默认地板 `1.24.0`——那不是「未知」而是**一个具体的、
    看起来合法的地板**，与「真的是 1.24.0 的仓」在产物里长得一模一样，下游无从分辨。
    实测已污染一个发布物：BOM v0.1.42 把 hertz（真实 `go 1.26.0`）写成 `1.24.0`，
    触发条件只是生成时本地 clone 尚未 fetch 到那个 tag。

    地板**低报比不报更危险**：不报会让人去查，报一个具体数字会让人以为那就是事实。
    ⇒ 宁可让生成失败（错误消息里带可执行的修复步骤），也不能写出一个假地板。
    """
    r = subprocess.run(["git", "-C", repo_dir_path, "show", f"{tag}:go.mod"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            f"{repo_dir_path}: 取不到 {tag} 的 go.mod（本地 clone 缺该 tag？）——"
            f"先 `git -C {repo_dir_path} fetch --tags` 再生成。\n"
            f"**不兜底**：兜底值会被写进 BOM 产物，与真实地板无法分辨"
            f"（已因此污染过 BOM v0.1.42，见 matrix-tools#100）。\n"
            f"git 说：{(r.stderr or r.stdout).strip()[:300]}")
    m = re.search(r"(?m)^go (\d+\.\d+(\.\d+)?)", r.stdout)
    if not m:
        raise SystemExit(
            f"{repo_dir_path}: {tag} 的 go.mod 里没有 go 指令——不兜底默认地板"
            f"（兜底值会写进 BOM 产物，与真实地板无法分辨，见 matrix-tools#100）")
    return m.group(1)


def collect() -> list[dict]:
    """[{repo, path, version, go, nested: bool}]，按 MODULES 自底向上序 + 嵌套字母序。"""
    out: list[dict] = []
    for repo in MODULES:
        d = str(repo_dir(repo, BASE))
        ver = latest_version(repo)
        if not ver:
            continue
        ensure_local_tag(d, ver)
        out.append({"repo": repo, "path": f"github.com/{ORG}/{repo}", "version": ver,
                    "go": go_floor(d, ver), "nested": False})
        seen: set[str] = set()
        nested: list[dict] = []
        for tag in _remote_tags(repo):
            m = NESTED_ONLY_TAGS.match(tag)
            if not m or m.group(1) in seen or (repo, m.group(1)) in SKIP_NESTED:
                continue
            seen.add(m.group(1))
            ensure_local_tag(d, tag)
            nested.append({"repo": repo, "path": f"github.com/{ORG}/{repo}/{m.group(1)}",
                           "version": tag.rsplit("/", 1)[1], "go": go_floor(d, tag), "nested": True})
        out.extend(sorted(nested, key=lambda x: x["path"]))
    return out


def next_version() -> str:
    r = subprocess.run(["git", "-C", OEKIT_DIR, "tag", "-l"], capture_output=True, text=True)
    tags = sorted(
        (t for t in r.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)),
        key=lambda t: tuple(int(x) for x in t[1:].split(".")),
    )
    if not tags:
        return "v0.1.0"
    major, minor, patch = (int(x) for x in tags[-1][1:].split("."))
    return f"v{major}.{minor}.{patch + 1}"


def render(modules: list[dict], version: str, generated_at: str) -> dict[str, str]:
    # require 地板按模块路径排序（与 go list 输出序一致）。注意：本仓不可跑
    # go mod tidy——require 无对应 import，tidy 会清掉；见文件头说明。
    go_mod = [f"module {MODULE_PATH}", "", f"go {BOM_GO}", "",
              "// ---- oekit BOM：矩阵兼容快照的地板 require（勿手改/勿 tidy，见 README）----",
              "require ("]
    for m in sorted(modules, key=lambda x: x["path"]):
        go_mod.append(f"\t{m['path']} {m['version']}")
    go_mod.append(")")

    lines = [
        "// Code generated by matrix-tools/scripts/gen_matrix_go.py; DO NOT EDIT.",
        f"// 生成时间 {generated_at}；口径与 https://oekit.github.io/versions.json 同源（各仓远端 tag）。",
        "",
        "// Package oekit 声明 oekit 矩阵当前互相兼容的模块版本快照（BOM，bill of materials）",
        "// （[oekit/.github#8](https://github.com/oekit/.github/issues/8)）。",
        "//",
        "// go.mod 以 require 地板钉住全部模块（含嵌套模块）版本：",
        "//",
        "//\tgo get github.com/oekit/oekit@latest   // 一次拉齐一组经矩阵 CI 验证的兼容版本",
        "//",
        "// 下游 MVS 被抬到快照版本（已用的模块升到快照口径，不会静默落到互不兼容的旧组合）；",
        "// 不需要整组对齐时按需 go get 各模块即可，本包零强制。长期跟随快照的服务可在某处",
        "// import 本包（如 /version 端点读 Versions），让 require 常驻 go.mod。",
        "//",
        "// 快照由 matrix-tools/scripts/gen_matrix_go.py 从各仓远端 tag 生成；矩阵版本变化后",
        "// 重跑生成并打新 tag（audit 有新鲜度门禁）。Breaking index 见站点 versions.json。",
        "//",
        "// 本仓 go.mod 的 require 是无 import 的地板，勿跑 go mod tidy（会清掉；误跑后重跑生成器）。",
        "package oekit",
        "",
        "// Version 是本快照自身的版本（与仓库 git tag 一致）。",
        f'const Version = "{version}"',
        "",
        "// GeneratedAt 是快照生成时间（UTC）。",
        f'const GeneratedAt = "{generated_at}"',
        "",
        "// Module 是矩阵中一个模块（根模块或嵌套模块）的版本声明。",
        "type Module struct {",
        "\t// Path 是 Go module 路径（github.com/oekit/pkg、github.com/oekit/logx/hlog）。",
        "\tPath string",
        "\t// Repo 是宿主仓库短名（pkg、logx）。",
        "\tRepo string",
        "\t// Version 是本快照钉住的版本。",
        "\tVersion string",
        "\t// Go 是该模块声明的最低 go 版本。",
        "\tGo string",
        "}",
        "",
        "// Matrix 是全部模块的版本声明：根模块按 matrix.py 依赖自底向上序，",
        "// 嵌套模块（logx/hlog、bootstrap/nacos 等）跟随宿主仓按路径字母序。",
        "var Matrix = []Module{",
    ]
    for m in modules:
        lines.append(f'\t{{Path: "{m["path"]}", Repo: "{m["repo"]}", Version: "{m["version"]}", Go: "{m["go"]}"}},')
    lines += [
        "}",
        "",
        "// Versions 是按 module 路径索引的版本（Matrix 的 map 视图）。",
        "var Versions = map[string]string{",
    ]
    for m in sorted(modules, key=lambda x: x["path"]):
        lines.append(f'\t"{m["path"]}": "{m["version"]}",')
    lines += [
        "}",
        "",
        "// VersionOf 返回模块路径对应的快照版本，未收录返回空串。",
        "func VersionOf(module string) string { return Versions[module] }",
        "",
    ]
    return {"go.mod": "\n".join(go_mod) + "\n",
            "matrix.go": "\n".join(lines)}


def gofmt_text(src: str) -> str:
    """渲染结果过一遍 gofmt（map 字面量对齐等），保证产物与 gofmt -w 一致。"""
    r = subprocess.run(["gofmt"], input=src, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gofmt 失败：{r.stderr[:300]}")
    return r.stdout


def existing_const(name: str, default: str) -> str:
    """读现有 matrix.go 里的字符串常量（Version / GeneratedAt），无则 default。
    --check 复用它们：只比对版本数据，时间戳与自身版本不算漂移。"""
    path = os.path.join(OEKIT_DIR, "matrix.go")
    if os.path.exists(path):
        m = re.search(r'const ' + name + r' = "([^"]+)"', open(path, encoding="utf-8").read())
        if m:
            return m.group(1)
    return default


def main() -> int:
    modules = collect()
    check = "--check" in sys.argv
    version = sys.argv[sys.argv.index("--version") + 1] if "--version" in sys.argv else existing_const("Version", "v0.1.0")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    generated_at = existing_const("GeneratedAt", now) if check else now
    files = render(modules, version, generated_at)
    files["matrix.go"] = gofmt_text(files["matrix.go"])
    if not check:
        ensure_clean_all([OEKIT_DIR], "gen_matrix_go")  # 产物必须落在已提交基线上
    changed = []
    for name, content in files.items():
        target = os.path.join(OEKIT_DIR, name)
        old = open(target, encoding="utf-8").read() if os.path.exists(target) else None
        if old != content:
            changed.append(name)
        if not check:
            open(target, "w", encoding="utf-8").write(content)

    if check:
        if changed:
            print(f"❌ oekit BOM 陈旧（跑 gen_matrix_go.py 重生成并打新 tag）：{', '.join(changed)}")
            return 1
        print(f"✓ oekit BOM 与远端 tag 一致（{version}，{len(modules)} 模块）")
        return 0

    d = subprocess.run(["git", "-C", OEKIT_DIR, "status", "--porcelain"],
                       capture_output=True, text=True).stdout.strip()
    print(f"✓ oekit BOM 已生成（{version}，{len(modules)} 模块）；变更文件：\n{d or '  （无变化）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
