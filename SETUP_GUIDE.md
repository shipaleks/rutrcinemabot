# 🚀 Инструкция по запуску Ralph Wiggum для Media Concierge Bot

## Что такое Ralph Wiggum?

Ralph Wiggum — это методология автономной AI-разработки, где Claude Code работает в цикле, итеративно выполняя задачи из `prd.json` до полного завершения проекта.

**Философия:**
- "Iteration beats perfection" — итерация важнее совершенства
- "Deterministically bad" — предсказуемые ошибки лучше непредсказуемых успехов
- "Let Ralph Ralph" — позволь ему работать, не мешай

---

## Предварительные требования

### 1. Установить Claude Code CLI

```bash
# Через npm
npm install -g @anthropic-ai/claude-code

# Проверить установку
claude --version
```

### 2. Настроить Anthropic API ключ

```bash
# Экспортировать переменную (добавить в ~/.bashrc или ~/.zshrc)
export ANTHROPIC_API_KEY="sk-ant-api03-..."

# Или использовать конфиг Claude Code
claude config set apiKey sk-ant-api03-...
```

### 3. Установить Python 3.11+

```bash
# macOS
brew install python@3.11

# Ubuntu/Debian
sudo apt install python3.11 python3.11-venv

# Проверить
python3 --version
```

### 4. Установить Git и jq

```bash
# macOS
brew install git jq

# Ubuntu/Debian
sudo apt install git jq
```

---

## Пошаговая настройка

### Шаг 1: Создать ПУСТОЙ репозиторий на GitHub

1. Зайти на [github.com/new](https://github.com/new)
2. **Repository name:** `media-concierge-bot`
3. **Description:** `AI-powered Telegram bot for media discovery`
4. **Visibility:** Private (рекомендуется)
5. ⚠️ **НЕ ставить галочки** на:
   - Add a README file
   - Add .gitignore
   - Choose a license
6. Нажать **Create repository**
7. Скопировать SSH URL: `git@github.com:YOUR_USERNAME/media-concierge-bot.git`

### Шаг 2: Склонировать и добавить файлы

```bash
# Перейти в папку для проектов
cd ~/projects  # или где у тебя проекты

# Склонировать ПУСТОЙ репозиторий
git clone git@github.com:YOUR_USERNAME/media-concierge-bot.git
cd media-concierge-bot

# Распаковать скачанный архив
# Вариант 1: если архив в Downloads
unzip ~/Downloads/media-concierge-bot.zip
mv media-concierge-bot/* .
mv media-concierge-bot/.* . 2>/dev/null || true
rmdir media-concierge-bot

# Вариант 2: скопировать файлы вручную из чата

# Проверить что всё на месте
ls -la
# Должны быть: prd.json, ralph.sh, PROMPT.md, CLAUDE.md, .env.example, .gitignore

# Создать структуру директорий
mkdir -p src/{bot,ai,search,media,seedbox,sync,user} tests data docs
touch data/.gitkeep

# Сделать ralph.sh исполняемым
chmod +x ralph.sh
```

### Шаг 3: Получить API ключи

| Сервис | Где получить | Время |
|--------|--------------|-------|
| **Telegram Bot** | [@BotFather](https://t.me/botfather) → `/newbot` | 1 мин |
| **Anthropic API** | [console.anthropic.com](https://console.anthropic.com) → API Keys | 2 мин |
| **TMDB** | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) | 5 мин |
| **Trakt** | [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications) | 3 мин |
| **Kinopoisk** | [kinopoiskapiunofficial.tech](https://kinopoiskapiunofficial.tech/) | 2 мин |

**Для Trakt OAuth App:**
- Name: `Media Concierge Bot`
- Redirect URI: `urn:ietf:wg:oauth:2.0:oob`
- Permissions: оставить по умолчанию

### Шаг 4: Настроить .env файл

```bash
# Скопировать шаблон
cp .env.example .env

# Открыть и заполнить ключами
nano .env  # или: code .env / vim .env
```

⚠️ **Важно:** Файл `.env` содержит секреты и НЕ должен попадать в git (уже в .gitignore)

### Шаг 5: Сгенерировать ключ шифрования

```bash
# Установить cryptography если нет
pip3 install cryptography

# Сгенерировать ключ
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Скопировать вывод в ENCRYPTION_KEY в .env
```

### Шаг 6: Первый коммит

```bash
git add -A
git commit -m "chore: initial project setup with Ralph PRD"
git push -u origin main
```

---

## Запуск Ralph

### Базовый запуск

```bash
cd ~/projects/media-concierge-bot
./ralph.sh
```

### С параметрами

```bash
# Ограничить количество итераций
./ralph.sh --max-iterations 30

# Увеличить таймаут на итерацию (для сложных задач)
./ralph.sh --timeout 15

# Использовать Sonnet вместо Opus (экономнее)
./ralph.sh --model sonnet

# Настроить паузу при исчерпании квоты (по умолчанию 4 часа)
./ralph.sh --pause-hours 6

# Пробный запуск без выполнения
./ralph.sh --dry-run

# Комбинация
./ralph.sh --max-iterations 50 --model sonnet --pause-hours 4
```

### Ночной запуск (AFK)

```bash
# Вариант 1: nohup
nohup ./ralph.sh --max-iterations 100 --pause-hours 4 > ralph_output.log 2>&1 &

# Следить за прогрессом
tail -f ralph.log

# Вариант 2: tmux (рекомендуется)
tmux new -s ralph
./ralph.sh --max-iterations 100
# Ctrl+B, затем D — отключиться от сессии
# tmux attach -t ralph — вернуться
```

---

## Обработка квоты Claude Code

Скрипт автоматически обрабатывает исчерпание квоты:

1. **Обнаружение** — ловит ошибки `rate limit`, `quota`, `429`, `capacity`
2. **Retry** — делает 3 попытки с паузой 60 секунд
3. **Пауза** — если не помогло, спит `--pause-hours` часов (по умолчанию 4)
4. **Продолжение** — после паузы продолжает с того же места

```
⏸️  Quota/rate limit hit (attempt 1/3)
    Waiting 60 seconds before retry...
⏸️  Quota/rate limit hit (attempt 2/3)
    Waiting 60 seconds before retry...
⏸️  Quota/rate limit hit (attempt 3/3)
😴 Pausing for 4 hours (until ~18:30)...
   You can Ctrl+C to stop, then resume later with: ./ralph.sh
```

**Можно безопасно прервать** через `Ctrl+C` — состояние сохраняется в git и `progress.txt`.

---

## Мониторинг прогресса

### Статус задач

```bash
# Красивый вывод статуса
cat prd.json | python3 -c "
import json, sys
prd = json.load(sys.stdin)
stories = prd['userStories']
done = sum(1 for s in stories if s.get('passes'))
print(f'═══════════════════════════════════════')
print(f'  Прогресс: {done}/{len(stories)} задач')
print(f'═══════════════════════════════════════')
print()
for s in sorted(stories, key=lambda x: x['priority']):
    status = '✅' if s.get('passes') else '⏳'
    print(f'{status} [{s[\"id\"]}] {s[\"title\"]}')"
```

### Логи и история

```bash
# Последние логи Ralph
tail -50 ralph.log

# Прогресс и блокеры
cat progress.txt

# История коммитов
git log --oneline -15

# Что изменилось в последнем коммите
git show --stat HEAD
```

### Живой мониторинг

```bash
# В отдельном терминале
watch -n 10 'cat prd.json | jq "[.userStories[] | select(.passes == true)] | length" | xargs -I {} echo "Completed: {}/20"'
```

---

## Деплой на Koyeb

После завершения разработки (все `passes: true`):

### 1. Финальный коммит

```bash
git add -A
git commit -m "feat: complete bot implementation"
git push origin main
```

### 2. Создать сервис на Koyeb

1. [koyeb.com](https://koyeb.com) → Create Service
2. **Source:** GitHub
3. **Repository:** `media-concierge-bot`
4. **Builder:** Dockerfile
5. **Instance:** Free tier

### 3. Environment Variables

В Koyeb Dashboard → Service → Settings → Environment:

```
TELEGRAM_BOT_TOKEN=...
ANTHROPIC_API_KEY=...
TMDB_API_KEY=...
TRAKT_CLIENT_ID=...
TRAKT_CLIENT_SECRET=...
KINOPOISK_API_TOKEN=...
SEEDBOX_HOST=...
SEEDBOX_USER=...
SEEDBOX_PASSWORD=...
ENCRYPTION_KEY=...
WEBHOOK_URL=https://YOUR-APP.koyeb.app/webhook
LOG_LEVEL=INFO
```

### 4. Deploy & Configure Webhook

```bash
# После деплоя, настроить webhook Telegram
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://YOUR-APP.koyeb.app/webhook"
```

---

## Troubleshooting

### Ralph зацикливается на одной задаче

1. Проверить `progress.txt` — там записи о попытках
2. Уменьшить scope задачи в `prd.json`
3. Добавить подсказки в `CLAUDE.md`
4. Попробовать другую модель (`--model opus`)

### Claude Code не запускается

```bash
# Проверить установку
claude --version

# Проверить API ключ
echo $ANTHROPIC_API_KEY

# Тест простого запроса
claude -p "Say hello"
```

### Ошибки с Python зависимостями

```bash
# Создать venv
python3 -m venv venv
source venv/bin/activate

# Установить зависимости (после того как Ralph создаст pyproject.toml)
pip install -e ".[dev]"
```

### Бот не отвечает на Koyeb

1. Проверить логи в Koyeb Dashboard → Logs
2. Проверить `/health` endpoint: `curl https://YOUR-APP.koyeb.app/health`
3. Проверить webhook: Telegram → @BotFather → `/mybots` → API Token → Webhook info
4. Убедиться что все env переменные установлены

---

## Стоимость

**Приблизительно** для полной разработки (20 задач):

| Модель | Стоимость | Комментарий |
|--------|-----------|-------------|
| **Opus 4.5** | ~$30-50 | Быстрее, меньше итераций |
| **Sonnet 4.5** | ~$10-20 | Дешевле, может потребовать больше итераций |

💡 **Совет:** Начни с Sonnet для простых задач (INFRA-*), переключись на Opus для сложных (AI-*, CONV-*).

---

## Полезные ссылки

- 📖 [Ralph Wiggum документация](https://awesomeclaude.ai/ralph-wiggum)
- 📖 [Claude Code документация](https://docs.anthropic.com/claude-code)
- 💬 [Claude Developers Discord](https://discord.gg/anthropic)
- 🐛 Issues в репозитории проекта
