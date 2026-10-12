# Collect homelab tool logs with Grafana Alloy

This optional collector sends selected tool logs to an existing Loki server.
Grafana and Loki are deployed separately; this repository does not deploy them.
Proxmox backup task logs can be added separately using the opt-in setup below.

Before installation, set the Loki push URL and host label in your local `agents/zfs-disk-standby-scheduler.alloy`
for your environment. Copy `agents/zfs-disk-standby-scheduler.example.alloy` to `agents/zfs-disk-standby-scheduler.alloy` and replace
`LOKI_HOST:3100` with your Loki address. The local config is ignored by Git.

## Manual setup on Proxmox

If the old `alloy-tank-window.service` is installed, disable it before installing
the new collector, to release its diagnostics port:

```bash
systemctl disable --now alloy-tank-window.service
```

The old collector files are kept. New logs use
`service="zfs-disk-standby-scheduler"`; existing Loki history keeps its previous
service label. Update saved Grafana queries to the new label.

Run the installation commands manually on the Proxmox host as root.

### 1. Copy this repository to Proxmox

Clone the repository if it is accessible there, or copy the `zfs-disk-standby-scheduler`
and `observability/agents` directories. From the repository root, as root:

```bash
cp zfs-disk-standby-scheduler/zfs-disk-standby-scheduler.example.json zfs-disk-standby-scheduler/zfs-disk-standby-scheduler.json
nano zfs-disk-standby-scheduler/zfs-disk-standby-scheduler.json
bash zfs-disk-standby-scheduler/install.sh
zfs-disk-standby-scheduler status
```

If an earlier version is already installed, stop its three timers and wait for
all three related services to finish before running the installer. Existing
`/etc/zfs-disk-standby-scheduler.json` is retained. The installer does not activate the disk timers.

```bash
systemctl disable --now zfs-disk-standby-scheduler.timer zfs-disk-standby-scheduler-on.timer zfs-disk-standby-scheduler-off.timer
systemctl is-active zfs-disk-standby-scheduler.service zfs-disk-standby-scheduler-on.service zfs-disk-standby-scheduler-off.service
```

After installation, verify the server's local timezone with `timedatectl`.
The intended disk window is 02:00–05:00. Backups remain a separate Proxmox job.

### 2. Install Grafana Alloy

Alloy is the collector: it reads the selected journal entries and forwards them
to Loki. It does not control the disk. Official installation instructions:
https://grafana.com/docs/alloy/latest/set-up/install/linux/

For Debian / Proxmox, as root:

```bash
apt-get update
apt-get install -y ca-certificates curl gpg
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://apt.grafana.com/gpg-full.key -o /tmp/grafana-signing-key.asc
gpg --dearmor --yes --output /etc/apt/keyrings/grafana.gpg /tmp/grafana-signing-key.asc
echo 'deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main' > /etc/apt/sources.list.d/grafana.list
apt-get update
apt-get install -y alloy
```

Use an existing Grafana apt source if already configured rather than adding a
duplicate. This setup leaves `/etc/alloy/config.alloy` intact and runs the
script collector as its own `alloy-zfs-disk-standby-scheduler.service`.

### 3. Start the collector

```bash
cp observability/agents/zfs-disk-standby-scheduler.example.alloy observability/agents/zfs-disk-standby-scheduler.alloy
nano observability/agents/zfs-disk-standby-scheduler.alloy
# Replace LOKI_HOST:3100 with your server address:
curl -fsS http://LOKI_HOST:3100/ready
bash observability/agents/install.sh
```

The agent listens for its own local diagnostics on `127.0.0.1:12346`.
It selects **only** `SYSLOG_IDENTIFIER=zfs-disk-standby-scheduler`. The three script units all
set this identifier. It retains the unit label and extracts JSON `level`.
Its WAL and source position are stored in `/var/lib/alloy-zfs-disk-standby-scheduler`, on the
Proxmox system disk, independently of the sleeping backup disk.
Network outages trigger retries; the WAL adds durability but does not replace
the local journal or guarantee unlimited buffering.

### 4. Test without changing disk state

```bash
systemd-cat --identifier=zfs-disk-standby-scheduler /usr/local/sbin/zfs-disk-standby-scheduler status
systemctl is-active alloy-zfs-disk-standby-scheduler.service
```

This runs only the script's status command and sends its output through the
selected journal identifier. Check Grafana for `service="zfs-disk-standby-scheduler"`.
If needed, inspect the agent locally:

```bash
journalctl -u alloy-zfs-disk-standby-scheduler.service -n 40 --no-pager
```

After confirming logs and finishing backup configuration, activate the disk timers:

```bash
systemctl enable --now zfs-disk-standby-scheduler.timer zfs-disk-standby-scheduler-on.timer zfs-disk-standby-scheduler-off.timer
systemctl list-timers 'zfs-disk-standby-scheduler*'
```

Activating the timers during the day does not shut the disk down immediately.

## Optional: collect Proxmox backup logs

For an already installed collector, run from the repository root on Proxmox:

```bash
git pull --ff-only
apt-get install -y acl
bash observability/agents/enable-proxmox-backups.sh
systemctl is-active alloy-zfs-disk-standby-scheduler.service
journalctl -u alloy-zfs-disk-standby-scheduler.service -n 40 --no-pager
```

The helper preserves the installed Loki endpoint and disk script configuration.
It validates the combined config with the installed Alloy before changing it,
saves the previous config and task log ACLs under
`/var/backups/alloy-zfs-disk-standby-scheduler`, then restarts only this collector.
Running the helper again leaves an already configured backup source unchanged.
The default Alloy service and disk scheduler timers are not changed.

The source is `/var/log/pve/tasks/*/UPID:*:vzdump:*`, on the system disk.
These are the task logs shown in the Proxmox Tasks interface: start, progress,
warnings and final `TASK OK` or `TASK ERROR`. Other task types, the complete
journal and guest logs are not forwarded. No backup is started by the helper.
The backup disk is not read to collect logs.

Proxmox task logs may be owned by `www-data` with mode `0640`. The helper adds
read/traverse ACLs for `alloy` on task directories and existing vzdump files.
Directory default ACLs also allow Alloy to read future task logs of other types;
the configured file glob forwards only vzdump. Alloy continues to run as its
unprivileged user. To restore the previous configuration and ACLs, use the
backup directory printed by the helper:

```bash
cp -p /var/backups/alloy-zfs-disk-standby-scheduler/BEFORE_DIRECTORY/config.alloy /etc/alloy-zfs-disk-standby-scheduler/config.alloy
setfacl --restore=/var/backups/alloy-zfs-disk-standby-scheduler/BEFORE_DIRECTORY/task-log-acls
systemctl restart alloy-zfs-disk-standby-scheduler.service
```

ACL restoration covers paths present in the snapshot; files created afterwards
retain inherited ACLs. Review those separately if removing all collector access.

Files changed in the last 24 hours are discovered every 10 seconds and read
from the beginning on first discovery. Existing recent logs may therefore
appear immediately. Positions remain in the collector's existing state
directory. Timestamps represent collection time, including for old logs read
on first setup; the original text retains any timestamps printed by vzdump.

In Grafana Explore use:

```logql
{host="proxmox",service="proxmox-backup"} | unpack
```

Each record carries the task filename in its JSON body, with the original line
in `_entry`. `unpack` displays the original line and makes the filename available
at query time without creating a separate indexed stream for every task.
For failures use `{host="proxmox",service="proxmox-backup",level="error"} | unpack`.
Start a backup only when its intended storage is mounted and active, then verify
its progress and final result in Grafana. No synthetic task files are needed.

## Optional: collect Proxmox host configuration backups

After installing the configuration backup service, from `~/homelab-tools` as root:

```bash
bash observability/agents/enable-pve-config-backup.sh
systemctl is-active alloy-zfs-disk-standby-scheduler.service
```

The helper appends `agents/pve-config-backup.alloy` to the installed collector,
reusing `loki.write.central` and preserving existing disk scheduler and vzdump
sources and the Loki endpoint. It validates the candidate with installed Alloy,
saves the previous configuration under
`/var/backups/alloy-zfs-disk-standby-scheduler/before-config-backup-*`, and restores
it on restart failure. Repeated installation does not append a second source.
It requires the `alloy` user to belong to `adm` and `systemd-journal`, as required
by [Alloy journal source documentation](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.journal/).
If missing, run `usermod -aG adm,systemd-journal alloy` and repeat the helper.

Only `SYSLOG_IDENTIFIER=pve-config-backup` is read. JSON `level` becomes a label;
raw tar/zstd diagnostic lines remain visible without a level label. Journal
history starts with up to 24 hours, using the collector's existing state storage.
No archive is read and no backup or disk operation is started by this helper.

```logql
{host="proxmox",service="pve-config-backup"}
{host="proxmox",service="pve-config-backup",level="error"}
```

For a test independent of backup storage:

```bash
printf '%s\n' '{"service":"pve-config-backup","level":"info","event":"collector_test","message":"Configuration backup log collection test"}' | systemd-cat --identifier=pve-config-backup
```

To roll back, copy `config.alloy` from the exact snapshot printed by the helper
back to `/etc/alloy-zfs-disk-standby-scheduler/config.alloy` and restart
`alloy-zfs-disk-standby-scheduler.service`. Group memberships are not modified
by the helper. See [backup instructions](../proxmox/pve-config-backup/README.md)
for installation, scheduling and storage checks.
