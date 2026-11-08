#!/usr/bin/env python3
"""install_lines.py + refresh_versions 嵌套安装行的自测（matrix-tools#250）。

**这条判据防的是什么**：12 个嵌套发版单元此前不在「版本行」纪律内——
refresh_versions 的正则按「仓库名 + @」定位，对嵌套安装行
（`go get github.com/oekit/logx/sink@v0.1.0`）永不匹配；audit 同款正则也不查
⇒ 实测 7/7 嵌套安装行陈旧（最坏落后 15 个 patch），且没有任何门禁会喊。

断言（每条都要能判红）：
  1. sync_repo（写模式）把父仓 README 里**每个**嵌套安装行刷到各自的最新
     tag——同仓多单元**同版本号**也不撞键（sink 与 hlog 各刷各的）；
  2. install_lines.check_readme_install_lines：
     a. 落后的嵌套安装行 ⇒ 报「≠ 最新」；
     b. 新鲜的 ⇒ 零漂移；
     c. **反向判据**：登记外路径（不是本仓根模块、也不是本仓嵌套单元）的
        带版本安装行 ⇒ 报「登记外路径」（漏登记探测器）；
     d. `@latest`（无版本号）行不进判据范围；
  3. audit.py 已接线（源码契约：嵌套安装行判据真的在逐仓循环里被调）。

**变异自证（#250 验收 4）**：把 `latest_nested_tag` 换成 `latest_tag`
（「键换形态」——嵌套单元用根模块的版本号起跳）⇒ 用例 1 必须**精确转红**
⇒ 证明「按嵌套 tag 刷」这个判据本身有牙齿，不是碰巧绿。

**无副作用、不联网**：夹具仓 + 假 remote_tags（内存表），不建 git、不碰网络。

用法：python3 scripts/install_lines_selftest.py [--mutate]
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import matrix  # noqa: E402
import refresh_versions  # noqa: E402
from install_lines import check_readme_install_lines, nested_units_of  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


# 假远端 tag 表（按仓）：sink 与 hlog **版本号不同** ⇒ 键撞了立刻现形
FAKE_TAGS = {
    "logx": ["sink/v0.1.5", "hlog/v0.2.3", "kitexlog/v0.1.1", "hertzlog/v0.1.1",
             "v0.8.2", "v0.8.1"],
    "registry": ["nacos/v0.1.9", "nacosimpl/v0.1.9", "v0.5.5"],
}

README_TEMPLATE = """# logx

```
go get github.com/oekit/logx@v0.8.2
go get github.com/oekit/logx/sink@{sink}
go get github.com/oekit/logx/hlog@{hlog}
```

| 模块 | 安装 |
|---|---|
| kitexlog | `go get github.com/oekit/logx/kitexlog@{kitexlog}` |
| hertzlog | `go get github.com/oekit/logx/hertzlog@latest` |
| 登记外 | `go get github.com/oekit/kitex/cmd/thrift2openapi@v0.1.0` |
"""

NESTED_VERSIONS = {"sink": "v0.1.0", "hlog": "v0.1.0", "kitexlog": "v0.1.0"}


def build_fixture(base: Path) -> Path:
    """父仓 logx：README（嵌套安装行全陈旧）+ 嵌套 go.mod（文件系统事实）。"""
    d = base / "logx"
    d.mkdir(parents=True)
    (d / "README.md").write_text(
        README_TEMPLATE.format(**NESTED_VERSIONS), encoding="utf-8")
    for sub in ("sink", "hlog", "kitexlog", "hertzlog"):
        (d / sub).mkdir()
        (d / sub / "go.mod").write_text(
            f"module github.com/oekit/logx/{sub}\n", encoding="utf-8")
    return d


def install_patches() -> None:
    """把远端 tag 读取换成本内存表（latest_tag(remote=True) 与
    latest_nested_tag 都经 matrix.remote_tags ⇒ 一个注入点全覆盖）。"""
    matrix.remote_tags = lambda repo, base=None: list(FAKE_TAGS.get(repo, []))


def case_refresh_nested_lines() -> None:
    """用例 1：sync_repo 把嵌套安装行刷到**各自**最新 tag（同号不撞键）。"""
    install_patches()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        d = build_fixture(base)
        changed = refresh_versions.sync_repo("logx", str(base), write=True)
        check("README.md" in changed, f"用例1 README 未被刷新：changed={changed}")
        after = (d / "README.md").read_text(encoding="utf-8")
        check("go get github.com/oekit/logx/sink@v0.1.5" in after,
              f"用例1 sink 没刷到 v0.1.5：\n{after}")
        check("go get github.com/oekit/logx/hlog@v0.2.3" in after,
              f"用例1 hlog 没刷到 v0.2.3（键撞了/被 sink 覆盖？）：\n{after}")
        check("go get github.com/oekit/logx/kitexlog@v0.1.1" in after,
              f"用例1 kitexlog 没刷到 v0.1.1：\n{after}")
        check("go get github.com/oekit/logx@v0.8.2" in after,
              f"用例1 根安装行不该被动（已是最新）：\n{after}")
        check("go get github.com/oekit/logx/hertzlog@latest" in after,
              f"用例1 无版本号（@latest）行不该被动：\n{after}")
        # 第二遍跑 ⇒ 幂等（无变更）
        changed2 = refresh_versions.sync_repo("logx", str(base), write=True)
        check(changed2 == [], f"用例1 第二遍跑仍有变更（不幂等）：{changed2}")


def case_check_readme() -> None:
    """用例 2：判据层的正反向。"""
    units = {"logx/sink", "logx/hlog", "logx/kitexlog", "logx/hertzlog"}
    latest = {"logx/sink": "v0.1.5", "logx/hlog": "v0.2.3"}

    def latest_of(path):
        return latest.get(path)

    # a. 落后的嵌套安装行 ⇒ 报「≠ 最新」
    stale = "go get github.com/oekit/logx/sink@v0.1.0\ngo get github.com/oekit/logx@v0.8.2\n"
    problems = check_readme_install_lines(stale, "logx", units, latest_of)
    check(any("sink" in p and "v0.1.5" in p for p in problems),
          f"用例2a 落后的嵌套安装行没报「≠ 最新」：{problems}")
    # b. 新鲜的 ⇒ 零漂移
    fresh = "go get github.com/oekit/logx/sink@v0.1.5\ngo get github.com/oekit/logx/hlog@v0.2.3\n"
    check(check_readme_install_lines(fresh, "logx", units, latest_of) == [],
          "用例2b 新鲜的安装行不该报漂移")
    # c. 登记外路径 ⇒ 反向判据
    alien = "go get github.com/oekit/kitex/cmd/thrift2openapi@v0.1.0\n"
    problems = check_readme_install_lines(alien, "logx", units, latest_of)
    check(any("登记外路径" in p and "thrift2openapi" in p for p in problems),
          f"用例2c 登记外路径没被反向判据抓到：{problems}")
    # d. @latest 不进判据
    lat = "go get github.com/oekit/logx/hertzlog@latest\n"
    check(check_readme_install_lines(lat, "logx", units, latest_of) == [],
          "用例2d 无版本号行不该进判据")


def case_nested_units_of() -> None:
    """nested_units_of 从仓内嵌套 go.mod 推导（文件系统事实，不依赖登记表）。"""
    with tempfile.TemporaryDirectory() as td:
        d = build_fixture(Path(td))
        units = nested_units_of(d)
        for want in ("logx/sink", "logx/hlog", "logx/kitexlog", "logx/hertzlog"):
            check(want in units, f"nested_units_of 缺 {want}：{sorted(units)}")
        check("logx" not in units, "nested_units_of 不该含根模块")
        # ★ 值必须是**相对仓根**的 tag 前缀（`sink`，不是 `logx/sink`）：
        #   Go 的子目录模块规则，tag 形如 `sink/v0.1.6`——拿带仓名的模块路径当
        #   过滤前缀会一个 tag 都匹配不到 ⇒ 正向判据静默失效（2026-11-08 实测
        #   抓到的真实缺陷：audit 接线后全部嵌套 tag 读不到 ⇒ 判据空转）。
        for path, prefix in units.items():
            want = path.split("/", 1)[1]
            check(prefix == want,
                  f"nested_units_of 的 tag 前缀应相对仓根：{path} → {prefix!r}（应为 {want!r}）")


def case_audit_wired() -> None:
    """源码契约：audit.py 的逐仓循环里真的调了判据（接线断了 ⇒ 判据成死码）。"""
    src = (ROOT / "scripts" / "audit.py").read_text(encoding="utf-8")
    check("check_readme_install_lines" in src,
          "audit.py 未调用 check_readme_install_lines（嵌套安装行门禁没接线）")
    m = re.search(r"for _i, repo in enumerate\(MODULES, 1\):.*?门面壳同步", src, re.S)
    check(m is not None and "check_readme_install_lines" in m.group(0),
          "audit.py 的嵌套安装行判据不在阶段 2 的逐仓循环里")


def main() -> None:
    mutated = "--mutate" in sys.argv
    if mutated:
        # ★ 变异（#250 验收 4「键换形态」）：把 latest_nested_tag 换成
        #   latest_tag（嵌套单元用**根模块**的版本号起跳）⇒ 用例 1 必须转红。
        #   ⚠️ 变异要打在**调用方**的名字上（refresh_versions 是 `from matrix
        #   import` 按值导入 ⇒ 只改 matrix.latest_nested_tag 不影响它）。
        refresh_versions.latest_nested_tag = lambda module_path, base=None: \
            matrix.latest_tag(module_path.split("/")[2], base, remote=True)
        print("== 变异模式：latest_nested_tag 换成根模块 latest_tag ==")
    case_refresh_nested_lines()
    case_check_readme()
    case_nested_units_of()
    if not mutated:
        case_audit_wired()
    if mutated:
        if FAILURES:
            print(f"✓ 变异被抓住（{len(FAILURES)} 条断言转红）")
            return
        print("✗ 变异没有让任何断言失败 ⇒ 判据没牙齿")
        raise SystemExit(1)
    if FAILURES:
        print("✗ 自测失败：")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("✓ 嵌套安装行自测通过：sync_repo 按嵌套 tag 刷各行（同号不撞键）、"
          "判据层正反向都有牙齿、audit 已接线")


if __name__ == "__main__":
    main()
