# Настройка VM на Freebox для синхронизации

Инструкция по настройке автоматической синхронизации с seedbox на NAS Freebox Ultra.

## Обзор

Система синхронизации:
1. Запускается каждые 30 минут на VM Freebox
2. Синхронизирует завершённые загрузки с seedbox через rsync
3. Сортирует файлы в папки Фильмы/Сериалы
4. Уведомляет бота, когда файлы готовы
5. Очищает seedbox

## Требования

- Freebox Ultra или Delta (с поддержкой VM)
- Аккаунт seedbox на Ultra.cc (см. [SEEDBOX_SETUP.md](SEEDBOX_SETUP.md))
- SSH-доступ к seedbox

## 1. Создание VM на Freebox

1. Открой Freebox OS: https://mafreebox.free.fr
2. Перейди в раздел **VMs**
3. Нажми **Создать новую VM**
4. Настрой:
   - **Имя**: `mediabot`
   - **Система**: Debian 12
   - **RAM**: 1 ГБ
   - **Диск**: 16 ГБ
   - **Сеть**: Bridge mode
   - Отметь **Доступ к диску Freebox**
5. Создай и запусти VM

## 2. Начальная настройка VM

Подключись к VM через консоль Freebox OS или SSH.

### Установка зависимостей

```bash
sudo apt update
sudo apt install -y rsync sshpass curl jq cron
```

### Создание пользователя и директорий

```bash
# Создаём отдельного пользователя
sudo useradd -m -s /bin/bash mediabot

# Создаём директории для синхронизации
sudo mkdir -p /home/mediabot/sync/logs
sudo chown -R mediabot:mediabot /home/mediabot
```

### Монтирование хранилища Freebox

Диск Freebox должен быть автоматически доступен по пути `/mnt/Freebox` или аналогичному.
Проверь:

```bash
ls /mnt/Freebox/Space/
```

Если не примонтирован, добавь в `/etc/fstab`:
```
//mafreebox.free.fr/Space /mnt/Freebox/Space cifs credentials=/home/mediabot/.smbcredentials,uid=mediabot,gid=mediabot 0 0
```

## 3. Установка скриптов синхронизации

### Копирование скриптов

Из репозитория бота скопируй скрипты:

```bash
# Под пользователем mediabot
sudo -u mediabot -i

# Создаём структуру директорий
mkdir -p ~/sync/logs

# Скачиваем скрипты (или копируем из репозитория)
# Вариант 1: Клонируем репозиторий
git clone https://github.com/ТВОЙ_РЕПО/media-concierge-bot.git /tmp/bot
cp /tmp/bot/scripts/sync_seedbox.sh ~/sync/
cp /tmp/bot/scripts/config.env.template ~/sync/config.env

# Вариант 2: Создаём вручную (копируем содержимое из репозитория)
```

### Настройка credentials

```bash
# Редактируем конфиг
nano ~/sync/config.env

# Защищаем файл
chmod 600 ~/sync/config.env
```

Заполни:
- `SEEDBOX_HOST`: Твой сервер Ultra.cc (например, `john.sb01.usbx.me`)
- `SEEDBOX_USER`: Твой логин
- `SEEDBOX_PASS`: Твой пароль
- `SEEDBOX_PATH`: Путь к завершённым загрузкам (обычно `/home/USERNAME/Downloads/completed`)
- `NAS_MOVIES`: Локальная папка для фильмов
- `NAS_TV`: Локальная папка для сериалов
- `BOT_API_URL`: URL бота на Koyeb
- `SYNC_API_KEY`: API-ключ для уведомлений

### Делаем скрипт исполняемым

```bash
chmod +x ~/sync/sync_seedbox.sh
```

## 4. Тестирование скрипта

Сначала запусти вручную:

```bash
~/sync/sync_seedbox.sh
```

Проверь лог:
```bash
tail -f ~/sync/logs/sync.log
```

## 5. Настройка Cron

```bash
# Редактируем crontab
crontab -e

# Добавляем строку (запуск каждые 30 минут):
*/30 * * * * /home/mediabot/sync/sync_seedbox.sh >> /home/mediabot/sync/logs/cron.log 2>&1
```

## 6. Настройка API-ключа бота

В деплое на Koyeb добавь переменную окружения:

```
SYNC_API_KEY=твой_секретный_ключ
```

Используй тот же ключ в `config.env`.

## Структура папок

После настройки NAS будет организован так:

```
/mnt/Freebox/Space/Фильмы и сериалы/
├── Кино/
│   ├── Movie.Name.2024.2160p.BluRay.mkv
│   └── Another.Movie.1080p.WEB-DL.mkv
└── Сериалы/
    ├── Series.Name.S01E01.720p.WEB.mkv
    └── Other.Show.S02E05.1080p.mkv
```

## Решение проблем

### Скрипт падает с «Permission denied»
```bash
chmod +x ~/sync/sync_seedbox.sh
chmod 600 ~/sync/config.env
```

### «Host key verification failed»
Сначала подключись по SSH вручную, чтобы принять ключ хоста:
```bash
ssh USERNAME@SERVERNAME.usbx.me
# Введи 'yes' для подтверждения
```

### Rsync зависает
Проверь подключение к seedbox:
```bash
ping SERVERNAME.usbx.me
ssh USERNAME@SERVERNAME.usbx.me "ls"
```

### Файлы сортируются неправильно
Скрипт определяет сериалы по паттернам типа `S01E01`. Если файлы названы иначе, они попадут в Фильмы по умолчанию.

### Нет уведомлений
1. Проверь, что `SYNC_API_KEY` совпадает в config.env и в Koyeb
2. Проверь правильность URL бота
3. Посмотри ошибки curl в sync.log

## Обслуживание

### Просмотр логов
```bash
tail -100 ~/sync/logs/sync.log
```

### Очистка старых логов
```bash
find ~/sync/logs -name "*.log" -mtime +30 -delete
```

### Проверка свободного места
```bash
df -h /mnt/Freebox/Space/
```
