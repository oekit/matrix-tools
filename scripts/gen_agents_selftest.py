#!/usr/bin/env python3
"""gen_agents 准入三问过闸自测（matrix-tools#344，2026-11-28 演进评审 P4）。

过闸是 fail-closed 的新仓冷启动闸——它红错了（拒掉合法冷启动）挡人，绿错了
（漏答案也放行）让「三问」退化成勾选戏。两个方向都要夹具钉死，且**零副作用**：
只测纯函数（parse_admission / render_admission_section），不写任何 AGENTS.md。

覆盖面：
  T1  三旗全缺 → missing 恰好三个标签
  T2  部分旗 → missing 恰好缺的那几个（多答不少算）
  T3  三旗全给非空答案 → answers 齐、missing 空
  T4  旗值为空 / 旗值是下一个旗（串位）→ 该问仍算 missing（不认占位）
  T5  render_admission_section 嵌入三个标签与答案原文（留档面）
  T6  MODULE_ADMISSION_3Q_MD 在场且含三问关键词（rules.py 被清空/删节 ⇒ 红）
  T7  gen_agents 可安全 import（脚本流收进 main 后守卫不再拒 import——
      回归钉：旧版 `if __name__ != "__main__": raise` 会让本自测无法存在）

用法：python3 scripts/gen_agents_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import gen_agents  # noqa: E402  （import 即 T7：不得有副作用/不得拒 import）
import rules  # noqa: E402

failures: list[str] = []


def case(name: str, cond: bool, detail: str = ""):
    tag = "✓" if cond else "❌"
    print(f"  {tag} {name}" + (f"——{detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    print("gen_agents 准入过闸自测（零副作用，纯函数）")

    # T1 三旗全缺
    _, missing = gen_agents.parse_admission(["--new-repo", "foo"])
    case("T1 三旗全缺 → 缺三问", sorted(missing) ==
         sorted(["独立生命周期", "使用场景", "稳定 API"]), str(missing))

    # T2 部分旗
    _, missing2 = gen_agents.parse_admission(
        ["--new-repo", "foo", "--admit-lifecycle", "独立发版 3 年有矩阵外消费方"])
    case("T2 只答一问 → 缺两问", sorted(missing2) == sorted(["使用场景", "稳定 API"]),
         str(missing2))

    # T3 全给非空
    argv3 = ["--new-repo", "foo",
             "--admit-lifecycle", "独立发版节奏，矩阵外 2 个消费方",
             "--admit-scenario", "实时消息通道（WebSocket/NATS 推送）",
             "--admit-api", "首版 api-lifecycle.yaml 全包 Stable"]
    answers3, missing3 = gen_agents.parse_admission(argv3)
    case("T3 三旗全给 → 答案齐、缺空", not missing3 and len(answers3) == 3,
         f"missing={missing3} answers={answers3}")

    # T4 空值 / 串位
    _, missing4a = gen_agents.parse_admission(
        ["--admit-lifecycle", "", "--admit-scenario", "s", "--admit-api", "a"])
    case("T4a 空值算缺", missing4a == ["独立生命周期"], str(missing4a))
    _, missing4b = gen_agents.parse_admission(
        ["--admit-lifecycle", "--admit-scenario", "s", "--admit-api", "a"])
    case("T4b 串位（吃到下一个旗）算缺", missing4b == ["独立生命周期"], str(missing4b))
    _, missing4c = gen_agents.parse_admission(["--admit-lifecycle"])  # 旗在尾部无值
    # 尾部旗无值 ⇒ lifecycle 因无值缺；另两旗本来就缺席 ⇒ 三问全缺
    case("T4c 尾部旗无值算缺（且另两旗本就缺）",
         sorted(missing4c) == sorted(["独立生命周期", "使用场景", "稳定 API"]),
         str(missing4c))

    # T5 留档段
    sec = gen_agents.render_admission_section(answers3)
    case("T5 留档段含三标签与答案原文",
         all(f"**{k}**：{v}" in sec for k, v in answers3.items())
         and "准入三问" in sec and "#344" in sec, sec)

    # T6 三问文本在场（rules.py 被清空/删节 ⇒ 红——文本是过闸打印面与 skill 投影的单源）
    md = rules.MODULE_ADMISSION_3Q_MD
    case("T6 三问文本在场且含三问关键词",
         all(k in md for k in ("独立生命周期", "使用场景", "稳定 API"))
         and "进度的尺子" in md, md[:80])

    # T7 import 安全（旧版守卫会 raise SystemExit——本自测能跑到这里就是证明，
    # 再显式断言 main 存在且脚本流已收编）
    case("T7 可 import 且 main() 在场", callable(getattr(gen_agents, "main", None)))

    if failures:
        print(f"\n{len(failures)} 个用例失败：{failures}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
