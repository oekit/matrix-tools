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
    """核对单个 tag 快照；返回漂移描述（None=通过）。读失败向上抛。"""
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
        if root is not None:
            m = re.search(r"^## \[(.+?)\]", root, re.M)
            faces.append(f"根面顶部停在 {m.group(1) if m else '无段'}")
        if nested is not None:
            m = re.search(r"^## \[(.+?)\]", nested, re.M)
            faces.append(f"嵌套面顶部停在 {m.group(1) if m else '无段'}")
        what = "；".join(faces) if faces else "两面均无此文件"
        return f"{repo}: tag {tag} 快照 CHANGELOG 缺本版段（{what}）——被钉版本无文档（#234）"
    root = read_fn(repo, tag, "CHANGELOG.md")
    if root is not None and section_re(tag).search(root):
        return None
    if root is None:
        return f"{repo}: tag {tag} 快照无 CHANGELOG.md——被钉版本无文档（#234）"
    m = re.search(r"^## \[(.+?)\]", root, re.M)
    return (f"{repo}: tag {tag} 快照 CHANGELOG 顶部停在 "
            f"{m.group(1) if m else '无段'}——缺本版段，被钉版本无文档（#234）")


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
