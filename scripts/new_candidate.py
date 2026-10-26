#!/usr/bin/env python3
"""从模板新建演进候选（自动分配序号，填基础字段）。

用法：
    python3 new_candidate.py --slug weight-mapping --title "签名011的映射补全" \
        [--source-run run_id] [--signature SIG-011] [--root PATH]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^C-(\d{3})-[a-z0-9-]+\.md$")


def next_seq(cand_dir: Path) -> int:
    nums = [int(m.group(1)) for f in cand_dir.glob("C-*.md")
            if (m := NAME_RE.match(f.name))]
    return max(nums, default=0) + 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--title", required=True)
    ap.add_argument("--slug", required=True,
                    help="文件名用短横线小写串，如 weight-mapping")
    ap.add_argument("--source-run", default=None)
    ap.add_argument("--signature", default="none")
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9-]+", args.slug):
        print(f"错误：slug 只允许小写字母/数字/短横线：{args.slug}", file=sys.stderr)
        return 2

    cand_dir = args.root / "candidates"
    template = cand_dir / "template.md"
    if not template.is_file():
        print(f"错误：找不到 {template}", file=sys.stderr)
        return 1

    seq = next_seq(cand_dir)
    out = cand_dir / f"C-{seq:03d}-{args.slug}.md"
    if out.exists():
        print(f"错误：{out} 已存在", file=sys.stderr)
        return 1

    text = template.read_text(encoding="utf-8")
    text = text.replace("# C-<seq>：<一句话标题>", f"# C-{seq:03d}：{args.title}")
    text = text.replace(
        "- 来源：run <run_id> / 契约二登记 / 账本行 <链接>",
        f"- 来源：run {args.source_run or '<待填>'} / 契约二登记")
    text = text.replace("- 关联签名：SIG-xxx / none",
                        f"- 关联签名：{args.signature}")
    out.write_text(text, encoding="utf-8")
    print(f"已创建 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
