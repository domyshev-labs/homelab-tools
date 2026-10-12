# External log collection configuration

Optional Grafana Alloy helpers. They do not deploy Grafana/Loki or choose your
endpoint, host labels, service labels or Grafana queries. Supply these through
complete Alloy config or source fragments in your deployment repository.
The example file has placeholders and must be configured before installation.

Install Grafana Alloy using its official Debian instructions. From this repository
on Proxmox as root:

```bash
bash observability/agents/install.sh /absolute/path/collector.alloy
```

Existing collector config is never overwritten by this initial installer.
The separate `alloy-zfs-disk-standby-scheduler.service` uses an unprivileged user,
its own config and state on the system disk, and diagnostics on loopback.
The default Alloy service is left alone. Journal sources need access via
`adm` and `systemd-journal`; the unit supplies these supplementary groups.

Extend an installed collector with explicit fragments:

```bash
bash observability/agents/enable-proxmox-backups.sh /absolute/path/vzdump-fragment.alloy
bash observability/agents/enable-pve-config-backup.sh /absolute/path/config-backup-fragment.alloy
```

Fragments must use component names `proxmox_backups` and `pve_config_backup`
respectively and reference the installed `loki.write.central`. Helpers validate
candidates, preserve existing endpoint/sources, save previous config, and restore
on restart failure. They do not start backups or operate on backup storage.
Config backup logs use journal identifier `pve-config-backup`.

The vzdump helper needs the acl package and adds alloy read/traverse ACLs to
`/var/log/pve/tasks`, default ACLs to task directories and read ACLs on existing
vzdump files. Directory defaults also permit access to future other task files;
your source glob should forward only vzdump. ACL snapshots are saved with config.
Restore the printed config/ACL snapshot and restart the collector to roll back;
new files created afterwards keep inherited ACLs and must be reviewed separately.

For ongoing declarative management, generate and validate the complete collector
configuration with your deployment system rather than appending fragments.
See https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.journal/
for journal source configuration and permissions.
