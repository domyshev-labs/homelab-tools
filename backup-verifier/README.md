# Backup verifier

Verify actual backup artifacts and optionally send a color HTML email report.
Python standard library only; Linux/Proxmox, ZFS/findmnt and zstd/tar are needed.
Keep actual jobs, datasets, schedules and SMTP settings in your deployment repo.
Only a generic example is tracked here.

## Checks and percentages

The two top-level jobs are host configuration and Proxmox VM/CT backups.
A valid item requires a regular readable nonempty final archive newer than the
expected run, a stable size/inode/mtime and no recent .part/.tmp artifacts.
The directory must be on the exact configured ZFS dataset. Old files cannot
satisfy a newly due job. The host archive also passes zstd integrity and contains
the configured required members. Contents and keys are never extracted or logged.

VM selection is read each run from the configured Proxmox job ID, not a hardcoded
list. Explicit vmid and all/exclude selections are supported. The node's resource
API supplies guest types. Unsupported pool selection or API failures produce a
failed job rather than 100% for an empty list. Other nodes' guests are not credited.

A guest is SUCCESS only if all its selected checks pass. VM job percentage is
successful expected guests / all expected guests. Host job is 100% or 0%.
Overall percentage is the equal-weight average of the two jobs. Four of five
VMs and a successful host backup give 90%, not five of six (83.3%).
Subject and large HTML banner use FAIL at 0%, SUCCESS at 100%, WARNING otherwise.
The table has two grouped job cells; VM rows show individual SUCCESS/FAIL states.
Color and text convey status, with a plain-text alternative for email clients.

## Use

```bash
python3 backup-verifier/backup-verifier.py --config /absolute/config.json --validate-config
python3 backup-verifier/backup-verifier.py --config /absolute/config.json --wait
python3 backup-verifier/backup-verifier.py --config /absolute/config.json --wait --send
```

No email is sent without --send. Normal mode reads only archive metadata for
large guest files; --full additionally runs zstd -t on every selected archive and
may read many GB. It verifies compression, not guest bootability or VMA structure.
Test restoration remains the strongest separate recovery check.

--not-before ISO_TIMESTAMP is an explicit historical-run override, useful when
adopting monitoring after changing a weekly schedule. It changes the freshness
cutoff and must be clearly identified in the report objective; don't use it to
hide a missed scheduled run. Without override, each job's current daily/weekly
schedule determines the last due run. Only HH:MM or weekday HH:MM is supported.

--wait waits for active vzdump and host config jobs up to the configured timeout.
A shared scheduler lock protects storage throughout waiting/checking, then is
released before SMTP. The single verifier lock remains held through reporting.
No pool is imported or exported by the verifier. Coordinate its timer with the
disk window. Scheduling a verifier also detects jobs that never started; a hook
only triggered on successful backups cannot do that.

## Install

```bash
python3 backup-verifier/install.py --config /absolute/config.json
systemctl cat backup-verifier.service backup-verifier.timer
```

The installer validates units, saves previous files, uses the checkout directly
and leaves the timer stopped. After a live test and SMTP confirmation:

```bash
systemctl enable --now backup-verifier.timer
```

Exit 0 means all checks passed, 1 means a reportable backup failure, 2 means
configuration/mail failure. JSON events use journal identifier backup-verifier.
The optional Alloy helper accepts your external source fragment:

```bash
bash observability/agents/enable-backup-verifier.sh /absolute/verifier.alloy
```

## SMTP

STARTTLS is mandatory and certificate validation is enabled. SMTP settings include
only a password-file path. Store a password/app password outside Git in a regular
0600 file in a restricted directory. Never pass it in CLI arguments or log it.
--send records successful SMTP acceptance; inbox delivery may still be delayed.
No SMTP debugging is enabled. Sending failure logs only the exception type.

Restoring backups of verifier units/config after a failed install leaves its
timer stopped. Don't run git pull while verification is executing.
