#!/bin/bash
# One-off cleanup of the AI-Sandbox VM (ai-sandbox) before the agent desktop goes on it.
# Run as root:  sudo bash cleanup.sh
# Everything removed here is from the abandoned "Open Terminal" experiment (Mar 2026),
# which is not running and is not referenced by Open WebUI.
set -u

H=/home/owui
# ~400 MB: open_terminal and its pip --user packages, uv, jupyter, and the pip/uv caches
rm -rf "$H/.local/lib" "$H/.local/bin" "$H/.local/share/uv" "$H/.local/share/jupyter" "$H/.local/state" \
       "$H/.cache/pip" "$H/.cache/uv" "$H/venvs" \
       "$H/systemd" "$H/.systemd-services" "$H/openterminal-daemon.sh" "$H/.openterminal-startup" \
       "$H/openterminal.log" "$H/.config/open-terminal" "$H/.config/uv" "$H/.config/fish" "$H/~"
# (the start scripts deleted above had an API key hard-coded in them)

# The extra key ssh-copy-id added by mistake (id_ubuntu_core.pub from the desktop).
# Pass its base64 part as $1 to remove it:  sudo bash cleanup.sh "$(awk '{print $2}' ~/.ssh/id_ubuntu_core.pub)"
if [ -n "${1:-}" ]; then
    grep -vF "$1" "$H/.ssh/authorized_keys" > "$H/.ssh/ak.new" && mv "$H/.ssh/ak.new" "$H/.ssh/authorized_keys"
    chown owui:owui "$H/.ssh/authorized_keys"; chmod 600 "$H/.ssh/authorized_keys"
fi

# mcp_admin: sudo-capable account with someone's SSH key, empty home, never logged in.
userdel -r mcp_admin 2>/dev/null; rm -f /etc/sudoers.d/mcp_admin

# LXD snap: never used
snap remove --purge lxd

# Services with nothing to do in a VM (no modem, no SAN/multipath, no firmware updates, no removable disks, no battery)
systemctl disable --now ModemManager.service multipathd.service multipathd.socket fwupd.service udisks2.service upower.service 2>/dev/null

apt-get -y autoremove --purge
echo "cleanup done"; du -sh "$H"; df -h / | tail -1
