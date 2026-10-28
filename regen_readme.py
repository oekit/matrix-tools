#!/usr/bin/env python3
"""按 DESC 重新生成各仓 README 的「矩阵总览」表。只动该表区块，其余内容不碰。"""
import re, sys
from pathlib import Path
# 按脚本自身位置定位 matrix-tools（写死旧工作区路径 ⇒ 工具在本机直接 ImportError，#116 同族）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import MODULES, ORG, DESC, repo_dir, ensure_clean_all, sibling_deps, DEFAULT_BASE

HEADING = f"## 矩阵总览（{len(MODULES)} 活跃模块，自动生成）"

def build_table(base=None):
    lines = [HEADING, "", "| 模块 | 内容 | 矩阵内依赖 |", "|---|---|---|"]
    for name in MODULES:
        d = repo_dir(name, base)
        desc = DESC[name]
        deps = sibling_deps(name, base)
        if deps:
            dep = "、".join(f"[{x}](https://github.com/{ORG}/{x})" for x in deps)
        else:
            dep = "—"
        lines.append(f"| [{name}](https://github.com/{ORG}/{name}) | {desc} | {dep} |")
    return lines

def regen(readme_path: Path, base=None):
    src = readme_path.read_text().split('\n')
    try:
        h = next(i for i, l in enumerate(src) if l.startswith('## 矩阵总览'))
    except StopIteration:
        return 'no-section'
    # 区块终点：表后的第一个非表行（> 注记 或 下一个标题 或 EOF）
    end = len(src)
    for i in range(h + 1, len(src)):
        if src[i].startswith('> ') or (src[i].startswith('## ') and i != h):
            end = i
            break
    # 保留区块内表后的注记行（> 开头）
    notes = [l for l in src[h+1:end] if l.startswith('> ')]
    new_block = build_table(base)
    if notes:
        new_block.append("")
        new_block.extend(notes)
    out = src[:h] + new_block + src[end:]
    if out == src:
        return 'unchanged'
    readme_path.write_text('\n'.join(out))
    return 'updated'

if __name__ == '__main__':
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    _dirs = []
    for _n in MODULES:
        try:
            _dirs.append(repo_dir(_n, base))
        except FileNotFoundError:
            pass
    ensure_clean_all(_dirs, 'regen_readme')  # 产物必须落在已提交基线上
    for name in MODULES:
        try:
            d = repo_dir(name, base)
        except FileNotFoundError:
            print(f'!! {name}: 目录不存在'); continue
        st = regen(d / 'README.md', base)
        print(f'{name}: {st}')
