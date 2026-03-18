#!/bin/bash
# Automated Sony Bridge Installation Script for Ubuntu VM

set -e

REPO_URL="https://github.com/Fireflaker/Sony-Camera-Image-Edge-REPLACEMENT.git"
INSTALL_DIR="/opt/sonybridge"
SYSTEMD_DIR="/etc/systemd/system"
VENV_DIR="${INSTALL_DIR}/.venv"

echo "=== Sony Bridge Installation ==="
echo "Target: $INSTALL_DIR"
echo ""

# Step 1: System dependencies
echo "[1/5] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
  python3 \
  python3-pip \
  python3-venv \
  ffmpeg \
  git \
  curl \
  networkmanager \
  >/dev/null 2>&1

# Step 2: Clone repository
echo "[2/5] Cloning Sony bridge repository..."
if [ -d "$INSTALL_DIR" ]; then
  echo "  (Updating existing installation)"
  cd "$INSTALL_DIR"
  git pull -q
else
  git clone -q "$REPO_URL" "$INSTALL_DIR"
fi

# Step 3: Create virtual environment
echo "[3/5] Creating Python virtual environment..."
cd "$INSTALL_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# Install Python dependencies
echo "  Installing Python packages..."
if [ -f "requirements.txt" ]; then
  pip install -q -r requirements.txt
else
  pip install -q requests
fi

# Step 4: Create systemd service
echo "[4/5] Creating systemd service..."
cat > "${SYSTEMD_DIR}/imagingedge-liveview.service" << 'EOF'
[Unit]
Description=Sony Camera ImagingEdge Liveview Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sonybridge/ImagingEdge4Linux
ExecStart=/opt/sonybridge/.venv/bin/python /opt/sonybridge/ImagingEdge4Linux/liveview_webui.py \
  --address 192.168.122.1 \
  --camera-port 10000 \
  --wifi-interface wlx00c0caa60b48 \
  --listen 0.0.0.0 \
  --port 8765

Restart=on-failure
RestartSec=10

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SYSTEMD_DIR}/imagingedge-liveview.service"
systemctl daemon-reload

# Step 5: Configure and start service
echo "[5/5] Starting bridge service..."
systemctl enable imagingedge-liveview.service
systemctl start imagingedge-liveview.service

# Verify
sleep 2
if systemctl is-active --quiet imagingedge-liveview; then
  echo "✓ Bridge service is running"
else
  echo "✗ Bridge service failed to start"
  systemctl status imagingedge-liveview
  exit 1
fi

echo ""
echo "=== Installation Complete ==="
echo "Service: imagingedge-liveview"
echo "Status: $(systemctl is-active imagingedge-liveview)"
echo "Logs: journalctl -u imagingedge-liveview -f"
echo "Health: curl http://127.0.0.1:8765/api/status"
echo ""
echo "Configure Frigate to use: http://<vm-host-ip>:8765/stream"
