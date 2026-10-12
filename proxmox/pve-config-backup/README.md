# Proxmox host configuration backup

Archives host configuration independently of VM/CT disks. Run as root on Proxmox.
Needs Bash, Python 3, GNU tar, zstd, ZFS, findmnt, flock, find, readlink and systemd.

## Configuration

Provide a JSON file matching `pve-config-backup.example.json`. No personal dataset,
path or schedule is embedded in the script. The default config location is
`/etc/pve-config-backup.json`; `--config FILE` selects another file.

| Field | Meaning |
| --- | --- |
| `backup_dir` | Exact absolute mountpoint of the destination dataset |
| `backup_dataset` | Expected ZFS dataset, verified before writing |
| `retention_days` | Remove this host's older archives after a successful backup |
| `compression_level` | zstd level, integer 1–19 |
| `schedule` | Daily HH:MM or weekday + HH:MM, e.g. Mon 02:20, in the server timezone |
| `randomized_delay_seconds` | Additional random timer delay |
| `persistent` | Catch up missed timer events; use false with a sleeping pool |

Validation and unit rendering require no root and do not access storage:

```bash
python3 proxmox/pve-config-backup/config.py validate /path/to/config.json
python3 proxmox/pve-config-backup/config.py units /path/to/config.json --checkout /path/to/homelab-tools --output /tmp/backup-units
```

## Installation and updates

From the repository root on Proxmox as root:

```bash
bash proxmox/pve-config-backup/install.sh --config /absolute/path/config.json
systemctl cat pve-config-backup.service pve-config-backup.timer
```

The installer saves prior config/units and the old standalone script under
`/var/backups/pve-config-backup`, stops the timer and refuses a running service.
Units execute the script from this checkout; `git pull` changes code for the next
run. Reinstall after changing configuration, units or checkout location. The
installer leaves the timer stopped. Existing `/usr/local/sbin/pve-config-backup`
is retained for recovery but is no longer used by the service.

With the intended dataset already mounted:

```bash
bash proxmox/pve-config-backup/pve-config-backup --config /etc/pve-config-backup.json --check
systemctl start pve-config-backup.service
journalctl -u pve-config-backup.service -n 60 --no-pager
systemctl enable --now pve-config-backup.timer
```

`--check` does not create an archive. Without the exact ZFS mounting, the script
fails and logs the reason. It never creates the destination or imports a pool.
Coordinate the timer with your disk availability window.

## Behaviour, logs and recovery

A shared `/run/lock/zfs-disk-standby-scheduler.lock` protects the entire run from
cooperating scheduler export. A separate lock prevents duplicate config backups.
Uncoordinated manual export does not honour this lock.

Archives contain `/etc`, live `config.db`, `/usr/local/sbin`, the executed script,
and `/root/.ssh` when present. They use ACLs and numeric owners, not xattrs.
A `.part` is verified with `zstd -t`, renamed and restricted to `0600`. Failures
remove the partial archive. Live SQLite copying is not a transactional snapshot;
compression verification does not prove recovery. Restore selectively, never over
a running Proxmox root filesystem. Archives contain secrets; store off-host copies.

JSON events include backup_started, backup_completed, backup_failed,
storage_unavailable, storage_ready and backup_busy. Systemd uses journal identifier
`pve-config-backup`; direct runs write to the terminal. Host/service labels and
Loki routing belong in your external Alloy configuration. Use a service or
systemd-cat to send manual output to journald.

For rollback, stop the timer, wait for the service, restore prior checkout/config/
units from the printed snapshot and daemon-reload. Review ExecStart and timer
catch-up behaviour before re-enabling; old deployments may use a standalone script.
