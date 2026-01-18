# API Documentation

This document describes the Claude AI tools available in the Media Concierge Bot.

## Tool Overview

The bot uses Claude's tool_use feature to execute various operations. When a user sends a natural language query, Claude analyzes the request and decides which tools to call.

| Tool | Purpose | Required Parameters |
|------|---------|---------------------|
| `rutracker_search` | Search Russian torrent tracker | `query` |
| `piratebay_search` | Search PirateBay (fallback) | `query` |
| `tmdb_search` | Get movie/TV metadata | `query` |
| `tmdb_credits` | Get cast and crew info | `tmdb_id`, `media_type` |
| `kinopoisk_search` | Search Kinopoisk database | `query` |
| `get_user_profile` | Get user preferences | `user_id` |
| `seedbox_download` | Send torrent to seedbox | `magnet`, `user_id` |

## Tool Definitions

### rutracker_search

Search for movies and TV shows on the Rutracker torrent tracker. Returns releases with title, size, seeders count, and magnet link. Best for Russian content and releases with Russian audio.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Movie or TV show title to search |
| `quality` | string | No | Video quality filter: `720p`, `1080p`, `4K`, `2160p`, `HDR` |
| `category` | string | No | Content category: `movie`, `tv_show`, `anime`, `documentary` |

**Example:**
```json
{
  "query": "Дюна 2021",
  "quality": "4K",
  "category": "movie"
}
```

**Response:**
```json
{
  "results": [
    {
      "title": "Дюна / Dune (2021) 4K UHD BDRemux",
      "size": "65.2 GB",
      "size_bytes": 70017503559,
      "seeds": 125,
      "leeches": 23,
      "magnet": "magnet:?xt=urn:btih:...",
      "quality": "4K"
    }
  ],
  "total": 15,
  "query": "Дюна 2021"
}
```

---

### piratebay_search

Search for torrents on PirateBay. Use as a fallback when Rutracker is unavailable or when looking for international releases.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Movie or TV show title (English preferred) |
| `quality` | string | No | Video quality: `720p`, `1080p`, `4K`, `2160p` |
| `min_seeds` | integer | No | Minimum seeders to filter results (default: 5) |

**Example:**
```json
{
  "query": "Dune 2021",
  "quality": "1080p",
  "min_seeds": 10
}
```

**Response:**
```json
{
  "results": [
    {
      "title": "Dune.2021.1080p.BluRay.x264-SPARKS",
      "size": "12.4 GB",
      "size_bytes": 13313655603,
      "seeds": 542,
      "leeches": 89,
      "magnet": "magnet:?xt=urn:btih:...",
      "quality": "1080p"
    }
  ],
  "total": 23,
  "query": "Dune 2021"
}
```

---

### tmdb_search

Search for movies and TV shows in The Movie Database (TMDB). Returns metadata including title, year, description, rating, and poster. Use before searching trackers to get accurate information.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Movie or TV show title |
| `year` | integer | No | Release year for precise search |
| `media_type` | string | No | Content type: `movie` or `tv` |
| `language` | string | No | Results language (ISO 639-1, default: `ru-RU`) |

**Example:**
```json
{
  "query": "Inception",
  "year": 2010,
  "media_type": "movie",
  "language": "ru-RU"
}
```

**Response:**
```json
{
  "results": [
    {
      "id": 27205,
      "title": "Начало",
      "original_title": "Inception",
      "overview": "Кобб — талантливый вор, лучший из лучших в опасном искусстве извлечения...",
      "release_date": "2010-07-16",
      "vote_average": 8.4,
      "poster_path": "/qmDpIHrmpJINaRKAfWQfftjCdyi.jpg",
      "media_type": "movie"
    }
  ],
  "total": 1,
  "query": "Inception"
}
```

---

### tmdb_credits

Get cast and crew information for a movie or TV show from TMDB. Returns director, actors, writers, and other crew members.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `tmdb_id` | integer | Yes | TMDB ID of the movie or TV show |
| `media_type` | string | Yes | Content type: `movie` or `tv` |

**Example:**
```json
{
  "tmdb_id": 27205,
  "media_type": "movie"
}
```

**Response:**
```json
{
  "cast": [
    {
      "id": 6193,
      "name": "Leonardo DiCaprio",
      "character": "Dom Cobb",
      "order": 0,
      "profile_path": "/wo2hJpn04vbtmh0B9utCFdsQhxM.jpg"
    }
  ],
  "crew": [
    {
      "id": 525,
      "name": "Christopher Nolan",
      "job": "Director",
      "department": "Directing"
    }
  ],
  "directors": ["Christopher Nolan"],
  "writers": ["Christopher Nolan"],
  "top_cast": ["Leonardo DiCaprio", "Joseph Gordon-Levitt", "Elliot Page"]
}
```

---

### kinopoisk_search

Search for movies in the Kinopoisk database. Returns Kinopoisk rating, Russian description, and Russian release information.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Movie title (Russian or English) |
| `year` | integer | No | Release year for precise search |

**Example:**
```json
{
  "query": "Брат",
  "year": 1997
}
```

**Response:**
```json
{
  "results": [
    {
      "kinopoisk_id": 41519,
      "title": "Брат",
      "original_title": "Брат",
      "year": 1997,
      "rating_kinopoisk": 8.1,
      "rating_imdb": 7.8,
      "description": "Демобилизованный из армии Данила Багров приезжает в Петербург...",
      "genres": ["криминал", "драма"],
      "countries": ["Россия"],
      "poster_url": "https://kinopoiskapiunofficial.tech/images/..."
    }
  ],
  "total": 1,
  "query": "Брат"
}
```

---

### get_user_profile

Get user profile with preferences. Returns preferred video quality, audio language, and favorite genres.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `user_id` | integer | Yes | Telegram user ID |

**Example:**
```json
{
  "user_id": 123456789
}
```

**Response:**
```json
{
  "user_id": 123456789,
  "username": "johndoe",
  "preferences": {
    "video_quality": "1080p",
    "audio_language": "russian",
    "preferred_genres": ["sci-fi", "thriller", "drama"],
    "notifications_enabled": true
  },
  "created_at": "2024-01-15T10:30:00Z"
}
```

---

### seedbox_download

Send a magnet link to the user's seedbox for downloading. If seedbox is not configured, returns the magnet link directly to the user.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `magnet` | string | Yes | Magnet link for download |
| `name` | string | No | Release name for display |
| `user_id` | integer | Yes | Telegram user ID |

**Example:**
```json
{
  "magnet": "magnet:?xt=urn:btih:ABCDEF123456...",
  "name": "Dune.2021.4K.UHD.BluRay",
  "user_id": 123456789
}
```

**Response (seedbox configured):**
```json
{
  "status": "downloading",
  "message": "Торрент добавлен на seedbox",
  "torrent_hash": "ABCDEF123456",
  "name": "Dune.2021.4K.UHD.BluRay"
}
```

**Response (seedbox not configured):**
```json
{
  "status": "magnet_link",
  "message": "Seedbox не настроен. Используйте magnet-ссылку:",
  "magnet": "magnet:?xt=urn:btih:ABCDEF123456..."
}
```

## Tool Executor

The `ToolExecutor` class routes tool calls to appropriate handler functions.

### Usage

```python
from src.ai.tools import ToolExecutor, get_tool_definitions

# Create executor
executor = ToolExecutor()

# Register handlers
executor.register_handler("tmdb_search", tmdb_search_handler)
executor.register_handler("rutracker_search", rutracker_search_handler)

# Execute tool call
result = await executor.execute("tmdb_search", {"query": "Inception"})
```

### Integration with ClaudeClient

```python
from src.ai.claude_client import ClaudeClient
from src.ai.tools import get_tool_definitions, ToolExecutor

# Create executor with handlers
executor = ToolExecutor()
executor.register_handlers({
    "tmdb_search": handle_tmdb_search,
    "rutracker_search": handle_rutracker_search,
    # ... other handlers
})

# Create Claude client
client = ClaudeClient(
    tools=get_tool_definitions(),
    tool_executor=executor,
)

# Send message - Claude will automatically use tools
response = await client.send_message("Найди Дюну в 4K")
```

## Error Handling

All tools return JSON-formatted error messages when issues occur:

```json
{
  "error": true,
  "error_type": "SearchError",
  "message": "Rutracker is blocked in your region",
  "suggestion": "Try using piratebay_search instead"
}
```

Common error types:
- `SearchError`: Search operation failed
- `NotFoundError`: Resource not found
- `RateLimitError`: API rate limit exceeded
- `AuthError`: Authentication failed
- `ConnectionError`: Network connection failed

## Rate Limits

| Service | Rate Limit | Notes |
|---------|------------|-------|
| TMDB | 40 req/10s | Caching reduces actual requests |
| Kinopoisk | 500 req/day | Unofficial API limitation |
| Rutracker | Best effort | HTML scraping, be respectful |
| PirateBay | Best effort | HTML scraping, mirrors available |
