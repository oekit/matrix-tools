#!/usr/bin/env python3
"""BOM 生成器的自测：Go 地板口径（改造B）+ breaking 索引与不可变对外面（改造A）。

**这个文件防什么**

1. **改造B：嵌套模块的 Go 地板必须取它自己的 go.mod**
   （旧写法一律读 `{tag}:go.mod` ⇒ 读到宿主仓根的 go.mod ⇒ 对外报的地板
   **低报**：实测 bootstrap/obs 真实 `go 1.26.0`，产物写 1.24.0）。

2. **改造A：产物带 breaking 索引，且导出的全局是**不可变**对外面**
   （旧产物把 `Versions`（可变 map）与 `Matrix`（可变切片）导出 ⇒ 消费方
   并发读写实测 `go run -race` DATA RACE，且可能触发不可 recover 的
   `fatal error: concurrent map read and map write`）。

3. **既有口径不许回归**：取不到 go.mod 必须报错（#100）、读不到远端 tag ⇒
   退出码 2（#79）、退出码分类。

4. **proxy 复核的覆盖与口径**（matrix-tools#261，2026-11-08）：--check 的 proxy
   复核读不到时**必须退出码 2**（「未核对」≠「一致」，不许 ⚠ 完照样 exit 0）；
   事实源可被 OEKIT_GO_PROXY / GOPROXY 覆盖、官方源读不到回退 goproxy.cn。

**变异自证（--mutate）**：把实现**改回旧写法**（源码级替换 → 临时目录里
加载变异体）⇒ 对应用例必须**精确转红**。

**无副作用、不联网**：全部在临时目录里造 fixture 仓（`git init` + commit +
tag；「远端」用本地 bare 仓当 origin，fetch 不出网）。Go 编译/race 用例在
`go` 不在 PATH 时**跳过**（不判红，但会说明）。

用法：
  python3 scripts/gen_matrix_go_selftest.py             # 正常模式
  python3 scripts/gen_matrix_go_selftest.py --mutate     # 变异自证
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # matrix.py 在仓根，不在 scripts/ 下

import gen_matrix_go  # noqa: E402

# 用例级结果收集（供 --mutate 比对「精确转红集合」）
_RESULTS: dict[str, bool] = {}
_FAILURES: list[str] = []


def _record(cid: str, good: bool, msg: str = "") -> bool:
    _RESULTS[cid] = good
    if not good and msg:
        _FAILURES.append(msg)
    return good


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{r.stderr.strip()[:200]}")
    return r.stdout.strip()


def make_repo(root: Path, name: str, go_directive: str = "go 1.26.0") -> Path:
    """造一个带 go.mod 的 fixture 仓，打 tag v0.1.0。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "selftest@example.invalid")
    git(d, "config", "user.name", "selftest")
    (d / "go.mod").write_text(
        f"module example.invalid/{name}\n\n{go_directive}\n", encoding="utf-8")
    git(d, "add", "go.mod")
    git(d, "commit", "-q", "-m", "init")
    git(d, "tag", "v0.1.0")
    return d


def make_nested_repo(root: Path, name: str, *, host_go: str, nested_go: str,
                     subpath: str, nested_tag: str) -> Path:
    """造一个**含嵌套模块**的 fixture 仓。

    嵌套模块的 go.mod 放在 `subpath/` 下，**go 指令与宿主根不同**
    （这正是改造B 的牙齿：旧写法读宿主根 ⇒ 拿到 host_go 而不是 nested_go）。

    `nested_tag` 形如 `obs/v0.2.14`（Go 的子目录模块规则：tag 前缀 =
    模块相对仓根的**完整子路径**）。
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "selftest@example.invalid")
    git(d, "config", "user.name", "selftest")
    (d / "go.mod").write_text(
        f"module example.invalid/{name}\n\n{host_go}\n", encoding="utf-8")
    sub = d / subpath
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "go.mod").write_text(
        f"module example.invalid/{name}/{subpath}\n\n{nested_go}\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    git(d, "tag", "v0.1.0")
    git(d, "tag", nested_tag)
    return d


def expect_raises(fn, *args) -> str:
    """调用 fn，断言它抛 SystemExit，并返回错误消息文本。"""
    try:
        fn(*args)
    except SystemExit as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{args} 应当抛 SystemExit，却正常返回了")


# ── 既有口径（不许回归）────────────────────────────────────────────────────
def case_A1_local_tag_present(gmg=gen_matrix_go) -> None:
    """正常路径：本地有该 tag ⇒ 取到真地板。"""
    with tempfile.TemporaryDirectory() as td:
        d = make_repo(Path(td), "normal")
        got = gmg.go_floor(str(d), "v0.1.0")
        _record("A1", got == "1.26.0", f"正常路径地板应为 1.26.0，实得 {got}")


def case_A2_missing_tag_must_fail(gmg=gen_matrix_go) -> None:
    """反证 1：本地缺该 tag ⇒ 必须抛错，且不能吐出一个「看起来合法的地板」。"""
    with tempfile.TemporaryDirectory() as td:
        d = make_repo(Path(td), "missing")
        msg = expect_raises(gmg.go_floor, str(d), "v0.9.9")
        ok = ("fetch" in msg and "#100" in msg
              and "1.24.0" not in msg and "1.26.0" not in msg)
        _record("A2", ok, f"错误消息应给出可执行修复步骤且不含合法地板值，实得：{msg[:200]}")


def case_A3_no_go_directive_must_fail(gmg=gen_matrix_go) -> None:
    """反证 2：该 tag 的 go.mod 里没有 go 指令 ⇒ 同样必须抛错（不能兜底）。"""
    with tempfile.TemporaryDirectory() as td:
        d = make_repo(Path(td), "nogo", go_directive="// no go directive here")
        msg = expect_raises(gmg.go_floor, str(d), "v0.1.0")
        _record("A3", "#100" in msg, f"错误消息应指向本 issue，实得：{msg[:200]}")


def case_A4_ensure_local_tag_fetches_from_origin(gmg=gen_matrix_go) -> None:
    """反证 3：本地缺 tag 但 origin 有 ⇒ ensure_local_tag 补取后能取到真地板。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        origin = root / "origin.git"
        src = make_repo(root, "src")
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        git(src, "remote", "add", "origin", str(origin))
        git(src, "push", "-q", "origin", "main", "--tags")

        clone = root / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        git(clone, "config", "user.email", "selftest@example.invalid")
        git(clone, "config", "user.name", "selftest")
        git(clone, "tag", "-d", "v0.1.0")

        gmg.ensure_local_tag(str(clone), "v0.1.0")
        got = gmg.go_floor(str(clone), "v0.1.0")
        _record("A4", got == "1.26.0", f"补取后应取到真地板 1.26.0，实得 {got}")


def case_A5_ensure_local_tag_noop_when_present(gmg=gen_matrix_go) -> None:
    """本地已有该 tag ⇒ ensure_local_tag 不动网络（稳态零额外开销）。"""
    with tempfile.TemporaryDirectory() as td:
        d = make_repo(Path(td), "noop")
        git(d, "remote", "add", "origin", str(Path(td) / "does-not-exist.git"))
        gmg.ensure_local_tag(str(d), "v0.1.0")
        _record("A5", gmg.go_floor(str(d), "v0.1.0") == "1.26.0")


def case_A6_unreadable_exits_with_code_2(gmg=gen_matrix_go) -> None:
    """反证 4：读不到远端 tag ⇒ **退出码 2 + 干净消息**，不是裸 traceback。"""
    orig = gmg.prefetch_remote_tags

    def boom(*_args, **_kwargs):
        raise gmg.RemoteReadError(
            "registry: 远端 tag 读不到（已试 https://github.com/oekit/registry.git）"
            "——读不到 ≠ 没有，不能用本地 tag 顶替（matrix-tools#79）")

    gmg.prefetch_remote_tags = boom
    old_argv = sys.argv
    sys.argv = ["gen_matrix_go.py", "--check"]
    try:
        rc = gmg.main()
    finally:
        gmg.prefetch_remote_tags = orig
        sys.argv = old_argv
    _record("A6", rc == gmg.EXIT_UNREADABLE == 2,
            f"读不到应退出码 2（EXIT_UNREADABLE），实得 {rc}")


def case_A7_classify_check_kinds(gmg=gen_matrix_go) -> None:
    """退出码分类：0=一致 / 1=真漂移 / 2=未核对（读不到）。"""
    kind, _ = gmg.classify_check(gmg.EXIT_OK, "✓ 一致")
    ok1 = kind == "ok"
    kind, msg = gmg.classify_check(gmg.EXIT_STALE, "❌ oekit BOM 陈旧：go.mod")
    ok2 = kind == "stale" and "BOM 陈旧" in msg
    kind, msg = gmg.classify_check(gmg.EXIT_UNREADABLE, "读不到远端 tag ⇒ 无法核对 BOM 新鲜度")
    ok3 = kind == "unreadable" and "未核对" in msg and "判不了" in msg
    _record("A7", ok1 and ok2 and ok3, f"退出码分类不对：{ok1}/{ok2}/{ok3}")


# ── 改造B：嵌套模块的 Go 地板 ──────────────────────────────────────────────
def case_B1_nested_floor_is_its_own(gmg=gen_matrix_go) -> None:
    """**改造B 牙齿**：嵌套模块的地板取**它自己的** go.mod（不是宿主根）。

    夹具：宿主根 `go 1.24.0`、嵌套 `obs/go.mod` 是 `go 1.26.0` ⇒
    旧写法（读 `{tag}:go.mod`）会返回 1.24.0 ⇒ 必须转红。
    """
    with tempfile.TemporaryDirectory() as td:
        d = make_nested_repo(Path(td), "boot", host_go="go 1.24.0",
                             nested_go="go 1.26.0", subpath="obs",
                             nested_tag="obs/v0.2.14")
        host = gmg.go_floor(str(d), "v0.1.0")
        nested = gmg.go_floor(str(d), "obs/v0.2.14")
        _record("B1", host == "1.24.0" and nested == "1.26.0",
                f"嵌套地板应取嵌套自己的 1.26.0（宿主是 {host}），实得 {nested}")


def case_B2_deep_nested_floor_is_its_own(gmg=gen_matrix_go) -> None:
    """**改造B 牙齿（多层）**：深度 4 的嵌套模块也取自己的 go.mod。

    真实形态：`observability/tracing/instrumentation/kitexotel`（tag 前缀 =
    相对仓根的**完整子路径**）。
    """
    deep = "observability/tracing/instrumentation/kitexotel"
    with tempfile.TemporaryDirectory() as td:
        d = make_nested_repo(Path(td), "ktx", host_go="go 1.24.0",
                             nested_go="go 1.26.0", subpath=deep,
                             nested_tag=f"{deep}/v0.7.7")
        nested = gmg.go_floor(str(d), f"{deep}/v0.7.7")
        _record("B2", nested == "1.26.0",
                f"深度 4 嵌套模块的地板应取自己的 1.26.0，实得 {nested}")


def case_B3_proxy_detects_floor_mismatch(gmg=gen_matrix_go) -> None:
    """**改造B 复核**：--check 用 proxy 的 .mod 复核 Go 字段，不符 ⇒ 判红。"""
    mods = [{"path": "github.com/oekit/pkg", "version": "v0.1.0", "go": "1.24.0"}]
    orig = gmg.proxy_go_directive
    gmg.proxy_go_directive = lambda p, v, timeout=20.0: "1.26.0"
    try:
        drift, unchecked = gmg.check_go_against_proxy(mods)
    finally:
        gmg.proxy_go_directive = orig
    ok = bool(drift) and "1.26.0" in drift[0] and not unchecked
    _record("B3", ok, f"proxy .mod 与产物不符时必须判红，实得 drift={drift} unchecked={unchecked}")


def case_B4_proxy_unreadable_is_unchecked_not_drift(gmg=gen_matrix_go) -> None:
    """proxy 读不到 ⇒ 记「未核对」，**不**当漂移（网络抖动不是矩阵漂移）。"""
    mods = [{"path": "github.com/oekit/pkg", "version": "v0.1.0", "go": "1.24.0"}]
    orig = gmg.proxy_go_directive
    gmg.proxy_go_directive = lambda p, v, timeout=20.0: None
    try:
        drift, unchecked = gmg.check_go_against_proxy(mods)
    finally:
        gmg.proxy_go_directive = orig
    ok = not drift and unchecked == ["github.com/oekit/pkg@v0.1.0"]
    _record("B4", ok, f"读不到应记未核对而非漂移，实得 drift={drift} unchecked={unchecked}")


def case_B5_proxy_unchecked_exits_2(gmg=gen_matrix_go) -> None:
    """matrix-tools#261：proxy 复核没跑完 ⇒ main() --check 必须退出码 2。

    旧写法 unchecked 只 ⚠ 一句然后照样「✓ 一致」+ exit 0 ⇒ proxy 全读不到时
    这道复核静默零覆盖，audit 还按退出码判「BOM 新鲜度 ✓」（假绿）。
    夹具：collect 注入夹具模块、产物落进临时 OEKIT_DIR（changed 空 ⇒ 不踩
    「BOM 陈旧」分支）、proxy 全读不到 ⇒ 唯一出路必须是 EXIT_UNREADABLE。
    """
    with tempfile.TemporaryDirectory() as td:
        bom = Path(td) / "oekit"
        bom.mkdir()
        files = gmg.render(_fixture_modules(), "v0.2.9", "2026-11-08T00:00:00Z")
        files["matrix.go"] = gmg.gofmt_text(files["matrix.go"])
        for name, content in files.items():
            (bom / name).write_text(content, encoding="utf-8")
        saved = (gmg.OEKIT_DIR, gmg.collect, gmg.proxy_go_directive, sys.argv)
        buf = io.StringIO()
        try:
            gmg.OEKIT_DIR = str(bom)
            gmg.collect = lambda: _fixture_modules()
            gmg.proxy_go_directive = lambda p, v, timeout=20.0: None
            sys.argv = ["gen_matrix_go.py", "--check"]
            with contextlib.redirect_stdout(buf):
                rc = gmg.main()
        finally:
            (gmg.OEKIT_DIR, gmg.collect, gmg.proxy_go_directive, sys.argv) = saved
        out = buf.getvalue()
        ok = rc == gmg.EXIT_UNREADABLE == 2 and "未核对" in out \
            and "✓ oekit BOM 与远端 tag 一致" not in out
        _record("B5", ok,
                f"proxy 全读不到 ⇒ 必须退出码 2 且不印「一致」（#261）："
                f"rc={rc} out={out[:240]}")


def case_B6_proxy_bases_env_override(gmg=gen_matrix_go) -> None:
    """matrix-tools#261：复核事实源可覆盖——OEKIT_GO_PROXY > GOPROXY 首段 > 默认双源。"""
    saved = {k: os.environ.get(k) for k in ("OEKIT_GO_PROXY", "GOPROXY")}
    try:
        os.environ["OEKIT_GO_PROXY"] = "http://127.0.0.1:9/mirror"
        os.environ["GOPROXY"] = "https://goproxy.cn,direct"
        bases1 = gmg.proxy_bases()
        ok1 = bool(bases1) and bases1[0] == "http://127.0.0.1:9/mirror" \
            and "https://goproxy.cn" in bases1
        os.environ.pop("OEKIT_GO_PROXY")
        bases2 = gmg.proxy_bases()
        ok2 = bool(bases2) and bases2[0] == "https://goproxy.cn"
        os.environ.pop("GOPROXY")
        bases3 = gmg.proxy_bases()
        ok3 = bases3 == ["https://proxy.golang.org", "https://goproxy.cn"]
        _record("B6", ok1 and ok2 and ok3,
                f"复核事实源的覆盖/顺序不对：{bases1}/{bases2}/{bases3}")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── 改造A：breaking 索引 + 不可变对外面 ───────────────────────────────────
def _fixture_modules() -> list[dict]:
    """两个模块的夹具（宿主 + 嵌套），各带自己的 breaking 条目。"""
    return [
        {"repo": "boot", "path": "github.com/oekit/boot", "version": "v0.6.8",
         "go": "1.24.0", "nested": False,
         "breaking": [{"version": "v0.5.0", "line": "宿主根模块的破坏性变更"}]},
        {"repo": "boot", "path": "github.com/oekit/boot/obs", "version": "v0.2.14",
         "go": "1.26.0", "nested": True,
         "breaking": [{"version": "obs/v0.2.0", "line": "嵌套模块自己的破坏性变更"}]},
    ]


def case_C1_breaking_belongs_to_its_own_module(gmg=gen_matrix_go) -> None:
    """**改造A**：嵌套模块的 breaking **只归自己**，不折叠进宿主。

    旧写法（BOM 里根本没有 breaking / 或全折叠进宿主）⇒ 必须转红。
    """
    by_module = {
        "": [{"version": "v0.5.0", "line": "宿主根模块的破坏性变更"}],
        "obs": [{"version": "obs/v0.2.0", "line": "嵌套模块自己的破坏性变更"}],
    }
    root_items = gmg.breaking_for(by_module, "", "v0.6.8")
    nested_items = gmg.breaking_for(by_module, "obs", "v0.2.14")
    ok_attr = ([i["line"] for i in root_items] == ["宿主根模块的破坏性变更"]
               and [i["line"] for i in nested_items] == ["嵌套模块自己的破坏性变更"])
    _record("C1", ok_attr,
            f"归属必须按模块拆开（宿主不得含嵌套条目），实得 root="
            f"{[i['line'] for i in root_items]} nested={[i['line'] for i in nested_items]}")
    # 端到端：渲染出来的产物里，嵌套模块带自己的条目、宿主行里没有
    # （条目来源走 breaking_for ⇒ 归属逻辑的变异会一路传到产物）
    mods = _fixture_modules()
    for m, sub in zip(mods, ("", "obs")):
        m["breaking"] = gmg.breaking_for(by_module, sub, m["version"])
    src = gmg.render(mods, "v0.2.0", "2026-01-01T00:00:00Z")["matrix.go"]
    host_lines = [ln for ln in src.splitlines() if 'Path: "github.com/oekit/boot"' in ln]
    ok_render = ('Line: "嵌套模块自己的破坏性变更"' in src
                 and bool(host_lines)
                 and "嵌套模块自己的破坏性变更" not in host_lines[0])
    _record("C1_render", ok_render,
            "渲染产物里嵌套条目必须挂在嵌套模块上、不得出现在宿主行里")


def case_C2_exported_surface_is_immutable(gmg=gen_matrix_go) -> None:
    """**改造A**：导出面必须是**不可变**的（返回副本），不许再导出可变全局。

    旧的 `var Versions = map[string]string{}` / `var Matrix = []Module{}`
    可被消费方并发改写 ⇒ go run -race 实测 DATA RACE ⇒ 必须转红。
    """
    src = gmg.render(_fixture_modules(), "v0.2.0", "2026-01-01T00:00:00Z")["matrix.go"]
    forbidden = ["var Versions = map[string]string{", "var Matrix = []Module{"]
    required = ["func Versions() map[string]string",
                "func Matrix() []Module",
                "func VersionOf(module string) string",
                "var versionsSnapshot",
                "var matrixSnapshot"]
    ok = all(f not in src for f in forbidden) and all(r in src for r in required)
    _record("C2", ok,
            "导出面必须降级为不可变（无导出的可变 map/切片，且保留 VersionOf）")


def case_C3_breaking_api_present(gmg=gen_matrix_go) -> None:
    """**改造A**：产物必须有 breaking 查询 API，且**零 import** 不变。"""
    src = gmg.render(_fixture_modules(), "v0.2.0", "2026-01-01T00:00:00Z")["matrix.go"]
    required = ["type Breaking struct",
                "func BreakingOf(module string) []Breaking",
                "func HasBreaking(module string) bool",
                "func BreakingModules() []string"]
    ok = all(r in src for r in required)
    _record("C3", ok, f"缺 breaking 查询 API：{[r for r in required if r not in src]}")
    _record("C3_zeroimport", "import (" not in src,
            "matrix.go 必须零 import（机读快照，不许引入依赖）")


CONSUMER_MAIN = textwrap.dedent("""
    package main

    import (
        "fmt"
        "sync"

        bom "example.invalid/bom"
    )

    func main() {
        var wg sync.WaitGroup
        for i := 0; i < 8; i++ {
            wg.Add(1)
            go func() {
                defer wg.Done()
                for j := 0; j < 200; j++ {
                    _ = bom.VersionOf("github.com/oekit/boot")
                    _ = bom.Matrix()
                    _ = bom.Versions()
                    _ = bom.BreakingOf("github.com/oekit/boot/obs")
                    _ = bom.HasBreaking("github.com/oekit/boot")
                    _ = bom.BreakingModules()
                }
            }()
        }
        wg.Wait()
        // 改副本：不得影响包内状态
        m := bom.Matrix()
        m[0].Version = "MUTATED"
        m[0].Breaking[0].Line = "MUTATED"
        v := bom.Versions()
        v["github.com/oekit/boot"] = "MUTATED"
        fmt.Println("state:", bom.VersionOf("github.com/oekit/boot"),
            bom.Matrix()[0].Version, bom.Matrix()[0].Breaking[0].Line)
    }
""")


def case_C4_generated_go_compiles_and_is_race_free(gmg=gen_matrix_go) -> None:
    """**改造A 端到端**：生成的 Go 能编译、并发读 race 干净、改副本不影响包内。

    没有 go 工具链 ⇒ **跳过**（不判红，但要说明）。
    """
    if shutil.which("go") is None:
        _record("C4", True, "")
        print("  ⚠ 跳过 C4（PATH 里没有 go 工具链）")
        return
    files = gmg.render(_fixture_modules(), "v0.2.0", "2026-01-01T00:00:00Z")
    files["matrix.go"] = gmg.gofmt_text(files["matrix.go"])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pkg = root / "bom"
        pkg.mkdir()
        (pkg / "go.mod").write_text("module example.invalid/bom\n\ngo 1.24\n",
                                    encoding="utf-8")
        (pkg / "matrix.go").write_text(files["matrix.go"], encoding="utf-8")
        main = root / "main"
        main.mkdir()
        (main / "go.mod").write_text(
            "module example.invalid/main\n\ngo 1.24\n\n"
            "require example.invalid/bom v0.0.0\n\n"
            "replace example.invalid/bom => ../bom\n", encoding="utf-8")
        (main / "main.go").write_text(CONSUMER_MAIN, encoding="utf-8")
        env = dict(**__import__("os").environ)
        env["GOFLAGS"] = "-mod=mod"
        env["GOPROXY"] = "off"
        r = subprocess.run(["go", "run", "-race", "."], cwd=main,
                           capture_output=True, text=True, env=env)
        ok = r.returncode == 0 and "MUTATED" not in r.stdout
        _record("C4", ok,
                f"生成的 Go 应能编译且并发读 race 干净、改副本不影响包内；"
                f"rc={r.returncode} out={r.stdout.strip()[:200]} err={r.stderr.strip()[:400]}")


def case_C5_old_api_shape_no_longer_compiles(gmg=gen_matrix_go) -> None:
    """**破坏点属实**：旧写法 `oekit.Versions["x"]` **不再能编译**。

    这是本轮 v0.2.0 的 breaking 点（导出的可变全局降级为不可变对外面）⇒
    必须真的编译不过，才算「破坏属实」而不是「文档说说而已」。
    """
    if shutil.which("go") is None:
        _record("C5", True, "")
        print("  ⚠ 跳过 C5（PATH 里没有 go 工具链）")
        return
    files = gmg.render(_fixture_modules(), "v0.2.0", "2026-01-01T00:00:00Z")
    files["matrix.go"] = gmg.gofmt_text(files["matrix.go"])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pkg = root / "bom"
        pkg.mkdir()
        (pkg / "go.mod").write_text("module example.invalid/bom\n\ngo 1.24\n",
                                    encoding="utf-8")
        (pkg / "matrix.go").write_text(files["matrix.go"], encoding="utf-8")
        main = root / "main"
        main.mkdir()
        (main / "go.mod").write_text(
            "module example.invalid/main\n\ngo 1.24\n\n"
            "require example.invalid/bom v0.0.0\n\n"
            "replace example.invalid/bom => ../bom\n", encoding="utf-8")
        (main / "main.go").write_text(
            'package main\n\nimport bom "example.invalid/bom"\n\n'
            'func main() { _ = bom.Versions["github.com/oekit/boot"] }\n',
            encoding="utf-8")
        env = dict(**__import__("os").environ)
        env["GOFLAGS"] = "-mod=mod"
        env["GOPROXY"] = "off"
        r = subprocess.run(["go", "build", "./..."], cwd=main,
                           capture_output=True, text=True, env=env)
        _record("C5", r.returncode != 0,
                f"旧写法 oekit.Versions[...] 必须编译不过（破坏属实），实得 rc={r.returncode}")


# ── 用例表 ────────────────────────────────────────────────────────────────
CASES = [
    ("A1 正常路径：本地有 tag ⇒ 取到真地板", case_A1_local_tag_present, ["A1"]),
    ("A2 反证1：本地缺 tag ⇒ 必须抛错（不得兜底）", case_A2_missing_tag_must_fail, ["A2"]),
    ("A3 反证2：go.mod 无 go 指令 ⇒ 必须抛错", case_A3_no_go_directive_must_fail, ["A3"]),
    ("A4 反证3：ensure_local_tag 能从 origin 补取", case_A4_ensure_local_tag_fetches_from_origin, ["A4"]),
    ("A5 稳态：tag 已在本地 ⇒ 不做额外 fetch", case_A5_ensure_local_tag_noop_when_present, ["A5"]),
    ("A6 反证4：读不到远端 tag ⇒ 退出码 2 + 干净消息", case_A6_unreadable_exits_with_code_2, ["A6"]),
    ("A7 反证5：退出码分类（0/1/2 ⇒ 一致/漂移/未核对）", case_A7_classify_check_kinds, ["A7"]),
    ("B1 改造B：嵌套模块的 Go 地板取自己的 go.mod", case_B1_nested_floor_is_its_own, ["B1"]),
    ("B2 改造B：深度 4 的嵌套模块也取自己的 go.mod", case_B2_deep_nested_floor_is_its_own, ["B2"]),
    ("B3 改造B：--check 用 proxy 的 .mod 复核 Go 字段（不符 ⇒ 判红）",
     case_B3_proxy_detects_floor_mismatch, ["B3"]),
    ("B4 改造B：proxy 读不到 ⇒ 记「未核对」而不是判红",
     case_B4_proxy_unreadable_is_unchecked_not_drift, ["B4"]),
    ("B5 #261：proxy 复核没跑完 ⇒ main() --check 退出码 2（不是「✓ 一致」+ 0）",
     case_B5_proxy_unchecked_exits_2, ["B5"]),
    ("B6 #261：复核事实源可覆盖（OEKIT_GO_PROXY/GOPROXY）+ 默认双源回退",
     case_B6_proxy_bases_env_override, ["B6"]),
    ("C1 改造A：嵌套模块的 breaking 只归自己（不折叠进宿主）",
     case_C1_breaking_belongs_to_its_own_module, ["C1", "C1_render"]),
    ("C2 改造A：导出面是不可变的（无导出的可变 map/切片）",
     case_C2_exported_surface_is_immutable, ["C2"]),
    ("C3 改造A：breaking 查询 API 在位 + 零 import 不变",
     case_C3_breaking_api_present, ["C3", "C3_zeroimport"]),
    ("C4 改造A：生成的 Go 能编译 + 并发读 race 干净 + 改副本不影响包内",
     case_C4_generated_go_compiles_and_is_race_free, ["C4"]),
    ("C5 破坏点属实：旧写法 oekit.Versions[...] 编译不过",
     case_C5_old_api_shape_no_longer_compiles, ["C5"]),
]


def run_suite(gmg=gen_matrix_go) -> None:
    for _title, fn, ids in CASES:
        try:
            fn(gmg)
        except Exception as e:  # noqa: BLE001 — 变异体把实现改崩也算「没抓住」
            for cid in ids:
                _record(cid, False, f"用例执行抛异常（变异体可能把实现改崩了）：{e!r}")


# ── 变异体（--mutate）：把实现改回旧写法 ───────────────────────────────────
def _load_mutated(name: str, replacements: list[tuple[str, str]]):
    """把 gen_matrix_go.py 按 replacements 改回旧写法 ⇒ 临时目录里加载变异体。"""
    src = (HERE / "gen_matrix_go.py").read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in src:
            raise AssertionError(f"变异锚点不存在（源码已变？）：{old[:80]!r}")
        src = src.replace(old, new, 1)
    tmp = Path(tempfile.mkdtemp())
    p = tmp / f"{name}.py"
    p.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mutate_go_floor_old_shape():
    """改造B 退回旧写法：go_floor 一律读 `{tag}:go.mod`（嵌套读到宿主根）。"""
    return _load_mutated("gmg_mut_go_floor", [(
        'r = subprocess.run(["git", "-C", repo_dir_path, "show", f"{tag}:{rel}"],',
        'r = subprocess.run(["git", "-C", repo_dir_path, "show", f"{tag}:go.mod"],',
    )])


def _mutate_proxy_no_compare():
    """改造B 复核退化成「只看读到没有」：不再比对地板 ⇒ 不符也判绿。"""
    return _load_mutated("gmg_mut_proxy", [(
        """        if got != m["go"]:
            drift.append(f"{m['path']}@{m['version']}：产物写 Go {m['go']}，"
                         f"proxy 上的 .mod 是 {got}")""",
        """        if False:  # 旧写法：只确认读到了，不比对
            drift.append(f"{m['path']}@{m['version']}")""",
    )])


def _mutate_proxy_unchecked_still_green():
    """matrix-tools#261 退回旧写法：未核对只 ⚠ 不改退出码 ⇒ 照样「✓ 一致」+ exit 0。"""
    return _load_mutated("gmg_mut_unchecked_green", [(
        """            print("   先修网络/凭证，或用 OEKIT_GO_PROXY 指定可达的镜像后重跑。")
            return EXIT_UNREADABLE""",
        """            print("   先修网络/凭证，或用 OEKIT_GO_PROXY 指定可达的镜像后重跑。")""",
    )])


def _mutate_breaking_folded():
    """改造A 退回旧写法：嵌套模块的 breaking 折叠进宿主（都取根模块的）。"""
    return _load_mutated("gmg_mut_folded", [(
        'return _breaking_upto(list(by_module.get(subpath, [])), pinned_version)',
        'return _breaking_upto(list(by_module.get("", [])), pinned_version)',
    )])


def _mutate_exported_mutable_globals():
    """改造A 退回旧写法：把包内私有的快照改名成**导出的可变全局**。"""
    return _load_mutated("gmg_mut_exported", [
        ("var matrixSnapshot = []Module{", "var Matrix = []Module{"),
        ("var versionsSnapshot = map[string]string{", "var Versions = map[string]string{"),
    ])


MUTATIONS = [
    ("改造B 退回旧写法：go_floor 一律读 {tag}:go.mod",
     _mutate_go_floor_old_shape, {"B1", "B2"}),
    ("改造B 复核退化成「只看读到没有」（不再比对地板）",
     _mutate_proxy_no_compare, {"B3"}),
    ("#261 退回旧写法：proxy 未核对只 ⚠ 不改退出码（照样 ✓ + exit 0）",
     _mutate_proxy_unchecked_still_green, {"B5"}),
    ("改造A 退回旧写法：嵌套模块的 breaking 折叠进宿主",
     _mutate_breaking_folded, {"C1", "C1_render"}),
    ("改造A 退回旧写法：导出可变全局（var Versions / var Matrix）",
     _mutate_exported_mutable_globals, {"C2", "C4"}),
]


def main() -> int:
    if "--mutate" in sys.argv:
        ok = True
        for name, mutate, expect in MUTATIONS:
            _RESULTS.clear()
            _FAILURES.clear()
            mod = mutate()
            try:
                run_suite(mod)
            finally:
                sys.modules.pop(getattr(mod, "__name__", ""), None)
            got = {cid for cid, good in _RESULTS.items() if not good}
            if got == expect:
                print(f"✓ 变异「{name}」被抓住（精确转红 {sorted(got)}）")
            else:
                ok = False
                print(f"✗ 变异「{name}」转红集合**不精确** ⇒ "
                      f"期望 {sorted(expect)}，实际 {sorted(got)}")
                for f_ in _FAILURES:
                    print("      -", f_)
        if ok:
            print(f"✓ 变异自证 {len(MUTATIONS)}/{len(MUTATIONS)} 全部**精确**转红")
            return 0
        print(f"✗ 变异自证未过：{len(MUTATIONS)} 个变异体里至少一个转红集合不精确")
        return 1

    _RESULTS.clear()
    _FAILURES.clear()
    run_suite(gen_matrix_go)
    failed = [cid for cid, good in _RESULTS.items() if not good]
    for cid, good in _RESULTS.items():
        print(f"  {'✓' if good else '✗'} {cid}")
    if failed:
        print(f"\n❌ selftest 失败 {len(failed)}/{len(_RESULTS)}")
        for f_ in _FAILURES:
            print("  -", f_)
        return 1
    print(f"\n✓ gen_matrix_go selftest 全绿（{len(_RESULTS)} 个断言面 / "
          f"{len(CASES)} 个用例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
