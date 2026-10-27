#!/usr/bin/env bash
# 反例回归：外部检视发现的每个 bug 固化为用例。
# 覆盖：跨天时间、覆盖保护、负跨度、md 误计、merged(锚点) 接受、
#       canary-passed 无记录拒绝、负值/非法枚举拒绝。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
FIX="$ROOT/tests/fixtures"
TD="$(mktemp -d "${TMPDIR:-/tmp}/rsi-ce.XXXXXX")"
trap 'rm -rf "$TD"' EXIT
fails=0
ck() { if [ "$1" = "0" ]; then echo "  PASS $2"; else echo "  FAIL $2"; fails=$((fails+1)); fi }

# —— 搭最小 lab ——
mkdir -p "$TD"/{runs,candidates,ledger,canaries}
cp "$ROOT/candidates/template.md" "$TD/candidates/"
cp "$ROOT/ledger/ledger.md" "$TD/ledger/"
cp "$ROOT/canaries/validated-versions.md" "$TD/canaries/"
cp "$FIX/signatures-sample.md" "$TD/"
python3 "$SCRIPTS/seed_signatures.py" "$TD/signatures-sample.md" --out "$TD/signatures.json" >/dev/null

# —— 用例1：跨天简写时间（23:50 → 00:10 应为 +20 分钟）——
mkdir -p "$TD/run1"
printf '| 时间 | 步骤 | 事件 | 产物 |\n|---|---|---|---|\n| 23:50:00 | P1 | 跨天前 | a |\n| 00:10:00 | P2 | 跨天后 | b |\n' > "$TD/run1/tracker.md"
python3 "$SCRIPTS/import_run.py" "$TD/run1" --out "$TD/runs/run1.json" >/dev/null 2>&1
S=$(python3 -I -c "import json;print(json.load(open('$TD/runs/run1.json'))['total_span_seconds'])")
[ "$S" = "1200" ]; ck $? "用例1 跨天跨度=1200s（实际 $S）"

# —— 用例2：已有 run_metrics.json 时不许覆盖，--force 才许 ——
echo '{"run_id":"run1","skill_anchor":"a0e648c","outcome":"signed"}' > "$TD/run1/run_metrics.json"
BEFORE=$(cat "$TD/run1/run_metrics.json")
python3 "$SCRIPTS/import_run.py" "$TD/run1" >/dev/null 2>&1
[ $? -ne 0 ] && [ "$(cat "$TD/run1/run_metrics.json")" = "$BEFORE" ]; ck $? "用例2 覆盖保护（原件未动且报错退出）"
python3 "$SCRIPTS/import_run.py" "$TD/run1" --force >/dev/null 2>&1
python3 -I -c "import json;d=json.load(open('$TD/run1/run_metrics.json'));exit(0 if d['skill_anchor'] is None else 1)"
ck $? "用例2b --force 明示后才覆盖"

# —— 用例3：完整日期乱序（负跨度）必须报错 ——
mkdir -p "$TD/run2"
printf '| 时间 | 步骤 | 事件 | 产物 |\n|---|---|---|---|\n| 2026-10-26 10:00:00 | P1 | 先 | a |\n| 2026-10-25 09:00:00 | P2 | 后（乱序） | b |\n' > "$TD/run2/tracker.md"
python3 "$SCRIPTS/import_run.py" "$TD/run2" --out "$TD/runs/run2.json" >/dev/null 2>&1
[ $? -ne 0 ]; ck $? "用例3 乱序时间报错退出"

# —— 用例4：分析文档里的错误文案不算签名命中 ——
mkdir -p "$TD/run3"
printf '复盘讨论：当时出现了 ValueError: shape mismatch 的现象。\n' > "$TD/run3/analysis.md"
printf 'log line: ValueError: shape mismatch under tensor parallel\n' > "$TD/run3/app.log"
cp "$TD/runs/run1.json" "$TD/runs/run3.json"
R=$(python3 "$SCRIPTS/count_signatures.py" --registry "$TD/signatures.json" --logs "$TD/run3" --update-metrics "$TD/runs/run3.json" 2>/dev/null | grep -c "SIG-902: 1 处命中")
N=$(python3 -I -c "import json;print(json.load(open('$TD/runs/run3.json'))['signature_hits']['SIG-902'])")
[ "$R" = "1" ] && [ "$N" = "1" ]; ck $? "用例4 仅 app.log 计命中（md 讨论不计），计数=$N"

# —— 用例5：merged(a0e648c) 带验证记录 → 接受 ——
printf '| 日期 | skill 锚点 | 候选/PR | 档位 | 段落 | 结果 | 机器时间 |\n|---|---|---|---|---|---|---|\n| 2026-10-26 | a0e648c | C-001 | 轻 | 全段 | PASS | 1s |\n' > "$TD/canaries/validated-versions.md"
mkdir -p "$TD/candidates"
printf -- '- 状态：merged(a0e648c)\n' > "$TD/candidates/C-001-x.md"
OUT=$(python3 "$SCRIPTS/check_repo.py" --root "$TD" 2>&1)
echo "$OUT" | grep -q "\[OK  \] 候选 C-001-x.md 状态合法"
ck $? "用例5 merged(锚点) 语法被接受"

# —— 用例6：canary-passed 无验证记录 → 拒绝 ——
printf -- '- 状态：canary-passed\n' > "$TD/candidates/C-002-y.md"
OUT=$(python3 "$SCRIPTS/check_repo.py" --root "$TD" 2>&1)
echo "$OUT" | grep -q "FAIL.*C-002-y.md 有验证记录"
ck $? "用例6 canary-passed 无记录被拒"

# —— 用例7/8：负值与非法 stage status 快照 → 拒绝 ——
printf '{"run_id":"bad1","skill_anchor":null,"model":null,"stages":{"S1":{"status":"ok","seconds":-5}},"total_span_seconds":-10,"human_wait_seconds":null,"authorization_windows_seconds":null,"signature_hits":null,"rework_rounds":null,"friction_count":null,"outcome":"signed","source_tracker":"x","notes":""}\n' > "$TD/runs/bad1.json"
OUT=$(python3 "$SCRIPTS/check_repo.py" --root "$TD" 2>&1)
echo "$OUT" | grep -q "FAIL.*bad1.json total_span_seconds 非负"; ck $? "用例7 负跨度被拒"
echo "$OUT" | grep -q "FAIL.*bad1.json S1 seconds 非负"; ck $? "用例7b 负 seconds 被拒"
echo "$OUT" | grep -q "FAIL.*bad1.json S1 status 枚举"; ck $? "用例8 非法 stage status 被拒"

if [ "$fails" = "0" ]; then echo "COUNTEREXAMPLE TESTS: 全部通过"; exit 0
else echo "COUNTEREXAMPLE TESTS: $fails 项失败"; exit 1; fi
