#!/bin/bash
# Add only the configuration backup journal to the existing collector.
set -Eeuo pipefail
cd -- "$(dirname -- "$0")"
[[ $EUID -eq 0 ]] || { echo 'Run as root on Proxmox'; exit 1; }
[[ -x /usr/bin/alloy ]] || { echo 'Install Alloy first'; exit 1; }
id alloy >/dev/null
config=/etc/alloy-zfs-disk-standby-scheduler/config.alloy
service=alloy-zfs-disk-standby-scheduler.service
[[ -f $config ]] || { echo 'Install the existing disk scheduler collector first'; exit 1; }
# Journal reads require both groups; the collector continues to run as alloy.
id -nG alloy | tr ' ' '\n' | grep -qx adm || { echo 'Add journal access: usermod -aG adm,systemd-journal alloy'; exit 1; }
id -nG alloy | tr ' ' '\n' | grep -qx systemd-journal || { echo 'Add journal access: usermod -aG adm,systemd-journal alloy'; exit 1; }
if grep -Eq 'loki\.(source\.journal|process)[[:space:]]+"pve_config_backup"' "$config"; then
  echo 'Configuration backup collection already configured; no changes made.'
  exit 0
fi
umask 077
candidate=$(mktemp)
trap 'rm -f -- "$candidate"' EXIT
cat "$config" > "$candidate"
printf '\n' >> "$candidate"
cat pve-config-backup.alloy >> "$candidate"
/usr/bin/alloy validate "$candidate"
install -d -m 0700 /var/backups/alloy-zfs-disk-standby-scheduler
backup=$(mktemp -d /var/backups/alloy-zfs-disk-standby-scheduler/before-config-backup-XXXXXX)
cp -p -- "$config" "$backup/config.alloy"
echo "Previous collector config saved: $backup"
rollback() {
  status=$?
  trap - ERR
  set +e
  cp -p -- "$backup/config.alloy" "$config"
  systemctl restart "$service"
  echo "Setup failed; restored previous configuration. Snapshot: $backup"
  exit "$status"
}
trap rollback ERR
install -o root -g alloy -m 0640 "$candidate" "$config"
systemctl restart "$service"
systemctl is-active --quiet "$service"
trap - ERR
echo 'Collector started: existing sources + configuration backup journal.'
echo 'Grafana query: {host="proxmox",service="pve-config-backup"}'
