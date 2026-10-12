import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.dest = self.base / 'backup'
        self.dest.mkdir()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        source = (ROOT / 'proxmox/pve-config-backup/pve-config-backup').read_text()
        # Isolate privileged paths and root guard; all host commands are simulated.
        source = source.replace('/run/lock/', str(self.base) + '/')
        source = source.replace('if [[ $EUID -ne 0 ]]; then', 'if false; then')
        self.script = self.base / 'backup-script'
        self.script.write_text(source)
        (self.base / 'config.py').write_text((ROOT / 'proxmox/pve-config-backup/config.py').read_text())
        self.config = self.base / 'config.json'
        self.config.write_text(json.dumps(dict(backup_dir=str(self.dest), backup_dataset='tank/backups/proxmox-host-backup', retention_days=180, compression_level=6, schedule='03:30', randomized_delay_seconds=600, persistent=False)))
        shim = '''#!/usr/bin/env python3
import os, pathlib, sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f: f.write(name+' '+repr(args)+'\\n')
if name == 'findmnt':
    print(os.environ.get('SOURCE', 'tank/backups/proxmox-host-backup') if args[-1]=='SOURCE' else os.environ.get('FSTYPE', 'zfs'))
elif name == 'zfs':
    print(os.environ['BACKUP_DIR'] if args[-2]=='mountpoint' else os.environ.get('MOUNTED', 'yes'))
elif name == 'tar':
    pathlib.Path(args[args.index('-cpf')+1]).write_bytes(b'fake archive')
    sys.exit(int(os.environ.get('TAR_STATUS', '0')))
elif name == 'zstd':
    sys.exit(int(os.environ.get('ZSTD_STATUS', '0')))
'''
        for name in ('findmnt', 'zfs', 'flock', 'tar', 'zstd'):
            f = self.bin / name
            f.write_text(shim)
            f.chmod(0o755)
        self.calls = self.base / 'calls'
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        BACKUP_DIR=str(self.dest), CALLS=str(self.calls))

    def run_script(self, args=(), **env):
        result = subprocess.run(['bash', str(self.script), '--config', str(self.config), *args],
                                env=dict(self.env, **env), text=True, capture_output=True)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        return result, records

    def assert_no_archive(self):
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_exported_pool_rejected_without_archive(self):
        result, logs = self.run_script(FSTYPE='ext4', SOURCE='/dev/root', MOUNTED='no')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(logs[-1]['event'], 'storage_unavailable')
        self.assert_no_archive()
        self.assertNotIn('tar ', self.calls.read_text())

    def test_wrong_zfs_dataset_rejected(self):
        result, logs = self.run_script(SOURCE='tank/other')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(logs[-1]['event'], 'storage_unavailable')
        self.assert_no_archive()

    def test_check_does_not_create_archive(self):
        result, logs = self.run_script(['--check'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(logs[-1]['event'], 'storage_ready')
        self.assert_no_archive()
        self.assertNotIn('tar ', self.calls.read_text())

    def test_tar_failure_removes_partial_archive(self):
        result, logs = self.run_script(TAR_STATUS='2')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(logs[-1]['event'], 'backup_failed')
        self.assertIn('archive_creation', logs[-1]['message'])
        self.assert_no_archive()

    def test_integrity_failure_removes_partial_archive(self):
        result, logs = self.run_script(ZSTD_STATUS='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(logs[-1]['level'], 'error')
        self.assertIn('archive_verification', logs[-1]['message'])
        self.assert_no_archive()

    def test_success_logs_completion_and_restricts_permissions(self):
        result, logs = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r['event'] for r in logs], ['backup_started', 'backup_completed'])
        archives = list(self.dest.glob('*.tar.zst'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.dest.glob('*.part')), [])


if __name__ == '__main__':
    unittest.main()
