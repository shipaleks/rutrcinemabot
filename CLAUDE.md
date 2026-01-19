# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Media Concierge Bot — Telegram bot for finding and downloading movies/TV shows with AI-powered natural language interface. Uses Claude API for understanding user queries and tool_use for searching torrent trackers.

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
```

## Architecture

```
User Message → Bot Module → Claude API (with tools) → Tool Executor → Search/Media Modules → Response
```

**Key modules:**

- `src/bot/` — Telegram handlers, streaming, onboarding
  - `main.py` — Entry point, webhook/polling modes
  - `conversation.py` — Natural language processing, tool handlers
  - `streaming.py` — Progressive message updates with typing indicator

- `src/ai/` — Claude API integration
  - `claude_client.py` — Async client with streaming and tool_use support
  - `tools.py` — 7 tool definitions (rutracker_search, tmdb_search, etc.)
  - `prompts.py` — System prompts with user preferences injection

- `src/search/` — Torrent tracker scrapers (Rutracker, PirateBay)
- `src/media/` — Metadata APIs (TMDB, Kinopoisk) with caching
- `src/seedbox/` — Torrent client APIs (Transmission, qBittorrent, Deluge)
- `src/user/` — SQLite storage with Fernet encryption for credentials

**Tool flow:** Claude receives user message → returns `tool_use` → `ToolExecutor` routes to handler → result sent back to Claude → final response generated.

## Code Conventions

- **Async everywhere** — All I/O operations must be async (httpx, aiosqlite)
- **Structured logging** — Use `structlog` with context: `logger.info("event_name", key=value)`
- **Error handling** — Never crash the bot; catch exceptions, log, reply with user-friendly message
- **Config** — All settings via environment variables through `src/config.py` (pydantic-settings)
- **Secrets** — Use `SecretStr` type, never log tokens/passwords

## Git Commits

Format: `type(scope): description`

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

## Required Environment Variables

- `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `TMDB_API_KEY`, `KINOPOISK_API_TOKEN`, `ENCRYPTION_KEY`
- Optional: `SEEDBOX_HOST`, `SEEDBOX_USER`, `SEEDBOX_PASSWORD`

## Deployment

Koyeb with Docker. Two ports: 8000 (webhook), 8080 (health check).
Webhook URL: `https://<app-name>.koyeb.app/webhook`
