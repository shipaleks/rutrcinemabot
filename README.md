# 🎬 Media Concierge Bot

AI-powered Telegram bot for finding and downloading movies & TV shows through natural language conversation.

## Features

- 🤖 **Natural Language Search** - "Найди что-то похожее на Интерстеллар в 4K"
- 🔍 **Multi-tracker Search** - Rutracker, PirateBay
- 📊 **Smart Recommendations** - Based on your watch history and favorite directors
- 🔄 **Trakt Sync** - Automatic watchlist & watched history sync
- 📥 **Seedbox Integration** - One-click download to your seedbox
- 🌐 **Streaming Responses** - Real-time AI response generation

## Quick Start

### Prerequisites

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/claude-code)
- Git
- Docker (for deployment)

### Local Development

```bash
# Clone the repository
git clone https://github.com/yourusername/media-concierge-bot.git
cd media-concierge-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Copy and configure environment variables
cp .env.example .env
# Edit .env with your API keys

# Run the bot
python -m src.bot.main
```

### Using Ralph Wiggum (Autonomous Development)

This project is designed to be built autonomously using the [Ralph Wiggum technique](https://awesomeclaude.ai/ralph-wiggum).

```bash
# Make sure Claude Code is installed
npm install -g @anthropic-ai/claude-code

# Run Ralph
chmod +x ralph.sh
./ralph.sh --max-iterations 50
```

## API Keys Required

| Service | Where to get |
|---------|--------------|
| Telegram Bot | [@BotFather](https://t.me/botfather) |
| Anthropic (Claude) | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| TMDB | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) |
| Trakt | [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications) |
| Kinopoisk | [kinopoiskapiunofficial.tech](https://kinopoiskapiunofficial.tech/) |

## Documentation

- [Architecture Overview](docs/ARCHITECTURE.md) - System design and module structure
- [API Reference](docs/API.md) - Claude tools and their parameters
- [Deployment Guide](docs/DEPLOYMENT.md) - Step-by-step Koyeb deployment

## Deployment to Koyeb

1. Push your code to GitHub
2. Create Koyeb secrets (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md))
3. Create new Koyeb service from GitHub repo
4. Configure webhook URL
5. Deploy!

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for detailed instructions.

## Project Structure

```
media-concierge-bot/
├── src/
│   ├── bot/          # Telegram bot handlers
│   ├── ai/           # Claude AI integration
│   ├── search/       # Torrent trackers
│   ├── media/        # TMDB, Kinopoisk
│   ├── seedbox/      # Seedbox clients
│   ├── sync/         # Trakt integration
│   └── user/         # User storage
├── tests/            # Unit tests
├── docs/             # Documentation
├── prd.json          # Product requirements
├── PROMPT.md         # Ralph development prompt
└── ralph.sh          # Ralph runner script
```

## Development with Ralph

The `prd.json` file contains all user stories with acceptance criteria. Ralph will:

1. Read the PRD
2. Pick the highest priority incomplete task
3. Implement it
4. Run verification steps
5. Mark as complete
6. Commit changes
7. Repeat until done

Check progress:
```bash
# View remaining tasks
cat prd.json | jq '.userStories[] | select(.passes == false) | {id, title, priority}'
```

## License

MIT

## Contributing

PRs welcome! Please read the contribution guidelines first.
