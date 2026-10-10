#!/usr/bin/env bash
# =============================================================================
# TripAgent 云主机部署脚本（Ubuntu / Debian）
#
# 用法（在云主机上，用 sudo 执行）：
#   sudo bash setup_server.sh <域名|none> [应用目录] [端口] [certbot邮箱]
#
# 两种模式：
#   ① 纯 IP（无域名）：  sudo bash setup_server.sh none
#       → 装依赖 + systemd 常驻（监听 0.0.0.0:3000）+ nginx 反代 80 → 3000。
#         访问： http://<公网IP>/（80）或 http://<公网IP>:3000/
#         为什么要 nginx 占用 80：很多公司网络/运营商只放行标准端口 80/443，
#         非标准端口（3000）常被出站拦截。挂 80 可最大化可达性。
#   ② 域名 + HTTPS：     sudo bash setup_server.sh trip.example.com /opt/tripagent 3000 me@example.com
#       → nginx 用 server_name=$DOMAIN，并额外 certbot 签证书跳 HTTPS。
#
# 【HTTP Basic 认证】用环境变量开关（两种模式都生效；服务会真实消耗 LLM 费用，
# 强列建议开）：
#   BASIC_AUTH_USER=trip BASIC_AUTH_PASS='…' sudo -E bash setup_server.sh <域名|none> …
#   - 只给 BASIC_AUTH_USER 不给 PASS → 现场生成强口令并打印出来；
#   - 都不给 → 不启用（保持无鉴权，仅适合内网/临时）。
#   ⚠️ 必须带 `sudo -E` 让这两个变量透传进 root 环境，否则会被 sudo 丢掉。
#   面板远端后端靠 `secrets.json` 的 `remote_basic_auth`（"user:pass"）带凭据访问，
#   两边口令必须一致。
#
# 幂等：重复执行安全（已建的 venv / 服务 / nginx 站点都会复用、覆盖）。
#      若 htpasswd 已存在且未显式给 BASIC_AUTH_PASS ⇒ **保留原口令**（不会把你锁在外面）。
#
# 注意：本脚本【不】创建 secrets.json —— 由本地 `sync_from_local.sh` 推送，
#       或手动放到 <应用目录>/secrets.json（含 deepseek_api_key / amap_key / amap_js_key）。
# =============================================================================
set -euo pipefail

DOMAIN_ARG="${1:-none}"
APP_DIR="${2:-/opt/tripagent}"
PORT="${3:-3000}"
EMAIL="${4:-}"
SERVICE="tripagent"

# Basic 认证开关（经 sudo -E 传入）
AUTH_USER="${BASIC_AUTH_USER:-}"
AUTH_PASS="${BASIC_AUTH_PASS:-}"
HTPASSWD="/etc/nginx/.htpasswd_${SERVICE}"
AUTH_ENABLED=""
AUTH_PASS_GENERATED=""
AUTH_PASS_FINAL=""

# 归一化：none / - / ip / 空 都视为「纯 IP 模式」
case "${DOMAIN_ARG,,}" in
  ""|none|-|ip) DOMAIN=""; MODE="ip" ;;
  *)            DOMAIN="$DOMAIN_ARG"; MODE="domain" ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo / root 运行本脚本" >&2
  exit 1
fi

# 以调用 sudo 的普通用户来跑服务（比 root 更安全）；没有就用 root。
RUN_USER="${SUDO_USER:-root}"
if [ "$RUN_USER" = "root" ]; then
  echo "⚠️  当前以 root 直接运行（非 sudo），服务将以 root 身份运行。推荐用 sudo 执行以保留你的普通用户。"
fi

echo "==> 模式：$([ "$MODE" = ip ] && echo "纯 IP（无域名）" || echo "域名 $DOMAIN")"
echo "==> 应用目录：$APP_DIR　端口：$PORT　运行用户：$RUN_USER"

echo "==> [1/6] 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
PKGS="python3 python3-venv python3-pip ca-certificates curl nginx"
[ "$MODE" = domain ] && PKGS="$PKGS certbot python3-certbot-nginx"
[ -n "$AUTH_USER" ] && PKGS="$PKGS apache2-utils"    # htpasswd
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $PKGS

echo "==> [2/6] 准备应用目录 $APP_DIR"
mkdir -p "$APP_DIR"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

if [ ! -f "$APP_DIR/requirements.txt" ]; then
  echo "⚠️  $APP_DIR/requirements.txt 不存在 —— 请先用本地 sync_from_local.sh 推送代码，再重跑本脚本。" >&2
  echo "    （依赖安装 / 服务启动都需要代码已就位。）" >&2
  exit 2
fi

echo "==> [3/6] 创建 venv 并安装 Python 依赖（ortools 较大，耐心等）"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> [4/6] 注册 systemd 服务 $SERVICE（开机自启 + 崩溃自动重启）"
cat > "/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=TripAgent Web UI (Python)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PORT=$PORT
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
Environment=TRIPPLANNER_HOME=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python webui/server.py
Restart=always
RestartSec=3
User=$RUN_USER
Group=$RUN_USER
StandardOutput=append:$APP_DIR/service.log
StandardError=append:$APP_DIR/service.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

echo "==> [5/6] 配置 nginx 反向代理（80 → 127.0.0.1:$PORT）"

# ---- HTTP Basic 认证（可选）----
# 服务背后是真实 LLM key（会花钱）⇒ 面向公网时建议开启。
AUTH_BLOCK=""
if [ -n "$AUTH_USER" ]; then
  if [ -n "$AUTH_PASS" ]; then
    printf '%s' "$AUTH_PASS" | htpasswd -i -c "$HTPASSWD" "$AUTH_USER"
    AUTH_PASS_FINAL="$AUTH_PASS"
  elif [ -f "$HTPASSWD" ]; then
    echo "    → htpasswd 已存在，保留原口令（本次未显式给 BASIC_AUTH_PASS）"
  else
    AUTH_PASS_FINAL="$(openssl rand -hex 12)"
    printf '%s' "$AUTH_PASS_FINAL" | htpasswd -i -c "$HTPASSWD" "$AUTH_USER"
    AUTH_PASS_GENERATED="1"
  fi
  chown root:www-data "$HTPASSWD" 2>/dev/null || true
  chmod 640 "$HTPASSWD"
  AUTH_ENABLED="1"
  # 🔴 OPTIONS 必须豁免：面板的远端请求带 Authorization（非简单请求）⇒ 浏览器先发
  #    CORS 预检 OPTIONS；若预检也被 401，请求**根本发不出去**（表现为「线上后端不可达」）。
  #    nginx 文档明确 limit_except 里可用 auth_basic —— 只对「非 OPTIONS」生效。
  AUTH_BLOCK="
    # HTTP Basic 认证（OPTIONS = 浏览器 CORS 预检，必须豁免）
    limit_except OPTIONS {
        auth_basic \"TripAgent\";
        auth_basic_user_file $HTPASSWD;
    }
"
  echo "    → 已启用 HTTP Basic 认证（用户：$AUTH_USER）"
fi

if [ "$MODE" = domain ]; then
  SRVNAME="$DOMAIN"; DEFSRV=""
else
  SRVNAME="_"; DEFSRV=" default_server"
fi
cat > "/etc/nginx/sites-available/${SERVICE}" <<NGINX
server {
    listen 80${DEFSRV};
    listen [::]:80${DEFSRV};
    server_name ${SRVNAME};

    # 规划请求可能耗时十几秒，超时给足
    proxy_connect_timeout 60s;
    proxy_send_timeout    300s;
    proxy_read_timeout    300s;
    client_max_body_size  16m;

    location / {${AUTH_BLOCK}
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
ln -sf "/etc/nginx/sites-available/${SERVICE}" "/etc/nginx/sites-enabled/${SERVICE}"
[ -e /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# 若本机 ufw 处于启用状态，放行所需端口（云厂商安全组/防火墙仍需在控制台单独放行）
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp || true
  [ "$MODE" = domain ] && ufw allow 443/tcp || true
  [ "$MODE" = ip ] && ufw allow "$PORT"/tcp || true
  echo "    → 已放行 ufw 端口 80$([ "$MODE" = domain ] && echo " / 443")$([ "$MODE" = ip ] && echo " / $PORT")"
fi

echo "==> [6/6] 申请 HTTPS 证书"
if [ "$MODE" = domain ]; then
  if [ -n "$EMAIL" ]; then
    CERTBOT_ARGS=(-m "$EMAIL")
  else
    echo "ℹ️  未提供 certbot 邮箱 → 用 --register-unsafely-without-email（缺点：收不到到期提醒）。"
    CERTBOT_ARGS=(--register-unsafely-without-email)
  fi
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos "${CERTBOT_ARGS[@]}" --redirect || {
    echo "⚠️  certbot 失败（常见原因：域名未解析到本机 / 80 端口未放行 / 申请频率限制）。" >&2
    echo "    修好后手动重试： certbot --nginx -d $DOMAIN --redirect" >&2
  }
else
  echo "ℹ️  纯 IP 模式：跳过 HTTPS（IP 无法签发公信证书；需要 HTTPS 时请改用域名重跑）。"
fi

echo
echo "==> 本地自检"
sleep 2
code=$(curl -s -o /tmp/_ta_health.json -w '%{http_code}' "http://127.0.0.1:$PORT/api/health" || true)
echo "  python(127.0.0.1:$PORT/api/health) -> HTTP $code"
[ -f /tmp/_ta_health.json ] && cat /tmp/_ta_health.json && echo
if [ -n "$AUTH_ENABLED" ]; then
  AUTHOPT=(-u "$AUTH_USER:$AUTH_PASS_FINAL")
  code80=$(curl -s -o /dev/null -w '%{http_code}' "${AUTHOPT[@]}" "http://127.0.0.1/api/health" || true)
  echo "  nginx (带凭据 /api/health)         -> HTTP $code80"
  code401=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1/api/health" || true)
  echo "  nginx (无凭据 /api/health)         -> HTTP $code401（应为 401）"
  codeopt=$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS "http://127.0.0.1/api/health" || true)
  echo "  nginx (OPTIONS 预检，免鉴权)       -> HTTP $codeopt（应为 204）"
  if [ "$MODE" = domain ]; then
    codehttps=$(curl -s -o /dev/null -w '%{http_code}' "${AUTHOPT[@]}" "https://$DOMAIN/api/health" || true)
    echo "  nginx (HTTPS 带凭据)               -> HTTP $codehttps"
  fi
else
  code80=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1/api/health" || true)
  echo "  nginx (127.0.0.1:80/api/health)    -> HTTP $code80"
fi
systemctl --no-pager --full status "$SERVICE" | head -6 || true

echo
if [ "$MODE" = domain ]; then
  echo "✅ 完成。外部访问： https://$DOMAIN/"
else
  PUBIP="$(curl -s -m 5 https://api.ipify.org || true)"
  echo "✅ 完成。外部访问： http://${PUBIP:-<公网IP>}/（80）　或　http://${PUBIP:-<公网IP>}:$PORT/（3000）"
fi
if [ -n "$AUTH_ENABLED" ]; then
  SCHEME="http"; [ "$MODE" = domain ] && SCHEME="https"
  echo
  echo "🔑 HTTP Basic 认证已启用"
  echo "   用户名： $AUTH_USER"
  if [ -n "$AUTH_PASS_FINAL" ]; then
    echo "   密码　： $AUTH_PASS_FINAL"
  else
    echo "   密码　： （沿用已有 htpasswd 中的原口令）"
  fi
  echo "   面板要能访问线上后端，需在本地 secrets.json 写："
  echo "     \"remote_basic_auth\": \"$AUTH_USER:<上面的密码>\""
  echo "   curl 自测： curl -u '$AUTH_USER:<密码>' $SCHEME://<域名或IP>/api/health"
fi
echo
if [ "$MODE" = domain ]; then
  echo "   ⚠️ 必须在云厂商控制台【防火墙/安全组】放行入站 TCP 80 与 **443**（申请证书用 80，HTTPS 用 443）。"
else
  echo "   ⚠️ 必须在云厂商控制台【防火墙/安全组】放行入站 TCP 80 与 $PORT（这一步脚本做不了）。"
fi
echo "   看日志： journalctl -u $SERVICE -f    或    tail -f $APP_DIR/service.log"
echo "   重启：   sudo systemctl restart $SERVICE"
