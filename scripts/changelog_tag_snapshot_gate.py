#!/usr/bin/env python3
"""tag 快照 CHANGELOG 门禁（matrix-tools#234）。

**这条门禁防的是什么**：tag 打在了 CHANGELOG 提交**之前** ⇒ tag 快照的
CHANGELOG.md 顶部**停在上一版**——被钉版本的内容完全无文档。main 分支上的
CHANGELOG 看起来永远正常（后来补上了），逃逸只存在于 **tag 快照**这一面：
机器可读面（breakingIndex/matrix 从 CHANGELOG 读）照「文档」放行，下游照
「文档」升级，编译期才被拦（llm v0.19.0 的 Limiter breaking 即此形态，报错
不提真正原因）。

2026-11-06 抽验实录（oagentkit 系四模块全中）：
  toolgate v0.12.6 / queue v0.6.6 / tools v0.11.1 / llm v0.19.0 的 tag 快照
  CHANGELOG 顶部段全停在上一版；oekit 系同批四仓均含自身版本段——漏检集中
  在 oagentkit 系，说明这是 org 侧覆盖门禁的缺口而非生成器全局故障。

**判据**：对每仓选取「最新顶层 tag + 每个嵌套前缀各自的最新 tag」，读
**tag 快照**（ref=tag，非 main）的 CHANGELOG：
  - 顶层 tag `vX.Y.Z` ⇒ 根 CHANGELOG.md 必须含 `## [vX.Y.Z]` 段头；
  - 嵌套 tag `sub/vX.Y.Z` ⇒ 根 CHANGELOG.md 含 `## [sub/vX.Y.Z]`，**或**
    嵌套模块自己的 CHANGELOG.md（`sub/CHANGELOG.md`）含该版本段
    （带前缀或裸版本头都算——两种仓内形态并存，读法对齐
    changelog_section 的「根面优先、嵌套回退」）。

**退出码契约**（audit.py 子进程判定依据）：
  0 = 全部核对通过；1 = 有真漂移；2 = 读不到（网络/权限/限速）⇒ 判不了 ≠
  通过，audit 侧按 gate_unavailable 判红（#79 口径：读到空/读不到不得伪装绿）。

自测：scripts/changelog_tag_snapshot_selftest.py（注入读取层，无副作用、
不联网；--mutate 变异自证）。

用法：
    python3 scripts/changelog_tag_snapshot_gate.py                 # 全矩阵
    python3 scripts/changelog_tag_snapshot_gate.py --repo toolgate # 单仓
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ghsafe  # noqa: E402
from matrix import (  # noqa: E402
    MODULES, ORG, RemoteReadError, enable_tag_cache, latest_tag, remote_tags,
)

EXIT_OK, EXIT_DRIFT, EXIT_UNAVAILABLE = 0, 1, 2

# 历史逃逸豁免名单（#234 判出的存量，tag 不可删改 ⇒ 快照永久缺段）：
#   - logx hertzlog/v0.1.0、sink/v0.1.0：嵌套 tag 初版，根面未写子模块段；
#   - data all/v0.1.1：伞形 meta tag，根面顶部停在 v0.4.1。
# 豁免键 = "<repo>:<tag>"。名单只许增不许删（删了对应的门禁判定会转红，
# 而那个 tag 的快照内容是不可变的）——新条目须带 issue 链接注明缘由。
HISTORIC_SNAPSHOT_EXEMPT = {
    "logx:hertzlog/v0.1.0",
    "logx:sink/v0.1.0",
    "data:all/v0.1.1",
    # 2026-11-08 发版潮：修复轮打 tag 时 CHANGELOG 段未先行（段已在 main 补齐，
    # tag 快照不可变）——core/data/gormx/middleware 四仓同因；data v0.20.5 为
    # tag 抢在补段 PR 合入前的脏 tag（v0.20.6 已干净重发，代码一致）。
    "core:v0.13.2",
    "data:v0.20.4",
    "gormx:v0.11.5",
    "middleware:v0.13.14",
    "data:v0.20.5",
    # 2026-11-08 bootstrap#115 收口：nacos/obs 历史嵌套 tag 的段在 tag 发布后补齐
    # （快照不可变）——15 个，段已按「tag 切段取 conventional commit」重构进 main。
    "bootstrap:nacos/v0.2.0",
    "bootstrap:nacos/v0.2.1",
    "bootstrap:nacos/v0.2.2",
    "bootstrap:nacos/v0.2.3",
    "bootstrap:nacos/v0.2.4",
    "bootstrap:nacos/v0.2.5",
    "bootstrap:nacos/v0.3.1",
    "bootstrap:nacos/v0.3.2",
    "bootstrap:obs/v0.2.0",
    "bootstrap:obs/v0.2.1",
    "bootstrap:obs/v0.2.3",
    "bootstrap:obs/v0.2.4",
    "bootstrap:obs/v0.2.5",
    "bootstrap:obs/v0.2.6",
    "bootstrap:obs/v0.2.7",
    # 2026-11-08 第三轮清零发版潮：agent 模板残留「空占位括号/地板缺版本号」形态
    # （#259 文案 lint 首轮照见），快照不可变——Release 正文已订正，段留原样。
    "concurrency:v0.29.0",
    "core:v0.13.4",
    "gormx:v0.11.7",
    "logx:v0.8.11",
    "logx:hertzlog/v0.1.1",
    "logx:sink/v0.1.1",
    "network:v0.7.13",
    "pkg:v0.38.0",
}

# 段头匹配：`## [v0.12.6]` / `## [toolgate/v0.12.6]`（允许行尾尾注）。
def section_re(version: str) -> re.Pattern:
    return re.compile(r"^## \[(" + re.escape(version) + r")\]", re.M)


# ---------- 读取层（可注入：selftest 用内存版替身） ----------

def read_snapshot_file(repo: str, ref: str, path: str) -> str | None:
    """读某仓某 ref（tag 快照）下的文件原文；404 ⇒ None（该 ref 无此文件）；
    其余读失败 ⇒ 抛异常（调用方计入 unavailable，不许当「无漂移」）。"""
    q = f"/repos/{ORG}/{repo}/contents/{path}?ref={ref}"
    try:
        out, _err = ghsafe.run_gh(["api", q])
        data = json.loads(out)
    except ghsafe.GhCommandError as e:
        if e.is_not_found:
            return None
        raise RuntimeError(f"{repo}@{ref}:{path}: {e}") from e
    if data.get("encoding") != "base64":
        raise RuntimeError(f"{repo}@{ref}:{path}: 非预期 encoding {data.get('encoding')!r}")
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


# ---------- tag 选择 ----------

def pick_tags(tags: list[str]) -> list[str]:
    """从全部远端 tag 里选出要核对的面：最新顶层 v* tag + 每个嵌套前缀的
    最新 tag。嵌套前缀 = 含 `/` 且首段不是 v 开头的 tag（如 redis/v0.2.9、
    hlog/v0.1.6、observability/.../kitexotel/v0.7.4）。"""
    def vkey(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v))

    top: list[str] = [t for t in tags if "/" not in t and re.match(r"^v\d", t)]
    nested: dict[str, list[str]] = {}
    for t in tags:
        if "/" in t:
            prefix, _, ver = t.rpartition("/")
            if re.match(r"^v\d", ver):
                nested.setdefault(prefix, []).append(ver)
    picked: list[str] = []
    if top:
        picked.append(max(top, key=vkey))
    for prefix, vers in sorted(nested.items()):
        picked.append(f"{prefix}/{max(vers, key=vkey)}")
    return picked


# ---------- 判定 ----------

def check_tag(repo: str, tag: str, read_fn) -> str | None:
    """核对单个 tag 快照；返回漂移描述（None=通过）。读失败向上抛。

    ★ 漂移消息自带**可照抄的修复模板**（matrix-tools#259，2026-11-08 实测 3 条
      Release 正文是半成品：空括号 `（）`、前缀无版本 `（hertzlog/）`、「带出新的
      地板」后跟**上一版号**、缺分隔符——全是修复者自己造句造出来的）：模板带上
      快照顶部实际停留的上一版段名（嵌套补全成完整 tag 名）、本版号，并按
      **该 tag 的 go.mod 是否有 oekit 内部依赖**决定要不要「带出新的地板」半句
      （core/logx hertzlog/logx sink 这类零内部依赖模块写地板是无中生有）。
    """
    if "/" in tag:
        prefix, _, ver = tag.rpartition("/")
        root = read_fn(repo, tag, "CHANGELOG.md")
        if root is not None and section_re(f"{prefix}/{ver}").search(root):
            return None
        nested = read_fn(repo, tag, f"{prefix}/CHANGELOG.md")
        if nested is not None and (section_re(f"{prefix}/{ver}").search(nested)
                                   or section_re(ver).search(nested)):
            return None
        faces = []
        prev = None
        if root is not None:
            m = re.search(r"^## \[(.+?)\]", root, re.M)
            faces.append(f"根面顶部停在 {m.group(1) if m else '无段'}")
            if m:
                prev = m.group(1)
        if nested is not None:
            m = re.search(r"^## \[(.+?)\]", nested, re.M)
            faces.append(f"嵌套面顶部停在 {m.group(1) if m else '无段'}")
            if m and prev is None:
                prev = m.group(1)
        what = "；".join(faces) if faces else "两面均无此文件"
        return (f"{repo}: tag {tag} 快照 CHANGELOG 缺本版段（{what}）"
                f"——被钉版本无文档（#234）。{repair_template(repo, tag, prev, read_fn)}")
    root = read_fn(repo, tag, "CHANGELOG.md")
    if root is not None and section_re(tag).search(root):
        return None
    if root is None:
        return f"{repo}: tag {tag} 快照无 CHANGELOG.md——被钉版本无文档（#234）"
    m = re.search(r"^## \[(.+?)\]", root, re.M)
    prev = m.group(1) if m else None
    return (f"{repo}: tag {tag} 快照 CHANGELOG 顶部停在 "
            f"{prev or '无段'}——缺本版段，被钉版本无文档（#234）。"
            f"{repair_template(repo, tag, prev, read_fn)}")


# ---------- 修复模板与文案形状校验（matrix-tools#259） ----------

def repair_template(repo: str, tag: str, prev: str | None, read_fn) -> str:
    """「缺本版段」的可照抄修复模板（拼在漂移消息里，一行、可直接复制）。

    prev = 快照顶部实际停留的段头（= 上一版的段名）⇒ 修复者不用猜；
    嵌套面的裸版本头补全成完整 tag 名（#259 实测的 `（hertzlog/）` 空前缀即此）。
    有无「带出新的地板」半句按该 tag 快照 go.mod 的 oekit 内部依赖定——
    core/hertzlog/sink 这类零内部依赖模块谈地板是无中生有。
    """
    if "/" in tag:
        prefix, _, ver = tag.rpartition("/")
    else:
        prefix, ver = "", tag
    if prev and "/" not in prev and prefix and re.fullmatch(r"v\d+\.\d+\.\d+", prev):
        prev_full = f"{prefix}/{prev}"
    else:
        prev_full = prev or "<上一版 tag 名>"
    deps: list[str] | None
    try:
        gomod = read_fn(repo, tag, "go.mod")
    except Exception:  # noqa: BLE001 — 读不到按「未知」处理，不猜
        gomod = None
    self_mod = f"github.com/{ORG}/{repo}" + (f"/{prefix}" if prefix else "")
    deps = sorted(set(re.findall(r"(github\.com/oekit/[A-Za-z0-9._\-/]+)\s+v\d", gomod or ""))
                  - {self_mod}) if gomod is not None else None
    if deps:
        floor = f"并带出新的地板：{ver}"
        note = ""
    elif deps is not None:
        floor = ""
        note = "（本模块无 oekit 内部依赖，不要写「带出新的地板」半句）"
    else:
        floor = ""
        note = "（该 tag 的 go.mod 读不到：内部依赖面未核对，按无内部依赖处理，不要猜地板）"
    return (f"修复模板：「chore: 补齐本版版本节——上一个已发布 tag（{prev_full}）的 "
            f"CHANGELOG 快照缺本版段（tag 快照不可变），本版补上{floor}」{note}")


# 半成品文案的四种残缺形态（#259 实测的三条 Release 正文全中）：
#   ① 空括号 `（）`／`( )`；② 括号里只有路径前缀、没有版本号 `（hertzlog/）`；
#   ③ 「新的地板」后缺分隔符 `地板v0.13.2`；④ 「新的地板：vX」处不是本版号。
# ①的空括号判据只锚定「占位残留」形态（matrix-tools#286 实测误报）：全宽
# 空括号无条件命中；ASCII 空括号仅在「前面不是标识符」或「括号内有空白」
# 时命中——`Err()`/`Strict()`/`nc.Close()` 这类 Go 方法名后缀空括号是合法
# 行文（gormx v0.11.12 / network v0.9.2 两段被误判实测），不许判残缺。
_LINT_EMPTY_PAREN_RE = re.compile(
    r"（[\s\u3000]*）|(?<![\w.)\]])\(\s*\)|\(\s+\)")
_LINT_PREFIX_ONLY_RE = re.compile(r"[（(][A-Za-z0-9._\-]+/[\s]*[）)]")
_LINT_FLOOR_NO_SEP_RE = re.compile(r"新的地板\s*v\d")
_LINT_FLOOR_VER_RE = re.compile(r"新的地板\s*[:：]\s*(v\d+\.\d+\.\d+)")
_LINT_MSG = {
    "empty": "空占位括号（上一版 tag 名必须写全，不许留空）",
    "prefix": "括号里只有路径前缀、没有版本号（如 `（hertzlog/）`）",
    "nosep": "「新的地板」后缺分隔符（应为「新的地板：vX.Y.Z」）",
    "ver": "「新的地板：vX.Y.Z」处不是本版号",
}


def lint_repair_bullet(text: str, version: str) -> list[str]:
    """核一段本版文案的形状；返回问题清单（空 = 形状完好）。

    只锚定 #259 实测的残缺形态，不泛化成「文案里出现别的版本号就红」——
    CHANGELOG 里合法地会提到依赖的版本，泛化必误报。
    """
    problems: list[str] = []
    if _LINT_EMPTY_PAREN_RE.search(text):
        problems.append(_LINT_MSG["empty"])
    if _LINT_PREFIX_ONLY_RE.search(text):
        problems.append(_LINT_MSG["prefix"])
    if _LINT_FLOOR_NO_SEP_RE.search(text):
        problems.append(_LINT_MSG["nosep"])
    for m in _LINT_FLOOR_VER_RE.finditer(text):
        if m.group(1) != version:
            problems.append(f"{_LINT_MSG['ver']}（实为 {m.group(1)}，本版 {version}）")
    return problems


def extract_section(text: str | None, version: str) -> str | None:
    """取快照 CHANGELOG 里本版段原文（段头到下一个 `## [` 之前）；没有 ⇒ None。"""
    if not text:
        return None
    m = section_re(version).search(text)
    if not m:
        return None
    nxt = re.search(r"^## \[", text[m.end():], re.M)
    return text[m.start():m.end() + nxt.start()] if nxt else text[m.start():]


def lint_tag_section(repo: str, tag: str, read_fn) -> list[str]:
    """快照里**写出来了**的本版段也要核文案形状（#259：写错的段随 tag 永久留痕）。"""
    if "/" in tag:
        prefix, _, ver = tag.rpartition("/")
        root = read_fn(repo, tag, "CHANGELOG.md")
        text = extract_section(root, f"{prefix}/{ver}")
        if text is None:
            nested = read_fn(repo, tag, f"{prefix}/CHANGELOG.md")
            text = extract_section(nested, f"{prefix}/{ver}") \
                or extract_section(nested, ver)
    else:
        ver = tag
        text = extract_section(read_fn(repo, tag, "CHANGELOG.md"), ver)
    if text is None:
        return []   # 段不存在归 check_tag 管；这里只核「写出来的」
    return [f"{repo}: tag {tag} 快照本版段文案残缺：{p}"
            f"——快照不可变，只能靠后续版本订正 Release 正文（#259）"
            for p in lint_repair_bullet(text, ver)]


def run(repos: list[str], read_fn=read_snapshot_file, tags_fn=None) -> int:
    """跑门禁；返回退出码。tags_fn 缺省=远端事实（remote_tags，缓存+预取）。"""
    if tags_fn is None:
        enable_tag_cache()
        tags_fn = lambda repo: remote_tags(repo, None)  # noqa: E731

    drifts: list[str] = []
    unavailable: list[str] = []
    for repo in repos:
        try:
            tags = tags_fn(repo)
        except RemoteReadError as e:
            unavailable.append(f"{repo}: 远端 tag 读不到（{e}）")
            continue
        except Exception as e:  # noqa: BLE001
            unavailable.append(f"{repo}: {e}")
            continue
        picked = pick_tags(tags)
        if not picked:
            continue  # 无 tag 的仓（如 .github/oekit.github.io）：无可钉版本，合法
        for tag in picked:
            if f"{repo}:{tag}" in HISTORIC_SNAPSHOT_EXEMPT:
                print(f"  · {repo}:{tag} 在历史逃逸豁免名单（快照不可变，见名单注释）")
                continue
            try:
                d = check_tag(repo, tag, read_fn)
            except Exception as e:  # noqa: BLE001
                unavailable.append(str(e))
                continue
            if d:
                drifts.append(d)
                continue
            # ★ 段写出来了也要核文案形状（matrix-tools#259）：半成品文案随 tag
            #   永久留痕（快照不可变）⇒ 门禁必须在发布链路上拦下，不能只拦「缺段」。
            try:
                drifts.extend(lint_tag_section(repo, tag, read_fn))
            except Exception as e:  # noqa: BLE001
                unavailable.append(str(e))
    if unavailable:
        print(f"⚠ tag 快照 CHANGELOG 门禁不可用 {len(unavailable)} 项（读不到 ≠ 无漂移，#79）：")
        for u in unavailable:
            print(f"  ⚠ {u}")
    if drifts:
        print(f"❌ tag 快照 CHANGELOG 漂移 {len(drifts)} 项：")
        for d in drifts:
            print(f"  ❌ {d}")
        return EXIT_DRIFT
    if unavailable:
        return EXIT_UNAVAILABLE
    print(f"✓ tag 快照 CHANGELOG 门禁通过（{len(repos)} 仓）")
    return EXIT_OK


def main() -> int:
    args = sys.argv[1:]
    repos = [args[args.index("--repo") + 1]] if "--repo" in args else list(MODULES)
    return run(repos)


if __name__ == "__main__":
    sys.exit(main())
