#!/usr/bin/env bash
#
# Install the Auction Deal Scanner on a fresh Ubuntu 22.04 / 24.04 host
# (x86-64 or arm64, e.g. an Oracle Cloud Ampere A1 instance).
#
# Run it from the project directory:
#     bash deploy/install.sh
#
# Override defaults with environment variables, e.g.:
#     APP_DIR=/srv/leilao SERVICE_USER=me bash deploy/install.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/leilao}"
SERVICE_USER="${SERVICE_USER:-ubuntu}"
TZ_NAME="${TZ_NAME:-America/Sao_Paulo}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "run as a normal user with sudo, not as root"
[[ -f "$APP_DIR/app.py" ]] || die "app.py not found in $APP_DIR (set APP_DIR=/path/to/project)"
id "$SERVICE_USER" >/dev/null 2>&1 || die "user $SERVICE_USER does not exist"
command -v sudo >/dev/null || die "sudo is required"
command -v curl >/dev/null || die "curl is required"

say "Timezone -> $TZ_NAME"
sudo timedatectl set-timezone "$TZ_NAME"
date

say "System packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3-venv python3-pip curl ca-certificates gnupg

if command -v node >/dev/null 2>&1 && [[ "$(node -v | cut -c2-3)" -ge 20 ]]; then
  say "Node.js already installed ($(node -v))"
else
  say "Installing Node.js 22"
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
fi

say "Python virtual environment"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r deploy/requirements-server.txt

say "Chromium for Playwright (shared libraries + browser)"
sudo "$APP_DIR/.venv/bin/python" -m playwright install-deps chromium
"$APP_DIR/.venv/bin/python" -m playwright install chromium

say "WhatsApp bridge dependencies"
npm --prefix "$APP_DIR/agents/leilao/whatsapp_bridge" ci --omit=dev

say "Directories"
mkdir -p "$APP_DIR/data"
chmod 755 "$APP_DIR/data"

say "systemd services"
sudo install -m 0644 deploy/systemd/leilao-web.service /etc/systemd/system/leilao-web.service
sudo install -m 0644 deploy/systemd/leilao-bridge.service /etc/systemd/system/leilao-bridge.service
sudo sed -i "s|/opt/leilao|$APP_DIR|g" /etc/systemd/system/leilao-web.service /etc/systemd/system/leilao-bridge.service
if [[ "$SERVICE_USER" != "ubuntu" ]]; then
  sudo sed -i "s|User=ubuntu|User=$SERVICE_USER|g; s|Group=ubuntu|Group=$SERVICE_USER|g; s|HOME=/home/ubuntu|HOME=/home/$SERVICE_USER|g" \
    /etc/systemd/system/leilao-web.service /etc/systemd/system/leilao-bridge.service
fi
sudo systemctl daemon-reload
sudo systemctl enable leilao-bridge.service leilao-web.service >/dev/null
sudo systemctl restart leilao-bridge.service
sudo systemctl restart leilao-web.service

say "Waiting for the web server on 127.0.0.1:8000"
for _ in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8000/api/leilao/status >/dev/null 2>&1; then
    echo "ok - the scanner is running"
    break
  fi
  sleep 1
done

say "Service status"
sudo systemctl --no-pager --full status leilao-web.service | head -n 12 || true
sudo systemctl --no-pager --full status leilao-bridge.service | head -n 8 || true

cat <<EOF

Next steps (see deploy/README.md):
  1. Open the UI through an SSH tunnel from your laptop.
  2. Link WhatsApp (QR) in the WhatsApp digest card.
  3. Set the digest to 08:00 and enable it.
  4. Send a test digest.

Time on this server: $(date)
EOF
