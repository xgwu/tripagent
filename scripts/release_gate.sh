#!/usr/bin/env bash
# 发布前门禁：语法编译 → 单测 → 离线回归 → 城市库校验。
# 全绿才允许发布线上/推送 GitHub。用法：bash scripts/release_gate.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python3}

echo "== 1/4 语法编译 =="
$PY -m compileall -q src webui scripts

echo "== 2/4 单元测试 =="
for t in scripts/test_*.py; do
  echo "-- $t"
  $PY "$t"
done

echo "== 3/4 离线回归 =="
$PY scripts/eval_regression.py | tail -2

echo "== 4/4 城市库校验 =="
VLOG=$(mktemp)
if ! $PY scripts/validate_city.py > "$VLOG" 2>&1; then
  cat "$VLOG"
  echo "❌ 城市库校验未通过"
  exit 1
fi
grep -c "✅ 通过" "$VLOG" | xargs -I{} echo "{} 个城市通过"
rm -f "$VLOG"

echo "🟢 门禁全绿，可以发布"
