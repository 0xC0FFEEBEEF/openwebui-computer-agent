#!/bin/bash
# Installs / updates the AI sandbox desktop on this VM. Idempotent; run as root
# from the directory holding the files. Tokens are generated once and kept.
set -euo pipefail
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive

if ! id agent >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash --comment "AI sandbox desktop" agent
fi

if [ ! -f /etc/apt/sources.list.d/google-chrome.list ]; then
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list
fi
apt-get update -q
apt-get install -y -q --no-install-recommends \
    tigervnc-standalone-server x11-xserver-utils x11-utils xdotool dbus-x11 \
    xfce4-session xfwm4 xfce4-panel xfdesktop4 xfce4-settings xfce4-terminal thunar \
    xfce4-notifyd adwaita-icon-theme-full fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji \
    google-chrome-stable novnc python3-venv nftables openbox

install -d -o root -g root -m 0755 /opt/sandbox-agent
install -m 0644 sandbox_agent.py elements.js viewer.html /opt/sandbox-agent/
[ -x /opt/sandbox-agent/venv/bin/python ] || python3 -m venv /opt/sandbox-agent/venv
/opt/sandbox-agent/venv/bin/pip install -q --upgrade pip
/opt/sandbox-agent/venv/bin/pip install -q "fastapi>=0.110" "uvicorn[standard]" httpx websockets pillow numpy scipy

install -d -o root -g agent -m 0750 /etc/sandbox-agent
if [ ! -f /etc/sandbox-agent/env ]; then
    umask 027
    printf 'CONTROL_TOKEN=%s\nVIEW_TOKEN=%s\n' "$(openssl rand -hex 24)" "$(openssl rand -hex 16)" > /etc/sandbox-agent/env
    chgrp agent /etc/sandbox-agent/env
fi
install -m 0644 sandbox-egress.nft /etc/sandbox-agent/egress.nft

cat > /etc/systemd/system/sandbox-egress.service <<'EOF'
[Unit]
Description=AI sandbox - block the agent user from the LAN
Before=sandbox-desktop.service
After=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/sandbox-agent/egress.nft
ExecStop=/usr/sbin/nft delete table inet sandbox_egress

[Install]
WantedBy=multi-user.target
EOF
install -m 0644 sandbox-desktop.service sandbox-session.service sandbox-agent.service /etc/systemd/system/

# Chrome is the default browser, no screensaver or lock screen on the kiosk desktop.
sudo -u agent mkdir -p ~agent/.config/xfce4/xfconf/xfce-perchannel-xml
sudo -u agent xdg-settings set default-web-browser google-chrome.desktop 2>/dev/null || true
cat > ~agent/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-screensaver.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-screensaver" version="1.0">
  <property name="saver" type="empty"><property name="enabled" type="bool" value="false"/></property>
  <property name="lock" type="empty"><property name="enabled" type="bool" value="false"/></property>
</channel>
EOF
chown -R agent:agent ~agent/.config

systemctl daemon-reload
systemctl enable --now sandbox-egress.service sandbox-desktop.service sandbox-session.service
systemctl enable sandbox-agent.service
systemctl restart sandbox-agent.service
echo "installed"
