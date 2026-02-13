# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Media Concierge Bot — Telegram bot for finding and downloading movies/TV shows with AI-powered natural language interface. Uses Claude API with tool_use for searching torrent trackers (Rutracker, PirateBay, TorAPI), fetching metadata (TMDB, Kinopoisk, OMDB), and managing user preferences with a MemGPT-style memory system.

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
  - `main.py` — Entry point, webhook/polling modes, raw HTTP health check server on port 8080 (handles `/health`, `/api/sync/*`)
  - `conversation.py` — Natural language handler, tool handler implementations, download callbacks, search result caching
  - `handlers.py` — Command handlers (`/help`, `/profile`, `/settings`, `/reset_profile`, `/digest`), error handling
  - `streaming.py` — Progressive message updates with markdown→HTML conversion
  - `onboarding.py` — User setup wizard (quality, genres, Letterboxd import, Rutracker auth)
  - `rutracker_auth.py` — Per-user Rutracker credentials conversation flow
  - `seedbox_auth.py` — Per-user seedbox credentials conversation handler
  - `model_settings.py` — `/model` command: per-user AI model selection (Haiku 4.5, Sonnet 4.5, Opus 4.6) and thinking budget (Off/Low 1K/Medium 5K/High 10K tokens)
  - `entity_cards.py` — Movie/TV/person entity cards with photos, captions, and inline keyboards for deep links
  - `library.py` — `/library` command: NAS media library browser with paginated inline keyboard navigation (reads index pushed by VM script)
  - `sync_api.py` — HTTP endpoints for VM sync daemon (`/api/sync/pending`, `/api/sync/complete`, `/api/sync/library-index`)

- `src/ai/` — Claude API integration
  - `claude_client.py` — Async streaming client with tool_use support, conversation history with token trimming (<30 messages, <80K tokens)
  - `tools.py` — 20+ tool definitions in JSON schema (search, profile, memory, watchlist, ratings, monitors, etc.)
  - `prompts.py` — System prompts with MemGPT-style memory hierarchy, user profile injection, blocklist support

- `src/search/` — Torrent tracker clients
  - `rutracker.py` — Rutracker search with quality filtering, magnet extraction, captcha/block error handling
  - `piratebay.py` — PirateBay fallback search for international content, seeds filtering
  - `torapi.py` — TorAPI unified API client for Russian trackers (RuTracker, Kinozal, RuTor, NoNameClub) — no auth or VPN required, clean JSON via `https://torapi.vercel.app`

- `src/media/` — Metadata APIs with response caching
  - `tmdb.py` — TMDB client: movie/TV/person search, credits, batch entity search, image URLs
  - `kinopoisk.py` — Kinopoisk client: Russian ratings, box office, release info
  - `omdb.py` — OMDB client: IMDB ratings, Rotten Tomatoes scores, Metascores

- `src/seedbox/` — Torrent clients (Transmission, qBittorrent, Deluge)
  - `client.py` — `SeedboxClient` base class (async context manager), `DelugeClient` (JSON-RPC), `TransmissionClient` (RPC), `QBittorrentClient` (Web API)
  - `__init__.py` — `send_magnet_to_seedbox()`, `send_magnet_to_user_seedbox()` convenience functions

- `src/user/` — Storage, memory, and profile management
  - `storage.py` — Dual-backend (Postgres production, SQLite dev) with Fernet encryption. Tables: users, preferences, watched_items, monitors, core_memory_blocks, sessions, downloads, memory_notes, blocklist, synced_torrents, library_index, etc.
  - `memory.py` — MemGPT-style memory hierarchy: `CoreMemoryManager` (8 block types with auto-compaction at 70% capacity), `SessionManager` (conversation tracking with 30-min timeout), `LearningDetector` (pattern extraction from viewing history), `MemoryArchiver` (automatic archival of old notes)
  - `profile.py` — `ProfileManager`: renders user profile as markdown for Claude system prompt context

- `src/services/` — External integrations
  - `letterboxd.py` — Letterboxd OAuth 2.0 client: import/export watchlist, ratings, diary entries (requires API approval)
  - `letterboxd_export.py` — Letterboxd ZIP export parser (`LetterboxdExportAnalysis`)
  - `letterboxd_rss.py` — Letterboxd RSS feed monitoring for activity changes
  - `news.py` — News source integrations (RSS, web search)

- `src/monitoring/` — Background scheduler for release tracking and notifications
  - `scheduler.py` — APScheduler: release checks (every 6h, smart intervals), torrent monitor (every 5 min), daily Deluge cleanup, digest delivery, proactive push notifications
  - `checker.py` — `ReleaseChecker`: smart check intervals based on release date (2h after release → 24h upcoming → 72h far future), auto-download support
  - `torrent_monitor.py` — `TorrentMonitor`: checks Deluge every 60s for completed downloads, notifies users via DB status tracking
  - `news_digest.py` — Personalized cinema news digests: daily (19:00, 3-5 topics) and weekly (Tue/Fri 19:00, 7-10 topics), Claude-powered generation

- `src/config.py` — Environment variables via pydantic-settings (`Settings` class with `SecretStr` for sensitive fields)
- `src/logger.py` — Structured logging setup: JSON for production, colored console for dev

### MemGPT-Style Memory System

The bot uses a MemGPT-inspired memory hierarchy for personalized conversations:

**Core Memory** (always in-context, agent-editable via tools):
- `identity` — User's basic info (name, language, system-managed)
- `preferences` — Content preferences (genres, directors, quality)
- `watch_context` — Current viewing context (what they're watching now)
- `active_context` — Ongoing conversations, pending searches
- `style` — Communication style notes
- `instructions` — User-specific instructions for the bot
- `blocklist` — Content/topics to avoid
- `learnings` — Auto-detected patterns from viewing history

**Recall Memory** — Searchable session summaries and notes (on-demand via tools)

**Archival Memory** — Long-term storage with automatic pruning

Compaction triggers at 70% block capacity. Claude can read/write memory blocks via `update_core_memory`, `create_memory_note`, `search_memory_notes` tools.

### Seedbox Sync Flow

```
User downloads → Magnet sent to Deluge → hash tracked in synced_torrents (status: downloading)
  → TorrentMonitor detects completion → status: seeding → push "Скачано! Копирую домой..."
  → sets sync_needed flag → VM daemon polls GET /api/sync/pending
  → rsync from seedbox to NAS → sorts into Кино/Сериалы → POST /api/sync/complete
  → push "✅ Готово к просмотру!" → daily cleanup removes completed torrents from Deluge
```

**Important:** The VM sync daemon (`scripts/sync_seedbox.sh`) sends POST `/api/sync/complete` with `filename` and `local_path` but **no `torrent_hash`** — it only has the cleaned-up series/movie name after sorting. The API resolves the user via fuzzy `torrent_name` match in `synced_torrents` (see `get_user_by_torrent_name`).

Key: all seedbox clients (`DelugeClient` etc.) must be used as `async with` context managers.

### Storage Pattern

Use `get_storage()` context manager for all database operations:
```python
async with get_storage() as storage:
    user = await storage.get_user_by_telegram_id(telegram_id)
    await storage.store_credential(user.id, CredentialType.RUTRACKER_USERNAME, value)
```

The storage has two implementations: `PostgresStorage` (production) and `SQLiteStorage` (dev). Abstract methods are declared in `BaseStorage` base class. When adding a new query method, implement it in **all three places**: abstract, SQLite, Postgres. Use `ILIKE` for Postgres and `LIKE ... COLLATE NOCASE` for SQLite for case-insensitive matching.

## Telegram Message Formatting

**Use HTML, not Markdown.** Streaming responses convert markdown→HTML via `_markdown_to_telegram_html()` in `streaming.py`. Non-streaming messages (torrent cards, callbacks in `conversation.py`) use `parse_mode="HTML"` directly.

Why: Telegram Markdown v1 is unreliable with links containing special characters, parentheses in URLs, underscores in names, etc. Broken markdown triggers a fallback to plain text (no formatting at all). HTML mode is stable.

When Claude generates `[text](url)` links, they are converted to `<a href="url">text</a>` before sending. Entity deep links follow the format: `https://t.me/<bot_username>?start=m_693134` (m_=movie, t_=tv, p_=person). Bot username is configured via `BOT_USERNAME` env var.

## Common Pitfalls & Lessons Learned

### Data flow mismatches between components
The most frequent bug category. When one component produces data and another consumes it, verify the **exact field names, formats, and presence** across the boundary. Examples:
- Sync daemon sends cleaned names with spaces; DB stores original names with dots → substring match fails
- API handler returns `telegram_id` but omits `filename`/`local_path` → notification sends but with empty content
- API returns `notify: True` without `telegram_id` → caller has no target user → notification silently skipped

### Notification delivery
When adding any push notification flow, always trace the full path from trigger to `bot.send_message()` and verify:
1. The `telegram_id` is actually present in the response/data passed between functions
2. The bot instance is available (`_bot_instance` is only set in webhook mode, not polling)
3. Errors are logged, not silently swallowed — add warning logs for "notification skipped" cases

### Tool handlers need user_id
Every tool handler in `conversation.py` must receive `user_id` (telegram_id). This has been a recurring issue — multiple tool handlers were missing `user_id` injection, causing them to silently fail or return wrong data.

### Claude API content blocks
When sending messages back to Claude API, strip any internal fields added during processing (e.g. `parsed_output`). Claude API rejects unknown fields in content blocks. Similarly, `thinking` / `redacted_thinking` blocks must be preserved correctly in conversation history.

### Thinking mode
When `thinking` is enabled, continuation calls (after tool_use) must also include thinking config. But if prior messages lack thinking blocks, disable it to avoid API errors.

### APScheduler gotchas
Scheduling async tasks at startup is tricky — `scheduler.start()` must happen before adding jobs, and the event loop must be running. One-shot cleanup jobs at startup went through 6+ fix iterations before working correctly (ended up using `loop.create_task` after scheduler start).

### Storage abstract methods
When adding a new storage method, you must implement it in **three places**: `BaseStorage` (abstract), `SQLiteStorage`, and `PostgresStorage`. Forgetting one leads to `TypeError` at runtime.

## Code Conventions

- **Async everywhere** — All I/O uses httpx (HTTP), asyncpg (Postgres), aiosqlite (SQLite)
- **Structured logging** — `structlog` with context: `logger.info("event_name", key=value)`
- **Config** — Environment variables via `src/config.py` (pydantic-settings with SecretStr)
- **Error handling** — Catch exceptions, log with context, reply with user-friendly message in Russian
- **Python 3.11+** — Uses `X | Y` union syntax, `datetime.UTC`, etc.
- **Type hints** — Strict mypy config; use `TYPE_CHECKING` for import-only types to avoid circular imports
- **Pydantic models** — Data classes use pydantic `BaseModel` with field aliases where needed

## Linter & Formatter Configuration

Ruff is used for both linting and formatting (configured in `pyproject.toml`):
- Line length: 100
- Target: Python 3.11
- Enabled rules: pycodestyle (E/W), pyflakes (F), isort (I), pep8-naming (N), pyupgrade (UP), flake8-bugbear (B), flake8-comprehensions (C4), flake8-simplify (SIM), flake8-return (RET)
- Ignored: E501 (line length handled by formatter), B008 (function calls in defaults)
- `__init__.py` files: F401 (unused imports) ignored
- Test files: S101 (assert) ignored

Mypy runs in strict mode with all strict checks enabled.

## Testing

- **Framework**: pytest 8.0+ with pytest-asyncio (auto mode)
- **Test files**: 11 test files in `tests/` covering all major modules
- **Async tests**: `asyncio_mode = "auto"` — no need for `@pytest.mark.asyncio` decorator
- **Fixtures**: Use `@pytest.fixture` for shared setup; mock external APIs with `unittest.mock.AsyncMock`
- **Coverage**: `pytest --cov=src --cov-report=term` — source is `src/`, omits test files
- **Markers**: `--strict-markers` and `--strict-config` enforced

Test files:
- `test_rutracker.py`, `test_piratebay.py` — Tracker search clients
- `test_tmdb.py`, `test_kinopoisk.py` — Media metadata APIs
- `test_claude_client.py`, `test_tools.py` — Claude AI integration
- `test_conversation.py` — Conversation handlers and callbacks
- `test_streaming.py` — Markdown→HTML streaming conversion
- `test_seedbox.py` — Seedbox client integration (Deluge, Transmission, qBittorrent)
- `test_onboarding.py` — User onboarding flow
- `test_user_storage.py` — Storage layer (SQLite and Postgres)

## Git Commits

Format: `type(scope): description`

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

## Environment Variables

### Required
- `TELEGRAM_BOT_TOKEN` — Telegram bot token from @BotFather
- `ANTHROPIC_API_KEY` — Anthropic API key for Claude
- `TMDB_API_KEY` — The Movie Database API key
- `KINOPOISK_API_TOKEN` — Kinopoisk unofficial API token
- `ENCRYPTION_KEY` — Fernet encryption key for sensitive user data

### Database
- `DATABASE_URL` — Postgres connection string (required for production persistence; without it, falls back to ephemeral SQLite)

### Optional: Tracker & Seedbox
- `RUTRACKER_USERNAME`, `RUTRACKER_PASSWORD` — Global fallback Rutracker credentials (users can set their own via `/rutracker`)
- `SEEDBOX_HOST`, `SEEDBOX_USER`, `SEEDBOX_PASSWORD` — Global fallback seedbox credentials (users can set their own via `/seedbox`)
- `SYNC_API_KEY` — Shared secret for VM sync daemon API authentication

### Optional: External Services
- `OMDB_API_KEY` — OMDB API key for IMDB/Rotten Tomatoes ratings (falls back to demo key)
- `LETTERBOXD_CLIENT_ID`, `LETTERBOXD_CLIENT_SECRET` — Letterboxd API credentials (requires API approval)
- `LETTERBOXD_REDIRECT_URI` — Letterboxd OAuth redirect URI (default: `https://localhost/callback`)
- `YANDEX_SEARCH_API_KEY`, `YANDEX_SEARCH_FOLDER_ID` — Yandex Search API for web search

### Application
- `BOT_USERNAME` — Telegram bot username without @ (default: `trmoviebot`), used for entity deep links
- `WEBHOOK_URL` — Webhook URL for Telegram (auto-configured on Koyeb)
- `PORT` — Webhook server port (default: 8000)
- `HEALTH_PORT` — Health check server port (default: 8080)
- `LOG_LEVEL` — Logging level (default: INFO)
- `ENVIRONMENT` — `development` or `production` (default: production)
- `CACHE_TTL` — Cache TTL in seconds (default: 3600)

## Project Structure

```
├── src/
│   ├── ai/                     # Claude API integration
│   │   ├── claude_client.py    # Async streaming client with tool_use
│   │   ├── tools.py            # 20+ tool definitions
│   │   └── prompts.py          # System prompts with memory injection
│   ├── bot/                    # Telegram bot handlers
│   │   ├── main.py             # Entry point (webhook/polling)
│   │   ├── conversation.py     # NL handler, tool execution, callbacks
│   │   ├── handlers.py         # /help, /profile, /settings commands
│   │   ├── streaming.py        # Progressive message streaming
│   │   ├── onboarding.py       # Setup wizard
│   │   ├── rutracker_auth.py   # Rutracker credentials flow
│   │   ├── seedbox_auth.py     # Seedbox credentials flow
│   │   ├── model_settings.py   # /model command (model + thinking)
│   │   ├── entity_cards.py     # Movie/TV/person entity cards
│   │   ├── library.py          # /library NAS browser
│   │   └── sync_api.py         # VM sync daemon endpoints
│   ├── search/                 # Torrent tracker clients
│   │   ├── rutracker.py        # Rutracker (direct scraping)
│   │   ├── piratebay.py        # PirateBay (international)
│   │   └── torapi.py           # TorAPI unified API (no auth)
│   ├── media/                  # Metadata APIs
│   │   ├── tmdb.py             # TMDB (movies, TV, people)
│   │   ├── kinopoisk.py        # Kinopoisk (Russian ratings)
│   │   └── omdb.py             # OMDB (IMDB, RT, Metacritic)
│   ├── seedbox/                # Torrent client integration
│   │   ├── client.py           # Deluge, Transmission, qBittorrent
│   │   └── __init__.py         # Convenience send_magnet functions
│   ├── user/                   # User data management
│   │   ├── storage.py          # Dual-backend DB (Postgres/SQLite)
│   │   ├── memory.py           # MemGPT memory hierarchy
│   │   └── profile.py          # Profile markdown renderer
│   ├── services/               # External service integrations
│   │   ├── letterboxd.py       # Letterboxd OAuth client
│   │   ├── letterboxd_export.py # Letterboxd ZIP export parser
│   │   ├── letterboxd_rss.py   # Letterboxd RSS monitoring
│   │   └── news.py             # News source integrations
│   ├── monitoring/             # Background jobs
│   │   ├── scheduler.py        # APScheduler job management
│   │   ├── checker.py          # Release detection logic
│   │   ├── torrent_monitor.py  # Deluge completion detector
│   │   └── news_digest.py      # Personalized news digests
│   ├── config.py               # pydantic-settings configuration
│   └── logger.py               # structlog setup
├── tests/                      # 11 test files (pytest + pytest-asyncio)
├── scripts/                    # Deployment & utility scripts
│   ├── sync_seedbox.sh         # Rsync from seedbox to NAS
│   ├── sync_daemon.sh          # Wrapper to run sync as daemon
│   ├── sync_daemon.service     # Systemd service file
│   ├── library_indexer.py      # NAS library index pusher
│   └── config.env.template     # Script configuration template
├── docs/                       # Extended documentation
│   ├── ARCHITECTURE.md         # System design
│   ├── API.md                  # Tool definitions & parameters
│   ├── FEATURES.md             # Feature documentation
│   ├── DEPLOYMENT.md           # Koyeb deployment guide
│   ├── SEEDBOX_SETUP.md        # Seedbox client setup
│   └── FREEBOX_VM_SETUP.md     # VM sync daemon setup
├── Dockerfile                  # Multi-stage build (Python 3.11-slim)
├── pyproject.toml              # Dependencies, ruff, pytest, mypy config
├── koyeb.yaml                  # Koyeb deployment config
└── .env.example                # Environment variables template
```

## Deployment

Koyeb with Docker. Two ports: 8000 (webhook), 8080 (health check + sync API).
Webhook URL: `https://<app-name>.koyeb.app/webhook`
Koyeb strips `/api` prefix when routing to port 8080 — handlers accept both `/api/sync/*` and `/sync/*`.

Docker uses multi-stage build (builder + runtime) with Python 3.11-slim, non-root user (`botuser`), and health check every 30s against port 8080.

Without `DATABASE_URL`, data is lost on redeploy (SQLite in ephemeral container).

The optional VM sync daemon (`scripts/sync_daemon.sh`) can run as a systemd service, polling the bot's `/api/sync/pending` endpoint.
