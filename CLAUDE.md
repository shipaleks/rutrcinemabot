# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Media Concierge Bot — Telegram bot for finding and downloading movies/TV shows with AI-powered natural language interface. Uses Claude API with tool_use for searching torrent trackers (Rutracker, PirateBay), fetching metadata (TMDB, Kinopoisk), and managing user preferences.

## Commands

```bash
# Install dependencies
pip install -e ".[dev]"

# Run linter and formatter
ruff check . --fix && ruff format .

# Run all tests
pytest -v

# Run single test file
pytest tests/test_rutracker.py -v

# Run single test
pytest tests/test_rutracker.py::TestRutrackerClient::test_search_returns_results -v

# Run tests with coverage
pytest --cov=src --cov-report=term

# Run bot locally (polling mode)
python -m src.bot.main

# Build Docker image
docker build -t media-concierge-bot .

# Deploy to Koyeb
koyeb service redeploy <app-name>/<service-name>
```

## Architecture

```
User Message → Bot Module → Claude API (with tools) → ToolExecutor → Search/Media Modules → Response
```

### Core Flow

1. **Telegram handler** (`src/bot/conversation.py`) receives user message
2. **ClaudeClient** (`src/ai/claude_client.py`) sends message with tool definitions to Claude API
3. Claude returns `tool_use` blocks specifying which tools to call
4. **ToolExecutor** (`src/ai/tools.py`) routes to appropriate handler
5. Tool results sent back to Claude for final response generation
6. **StreamingMessage** (`src/bot/streaming.py`) progressively updates Telegram message

### Key Modules

- `src/bot/` — Telegram handlers and conversation flow
  - `main.py` — Entry point, webhook/polling modes, health check server
  - `conversation.py` — Tool handler implementations, download callbacks
  - `streaming.py` — Progressive message updates with typing indicator
  - `onboarding.py` — User setup wizard (quality, genres, Letterboxd import)
  - `rutracker_auth.py` — Per-user Rutracker credentials flow

- `src/ai/` — Claude API integration
  - `claude_client.py` — Async streaming client with tool_use support
  - `tools.py` — 20+ tool definitions (search, profile, watchlist, ratings)
  - `prompts.py` — System prompts with user profile injection

- `src/search/` — Torrent tracker clients (Rutracker, PirateBay)
- `src/media/` — Metadata APIs (TMDB, Kinopoisk) with response caching
- `src/seedbox/` — Torrent clients (Transmission, qBittorrent, Deluge)
  - `client.py` — `SeedboxClient` base class (async context manager), `DelugeClient` etc.
- `src/user/` — Storage and profile management
  - `storage.py` — Dual-backend (Postgres production, SQLite dev) with Fernet encryption
  - `profile.py` — Markdown-based user profiles for Claude context
- `src/services/` — External integrations (Letterboxd export parser)
- `src/monitoring/` — Background scheduler for release tracking and torrent monitoring
  - `scheduler.py` — APScheduler jobs (release checks, torrent monitor, Deluge cleanup, follow-ups, push notifications)
  - `torrent_monitor.py` — Checks Deluge every 60s for completed downloads, notifies users
- `src/bot/seedbox_auth.py` — Per-user seedbox credentials conversation handler
- `src/bot/sync_api.py` — Webhook endpoints for VM sync daemon

### Seedbox Sync Flow

```
User downloads → Magnet sent to Deluge → hash tracked in synced_torrents (status: downloading)
  → TorrentMonitor detects completion → status: seeding → push "Скачано! Копирую домой..."
  → sets sync_needed flag → VM daemon polls GET /api/sync/pending
  → rsync from seedbox to NAS → sorts into Кино/Сериалы → POST /api/sync/complete
  → push "✅ Готово к просмотру!" → daily cleanup removes completed torrents from Deluge
```

Key: all seedbox clients (`DelugeClient` etc.) must be used as `async with` context managers.

### Storage Pattern

Use `get_storage()` context manager for all database operations:
```python
async with get_storage() as storage:
    user = await storage.get_user_by_telegram_id(telegram_id)
    await storage.store_credential(user.id, CredentialType.RUTRACKER_USERNAME, value)
```

## Code Conventions

- **Async everywhere** — All I/O uses httpx (HTTP), asyncpg (Postgres), aiosqlite (SQLite)
- **Structured logging** — `structlog` with context: `logger.info("event_name", key=value)`
- **Config** — Environment variables via `src/config.py` (pydantic-settings with SecretStr)
- **Error handling** — Catch exceptions, log with context, reply with user-friendly message in Russian

## Git Commits

Format: `type(scope): description`

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

## Required Environment Variables

- `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `TMDB_API_KEY`, `KINOPOISK_API_TOKEN`, `ENCRYPTION_KEY`
- `DATABASE_URL` — Postgres connection string (required for production persistence)
- Optional: `SEEDBOX_HOST`, `SEEDBOX_USER`, `SEEDBOX_PASSWORD` (global fallback; users can set their own via `/seedbox`)
- Optional: `RUTRACKER_USERNAME`, `RUTRACKER_PASSWORD` (global fallback)
- Optional: `SYNC_API_KEY` — shared secret for VM sync daemon API

## Deployment

Koyeb with Docker. Two ports: 8000 (webhook), 8080 (health check + sync API).
Webhook URL: `https://<app-name>.koyeb.app/webhook`
Koyeb strips `/api` prefix when routing to port 8080 — handlers accept both `/api/sync/*` and `/sync/*`.

Without `DATABASE_URL`, data is lost on redeploy (SQLite in ephemeral container).

VM sync daemon runs on Freebox VM (`scripts/sync_daemon.sh`) as a systemd service, polling the bot's `/api/sync/pending` endpoint.
