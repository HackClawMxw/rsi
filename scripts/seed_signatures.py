#!/usr/bin/env python3
"""从 AscendBot 的 troubleshooting-signatures.md 播种签名注册表。

用法：
    python3 seed_signatures.py <signatures.md> [--out PATH]

抽取规则：表行首列为 SIG-0NN 编号的，取正则列所有反引号包裹的片段作为候选
正则；能编译的进 patterns，编不过的记入 dropped 并标 needs_review。
产物是草稿——入库前人工过一遍 needs_review 行。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

SIG_ID = re.compile(r"^SIG-\d{3}$")
BACKTICK = re.compile(r"`([^`]+)`")


def parse(md_text: str) -> list[dict]:
    sigs: dict[str, dict] = {}
    for line in md_text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not SIG_ID.match(cells[0]):
            continue
        sig_id = cells[0]
        candidates = BACKTICK.findall(cells[1])
        patterns, dropped = [], []
        for pat in candidates:
            try:
                re.compile(pat)
                patterns.append(pat)
            except re.error:
                dropped.append(pat)
        sigs[sig_id] = {
            "id": sig_id,
            "patterns": patterns,
            "dropped_patterns": dropped,
            "root_cause": cells[2] if len(cells) > 2 else "",
            "applicability": cells[-1] if len(cells) > 3 else "",
            "needs_review": not patterns or bool(dropped),
        }
    return list(sigs.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("signatures_md", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    md = args.signatures_md
    if not md.is_file():
        print(f"错误：找不到 {md}", file=sys.stderr)
        return 1

    sigs = parse(md.read_text(encoding="utf-8", errors="replace"))
    if not sigs:
        print("错误：没抽到任何 SIG-0NN 行，检查文件格式", file=sys.stderr)
        return 1

    review = [s["id"] for s in sigs if s["needs_review"]]
    registry = {
        "source": str(md),
        "seeded_at": date.today().isoformat(),
        "signatures": sigs,
    }
    out = args.out or (Path(__file__).resolve().parent.parent / "signatures.json")
    out.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"已写出 {out}")
    print(f"  签名 {len(sigs)} 条；带正则 {len(sigs) - len(review)} 条；"
          f"需人工复核 {len(review)} 条"
          + (f"：{', '.join(review)}" if review else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
