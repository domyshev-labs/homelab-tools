#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
[[ $EUID -eq 0 ]] || { echo 'Run as root on Proxmox'; exit 1; }
[[ -x /usr/bin/alloy ]] || { echo 'Install the Grafana Alloy Debian package first'; exit 1; }
id alloy >/dev/null
if systemctl is-active --quiet alloy-tank-window.service || systemctl is-enabled --quiet alloy-tank-window.service; then
    echo 'Disable the legacy collector first: systemctl disable --now alloy-tank-window.service'
    exit 1
fi
# Keep any existing default Alloy configuration intact.
if [[ -e /etc/alloy-zfs-disk-standby-scheduler/config.alloy ]]; then
    echo 'Existing zfs-disk-standby-scheduler collector found; back up and review before updating.'
    exit 1
fi
for unit in zfs-disk-standby-scheduler.service zfs-disk-standby-scheduler-on.service zfs-disk-standby-scheduler-off.service; do
    if [[ $(systemctl show -p SyslogIdentifier --value "$unit") != zfs-disk-standby-scheduler ]]; then
        echo "Install the repository version of $unit with SyslogIdentifier=zfs-disk-standby-scheduler first"
        exit 1
    fi
done
[[ -f zfs-disk-standby-scheduler.alloy ]] || {
    echo 'Copy zfs-disk-standby-scheduler.example.alloy to zfs-disk-standby-scheduler.alloy and configure your Loki endpoint first'
    exit 1
}
if grep -q 'LOKI_HOST' zfs-disk-standby-scheduler.alloy; then
    echo 'Replace LOKI_HOST before installation'; exit 1
fi
/usr/bin/alloy validate zfs-disk-standby-scheduler.alloy
install -d -o root -g alloy -m 0750 /etc/alloy-zfs-disk-standby-scheduler
install -o root -g alloy -m 0640 zfs-disk-standby-scheduler.alloy /etc/alloy-zfs-disk-standby-scheduler/config.alloy
install -m 0644 alloy-zfs-disk-standby-scheduler.service /etc/systemd/system/alloy-zfs-disk-standby-scheduler.service
systemctl daemon-reload
systemctl enable --now alloy-zfs-disk-standby-scheduler.service
systemctl is-active alloy-zfs-disk-standby-scheduler.service
echo 'Collector started; only SYSLOG_IDENTIFIER=zfs-disk-standby-scheduler is selected.'
