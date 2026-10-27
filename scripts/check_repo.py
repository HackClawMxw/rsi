#!/usr/bin/env python3
"""全仓自检：契约 schema、候选生命周期、签名注册表、目录结构。

用法：
    python3 check_repo.py [--root PATH]

全部通过退出 0，任何 FAIL 退出 1。提交前和复盘会话收尾前各跑一次。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REQUIRED_METRIC_KEYS = [
    "run_id", "skill_anchor", "model", "stages", "total_span_seconds",
    "human_wait_seconds", "authorization_windows_seconds", "signature_hits",
    "rework_rounds", "friction_count", "outcome", "source_tracker",
]
OUTCOMES = {"signed", "pending", "aborted"}
STAGES = {"S1", "S2", "S3", "S4"}
STAGE_STATUS = {"signed", "fail", "pending", "skipped", "na"}
CAND_FILE = re.compile(r"^C-\d{3}-[a-z0-9-]+\.md$")
STATES = {"registered", "evidence-attached", "adjudicated", "change-proposed",
          "canary-passed", "merged"}
STATUS_LINE = re.compile(r"^-\s*状态：(\S+)", re.M)
NON_NEGATIVE_FIELDS = ["total_span_seconds", "human_wait_seconds",
                       "authorization_windows_seconds", "rework_rounds",
                       "friction_count"]


class Report:
    def __init__(self) -> None:
        self.fails = 0

    def check(self, ok: bool, name: str, detail: str = "") -> None:
        mark = "OK  " if ok else "FAIL"
        if not ok:
            self.fails += 1
        print(f"[{mark}] {name}" + (f"：{detail}" if detail and not ok else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args()
    root = args.root
    rep = Report()

    # 1. 目录结构。真仓模式（scripts/ 在 root 内）额外检查仓级文件；
    #    数据工作区（--root 指向临时 lab，e2e 用）只查数据件。
    repo_mode = (root / "scripts" / "check_repo.py").is_file()
    for rel in (["runs", "candidates", "ledger", "canaries",
                 "ledger/ledger.md", "canaries/validated-versions.md",
                 "candidates/template.md"]
                + (["scripts", "CONTRACT.md", "RUNBOOK.md"] if repo_mode else [])):
        rep.check((root / rel).exists(), f"结构 {rel}")

    # 2. runs/ 快照 schema
    seen_ids: set[str] = set()
    for f in sorted((root / "runs").glob("*.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            rep.check(False, f"快照 {f.name}", f"JSON 解析失败：{e}")
            continue
        missing = [k for k in REQUIRED_METRIC_KEYS if k not in m]
        rep.check(not missing, f"快照 {f.name} 必填字段",
                  f"缺 {missing}" if missing else "")
        rid = m.get("run_id")
        rep.check(rid == f.stem, f"快照 {f.name} run_id 与文件名一致",
                  f"{rid!r} != {f.stem!r}")
        if rid in seen_ids:
            rep.check(False, f"快照 {f.name} run_id 重复", rid)
        seen_ids.add(rid)
        if m.get("outcome") is not None:
            rep.check(m["outcome"] in OUTCOMES, f"快照 {f.name} outcome 枚举",
                      str(m["outcome"]))
        for field in NON_NEGATIVE_FIELDS:
            v = m.get(field)
            if v is not None:
                rep.check(isinstance(v, (int, float)) and v >= 0,
                          f"快照 {f.name} {field} 非负",
                          str(v))
        stages = m.get("stages")
        if isinstance(stages, dict) and stages:
            rep.check(set(stages) <= STAGES, f"快照 {f.name} stages 键",
                      str(set(stages) - STAGES))
            for sid, sv in stages.items():
                if isinstance(sv, dict) and sv.get("status") is not None:
                    rep.check(sv["status"] in STAGE_STATUS,
                              f"快照 {f.name} {sid} status 枚举",
                              str(sv["status"]))
                if isinstance(sv, dict) and sv.get("seconds") is not None:
                    rep.check(isinstance(sv["seconds"], (int, float))
                              and sv["seconds"] >= 0,
                              f"快照 {f.name} {sid} seconds 非负",
                              str(sv["seconds"]))

    # 3. 候选生命周期
    cand_dir = root / "candidates"
    vv_path = root / "canaries" / "validated-versions.md"
    vv_text = vv_path.read_text(encoding="utf-8") if vv_path.is_file() else ""
    for f in sorted(cand_dir.glob("C-*.md")):
        if not CAND_FILE.match(f.name):
            rep.check(False, f"候选命名 {f.name}", "须为 C-NNN-slug.md")
            continue
        text = f.read_text(encoding="utf-8")
        m = STATUS_LINE.search(text)
        # 状态允许带参数：merged(a0e648c) —— 取括号前的基本状态做枚举校验
        state = m.group(1).split("(")[0].strip() if m else None
        rep.check(state in STATES, f"候选 {f.name} 状态合法",
                  f"{state!r} 不在 {sorted(STATES)}")
        if state in {"canary-passed", "merged"}:
            rep.check(f.stem in vv_text,
                      f"候选 {f.name} 有验证记录",
                      f"{state} 但 {vv_path.name} 无 {f.stem} 的行——无记录不得放行")
        if state == "merged":
            rep.check(bool(re.search(r"merged\([^)]+\)", text)),
                      f"候选 {f.name} 已合入须带锚点",
                      "缺 merged(<skill_anchor>)")

    # 4. 签名注册表
    reg = root / "signatures.json"
    if reg.is_file():
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
            sigs = data.get("signatures", [])
            ids = [s.get("id") for s in sigs]
            rep.check(len(ids) == len(set(ids)), "签名注册表 id 唯一",
                      "有重复：" + ", ".join(
                          i for i in set(ids) if ids.count(i) > 1))
            bad = []
            for s in sigs:
                for pat in s.get("patterns", []):
                    try:
                        re.compile(pat)
                    except re.error:
                        bad.append(f"{s.get('id')}:{pat}")
            rep.check(not bad, "签名正则全部可编译", "; ".join(bad[:5]))
            review = [s["id"] for s in sigs if s.get("needs_review")]
            if review:
                print(f"[note] 签名需人工复核 {len(review)} 条："
                      + ", ".join(review))
        except (json.JSONDecodeError, OSError) as e:
            rep.check(False, "签名注册表", f"解析失败：{e}")
    else:
        print("[note] 无 signatures.json（先跑 seed_signatures.py）")

    # 5. 账本自动节标记（提示性）
    ledger = (root / "ledger" / "ledger.md")
    if ledger.is_file():
        has_auto = "AUTO:BEGIN" in ledger.read_text(encoding="utf-8")
        if not has_auto:
            print("[note] ledger.md 尚无自动统计节（先跑 update_ledger.py --write）")

    print(f"\n{'全部通过' if rep.fails == 0 else f'{rep.fails} 项失败'}")
    return 0 if rep.fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
