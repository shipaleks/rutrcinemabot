# Media Concierge Bot - Ralph Wiggum Development Loop

You are building a Media Concierge Telegram Bot. Follow these instructions carefully.

## Your Mission

Build a fully functional Telegram bot that helps users find and download movies/TV shows through natural language conversation with Claude AI integration.

## Files to Read First

1. **prd.json** - Contains all user stories with acceptance criteria and passes status
2. **progress.txt** - Contains learnings and blockers from previous iterations
3. **CLAUDE.md** - Project-specific instructions (if exists)

## Workflow for Each Iteration

### Step 1: Assess Current State
```bash
# Check what's been done
git log --oneline -10
# Check current test status
python -m pytest --tb=short 2>/dev/null || echo "Tests not yet configured"
# Check if project runs
python -c "from src.config import settings" 2>/dev/null || echo "Config not yet ready"
```

### Step 2: Select Next Task
- Open `prd.json`
- Find the **highest priority** story where `passes: false`
- ONLY work on ONE story per iteration
- Do not jump ahead to lower priority tasks

### Step 3: Implement the Story
- Follow the acceptance criteria exactly
- Write clean, typed Python code
- Add docstrings to all public functions
- Run linter: `ruff check . --fix`

### Step 4: Verify Completion
- Run all verification steps from the story
- Run tests: `pytest`
- Ensure no regressions

### Step 5: Update Progress
- If story passes: Update `prd.json` to set `passes: true`
- If blocked: Document in `progress.txt` what's blocking and why
- Commit changes with descriptive message

### Step 6: Exit Condition
- If ALL stories have `passes: true`, output: `<promise>COMPLETE</promise>`
- Otherwise, continue to next iteration

## Code Style Requirements

```python
# Use type hints everywhere
async def search_movie(query: str, quality: str | None = None) -> list[SearchResult]:
    """Search for movies on torrent trackers.
    
    Args:
        query: Movie name to search for
        quality: Optional quality filter (1080p, 4K, etc.)
    
    Returns:
        List of search results with magnet links
    """
    ...

# Use Pydantic for data models
class SearchResult(BaseModel):
    title: str
    size: str
    seeds: int
    magnet: str
    quality: str | None = None
```

## Project Structure

```
media-concierge-bot/
├── src/
│   ├── __init__.py
│   ├── config.py           # Pydantic settings
│   ├── logger.py           # Structlog setup
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── main.py         # Bot entry point
│   │   ├── handlers.py     # Message handlers
│   │   └── streaming.py    # sendMessageDraft logic
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── claude_client.py
│   │   ├── tools.py        # Tool definitions
│   │   └── prompts.py      # System prompts
│   ├── search/
│   │   ├── __init__.py
│   │   ├── rutracker.py
│   │   └── piratebay.py
│   ├── media/
│   │   ├── __init__.py
│   │   ├── tmdb.py
│   │   └── kinopoisk.py
│   ├── seedbox/
│   │   ├── __init__.py
│   │   └── client.py
│   ├── sync/
│   │   ├── __init__.py
│   │   └── trakt.py
│   └── user/
│       ├── __init__.py
│       └── storage.py
├── tests/
│   ├── __init__.py
│   ├── test_search.py
│   ├── test_media.py
│   └── test_user.py
├── data/
│   └── .gitkeep
├── docs/
│   ├── ARCHITECTURE.md
│   ├── API.md
│   └── DEPLOYMENT.md
├── pyproject.toml
├── Dockerfile
├── koyeb.yaml
├── .env.example
├── .gitignore
├── README.md
├── prd.json
├── progress.txt
└── CLAUDE.md
```

## Key Dependencies

```toml
[project]
dependencies = [
    "python-telegram-bot>=21.0",
    "anthropic>=0.40.0",
    "httpx>=0.27.0",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "structlog>=24.0",
    "aiosqlite>=0.20.0",
    "cryptography>=42.0",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5.0",
    "ruff>=0.6",
]
```

## Important Notes

- **Never commit secrets** - use .env and environment variables
- **Handle errors gracefully** - the bot should never crash
- **Use async everywhere** - python-telegram-bot 21.x is fully async
- **Cache expensive operations** - TMDB calls, search results
- **Log structured data** - use structlog with JSON output

## If Stuck

After 5 iterations on the same story:
1. Document the blocker in progress.txt
2. List what was attempted
3. Set a `blocked: true` field on the story
4. Move to the next story
5. Output `<promise>BLOCKED_ON_STORY_ID</promise>`

## Success Output

When ALL stories pass:
```
<promise>COMPLETE</promise>
```
