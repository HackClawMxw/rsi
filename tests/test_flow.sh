#!/usr/bin/env bash
# e2e 自检：在临时目录搭最小 lab，跑通 播种→入账→签名清点→账本→建候选→全仓自检。
# 用法：bash tests/test_flow.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
FIX="$ROOT/tests/fixtures"
TD="$(mktemp -d "${TMPDIR:-/tmp}/rsi-e2e.XXXXXX")"
trap 'rm -rf "$TD"' EXIT

echo "== 0) 搭最小 lab =="
mkdir -p "$TD/runs" "$TD/candidates" "$TD/ledger" "$TD/canaries"
cp "$ROOT/candidates/template.md" "$TD/candidates/"
cp "$ROOT/ledger/ledger.md" "$TD/ledger/"
cp "$ROOT/canaries/validated-versions.md" "$TD/canaries/"
cp "$FIX/signatures-sample.md" "$TD/"

echo "== 1) 播种签名注册表 =="
python3 "$SCRIPTS/seed_signatures.py" "$TD/signatures-sample.md" --out "$TD/signatures.json" | sed 's/^/  /'
test -f "$TD/signatures.json"

echo "== 2) 入账 run =="
python3 "$SCRIPTS/import_run.py" "$FIX/run-sample" --out "$TD/runs/run-sample.json" | sed 's/^/  /'

echo "== 3) 签名清点并回写 metrics =="
python3 "$SCRIPTS/count_signatures.py" --registry "$TD/signatures.json" \
  --logs "$FIX/run-sample" --update-metrics "$TD/runs/run-sample.json" | sed 's/^/  /'
python3 - "$TD/runs/run-sample.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
assert m["total_span_seconds"] > 0, "跨度应 > 0"
assert m["signature_hits"], "signature_hits 不应为 null"
assert m["signature_hits"].get("SIG-901", 0) >= 1, "SIG-901 应命中"
assert m["signature_hits"].get("SIG-902", 0) >= 1, "SIG-902 应命中"
assert m["extraction"]["wait_candidates"], "应有等待候选窗口"
print("  metrics 断言通过:", {k: v for k, v in m["signature_hits"].items() if v})
PY

echo "== 4) 账本重算 =="
python3 "$SCRIPTS/update_ledger.py" --root "$TD" --write | sed 's/^/  /'
grep -q "AUTO:BEGIN" "$TD/ledger/ledger.md" && echo "  账本自动节已写入"

echo "== 5) 新建候选 =="
python3 "$SCRIPTS/new_candidate.py" --root "$TD" --slug test-sig \
  --title "端到端自检候选" --source-run run-sample --signature SIG-901 | sed 's/^/  /'
test -f "$TD/candidates/C-001-test-sig.md"

echo "== 6) 全仓自检 =="
python3 "$SCRIPTS/check_repo.py" --root "$TD" | sed 's/^/  /'

echo "E2E PASS"
