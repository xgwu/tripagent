#!/usr/bin/env bash
# =============================================================================
# 本地 → 云主机：推送代码并重启服务（Windows Git Bash 可用）
#
# 为什么不用 rsync：Windows Git Bash 自带 ssh/scp/tar，但没有 rsync。
# 这里用 `tar | ssh tar` 传，等价且零额外依赖，也不在云主机上留任何 git 凭据。
#
# 用法：
#   bash deploy/cvm/sync_from_local.sh <SSH目标> [远程目录] [私钥路径] [端口]
# 例：
#   bash deploy/cvm/sync_from_local.sh ubuntu@1.2.3.4
#   bash deploy/cvm/sync_from_local.sh ubuntu@1.2.3.4 /opt/tripagent ~/.ssh/tripagent_cvm 3000
#
# 幂等：重复执行只更新代码并重启服务。
# 前置：已把本机公钥加到云主机 ~/.ssh/authorized_keys（见 README 部署一节）。
# =============================================================================
set -euo pipefail

TARGET="${1:?用法: bash sync_from_local.sh <user@host> [远程目录] [私钥路径] [端口]}"
REMOTE_DIR="${2:-/opt/tripagent}"
KEYFILE="${3:-$HOME/.ssh/tripagent_cvm}"
PORT="${4:-3000}"
SERVICE="tripagent"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
[ -n "$KEYFILE" ] && [ -f "$KEYFILE" ] && SSH_OPTS+=(-i "$KEYFILE" -o IdentitiesOnly=yes)
SSH_CMD=(ssh "${SSH_OPTS[@]}" "$TARGET")

echo "==> 源目录: $ROOT"
echo "==> 目标:   $TARGET:$REMOTE_DIR　端口: $PORT"

[ -f "$ROOT/requirements.txt" ] || { echo "缺少 requirements.txt，路径不对？" >&2; exit 1; }
[ -f "$ROOT/secrets.json" ] || echo "⚠️  本地没有 secrets.json —— 云主机将以「离线兜底」模式运行（无 LLM/高德）。"

echo "==> 准备远程目录（/opt 等需 root，用 sudo 建好后归还给登录用户）"
"${SSH_CMD[@]}" "sudo mkdir -p '$REMOTE_DIR' && sudo chown \"\$(id -u):\$(id -g)\" '$REMOTE_DIR'"

echo "==> 打包并传输（排除 .git / __pycache__ / output / venv / .workbuddy）"
tar czf - \
  -C "$ROOT" \
  --exclude='./.git' \
  --exclude='./.git/*' \
  --exclude='./.workbuddy' \
  --exclude='./.workbuddy/*' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='./output' \
  --exclude='./output/*' \
  --exclude='./venv' \
  --exclude='./node_modules' \
  . | "${SSH_CMD[@]}" "tar xzf - -C '$REMOTE_DIR'"

if "${SSH_CMD[@]}" "systemctl list-unit-files | grep -q '^${SERVICE}\.service'"; then
  echo "==> 服务已存在：重启并自检"
  "${SSH_CMD[@]}" "sudo systemctl restart '$SERVICE'; sleep 2; systemctl is-active '$SERVICE'; curl -s -o /dev/null -w 'local /api/health -> HTTP %{http_code}\n' http://127.0.0.1:$PORT/api/health"
  echo
  echo "✅ 同步完成（已重启服务）。"
else
  echo
  echo "ℹ️  代码已同步，但 systemd 服务尚未注册。请在云主机上执行一次："
  echo "     sudo bash $REMOTE_DIR/deploy/cvm/setup_server.sh none $REMOTE_DIR $PORT"
fi
