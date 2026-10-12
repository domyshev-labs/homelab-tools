# ZFS disk standby scheduler

Imports a configured ZFS pool and requests export/standby on a daily schedule.
Currently Linux with Proxmox VE only. Standby stops disk rotation, not power.
Uses Python 3 standard library plus ZFS, Proxmox commands, hdparm and findmnt.

## External configuration

Use `zfs-disk-standby-scheduler.example.json` as a schema example. Keep the real
configuration outside this project. All fields are required; unknown fields,
duplicates and invalid types are rejected.

| Field | Meaning |
| --- | --- |
| `pool` | ZFS pool name, backed by one selected physical disk |
| `disk` | Stable whole-disk `/dev/disk/by-id/...`, never a partition or /dev/sdX |
| `backup_path` | Existing ZFS directory on this pool, verified when waking |
| `storages` | Proxmox storage IDs tied to the pool or exact directory |
| `start`, `end` | Different daily HH:MM values, server timezone; overnight supported |
| `samba_services` | Optional service list, e.g. smbd.service; [] disables management |

Read-only validation, without root or disk operations:

```bash
python3 zfs-disk-standby-scheduler/zfs-disk-standby-scheduler.py validate-config /path/to/config.json
```

## Install/update on Proxmox

Before installing, disable the three scheduler timers and wait for the associated
services to finish. Disable old `tank-window*` timers too when migrating.
The installer checks idle state, shared locks, legacy timers, storage and disk
settings. It refuses unsafe or incomplete deployments.

```bash
systemctl disable --now zfs-disk-standby-scheduler.timer zfs-disk-standby-scheduler-on.timer zfs-disk-standby-scheduler-off.timer
bash zfs-disk-standby-scheduler/install.sh --config /absolute/path/config.json
```

The script is installed in `/usr/local/sbin`, config in `/etc`, and daily timers
are generated from JSON. Normal reinstall without --config preserves installed
config. All previous files are saved under `/var/backups/zfs-disk-standby-scheduler`;
failed writes/reloads restore them. Timers remain stopped until you verify:

```bash
zfs-disk-standby-scheduler status
systemctl enable --now zfs-disk-standby-scheduler.timer zfs-disk-standby-scheduler-on.timer zfs-disk-standby-scheduler-off.timer
```

Daily on/off timers request operations; a minute timer retries pending requests.
Missed events are not replayed after reboot. Activating timers does not immediately
export a pool. Active vzdump, scrub/resilver, mismatched devices and failed checks
defer or reject shutdown. Export never uses -f. On export failure previous storage
and Samba state is restored if the pool is still imported. The scheduler never
stops guests and leaves pools busy with guest disks online.

Operations cooperate via exclusive `/run/lock/zfs-disk-standby-scheduler.lock`.
Other tasks should hold a shared lock for their entire run and verify mounting.
The config backup tool implements this. Manual operations that bypass locking,
monitoring or other disk access can wake the disk; silence is not guaranteed.

Status and journal logs:

```bash
zfs-disk-standby-scheduler status
journalctl -u zfs-disk-standby-scheduler.service -n 60 --no-pager
```

JSON event identifiers and journal `zfs-disk-standby-scheduler` are stable tool
interfaces. Grafana labels, routing and queries are external deployment settings.

Rollback with timers stopped and services idle:

```bash
bash zfs-disk-standby-scheduler/install.sh --rollback /var/backups/zfs-disk-standby-scheduler/EXACT_SNAPSHOT
```

Verify status and enable timers after rollback. For manual disk availability,
stop timers, wait for services, then use `zfs-disk-standby-scheduler on` or `off`;
these requests do not cancel future daily events. SMB services may be unavailable
while stopped; if guests keep the pool busy it remains online.
