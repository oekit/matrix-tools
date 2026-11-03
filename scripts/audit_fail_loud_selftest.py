#!/usr/bin/env python3
"""审计「**假绿**」三缺陷的自测：崩溃当成零漂移 / 深层 CHANGELOG 扫不到 /
门禁不可用只 ⚠ 不判红。

**为什么要有这个自测**（实测 2026-11-03，巡检复现）：一道没人验证过的
门禁，很可能自己就是个假绿。本次修的三条：

  1. **audit.py 一崩 ⇒ PR 门禁判成「零漂移」全绿**
     旧形态：audit.py 抛异常 ⇒ stdout **0 字节**（❌ 漂移行只在**末尾**
     统一打印，崩前一条都吐不出来）⇒ CI 的 grep -c '❌' = **0** ⇒
     「Fail on drift」步骤**不触发** ⇒ PR 全绿。
     修法：① CI 改判**退出码**（0 通过 / 1 真漂移 / 2 基础设施失败）并加
     「报告里没有成功标记就不许过」的兜底；② audit.py 自己把仓读不到
     记成漂移（不许让异常冒泡成空报告）；③ 未捕获异常统一映射到退出码 2。
  2. **嵌套 CHANGELOG 只扫一层** ⇒ 深度 4 的 kitexotel 独立 CHANGELOG
     同时脱离「解析器」与「独立 oracle」两条判据（两边同款深度口径 ⇒
     共同失明，违背 audit.py 自己写的「判据不复用被检对象逻辑」纪律）。
  3. **门禁不可用只 ⚠ 不判红** ⇒ 三道门禁在异常路径上**整条消失**，
     而报告照样印「✓ 矩阵审计通过」（判不了 ≠ 通过）。

**反证怎么构造**（全部离线：不联网、不写远端）：
  - 用例 1：空 base 跑 audit.py ⇒ 退出码非 0 **且**报告里有 ❌ 行；
  - 用例 2：注入未捕获异常 ⇒ 退出码必须是 **2**（不是 0、也不是 1）；
  - 用例 3：**把 audit.yml 的 run 块原样抽出来跑**（假 audit.py 桩），
            四种形态（干净 / 真漂移 / 崩了 / 旧式崩了）分别验「门禁判红还是绿」；
  - 用例 4：假仓树（深度 1 与深度 4 各一份带破坏性标记的 CHANGELOG）⇒
            深度 4 的那条**必须**被独立 oracle 判出来；
  - 用例 5：AST 扫描 ⇒ audit.py 里**每个** except Exception 分支都必须
            记进 issues（不许有门禁静默消失）。

**变异自证**：把实现改回旧写法，下列用例必须转红：
  - 模块循环不再兜住 repo_dir（回到裸调用）⇒ 用例 1 转红；
  - 崩溃处理器退出码改成 1 ⇒ 用例 2 转红；
  - oracle 改回「*/go.mod」（只扫一层）⇒ 用例 4 转红；
  - 三处 except Exception 改回只 print ⇒ 用例 5 转红；
  - audit.yml 改回「只 grep 报告文本」⇒ 用例 3 转红。

用法：python3 scripts/audit_fail_loud_selftest.py
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


def audit_src() -> str:
    return (HERE / "audit.py").read_text(encoding="utf-8")


def slice_between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


# ---------- 用例 1：audit.py 崩了不许变成「零漂移」 ----------

def case_missing_repo_is_drift() -> None:
    """空 base ⇒ 仓读不到 ⇒ **记成漂移**（❌ 行），退出码非 0。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "empty_base"
        base.mkdir()
        env = dict(os.environ, MATRIX_BASE=str(base))
        p = subprocess.run([sys.executable, str(HERE / "audit.py")],
                           capture_output=True, text=True, env=env, cwd=str(ROOT))
    out = p.stdout or ""
    check(p.returncode != 0,
          f"用例1：audit.py 在空 base 上必须**非 0** 退出（零漂移=假绿），实得 rc={p.returncode}")
    check("❌" in out,
          f"用例1：报告里必须有 ❌ 漂移行（读不到的仓必须被记成漂移），stdout={out[:200]!r}")
    check("仓检出失败" in out,
          f"用例1：模块循环必须把「仓读不到」记成漂移（旧写法让异常冒泡 ⇒ 报告空），"
          f"stdout={out[:300]!r}")


# ---------- 用例 2：未捕获异常 ⇒ 退出码 2（基础设施失败） ----------

def case_crash_exit_code() -> None:
    """未捕获异常 ⇒ 退出码 2（不是 0，也不是与「有漂移」撞码的 1）。"""
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.py"
        probe.write_text(
            "import os, sys\n"
            f"src = open({str(HERE / 'audit.py')!r}, encoding='utf-8').read()\n"
            "handler = src[src.index('def _audit_crash_handler'):"
            "src.index('sys.excepthook = _audit_crash_handler')]\n"
            "ns = {'sys': sys, 'os': os, 'issues': ['已发现的一条漂移']}\n"
            "exec(compile(handler, '<audit-slice>', 'exec'), ns)\n"
            "sys.excepthook = ns['_audit_crash_handler']\n"
            "raise RuntimeError('boom')\n",
            encoding="utf-8")
        p = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True)
    check(p.returncode == 2,
          f"用例2：未捕获异常必须映射到退出码 2（基础设施失败）——"
          f"1 与「有漂移」撞码、0 是假绿；实得 rc={p.returncode}")
    check("boom" in (p.stdout or "") + (p.stderr or ""),
          f"用例2：崩溃必须留下定位信息（boom）：{(p.stdout or '')[-200:]!r}")
    check("❌" in (p.stdout or ""),
          f"用例2：崩溃前的 issues 也要打出来（报告不许是空的）：{(p.stdout or '')[:200]!r}")


# ---------- 用例 3：audit.yml 的门禁必须判「有没有跑完」 ----------

def _step_block(text: str, step_name: str) -> list[str]:
    lines = text.splitlines()
    start = None
    for i, l in enumerate(lines):
        if l.strip() == f"- name: {step_name}":
            start = i
            break
    if start is None:
        return []
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("- name:") or s.startswith("- uses:"):
            return lines[start:j]
    return lines[start:]


def _run_of(block: list[str]) -> str:
    """取 step 的 run 块（按缩进去公共前缀）。"""
    try:
        j = next(i for i, l in enumerate(block) if l.strip() == "run: |")
    except StopIteration:
        return ""
    indent = len(block[j]) - len(block[j].lstrip())
    body: list[str] = []
    for l in block[j + 1:]:
        if not l.strip():
            body.append("")
            continue
        ind = len(l) - len(l.lstrip())
        if ind <= indent:
            break
        body.append(l)
    cut = indent + 2
    return "\n".join(l[cut:] if len(l) >= cut else l for l in body)


def _if_of(block: list[str]) -> str:
    for l in block:
        s = l.strip()
        if s.startswith("if:"):
            return s[3:].strip()
    return ""


def _eval_if(expr: str, event_name: str, outputs: dict) -> bool:
    """把 GitHub 表达式求成 bool（只支持本工作流用到的形态）。"""
    e = re.sub(r"github\.event_name", repr(event_name), expr)
    e = re.sub(r"steps\.audit\.outputs\.([A-Za-z0-9_]+)",
               lambda m: repr(str(outputs.get(m.group(1), ""))), e)
    e = e.replace("&&", " and ").replace("||", " or ")
    if not e.strip():
        return True
    return bool(eval(e, {"__builtins__": {}}, {}))   # noqa: S307 - 只喂本文件里的常量


FAKE_AUDIT = """#!/usr/bin/env python3
import os, sys
sys.stdout.write(os.environ.get("FAKE_OUT", ""))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""

CLEAN_REPORT = "远端 tag 读取：24/24 仓成功\n✓ 矩阵审计通过（24 模块 + 站点）\n"
DRIFT_REPORT = "发现 1 项漂移：\n  ❌ pkg: 地板落后（跑 align_floors.py）\n"

GATE_STEPS = (
    "Close previous drift issue",
    "Open drift issue",
    "审计未跑完 ⇒ 判红（基础设施失败，不是「零漂移」）",
    "Fail on drift (PR only)",
)


def _run_audit_step(run_block: str, rc: int, report: str, workdir: Path) -> dict:
    """**原样**执行 audit.yml 里的 run 块（假 audit.py 桩）⇒ 解析 GITHUB_OUTPUT。"""
    (workdir / "scripts").mkdir(parents=True, exist_ok=True)
    (workdir / "scripts" / "audit.py").write_text(FAKE_AUDIT, encoding="utf-8")
    out_file = workdir / "gh_output.txt"
    out_file.write_text("", encoding="utf-8")
    env = dict(os.environ, FAKE_RC=str(rc), FAKE_OUT=report, GITHUB_OUTPUT=str(out_file))
    # ⚠️ 用 GitHub Actions 的**默认 shell 语义**跑（bash -eo pipefail）：
    #   run 块里任何一条会失败的命令都会让整个步骤提前退出 ⇒ 判据必须写成
    #   「|| rc=$?」这种 errexit 安全形态，否则拿不到退出码。
    p = subprocess.run(["bash", "-eo", "pipefail", "-c", run_block],
                       capture_output=True, text=True, env=env, cwd=str(workdir))
    outputs: dict = {}
    for line in out_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            outputs[k.strip()] = v.strip()
    outputs["_step_rc"] = p.returncode
    return outputs


def case_workflow_gate() -> None:
    """audit.yml 的 PR 门禁：**崩了必须红**，漂移必须红，干净才绿。"""
    text = (ROOT / ".github" / "workflows" / "audit.yml").read_text(encoding="utf-8")
    run_block = _run_of(_step_block(text, "Run audit"))
    if not run_block:
        check(False, "用例3：audit.yml 里找不到「Run audit」步骤的 run 块")
        return

    # 结构性：判据必须看**退出码**，不许只看报告文本
    check("audit_rc=" in run_block,
          "用例3：Run audit 步骤必须把 audit.py 的**退出码**记下来（判据不许只看报告文本）")
    check("audit_ok=" in run_block,
          "用例3：Run audit 步骤必须算出 audit_ok（结论是否可信）")
    check("✓ 矩阵审计通过" in run_block,
          "用例3：Run audit 步骤必须有「报告里没有成功标记就不许过」的兜底")

    blocks = {}
    for name in GATE_STEPS:
        blk = _step_block(text, name)
        if not blk:
            check(False, f"用例3：audit.yml 里找不到步骤「{name}」")
            return
        blocks[name] = blk

    def job_failed(outputs: dict, event_name: str) -> bool:
        """按各步骤的 if 条件求值 ⇒ 这个 job 会不会红。"""
        for name in GATE_STEPS:
            blk = blocks[name]
            if not _eval_if(_if_of(blk), event_name, outputs):
                continue
            if "exit 1" in (_run_of(blk) or ""):
                return True
        return False

    def closed_issue(outputs: dict) -> bool:
        return _eval_if(_if_of(blocks["Close previous drift issue"]), "schedule", outputs)

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)

        # 形态①：干净 ⇒ PR 绿、周报关单
        o = _run_audit_step(run_block, 0, CLEAN_REPORT, wd)
        check(o.get("audit_ok") == "1", f"用例3①：干净报告必须 audit_ok=1，实得 {o}")
        check(o.get("drift") == "0", f"用例3①：干净报告必须 drift=0，实得 {o}")
        check(not job_failed(o, "pull_request"),
              f"用例3①：干净审计不得让 PR 门禁变红（实得 outputs={o}）")
        check(closed_issue(o),
              f"用例3①：周报形态零漂移且审计跑完 ⇒ 应关闭上一轮漂移 issue（实得 {o}）")

        # 形态②：真漂移 ⇒ PR 红、周报开单
        o = _run_audit_step(run_block, 1, DRIFT_REPORT, wd)
        check(o.get("audit_ok") == "1", f"用例3②：真漂移（有 ❌ 行）必须 audit_ok=1，实得 {o}")
        check(o.get("drift") == "1", f"用例3②：真漂移必须 drift=1，实得 {o}")
        check(job_failed(o, "pull_request"),
              f"用例3②：真漂移必须让 PR 门禁变红（实得 outputs={o}）")
        check(not closed_issue(o),
              f"用例3②：有漂移时不得关掉上一轮漂移 issue（实得 {o}）")

        # 形态③：audit.py 崩了（退出码 2、报告空）⇒ **PR 红**、周报**不关单**
        o = _run_audit_step(run_block, 2, "", wd)
        check(o.get("audit_ok") == "0", f"用例3③：崩溃必须 audit_ok=0，实得 {o}")
        check(o.get("drift") == "0",
              f"用例3③：崩溃时报告为空 ⇒ drift=0（正是假绿的由来），实得 {o}")
        check(job_failed(o, "pull_request"),
              f"用例3③：audit.py 崩了 ⇒ PR 门禁必须红（假绿修复点），实得 outputs={o}")
        check(job_failed(o, "schedule"),
              f"用例3③：audit.py 崩了 ⇒ 周报 job 也必须红（不许静默绿），实得 outputs={o}")
        check(not closed_issue(o),
              f"用例3③：审计没跑完 ⇒ **不许**把上一轮的真实漂移 issue 关掉（实得 {o}）")

        # 形态④：旧式崩法（Python 未捕获异常的退出码就是 1）⇒ 仍必须红
        o = _run_audit_step(run_block, 1, "", wd)
        check(o.get("audit_ok") == "0",
              f"用例3④：退出码 1 但报告里没有 ❌ 行 ⇒ 结论不可信 ⇒ audit_ok=0，实得 {o}")
        check(job_failed(o, "pull_request"),
              f"用例3④：退出码 1 + 空报告（旧式崩法）⇒ PR 门禁必须红，实得 outputs={o}")
        check(not closed_issue(o),
              f"用例3④：退出码 1 + 空报告 ⇒ 不许关单（实得 {o}）")


# ---------- 用例 4：独立 oracle 必须扫到**任意深度**的 CHANGELOG ----------

def _load_oracle(src: str, modules, base: Path):
    """抽出 oracle 判据（常量 + 两个 helper + check_breaking_oracle）并执行。"""
    from matrix import repo_dir  # noqa: PLC0415 - 延迟导入，避免加载全矩阵
    body = slice_between(src, "ORACLE_WORD_RE = re.compile(",
                         "# ---------- Go 版本地板三层口径")
    issues: list[str] = []
    ns: dict = {
        "re": re,
        "MODULES": modules,
        "repo_dir": repo_dir,
        "BASE": str(base),
        "issues": issues,
        "check": lambda cond, msg: None if cond else issues.append(msg),
        "print": lambda *a, **k: None,
    }
    exec(compile(body, "<audit-oracle>", "exec"), ns)   # noqa: S102 - 判据切片
    return ns, issues


def case_oracle_deep_changelog() -> None:
    """深度 4 的嵌套模块 CHANGELOG 的破坏性标记必须被独立 oracle 判出来。"""
    fake_gr = types.ModuleType("gen_versions")
    fake_gr.breaking_index = lambda repo: []      # noqa: E731 - 桩：矩阵里什么都没有
    saved = sys.modules.get("gen_versions")
    sys.modules["gen_versions"] = fake_gr
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "kitex"
            (repo / "istio").mkdir(parents=True)
            deep = repo / "observability" / "tracing" / "instrumentation" / "kitexotel"
            deep.mkdir(parents=True)
            (repo / "go.mod").write_text("module github.com/oekit/kitex\n", encoding="utf-8")
            (repo / "istio" / "go.mod").write_text(
                "module github.com/oekit/kitex/istio\n", encoding="utf-8")
            (deep / "go.mod").write_text(
                "module github.com/oekit/kitex/observability/tracing/instrumentation/kitexotel\n",
                encoding="utf-8")
            (repo / "CHANGELOG.md").write_text(
                "# Changelog\n\n## [v0.7.7] — 2026-11-01\n\n- feat: 无关紧要的改动\n",
                encoding="utf-8")
            (repo / "istio" / "CHANGELOG.md").write_text(
                "# Changelog\n\n## [v0.1.0] — 2026-11-01\n\n"
                "- **BREAKING**: 深度一层破坏性变更\n", encoding="utf-8")
            (deep / "CHANGELOG.md").write_text(
                "# Changelog\n\n## [v0.7.7] — 2026-11-01\n\n"
                "- **BREAKING**: 深度四层破坏性变更\n", encoding="utf-8")

            src = audit_src()
            _ns, issues = _load_oracle(src, ("kitex",), base)
            _ns["check_breaking_oracle"]()
            blob = "\n".join(issues)
            check("深度一层破坏性变更" in blob,
                  f"用例4：深度 1 的嵌套 CHANGELOG 必须被独立 oracle 判出来（回归护栏）：{issues}")
            check("深度四层破坏性变更" in blob,
                  f"用例4：**深度 4** 的嵌套 CHANGELOG 必须被独立 oracle 判出来"
                  f"（旧写法只扫一层 ⇒ kitexotel 独立 CHANGELOG 同时脱离两条判据）：{issues}")

            # 变异自证：改回「只扫一层」⇒ 深度 4 的那条必须漏判
            mut = src.replace('root.glob("**/go.mod")', 'root.glob("*/go.mod")')
            check(mut != src, "用例4变异：没找到 oracle 的 glob 调用点（代码形态已变？）")
            if mut != src:
                _ns2, issues2 = _load_oracle(mut, ("kitex",), base)
                _ns2["check_breaking_oracle"]()
                blob2 = "\n".join(issues2)
                check("深度四层破坏性变更" not in blob2,
                      f"用例4变异：把 oracle 退回「只扫一层」后，深度 4 的 CHANGELOG "
                      f"**仍然**被判出来了 ⇒ 本用例没有牙齿（issues={issues2}）")
    finally:
        if saved is not None:
            sys.modules["gen_versions"] = saved
        else:
            sys.modules.pop("gen_versions", None)


# ---------- 用例 5：门禁不可用 ⇒ 必须判红（不许整条消失） ----------

def _except_exception_violations(src: str) -> list[str]:
    tree = ast.parse(src)
    out: list[str] = []
    # 崩溃处理器（_audit_crash_handler）**不算门禁**：它已经在退出码 2 这条
    # 路上（判红），自己的兜底 try/except 不该再记 issues。
    skip = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_audit_crash_handler":
            for sub in ast.walk(fn):
                if isinstance(sub, ast.ExceptHandler):
                    skip.add(id(sub))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or id(node) in skip:
            continue
        t = node.type
        if not (isinstance(t, ast.Name) and t.id == "Exception"):
            continue
        marks = False
        for c in ast.walk(node):
            if not isinstance(c, ast.Call):
                continue
            f = c.func
            if isinstance(f, ast.Attribute) and f.attr == "append" and \
                    isinstance(f.value, ast.Name) and f.value.id == "issues":
                marks = True
            if isinstance(f, ast.Name) and f.id == "check":
                marks = True
        if not marks:
            out.append(f"行 {node.lineno}")
    return out


def case_gate_unavailable_is_red() -> None:
    """AST 扫描：每个 except Exception 分支都必须记进 issues。"""
    src = audit_src()
    violations = _except_exception_violations(src)
    check(not violations,
          "用例5：audit.py 里有 except Exception 分支只打印不判红 —— "
          + "；".join(violations))

    for what in ("llms.txt 标识符判据", "release 对账", "BOM 检查"):
        check(f'gate_unavailable("{what}"' in src,
              f"用例5：「{what}」的不可用路径必须走 gate_unavailable（判红）")

    # 变异自证：把三处改回「只 ⚠ 不判红」⇒ 用例 5 必须转红
    mut = src
    for what in ("llms.txt 标识符判据", "release 对账", "BOM 检查"):
        mut = mut.replace(f'issues.append(gate_unavailable("{what}", e))',
                          f'print(f"  ⚠ {what}不可用：{{e}}")')
    check(mut != src, "用例5变异：没找到 gate_unavailable 的三处调用（代码形态已变？）")
    if mut != src:
        mut_viol = _except_exception_violations(mut)
        check(mut_viol,
              f"用例5变异：把三处 except Exception 改回只 ⚠ 之后，本用例竟然没转红 "
              f"⇒ 没有牙齿（变异后仍判红的位置：{mut_viol}）")


def main() -> None:
    case_missing_repo_is_drift()
    case_crash_exit_code()
    case_workflow_gate()
    case_oracle_deep_changelog()
    case_gate_unavailable_is_red()

    if FAILURES:
        print("audit 假绿自测失败：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("✓ audit 假绿自测通过：崩了≠零漂移（退出码 2）、CI 判退出码+成功标记、"
          "深度 4 的嵌套 CHANGELOG 被独立 oracle 扫到、门禁不可用一律判红，"
          "变异自证有效")


if __name__ == "__main__":
    main()
