# Architecture Overview

This document describes the architecture of the Media Concierge Bot.

## System Architecture

```
                                    +-------------------+
                                    |   Telegram API    |
                                    +--------+----------+
                                             |
                                             | Webhook/Polling
                                             v
+-----------------------------------------------------------------------------------+
|                              Media Concierge Bot                                   |
|                                                                                   |
|  +-------------+     +----------------+     +------------------+                  |
|  |   Bot       |     |   AI Module    |     |  Search Module   |                  |
|  |  Module     |<--->|  (Claude API)  |<--->|  (Trackers)      |                  |
|  +-------------+     +----------------+     +------------------+                  |
|        |                    |                       |                             |
|        v                    v                       v                             |
|  +-------------+     +----------------+     +------------------+                  |
|  | User        |     |  Tool          |     |  Media Module    |                  |
|  | Storage     |     |  Executor      |     |  (TMDB/KP)       |                  |
|  | (SQLite)    |     |                |     +------------------+                  |
|  +-------------+     +----------------+            |                             |
|        |                    |                      v                             |
|        v                    v              +------------------+                  |
|  +-------------+     +----------------+   |  Seedbox Client   |                  |
|  | Encryption  |     |  Streaming     |   |  (Optional)       |                  |
|  | (Fernet)    |     |  Handler       |   +------------------+                  |
|  +-------------+     +----------------+                                          |
|                                                                                   |
+-----------------------------------------------------------------------------------+
```

## Module Overview

### 1. Bot Module (`src/bot/`)

The bot module handles all Telegram interactions.

```
src/bot/
├── __init__.py          # Module exports
├── main.py              # Application entry point, webhook/polling
├── handlers.py          # Command handlers (/start, /help)
├── onboarding.py        # User onboarding flow with inline buttons
├── conversation.py      # Natural language message processing
└── streaming.py         # Progressive message updates
```

**Key Components:**
- **main.py**: Initializes the bot, sets up handlers, manages webhook/polling modes
- **handlers.py**: Handles basic commands (/start, /help)
- **onboarding.py**: Multi-step user onboarding with preference selection
- **conversation.py**: Processes natural language queries via Claude AI
- **streaming.py**: Implements progressive message updates with typing indicators

### 2. AI Module (`src/ai/`)

The AI module integrates with Anthropic's Claude API.

```
src/ai/
├── __init__.py          # Module exports
├── claude_client.py     # Async Claude API client
├── tools.py             # Tool definitions for Claude
└── prompts.py           # System prompts
```

**Key Components:**
- **ClaudeClient**: Async client with streaming support
- **ToolExecutor**: Routes tool calls to handler functions
- **ConversationContext**: Manages conversation history

**Tool Flow:**
```
User Message
    |
    v
ClaudeClient.send_message()
    |
    v
Claude API (may return tool_use)
    |
    v
ToolExecutor.__call__(tool_name, input)
    |
    v
Handler function (e.g., rutracker_search)
    |
    v
Tool result sent back to Claude
    |
    v
Claude generates final response
```

### 3. Search Module (`src/search/`)

The search module provides torrent tracker integration.

```
src/search/
├── __init__.py          # Module exports
├── rutracker.py         # Rutracker scraper
└── piratebay.py         # PirateBay scraper
```

**Features:**
- Async HTTP requests with httpx
- HTML parsing with BeautifulSoup
- Quality detection from titles
- Magnet link extraction
- Mirror fallback for blocked sites

### 4. Media Module (`src/media/`)

The media module provides movie/TV metadata.

```
src/media/
├── __init__.py          # Module exports
├── tmdb.py              # TMDB API client
└── kinopoisk.py         # Kinopoisk API client
```

**Features:**
- Movie/TV search and details
- Cast and crew information
- Recommendations
- In-memory caching with TTL

### 5. Seedbox Module (`src/seedbox/`)

The seedbox module provides torrent client integration.

```
src/seedbox/
├── __init__.py          # Module exports
└── client.py            # Multi-client support
```

**Supported Clients:**
- Transmission (RPC API)
- qBittorrent (Web API)
- Deluge (JSON-RPC API)

**Graceful Degradation:**
When seedbox is not configured, magnet links are returned directly to users.

### 6. User Module (`src/user/`)

The user module manages user data and preferences.

```
src/user/
├── __init__.py          # Module exports
└── storage.py           # SQLite storage with encryption
```

**Database Schema:**
```sql
-- User profiles
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    telegram_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    language_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Encrypted credentials (OAuth tokens)
CREATE TABLE credentials (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    credential_type TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- User preferences
CREATE TABLE preferences (
    id INTEGER PRIMARY KEY,
    user_id INTEGER UNIQUE NOT NULL,
    video_quality TEXT DEFAULT '1080p',
    audio_language TEXT DEFAULT 'russian',
    preferred_genres TEXT DEFAULT '[]',
    notifications_enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Watch history
CREATE TABLE watched (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id INTEGER,
    kinopoisk_id INTEGER,
    title TEXT NOT NULL,
    watched_at TEXT NOT NULL,
    rating INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

## Data Flow

### 1. User Search Query

```
1. User sends: "Найди Дюну в 4K"
2. Bot receives message via webhook
3. conversation.py loads user preferences from SQLite
4. System prompt is built with user preferences
5. Message sent to Claude API with tools
6. Claude returns tool_use: rutracker_search(query="Дюна", quality="4K")
7. ToolExecutor calls handle_rutracker_search()
8. RutrackerClient searches and returns results
9. Results sent back to Claude
10. Claude formats response with recommendations
11. Bot sends response with inline download buttons
```

### 2. Download Request

```
1. User clicks download button
2. Callback contains result_id
3. Bot retrieves cached search result
4. If seedbox configured:
   - Send magnet to seedbox
   - Notify user of download start
5. If no seedbox:
   - Send magnet link directly
```

## Deployment Architecture

```
                    Internet
                       |
                       v
              +----------------+
              |    Koyeb       |
              |  (Docker)      |
              +----------------+
                  |        |
          Port 8000    Port 8080
          (Webhook)    (Health)
                  |        |
                  v        v
        +----------+  +----------+
        | Telegram |  | Koyeb    |
        | Updates  |  | Health   |
        +----------+  | Check    |
                      +----------+
```

**Ports:**
- **8000**: Telegram webhook endpoint
- **8080**: Health check endpoint for Koyeb

**Environment:**
- All secrets stored in Koyeb Secrets
- SQLite database in persistent volume
- Logs in JSON format for Koyeb log aggregation

## Security Considerations

1. **Token Security**: All API tokens stored as SecretStr, never logged
2. **Credential Encryption**: OAuth tokens encrypted with Fernet
3. **Input Validation**: All user inputs validated before processing
4. **Error Handling**: Errors logged without sensitive data
5. **No Hardcoded Secrets**: All configuration via environment variables

## Performance Considerations

1. **Async Everywhere**: All I/O operations are async
2. **Connection Pooling**: httpx client reused within context managers
3. **Caching**: TMDB and Kinopoisk responses cached with TTL
4. **Rate Limiting**: Streaming updates rate-limited to avoid Telegram limits
5. **Context Trimming**: Conversation history limited to prevent token overflow
