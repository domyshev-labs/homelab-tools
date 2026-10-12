# Proxmox host configuration backup

Скрипт `pve-config-backup` сохраняет настройки хоста Proxmox VE для восстановления
после переустановки или поломки системного диска. Это собственный скрипт,
настроенный 24 сентября 2026 года; версия восстановлена из истории чата с учётом
удалённого тогда `--xattrs`. Текущее содержимое файлов на сервере не сверялось.

## Содержимое и поведение

В архив входят:

- весь `/etc`, включая `/etc/pve` (конфиги всех VM/CT, storage, пользователи,
  задания бэкапов), сеть и systemd units;
- `/var/lib/pve-cluster/config.db`;
- `/usr/local/sbin`, включая сам скрипт;
- `/root/.ssh`, если каталог существует.

Диски VM/CT и данные приложений сюда не входят. Их сохраняют отдельные задания
Proxmox в Datacenter → Backup.

| Параметр | Значение |
| --- | --- |
| Каталог | `/tank/backups/proxmox-host-backup` |
| Имя | `<hostname>-config-YYYY-MM-DD_HH-MM-SS.tar.zst` |
| Сжатие | `zstd -T0 -6` |
| Права | `umask 077`, итоговый архив `0600` |
| Хранение | Удаление архивов этого хоста с `-mtime +180` после успешного бэкапа |
| Расписание | Ежедневно 03:30 по часовому поясу сервера, задержка до 10 минут |
| Пропущенный запуск | `Persistent=true`: выполняется после активации таймера |

Архив сначала пишется в `.part`, проверяется `zstd -t` и переименовывается.
При выходе скрипт удаляет незавершённый `.part`. Tar сохраняет ACL и числовые
UID/GID; `--xattrs` убран из-за отсутствия поддержки в `/etc/pve` (pmxcfs).
Проверка zstd подтверждает целостность сжатого потока, а не возможность полного
восстановления хоста. `config.db` копируется как обычный файл работающей базы,
без SQLite snapshot; это ограничение исходного скрипта.

## Установка на Proxmox

Нужны root, Bash, GNU tar, zstd, find и systemd. Для указанного назначения нужен
существующий ZFS-пул `tank` и родительский dataset `tank/backups`.
Все команды ниже выполняются на Proxmox, из корня репозитория.

Проверь dataset и монтирование:

```bash
zfs list tank/backups/proxmox-host-backup
findmnt -T /tank/backups/proxmox-host-backup -o SOURCE,FSTYPE,TARGET
```

Если dataset ещё не существует, при импортированном пуле создай его:

```bash
zfs create -o compression=zstd tank/backups/proxmox-host-backup
chmod 700 /tank/backups/proxmox-host-backup
```

Ожидается SOURCE `tank/backups/proxmox-host-backup`, FSTYPE `zfs` и соответствующий
mountpoint. Не создавай обычный каталог вместо отсутствующего ZFS-монтирования.

При обновлении останови таймер и дождись завершения службы. Перед заменой сохрани
существующие скрипт и unit-файлы в отдельный каталог с правами `0700`.

```bash
systemctl disable --now pve-config-backup.timer
systemctl is-active pve-config-backup.service
```

Для первой установки эти две команды не нужны. Копирование файлов:

```bash
install -m 0700 proxmox/pve-config-backup/pve-config-backup /usr/local/sbin/pve-config-backup
install -m 0644 proxmox/pve-config-backup/pve-config-backup.service /etc/systemd/system/pve-config-backup.service
install -m 0644 proxmox/pve-config-backup/pve-config-backup.timer /etc/systemd/system/pve-config-backup.timer
systemd-analyze verify /etc/systemd/system/pve-config-backup.service /etc/systemd/system/pve-config-backup.timer
systemctl daemon-reload
```

При другом назначении измени `BACKUP_DIR` в скрипте и `RequiresMountsFor` в службе
согласованно. Проверь часовой пояс через `timedatectl`.

## Проверка, расписание и ручной запуск

При смонтированном dataset выполни:

```bash
systemctl start pve-config-backup.service
systemctl status pve-config-backup.service --no-pager
journalctl -u pve-config-backup.service -n 60 --no-pager
ls -lht /tank/backups/proxmox-host-backup
```

Для одноразовой службы `inactive (dead)` после `status=0/SUCCESS` нормально.
Проверь конкретный созданный архив, подставив его имя:

```bash
zstd -t /tank/backups/proxmox-host-backup/ARCHIVE.tar.zst
tar -I zstd -tf /tank/backups/proxmox-host-backup/ARCHIVE.tar.zst | less
```

Конфиги VM находятся в `etc/pve/nodes/<node>/qemu-server/<ID>.conf`,
контейнеров — в `etc/pve/nodes/<node>/lxc/<ID>.conf`.
После успешной проверки включи расписание:

```bash
systemctl enable --now pve-config-backup.timer
systemctl list-timers pve-config-backup.timer --all
```

Ручной запуск через `systemctl start pve-config-backup.service` пишет лог в journald.
Прямой запуск `/usr/local/sbin/pve-config-backup` выводит результат в терминал;
не запускай его одновременно со службой: блокировки в исходном скрипте нет.

## Совместимость с расписанием диска

Этот скрипт не импортирует ZFS-пул и не управляет standby. Планируй бэкап внутри
окна доступности диска (например, 02:00–05:00). Исходный скрипт проверяет только
наличие каталога, не его ZFS-монтирование; `RequiresMountsFor` не заменяет проверку
пула. При экспортированном пуле каталог может остаться на системном диске.

`Persistent=true` может запустить пропущенную задачу вне окна доступности.
Перед установкой вместе с [ZFS disk standby scheduler](../../zfs-disk-standby-scheduler/README.md)
согласуй это поведение: используй описанную там общую блокировку и проверку
монтирования, а при необходимости отключи догоняющие запуски таймера.
Эта историческая версия не включает такую интеграцию автоматически.

## Восстановление и хранение

Архив содержит секреты хоста; не добавляй его в Git. Копию храни также вне сервера.
При восстановлении сначала проверь список файлов и распакуй архив в отдельный
защищённый каталог. Возвращай настройки выборочно после установки совместимого
Proxmox; не распаковывай весь архив поверх работающего `/`, особенно `/etc/pve`
и `config.db`. Восстановление работающего pmxcfs требует отдельного плана.

24 сентября 2026 года запуск через systemd был подтверждён `status=0/SUCCESS`.
На 12 октября в предоставленном списке последний архив датирован 25 сентября:
текущее состояние таймера и причины отсутствия новых архивов ещё не проверены.

## Локальная проверка

```bash
bash -n proxmox/pve-config-backup/pve-config-backup
```

Это проверяет синтаксис. Для проверки фактического бэкапа используй команды
выше на Proxmox; запуск на рабочем Mac не предназначен для тестирования.
