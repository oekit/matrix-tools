#!/usr/bin/env python3
"""rerender_receipt.py 的自测：证明「重渲染历史回执」这条路径有牙齿。

#163 的对象是**已发布**的历史回执（pre-#153 形态：整段版本节当「变更内容」）。
本自测用**假 gh** 复刻真实 GitHub 的评论读写（REST 列表 + PATCH 编辑），
不联网、不改任何真实评论。

覆盖的用例：

1. pre-#153 回执 + 版本节能定位到本 issue
   ⇒ 重渲染后正文含「**本条修复**」且指向**正确的那条条目**；
   ⇒ **其余部分原样保留**（Release 行 / 合入证据行 / CI 行）；
   ⇒ **只编辑原评论**：不发新评论、不关单（PATCH 恰好 1 次）；
2. 版本节**只在叙述文字里**提到本 issue
   ⇒ 明说「未绑定到具体条目」，且**不做**「不含本条修复」这种事实断言（#158）；
3. 已是 #153 形态 ⇒ **幂等跳过**（不改写、不重复 PATCH）；
4. 没有回执 ⇒ 明确失败（不许「没找到就当没事」）；
5. 一条 issue 上有多条回执 ⇒ **要求 --tag 消歧**（不许瞎挑一条改）；
6. CLI 端到端（子进程）跑通（证明 main() 这条路也是通的）。

变异自证（每条必须**精确转红**，否则这份断言没有牙齿）：

- M1「变更内容段原样保留」（等于不重渲染）⇒ 用例1 转红
- M2「渲染退回 pre-#153 形态」（整段版本节）⇒ 用例1 转红
- M3「幂等检查失效」⇒ 用例3 转红
- M4「尾部（Release 行 / 合入证据）被丢掉」⇒ 用例1 的「其余部分保留」断言转红

用法：python3 scripts/rerender_receipt_selftest.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import matrix  # noqa: E402
import rerender_receipt  # noqa: E402

# 假 gh：复刻真实 GitHub 的「列评论」与「PATCH 编辑评论」。
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
cf = os.environ.get("FAKE_GH_COMMENTS_FILE", "")
pr = os.environ.get("FAKE_GH_PATCH_RECORD", "")
orc = os.environ.get("FAKE_GH_OTHER_RECORD", "")

def note(line):
    if orc:
        with open(orc, "a", encoding="utf-8") as f:
            f.write(line + "\n")

if args and args[0] == "api":
    method = args[args.index("--method") + 1] if "--method" in args else "GET"
    if method == "PATCH":
        path = args[args.index("--method") + 2]
        cid = path.rsplit("/", 1)[-1]
        inp = args[args.index("--input") + 1]
        with open(inp, encoding="utf-8") as f:
            body = json.load(f)["body"]
        if pr:
            with open(pr, "a", encoding="utf-8") as f:
                f.write(cid + "\n")
        data = []
        if cf and os.path.exists(cf):
            with open(cf, encoding="utf-8") as f:
                data = json.load(f)
        for c in data:
            if str(c.get("id")) == cid:
                c["body"] = body
        if cf:
            with open(cf, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        print(json.dumps({"id": int(cid), "body": body}, ensure_ascii=False))
        sys.exit(0)
    if "/comments" in " ".join(args):
        data = []
        if cf and os.path.exists(cf):
            with open(cf, encoding="utf-8") as f:
                data = json.load(f)
        print(json.dumps(data, ensure_ascii=False))
        sys.exit(0)
    print("{}")
    sys.exit(0)

if args and args[0] == "release" and args[1] == "view":
    rb = os.environ.get("FAKE_GH_RELEASE_BODY", "")
    if not rb:
        sys.stderr.write("release not found\n")
        sys.exit(1)
    print(json.dumps({"body": rb}, ensure_ascii=False))
    sys.exit(0)

# 其它调用（发评论 / 关单 / 查状态…）都记下来 ——
# 用来断言「重渲染**只编辑**原评论，不发新评论、不关单」。
note(" ".join(args))
print("{}")
'''

# 版本节：#45 绑在 **bullet 首行** ⇒ 定位器能绑到具体条目
CHANGELOG_BOUND = """## [v0.19.0] — 2026-10-21

- #45：回执重渲染：定位器能绑到本条 ⇒ 读者分得清

### 另一条修复

- #46：别的 issue 的修复
"""

# 版本节：#45 **只在叙述文字里**出现（顶格行）⇒ 绑不到具体条目
CHANGELOG_NARRATIVE = """## [v0.19.0] — 2026-10-21

### 修好了某件事

这节正文提到了 #45，但它不是 bullet ⇒ 定位器绑不到具体条目。

- 另一条修复
"""

# 两个版本都存在（用例5：一条 issue 上有两条回执，分别引不同版本）
CHANGELOG_TWO = """## [v0.19.0] — 2026-10-21

- #45：回执重渲染：定位器能绑到本条 ⇒ 读者分得清

### 另一条修复

- #46：别的 issue 的修复

## [v0.18.0] — 2026-10-20

- #45：更早那版的修复
"""


def old_receipt(tag: str = "v0.19.0") -> str:
    """一条 **pre-#153 形态**的历史回执（「变更内容」= 整段版本节）。"""
    return (
        f"已在 `{tag}` 修复并发布 ✅\n"
        "\n"
        "## 变更内容\n"
        "\n"
        "### 修好了某件事\n"
        "\n"
        "- 旧内容（整段版本节）\n"
        "\n"
        f"Release: https://github.com/oekit/pkg/releases/tag/{tag}\n"
        "\n"
        "**合入**：PR #70（https://github.com/oekit/pkg/pull/70），merge commit `18c00875`，2026-10-22T10:16:38Z\n"
        "**CI**：link ✅\n"
    )


def new_receipt(tag: str = "v0.19.0") -> str:
    """一条 **#153 形态**的回执（已按 issue 号定位过）。"""
    return (
        f"已在 `{tag}` 修复并发布 ✅\n"
        "\n"
        "## 变更内容\n"
        "\n"
        "**本条修复**（在 `v0.19.0` 的版本节里定位到）：\n"
        "\n"
        "- #45：回执重渲染：定位器能绑到本条 ⇒ 读者分得清\n"
        "\n"
        f"Release: https://github.com/oekit/pkg/releases/tag/{tag}\n"
    )


def make_env(tmp: Path, suffix: str, comments: list[dict], release_body: str,
             changelog: str) -> tuple[dict, Path, Path, Path]:
    """搭一套假环境（假 gh + 假仓检出根），返回 (env, 评论文件, PATCH 记录, 其它调用记录)。"""
    bindir = tmp / f"bin-{suffix}"
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    base = tmp / f"base-{suffix}"
    (base / "pkg").mkdir(parents=True, exist_ok=True)
    (base / "pkg" / "README.md").write_text("# fake pkg\n", encoding="utf-8")
    (base / "pkg" / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    cfile = tmp / f"comments-{suffix}.json"
    cfile.write_text(json.dumps(comments, ensure_ascii=False), encoding="utf-8")
    prec = tmp / f"patch-{suffix}.txt"
    orec = tmp / f"other-{suffix}.txt"

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GH_COMMENTS_FILE"] = str(cfile)
    env["FAKE_GH_PATCH_RECORD"] = str(prec)
    env["FAKE_GH_OTHER_RECORD"] = str(orec)
    env["FAKE_GH_RELEASE_BODY"] = release_body
    env["MATRIX_BASE"] = str(base)
    return env, cfile, prec, orec


def call(env: dict, repo: str, num: str, tag: str | None = None,
         apply: bool = True, force: bool = False) -> tuple[bool, str]:
    """在假环境里跑一次重渲染（进程内 ⇒ 变异可打桩）。"""
    saved_env = dict(os.environ)
    saved_base = matrix.DEFAULT_BASE
    try:
        os.environ.update(env)
        matrix.DEFAULT_BASE = env["MATRIX_BASE"]
        return rerender_receipt.rerender(
            repo, num, tag, apply=apply, force=force,
            backup_dir=Path(env["MATRIX_BASE"]).parent / f"backup-{repo}")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        matrix.DEFAULT_BASE = saved_base


def patched_body(cfile: Path, cid: int) -> str:
    data = json.loads(cfile.read_text(encoding="utf-8"))
    for c in data:
        if int(c.get("id", -1)) == cid:
            return c.get("body") or ""
    return ""


def run_all(tmp: Path, rid: str) -> list[str]:
    """跑一遍全部用例，返回 failures（空 = 全过）。"""
    failures: list[str] = []
    rel_body = "## 变更记录\n\n- 合入记录：fix(tools): 修好了（#45）\n"

    # ── 用例 1：pre-#153 回执 + 版本节能定位 ⇒ 必须重渲染并指向正确条目 ──
    env, cfile, prec, orec = make_env(
        tmp, f"{rid}-c1",
        [{"id": 900, "body": old_receipt(), "html_url": "https://example.invalid/c/900"}],
        rel_body, CHANGELOG_BOUND)
    ok, msg = call(env, "pkg", "45")
    if not ok:
        failures.append(f"用例1 重渲染失败：{msg[:200]}")
    patches = prec.read_text(encoding="utf-8").split() if prec.exists() else []
    if len(patches) != 1:
        failures.append(f"用例1 期望恰好 PATCH 1 次，实得 {len(patches)} 次")
    body = patched_body(cfile, 900)
    if "**本条修复**" not in body:
        failures.append("用例1 重渲染后正文没有「**本条修复**」标记"
                        f"——读者仍然分不清哪条是自己的：{body[:200]}")
    if "#45：回执重渲染" not in body:
        failures.append("用例1 重渲染后没有指向本 issue 的那条条目")
    if "Release: https://github.com/oekit/pkg/releases/tag/v0.19.0" not in body:
        failures.append("用例1 重渲染把 Release 行弄丢了（其余部分必须原样保留）")
    if "**合入**：PR #70" not in body:
        failures.append("用例1 重渲染把合入证据行弄丢了（其余部分必须原样保留）")
    if "- 旧内容（整段版本节）" in body:
        failures.append("用例1 重渲染后**旧的整段版本节还在**（新旧两份「变更内容」并存"
                        "⇒ 读者更分不清是哪一条）")
    if orec.exists() and orec.read_text(encoding="utf-8").strip():
        failures.append("用例1 重渲染**只该编辑原评论**，不该发新评论/关单："
                        f"{orec.read_text(encoding='utf-8')[:200]}")

    # ── 用例 2：版本节只在叙述文字里提到 ⇒ 明说「未绑定」，不做事实断言 ──
    env, cfile, prec, _ = make_env(
        tmp, f"{rid}-c2",
        [{"id": 901, "body": old_receipt(), "html_url": "https://example.invalid/c/901"}],
        rel_body, CHANGELOG_NARRATIVE)
    ok, msg = call(env, "pkg", "45")
    if not ok:
        failures.append(f"用例2 重渲染失败：{msg[:200]}")
    body = patched_body(cfile, 901)
    if "未绑定到具体条目" not in body:
        failures.append("用例2 定位不到条目时必须明说「未绑定到具体条目」"
                        f"（读者需要知道这版讲的是哪些修复）：{body[:200]}")
    if "不含本条修复" in body:
        failures.append("用例2 回执写了「不含本条修复」——那是关于版本内容的"
                        "**事实断言**，与事实可能相反（#158）")

    # ── 用例 3：已是 #153 形态 ⇒ 幂等跳过 ──
    env, cfile, prec, _ = make_env(
        tmp, f"{rid}-c3",
        [{"id": 902, "body": new_receipt(), "html_url": "https://example.invalid/c/902"}],
        rel_body, CHANGELOG_BOUND)
    ok, msg = call(env, "pkg", "45")
    if not ok:
        failures.append(f"用例3 期望成功返回，实得失败：{msg[:200]}")
    if prec.exists() and prec.read_text(encoding="utf-8").strip():
        failures.append("用例3 已是 #153 形态的回执必须幂等跳过（不该再改写）")
    if "跳过" not in msg:
        failures.append(f"用例3 期望报「跳过」，实得：{msg[:200]}")

    # ── 用例 4：没有回执 ⇒ 明确失败（不许当没事） ──
    env, _cfile, prec, _ = make_env(
        tmp, f"{rid}-c4",
        [{"id": 903, "body": "进度通报：v0.19.0 已打 tag，正在等 CI。\n",
          "html_url": "https://example.invalid/c/903"}],
        rel_body, CHANGELOG_BOUND)
    ok, msg = call(env, "pkg", "45")
    if ok:
        failures.append("用例4 期望失败（没有回执就没法重渲染），实得成功")
    if "没找到回执" not in msg:
        failures.append(f"用例4 期望报「没找到回执」，实得：{msg[:200]}")
    if prec.exists() and prec.read_text(encoding="utf-8").strip():
        failures.append("用例4 没有回执时不该有任何 PATCH")

    # ── 用例 5：一条 issue 上有多条回执 ⇒ 要求 --tag 消歧 ──
    env, cfile, prec, _ = make_env(
        tmp, f"{rid}-c5",
        [{"id": 904, "body": old_receipt("v0.19.0"), "html_url": "https://example.invalid/c/904"},
         {"id": 905, "body": old_receipt("v0.18.0"), "html_url": "https://example.invalid/c/905"}],
        rel_body, CHANGELOG_TWO)
    ok, msg = call(env, "pkg", "45")
    if ok:
        failures.append("用例5 期望失败（多条回执必须消歧），实得成功")
    if "--tag" not in msg:
        failures.append(f"用例5 期望提示用 --tag 消歧，实得：{msg[:200]}")
    if prec.exists() and prec.read_text(encoding="utf-8").strip():
        failures.append("用例5 消歧前不许改写任何回执")
    # 给了 --tag ⇒ 只改那一条
    ok, msg = call(env, "pkg", "45", tag="v0.18.0")
    if not ok:
        failures.append(f"用例5 指定 --tag 后应成功，实得失败：{msg[:200]}")
    if patched_body(cfile, 904) != old_receipt("v0.19.0"):
        failures.append("用例5 改错了那条回执（只该改 --tag 指定的那条）")
    if "**本条修复**" not in patched_body(cfile, 905):
        failures.append("用例5 指定的那条回执没被重渲染")

    # ── 用例 6：CLI 端到端（子进程） ──
    env, cfile, prec, _ = make_env(
        tmp, f"{rid}-c6",
        [{"id": 906, "body": old_receipt(), "html_url": "https://example.invalid/c/906"}],
        rel_body, CHANGELOG_BOUND)
    p = subprocess.run(
        [sys.executable, str(HERE / "rerender_receipt.py"), "pkg#45", "--apply"],
        capture_output=True, text=True, env=env)
    if p.returncode != 0:
        failures.append(f"用例6 CLI 期望退出码 0，实得 {p.returncode}："
                        f"{(p.stdout or '') + (p.stderr or '')[:200]}")
    if "**本条修复**" not in patched_body(cfile, 906):
        failures.append("用例6 CLI 跑完评论没有被重渲染")

    # ── 用例 7：默认**只预览**，不改任何东西 ──
    env, cfile, prec, _ = make_env(
        tmp, f"{rid}-c7",
        [{"id": 907, "body": old_receipt(), "html_url": "https://example.invalid/c/907"}],
        rel_body, CHANGELOG_BOUND)
    ok, msg = call(env, "pkg", "45", apply=False)
    if not ok:
        failures.append(f"用例7 预览失败：{msg[:200]}")
    if prec.exists() and prec.read_text(encoding="utf-8").strip():
        failures.append("用例7 默认只预览，不该有任何 PATCH")
    if "预览" not in msg:
        failures.append(f"用例7 期望报「预览」，实得：{msg[:200]}")

    return failures


# ── 变异自证 ────────────────────────────────────────────────────────────
# 变异必须**真的改掉被检行为**：第一版 M4 把整段塞进 head ⇒ 尾部其实**没丢**
# ⇒ 变异**无效**、断言自然抓不到 ⇒「变异没被抓住」在这里等于「变异没生效」，
# 不能当成「断言没牙齿」。⇒ 下面每个变异都对照**原始实现**写。
_ORIG = {
    "rebuild_body": rerender_receipt.rebuild_body,
    "build_change_content": rerender_receipt.build_change_content,
    "NEW_SHAPE_MARK": rerender_receipt.NEW_SHAPE_MARK,
    "split_receipt": rerender_receipt.split_receipt,
}


def _mutate_keep_change_section():
    """M1：变更内容段原样保留（等于不重渲染）。"""
    rerender_receipt.rebuild_body = (
        lambda body, sec, repo, num, tag, evidence: body)


def _mutate_pre153_rendering():
    """M2：渲染退回 pre-#153 形态（整段版本节当变更内容）。"""
    rerender_receipt.build_change_content = (
        lambda sec, repo, num, tag, evidence: sec or "")


def _mutate_no_idempotency():
    """M3：幂等检查失效（已是 #153 形态也照样改写）。"""
    rerender_receipt.NEW_SHAPE_MARK = "«永远不会出现的标记»"


def _mutate_drop_tail():
    """M4：尾部（Release 行 / 合入证据）被丢掉。"""
    def rebuild(body, sec, repo, num, tag, evidence):
        head, _mid, _tail = _ORIG["split_receipt"](body, repo, tag)
        return (head + "\n\n" + rerender_receipt.CHANGE_HEADING + "\n\n"
                + rerender_receipt.build_change_content(sec, repo, num, tag, evidence)
                + "\n")
    rerender_receipt.rebuild_body = rebuild


MUTATIONS = [
    ("变更内容段原样保留", _mutate_keep_change_section),
    ("渲染退回 pre-#153 形态", _mutate_pre153_rendering),
    ("幂等检查失效", _mutate_no_idempotency),
    ("尾部被丢掉", _mutate_drop_tail),
]


def main() -> None:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        failures = run_all(tmp, "base")
        if failures:
            print("✗ rerender_receipt 自测未过：")
            for f in failures:
                print("  -", f)
            raise SystemExit(1)

        saved = (rerender_receipt.rebuild_body, rerender_receipt.build_change_content,
                 rerender_receipt.NEW_SHAPE_MARK, rerender_receipt.split_receipt)
        for i, (name, mutate) in enumerate(MUTATIONS):
            try:
                mutate()
                fs = run_all(tmp, f"mut{i}")
            finally:
                (rerender_receipt.rebuild_body, rerender_receipt.build_change_content,
                 rerender_receipt.NEW_SHAPE_MARK,
                 rerender_receipt.split_receipt) = saved
            if not fs:
                print(f"✗ 变异「{name}」没有被抓住——这份断言没有牙齿")
                raise SystemExit(1)
            print(f"✓ 变异「{name}」被抓住（{len(fs)} 条断言转红）")

    print(f"✓ rerender_receipt 自测通过：历史回执可按 issue 号重渲染且只编辑原评论；"
          f"已是 #153 形态幂等跳过；无回执/多条回执明确报错；"
          f"变异自证 {len(MUTATIONS)}/{len(MUTATIONS)} 全部转红")


if __name__ == "__main__":
    main()
