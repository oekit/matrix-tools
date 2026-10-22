#!/usr/bin/env python3
"""ghsafe 的自测：证明「读到的空」不会再被当成「事实为空」。

**为什么要有这个自测**：audit.yml 的 selftest job 有个原则——
「判据脚本自己先得能证明『它抓得住自己要防的东西』」。
这条判据的特殊之处在于它**防的是假绿与假红两个方向**：

- 旧语义「读到空 = 事实为空」⇒ audit 报**假漂移**
  （2026-10-22 实测 11 个仓被报「N 个 tag 缺 Release」，真实 parity 是 24/24 全绿）；
- 另一处旧语义「读到空 = 没有这个对象」⇒ `close_issue` 把 PR 当 issue 关掉
  （v0.1.8 刚修掉的坑从这条缝复活）。

**怎么复刻真实故障**：假 gh 可以按 spec 回放
「前 N 次 exit 0 + 空正文、之后才有数据」「永远空」「命令失败（403/404）」
这三种真实形态——真实故障就是 GitHub 偶发返回空集合
（`GET /repos/{o}/{r}/releases` 返回 `200` + `[]`，curl 直连同样可复现）。

**反证**：把 `ghsafe.gh_json` 退回旧语义（空正文当成 `{}`），
本自测必须在用例 B/D/H/K 上失败——红在**断言**上（不是编译/导入层）。

**无副作用、不联网、不依赖工作区布局**：假 gh 放在临时目录并前置到 PATH。

用法：python3 scripts/ghsafe_selftest.py
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
REPO = HERE.parent
SCRIPTS = str(HERE)

FAKE_GH = r'''#!/usr/bin/env python3
"""假 gh：按 FAKE_GH_SPEC 回放响应，复刻「exit 0 + 空正文」这类真实故障。"""
import json
import os
import sys

spec = json.loads(os.environ.get("FAKE_GH_SPEC", "{}"))
args = sys.argv[1:]

log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write("\t".join(args) + "\n")

state_dir = os.environ.get("FAKE_GH_STATE", "")
for rule in spec.get("rules", []):
    prefix = rule.get("prefix", [])
    if args[:len(prefix)] != prefix:
        continue
    key = rule.get("key") or "_".join(prefix)
    key = key.replace("/", "_")          # key 会当文件名用，不能带路径分隔符
    n = 0
    if state_dir:
        path = os.path.join(state_dir, key)
        if os.path.exists(path):
            try:
                n = int(open(path, encoding="utf-8").read() or "0")
            except ValueError:
                n = 0
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(n + 1))
    if n < int(rule.get("empty_times", 0)):
        sys.stdout.write(rule.get("empty_body", ""))
        sys.exit(int(rule.get("empty_exit", 0)))
    sys.stdout.write(rule.get("body", ""))
    sys.exit(int(rule.get("exit", 0)))

sys.stdout.write(spec.get("default_body", ""))
sys.exit(int(spec.get("default_exit", 0)))
'''


def run_case(name: str, snippet: str, spec: dict, expect: str) -> bool:
    """跑一个用例：返回是否通过。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        fake = tmpd / "gh"
        fake.write_text(FAKE_GH, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        state = tmpd / "state"
        state.mkdir()
        log = tmpd / "calls.log"

        env = dict(os.environ)
        env["PATH"] = f"{tmpd}{os.pathsep}{env.get('PATH','')}"
        env["FAKE_GH_SPEC"] = json.dumps(spec)
        env["FAKE_GH_STATE"] = str(state)
        env["FAKE_GH_LOG"] = str(log)
        env["PYTHONPATH"] = f"{SCRIPTS}{os.pathsep}{REPO}"
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)

        p = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                           text=True, env=env, cwd=str(REPO))
        out = (p.stdout or "").strip().splitlines()
        got = out[-1].strip() if out else f"<no output; rc={p.returncode}>"

        # 统计各类调用次数（反证用：必须证明脚本**重试过**）
        counts: dict[str, int] = {}
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                head = "\t".join(line.split("\t")[:2])
                counts[head] = counts.get(head, 0) + 1

        ok = (got == expect)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print(f"         期望 {expect!r}，实得 {got!r}")
            if p.stderr.strip():
                print(f"         stderr: {p.stderr.strip()[:300]}")
        return ok, counts


AUTH_OK = {"prefix": ["auth", "status"], "body": "github.com\n  ✓ Logged in\n", "exit": 0}
AUTH_NO = {"prefix": ["auth", "status"], "body": "not logged in\n", "exit": 1}

RELEASE_LIST = ["release", "list"]
RELEASE_API = ["api", "--paginate", "repos/oekit/core/releases?per_page=100"]


def main() -> int:
    results: list[bool] = []
    counts_all: dict[str, dict[str, int]] = {}

    def case(name, snippet, spec, expect):
        ok, counts = run_case(name, snippet, spec, expect)
        results.append(ok)
        counts_all[name] = counts

    print("ghsafe 自测：空结果不得被当成事实为空")

    # A. 前 2 次空、第 3 次才有数据 ⇒ 必须重试到拿到数据
    case("A gh_json 遇到空正文会重试（不是直接当成空）",
         "import ghsafe; print(ghsafe.gh_json(['api','x']))",
         {"rules": [AUTH_OK, {"prefix": ["api", "x"], "empty_times": 2, "body": '{"a":1}'}]},
         "{'a': 1}")

    # B. 永远空 ⇒ 必须报错，不能返回空/None
    case("B gh_json 永远读到空 ⇒ 抛 GhReadError",
         ("import ghsafe\n"
          "try:\n"
          "    print(ghsafe.gh_json(['api','x']))\n"
          "except ghsafe.GhReadError:\n"
          "    print('GHREAD_ERROR')\n"),
         {"rules": [AUTH_OK, {"prefix": ["api", "x"], "empty_times": 99, "body": ""}]},
         "GHREAD_ERROR")

    # C. 404 ⇒ GhCommandError 且 is_not_found=True（与「读不到」区分开）
    case("C gh_json 404 ⇒ GhCommandError.is_not_found",
         ("import ghsafe\n"
          "try:\n"
          "    ghsafe.gh_json(['api','x'])\n"
          "except ghsafe.GhCommandError as e:\n"
          "    print('NOT_FOUND' if e.is_not_found else 'OTHER_ERROR')\n"),
         {"rules": [AUTH_OK, {"prefix": ["api", "x"], "exit": 1,
                              "body": "gh: Not Found (HTTP 404)\n"}]},
         "NOT_FOUND")

    # D. 空容器 [] ⇒ 读不到（空容器正是「这仓什么都没有」的来源）
    case("D gh_json 读到空容器 [] ⇒ 抛 GhReadError",
         ("import ghsafe\n"
          "try:\n"
          "    print(ghsafe.gh_json(['api','x']))\n"
          "except ghsafe.GhReadError:\n"
          "    print('GHREAD_ERROR')\n"),
         {"rules": [AUTH_OK, {"prefix": ["api", "x"], "body": "[]\n"}]},
         "GHREAD_ERROR")

    # E. existing_releases：两条路径都先读到空、第 3 轮才有数据 ⇒ 必须拿到真实集合
    #    （旧实现第一轮读到空就返回空集合 ⇒ audit 会报「全部 tag 缺 Release」的假漂移）
    case("E existing_releases 抖动后仍拿到真实 Release 集合",
         ("import gen_releases\n"
          "print(sorted(gen_releases.existing_releases('core')))\n"),
         {"rules": [AUTH_OK,
                    {"prefix": RELEASE_LIST, "key": "rel_list", "empty_times": 2,
                     "body": "v0.1.0\nv0.2.0\n"},
                    {"prefix": RELEASE_API, "key": "rel_api", "empty_times": 2,
                     "body": "v0.1.0\nv0.2.0\n"}]},
         "['v0.1.0', 'v0.2.0']")

    # F. existing_releases：真的一个 Release 都没有 ⇒ 空集合（真漂移仍要报得出来）
    case("F existing_releases 确实没有 Release ⇒ 返回空集合",
         ("import gen_releases\n"
          "print(sorted(gen_releases.existing_releases('core')))\n"),
         {"rules": [AUTH_OK,
                    {"prefix": RELEASE_LIST, "key": "rel_list", "empty_times": 99},
                    {"prefix": RELEASE_API, "key": "rel_api", "empty_times": 99}]},
         "[]")

    # G. existing_releases：命令失败（权限）⇒ 抛 ReleaseUnreachable（按「不可核对」跳过）
    case("G existing_releases 读不到 ⇒ ReleaseUnreachable",
         ("import gen_releases\n"
          "try:\n"
          "    gen_releases.existing_releases('core')\n"
          "    print('NO_ERROR')\n"
          "except gen_releases.ReleaseUnreachable:\n"
          "    print('UNREACHABLE')\n"),
         {"rules": [AUTH_OK,
                    {"prefix": RELEASE_LIST, "exit": 1,
                     "body": "gh: Resource not accessible by personal access token (HTTP 403)\n"},
                    {"prefix": RELEASE_API, "exit": 1,
                     "body": "gh: Resource not accessible by personal access token (HTTP 403)\n"}]},
         "UNREACHABLE")

    # H. target_is_pr：读不到 ⇒ 必须中止，绝不能当成「不是 PR」
    case("H close_issue 读不到元数据 ⇒ 中止（不当成不是 PR）",
         ("import close_issue\n"
          "try:\n"
          "    print('IS_PR' if close_issue.target_is_pr('core','1') else 'IS_ISSUE')\n"
          "except SystemExit:\n"
          "    print('ABORTED')\n"),
         {"rules": [AUTH_OK, {"prefix": ["api", "repos/oekit/core/issues/1"],
                              "empty_times": 99}]},
         "ABORTED")

    # I. target_is_pr：REST 说不是 PR，但 gh pr view 说它是 PR ⇒ 以 PR 为准（交叉验证）
    case("I close_issue 两条路径不一致 ⇒ 认 PR",
         ("import close_issue\n"
          "print('IS_PR' if close_issue.target_is_pr('core','1') else 'IS_ISSUE')\n"),
         {"rules": [AUTH_OK,
                    {"prefix": ["api", "repos/oekit/core/issues/1"], "body": '{"number":1}\n'},
                    {"prefix": ["pr", "view"], "body": '{"number":1}\n'}]},
         "IS_PR")

    # J. target_is_pr：确实是 issue ⇒ False
    case("J close_issue 确实是 issue ⇒ 不是 PR",
         ("import close_issue\n"
          "print('IS_PR' if close_issue.target_is_pr('core','1') else 'IS_ISSUE')\n"),
         {"rules": [AUTH_OK,
                    {"prefix": ["api", "repos/oekit/core/issues/1"], "body": '{"number":1}\n'},
                    {"prefix": ["pr", "view"], "exit": 1, "body": "not a pull request\n"}]},
         "IS_ISSUE")

    # K. current_protection：读不到 ⇒ 中止，不能当成「没有保护」
    case("K pr_flow 读不到分支保护 ⇒ 中止（不当成无保护）",
         ("import pr_flow\n"
          "try:\n"
          "    print('NO_PROTECTION' if pr_flow.current_protection('core') is None else 'HAS_PROTECTION')\n"
          "except SystemExit:\n"
          "    print('ABORTED')\n"),
         {"rules": [AUTH_OK, {"prefix": ["api", "repos/oekit/core/branches/main/protection"],
                              "empty_times": 99}]},
         "ABORTED")

    # L. current_protection：404 ⇒ 确实没有保护
    case("L pr_flow 分支保护 404 ⇒ 判为无保护",
         ("import pr_flow\n"
          "print('NO_PROTECTION' if pr_flow.current_protection('core') is None else 'HAS_PROTECTION')\n"),
         {"rules": [AUTH_OK, {"prefix": ["api", "repos/oekit/core/branches/main/protection"],
                              "exit": 1, "body": "gh: Not Found (HTTP 404)\n"}]},
         "NO_PROTECTION")

    # 额外断言：E/F 两例必须**真的重试过**（否则「拿到真实集合」可能是碰巧）
    e_counts = counts_all.get("E existing_releases 抖动后仍拿到真实 Release 集合", {})
    f_counts = counts_all.get("F existing_releases 确实没有 Release ⇒ 返回空集合", {})
    # E：两条路径都先空 ⇒ 至少要到第 3 轮才拿得到数据（GraphQL 至少读 3 次、REST 至少 2 次）
    e_list = e_counts.get("release\tlist", 0)
    e_api = e_counts.get("api\t--paginate", 0)
    # F：真的没有 ⇒ 空是稳定的 ⇒ 必须把两条路径都读满 EMPTY_ROUNDS 轮才认「事实为空」
    f_list = f_counts.get("release\tlist", 0)
    checks = (
        ("E", e_list, 3, "release list 至少读 3 次"),
        ("E", e_api, 2, "releases API 至少读 2 次（第一轮空时必须换路径再读）"),
        ("F", f_list, 4, f"release list 至少读 {4} 轮（空要连续读到一致才采信）"),
    )
    for label, calls, minimum, desc in checks:
        ok = calls >= minimum
        results.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] 断言 {label}：{desc}（实得 {calls}）")
        if not ok:
            print("         没重试 ⇒ 读到一次空就下结论，正是那个假漂移的成因")

    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
