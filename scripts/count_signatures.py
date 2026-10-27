#!/usr/bin/env python3
"""按签名注册表清点 run 日志命中（签名频次的机械来源）。

用法：
    python3 count_signatures.py --registry signatures.json --logs <file|dir>... \
        [--update-metrics run_metrics.json] [--context N]

对注册表里每个签名的每条正则，在给定日志（文件或目录递归）中计数。
这是普查不是门禁——无论命中与否退出码都是 0（门禁用 check_signature.py）。
--update-metrics 把结果并进 run_metrics.json 的 signature_hits 字段。

默认只扫日志类文件（.log/.out/.txt/无扩展名）——分析文档里讨论到的错误
文案不算故障命中；确要扫全部文件加 --all。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAX_FILE_BYTES = 100 * 1024 * 1024
SKIP_DIRS = {".git", "__pycache__", ".ruff_cache"}
SCAN_EXTS = {".log", ".out", ".txt"}


def iter_logs(inputs: list[Path], scan_all: bool):
    seen: set[Path] = set()
    skipped_md = 0

    def want(f: Path) -> bool:
        nonlocal skipped_md
        if scan_all or f.suffix.lower() in SCAN_EXTS or f.suffix == "":
            return True
        skipped_md += 1
        return False

    for p in inputs:
        if p.is_file():
            if p in seen:
                continue
            seen.add(p)
            if want(p):
                yield p
            elif p.suffix.lower() in {".md", ".json"}:
                print(f"跳过（非日志，{p.suffix}）：{p.name}", file=sys.stderr)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not any(d in SKIP_DIRS for d in f.parts):
                    if f in seen:
                        continue
                    seen.add(f)
                    if want(f):
                        yield f
    if skipped_md:
        print(f"提示：跳过 {skipped_md} 个非日志文件（--all 可扫全部）",
              file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", required=True, type=Path)
    ap.add_argument("--logs", nargs="+", required=True, type=Path)
    ap.add_argument("--context", type=int, default=2)
    ap.add_argument("--update-metrics", type=Path, default=None)
    ap.add_argument("--all", action="store_true",
                    help="扫描全部文件（默认仅 .log/.out/.txt/无扩展名）")
    args = ap.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    files = list(iter_logs(args.logs, args.all))
    texts: list[tuple[Path, list[str]]] = []
    for f in files:
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                print(f"跳过（过大）：{f}", file=sys.stderr)
                continue
            texts.append((f, f.read_text(encoding="utf-8",
                                         errors="replace").splitlines()))
        except OSError as e:
            print(f"跳过（读取失败：{e}）：{f}", file=sys.stderr)

    counts: dict[str, int] = {}
    for sig in registry.get("signatures", []):
        total = 0
        for pat in sig.get("patterns", []):
            try:
                rx = re.compile(pat)
            except re.error:
                print(f"警告：{sig['id']} 存在编不过的正则（应标 needs_review）",
                      file=sys.stderr)
                continue
            for path, lines in texts:
                total += sum(1 for line in lines if rx.search(line))
        counts[sig["id"]] = total

    hit = {k: v for k, v in counts.items() if v > 0}
    for sig_id, n in sorted(hit.items()):
        print(f"{sig_id}: {n} 处命中")
        if args.context > 0:
            sig = next(s for s in registry["signatures"] if s["id"] == sig_id)
            for pat in sig["patterns"]:
                rx = re.compile(pat)
                for path, lines in texts:
                    shown = 0
                    for i, line in enumerate(lines, 1):
                        if rx.search(line) and shown < args.context:
                            print(f"  {path.name} L{i}: {line.strip()[:160]}")
                            shown += 1
    print(f"\n命中签名 {len(hit)}/{len(counts)} 条"
          + ("：" + ", ".join(sorted(hit)) if hit else "（全部零命中）"))

    if args.update_metrics:
        mpath = args.update_metrics
        metrics = json.loads(mpath.read_text(encoding="utf-8"))
        metrics["signature_hits"] = counts
        mpath.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
        print(f"已更新 {mpath} 的 signature_hits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
