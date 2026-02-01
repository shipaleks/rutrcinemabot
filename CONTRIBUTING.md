# Contributing

Contributions are welcome! Here's how to get started.

## Setup

```bash
git clone https://github.com/shipaleks/media-concierge-bot.git
cd media-concierge-bot
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Fill in your API keys in .env
```

## Development Workflow

1. Create a branch for your changes
2. Make your changes
3. Run linting: `ruff check . --fix && ruff format .`
4. Run tests: `pytest -v`
5. Commit with conventional format: `type(scope): description`
   - Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
6. Open a pull request

## Code Conventions

- **Async everywhere** — all I/O uses httpx, asyncpg, aiosqlite
- **Structured logging** — use `structlog` with context: `logger.info("event_name", key=value)`
- **Config** — environment variables via `src/config.py` (pydantic-settings)
- **Error handling** — catch exceptions, log with context, reply in Russian
- **HTML for Telegram** — use `parse_mode="HTML"`, not Markdown

## Storage

When adding new database queries, implement in all three places:
1. `UserStorage` abstract base class
2. `SQLiteStorage` (dev)
3. `PostgresStorage` (production)

Use `ILIKE` for Postgres and `LIKE ... COLLATE NOCASE` for SQLite.

## Tests

```bash
pytest -v                          # all tests
pytest tests/test_rutracker.py -v  # single file
pytest --cov=src --cov-report=term # with coverage
```
