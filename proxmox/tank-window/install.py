#!/usr/bin/env python3
"""Install/update with validated configuration, generated timers and rollback."""
import argparse
import ast
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('tank_window', SOURCE / 'tank-window.py')
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)
UNITS = ('tank-window.service', 'tank-window-on.service', 'tank-window-off.service',
         'tank-window.timer', 'tank-window-on.timer', 'tank-window-off.timer')
TARGETS = ('usr/local/sbin/tank-window', 'etc/tank-window.json') + tuple(
    'etc/systemd/system/' + unit for unit in UNITS)


def timer_text(action, clock):
    return (f'[Unit]\nDescription=Daily tank disk {action} request\n\n'
            f'[Timer]\nOnCalendar=*-*-* {clock}:00\nAccuracySec=1s\n'
            f'Persistent=false\nUnit=tank-window-{action}.service\n\n'
            '[Install]\nWantedBy=timers.target\n')


def deployment(config_path, root=Path('/')):
    """Default updates preserve the installed config; --config replaces it."""
    installed = root / 'etc/tank-window.json'
    selected = Path(config_path) if config_path else (
        installed if installed.exists() else SOURCE / 'tank-window.json')
    if not selected.is_file():
        raise ValueError('Copy tank-window.example.json to tank-window.json and configure it first')
    config = SCRIPT.validate_config(json.loads(selected.read_text()))
    script = (SOURCE / 'tank-window.py').read_bytes()
    ast.parse(script)
    files = {'usr/local/sbin/tank-window': (script, 0o755)}
    if config_path or not installed.exists():
        files['etc/tank-window.json'] = ((json.dumps(config, indent=2) + '\n').encode(), 0o600)
    for unit in UNITS:
        if unit == 'tank-window-on.timer':
            data = timer_text('on', config['start']).encode()
        elif unit == 'tank-window-off.timer':
            data = timer_text('off', config['end']).encode()
        else:
            data = (SOURCE / unit).read_bytes()
        files['etc/systemd/system/' + unit] = (data, 0o644)
    return config, files


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def backup(root):
    parent = root / 'var/backups/tank-window'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(parent, 0o700)
    directory = Path(tempfile.mkdtemp(prefix='before-', dir=parent))
    manifest = {}
    for relative in TARGETS:
        target = root / relative
        if target.is_symlink():
            raise ValueError('Refusing to replace symlink: ' + str(target))
        manifest[relative] = target.exists()
        if target.exists():
            saved = directory / relative
            saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(target, saved)
    atomic_write(directory / 'manifest.json', (json.dumps(manifest) + '\n').encode(), 0o600)
    return directory


def restore(directory, root):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if set(manifest) != set(TARGETS) or any(type(value) is not bool for value in manifest.values()):
        raise ValueError('Unexpected backup manifest')
    files = {}
    for relative, existed in manifest.items():
        if existed:
            saved = directory / relative
            files[relative] = (saved.read_bytes(), saved.stat().st_mode & 0o777)
    for relative, existed in manifest.items():
        if existed:
            atomic_write(root / relative, *files[relative])
        else:
            (root / relative).unlink(missing_ok=True)


def apply(files, root=Path('/'), reload_units=None):
    directory = backup(root)
    try:
        for relative, (data, mode) in files.items():
            atomic_write(root / relative, data, mode)
        if reload_units:
            reload_units()
    except Exception:
        restore(directory, root)
        if reload_units:
            reload_units()
        raise
    return directory


def check_idle():
    for unit in UNITS:
        result = subprocess.run(['systemctl', 'show', '-p', 'LoadState', '-p', 'ActiveState', unit],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        state = properties.get('ActiveState', '')
        absent = properties.get('LoadState') == 'not-found' and state == 'inactive'
        if (result.returncode and not absent) or state not in ('inactive', 'failed'):
            raise RuntimeError(f'Stop timers and wait for services before installing: {unit} ({state or "unknown"})')


def reload_units():
    subprocess.run(['systemctl', 'daemon-reload'], check=True)


def install(args):
    check_idle()
    if args.rollback:
        directory = backup(Path('/'))
        try:
            restore(args.rollback.resolve(), Path('/'))
            reload_units()
        except Exception:
            restore(directory, Path('/'))
            reload_units()
            raise
        print('Restored backup. Current state saved at: ' + str(directory))
    else:
        config, files = deployment(args.config)
        # Reuse read-only safety checks without creating controller state directories.
        controller = object.__new__(SCRIPT.Controller)
        controller.c, controller.pool, controller.disk = config, config['pool'], config['disk']
        controller.storage_flags()
        controller.validate_disk()
        directory = apply(files, reload_units=reload_units)
        print(f'Installed. Daily window: {config["start"]}-{config["end"]} (server timezone).')
        print('Backup: ' + str(directory))
        print('Existing config preserved.' if 'etc/tank-window.json' not in files else 'Configuration installed.')
    print('Timers remain stopped. Verify: tank-window status')
    print('Activate: systemctl enable --now tank-window.timer tank-window-on.timer tank-window-off.timer')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--config', type=Path, help='explicitly replace the installed configuration')
    group.add_argument('--rollback', type=Path, help='restore a backup created by this installer')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run as root on Proxmox')
    for tool in ('zpool', 'zfs', 'pvesm', 'pvenode', 'hdparm', 'findmnt', 'lsblk', 'systemctl'):
        if not shutil.which(tool):
            raise RuntimeError('Missing command: ' + tool)
    with open('/run/lock/tank-window.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A disk operation or coordinated task is running; try again after it finishes')
        install(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error))
