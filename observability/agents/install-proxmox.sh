#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
[[ $EUID -eq 0 ]] || { echo 'Run as root on Proxmox'; exit 1; }
[[ -x /usr/bin/alloy ]] || { echo 'Install the Grafana Alloy Debian package first'; exit 1; }
id alloy >/dev/null
# Keep any existing default Alloy configuration intact.
if [[ -e /etc/alloy-tank-window/config.alloy ]]; then
    echo 'Existing tank-window collector found; back up and review before updating.'
    exit 1
fi
for unit in tank-window.service tank-window-on.service tank-window-off.service; do
    if [[ $(systemctl show -p SyslogIdentifier --value "$unit") != tank-window ]]; then
        echo "Install the repository version of $unit with SyslogIdentifier=tank-window first"
        exit 1
    fi
done
[[ -f proxmox.alloy ]] || {
    echo 'Copy proxmox.example.alloy to proxmox.alloy and configure your Loki endpoint first'
    exit 1
}
if grep -q 'LOKI_HOST' proxmox.alloy; then
    echo 'Replace LOKI_HOST before installation'; exit 1
fi
/usr/bin/alloy validate proxmox.alloy
install -d -o root -g alloy -m 0750 /etc/alloy-tank-window
install -o root -g alloy -m 0640 proxmox.alloy /etc/alloy-tank-window/config.alloy
install -m 0644 alloy-tank-window.service /etc/systemd/system/alloy-tank-window.service
systemctl daemon-reload
systemctl enable --now alloy-tank-window.service
systemctl is-active alloy-tank-window.service
echo 'Collector started; only SYSLOG_IDENTIFIER=tank-window is selected.'
