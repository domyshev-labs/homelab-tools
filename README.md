# Homelab tools

Scripts for home servers. Each tool has its own configuration and installation
instructions. This project does not deploy the Grafana/Loki server stack.

## ZFS disk standby scheduler

[zfs-disk-standby-scheduler](zfs-disk-standby-scheduler/README.md) imports a ZFS pool
and puts its disk into standby after exporting the pool, according to a schedule.
Useful for noisy home-server disks, occasional data access, maintenance and backups.
The current implementation requires Linux with Proxmox VE; support for other
ZFS hosts can be added later. The tool name does not depend on a pool name.

- Stable whole-disk `/dev/disk/by-id` path; no fixed `/dev/sdX` names.
- Separate opening, closing and retry timers; backup schedules stay independent.
- Shutdown is deferred while backups, scrub/resilver or coordinated tasks run.
- JSON logs in journald; optional [Alloy collector](observability/README.md).

Only configuration examples are tracked. Copy the example files and fill in
your disk ID, pool, storage names, paths and Loki endpoint. Local deployment
configurations are ignored by Git. Existing `/etc/zfs-disk-standby-scheduler.json` is preserved
on reinstall.

The installer generates both daily timers from the config, validates the disk
and storage settings, and saves the previous deployment for rollback. Use
`--config PATH` to explicitly change configuration; normal updates preserve it.
See the tool README for the parameter table, update and rollback instructions.

From the repository root on Proxmox, as root:

```bash
cp zfs-disk-standby-scheduler/zfs-disk-standby-scheduler.example.json zfs-disk-standby-scheduler/zfs-disk-standby-scheduler.json
# Edit the local configuration before installing.
nano zfs-disk-standby-scheduler/zfs-disk-standby-scheduler.json
bash zfs-disk-standby-scheduler/install.sh
zfs-disk-standby-scheduler status
```

Read the tool instructions before enabling timers. For optional centralized
logs, install the Alloy package and follow `observability/README.md`.

## Local verification

```bash
python3 -B -m unittest discover -s tests -v
bash -n zfs-disk-standby-scheduler/install.sh observability/agents/install.sh
```

Repository: https://github.com/domyshev-labs/homelab-tools
