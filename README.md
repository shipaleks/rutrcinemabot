# Media Concierge Bot

AI-powered Telegram bot for finding and downloading movies & TV shows through natural language conversation.

## Features

- **Natural Language Search** — ask in plain Russian: "Найди что-то похожее на Интерстеллар в 4K"
- **Multi-tracker Search** — Rutracker, PirateBay searched in parallel
- **Smart Recommendations** — based on your watch history, favorite directors, and genres
- **Memory System** — the bot remembers your preferences, watch context, and conversation history
- **Seedbox Integration** — one-click download to Transmission, qBittorrent, or Deluge
- **Streaming Responses** — real-time AI response generation in Telegram
- **Watchlist & Ratings** — track what you want to watch and rate what you've seen
- **Release Monitoring** — get notified when a specific release appears on trackers

## Quick Start

### Prerequisites

- Python 3.11+
- API keys (see below)
- Docker (for deployment)

### Local Development

```bash
# Clone the repository
git clone https://github.com/shipaleks/media-concierge-bot.git
cd media-concierge-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Copy and configure environment variables
cp .env.example .env
# Edit .env with your API keys

# Run the bot (polling mode)
python -m src.bot.main
```

## API Keys Required

| Service | Where to get |
|---------|--------------|
| Telegram Bot | [@BotFather](https://t.me/botfather) |
| Anthropic (Claude) | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| TMDB | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) |
| Kinopoisk | [kinopoiskapiunofficial.tech](https://kinopoiskapiunofficial.tech/) |

See `.env.example` for the full list of configuration options.

## Architecture

```
User Message -> Telegram Bot -> Claude API (with tools) -> Tool Executor -> Search/Media Modules -> Response
```

The bot uses Claude's tool_use capability to orchestrate searches across torrent trackers and metadata APIs. Claude decides which tools to call based on the user's natural language request.

### Key Modules

- `src/bot/` — Telegram handlers, streaming responses, onboarding
- `src/ai/` — Claude API client, tool definitions, system prompts
- `src/search/` — Torrent tracker clients (Rutracker, PirateBay)
- `src/media/` — Metadata APIs (TMDB, Kinopoisk) with response caching
- `src/seedbox/` — Torrent client integrations (Transmission, qBittorrent, Deluge)
- `src/user/` — User storage (Postgres/SQLite) with encrypted credentials
- `src/monitoring/` — Background jobs for release tracking and download monitoring

## Documentation

- [Architecture Overview](docs/ARCHITECTURE.md) — system design and module structure
- [API Reference](docs/API.md) — Claude tools and their parameters
- [Deployment Guide](docs/DEPLOYMENT.md) — step-by-step Koyeb deployment
- [Features](docs/FEATURES.md) — detailed feature documentation

## Deployment

The bot is designed to run on [Koyeb](https://www.koyeb.com/) with Docker:

1. Push your code to GitHub
2. Create Koyeb secrets (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md))
3. Create a new Koyeb service from the GitHub repo
4. Configure the webhook URL
5. Deploy

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for detailed instructions.

## Project Structure

```
media-concierge-bot/
├── src/
│   ├── bot/          # Telegram bot handlers
│   ├── ai/           # Claude AI integration
│   ├── search/       # Torrent tracker clients
│   ├── media/        # TMDB, Kinopoisk
│   ├── seedbox/      # Seedbox clients
│   ├── monitoring/   # Background scheduler
│   └── user/         # User storage & profiles
├── tests/            # Unit tests
├── docs/             # Documentation
├── scripts/          # Deployment scripts
└── Dockerfile
```

## Development

```bash
# Run linter
ruff check . --fix && ruff format .

# Run tests
pytest -v

# Run tests with coverage
pytest --cov=src --cov-report=term
```

## License

[MIT](LICENSE)
