#!/usr/bin/python3
"""Daily disk window; standard library only. Run as root on the PVE node."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def run(*args, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result


def in_window(start, end, now):
    start = datetime.time.fromisoformat(start)
    end = datetime.time.fromisoformat(end)
    if start == end:
        raise ValueError("start and end must differ")
    current = now.time().replace(tzinfo=None)
    return start <= current < end if start < end else current >= start or current < end


def log(message, level='info', event='operation'):
    print(json.dumps({
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'service': 'tank-window', 'level': level, 'event': event, 'message': message,
    }, ensure_ascii=False), flush=True)


def validate_config(config):
    """Validate without creating directories or accessing pool/disk state."""
    fields = {'pool', 'disk', 'backup_path', 'storages', 'start', 'end', 'samba_services'}
    if not isinstance(config, dict) or set(config) != fields:
        raise ValueError('Config must contain exactly: ' + ', '.join(sorted(fields)))
    for field in ('pool', 'disk', 'backup_path', 'start', 'end'):
        if not isinstance(config[field], str) or not config[field]:
            raise ValueError(f'{field} must be a non-empty string')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]*', config['pool']):
        raise ValueError('pool must be a pool name, without a dataset path')
    disk = config['disk']
    if (not re.fullmatch(r'/dev/disk/by-id/[A-Za-z0-9_.:+-]+', disk)
            or Path(disk).name in ('.', '..')
            or re.search(r'-part\d+$', disk) or 'REPLACE_' in disk):
        raise ValueError('disk must be a configured whole-disk /dev/disk/by-id path')
    path = Path(config['backup_path'])
    if (not path.is_absolute() or path == Path('/') or '..' in path.parts
            or any(ord(char) < 32 for char in config['backup_path'])):
        raise ValueError('backup_path must be an absolute directory path without ..')
    for field, pattern, allow_empty in (
            ('storages', r'[A-Za-z][A-Za-z0-9_-]*', False),
            ('samba_services', r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*\.service', True)):
        values = config[field]
        if (not isinstance(values, list) or (not values and not allow_empty)
                or any(not isinstance(value, str) or not re.fullmatch(pattern, value)
                       for value in values)):
            raise ValueError(f'{field} must be a list of valid, unique names')
        if len(values) != len(set(values)):
            raise ValueError(f'{field} must not contain duplicates')
    for field in ('start', 'end'):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', config[field]):
            raise ValueError(f'{field} must use HH:MM in 24-hour time')
    if config['start'] == config['end']:
        raise ValueError('start and end must differ')
    return config


class Controller:
    def __init__(self, config, state_dir=Path('/var/lib/tank-window'),
                 run_dir=Path('/run/tank-window')):
        self.c = validate_config(config)
        self.pool = config['pool']
        self.disk = config['disk']
        self.state_dir = state_dir
        self.run_dir = run_dir
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.sleep_marker = run_dir / 'standby'
        self.samba_state = state_dir / 'samba.json'

    def imported(self):
        # An imported-pool listing uses kernel state; it does not discover disks.
        pools = run('zpool', 'list', '-H', '-o', 'name').stdout.splitlines()
        return self.pool in pools

    def storage_flags(self):
        sections = {}
        current = None
        for line in Path('/etc/pve/storage.cfg').read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if not line[0].isspace() and ':' in line:
                kind, name = stripped.split(':', 1)
                current = name.strip()
                sections[current] = {'type': kind}
            elif current:
                key, *value = stripped.split(None, 1)
                sections[current][key] = value[0] if value else '1'
        for name in self.c['storages']:
            section = sections.get(name)
            if section is None:
                raise RuntimeError(f'Missing storage {name}')
            if section['type'] == 'zfspool':
                source = section.get('pool', '')
                if source != self.pool and not source.startswith(self.pool + '/'):
                    raise RuntimeError(f'{name} belongs to another pool')
            elif section['type'] == 'dir':
                if section.get('path') != self.c['backup_path']:
                    raise RuntimeError(f'Unexpected backup path for {name}')
            else:
                raise RuntimeError(f'Unsupported storage type for {name}')
        return {name: sections[name].get('disable', '0') in ('1', 'yes', 'true')
                for name in self.c['storages']}

    def set_storage(self, name, disabled):
        if self.storage_flags()[name] != disabled:
            run('pvesm', 'set', name, '--disable', str(int(disabled)))
            log(f'{name}: disabled={int(disabled)}')

    def validate_disk(self):
        if not Path(self.disk).exists() or not stat.S_ISBLK(os.stat(self.disk).st_mode):
            raise RuntimeError(f'Disk not connected: {self.disk}')
        if not self.imported():
            return
        status = run('zpool', 'status', '-LP', self.pool).stdout
        leaves = [line.split()[0] for line in status.splitlines()
                  if line.strip().startswith('/dev/')]
        if not leaves:
            raise RuntimeError('Cannot identify pool devices; refusing disk shutdown')
        expected = os.path.realpath(self.disk)
        for leaf in leaves:
            parent = run('lsblk', '-ndo', 'PKNAME', leaf).stdout.strip()
            whole = os.path.realpath('/dev/' + parent) if parent else os.path.realpath(leaf)
            if whole != expected:
                raise RuntimeError(f'{self.pool} contains another disk: {leaf}; refusing shutdown')

    def validate_mount(self):
        path = self.c['backup_path']
        if not Path(path).is_dir():
            raise RuntimeError(f'Backup directory missing: {path}; not creating it')
        result = json.loads(run('findmnt', '-J', '-T', path,
                                '-o', 'SOURCE,FSTYPE').stdout)
        fs = result['filesystems'][0]
        source = fs['source']
        if fs['fstype'] != 'zfs' or not (source == self.pool or source.startswith(self.pool + '/')):
            raise RuntimeError(f'{path} is not on {self.pool}; storage stays disabled')

    def active_backups(self):
        # Query failure is an error, never interpreted as "no backups".
        tasks = json.loads(run('pvenode', 'task', 'list', '--source', 'active',
                               '--typefilter', 'vzdump', '--limit', '0',
                               '--output-format', 'json').stdout)
        if not isinstance(tasks, list):
            raise RuntimeError('Unexpected active-task response')
        return bool(tasks)

    def active_samba(self):
        return [service for service in self.c['samba_services']
                if run('systemctl', 'is-active', '--quiet', service, check=False).returncode == 0]

    def save_samba(self, services):
        tmp = self.samba_state.with_suffix('.tmp')
        tmp.write_text(json.dumps(services))
        tmp.replace(self.samba_state)

    def restore_samba(self):
        # First wake also restores Samba stopped manually by the user.
        services = json.loads(self.samba_state.read_text()) if self.samba_state.exists() else self.c['samba_services']
        for service in services:
            load = run('systemctl', 'show', '-p', 'LoadState', '--value', service).stdout.strip()
            if load != 'not-found':
                run('systemctl', 'start', service)
        self.samba_state.unlink(missing_ok=True)

    def wake(self):
        self.validate_disk()
        if not self.imported():
            for name in self.c['storages']:
                self.set_storage(name, True)
            log(f'Importing {self.pool} from {self.disk}')
            run('zpool', 'import', '-d', self.disk + '-part1', self.pool)
            self.validate_disk()
        try:
            # Mount only this pool's regular automount datasets if already imported.
            for line in run('zfs', 'list', '-H', '-r', '-t', 'filesystem',
                            '-o', 'name,canmount,mounted', self.pool).stdout.splitlines():
                name, canmount, mounted = line.split()
                if canmount == 'on' and mounted == 'no':
                    run('zfs', 'mount', name)
            self.validate_mount()
        except Exception:
            for name in self.c['storages']:
                self.set_storage(name, True)
            raise
        for name in self.c['storages']:
            self.set_storage(name, False)
        self.sleep_marker.unlink(missing_ok=True)
        # Avoid starting deliberately stopped services on every minute check.
        if self.samba_state.exists() or not (self.run_dir / 'awake').exists():
            self.restore_samba()
        (self.run_dir / 'awake').touch()

    def sleep(self):
        if not self.imported():
            for name in self.c['storages']:
                self.set_storage(name, True)
            if self.sleep_marker.exists():
                return
            self.validate_disk()
            run('hdparm', '-y', self.disk)
            self.sleep_marker.touch()
            (self.run_dir / 'awake').unlink(missing_ok=True)
            log('Disk is in standby')
            return
        self.sleep_marker.unlink(missing_ok=True)
        self.validate_disk()
        status = run('zpool', 'status', self.pool).stdout.lower()
        if 'scrub in progress' in status or 'resilver in progress' in status:
            log('Scrub/resilver active: shutdown deferred until the next minute')
            return
        if self.active_backups():
            log('Backup active: shutdown deferred until the next minute')
            return
        before = self.storage_flags()
        # Keep the first saved service state across an interrupted transition.
        if not self.samba_state.exists():
            self.save_samba(self.active_samba())
        try:
            for name in self.c['storages']:
                self.set_storage(name, True)
            # Check again after disabling storage, closing the normal scheduler race.
            if self.active_backups():
                raise RuntimeError('A backup started during shutdown; deferred')
            for service in self.active_samba():
                run('systemctl', 'stop', service)
            log(f'Exporting {self.pool} (without force)')
            run('zpool', 'export', self.pool)
        except Exception:
            if self.imported():
                for name, disabled in before.items():
                    self.set_storage(name, disabled)
                self.restore_samba()
            raise
        # Never issue standby after an unsuccessful export.
        run('hdparm', '-y', self.disk)
        self.sleep_marker.touch()
        (self.run_dir / 'awake').unlink(missing_ok=True)
        log('Pool exported; disk is in standby')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == 'validate-config':
        validate_config(json.loads(Path(sys.argv[2]).read_text()))
        log('Configuration is valid', event='config_validated')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Run as root')
    mode = sys.argv[1] if len(sys.argv) == 2 else 'check'
    if mode not in ('check', 'on', 'off', 'status'):
        raise ValueError('Usage: tank-window [check|on|off|status] or validate-config PATH')
    with open('/run/lock/tank-window.lock', 'a') as lock:
        try:
            # Scheduled events wait for a concurrent check/task rather than being lost.
            flags = fcntl.LOCK_EX if mode in ('on', 'off') else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(lock, flags)
        except BlockingIOError:
            log('Another operation holds the lock; deferred')
            return
        config = json.loads(Path('/etc/tank-window.json').read_text())
        control = Controller(config)
        now = datetime.datetime.now().astimezone()
        wanted = in_window(config['start'], config['end'], now)
        pending_on = control.run_dir / 'pending-on'
        pending_off = control.run_dir / 'pending-off'
        if mode == 'status':
            log(f'Server time: {now.isoformat()}; window {config["start"]}–{config["end"]}')
            log(f'Pool imported: {control.imported()}; expected online: {wanted}')
            log(f'Storage disabled flags: {control.storage_flags()}')
            log(f'Pending wake: {pending_on.exists()}; pending shutdown: {pending_off.exists()}')
        elif mode == 'on':
            pending_off.unlink(missing_ok=True)
            pending_on.touch()
            control.wake()
            pending_on.unlink(missing_ok=True)
        elif mode == 'off':
            pending_on.unlink(missing_ok=True)
            pending_off.touch()
            control.sleep()
            if control.sleep_marker.exists():
                pending_off.unlink(missing_ok=True)
        elif pending_on.exists():
            if wanted:
                control.wake()
                pending_on.unlink(missing_ok=True)
            else:
                pending_on.unlink(missing_ok=True)
        elif pending_off.exists():
            if wanted:
                # A delayed shutdown must not close the next working window.
                pending_off.unlink(missing_ok=True)
            else:
                control.sleep()
                if control.sleep_marker.exists():
                    pending_off.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        log(f'{error}; standby was not forced. Pending operations retry next minute.',
            level='error', event='operation_failed')
        sys.exit(1)
