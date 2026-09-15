#!/usr/bin/env bash
# 兼容入口（README / docs / GitHub Actions 历史上引用此路径）。
# 真正的门禁逻辑在 scripts/release_gate.py —— 纯 Python 实现，不依赖
# dirname/grep/mktemp/head，因此本机 Git Bash shim 故障时也能用：
#     python scripts/release_gate.py
set -euo pipefail
SELF="${BASH_SOURCE[0]}"
cd "${SELF%/*}/.."
exec "${PY:-python3}" scripts/release_gate.py "$@"
