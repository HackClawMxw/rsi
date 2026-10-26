#!/usr/bin/env python3
"""用 runs/ 快照重算账本自动统计节（机械部分；耗时归因列永远人工填）。

用法：
    python3 update_ledger.py [--root PATH] [--write]

不带 --write 时把将生成的 markdown 打到 stdout 预览。
只重写 ledger.md 中 `<!-- AUTO:BEGIN ... -->` 与 `<!-- AUTO:END -->`
之间的内容；标记不存在时在文末追加。人工写的内容不会被动到。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

BEGIN = "<!-- AUTO:BEGIN update_ledger -->"
END = "<!-- AUTO:END -->"


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def load_runs(runs_dir: Path) -> list[dict]:
    runs = []
    for f in sorted(runs_dir.glob("*.json")):
        try:
            runs.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            print(f"警告：跳过坏快照 {f.name}：{e}", file=sys.stderr)
    return runs


def build_block(runs: list[dict]) -> str:
    today = date.today().isoformat()
    sig_stats: dict[str, dict] = {}
    lines_b = []
    for r in runs:
        rid = r.get("run_id", "?")
        span = r.get("total_span_seconds")
        ext = r.get("extraction") or {}
        wait_bound = sum(w.get("gap_seconds", 0)
                         for w in ext.get("wait_candidates", []))
        human = r.get("human_wait_seconds")
        friction = r.get("friction_count")
        lines_b.append(
            f"| {rid} | {fmt_duration(span)} | {fmt_duration(wait_bound)} "
            f"| {fmt_duration(human) if human is not None else '—'} "
            f"| {friction if friction is not None else '—'} |")
        for sid, n in (r.get("signature_hits") or {}).items():
            st = sig_stats.setdefault(sid, {"runs": 0, "hits": 0})
            if n:
                st["runs"] += 1
                st["hits"] += n

    lines_a = [f"| {sid} | {st['runs']}/{len(runs)} | {st['hits']} |"
               for sid, st in sorted(sig_stats.items()) if st["hits"] > 0]

    block = f"""{BEGIN}
## 自动统计（{today}，update_ledger.py 生成；共 {len(runs)} 个 run）

> 口径：本节只做机械统计，不含归因；耗时归因列在上方人工表格里填（实测/回算分开标）。

### A. 签名频次

| 签名 | 命中 run 数 | 命中总次数 |
|---|---|---|
{chr(10).join(lines_a) if lines_a else "| （无命中数据） | — | — |"}

### B. 流程性指标

| run | 跨度 | 等待窗口合计（下界） | 人工等待 | 摩擦数 |
|---|---|---|---|---|
{chr(10).join(lines_b) if lines_b else "| （无 run） | — | — | — | — |"}

（等待窗口合计 = 该 run 内 ≥阈值间隔的机械下界，归因见各 run 快照；「—」= 未测。）
{END}"""
    return block


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    ledger = args.root / "ledger" / "ledger.md"
    runs_dir = args.root / "runs"
    if not ledger.is_file():
        print(f"错误：找不到 {ledger}", file=sys.stderr)
        return 1
    runs = load_runs(runs_dir) if runs_dir.is_dir() else []
    if not runs:
        print("提示：runs/ 里没有可用快照，先跑 import_run.py", file=sys.stderr)

    block = build_block(runs)
    text = ledger.read_text(encoding="utf-8")
    if BEGIN in text:
        pre = text[: text.index(BEGIN)]
        post = text[text.index(END) + len(END):]
        new_text = pre + block + post
    else:
        new_text = text.rstrip() + "\n\n" + block + "\n"

    if args.write:
        ledger.write_text(new_text, encoding="utf-8")
        print(f"已重算 {ledger}（{len(runs)} 个 run）")
    else:
        print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
