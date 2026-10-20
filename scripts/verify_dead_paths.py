#!/usr/bin/env python3
"""独立 oracle 门禁：**死路径必须被 breakingIndex 提及**。

**为什么要有这条**：breakingIndex 的生成器靠「文本措辞」判断什么算破坏性变更，
措辞不受控——hertz v0.5.1 写「子包收成一级」「适配器扁平化」「可观测路径收拢」，
bootstrap v0.5.0 写「healthz 路径同步」，这些都不在生成器的动作词表里，
于是 4 条**下游 import 会直接构建失败**的死路径没进矩阵，而矩阵对下游说
「无破坏性变更」（实测 2026-10-20）。

**本脚本刻意不复用生成器的任何解析函数**——共享逻辑就会有共同盲区：
解析器漏收时两边一起漏、门禁全绿而矩阵对下游说谎。
本脚本的判据是**客观事实**：路径在文件系统里还在不在。

判据：
1. 扫各仓 CHANGELOG 原文，抽出所有 `github.com/oekit/<path>`；
2. 只看**归属本仓**的（首段 == 仓名）；
3. 在工作区里查该路径**还在不在**——不在 = 死路径；
4. 每条死路径必须在该仓 versions.json 的 breakingIndex 里被提及（全文路径 /
   去仓前缀 / 末段包名 任一命中即算覆盖）。

用法：
  python3 scripts/verify_dead_paths.py --base <27 仓工作区> [--versions <versions.json>]
  python3 scripts/verify_dead_paths.py --selftest

退出码：0 = 无未覆盖死路径；1 = 有未覆盖死路径（打印清单）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from matrix import MODULES, ORG, repo_dir  # noqa: E402

ORG_PATH_RE = re.compile(r"github\.com/" + re.escape(ORG) + r"/([a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*)")
URLISH = {"issues", "pull", "pulls", "requests", "blob", "tree", "releases",
          "actions", "wiki", "compare", "discussions", "labels", "milestones",
          "projects", "security", "graphs", "network", "settings"}


def dead_paths(repo: str, base: Path) -> list[tuple[str, str]]:
    """→ [(死路径, 首次出现的版本)]，只含归属本仓的 org 限定路径。"""
    cl = base / repo / "CHANGELOG.md"
    if not cl.is_file():
        return []
    text = cl.read_text(encoding="utf-8", errors="ignore")
    version = ""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        m = re.match(r"^##\s+\[([^\]]+)\]", raw)
        if m:
            version = m.group(1)
            continue
        for pm in ORG_PATH_RE.finditer(raw):
            rel = pm.group(1).rstrip("/")
            if not rel or rel.split("/")[0] != repo:
                continue                      # 归属别仓
            parts = rel.split("/")
            if len(parts) >= 2 and parts[1] in URLISH:
                continue                      # issue/pull 链接不是 import 路径
            if rel in out:
                continue
            if not (base / rel).exists():
                out[rel] = version or "?"
    return sorted(out.items())


# 迁移映射的旧侧 → 新侧。箭头是**结构性**标记，与中文措辞无关。
ARROW_RE = re.compile(r"`?([A-Za-z0-9_./-]+)`?\s*(?:→|->)\s*`?([A-Za-z0-9_./-]+)`?")


def _repo_relative(path: str, repo: str) -> str:
    """去掉 org 前缀与仓名前缀，得到仓内相对路径。"""
    p = path
    if p.startswith(f"github.com/{ORG}/"):
        p = p[len(f"github.com/{ORG}/"):]
    segs = p.split("/")
    if segs and segs[0] == repo:
        segs = segs[1:]
    return "/".join(segs)


def covered(path: str, blob: str, repo: str = "") -> bool:
    """该死路径是否在 breakingIndex 里被提及。

    判据按「语义覆盖」而非字面出现：
      ① 死路径本身（或其仓内相对形态、或末段包名）出现在 breakingIndex 里；
      ② **迁移映射的旧侧是死路径的祖先**也算覆盖——
         死路径 hertz/hertz/health/redischeck 在 CHANGELOG 里只出现在目录树代码块里，
         bullet 写的是父包的 `hertz/health` → `health`；消费者读到那条就知道
         双层路径没了、该用 health/redischeck。字面搜「redischeck」搜不到，
         但**语义上已告知**——门禁不该因为措辞形态而误报。
    """
    segs = path.split("/")
    candidates = {path}
    if len(segs) > 1:
        candidates.add("/".join(segs[1:]))
        candidates.add(segs[-1])
    if any(c and c in blob for c in candidates):
        return True

    # ② 祖先覆盖：旧侧是死路径仓内相对形态的前缀
    rel = _repo_relative(path, repo) if repo else path
    if not rel:
        return False
    for old, _new in ARROW_RE.findall(blob):
        raw = old.strip().strip("`")
        if not raw:
            continue
        # 旧侧有两种解释：原样（可能是旧双层路径形态，如 hertz/health 指
        # hertz/hertz/health）与去仓前缀（仓内相对形态，如 health）。
        # 两种都试——歧义本身就是「旧布局 vs 新布局」的表现。
        forms = {raw}
        if repo:
            forms.add(_repo_relative(raw, repo))
        for o in forms:
            if not o:
                continue
            if rel == o or rel.startswith(o.rstrip("/") + "/"):
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("MATRIX_BASE"))
    ap.add_argument("--versions", default=None)
    ap.add_argument("--org", default=ORG)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    base = Path(args.base or HERE.parent.parent).resolve()
    versions_path = Path(args.versions) if args.versions else (
        base / "oekit.github.io" / "docs" / "public" / "versions.json")
    if not versions_path.is_file():
        print(f"❌ 找不到 versions.json：{versions_path}")
        return 1
    data = json.loads(versions_path.read_text(encoding="utf-8"))
    blobs = {m["module"]: "\n".join(e.get("line", "") for e in (m.get("breakingIndex") or []))
             for m in data.get("modules", [])}

    total_dead = 0
    uncovered: list[tuple[str, str, str]] = []
    for repo in MODULES:
        if repo not in blobs:
            continue
        d = repo_dir(repo, str(base))
        if not d.is_dir():
            continue
        for path, ver in dead_paths(repo, base):
            total_dead += 1
            if not covered(path, blobs[repo], repo):
                uncovered.append((repo, path, ver))

    if not uncovered:
        print(f"✅ 死路径全部被 breakingIndex 提及（共 {total_dead} 条死路径）")
        return 0
    print(f"❌ 有 {len(uncovered)} 条死路径未被 breakingIndex 提及（下游 import 会构建失败）：")
    for repo, path, ver in uncovered:
        print(f"   {repo}  {path}  （提及于 {ver}）")
    print("\n修法：让生成器收录对应条目（判据是「旧路径在当前源码树里已不存在」），"
          "或在该版本节补写迁移说明后重生成。")
    return 1


def selftest() -> int:
    """自测：证明本脚本真能判出「未被提及的死路径」，且不误报。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "foo").mkdir()
        (base / "foo" / "CHANGELOG.md").write_text(
            "## [v0.1.0]\n\n- 迁出：`github.com/oekit/foo/old` → `github.com/oekit/foo/new`\n",
            encoding="utf-8")
        (base / "foo" / "new").mkdir()
        # old 不存在 → 死路径
        got = dead_paths("foo", base)
        ok1 = len(got) == 1 and got[0][0] == "foo/old"
        print(("✅" if ok1 else "❌"), f"自测1：死路径识别（得到 {got}）")
        # 覆盖判定
        ok2 = covered("foo/old", "本条讲 foo/old → foo/new 的迁移")
        ok3 = not covered("foo/old", "本条讲的是别的东西")
        print(("✅" if ok2 else "❌"), "自测2：已提及 → 算覆盖")
        print(("✅" if ok3 else "❌"), "自测3：未提及 → 不算覆盖")
        # 活路径不算死
        (base / "foo" / "old").mkdir()
        ok4 = len(dead_paths("foo", base)) == 0
        print(("✅" if ok4 else "❌"), "自测4：路径仍存在 → 不算死路径")
        return 0 if all([ok1, ok2, ok3, ok4]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
