#!/bin/bash
set -e
echo "============================================"
echo "  随心一阅 云服务器一键部署"
echo "============================================"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_DIR="/opt/suixin-yiyue"
VENV_DIR="$APP_DIR/venv"
ENV_FILE="/etc/suixin-yiyue.env"

echo "[1/6] 安装系统依赖 (Python)..."
if command -v dnf &>/dev/null; then
    dnf install -y python3 python3-pip python3-devel 2>/dev/null || true
elif command -v apt &>/dev/null; then
    apt update -y && apt install -y python3 python3-pip python3-venv 2>/dev/null || true
fi

echo "[2/6] 创建应用目录..."
mkdir -p "$APP_DIR"
# Create dedicated user if not exists
if ! id -u suixin &>/dev/null; then
    useradd -r -s /bin/false suixin
fi
cp -r "$PROJECT_DIR/backend" "$APP_DIR/"
cp -r "$PROJECT_DIR/frontend-dist" "$APP_DIR/frontend-dist"
chown -R suixin:suixin "$APP_DIR"

echo "[3/6] 创建虚拟环境..."
python3 -m venv "$VENV_DIR"
chown -R suixin:suixin "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "[4/6] 安装 Python 依赖..."
pip install --upgrade pip -q
pip install -r "$PROJECT_DIR/backend/requirements-cloud.txt" -q

echo "[5/6] 配置 systemd 服务..."
if [ ! -f "$ENV_FILE" ]; then
    ADMIN_PASSWORD="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(18))
PY
)"
    JWT_SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
    cat > "$ENV_FILE" <<EOF
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
JWT_SECRET=$JWT_SECRET
EOF
    chmod 600 "$ENV_FILE"
fi
cp "$SCRIPT_DIR/suixin-yiyue.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable suixin-yiyue

echo "[6/6] 启动服务..."
systemctl restart suixin-yiyue

echo ""
echo "============================================"
echo "  部署完成!"
echo "  访问地址: http://$(hostname -I | awk '{print $1}'):8765"
echo "  管理员账号: admin"
echo "  管理员密码位置: $ENV_FILE"
echo "============================================"
echo ""
echo "常用命令:"
echo "  systemctl status suixin-yiyue   - 查看状态"
echo "  systemctl restart suixin-yiyue  - 重启服务"
echo "  journalctl -u suixin-yiyue -f   - 查看日志"
