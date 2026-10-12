# Proxmox host configuration backup

Собственный скрипт сохраняет конфигурацию хоста Proxmox VE в проверенный ZFS
dataset. Служба запускает код прямо из checkout `~/homelab-tools`; копировать
скрипт в `/usr/local/sbin` больше не нужно. После `git pull` следующий запуск
использует новую версию. Установщик запоминает абсолютный путь checkout.

## Состав и настройки

В архив входят `/etc` (включая `/etc/pve`, сеть, systemd, storage, пользователей и
задания бэкапов), `/var/lib/pve-cluster/config.db`, `/usr/local/sbin`, текущий файл
скрипта из репозитория и `/root/.ssh`, если существует. Диски VM/CT и данные
приложений сохраняют отдельные задания Datacenter → Backup.

| Настройка | Значение |
| --- | --- |
| Dataset | `tank/backups/proxmox-host-backup` |
| Каталог | `/tank/backups/proxmox-host-backup` |
| Архив | `<hostname>-config-YYYY-MM-DD_HH-MM-SS.tar.zst` |
| Сжатие | `zstd -T0 -6` |
| Права | `umask 077`, итоговый архив `0600` |
| Хранение | После успеха удаляются архивы этого хоста с `-mtime +180` |
| Расписание | Каждый день 03:30 по времени сервера, задержка до 10 минут |
| Пропущенный запуск | `Persistent=false`: не догоняется после включения |

`BACKUP_DIR` и `BACKUP_DATASET` можно переопределить через `Environment=` в
systemd drop-in. Указанный каталог должен совпадать с mountpoint dataset.
Зависимости: root, Linux, Bash, Python 3, GNU tar, zstd, ZFS, findmnt, flock,
find, readlink и systemd. Для назначения нужны существующие pool и dataset.

## Защита при отключении tank

Скрипт держит общую shared-блокировку
`/run/lock/zfs-disk-standby-scheduler.lock` на протяжении проверки и бэкапа.
[Планировщик диска](../../zfs-disk-standby-scheduler/README.md) использует exclusive
блокировку и ждёт завершения бэкапа перед экспортом. Отдельная блокировка
`/run/lock/pve-config-backup.lock` запрещает одновременные конфигурационные бэкапы.
Ручной export без этой блокировки не координируется со скриптом.

Перед записью проверяются наличие каталога, SOURCE/FSTYPE через findmnt,
`mounted` и `mountpoint` через ZFS. Без нужного ZFS-монтирования скрипт пишет
ошибку и выходит; не создаёт каталог на системном диске и не импортирует пул.
Назначение не управляется через `RequiresMountsFor`: доступностью диска управляет
отдельный планировщик. Расписание бэкапа должно попадать в его окно, например
02:00–05:00. При выключенном tank бэкап завершается ошибкой без архива.

## Установка и переход с прежнего скрипта

На Proxmox под root, из существующего checkout:

```bash
cd ~/homelab-tools
git pull --ff-only
bash proxmox/pve-config-backup/install.sh
bash observability/agents/enable-pve-config-backup.sh
systemctl cat pve-config-backup.service pve-config-backup.timer
```

Установщик проверяет units, сохраняет прежние units и standalone-скрипт в
`/var/backups/pve-config-backup/before-install-*`, останавливает таймер,
регистрирует ExecStart из текущего checkout и оставляет таймер остановленным.
При работающей службе нужно дождаться её завершения. Старый файл в
`/usr/local/sbin` остаётся для восстановления, но служба его больше не запускает.
Если нет zstd: `apt-get install -y zstd`.

Сборщик логов должен уже существовать; см. [observability](../../observability/README.md).
Helper сохраняет endpoint Loki и все прежние источники, проверяет конфиг Alloy,
делает резервную копию и перезапускает только сборщик. При отсутствии доступа
к journald helper напечатает команду `usermod -aG adm,systemd-journal alloy`;
выполни её и повтори установку helper. Архивы сборщику недоступны и не нужны.

Безопасная проверка передачи лога без обращения к tank:

```bash
printf '%s\n' '{"service":"pve-config-backup","level":"info","event":"collector_test","message":"Configuration backup log collection test"}' | systemd-cat --identifier=pve-config-backup
```

Когда tank импортирован и dataset смонтирован, проверь назначение и бэкап:

```bash
systemd-cat --identifier=pve-config-backup /bin/bash "$PWD/proxmox/pve-config-backup/pve-config-backup" --check
systemctl start pve-config-backup.service
systemctl status pve-config-backup.service --no-pager
journalctl -u pve-config-backup.service -n 60 --no-pager
ls -lht /tank/backups/proxmox-host-backup
```

`--check` проверяет назначение и блокировки без создания архива; при выключенном
tank ожидается ошибка `storage_unavailable`. Успешная oneshot-служба показывает
`inactive (dead)` и `status=0/SUCCESS`. После проверки включи расписание:

```bash
systemctl enable --now pve-config-backup.timer
systemctl list-timers pve-config-backup.timer --all
```

## Обновление и откат

Для изменения кода достаточно `git pull --ff-only` в `~/homelab-tools`.
Не обновляй checkout во время работающего бэкапа. Если изменились unit-файлы,
повтори `install.sh`, проверь их и снова включи таймер. При переносе checkout
тоже нужно повторить установку: systemd хранит абсолютный путь.

Для отката останови таймер и дождись завершения службы. Верни прежнюю версию
checkout и переустанови units либо восстанови units из конкретного каталога
`/var/backups/pve-config-backup/before-install-*`. Затем `systemctl daemon-reload`.
Проверь ExecStart и расписание перед включением: старая служба может указывать
на standalone-скрипт, а старый таймер иметь `Persistent=true`.

## Логи и Grafana

Служба пишет stdout/stderr в journald с `SyslogIdentifier=pve-config-backup`.
Структурированные JSON-события: `backup_started`, `backup_completed`,
`backup_failed`, `storage_unavailable`, `storage_ready`, `backup_busy`.
Ошибки внешних команд остаются в журнале, после них идёт JSON с фазой сбоя.
В Grafana Explore:

```logql
{host="proxmox",service="pve-config-backup"}
```

Только ошибки:

```logql
{host="proxmox",service="pve-config-backup",level="error"}
```

Alloy читает только этот journal identifier и отправляет в существующий Loki.
Прямой запуск Bash пишет в терминал; используй службу или `systemd-cat`, чтобы
отправить вывод в Grafana. Логи не заменяют оповещение об отсутствии запусков:
правило alert в Grafana этим пакетом не создаётся.

## Архив и восстановление

Архив сначала пишется в `.part`, проверяется `zstd -t` и переименовывается.
При ошибке `.part` удаляется. Tar сохраняет ACL и числовые UID/GID; xattrs
не включены из-за pmxcfs. Проверка zstd подтверждает сжатый поток, а не полное
восстановление. `config.db` пока копируется как обычный файл работающей базы,
без транзакционного SQLite snapshot.

Архив содержит секреты: не публикуй его, храни копию вне сервера. Содержимое
проверяют через `tar -I zstd -tf /path/to/archive.tar.zst`. Восстанавливай настройки
выборочно по отдельному плану, не распаковывай весь архив поверх работающего `/`.

## Проверка разработки

```bash
bash -n proxmox/pve-config-backup/pve-config-backup proxmox/pve-config-backup/install.sh observability/agents/enable-pve-config-backup.sh
python3 -B -m unittest discover -s tests -v
```

Локальные проверки используют изолированные пути и имитацию Linux-команд;
не читают конфигурацию хоста и не управляют tank. Проверка Alloy выполняется
установленным на Proxmox `alloy validate` до изменения рабочего конфига.
