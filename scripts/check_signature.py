#!/usr/bin/env python3
"""签名正则回放校验（轻档验证的门）。

用法：
    python3 check_signature.py --pattern '<regex>' --logs <file|dir>... [--context N]

在给定日志（文件，或目录递归）中检索正则；任一命中退出码 0，否则 1。
新签名入库前的必过门：它声称来源的历史日志必须被其正则命中。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAX_FILE_BYTES = 100 * 1024 * 1024
SKIP_DIRS = {".git", "__pycache__", ".ruff_cache"}


def iter_logs(inputs: list[Path]):
    seen = set()
    for p in inputs:
        if p.is_file():
            if p not in seen:
                seen.add(p)
                yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not any(part in SKIP_DIRS for part in f.parts):
                    if f not in seen:
                        seen.add(f)
                        yield f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pattern", required=True, help="签名 grep 正则（Python 语法）")
    ap.add_argument("--logs", nargs="+", required=True, type=Path,
                    help="待回放的日志文件或目录")
    ap.add_argument("--context", type=int, default=3,
                    help="每个文件最多展示的命中行数（默认 3）")
    args = ap.parse_args()

    try:
        rx = re.compile(args.pattern)
    except re.error as e:
        print(f"错误：正则不合法：{e}", file=sys.stderr)
        return 2

    total = 0
    files_hit = 0
    for path in iter_logs(args.logs):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                print(f"跳过（过大）：{path}", file=sys.stderr)
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"跳过（读取失败：{e}）：{path}", file=sys.stderr)
            continue

        hits = [(i, line) for i, line in enumerate(text.splitlines(), 1)
                if rx.search(line)]
        if not hits:
            continue
        total += len(hits)
        files_hit += 1
        print(f"{path}: {len(hits)} 处命中")
        for lineno, line in hits[: args.context]:
            print(f"  L{lineno}: {line.strip()[:200]}")

    print(f"\n合计 {total} 处命中，分布于 {files_hit} 个文件。")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
