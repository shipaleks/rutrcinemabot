# CLAUDE.md - Project Instructions for Claude Code

## Project: Media Concierge Bot

A Telegram bot for finding and downloading movies/TV shows with AI-powered natural language interface.

## Quick Commands

```bash
# Install dependencies
pip install -e ".[dev]"

# Run linter
ruff check . --fix
ruff format .

# Run tests
pytest -v

# Run bot locally
python -m src.bot.main

# Build Docker image
docker build -t media-concierge-bot .

# Check types (optional)
mypy src/
```

## Code Conventions

### Async Everywhere
All I/O operations must be async:
```python
# ✅ Good
async def fetch_movie(movie_id: int) -> Movie:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"/movie/{movie_id}")
        return Movie.model_validate(response.json())

# ❌ Bad - blocking I/O
def fetch_movie(movie_id: int) -> Movie:
    response = requests.get(f"/movie/{movie_id}")
    return Movie(**response.json())
```

### Error Handling
Never let errors crash the bot:
```python
# ✅ Good
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = await process_query(update.message.text)
        await update.message.reply_text(result)
    except SearchError as e:
        logger.warning("search_failed", error=str(e))
        await update.message.reply_text(f"Поиск не удался: {e}")
    except Exception as e:
        logger.exception("unexpected_error")
        await update.message.reply_text("Произошла ошибка. Попробуйте позже.")
```

### Logging
Use structlog with context:
```python
import structlog
logger = structlog.get_logger()

# ✅ Good
logger.info("search_completed", query=query, results_count=len(results), duration_ms=elapsed)

# ❌ Bad
print(f"Search for {query} returned {len(results)} results")
```

### Configuration
All config through environment variables:
```python
# src/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    telegram_bot_token: str
    anthropic_api_key: str
    tmdb_api_key: str
    
    # Optional with defaults
    log_level: str = "INFO"
    cache_ttl: int = 3600
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
```

## File Naming

- Python files: `snake_case.py`
- Classes: `PascalCase`
- Functions/methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Test files: `test_*.py`

## Git Commits

Format: `type(scope): description`

Types:
- `feat`: New feature
- `fix`: Bug fix
- `refactor`: Code refactoring
- `test`: Adding tests
- `docs`: Documentation
- `chore`: Maintenance

Examples:
```
feat(search): add Rutracker parser
fix(bot): handle empty search results
test(tmdb): add tests for credits endpoint
docs: update deployment instructions
```

## Testing

Use pytest with async support:
```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_search_returns_results():
    with patch("src.search.rutracker.fetch_page", new_callable=AsyncMock) as mock:
        mock.return_value = SAMPLE_HTML
        results = await search("Dune")
        assert len(results) > 0
        assert results[0].magnet.startswith("magnet:")
```

## Secrets Management

NEVER commit secrets. Use:
1. `.env` file locally (gitignored)
2. Koyeb secrets in production

Required env vars:
- `TELEGRAM_BOT_TOKEN` - from @BotFather
- `ANTHROPIC_API_KEY` - from console.anthropic.com
- `TMDB_API_KEY` - from themoviedb.org
- `KINOPOISK_API_TOKEN` - from kinopoiskapiunofficial.tech
- `ENCRYPTION_KEY` - for encrypting user data (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)

Optional env vars (for seedbox integration):
- `SEEDBOX_HOST` - your seedbox URL
- `SEEDBOX_USER` - seedbox username
- `SEEDBOX_PASSWORD` - seedbox password

## Koyeb Deployment

The bot runs as a web service with webhook:
1. Koyeb calls health endpoint
2. Telegram sends updates to webhook URL
3. Bot processes and responds

Webhook URL pattern: `https://<app-name>-<org>.koyeb.app/webhook`
