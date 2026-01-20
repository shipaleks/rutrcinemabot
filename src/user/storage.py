"""User profile storage with SQLite/Postgres support and encrypted credentials.

This module provides:
- Dual-backend storage (Postgres for production, SQLite for development)
- Encrypted storage for OAuth tokens and sensitive credentials
- CRUD operations for user profiles, preferences, and watch history
- Database migrations for schema evolution
- Extended tables for profiles, watchlist, ratings, monitors, crew_stats

Usage:
    # With DATABASE_URL set (Postgres):
    async with get_storage() as storage:
        user = await storage.create_user(telegram_id=123456)

    # Without DATABASE_URL (SQLite fallback):
    async with get_storage() as storage:
        user = await storage.create_user(telegram_id=123456)
"""

import base64
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import structlog
from cryptography.fernet import Fernet
from pydantic import BaseModel, Field

logger = structlog.get_logger()


# =============================================================================
# Data Models
# =============================================================================


class CredentialType(str, Enum):
    """Types of stored credentials."""

    TRAKT_TOKEN = "trakt_token"
    TRAKT_REFRESH = "trakt_refresh"
    SEEDBOX_PASSWORD = "seedbox_password"
    RUTRACKER_SESSION = "rutracker_session"
    RUTRACKER_USERNAME = "rutracker_username"
    RUTRACKER_PASSWORD = "rutracker_password"
    LETTERBOXD_TOKEN = "letterboxd_token"
    LETTERBOXD_REFRESH = "letterboxd_refresh"
    CUSTOM = "custom"


class User(BaseModel):
    """User profile model."""

    id: int
    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = Field(default="ru")
    is_active: bool = True
    created_at: datetime
    updated_at: datetime

    @property
    def display_name(self) -> str:
        """Get user's display name."""
        if self.first_name:
            if self.last_name:
                return f"{self.first_name} {self.last_name}"
            return self.first_name
        if self.username:
            return f"@{self.username}"
        return f"User {self.telegram_id}"


class Credential(BaseModel):
    """Encrypted credential model."""

    id: int
    user_id: int
    credential_type: CredentialType
    encrypted_value: str  # Base64-encoded encrypted data
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Preference(BaseModel):
    """User preferences model."""

    id: int
    user_id: int
    video_quality: str | None = Field(default="1080p")
    audio_language: str | None = Field(default="ru")
    subtitle_language: str | None = None
    preferred_genres: list[str] = Field(default_factory=list)
    excluded_genres: list[str] = Field(default_factory=list)
    auto_download: bool = False
    notification_enabled: bool = True
    # AI model settings
    claude_model: str = Field(default="claude-sonnet-4-5-20250929")
    thinking_budget: int = Field(default=0)  # 0 = disabled, >0 = max thinking tokens
    created_at: datetime
    updated_at: datetime


class WatchedItem(BaseModel):
    """Watched content item model."""

    id: int
    user_id: int
    media_type: str  # "movie" or "tv"
    tmdb_id: int | None = None
    kinopoisk_id: int | None = None
    title: str
    year: int | None = None
    season: int | None = None  # For TV shows
    episode: int | None = None  # For TV shows
    rating: float | None = None  # User's rating (1-10)
    review: str | None = None  # User's review text
    watched_at: datetime
    created_at: datetime


class WatchlistItem(BaseModel):
    """Watchlist item model."""

    id: int
    user_id: int
    tmdb_id: int | None = None
    kinopoisk_id: int | None = None
    media_type: str  # "movie" or "tv"
    title: str
    year: int | None = None
    poster_url: str | None = None
    priority: int = 0  # Higher = more priority
    notes: str | None = None
    added_at: datetime


class UserProfile(BaseModel):
    """Extended user profile with markdown content."""

    id: int
    user_id: int
    profile_md: str  # Full markdown profile content
    updated_at: datetime


class Monitor(BaseModel):
    """Release monitor model."""

    id: int
    user_id: int
    title: str
    tmdb_id: int | None = None
    media_type: str = "movie"
    quality: str = "1080p"
    auto_download: bool = False
    status: str = "active"  # active, found, cancelled
    found_at: datetime | None = None
    release_date: datetime | None = None  # Expected release date from TMDB
    last_checked: datetime | None = None  # Last time this monitor was checked
    created_at: datetime


class CrewStat(BaseModel):
    """Crew statistics model."""

    id: int
    user_id: int
    person_id: int  # TMDB person ID
    person_name: str
    role: str  # director, cinematographer, composer, writer, actor
    films_count: int = 0
    total_rating: int = 0
    film_ids: list[int] = Field(default_factory=list)
    updated_at: datetime

    @property
    def avg_rating(self) -> float:
        """Calculate average rating."""
        if self.films_count == 0:
            return 0.0
        return self.total_rating / self.films_count


class BlocklistItem(BaseModel):
    """Blocklist item model."""

    id: int
    user_id: int
    block_type: str  # "title", "franchise", "genre", "person"
    block_value: str  # The value to block
    block_level: str = "dont_recommend"  # "dont_recommend" or "never_mention"
    notes: str | None = None  # e.g., "horror except psychological"
    created_at: datetime


# =============================================================================
# Encryption Helper
# =============================================================================


class EncryptionHelper:
    """Helper class for encrypting and decrypting sensitive data."""

    def __init__(self, key: str | bytes):
        """Initialize with Fernet encryption key.

        Args:
            key: Fernet key as string or bytes
        """
        if isinstance(key, str):
            key = key.encode()
        self._fernet = Fernet(key)

    def encrypt(self, data: str) -> str:
        """Encrypt string data and return base64-encoded result.

        Args:
            data: Plain text to encrypt

        Returns:
            Base64-encoded encrypted data
        """
        encrypted = self._fernet.encrypt(data.encode())
        return base64.b64encode(encrypted).decode()

    def decrypt(self, encrypted_data: str) -> str:
        """Decrypt base64-encoded encrypted data.

        Args:
            encrypted_data: Base64-encoded encrypted string

        Returns:
            Decrypted plain text

        Raises:
            InvalidToken: If decryption fails (wrong key or corrupted data)
        """
        encrypted = base64.b64decode(encrypted_data.encode())
        return self._fernet.decrypt(encrypted).decode()


# =============================================================================
# Abstract Storage Interface
# =============================================================================


class BaseStorage(ABC):
    """Abstract base class for storage backends."""

    def __init__(self, encryption_key: str | bytes | None = None):
        """Initialize storage with optional encryption.

        Args:
            encryption_key: Optional Fernet key for encrypting credentials
        """
        self._encryption: EncryptionHelper | None = None
        if encryption_key:
            self._encryption = EncryptionHelper(encryption_key)

    @abstractmethod
    async def connect(self) -> None:
        """Open database connection and initialize schema."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close database connection."""
        pass

    async def __aenter__(self) -> "BaseStorage":
        """Open database connection and apply migrations."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: object | None,
    ) -> None:
        """Close database connection."""
        await self.close()

    # -------------------------------------------------------------------------
    # User CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = "ru",
    ) -> User:
        """Create a new user."""
        pass

    @abstractmethod
    async def get_user(self, user_id: int) -> User | None:
        """Get user by internal ID."""
        pass

    @abstractmethod
    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        """Get user by Telegram ID."""
        pass

    @abstractmethod
    async def update_user(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_active: bool | None = None,
    ) -> User | None:
        """Update user profile."""
        pass

    @abstractmethod
    async def delete_user(self, user_id: int) -> bool:
        """Delete user and all related data."""
        pass

    @abstractmethod
    async def list_users(
        self,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[User]:
        """List users with pagination."""
        pass

    async def get_or_create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = "ru",
    ) -> tuple[User, bool]:
        """Get existing user or create new one.

        Returns:
            Tuple of (User, created) where created is True if new user
        """
        user = await self.get_user_by_telegram_id(telegram_id)

        if user:
            # Update user info if changed
            if any(
                [
                    username != user.username,
                    first_name != user.first_name,
                    last_name != user.last_name,
                ]
            ):
                updated_user = await self.update_user(
                    user.id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                )
                if updated_user:
                    user = updated_user
            return user, False

        user = await self.create_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
        )
        return user, True

    # -------------------------------------------------------------------------
    # Credentials CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def store_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
        value: str,
        expires_at: datetime | None = None,
    ) -> Credential:
        """Store an encrypted credential."""
        pass

    @abstractmethod
    async def get_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
    ) -> str | None:
        """Get decrypted credential value."""
        pass

    @abstractmethod
    async def delete_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
    ) -> bool:
        """Delete a credential."""
        pass

    @abstractmethod
    async def list_credentials(self, user_id: int) -> list[CredentialType]:
        """List credential types for a user (not values)."""
        pass

    # -------------------------------------------------------------------------
    # Letterboxd Token Helpers
    # -------------------------------------------------------------------------

    async def get_letterboxd_token(self, user_id: int) -> dict[str, Any] | None:
        """Get Letterboxd OAuth token for a user.

        Args:
            user_id: Internal user ID

        Returns:
            Dict with access_token, refresh_token, etc., or None if not configured
        """
        token_json = await self.get_credential(user_id, CredentialType.LETTERBOXD_TOKEN)
        if token_json:
            try:
                return json.loads(token_json)
            except json.JSONDecodeError:
                return None
        return None

    async def save_letterboxd_token(
        self,
        user_id: int,
        access_token: str,
        refresh_token: str | None = None,
        expires_in: int = 3600,
    ) -> None:
        """Save Letterboxd OAuth token for a user.

        Args:
            user_id: Internal user ID
            access_token: OAuth access token
            refresh_token: Optional refresh token
            expires_in: Token expiration in seconds
        """
        token_data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
        }
        await self.store_credential(
            user_id=user_id,
            credential_type=CredentialType.LETTERBOXD_TOKEN,
            value=json.dumps(token_data),
        )

    async def delete_letterboxd_token(self, user_id: int) -> bool:
        """Delete Letterboxd OAuth token for a user.

        Args:
            user_id: Internal user ID

        Returns:
            True if token was deleted
        """
        return await self.delete_credential(user_id, CredentialType.LETTERBOXD_TOKEN)

    # -------------------------------------------------------------------------
    # Preferences CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def get_preferences(self, user_id: int) -> Preference | None:
        """Get user preferences."""
        pass

    @abstractmethod
    async def update_preferences(
        self,
        user_id: int,
        video_quality: str | None = None,
        audio_language: str | None = None,
        subtitle_language: str | None = None,
        preferred_genres: list[str] | None = None,
        excluded_genres: list[str] | None = None,
        auto_download: bool | None = None,
        notification_enabled: bool | None = None,
        claude_model: str | None = None,
        thinking_budget: int | None = None,
    ) -> Preference | None:
        """Update user preferences."""
        pass

    # -------------------------------------------------------------------------
    # Watched Items CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def add_watched(
        self,
        user_id: int,
        media_type: str,
        title: str,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
        rating: float | None = None,
        review: str | None = None,
        watched_at: datetime | None = None,
    ) -> WatchedItem:
        """Add item to watch history."""
        pass

    @abstractmethod
    async def get_watched(
        self,
        user_id: int,
        media_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WatchedItem]:
        """Get user's watch history."""
        pass

    @abstractmethod
    async def is_watched(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Check if user has watched content."""
        pass

    @abstractmethod
    async def delete_watched(self, item_id: int) -> bool:
        """Delete item from watch history."""
        pass

    @abstractmethod
    async def update_watched_rating(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        rating: float | None = None,
        review: str | None = None,
    ) -> WatchedItem | None:
        """Update rating/review for watched item."""
        pass

    # -------------------------------------------------------------------------
    # Watchlist CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def add_to_watchlist(
        self,
        user_id: int,
        media_type: str,
        title: str,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        year: int | None = None,
        poster_url: str | None = None,
        priority: int = 0,
        notes: str | None = None,
    ) -> WatchlistItem:
        """Add item to watchlist."""
        pass

    @abstractmethod
    async def get_watchlist(
        self,
        user_id: int,
        media_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WatchlistItem]:
        """Get user's watchlist."""
        pass

    @abstractmethod
    async def remove_from_watchlist(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Remove item from watchlist."""
        pass

    @abstractmethod
    async def is_in_watchlist(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Check if item is in watchlist."""
        pass

    # -------------------------------------------------------------------------
    # Profile CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def get_profile(self, user_id: int) -> UserProfile | None:
        """Get user's markdown profile."""
        pass

    @abstractmethod
    async def update_profile(
        self,
        user_id: int,
        profile_md: str,
    ) -> UserProfile:
        """Update user's markdown profile."""
        pass

    # -------------------------------------------------------------------------
    # Monitor CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def create_monitor(
        self,
        user_id: int,
        title: str,
        tmdb_id: int | None = None,
        media_type: str = "movie",
        quality: str = "1080p",
        auto_download: bool = False,
        release_date: datetime | None = None,
    ) -> Monitor:
        """Create a release monitor."""
        pass

    @abstractmethod
    async def get_monitors(
        self,
        user_id: int | None = None,
        status: str | None = None,
    ) -> list[Monitor]:
        """Get monitors, optionally filtered by user or status."""
        pass

    @abstractmethod
    async def update_monitor_status(
        self,
        monitor_id: int,
        status: str,
        found_at: datetime | None = None,
    ) -> Monitor | None:
        """Update monitor status."""
        pass

    @abstractmethod
    async def delete_monitor(self, monitor_id: int) -> bool:
        """Delete a monitor."""
        pass

    @abstractmethod
    async def get_monitor(self, monitor_id: int) -> Monitor | None:
        """Get a single monitor by ID."""
        pass

    @abstractmethod
    async def update_monitor_last_checked(self, monitor_id: int) -> None:
        """Update the last_checked timestamp for a monitor."""
        pass

    @abstractmethod
    async def get_all_active_monitors(self) -> list[Monitor]:
        """Get all active monitors across all users."""
        pass

    @abstractmethod
    async def get_all_users(self, limit: int = 1000) -> list[User]:
        """Get all users (with limit for safety)."""
        pass

    # -------------------------------------------------------------------------
    # Crew Stats CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def update_crew_stat(
        self,
        user_id: int,
        person_id: int,
        person_name: str,
        role: str,
        film_id: int,
        rating: int,
    ) -> CrewStat:
        """Update crew statistics when user watches/rates a film."""
        pass

    @abstractmethod
    async def get_crew_stats(
        self,
        user_id: int,
        role: str | None = None,
        min_films: int = 1,
        limit: int = 20,
    ) -> list[CrewStat]:
        """Get crew statistics for a user."""
        pass

    # -------------------------------------------------------------------------
    # Blocklist CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def add_to_blocklist(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
        block_level: str = "dont_recommend",
        notes: str | None = None,
    ) -> BlocklistItem:
        """Add item to blocklist."""
        pass

    @abstractmethod
    async def get_blocklist(
        self,
        user_id: int,
        block_type: str | None = None,
    ) -> list[BlocklistItem]:
        """Get user's blocklist."""
        pass

    @abstractmethod
    async def remove_from_blocklist(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
    ) -> bool:
        """Remove item from blocklist."""
        pass

    @abstractmethod
    async def is_blocked(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
    ) -> bool:
        """Check if item is blocked."""
        pass


# =============================================================================
# SQLite Implementation
# =============================================================================


class SQLiteStorage(BaseStorage):
    """SQLite-based user profile storage with encryption support."""

    def __init__(
        self,
        db_path: str | Path,
        encryption_key: str | bytes | None = None,
    ):
        """Initialize SQLite storage.

        Args:
            db_path: Path to SQLite database file
            encryption_key: Optional Fernet key for encrypting credentials
        """
        super().__init__(encryption_key)
        self._db_path = Path(db_path)
        self._db: Any = None

    async def connect(self) -> None:
        """Open database connection and initialize schema."""
        import aiosqlite

        # Ensure parent directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # Enable foreign keys
        await self._db.execute("PRAGMA foreign_keys = ON")

        # Apply migrations
        await self._apply_migrations()

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> Any:
        """Get active database connection."""
        if self._db is None:
            raise RuntimeError("Database not connected. Use 'async with' or call connect()")
        return self._db

    async def _apply_migrations(self) -> None:
        """Apply database migrations."""
        migrations = [
            # Migration 1: Users table
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT DEFAULT 'ru',
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
            """,
            # Migration 2: Credentials table
            """
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                credential_type TEXT NOT NULL,
                encrypted_value TEXT NOT NULL,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, credential_type)
            );
            CREATE INDEX IF NOT EXISTS idx_credentials_user_id ON credentials(user_id);
            """,
            # Migration 3: Preferences table
            """
            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                video_quality TEXT DEFAULT '1080p',
                audio_language TEXT DEFAULT 'ru',
                subtitle_language TEXT,
                preferred_genres TEXT DEFAULT '[]',
                excluded_genres TEXT DEFAULT '[]',
                auto_download INTEGER DEFAULT 0,
                notification_enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_preferences_user_id ON preferences(user_id);
            """,
            # Migration 4: Watched table
            """
            CREATE TABLE IF NOT EXISTS watched (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                tmdb_id INTEGER,
                kinopoisk_id INTEGER,
                title TEXT NOT NULL,
                year INTEGER,
                season INTEGER,
                episode INTEGER,
                rating REAL,
                review TEXT,
                watched_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_watched_user_id ON watched(user_id);
            CREATE INDEX IF NOT EXISTS idx_watched_tmdb_id ON watched(tmdb_id);
            CREATE INDEX IF NOT EXISTS idx_watched_kinopoisk_id ON watched(kinopoisk_id);
            """,
            # Migration 5: Migrations tracking table
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            """,
            # Migration 6: Watchlist table
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tmdb_id INTEGER,
                kinopoisk_id INTEGER,
                media_type TEXT NOT NULL,
                title TEXT NOT NULL,
                year INTEGER,
                poster_url TEXT,
                priority INTEGER DEFAULT 0,
                notes TEXT,
                added_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_watchlist_user_id ON watchlist(user_id);
            """,
            # Migration 7: Profiles table
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                profile_md TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_profiles_user_id ON profiles(user_id);
            """,
            # Migration 8: Monitors table
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                tmdb_id INTEGER,
                media_type TEXT DEFAULT 'movie',
                quality TEXT DEFAULT '1080p',
                auto_download INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                found_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_monitors_user_id ON monitors(user_id);
            CREATE INDEX IF NOT EXISTS idx_monitors_status ON monitors(status);
            """,
            # Migration 9: Crew stats table
            """
            CREATE TABLE IF NOT EXISTS crew_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                person_name TEXT NOT NULL,
                role TEXT NOT NULL,
                films_count INTEGER DEFAULT 0,
                total_rating INTEGER DEFAULT 0,
                film_ids TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, person_id, role)
            );
            CREATE INDEX IF NOT EXISTS idx_crew_stats_user_id ON crew_stats(user_id);
            """,
            # Migration 10: Blocklist table
            """
            CREATE TABLE IF NOT EXISTS blocklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                block_type TEXT NOT NULL,
                block_value TEXT NOT NULL,
                block_level TEXT DEFAULT 'dont_recommend',
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, block_type, block_value)
            );
            CREATE INDEX IF NOT EXISTS idx_blocklist_user_id ON blocklist(user_id);
            """,
            # Migration 11: Add AI model settings to preferences
            """
            ALTER TABLE preferences ADD COLUMN claude_model TEXT DEFAULT 'claude-sonnet-4-5-20250929';
            ALTER TABLE preferences ADD COLUMN thinking_budget INTEGER DEFAULT 0;
            """,
        ]

        # Get current migration version
        try:
            cursor = await self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='_migrations'"
            )
            if await cursor.fetchone() is None:
                current_version = 0
            else:
                cursor = await self.db.execute("SELECT MAX(version) FROM _migrations")
                row = await cursor.fetchone()
                current_version = row[0] if row and row[0] else 0
        except Exception:
            current_version = 0

        # Apply pending migrations
        for i, sql in enumerate(migrations, 1):
            if i <= current_version:
                continue

            logger.info("applying_migration", version=i)
            await self.db.executescript(sql)

            # Record migration
            if i >= 5:  # Only record after _migrations table exists
                await self.db.execute(
                    "INSERT OR IGNORE INTO _migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (i, f"migration_{i}", datetime.now(UTC).isoformat()),
                )

            await self.db.commit()
            logger.info("migration_applied", version=i)

    # -------------------------------------------------------------------------
    # User CRUD Implementation
    # -------------------------------------------------------------------------

    async def create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = "ru",
    ) -> User:
        """Create a new user."""
        now = datetime.now(UTC).isoformat()

        cursor = await self.db.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name,
                             language_code, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (telegram_id, username, first_name, last_name, language_code, now, now),
        )
        await self.db.commit()

        user_id = cursor.lastrowid
        if user_id is None:
            raise RuntimeError("Failed to create user")

        # Create default preferences
        await self.db.execute(
            "INSERT INTO preferences (user_id, created_at, updated_at) VALUES (?, ?, ?)",
            (user_id, now, now),
        )
        await self.db.commit()

        logger.info("user_created", user_id=user_id, telegram_id=telegram_id)

        return User(
            id=user_id,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            is_active=True,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    async def get_user(self, user_id: int) -> User | None:
        """Get user by internal ID."""
        cursor = await self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        return self._row_to_user(row) if row else None

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        """Get user by Telegram ID."""
        cursor = await self.db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        return self._row_to_user(row) if row else None

    async def update_user(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_active: bool | None = None,
    ) -> User | None:
        """Update user profile."""
        updates: list[str] = []
        params: list[Any] = []

        if username is not None:
            updates.append("username = ?")
            params.append(username)
        if first_name is not None:
            updates.append("first_name = ?")
            params.append(first_name)
        if last_name is not None:
            updates.append("last_name = ?")
            params.append(last_name)
        if language_code is not None:
            updates.append("language_code = ?")
            params.append(language_code)
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if is_active else 0)

        if not updates:
            return await self.get_user(user_id)

        updates.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(user_id)

        await self.db.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await self.db.commit()

        return await self.get_user(user_id)

    async def delete_user(self, user_id: int) -> bool:
        """Delete user and all related data."""
        cursor = await self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await self.db.commit()
        deleted = cursor.rowcount > 0 if cursor.rowcount else False
        if deleted:
            logger.info("user_deleted", user_id=user_id)
        return deleted

    async def list_users(
        self,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[User]:
        """List users with pagination."""
        query = "SELECT * FROM users"
        params: list[int] = []

        if active_only:
            query += " WHERE is_active = 1"

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_user(row) for row in rows]

    def _row_to_user(self, row: Any) -> User:
        """Convert database row to User model."""
        return User(
            id=row["id"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            language_code=row["language_code"],
            is_active=bool(row["is_active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # -------------------------------------------------------------------------
    # Credentials CRUD Implementation
    # -------------------------------------------------------------------------

    async def store_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
        value: str,
        expires_at: datetime | None = None,
    ) -> Credential:
        """Store an encrypted credential."""
        if self._encryption is None:
            raise RuntimeError("Encryption key not configured")

        encrypted_value = self._encryption.encrypt(value)
        now = datetime.now(UTC).isoformat()
        expires_str = expires_at.isoformat() if expires_at else None

        cursor = await self.db.execute(
            """
            INSERT INTO credentials
                (user_id, credential_type, encrypted_value, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, credential_type) DO UPDATE SET
                encrypted_value = excluded.encrypted_value,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (user_id, credential_type.value, encrypted_value, expires_str, now, now),
        )
        await self.db.commit()

        credential_id = cursor.lastrowid
        if credential_id is None:
            cursor = await self.db.execute(
                "SELECT id FROM credentials WHERE user_id = ? AND credential_type = ?",
                (user_id, credential_type.value),
            )
            row = await cursor.fetchone()
            credential_id = row["id"] if row else 0

        logger.info(
            "credential_stored",
            user_id=user_id,
            credential_type=credential_type.value,
        )

        return Credential(
            id=credential_id,
            user_id=user_id,
            credential_type=credential_type,
            encrypted_value=encrypted_value,
            expires_at=expires_at,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    async def get_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
    ) -> str | None:
        """Get decrypted credential value."""
        if self._encryption is None:
            raise RuntimeError("Encryption key not configured")

        cursor = await self.db.execute(
            "SELECT encrypted_value, expires_at FROM credentials WHERE user_id = ? AND credential_type = ?",
            (user_id, credential_type.value),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        if row["expires_at"]:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at < datetime.now(UTC):
                logger.warning(
                    "credential_expired",
                    user_id=user_id,
                    credential_type=credential_type.value,
                )
                return None

        return self._encryption.decrypt(row["encrypted_value"])

    async def delete_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
    ) -> bool:
        """Delete a credential."""
        cursor = await self.db.execute(
            "DELETE FROM credentials WHERE user_id = ? AND credential_type = ?",
            (user_id, credential_type.value),
        )
        await self.db.commit()
        deleted = cursor.rowcount > 0 if cursor.rowcount else False
        if deleted:
            logger.info(
                "credential_deleted",
                user_id=user_id,
                credential_type=credential_type.value,
            )
        return deleted

    async def list_credentials(self, user_id: int) -> list[CredentialType]:
        """List credential types for a user."""
        cursor = await self.db.execute(
            "SELECT credential_type FROM credentials WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [CredentialType(row["credential_type"]) for row in rows]

    # -------------------------------------------------------------------------
    # Preferences CRUD Implementation
    # -------------------------------------------------------------------------

    async def get_preferences(self, user_id: int) -> Preference | None:
        """Get user preferences."""
        cursor = await self.db.execute("SELECT * FROM preferences WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return self._row_to_preference(row) if row else None

    async def update_preferences(
        self,
        user_id: int,
        video_quality: str | None = None,
        audio_language: str | None = None,
        subtitle_language: str | None = None,
        preferred_genres: list[str] | None = None,
        excluded_genres: list[str] | None = None,
        auto_download: bool | None = None,
        notification_enabled: bool | None = None,
        claude_model: str | None = None,
        thinking_budget: int | None = None,
    ) -> Preference | None:
        """Update user preferences."""
        existing = await self.get_preferences(user_id)
        if existing is None:
            return None

        updates: list[str] = []
        params: list[Any] = []

        if video_quality is not None:
            updates.append("video_quality = ?")
            params.append(video_quality)
        if audio_language is not None:
            updates.append("audio_language = ?")
            params.append(audio_language)
        if subtitle_language is not None:
            updates.append("subtitle_language = ?")
            params.append(subtitle_language)
        if preferred_genres is not None:
            updates.append("preferred_genres = ?")
            params.append(json.dumps(preferred_genres))
        if excluded_genres is not None:
            updates.append("excluded_genres = ?")
            params.append(json.dumps(excluded_genres))
        if auto_download is not None:
            updates.append("auto_download = ?")
            params.append(1 if auto_download else 0)
        if notification_enabled is not None:
            updates.append("notification_enabled = ?")
            params.append(1 if notification_enabled else 0)
        if claude_model is not None:
            updates.append("claude_model = ?")
            params.append(claude_model)
        if thinking_budget is not None:
            updates.append("thinking_budget = ?")
            params.append(thinking_budget)

        if not updates:
            return existing

        updates.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(user_id)

        await self.db.execute(
            f"UPDATE preferences SET {', '.join(updates)} WHERE user_id = ?",
            params,
        )
        await self.db.commit()

        logger.info("preferences_updated", user_id=user_id)
        return await self.get_preferences(user_id)

    def _row_to_preference(self, row: Any) -> Preference:
        """Convert database row to Preference model."""
        return Preference(
            id=row["id"],
            user_id=row["user_id"],
            video_quality=row["video_quality"],
            audio_language=row["audio_language"],
            subtitle_language=row["subtitle_language"],
            preferred_genres=json.loads(row["preferred_genres"] or "[]"),
            excluded_genres=json.loads(row["excluded_genres"] or "[]"),
            auto_download=bool(row["auto_download"]),
            notification_enabled=bool(row["notification_enabled"]),
            claude_model=row["claude_model"] if "claude_model" in row.keys() else "claude-sonnet-4-5-20250929",
            thinking_budget=row["thinking_budget"] if "thinking_budget" in row.keys() else 0,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # -------------------------------------------------------------------------
    # Watched Items CRUD Implementation
    # -------------------------------------------------------------------------

    async def add_watched(
        self,
        user_id: int,
        media_type: str,
        title: str,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
        rating: float | None = None,
        review: str | None = None,
        watched_at: datetime | None = None,
    ) -> WatchedItem:
        """Add item to watch history."""
        now = datetime.now(UTC)
        watched_at = watched_at or now

        cursor = await self.db.execute(
            """
            INSERT INTO watched
                (user_id, media_type, tmdb_id, kinopoisk_id, title, year,
                 season, episode, rating, review, watched_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                media_type,
                tmdb_id,
                kinopoisk_id,
                title,
                year,
                season,
                episode,
                rating,
                review,
                watched_at.isoformat(),
                now.isoformat(),
            ),
        )
        await self.db.commit()

        item_id = cursor.lastrowid
        if item_id is None:
            raise RuntimeError("Failed to create watched item")

        logger.info("watched_added", user_id=user_id, media_type=media_type, title=title)

        return WatchedItem(
            id=item_id,
            user_id=user_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            kinopoisk_id=kinopoisk_id,
            title=title,
            year=year,
            season=season,
            episode=episode,
            rating=rating,
            review=review,
            watched_at=watched_at,
            created_at=now,
        )

    async def get_watched(
        self,
        user_id: int,
        media_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WatchedItem]:
        """Get user's watch history."""
        query = "SELECT * FROM watched WHERE user_id = ?"
        params: list[Any] = [user_id]

        if media_type:
            query += " AND media_type = ?"
            params.append(media_type)

        query += " ORDER BY watched_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_watched(row) for row in rows]

    async def is_watched(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Check if user has watched content."""
        if tmdb_id is None and kinopoisk_id is None:
            return False

        if tmdb_id and kinopoisk_id:
            query = "SELECT 1 FROM watched WHERE user_id = ? AND (tmdb_id = ? OR kinopoisk_id = ?) LIMIT 1"
            params = [user_id, tmdb_id, kinopoisk_id]
        elif tmdb_id:
            query = "SELECT 1 FROM watched WHERE user_id = ? AND tmdb_id = ? LIMIT 1"
            params = [user_id, tmdb_id]
        else:
            query = "SELECT 1 FROM watched WHERE user_id = ? AND kinopoisk_id = ? LIMIT 1"
            params = [user_id, kinopoisk_id]

        cursor = await self.db.execute(query, params)
        return await cursor.fetchone() is not None

    async def delete_watched(self, item_id: int) -> bool:
        """Delete item from watch history."""
        cursor = await self.db.execute("DELETE FROM watched WHERE id = ?", (item_id,))
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def update_watched_rating(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        rating: float | None = None,
        review: str | None = None,
    ) -> WatchedItem | None:
        """Update rating/review for watched item."""
        if tmdb_id is None and kinopoisk_id is None:
            return None

        updates: list[str] = []
        params: list[Any] = []

        if rating is not None:
            updates.append("rating = ?")
            params.append(rating)
        if review is not None:
            updates.append("review = ?")
            params.append(review)

        if not updates:
            return None

        if tmdb_id:
            where_clause = "user_id = ? AND tmdb_id = ?"
            params.extend([user_id, tmdb_id])
        else:
            where_clause = "user_id = ? AND kinopoisk_id = ?"
            params.extend([user_id, kinopoisk_id])

        await self.db.execute(
            f"UPDATE watched SET {', '.join(updates)} WHERE {where_clause}",
            params,
        )
        await self.db.commit()

        # Fetch updated item
        if tmdb_id:
            cursor = await self.db.execute(
                "SELECT * FROM watched WHERE user_id = ? AND tmdb_id = ?",
                (user_id, tmdb_id),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM watched WHERE user_id = ? AND kinopoisk_id = ?",
                (user_id, kinopoisk_id),
            )

        row = await cursor.fetchone()
        return self._row_to_watched(row) if row else None

    def _row_to_watched(self, row: Any) -> WatchedItem:
        """Convert database row to WatchedItem model."""
        return WatchedItem(
            id=row["id"],
            user_id=row["user_id"],
            media_type=row["media_type"],
            tmdb_id=row["tmdb_id"],
            kinopoisk_id=row["kinopoisk_id"],
            title=row["title"],
            year=row["year"],
            season=row["season"],
            episode=row["episode"],
            rating=row["rating"],
            review=row.get("review"),
            watched_at=datetime.fromisoformat(row["watched_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # -------------------------------------------------------------------------
    # Watchlist CRUD Implementation
    # -------------------------------------------------------------------------

    async def add_to_watchlist(
        self,
        user_id: int,
        media_type: str,
        title: str,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        year: int | None = None,
        poster_url: str | None = None,
        priority: int = 0,
        notes: str | None = None,
    ) -> WatchlistItem:
        """Add item to watchlist."""
        now = datetime.now(UTC)

        cursor = await self.db.execute(
            """
            INSERT INTO watchlist
                (user_id, tmdb_id, kinopoisk_id, media_type, title, year,
                 poster_url, priority, notes, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                tmdb_id,
                kinopoisk_id,
                media_type,
                title,
                year,
                poster_url,
                priority,
                notes,
                now.isoformat(),
            ),
        )
        await self.db.commit()

        item_id = cursor.lastrowid
        if item_id is None:
            raise RuntimeError("Failed to add to watchlist")

        logger.info("watchlist_added", user_id=user_id, title=title)

        return WatchlistItem(
            id=item_id,
            user_id=user_id,
            tmdb_id=tmdb_id,
            kinopoisk_id=kinopoisk_id,
            media_type=media_type,
            title=title,
            year=year,
            poster_url=poster_url,
            priority=priority,
            notes=notes,
            added_at=now,
        )

    async def get_watchlist(
        self,
        user_id: int,
        media_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WatchlistItem]:
        """Get user's watchlist."""
        query = "SELECT * FROM watchlist WHERE user_id = ?"
        params: list[Any] = [user_id]

        if media_type:
            query += " AND media_type = ?"
            params.append(media_type)

        query += " ORDER BY priority DESC, added_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_watchlist(row) for row in rows]

    async def remove_from_watchlist(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Remove item from watchlist."""
        if tmdb_id is None and kinopoisk_id is None:
            return False

        if tmdb_id:
            cursor = await self.db.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND tmdb_id = ?",
                (user_id, tmdb_id),
            )
        else:
            cursor = await self.db.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND kinopoisk_id = ?",
                (user_id, kinopoisk_id),
            )

        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def is_in_watchlist(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Check if item is in watchlist."""
        if tmdb_id is None and kinopoisk_id is None:
            return False

        if tmdb_id:
            cursor = await self.db.execute(
                "SELECT 1 FROM watchlist WHERE user_id = ? AND tmdb_id = ? LIMIT 1",
                (user_id, tmdb_id),
            )
        else:
            cursor = await self.db.execute(
                "SELECT 1 FROM watchlist WHERE user_id = ? AND kinopoisk_id = ? LIMIT 1",
                (user_id, kinopoisk_id),
            )

        return await cursor.fetchone() is not None

    def _row_to_watchlist(self, row: Any) -> WatchlistItem:
        """Convert database row to WatchlistItem model."""
        return WatchlistItem(
            id=row["id"],
            user_id=row["user_id"],
            tmdb_id=row["tmdb_id"],
            kinopoisk_id=row["kinopoisk_id"],
            media_type=row["media_type"],
            title=row["title"],
            year=row["year"],
            poster_url=row["poster_url"],
            priority=row["priority"],
            notes=row["notes"],
            added_at=datetime.fromisoformat(row["added_at"]),
        )

    # -------------------------------------------------------------------------
    # Profile CRUD Implementation
    # -------------------------------------------------------------------------

    async def get_profile(self, user_id: int) -> UserProfile | None:
        """Get user's markdown profile."""
        cursor = await self.db.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return UserProfile(
            id=row["id"],
            user_id=row["user_id"],
            profile_md=row["profile_md"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def update_profile(
        self,
        user_id: int,
        profile_md: str,
    ) -> UserProfile:
        """Update user's markdown profile."""
        now = datetime.now(UTC)

        # Try to update first
        cursor = await self.db.execute(
            """
            UPDATE profiles SET profile_md = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (profile_md, now.isoformat(), user_id),
        )

        if cursor.rowcount == 0:
            # Insert if not exists
            cursor = await self.db.execute(
                """
                INSERT INTO profiles (user_id, profile_md, updated_at)
                VALUES (?, ?, ?)
                """,
                (user_id, profile_md, now.isoformat()),
            )

        await self.db.commit()

        profile = await self.get_profile(user_id)
        if profile is None:
            raise RuntimeError("Failed to update profile")
        return profile

    # -------------------------------------------------------------------------
    # Monitor CRUD Implementation
    # -------------------------------------------------------------------------

    async def create_monitor(
        self,
        user_id: int,
        title: str,
        tmdb_id: int | None = None,
        media_type: str = "movie",
        quality: str = "1080p",
        auto_download: bool = False,
        release_date: datetime | None = None,
    ) -> Monitor:
        """Create a release monitor."""
        now = datetime.now(UTC)

        cursor = await self.db.execute(
            """
            INSERT INTO monitors
                (user_id, title, tmdb_id, media_type, quality, auto_download, status, release_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                user_id,
                title,
                tmdb_id,
                media_type,
                quality,
                1 if auto_download else 0,
                release_date.isoformat() if release_date else None,
                now.isoformat(),
            ),
        )
        await self.db.commit()

        monitor_id = cursor.lastrowid
        if monitor_id is None:
            raise RuntimeError("Failed to create monitor")

        logger.info("monitor_created", user_id=user_id, title=title)

        return Monitor(
            id=monitor_id,
            user_id=user_id,
            title=title,
            tmdb_id=tmdb_id,
            media_type=media_type,
            quality=quality,
            auto_download=auto_download,
            status="active",
            found_at=None,
            release_date=release_date,
            last_checked=None,
            created_at=now,
        )

    async def get_monitors(
        self,
        user_id: int | None = None,
        status: str | None = None,
    ) -> list[Monitor]:
        """Get monitors, optionally filtered."""
        query = "SELECT * FROM monitors WHERE 1=1"
        params: list[Any] = []

        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        if status is not None:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC"

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_monitor(row) for row in rows]

    async def update_monitor_status(
        self,
        monitor_id: int,
        status: str,
        found_at: datetime | None = None,
    ) -> Monitor | None:
        """Update monitor status."""
        found_str = found_at.isoformat() if found_at else None

        await self.db.execute(
            "UPDATE monitors SET status = ?, found_at = ? WHERE id = ?",
            (status, found_str, monitor_id),
        )
        await self.db.commit()

        cursor = await self.db.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
        row = await cursor.fetchone()
        return self._row_to_monitor(row) if row else None

    async def delete_monitor(self, monitor_id: int) -> bool:
        """Delete a monitor."""
        cursor = await self.db.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def get_monitor(self, monitor_id: int) -> Monitor | None:
        """Get a single monitor by ID."""
        cursor = await self.db.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
        row = await cursor.fetchone()
        return self._row_to_monitor(row) if row else None

    async def update_monitor_last_checked(self, monitor_id: int) -> None:
        """Update the last_checked timestamp for a monitor."""
        now = datetime.now(UTC)
        await self.db.execute(
            "UPDATE monitors SET last_checked = ? WHERE id = ?",
            (now.isoformat(), monitor_id),
        )
        await self.db.commit()

    async def get_all_active_monitors(self) -> list[Monitor]:
        """Get all active monitors across all users."""
        cursor = await self.db.execute(
            "SELECT * FROM monitors WHERE status = 'active' ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_monitor(row) for row in rows]

    async def get_all_users(self, limit: int = 1000) -> list[User]:
        """Get all users (with limit for safety)."""
        cursor = await self.db.execute(
            "SELECT * FROM users WHERE is_active = 1 ORDER BY id LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_user(row) for row in rows]

    def _row_to_monitor(self, row: Any) -> Monitor:
        """Convert database row to Monitor model."""
        return Monitor(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            tmdb_id=row["tmdb_id"],
            media_type=row["media_type"],
            quality=row["quality"],
            auto_download=bool(row["auto_download"]),
            status=row["status"],
            found_at=datetime.fromisoformat(row["found_at"]) if row["found_at"] else None,
            release_date=datetime.fromisoformat(row["release_date"]) if row.get("release_date") else None,
            last_checked=datetime.fromisoformat(row["last_checked"]) if row.get("last_checked") else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # -------------------------------------------------------------------------
    # Crew Stats CRUD Implementation
    # -------------------------------------------------------------------------

    async def update_crew_stat(
        self,
        user_id: int,
        person_id: int,
        person_name: str,
        role: str,
        film_id: int,
        rating: int,
    ) -> CrewStat:
        """Update crew statistics when user watches/rates a film."""
        now = datetime.now(UTC)

        # Get existing stat
        cursor = await self.db.execute(
            "SELECT * FROM crew_stats WHERE user_id = ? AND person_id = ? AND role = ?",
            (user_id, person_id, role),
        )
        row = await cursor.fetchone()

        if row:
            # Update existing
            film_ids = json.loads(row["film_ids"] or "[]")
            if film_id not in film_ids:
                film_ids.append(film_id)
                films_count = row["films_count"] + 1
                total_rating = row["total_rating"] + rating
            else:
                films_count = row["films_count"]
                total_rating = row["total_rating"]

            await self.db.execute(
                """
                UPDATE crew_stats
                SET films_count = ?, total_rating = ?, film_ids = ?, updated_at = ?
                WHERE user_id = ? AND person_id = ? AND role = ?
                """,
                (
                    films_count,
                    total_rating,
                    json.dumps(film_ids),
                    now.isoformat(),
                    user_id,
                    person_id,
                    role,
                ),
            )
            stat_id = row["id"]
        else:
            # Create new
            cursor = await self.db.execute(
                """
                INSERT INTO crew_stats
                    (user_id, person_id, person_name, role, films_count, total_rating, film_ids, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    user_id,
                    person_id,
                    person_name,
                    role,
                    rating,
                    json.dumps([film_id]),
                    now.isoformat(),
                ),
            )
            stat_id = cursor.lastrowid
            films_count = 1
            total_rating = rating
            film_ids = [film_id]

        await self.db.commit()

        return CrewStat(
            id=stat_id or 0,
            user_id=user_id,
            person_id=person_id,
            person_name=person_name,
            role=role,
            films_count=films_count,
            total_rating=total_rating,
            film_ids=film_ids,
            updated_at=now,
        )

    async def get_crew_stats(
        self,
        user_id: int,
        role: str | None = None,
        min_films: int = 1,
        limit: int = 20,
    ) -> list[CrewStat]:
        """Get crew statistics for a user."""
        query = "SELECT * FROM crew_stats WHERE user_id = ? AND films_count >= ?"
        params: list[Any] = [user_id, min_films]

        if role:
            query += " AND role = ?"
            params.append(role)

        query += " ORDER BY films_count DESC, (total_rating * 1.0 / films_count) DESC LIMIT ?"
        params.append(limit)

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_crew_stat(row) for row in rows]

    def _row_to_crew_stat(self, row: Any) -> CrewStat:
        """Convert database row to CrewStat model."""
        return CrewStat(
            id=row["id"],
            user_id=row["user_id"],
            person_id=row["person_id"],
            person_name=row["person_name"],
            role=row["role"],
            films_count=row["films_count"],
            total_rating=row["total_rating"],
            film_ids=json.loads(row["film_ids"] or "[]"),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # -------------------------------------------------------------------------
    # Blocklist CRUD Implementation
    # -------------------------------------------------------------------------

    async def add_to_blocklist(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
        block_level: str = "dont_recommend",
        notes: str | None = None,
    ) -> BlocklistItem:
        """Add item to blocklist."""
        now = datetime.now(UTC)

        cursor = await self.db.execute(
            """
            INSERT INTO blocklist (user_id, block_type, block_value, block_level, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, block_type, block_value) DO UPDATE SET
                block_level = excluded.block_level,
                notes = excluded.notes
            """,
            (user_id, block_type, block_value, block_level, notes, now.isoformat()),
        )
        await self.db.commit()

        item_id = cursor.lastrowid
        if item_id is None:
            cursor = await self.db.execute(
                "SELECT id FROM blocklist WHERE user_id = ? AND block_type = ? AND block_value = ?",
                (user_id, block_type, block_value),
            )
            row = await cursor.fetchone()
            item_id = row["id"] if row else 0

        logger.info(
            "blocklist_added", user_id=user_id, block_type=block_type, block_value=block_value
        )

        return BlocklistItem(
            id=item_id,
            user_id=user_id,
            block_type=block_type,
            block_value=block_value,
            block_level=block_level,
            notes=notes,
            created_at=now,
        )

    async def get_blocklist(
        self,
        user_id: int,
        block_type: str | None = None,
    ) -> list[BlocklistItem]:
        """Get user's blocklist."""
        query = "SELECT * FROM blocklist WHERE user_id = ?"
        params: list[Any] = [user_id]

        if block_type:
            query += " AND block_type = ?"
            params.append(block_type)

        query += " ORDER BY created_at DESC"

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_blocklist(row) for row in rows]

    async def remove_from_blocklist(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
    ) -> bool:
        """Remove item from blocklist."""
        cursor = await self.db.execute(
            "DELETE FROM blocklist WHERE user_id = ? AND block_type = ? AND block_value = ?",
            (user_id, block_type, block_value),
        )
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def is_blocked(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
    ) -> bool:
        """Check if item is blocked."""
        cursor = await self.db.execute(
            "SELECT 1 FROM blocklist WHERE user_id = ? AND block_type = ? AND block_value = ? LIMIT 1",
            (user_id, block_type, block_value),
        )
        return await cursor.fetchone() is not None

    def _row_to_blocklist(self, row: Any) -> BlocklistItem:
        """Convert database row to BlocklistItem model."""
        return BlocklistItem(
            id=row["id"],
            user_id=row["user_id"],
            block_type=row["block_type"],
            block_value=row["block_value"],
            block_level=row["block_level"],
            notes=row["notes"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# =============================================================================
# PostgreSQL Implementation
# =============================================================================


class PostgresStorage(BaseStorage):
    """PostgreSQL-based storage with asyncpg."""

    def __init__(
        self,
        database_url: str,
        encryption_key: str | bytes | None = None,
    ):
        """Initialize Postgres storage.

        Args:
            database_url: PostgreSQL connection URL
            encryption_key: Optional Fernet key for encrypting credentials
        """
        super().__init__(encryption_key)
        self._database_url = database_url
        self._pool: Any = None

    async def connect(self) -> None:
        """Open database connection pool and initialize schema."""
        import asyncpg

        self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=10)

        # Apply migrations
        await self._apply_migrations()

        logger.info("postgres_connected")

    async def close(self) -> None:
        """Close database connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("postgres_disconnected")

    @property
    def pool(self) -> Any:
        """Get active connection pool."""
        if self._pool is None:
            raise RuntimeError("Database not connected. Use 'async with' or call connect()")
        return self._pool

    async def _apply_migrations(self) -> None:
        """Apply database migrations."""
        migrations = [
            # Migration 1: Users table
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT DEFAULT 'ru',
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
            """,
            # Migration 2: Credentials table
            """
            CREATE TABLE IF NOT EXISTS credentials (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                credential_type TEXT NOT NULL,
                encrypted_value TEXT NOT NULL,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(user_id, credential_type)
            );
            CREATE INDEX IF NOT EXISTS idx_credentials_user_id ON credentials(user_id);
            """,
            # Migration 3: Preferences table
            """
            CREATE TABLE IF NOT EXISTS preferences (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                video_quality TEXT DEFAULT '1080p',
                audio_language TEXT DEFAULT 'ru',
                subtitle_language TEXT,
                preferred_genres JSONB DEFAULT '[]',
                excluded_genres JSONB DEFAULT '[]',
                auto_download BOOLEAN DEFAULT FALSE,
                notification_enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_preferences_user_id ON preferences(user_id);
            """,
            # Migration 4: Watched table
            """
            CREATE TABLE IF NOT EXISTS watched (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                media_type TEXT NOT NULL,
                tmdb_id INTEGER,
                kinopoisk_id INTEGER,
                title TEXT NOT NULL,
                year INTEGER,
                season INTEGER,
                episode INTEGER,
                rating REAL,
                review TEXT,
                watched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_watched_user_id ON watched(user_id);
            CREATE INDEX IF NOT EXISTS idx_watched_tmdb_id ON watched(tmdb_id);
            CREATE INDEX IF NOT EXISTS idx_watched_kinopoisk_id ON watched(kinopoisk_id);
            """,
            # Migration 5: Migrations tracking table
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """,
            # Migration 6: Watchlist table
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tmdb_id INTEGER,
                kinopoisk_id INTEGER,
                media_type TEXT NOT NULL,
                title TEXT NOT NULL,
                year INTEGER,
                poster_url TEXT,
                priority INTEGER DEFAULT 0,
                notes TEXT,
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_watchlist_user_id ON watchlist(user_id);
            """,
            # Migration 7: Profiles table
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                profile_md TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_profiles_user_id ON profiles(user_id);
            """,
            # Migration 8: Monitors table
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                tmdb_id INTEGER,
                media_type TEXT DEFAULT 'movie',
                quality TEXT DEFAULT '1080p',
                auto_download BOOLEAN DEFAULT FALSE,
                status TEXT DEFAULT 'active',
                found_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_monitors_user_id ON monitors(user_id);
            CREATE INDEX IF NOT EXISTS idx_monitors_status ON monitors(status);
            """,
            # Migration 9: Crew stats table
            """
            CREATE TABLE IF NOT EXISTS crew_stats (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                person_id INTEGER NOT NULL,
                person_name TEXT NOT NULL,
                role TEXT NOT NULL,
                films_count INTEGER DEFAULT 0,
                total_rating INTEGER DEFAULT 0,
                film_ids JSONB DEFAULT '[]',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(user_id, person_id, role)
            );
            CREATE INDEX IF NOT EXISTS idx_crew_stats_user_id ON crew_stats(user_id);
            """,
            # Migration 10: Blocklist table
            """
            CREATE TABLE IF NOT EXISTS blocklist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                block_type TEXT NOT NULL,
                block_value TEXT NOT NULL,
                block_level TEXT DEFAULT 'dont_recommend',
                notes TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(user_id, block_type, block_value)
            );
            CREATE INDEX IF NOT EXISTS idx_blocklist_user_id ON blocklist(user_id);
            """,
            # Migration 11: Add AI model settings to preferences
            """
            ALTER TABLE preferences ADD COLUMN IF NOT EXISTS claude_model TEXT DEFAULT 'claude-sonnet-4-5-20250929';
            ALTER TABLE preferences ADD COLUMN IF NOT EXISTS thinking_budget INTEGER DEFAULT 0;
            """,
        ]

        async with self.pool.acquire() as conn:
            # Get current migration version
            try:
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = '_migrations')"
                )
                if not exists:
                    current_version = 0
                else:
                    current_version = await conn.fetchval(
                        "SELECT COALESCE(MAX(version), 0) FROM _migrations"
                    )
            except Exception:
                current_version = 0

            # Apply pending migrations
            for i, sql in enumerate(migrations, 1):
                if i <= current_version:
                    continue

                logger.info("applying_postgres_migration", version=i)
                await conn.execute(sql)

                # Record migration
                if i >= 5:
                    await conn.execute(
                        "INSERT INTO _migrations (version, name) VALUES ($1, $2) ON CONFLICT (version) DO NOTHING",
                        i,
                        f"migration_{i}",
                    )

                logger.info("postgres_migration_applied", version=i)

    # -------------------------------------------------------------------------
    # User CRUD Implementation
    # -------------------------------------------------------------------------

    async def create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = "ru",
    ) -> User:
        """Create a new user."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name, language_code)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                telegram_id,
                username,
                first_name,
                last_name,
                language_code,
            )

            # Create default preferences
            await conn.execute(
                "INSERT INTO preferences (user_id) VALUES ($1)",
                row["id"],
            )

        logger.info("user_created", user_id=row["id"], telegram_id=telegram_id)
        return self._row_to_user(row)

    async def get_user(self, user_id: int) -> User | None:
        """Get user by internal ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return self._row_to_user(row) if row else None

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        """Get user by Telegram ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
        return self._row_to_user(row) if row else None

    async def update_user(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_active: bool | None = None,
    ) -> User | None:
        """Update user profile."""
        updates: list[str] = []
        params: list[Any] = []
        param_idx = 1

        if username is not None:
            updates.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1
        if first_name is not None:
            updates.append(f"first_name = ${param_idx}")
            params.append(first_name)
            param_idx += 1
        if last_name is not None:
            updates.append(f"last_name = ${param_idx}")
            params.append(last_name)
            param_idx += 1
        if language_code is not None:
            updates.append(f"language_code = ${param_idx}")
            params.append(language_code)
            param_idx += 1
        if is_active is not None:
            updates.append(f"is_active = ${param_idx}")
            params.append(is_active)
            param_idx += 1

        if not updates:
            return await self.get_user(user_id)

        updates.append("updated_at = NOW()")
        params.append(user_id)

        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id = ${param_idx}",
                *params,
            )

        return await self.get_user(user_id)

    async def delete_user(self, user_id: int) -> bool:
        """Delete user and all related data."""
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        deleted = result == "DELETE 1"
        if deleted:
            logger.info("user_deleted", user_id=user_id)
        return deleted

    async def list_users(
        self,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[User]:
        """List users with pagination."""
        query = "SELECT * FROM users"
        params: list[Any] = []

        if active_only:
            query += " WHERE is_active = TRUE"

        query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_user(row) for row in rows]

    def _row_to_user(self, row: Any) -> User:
        """Convert database row to User model."""
        return User(
            id=row["id"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            language_code=row["language_code"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Credentials CRUD Implementation
    # -------------------------------------------------------------------------

    async def store_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
        value: str,
        expires_at: datetime | None = None,
    ) -> Credential:
        """Store an encrypted credential."""
        if self._encryption is None:
            raise RuntimeError("Encryption key not configured")

        encrypted_value = self._encryption.encrypt(value)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO credentials (user_id, credential_type, encrypted_value, expires_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, credential_type) DO UPDATE SET
                    encrypted_value = EXCLUDED.encrypted_value,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                RETURNING *
                """,
                user_id,
                credential_type.value,
                encrypted_value,
                expires_at,
            )

        logger.info("credential_stored", user_id=user_id, credential_type=credential_type.value)

        return Credential(
            id=row["id"],
            user_id=row["user_id"],
            credential_type=credential_type,
            encrypted_value=row["encrypted_value"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
    ) -> str | None:
        """Get decrypted credential value."""
        if self._encryption is None:
            raise RuntimeError("Encryption key not configured")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT encrypted_value, expires_at FROM credentials WHERE user_id = $1 AND credential_type = $2",
                user_id,
                credential_type.value,
            )

        if row is None:
            return None

        if row["expires_at"] and row["expires_at"] < datetime.now(UTC):
            logger.warning(
                "credential_expired",
                user_id=user_id,
                credential_type=credential_type.value,
            )
            return None

        return self._encryption.decrypt(row["encrypted_value"])

    async def delete_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
    ) -> bool:
        """Delete a credential."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM credentials WHERE user_id = $1 AND credential_type = $2",
                user_id,
                credential_type.value,
            )
        deleted = result == "DELETE 1"
        if deleted:
            logger.info(
                "credential_deleted",
                user_id=user_id,
                credential_type=credential_type.value,
            )
        return deleted

    async def list_credentials(self, user_id: int) -> list[CredentialType]:
        """List credential types for a user."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT credential_type FROM credentials WHERE user_id = $1",
                user_id,
            )
        return [CredentialType(row["credential_type"]) for row in rows]

    # -------------------------------------------------------------------------
    # Preferences CRUD Implementation
    # -------------------------------------------------------------------------

    async def get_preferences(self, user_id: int) -> Preference | None:
        """Get user preferences."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM preferences WHERE user_id = $1", user_id)
        return self._row_to_preference(row) if row else None

    async def update_preferences(
        self,
        user_id: int,
        video_quality: str | None = None,
        audio_language: str | None = None,
        subtitle_language: str | None = None,
        preferred_genres: list[str] | None = None,
        excluded_genres: list[str] | None = None,
        auto_download: bool | None = None,
        notification_enabled: bool | None = None,
        claude_model: str | None = None,
        thinking_budget: int | None = None,
    ) -> Preference | None:
        """Update user preferences."""
        existing = await self.get_preferences(user_id)
        if existing is None:
            return None

        updates: list[str] = []
        params: list[Any] = []
        param_idx = 1

        if video_quality is not None:
            updates.append(f"video_quality = ${param_idx}")
            params.append(video_quality)
            param_idx += 1
        if audio_language is not None:
            updates.append(f"audio_language = ${param_idx}")
            params.append(audio_language)
            param_idx += 1
        if subtitle_language is not None:
            updates.append(f"subtitle_language = ${param_idx}")
            params.append(subtitle_language)
            param_idx += 1
        if preferred_genres is not None:
            updates.append(f"preferred_genres = ${param_idx}")
            params.append(json.dumps(preferred_genres))
            param_idx += 1
        if excluded_genres is not None:
            updates.append(f"excluded_genres = ${param_idx}")
            params.append(json.dumps(excluded_genres))
            param_idx += 1
        if auto_download is not None:
            updates.append(f"auto_download = ${param_idx}")
            params.append(auto_download)
            param_idx += 1
        if notification_enabled is not None:
            updates.append(f"notification_enabled = ${param_idx}")
            params.append(notification_enabled)
            param_idx += 1
        if claude_model is not None:
            updates.append(f"claude_model = ${param_idx}")
            params.append(claude_model)
            param_idx += 1
        if thinking_budget is not None:
            updates.append(f"thinking_budget = ${param_idx}")
            params.append(thinking_budget)
            param_idx += 1

        if not updates:
            return existing

        updates.append("updated_at = NOW()")
        params.append(user_id)

        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE preferences SET {', '.join(updates)} WHERE user_id = ${param_idx}",
                *params,
            )

        logger.info("preferences_updated", user_id=user_id)
        return await self.get_preferences(user_id)

    def _row_to_preference(self, row: Any) -> Preference:
        """Convert database row to Preference model."""
        preferred = row["preferred_genres"]
        excluded = row["excluded_genres"]

        # Handle both JSON string and already parsed list
        if isinstance(preferred, str):
            preferred = json.loads(preferred)
        if isinstance(excluded, str):
            excluded = json.loads(excluded)

        return Preference(
            id=row["id"],
            user_id=row["user_id"],
            video_quality=row["video_quality"],
            audio_language=row["audio_language"],
            subtitle_language=row["subtitle_language"],
            preferred_genres=preferred or [],
            excluded_genres=excluded or [],
            auto_download=row["auto_download"],
            notification_enabled=row["notification_enabled"],
            claude_model=row.get("claude_model", "claude-sonnet-4-5-20250929"),
            thinking_budget=row.get("thinking_budget", 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Watched Items CRUD Implementation
    # -------------------------------------------------------------------------

    async def add_watched(
        self,
        user_id: int,
        media_type: str,
        title: str,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
        rating: float | None = None,
        review: str | None = None,
        watched_at: datetime | None = None,
    ) -> WatchedItem:
        """Add item to watch history."""
        watched_at = watched_at or datetime.now(UTC)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO watched
                    (user_id, media_type, tmdb_id, kinopoisk_id, title, year,
                     season, episode, rating, review, watched_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING *
                """,
                user_id,
                media_type,
                tmdb_id,
                kinopoisk_id,
                title,
                year,
                season,
                episode,
                rating,
                review,
                watched_at,
            )

        logger.info("watched_added", user_id=user_id, media_type=media_type, title=title)
        return self._row_to_watched(row)

    async def get_watched(
        self,
        user_id: int,
        media_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WatchedItem]:
        """Get user's watch history."""
        query = "SELECT * FROM watched WHERE user_id = $1"
        params: list[Any] = [user_id]
        param_idx = 2

        if media_type:
            query += f" AND media_type = ${param_idx}"
            params.append(media_type)
            param_idx += 1

        query += f" ORDER BY watched_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_watched(row) for row in rows]

    async def is_watched(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Check if user has watched content."""
        if tmdb_id is None and kinopoisk_id is None:
            return False

        async with self.pool.acquire() as conn:
            if tmdb_id and kinopoisk_id:
                row = await conn.fetchrow(
                    "SELECT 1 FROM watched WHERE user_id = $1 AND (tmdb_id = $2 OR kinopoisk_id = $3) LIMIT 1",
                    user_id,
                    tmdb_id,
                    kinopoisk_id,
                )
            elif tmdb_id:
                row = await conn.fetchrow(
                    "SELECT 1 FROM watched WHERE user_id = $1 AND tmdb_id = $2 LIMIT 1",
                    user_id,
                    tmdb_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT 1 FROM watched WHERE user_id = $1 AND kinopoisk_id = $2 LIMIT 1",
                    user_id,
                    kinopoisk_id,
                )

        return row is not None

    async def delete_watched(self, item_id: int) -> bool:
        """Delete item from watch history."""
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM watched WHERE id = $1", item_id)
        return result == "DELETE 1"

    async def update_watched_rating(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        rating: float | None = None,
        review: str | None = None,
    ) -> WatchedItem | None:
        """Update rating/review for watched item."""
        if tmdb_id is None and kinopoisk_id is None:
            return None

        updates: list[str] = []
        params: list[Any] = []
        param_idx = 1

        if rating is not None:
            updates.append(f"rating = ${param_idx}")
            params.append(rating)
            param_idx += 1
        if review is not None:
            updates.append(f"review = ${param_idx}")
            params.append(review)
            param_idx += 1

        if not updates:
            return None

        params.append(user_id)
        if tmdb_id:
            where_clause = f"user_id = ${param_idx} AND tmdb_id = ${param_idx + 1}"
            params.append(tmdb_id)
        else:
            where_clause = f"user_id = ${param_idx} AND kinopoisk_id = ${param_idx + 1}"
            params.append(kinopoisk_id)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE watched SET {', '.join(updates)} WHERE {where_clause} RETURNING *",
                *params,
            )

        return self._row_to_watched(row) if row else None

    def _row_to_watched(self, row: Any) -> WatchedItem:
        """Convert database row to WatchedItem model."""
        return WatchedItem(
            id=row["id"],
            user_id=row["user_id"],
            media_type=row["media_type"],
            tmdb_id=row["tmdb_id"],
            kinopoisk_id=row["kinopoisk_id"],
            title=row["title"],
            year=row["year"],
            season=row["season"],
            episode=row["episode"],
            rating=row["rating"],
            review=row["review"],
            watched_at=row["watched_at"],
            created_at=row["created_at"],
        )

    # -------------------------------------------------------------------------
    # Watchlist CRUD Implementation
    # -------------------------------------------------------------------------

    async def add_to_watchlist(
        self,
        user_id: int,
        media_type: str,
        title: str,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
        year: int | None = None,
        poster_url: str | None = None,
        priority: int = 0,
        notes: str | None = None,
    ) -> WatchlistItem:
        """Add item to watchlist."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO watchlist
                    (user_id, tmdb_id, kinopoisk_id, media_type, title, year,
                     poster_url, priority, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
                """,
                user_id,
                tmdb_id,
                kinopoisk_id,
                media_type,
                title,
                year,
                poster_url,
                priority,
                notes,
            )

        logger.info("watchlist_added", user_id=user_id, title=title)
        return self._row_to_watchlist(row)

    async def get_watchlist(
        self,
        user_id: int,
        media_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WatchlistItem]:
        """Get user's watchlist."""
        query = "SELECT * FROM watchlist WHERE user_id = $1"
        params: list[Any] = [user_id]
        param_idx = 2

        if media_type:
            query += f" AND media_type = ${param_idx}"
            params.append(media_type)
            param_idx += 1

        query += (
            f" ORDER BY priority DESC, added_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        )
        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_watchlist(row) for row in rows]

    async def remove_from_watchlist(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Remove item from watchlist."""
        if tmdb_id is None and kinopoisk_id is None:
            return False

        async with self.pool.acquire() as conn:
            if tmdb_id:
                result = await conn.execute(
                    "DELETE FROM watchlist WHERE user_id = $1 AND tmdb_id = $2",
                    user_id,
                    tmdb_id,
                )
            else:
                result = await conn.execute(
                    "DELETE FROM watchlist WHERE user_id = $1 AND kinopoisk_id = $2",
                    user_id,
                    kinopoisk_id,
                )

        return result == "DELETE 1"

    async def is_in_watchlist(
        self,
        user_id: int,
        tmdb_id: int | None = None,
        kinopoisk_id: int | None = None,
    ) -> bool:
        """Check if item is in watchlist."""
        if tmdb_id is None and kinopoisk_id is None:
            return False

        async with self.pool.acquire() as conn:
            if tmdb_id:
                row = await conn.fetchrow(
                    "SELECT 1 FROM watchlist WHERE user_id = $1 AND tmdb_id = $2 LIMIT 1",
                    user_id,
                    tmdb_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT 1 FROM watchlist WHERE user_id = $1 AND kinopoisk_id = $2 LIMIT 1",
                    user_id,
                    kinopoisk_id,
                )

        return row is not None

    def _row_to_watchlist(self, row: Any) -> WatchlistItem:
        """Convert database row to WatchlistItem model."""
        return WatchlistItem(
            id=row["id"],
            user_id=row["user_id"],
            tmdb_id=row["tmdb_id"],
            kinopoisk_id=row["kinopoisk_id"],
            media_type=row["media_type"],
            title=row["title"],
            year=row["year"],
            poster_url=row["poster_url"],
            priority=row["priority"],
            notes=row["notes"],
            added_at=row["added_at"],
        )

    # -------------------------------------------------------------------------
    # Profile CRUD Implementation
    # -------------------------------------------------------------------------

    async def get_profile(self, user_id: int) -> UserProfile | None:
        """Get user's markdown profile."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM profiles WHERE user_id = $1", user_id)
        if row is None:
            return None
        return UserProfile(
            id=row["id"],
            user_id=row["user_id"],
            profile_md=row["profile_md"],
            updated_at=row["updated_at"],
        )

    async def update_profile(
        self,
        user_id: int,
        profile_md: str,
    ) -> UserProfile:
        """Update user's markdown profile."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO profiles (user_id, profile_md)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET
                    profile_md = EXCLUDED.profile_md,
                    updated_at = NOW()
                RETURNING *
                """,
                user_id,
                profile_md,
            )

        return UserProfile(
            id=row["id"],
            user_id=row["user_id"],
            profile_md=row["profile_md"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Monitor CRUD Implementation
    # -------------------------------------------------------------------------

    async def create_monitor(
        self,
        user_id: int,
        title: str,
        tmdb_id: int | None = None,
        media_type: str = "movie",
        quality: str = "1080p",
        auto_download: bool = False,
        release_date: datetime | None = None,
    ) -> Monitor:
        """Create a release monitor."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO monitors
                    (user_id, title, tmdb_id, media_type, quality, auto_download, release_date)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                user_id,
                title,
                tmdb_id,
                media_type,
                quality,
                auto_download,
                release_date,
            )

        logger.info("monitor_created", user_id=user_id, title=title)
        return self._row_to_monitor(row)

    async def get_monitors(
        self,
        user_id: int | None = None,
        status: str | None = None,
    ) -> list[Monitor]:
        """Get monitors, optionally filtered."""
        query = "SELECT * FROM monitors WHERE 1=1"
        params: list[Any] = []
        param_idx = 1

        if user_id is not None:
            query += f" AND user_id = ${param_idx}"
            params.append(user_id)
            param_idx += 1
        if status is not None:
            query += f" AND status = ${param_idx}"
            params.append(status)
            param_idx += 1

        query += " ORDER BY created_at DESC"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_monitor(row) for row in rows]

    async def update_monitor_status(
        self,
        monitor_id: int,
        status: str,
        found_at: datetime | None = None,
    ) -> Monitor | None:
        """Update monitor status."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE monitors SET status = $1, found_at = $2 WHERE id = $3 RETURNING *",
                status,
                found_at,
                monitor_id,
            )

        return self._row_to_monitor(row) if row else None

    async def delete_monitor(self, monitor_id: int) -> bool:
        """Delete a monitor."""
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM monitors WHERE id = $1", monitor_id)
        return result == "DELETE 1"

    async def get_monitor(self, monitor_id: int) -> Monitor | None:
        """Get a single monitor by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM monitors WHERE id = $1", monitor_id)
        return self._row_to_monitor(row) if row else None

    async def update_monitor_last_checked(self, monitor_id: int) -> None:
        """Update the last_checked timestamp for a monitor."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE monitors SET last_checked = NOW() WHERE id = $1",
                monitor_id,
            )

    async def get_all_active_monitors(self) -> list[Monitor]:
        """Get all active monitors across all users."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM monitors WHERE status = 'active' ORDER BY created_at"
            )
        return [self._row_to_monitor(row) for row in rows]

    async def get_all_users(self, limit: int = 1000) -> list[User]:
        """Get all users (with limit for safety)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE is_active = TRUE ORDER BY id LIMIT $1",
                limit,
            )
        return [self._row_to_user(row) for row in rows]

    def _row_to_monitor(self, row: Any) -> Monitor:
        """Convert database row to Monitor model."""
        return Monitor(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            tmdb_id=row["tmdb_id"],
            media_type=row["media_type"],
            quality=row["quality"],
            auto_download=row["auto_download"],
            status=row["status"],
            found_at=row["found_at"],
            release_date=row.get("release_date"),
            last_checked=row.get("last_checked"),
            created_at=row["created_at"],
        )

    # -------------------------------------------------------------------------
    # Crew Stats CRUD Implementation
    # -------------------------------------------------------------------------

    async def update_crew_stat(
        self,
        user_id: int,
        person_id: int,
        person_name: str,
        role: str,
        film_id: int,
        rating: int,
    ) -> CrewStat:
        """Update crew statistics when user watches/rates a film."""
        async with self.pool.acquire() as conn:
            # Get existing stat
            row = await conn.fetchrow(
                "SELECT * FROM crew_stats WHERE user_id = $1 AND person_id = $2 AND role = $3",
                user_id,
                person_id,
                role,
            )

            if row:
                # Update existing
                film_ids = row["film_ids"] or []
                if isinstance(film_ids, str):
                    film_ids = json.loads(film_ids)

                if film_id not in film_ids:
                    film_ids.append(film_id)
                    films_count = row["films_count"] + 1
                    total_rating = row["total_rating"] + rating
                else:
                    films_count = row["films_count"]
                    total_rating = row["total_rating"]

                row = await conn.fetchrow(
                    """
                    UPDATE crew_stats
                    SET films_count = $1, total_rating = $2, film_ids = $3, updated_at = NOW()
                    WHERE user_id = $4 AND person_id = $5 AND role = $6
                    RETURNING *
                    """,
                    films_count,
                    total_rating,
                    json.dumps(film_ids),
                    user_id,
                    person_id,
                    role,
                )
            else:
                # Create new
                row = await conn.fetchrow(
                    """
                    INSERT INTO crew_stats
                        (user_id, person_id, person_name, role, films_count, total_rating, film_ids)
                    VALUES ($1, $2, $3, $4, 1, $5, $6)
                    RETURNING *
                    """,
                    user_id,
                    person_id,
                    person_name,
                    role,
                    rating,
                    json.dumps([film_id]),
                )

        return self._row_to_crew_stat(row)

    async def get_crew_stats(
        self,
        user_id: int,
        role: str | None = None,
        min_films: int = 1,
        limit: int = 20,
    ) -> list[CrewStat]:
        """Get crew statistics for a user."""
        query = "SELECT * FROM crew_stats WHERE user_id = $1 AND films_count >= $2"
        params: list[Any] = [user_id, min_films]
        param_idx = 3

        if role:
            query += f" AND role = ${param_idx}"
            params.append(role)
            param_idx += 1

        query += f" ORDER BY films_count DESC, (total_rating::float / films_count) DESC LIMIT ${param_idx}"
        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_crew_stat(row) for row in rows]

    def _row_to_crew_stat(self, row: Any) -> CrewStat:
        """Convert database row to CrewStat model."""
        film_ids = row["film_ids"] or []
        if isinstance(film_ids, str):
            film_ids = json.loads(film_ids)

        return CrewStat(
            id=row["id"],
            user_id=row["user_id"],
            person_id=row["person_id"],
            person_name=row["person_name"],
            role=row["role"],
            films_count=row["films_count"],
            total_rating=row["total_rating"],
            film_ids=film_ids,
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Blocklist CRUD Implementation
    # -------------------------------------------------------------------------

    async def add_to_blocklist(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
        block_level: str = "dont_recommend",
        notes: str | None = None,
    ) -> BlocklistItem:
        """Add item to blocklist."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO blocklist (user_id, block_type, block_value, block_level, notes)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, block_type, block_value) DO UPDATE SET
                    block_level = EXCLUDED.block_level,
                    notes = EXCLUDED.notes
                RETURNING *
                """,
                user_id,
                block_type,
                block_value,
                block_level,
                notes,
            )

        logger.info(
            "blocklist_added", user_id=user_id, block_type=block_type, block_value=block_value
        )
        return self._row_to_blocklist(row)

    async def get_blocklist(
        self,
        user_id: int,
        block_type: str | None = None,
    ) -> list[BlocklistItem]:
        """Get user's blocklist."""
        query = "SELECT * FROM blocklist WHERE user_id = $1"
        params: list[Any] = [user_id]

        if block_type:
            query += " AND block_type = $2"
            params.append(block_type)

        query += " ORDER BY created_at DESC"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_blocklist(row) for row in rows]

    async def remove_from_blocklist(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
    ) -> bool:
        """Remove item from blocklist."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM blocklist WHERE user_id = $1 AND block_type = $2 AND block_value = $3",
                user_id,
                block_type,
                block_value,
            )
        return result == "DELETE 1"

    async def is_blocked(
        self,
        user_id: int,
        block_type: str,
        block_value: str,
    ) -> bool:
        """Check if item is blocked."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM blocklist WHERE user_id = $1 AND block_type = $2 AND block_value = $3 LIMIT 1",
                user_id,
                block_type,
                block_value,
            )
        return row is not None

    def _row_to_blocklist(self, row: Any) -> BlocklistItem:
        """Convert database row to BlocklistItem model."""
        return BlocklistItem(
            id=row["id"],
            user_id=row["user_id"],
            block_type=row["block_type"],
            block_value=row["block_value"],
            block_level=row["block_level"],
            notes=row["notes"],
            created_at=row["created_at"],
        )


# =============================================================================
# Backward Compatibility Aliases
# =============================================================================


# Alias for backward compatibility
UserStorage = SQLiteStorage


# =============================================================================
# Factory Functions
# =============================================================================


def get_storage_backend(
    database_url: str | None = None,
    db_path: str | Path = "data/users.db",
    encryption_key: str | bytes | None = None,
) -> BaseStorage:
    """Get the appropriate storage backend.

    Args:
        database_url: PostgreSQL connection URL (if set, uses Postgres)
        db_path: Path to SQLite database (fallback if no database_url)
        encryption_key: Fernet key for encrypting credentials

    Returns:
        Either PostgresStorage or SQLiteStorage instance
    """
    if database_url:
        logger.info("using_postgres_storage")
        return PostgresStorage(database_url, encryption_key)
    logger.info("using_sqlite_storage", db_path=str(db_path))
    return SQLiteStorage(db_path, encryption_key)


@asynccontextmanager
async def get_user_storage(
    db_path: str | Path = "data/users.db",
    encryption_key: str | bytes | None = None,
) -> AsyncIterator[BaseStorage]:
    """Get user storage instance as context manager (backward compatible).

    This function automatically detects if DATABASE_URL is set and uses
    Postgres, otherwise falls back to SQLite.

    Args:
        db_path: Path to SQLite database file (used if no DATABASE_URL)
        encryption_key: Optional Fernet key for encrypting credentials

    Yields:
        Storage instance (either Postgres or SQLite)
    """
    from src.config import settings

    database_url = None
    if settings.database_url:
        database_url = settings.database_url.get_secret_value()

    storage = get_storage_backend(database_url, db_path, encryption_key)
    async with storage:
        yield storage


@asynccontextmanager
async def get_storage(
    encryption_key: str | bytes | None = None,
) -> AsyncIterator[BaseStorage]:
    """Get storage instance with auto-detection of backend.

    This is the recommended way to get storage - it automatically uses
    the DATABASE_URL environment variable if set.

    Args:
        encryption_key: Optional Fernet key for encrypting credentials

    Yields:
        Storage instance
    """
    from src.config import settings

    if encryption_key is None:
        encryption_key = settings.encryption_key.get_secret_value()

    database_url = None
    if settings.database_url:
        database_url = settings.database_url.get_secret_value()

    storage = get_storage_backend(
        database_url,
        "data/users.db",
        encryption_key,
    )
    async with storage:
        yield storage
