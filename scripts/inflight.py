#!/usr/bin/env python3
"""在途登记：同机并行会话的互斥协调（库 + CLI）。

所有并行会话共享同一份本地工作区（同一文件系统），撞车都发生在本地工作树上
（checkout -f 冲掉对方未提交产物、把别人分支上的内容推走）。本登记以
matrix-tools/.in-flight/<repo>.json 的原子创建（O_EXCL）当互斥锁：

  python3 scripts/inflight.py claim <repo> 正在做的事   # 开工登记（占用中即拒绝）
  python3 scripts/inflight.py release <repo>            # 收尾释放
  python3 scripts/inflight.py list                      # 全部在途

PID 存活检测：持有者进程已死 → 判定陈旧锁，claim 可接管（打印告警）。
preflight.py（开工检查）、handoff.py（交接清单）、release_train.py --apply 均读取本登记。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / ".in-flight"

# claim 的 pid 是登记时的短命 CLI 进程，收尾即退出——活性判定不能只看 pid。
# 持有 = pid 存活（仍在跑的导入式长任务）或 未过时效（会话尺度 TTL）；两者皆否 = 陈旧可接管。
FRESH_SECONDS = 4 * 3600


def _path(repo: str) -> Path:
    return REGISTRY / f"{repo}.json"


def held(data: dict) -> bool:
    """登记是否仍视为被持有：pid 活 或 未过 TTL。"""
    alive = False
    pid = data.get("pid")
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
    fresh = (time.time() - data.get("ts", 0)) < FRESH_SECONDS
    return alive or fresh


def age_of(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def read(repo: str) -> dict | None:
    p = _path(repo)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def entries() -> list[tuple[str, dict, bool]]:
    """全部登记 [(repo, data, holder_alive)]。"""
    out = []
    if not REGISTRY.exists():
        return out
    for p in sorted(REGISTRY.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append((p.stem, data, held(data)))
    return out


def claim(repo: str, note: str) -> dict:
    """登记占用。已被活进程持有 → 拒绝；陈旧锁 → 告警后接管。"""
    REGISTRY.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        cur = read(repo)
        if cur and held(cur):
            raise SystemExit(
                f"❌ {repo} 已被占用：pid {cur.get('pid')}（{age_of(cur.get('ts', 0))} 前）"
                f"「{cur.get('note', '')}」——先跑 handoff.py 看现场；"
                f"确认对方已收尾可 release 后重试")
        if cur:
            print(f"⚠ 陈旧锁（pid {cur.get('pid')} 已退出且超过 {FRESH_SECONDS//3600}h 时效），接管：{repo}")
        data = {"repo": repo, "note": note, "pid": os.getpid(), "ts": time.time()}
        try:
            fd = os.open(_path(repo), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            return data
        except FileExistsError:
            continue  # read 与 open 之间被人抢先，重读裁决
    raise SystemExit(f"❌ {repo}: 登记竞争激烈，稍后重试")


def release(repo: str) -> None:
    p = _path(repo)
    if not p.exists():
        print(f"☑ {repo}: 无在途登记")
        return
    cur = read(repo) or {}
    p.unlink()
    holder = f"pid {cur.get('pid')}「{cur.get('note', '')}」" if cur else "?"
    print(f"✓ {repo}: 已释放（原持有 {holder}）")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    cmd = args[0]
    if cmd == "claim" and len(args) >= 3:
        d = claim(args[1], " ".join(args[2:]))
        print(f"✓ 已登记：{d['repo']}（pid {d['pid']}）「{d['note']}」——收尾记得 release")
    elif cmd == "release" and len(args) == 2:
        release(args[1])
    elif cmd == "list":
        es = entries()
        if not es:
            print("（无在途登记）")
            return
        for repo, data, alive in es:
            mark = "●" if alive else "○陈旧"
            print(f"{mark} {repo:<16} pid {data.get('pid')} "
                  f"{age_of(data.get('ts', 0))}前「{data.get('note', '')}」")
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
