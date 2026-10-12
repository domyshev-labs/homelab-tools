import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'zfs-disk-standby-scheduler'
SPEC = importlib.util.spec_from_file_location('installer', SOURCE / 'install.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
CONFIG = json.loads((SOURCE / 'zfs-disk-standby-scheduler.example.json').read_text())
CONFIG.update(disk='/dev/disk/by-id/wwn-0x0000000000000001', pool='tank', backup_path='/tank/backups/proxmox-vm-backups', storages=['tank-vmdata','proxmox-vm-backups'], start='02:00', end='05:00')


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / 'candidate.json'
        self.candidate.write_text(json.dumps(CONFIG))

    def installed_config(self, config):
        path = self.root / 'etc/zfs-disk-standby-scheduler.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        return path

    def test_validation_rejects_invalid_fields_and_types(self):
        changes = [dict(disk='/dev/sdb'), dict(disk=CONFIG['disk'] + '-part1'),
                   dict(disk='/dev/disk/by-id/REPLACE_WITH_WHOLE_DISK_ID'),
                   dict(disk='/dev/disk/by-id/../sdb'), dict(pool='tank/data'),
                   dict(pool='--help'), dict(backup_path='/'), dict(backup_path='relative'),
                   dict(backup_path='/tank/../backups'), dict(start='25:00'),
                   dict(start='2:00'), dict(start='02:00:00'), dict(end='02:00'),
                   dict(storages=[]), dict(storages=['backup', 'backup']),
                   dict(storages='backup'), dict(storages=[True]),
                   dict(samba_services=['smbd']), dict(samba_services=None)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                m.SCRIPT.validate_config(dict(CONFIG, **change))
        for config in ([], dict(CONFIG, extra=True), {key: value for key, value in CONFIG.items() if key != 'disk'}):
            with self.assertRaises(ValueError):
                m.SCRIPT.validate_config(config)

    def test_validation_supports_overnight_window_and_no_samba(self):
        config = dict(CONFIG, start='23:30', end='01:15', samba_services=[])
        self.assertEqual(m.SCRIPT.validate_config(config), config)

    def test_update_uses_existing_config_and_generates_matching_timers(self):
        config = dict(CONFIG, start='23:30', end='01:15')
        path = self.installed_config(config)
        original = path.read_bytes()
        selected, files = m.deployment(None, self.root)
        self.assertEqual(selected, config)
        self.assertNotIn('etc/zfs-disk-standby-scheduler.json', files)
        m.apply(files, self.root)
        self.assertEqual(path.read_bytes(), original)
        for action, clock in [('on', '23:30'), ('off', '01:15')]:
            text = (self.root / f'etc/systemd/system/zfs-disk-standby-scheduler-{action}.timer').read_text()
            self.assertIn(f'OnCalendar=*-*-* {clock}:00', text)
            self.assertIn('Persistent=false', text)

    def test_explicit_config_replaces_existing_and_backup_restores_it(self):
        path = self.installed_config(dict(CONFIG, start='01:00'))
        original = path.read_bytes()
        _, files = m.deployment(self.candidate, self.root)
        directory = m.apply(files, self.root)
        self.assertEqual(json.loads(path.read_text()), CONFIG)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        m.restore(directory, self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / 'usr/local/sbin/zfs-disk-standby-scheduler').exists())

    def test_failed_reload_restores_previous_files(self):
        path = self.installed_config(dict(CONFIG, start='01:00'))
        original = path.read_bytes()
        _, files = m.deployment(self.candidate, self.root)
        calls = []
        def reload():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError('reload failed')
        with self.assertRaises(RuntimeError):
            m.apply(files, self.root, reload)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / 'usr/local/sbin/zfs-disk-standby-scheduler').exists())
        self.assertEqual(len(calls), 2)

    def test_partial_write_failure_restores_previous_deployment(self):
        path = self.installed_config(CONFIG)
        original = path.read_bytes()
        _, files = m.deployment(self.candidate, self.root)
        real_write = m.atomic_write
        def write(target, data, mode):
            if target == self.root / 'etc/systemd/system/zfs-disk-standby-scheduler.service':
                raise OSError('disk full')
            real_write(target, data, mode)
        with patch.object(m, 'atomic_write', side_effect=write), self.assertRaises(OSError):
            m.apply(files, self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / 'usr/local/sbin/zfs-disk-standby-scheduler').exists())

    def test_validation_cli_has_no_disk_or_state_side_effects(self):
        result = subprocess.run(['python3', '-B', str(SOURCE / 'zfs-disk-standby-scheduler.py'),
                                 'validate-config', str(self.candidate)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['event'], 'config_validated')

    def test_invalid_config_does_not_modify_deployment(self):
        path = self.installed_config(CONFIG)
        original = path.read_bytes()
        self.candidate.write_text(json.dumps(dict(CONFIG, start='invalid')))
        with self.assertRaises(ValueError):
            m.deployment(self.candidate, self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / 'var/backups').exists())

    def test_installer_refuses_active_or_transitioning_services(self):
        for state in ('active', 'activating', 'deactivating', 'reloading', ''):
            output = f'LoadState=loaded\nActiveState={state}\n'
            with self.subTest(state=state), patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
                with self.assertRaises(RuntimeError):
                    m.check_idle()
        with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=inactive\n', '')):
            m.check_idle()
        with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'LoadState=not-found\nActiveState=inactive\n', '')):
            m.check_idle()

    def test_backup_refuses_symlink_targets(self):
        path = self.root / 'etc/zfs-disk-standby-scheduler.json'
        path.parent.mkdir()
        path.symlink_to(self.candidate)
        with self.assertRaises(ValueError):
            m.backup(self.root)

    def test_legacy_active_scheduler_is_rejected(self):
        result = subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=active\n', '')
        with patch.object(m.subprocess, 'run', return_value=result), self.assertRaises(RuntimeError):
            m.check_legacy()

    def test_legacy_enabled_timer_is_rejected(self):
        def query(args, **kwargs):
            output = 'LoadState=loaded\nActiveState=inactive\n' if 'show' in args else ''
            return subprocess.CompletedProcess(args, 0, output, '')
        with patch.object(m.subprocess, 'run', side_effect=query), self.assertRaises(RuntimeError):
            m.check_legacy()

    def test_legacy_missing_scheduler_does_not_block_installation(self):
        result = subprocess.CompletedProcess([], 1, 'LoadState=not-found\nActiveState=inactive\n', '')
        with patch.object(m.subprocess, 'run', return_value=result):
            m.check_legacy()


if __name__ == '__main__':
    unittest.main()
