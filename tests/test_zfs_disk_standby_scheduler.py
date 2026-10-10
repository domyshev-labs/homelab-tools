import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import os
import stat
import builtins
import tempfile
import unittest
from unittest.mock import patch

script = Path(__file__).resolve().parents[1] / 'zfs-disk-standby-scheduler/zfs-disk-standby-scheduler.py'
spec = importlib.util.spec_from_file_location('zfs_disk_standby_scheduler', script)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
CONFIG = json.loads(script.with_name('zfs-disk-standby-scheduler.example.json').read_text())
CONFIG['disk'] = '/dev/disk/by-id/wwn-0x0000000000000001'


class Fake(m.Controller):
    def __init__(self, directory):
        super().__init__(CONFIG, directory / 'state', directory / 'run')
        self.online = True
        self.flags = {name: False for name in CONFIG['storages']}
        self.backups = [False, False]
        self.services = ['smbd.service']
        self.calls = []
        self.fail_export = False
        self.fail_standby = False
        self.disk_bad = False
        self.mount_bad = False

    def imported(self): return self.online
    def storage_flags(self): return self.flags.copy()
    def set_storage(self, name, disabled): self.flags[name] = disabled
    def validate_disk(self):
        if self.disk_bad: raise RuntimeError('wrong disk')
    def validate_mount(self):
        if self.mount_bad: raise RuntimeError('wrong filesystem')
    def active_backups(self): return self.backups.pop(0)
    def active_samba(self): return self.services.copy()
    def restore_samba(self):
        self.services = json.loads(self.samba_state.read_text()) if self.samba_state.exists() else CONFIG['samba_services']
        self.samba_state.unlink(missing_ok=True)
    def command(self, *args, check=True):
        self.calls.append(args)
        if args[:2] == ('zpool', 'export'):
            if self.fail_export: raise RuntimeError('pool busy')
            self.online = False
        if args[:2] == ('zpool', 'import'): self.online = True
        if args[:2] == ('hdparm', '-y') and self.fail_standby:
            raise RuntimeError('standby failed')
        if args[:2] == ('systemctl', 'stop'): self.services.remove(args[2])
        return subprocess.CompletedProcess(args, 0, '', '')


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = Fake(Path(self.temp.name))
        self.mock = patch.object(m, 'run', self.c.command)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_window_boundaries_and_midnight(self):
        for hour, minute, expected in [(1, 59, False), (2, 0, True), (4, 59, True), (5, 0, False)]:
            self.assertEqual(m.in_window('02:00', '05:00', dt.datetime(2026, 10, 9, hour, minute)), expected)
        self.assertTrue(m.in_window('23:00', '02:00', dt.datetime(2026, 10, 9, 1)))
        self.assertFalse(m.in_window('23:00', '02:00', dt.datetime(2026, 10, 9, 3)))

    def test_active_backup_no_shutdown(self):
        self.c.backups = [True]
        self.c.sleep()
        self.assertFalse(any(cmd[0] == 'hdparm' for cmd in self.c.calls))
        self.assertFalse(any(cmd[:2] == ('zpool', 'export') for cmd in self.c.calls))
        self.assertFalse(any(self.c.flags.values()))

    def test_backup_starts_during_transition_rollback(self):
        self.c.backups = [False, True]
        with self.assertRaises(RuntimeError): self.c.sleep()
        self.assertFalse(any(self.c.flags.values()))
        self.assertEqual(self.c.services, ['smbd.service'])
        self.assertFalse(any(cmd[0] == 'hdparm' for cmd in self.c.calls))

    def test_export_failure_rollback_never_standby(self):
        self.c.fail_export = True
        with self.assertRaises(RuntimeError): self.c.sleep()
        self.assertTrue(self.c.online)
        self.assertFalse(any(self.c.flags.values()))
        self.assertEqual(self.c.services, ['smbd.service'])
        self.assertFalse(any(cmd[0] == 'hdparm' for cmd in self.c.calls))

    def test_successful_export_before_standby_and_no_repeat(self):
        self.c.sleep()
        export = self.c.calls.index(('zpool', 'export', 'tank'))
        standby = self.c.calls.index(('hdparm', '-y', CONFIG['disk']))
        self.assertLess(export, standby)
        self.assertTrue(all(self.c.flags.values()))
        self.assertEqual(self.c.services, [])
        self.assertTrue(self.c.sleep_marker.exists())
        count = len(self.c.calls)
        self.c.sleep()
        self.assertEqual(len(self.c.calls), count)

    def test_standby_failure_can_retry_after_export(self):
        self.c.fail_standby = True
        with self.assertRaises(RuntimeError): self.c.sleep()
        self.assertFalse(self.c.online)
        self.assertFalse(self.c.sleep_marker.exists())
        self.c.fail_standby = False
        self.c.sleep()
        self.assertTrue(self.c.sleep_marker.exists())

    def test_disk_mismatch_prevents_export_and_standby(self):
        self.c.disk_bad = True
        with self.assertRaises(RuntimeError): self.c.sleep()
        self.assertEqual(self.c.calls, [])

    def test_bad_mount_disables_storages(self):
        self.c.mount_bad = True
        with self.assertRaises(RuntimeError): self.c.wake()
        self.assertTrue(all(self.c.flags.values()))

    def test_task_query_failure_does_not_shutdown(self):
        def fail(): raise RuntimeError('pvenode unavailable')
        self.c.active_backups = fail
        with self.assertRaises(RuntimeError): self.c.sleep()
        self.assertFalse(any(cmd[0] == 'hdparm' for cmd in self.c.calls))
        self.assertFalse(any(self.c.flags.values()))

    def test_wake_restores_services_saved_before_export(self):
        self.c.sleep()
        self.c.wake()
        self.assertTrue(self.c.online)
        self.assertFalse(any(self.c.flags.values()))
        self.assertEqual(self.c.services, ['smbd.service'])
        self.assertFalse(self.c.sleep_marker.exists())

    def test_stable_whole_disk_required(self):
        for disk in ['/dev/sdb', CONFIG['disk'] + '-part1']:
            config = dict(CONFIG, disk=disk)
            with self.assertRaises(ValueError):
                m.Controller(config, Path(self.temp.name) / 's', Path(self.temp.name) / 'r')

    def test_real_task_parser(self):
        with patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 0, '[{"type":"vzdump"}]', '')):
            self.assertTrue(m.Controller.active_backups(self.c))
        with patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')):
            with self.assertRaises(RuntimeError): m.Controller.active_backups(self.c)

    def test_real_pool_device_validation(self):
        self.c.disk = '/dev/sdb'
        def devices(*args, **kwargs):
            content = '  /dev/sdb1 ONLINE 0 0 0\n' if args[0] == 'zpool' else 'sdb\n'
            return subprocess.CompletedProcess(args, 0, content, '')
        block = os.stat_result((stat.S_IFBLK, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        with patch.object(Path, 'exists', return_value=True), patch.object(m.os, 'stat', return_value=block), patch.object(m, 'run', side_effect=devices):
            m.Controller.validate_disk(self.c)
            self.c.disk = '/dev/sdc'
            with self.assertRaises(RuntimeError): m.Controller.validate_disk(self.c)

    def test_real_mount_guard(self):
        self.c.c = dict(CONFIG, backup_path=self.temp.name)
        for source, fstype, good in [('tank/backups', 'zfs', True), ('/dev/sda3', 'ext4', False), ('tank-wrong', 'zfs', False)]:
            result = json.dumps({'filesystems': [{'source': source, 'fstype': fstype}]})
            with patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 0, result, '')):
                if good: m.Controller.validate_mount(self.c)
                else:
                    with self.assertRaises(RuntimeError): m.Controller.validate_mount(self.c)

    def invoke_main(self, mode, hour):
        original_open = builtins.open
        original_read = Path.read_text
        directory = Path(self.temp.name)
        class FixedDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None): return cls(2026, 10, 9, hour, 0)
        def open_local(file, *args, **kwargs):
            if file == '/run/lock/zfs-disk-standby-scheduler.lock': file = directory / 'lock'
            return original_open(file, *args, **kwargs)
        def read_local(path, *args, **kwargs):
            if str(path) == '/etc/zfs-disk-standby-scheduler.json': return json.dumps(CONFIG)
            return original_read(path, *args, **kwargs)
        with patch.object(m, 'Controller', return_value=self.c), patch.object(m.os, 'geteuid', return_value=0), patch.object(m.sys, 'argv', ['zfs-disk-standby-scheduler', mode]), patch.object(m.datetime, 'datetime', FixedDateTime), patch.object(Path, 'read_text', read_local), patch('builtins.open', open_local):
            m.main()

    def test_enabling_check_in_daytime_does_nothing(self):
        self.invoke_main('check', 14)
        self.assertTrue(self.c.online)
        self.assertEqual(self.c.calls, [])

    def test_off_event_retries_busy_pool_then_clears_request(self):
        self.c.backups = [True]
        self.invoke_main('off', 5)
        self.assertTrue((self.c.run_dir / 'pending-off').exists())
        self.c.backups = [False, False]
        self.invoke_main('check', 6)
        self.assertFalse(self.c.online)
        self.assertFalse((self.c.run_dir / 'pending-off').exists())

    def test_stale_shutdown_cancelled_in_new_window(self):
        (self.c.run_dir / 'pending-off').touch()
        self.invoke_main('check', 2)
        self.assertTrue(self.c.online)
        self.assertEqual(self.c.calls, [])
        self.assertFalse((self.c.run_dir / 'pending-off').exists())

    def test_on_event_cancels_pending_shutdown(self):
        (self.c.run_dir / 'pending-off').touch()
        self.invoke_main('on', 2)
        self.assertTrue(self.c.online)
        self.assertFalse((self.c.run_dir / 'pending-off').exists())
        self.assertFalse((self.c.run_dir / 'pending-on').exists())


if __name__ == '__main__': unittest.main()
