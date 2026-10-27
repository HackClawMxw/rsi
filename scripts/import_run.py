#!/usr/bin/env python3
"""从目标仓 .day0/<run>/ 提取 run_metrics.json（契约一；也用于挖历史 run）。

用法：
    python3 import_run.py <run_dir> [--out PATH] [--gap-threshold SECONDS]

解析 <run_dir>/tracker.md 的进度日志表（表头：时间 | 步骤 | 事件 | …），
机械提取两类值：
  - total_span_seconds   首末时间戳跨度
  - wait_candidates      相邻事件间隔超过 --gap-threshold（默认 600s）的窗口，
                          供人工归因为等待/授权窗——脚本只报窗口，不做归因

语义字段（阶段时长、等待归因、签名命中、返工轮数、摩擦数）无法机械判定，
一律留 null，由 closeout 侧或人工补齐。本脚本宁可少算，不许编数。

退出码：0 成功；1 tracker 缺失或没有可识别的进度日志表；2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%H:%M:%S",
    "%H:%M",
)

FULL_DATE_FORMATS = TIME_FORMATS[:3]


def parse_time_cell(text: str, base: datetime | None) -> datetime | None:
    """解析时间单元格；纯时间列借进度表里首次出现的日期补全。"""
    text = text.strip()
    for fmt in TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.year == 1900 and base is not None and fmt not in FULL_DATE_FORMATS:
            dt = dt.replace(year=base.year, month=base.month, day=base.day)
        return dt
    return None


def extract_progress_rows(tracker_text: str) -> list[list[str]]:
    """取「时间 | 步骤 | …」表头之后、首个非表行之前的所有数据行。"""
    rows: list[list[str]] = []
    in_table = False
    for line in tracker_text.splitlines():
        if not line.lstrip().startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0] == "时间" and any("步骤" in c for c in cells[1:3]):
            in_table = True
            continue
        if not in_table:
            continue
        if set(cells[0]) <= set("-: "):
            continue  # 表头分隔行
        rows.append(cells)
    return rows


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path, help="目标仓 .day0/<run>/ 目录")
    ap.add_argument("--out", type=Path, default=None,
                    help="输出路径（默认 <run_dir>/run_metrics.json）")
    ap.add_argument("--gap-threshold", type=int, default=600,
                    help="等待候选窗口阈值，秒（默认 600）")
    ap.add_argument("--force", action="store_true",
                    help="允许覆盖已存在的输出文件（closeout 原件保护）")
    args = ap.parse_args()

    run_dir = args.run_dir.resolve()
    tracker = run_dir / "tracker.md"
    if not tracker.is_file():
        print(f"错误：找不到 {tracker}", file=sys.stderr)
        return 1

    rows = extract_progress_rows(tracker.read_text(encoding="utf-8", errors="replace"))
    if not rows:
        print("错误：tracker.md 里没有可识别的进度日志表（表头须含「时间 | 步骤」）",
              file=sys.stderr)
        return 1

    base: datetime | None = None
    parsed: list[tuple[str, str, datetime]] = []  # (time_raw, event, dt)
    skipped = 0
    prev: datetime | None = None
    for cells in rows:
        dt = parse_time_cell(cells[0], base)
        if dt is None:
            skipped += 1
            continue
        if base is None and dt.year != 1900:  # 首个带完整日期的时间戳，供纯时间列补全
            base = dt
        if prev is not None and "-" not in cells[0]:
            while dt < prev:  # 仅纯时间列补跨天（如 23:50 → 00:10）；完整日期乱序是数据错误，交给负跨度检查
                dt += timedelta(days=1)
        parsed.append((cells[0], cells[2], dt))
        prev = dt

    if len(parsed) < 2:
        print(f"错误：可解析的时间戳不足（{len(parsed)}/{len(rows)}），无法计算跨度",
              file=sys.stderr)
        return 1

    first, last = parsed[0][2], parsed[-1][2]
    span = (last - first).total_seconds()
    if span < 0:
        print(f"错误：时间序列回退（跨度 {fmt_duration(int(span))}），"
              "检查 tracker 时间列是否有乱序/错日期", file=sys.stderr)
        return 1

    wait_candidates = []
    for (t0, e0, d0), (t1, e1, d1) in zip(parsed, parsed[1:]):
        gap = (d1 - d0).total_seconds()
        if gap >= args.gap_threshold:
            wait_candidates.append({
                "after": f"{t0} {e0}",
                "before": f"{t1} {e1}",
                "gap_seconds": int(gap),
            })

    metrics = {
        "run_id": run_dir.name,
        "skill_anchor": None,
        "model": None,
        "stages": {"S1": null_stage(), "S2": null_stage(),
                   "S3": null_stage(), "S4": null_stage()},
        "total_span_seconds": int(span),
        "human_wait_seconds": None,
        "authorization_windows_seconds": None,
        "signature_hits": None,
        "rework_rounds": None,
        "friction_count": None,
        "outcome": None,
        "source_tracker": str(tracker),
        "notes": "import_run.py 从事后挖账生成；语义字段待人工补齐",
        "extraction": {
            "events_total": len(rows),
            "events_parsed": len(parsed),
            "events_skipped": skipped,
            "span_human": fmt_duration(span),
            "wait_candidates": wait_candidates,
        },
    }

    out = args.out or (run_dir / "run_metrics.json")
    if out.exists() and not args.force:
        print(f"错误：{out} 已存在（可能是 closeout 产出的原件）。"
              "确认覆盖用 --force，或用 --out 指到别的路径", file=sys.stderr)
        return 1
    out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"已写出 {out}")
    print(f"  事件 {len(parsed)}/{len(rows)} 可解析，跨度 {fmt_duration(span)}")
    print(f"  等待候选窗口（≥{args.gap_threshold}s）：{len(wait_candidates)} 个")
    return 0


def null_stage() -> dict:
    return {"status": None, "seconds": None}


if __name__ == "__main__":
    sys.exit(main())
