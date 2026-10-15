#!/usr/bin/env python3
"""oekit 模块矩阵——唯一事实源与共享工具。

仓库名解析支持三种本地布局（按序探测）：
  <base>/<name>          # 规范布局（Actions / 新检出）
  <base>/ekit-<name>     # 历史布局（本会话遗留的本地检出）
  <base>/repos/<name>    # Actions 批量克隆布局
"""
from __future__ import annotations
import os
import re
import subprocess
from pathlib import Path

# 规范模块清单（23 个，依赖自底向上的顺序）
MODULES = [
    "pkg", "concurrency", "logx", "core", "istio",
    "metrics", "tracing", "registry",
    "data", "gorm", "redis", "httpx", "network", "timertask", "middleware",
    "nns", "nns-endpoint",
    "bootstrap", "handler", "hertz", "kitex", "workflow", "kitex-examples",
]

ORG = "oekit"
SITE_REPO = "oekit.github.io"
STALE_RE = re.compile(r"yi-nology|openekit|github\.com/oekit/ekit-")

DESC = {
    "pkg": "微工具集 16 合 1（errors/validator/stringx/slicex/enumx/convert/encoding/jsonrepair/httpjson/mathx/pathx/pagination/ptrx/timex/uidgen/cache）",
    "concurrency": "并发与流控原语（async/group/singleflight/progress/syncx/ratelimit/retry）",
    "logx": "统一日志门面（多后端可替换 + Sink + caller 归属）+ 优雅关停",
    "core": "应用基建件（governance 治理 schema / lifecycle 契约 / network IP 工具 / version）",
    "istio": "Istio Proxyless——Kitex xDS 动态熔断",
    "metrics": "Prometheus 门面 + 弹性指标族",
    "tracing": "OpenTelemetry 追踪 + trace_id 真相源",
    "registry": "注册发现核心 + Nacos 实现 + 配置加载 + sdbridge",
    "timertask": "分片定时任务（FNV-1a、心跳续约）",
    "data": "缓存检查器（TTL/failTTL/inflight 合并）+ LocalCache",
    "gorm": "SQL/GORM 全家——连接池增强、框架无关 CRUD Service、仓储",
    "redis": "Redis 全家——客户端封装、缓存后端、分布式锁",
    "httpx": "HTTP 客户端 + 负载均衡——服务发现/熔断/重试/指标/OTel 透传",
    "middleware": "Hertz 自研标准中间件栈 + contrib 桥接 + hmw",
    "nns": "NNS 服务治理客户端——provider 无关发现/配置/实例缓存",
    "nns-endpoint": "NNS 端点工厂——per-service 熔断、换实例重试、gRPC 池",
    "bootstrap": "应用生命周期引导——bootstrap.Run 骨架 + healthz",
    "handler": "HTTP handler 层——通用 CRUD Handler、参数绑定、版本端点",
    "hertz": "Hertz 运行时集成——日志桥接、生命周期、SSE/health/metrics、适配器、OTel",
    "kitex": "Kitex 生态——日志桥接、治理热更、生命周期、BizError 桥接、kitexotel",
    "workflow": "组合式流程编排——串行/并行/异步/条件分支 + Hook AOP",
    "kitex-examples": "Kitex echo 示例——client/server + IDL + 治理配置",
}

DEFAULT_BASE = os.environ.get("MATRIX_BASE", "/Users/zhangyi/my_project/ekit-split")


def repo_dir(repo: str, base: str | None = None) -> Path:
    base = Path(base or os.environ.get("MATRIX_BASE", DEFAULT_BASE))
    for cand in (base / repo, base / f"ekit-{repo}", base / "repos" / repo):
        if (cand / "go.mod").exists() or (cand / "README.md").exists():
            return cand
    raise FileNotFoundError(f"repo {repo} 不存在于 {base} 的任何已知布局中")


def go_mod(repo: str, base: str | None = None) -> str:
    return (repo_dir(repo, base) / "go.mod").read_text()


def module_line(repo: str, base: str | None = None) -> str:
    return go_mod(repo, base).splitlines()[0].strip()


def sibling_deps(repo: str, base: str | None = None) -> list[str]:
    """go.mod 里 require 的其他矩阵模块（排序去重）。"""
    out = set()
    for ln in go_mod(repo, base).splitlines():
        m = re.match(r"^\tgithub\.com/oekit/([a-z-]+) v", ln)
        if m and m.group(1) != repo:
            out.add(m.group(1))
    return sorted(out)


def latest_tag(repo: str, base: str | None = None, remote: bool = False) -> str | None:
    d = repo_dir(repo, base)
    if remote:
        r = subprocess.run(["git", "ls-remote", "--tags", f"https://github.com/{ORG}/{repo}.git"],
                           cwd=d, capture_output=True, text=True)
        tags = [ln.rsplit("/", 1)[-1] for ln in r.stdout.splitlines()
                if ln.strip() and not ln.rstrip().endswith("^{}")]
    else:
        r = subprocess.run(["git", "-C", str(d), "tag", "-l", "--sort=-v:refname"],
                           capture_output=True, text=True)
        tags = r.stdout.split()
    semver = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return semver[0] if semver else None


def bump_patch(v: str) -> str:
    a, b, c = v[1:].split(".")
    return f"v{a}.{b}.{int(c) + 1}"


def sh(args, cwd, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr[-400:]}")
    return r
