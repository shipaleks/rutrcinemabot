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
    SEEDBOX_HOST = "seedbox_host"
    SEEDBOX_USERNAME = "seedbox_username"
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
    thinking_budget: int = Field(default=5120)  # 0 = disabled, >0 = max thinking tokens
    # Search settings
    default_search_source: str = Field(default="auto")  # "auto", "rutracker", "piratebay"
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
    # Found release data (stored when release is found)
    found_data: dict[str, Any] | None = None  # magnet, quality, size, seeds, source, torrent_title
    # TV series episode tracking
    season_number: int | None = None  # For episode-by-episode tracking
    episode_number: int | None = None  # Current episode to track
    tracking_mode: str = "season"  # "season" (whole season) or "episode" (per episode)


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


class CoreMemoryBlock(BaseModel):
    """Core memory block for structured user profile.

    Implements MemGPT-style memory hierarchy with character limits.
    """

    id: int
    user_id: int
    block_name: str  # identity, preferences, watch_context, active_context, style, instructions, blocklist, learnings
    content: str = ""
    max_chars: int = 500
    updated_at: datetime

    @property
    def usage_percent(self) -> float:
        """Calculate block usage percentage."""
        if self.max_chars == 0:
            return 0.0
        return len(self.content) / self.max_chars * 100


class ConversationSession(BaseModel):
    """Conversation session for tracking message boundaries.

    Sessions are automatically ended after 30 minutes of inactivity.
    """

    id: int
    user_id: int
    started_at: datetime
    ended_at: datetime | None = None
    message_count: int = 0
    summary: str | None = None
    key_learnings: list[str] = Field(default_factory=list)
    status: str = "active"  # active, ended, summarized


class MemoryNote(BaseModel):
    """Zettelkasten-style memory note for searchable recall.

    Notes are auto-promoted to core memory if frequently accessed.
    """

    id: int
    user_id: int
    content: str
    source: str  # conversation, letterboxd, rating_pattern, monitor
    keywords: list[str] = Field(default_factory=list)
    confidence: float = 0.5  # 0-1, higher = more reliable
    created_at: datetime
    last_accessed: datetime
    access_count: int = 0
    archived_at: datetime | None = None  # NULL = active


class Download(BaseModel):
    """Download history item model.

    Tracks user downloads for follow-up and recommendations.
    """

    id: int
    user_id: int
    tmdb_id: int | None = None
    media_type: str | None = None  # movie, tv
    title: str
    season: int | None = None  # For TV series
    episode: int | None = None  # For TV series
    quality: str | None = None
    source: str | None = None  # rutracker, piratebay
    magnet_hash: str | None = None
    downloaded_at: datetime
    followed_up: int = 0  # 0=pending, 1=sent, 2=answered
    rating: float | None = None  # Filled after follow-up


class PendingPush(BaseModel):
    """Pending push notification model.

    Used for throttling and prioritizing proactive notifications.
    """

    id: int
    user_id: int
    push_type: str  # followup, director, gem, news
    priority: int  # 1=high, 2=medium, 3=low
    content: dict[str, Any]  # JSON with push data
    created_at: datetime
    sent_at: datetime | None = None


class SyncedTorrent(BaseModel):
    """Synced torrent model for seedbox tracking.

    Tracks torrents sent to seedbox and their sync status to local NAS.
    """

    id: int
    user_id: int
    torrent_hash: str  # Torrent info hash
    torrent_name: str  # Display name
    seedbox_path: str | None = None  # Path on seedbox
    local_path: str | None = None  # Path on local NAS after sync
    size_bytes: int | None = None
    status: str = "downloading"  # downloading, seeding, synced, deleted
    synced_at: datetime | None = None  # When synced to local NAS
    deleted_from_seedbox_at: datetime | None = None  # When removed from seedbox
    created_at: datetime


# Block name constants and limits
CORE_MEMORY_BLOCKS = {
    "identity": {"max_chars": 500, "agent_editable": False},
    "preferences": {"max_chars": 800, "agent_editable": True},
    "watch_context": {"max_chars": 500, "agent_editable": True},
    "active_context": {"max_chars": 600, "agent_editable": True, "auto_expire_days": 14},
    "style": {"max_chars": 400, "agent_editable": True},
    "instructions": {"max_chars": 600, "agent_editable": True, "confirm_update": True},
    "blocklist": {"max_chars": 600, "agent_editable": True, "confirm_update": True},
    "learnings": {"max_chars": 1000, "agent_editable": False},
}


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
        default_search_source: str | None = None,
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
    async def is_watched_by_title(self, user_id: int, title: str) -> bool:
        """Check if user has watched content by title (case-insensitive)."""
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

    @abstractmethod
    async def get_watched_without_tmdb_data(
        self,
        limit: int = 50,
    ) -> list[WatchedItem]:
        """Get watched items that don't have TMDB data (for enrichment)."""
        pass

    @abstractmethod
    async def mark_tmdb_enrichment_failed(
        self,
        watched_id: int,
    ) -> None:
        """Mark a watched item as failed for TMDB enrichment."""
        pass

    @abstractmethod
    async def update_watched_tmdb_data(
        self,
        watched_id: int,
        tmdb_id: int,
        director: str | None = None,
    ) -> bool:
        """Update TMDB data for a watched item."""
        pass

    @abstractmethod
    async def clear_watched(self, user_id: int) -> int:
        """Delete all watched items for a user. Returns count deleted."""
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

    @abstractmethod
    async def clear_watchlist(self, user_id: int) -> int:
        """Delete all watchlist items for a user. Returns count deleted."""
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
        tracking_mode: str = "season",
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> Monitor:
        """Create a release monitor.

        Args:
            user_id: Internal user ID
            title: Title to search for
            tmdb_id: Optional TMDB ID
            media_type: 'movie' or 'tv'
            quality: Desired quality (720p, 1080p, 4K)
            auto_download: Auto-download when found
            release_date: Expected release date
            tracking_mode: For TV: 'season' (whole season) or 'episode' (specific ep)
            season_number: Season number for TV tracking
            episode_number: Episode number for 'episode' tracking mode
        """
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
        found_data: dict[str, Any] | None = None,
    ) -> Monitor | None:
        """Update monitor status and optionally store found release data."""
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

    # -------------------------------------------------------------------------
    # Core Memory Blocks CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def get_core_memory_block(
        self,
        user_id: int,
        block_name: str,
    ) -> CoreMemoryBlock | None:
        """Get a specific core memory block."""
        pass

    @abstractmethod
    async def get_all_core_memory_blocks(
        self,
        user_id: int,
    ) -> list[CoreMemoryBlock]:
        """Get all core memory blocks for a user."""
        pass

    @abstractmethod
    async def update_core_memory_block(
        self,
        user_id: int,
        block_name: str,
        content: str,
    ) -> CoreMemoryBlock:
        """Update a core memory block (creates if not exists)."""
        pass

    @abstractmethod
    async def initialize_core_memory_blocks(
        self,
        user_id: int,
    ) -> list[CoreMemoryBlock]:
        """Initialize all core memory blocks for a new user."""
        pass

    # -------------------------------------------------------------------------
    # Conversation Sessions CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def get_active_session(
        self,
        user_id: int,
    ) -> ConversationSession | None:
        """Get the active session for a user."""
        pass

    @abstractmethod
    async def create_session(
        self,
        user_id: int,
    ) -> ConversationSession:
        """Create a new conversation session."""
        pass

    @abstractmethod
    async def end_session(
        self,
        session_id: int,
        summary: str | None = None,
        key_learnings: list[str] | None = None,
    ) -> ConversationSession | None:
        """End a session and optionally add summary."""
        pass

    @abstractmethod
    async def increment_session_message_count(
        self,
        session_id: int,
    ) -> None:
        """Increment the message count for a session."""
        pass

    @abstractmethod
    async def get_recent_sessions(
        self,
        user_id: int,
        limit: int = 10,
        days: int = 30,
    ) -> list[ConversationSession]:
        """Get recent sessions for a user."""
        pass

    # -------------------------------------------------------------------------
    # Memory Notes CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def create_memory_note(
        self,
        user_id: int,
        content: str,
        source: str,
        keywords: list[str] | None = None,
        confidence: float = 0.5,
    ) -> MemoryNote:
        """Create a new memory note."""
        pass

    @abstractmethod
    async def get_memory_notes(
        self,
        user_id: int,
        source: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[MemoryNote]:
        """Get memory notes for a user."""
        pass

    @abstractmethod
    async def search_memory_notes(
        self,
        user_id: int,
        query: str,
        limit: int = 10,
    ) -> list[MemoryNote]:
        """Search memory notes by keywords or content."""
        pass

    @abstractmethod
    async def update_memory_note_access(
        self,
        note_id: int,
    ) -> None:
        """Update last_accessed and increment access_count."""
        pass

    @abstractmethod
    async def archive_memory_note(
        self,
        note_id: int,
    ) -> bool:
        """Archive a memory note."""
        pass

    @abstractmethod
    async def get_notes_for_archival(
        self,
        user_id: int,
        age_days: int = 90,
        min_access_count: int = 3,
    ) -> list[MemoryNote]:
        """Get notes that should be considered for archival."""
        pass

    # -------------------------------------------------------------------------
    # Downloads CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def add_download(
        self,
        user_id: int,
        title: str,
        tmdb_id: int | None = None,
        media_type: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        quality: str | None = None,
        source: str | None = None,
        magnet_hash: str | None = None,
    ) -> Download:
        """Record a download event."""
        pass

    @abstractmethod
    async def get_downloads(
        self,
        user_id: int,
        limit: int = 50,
    ) -> list[Download]:
        """Get user's download history."""
        pass

    @abstractmethod
    async def get_pending_followups(
        self,
        days: int = 3,
    ) -> list[Download]:
        """Get downloads that need follow-up (older than N days, not followed up)."""
        pass

    @abstractmethod
    async def mark_followup_sent(
        self,
        download_id: int,
    ) -> bool:
        """Mark follow-up as sent."""
        pass

    @abstractmethod
    async def mark_followup_answered(
        self,
        download_id: int,
        rating: float | None = None,
    ) -> bool:
        """Mark follow-up as answered with optional rating."""
        pass

    @abstractmethod
    async def get_download(
        self,
        download_id: int,
    ) -> Download | None:
        """Get a single download by ID."""
        pass

    # -------------------------------------------------------------------------
    # Pending Pushes CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def create_pending_push(
        self,
        user_id: int,
        push_type: str,
        priority: int,
        content: dict[str, Any],
    ) -> PendingPush:
        """Create a pending push notification."""
        pass

    @abstractmethod
    async def get_pending_pushes(
        self,
        user_id: int | None = None,
        push_type: str | None = None,
    ) -> list[PendingPush]:
        """Get pending pushes, optionally filtered by user or type."""
        pass

    @abstractmethod
    async def get_highest_priority_push(
        self,
        user_id: int,
    ) -> PendingPush | None:
        """Get the highest priority unsent push for a user."""
        pass

    @abstractmethod
    async def mark_push_sent(
        self,
        push_id: int,
    ) -> bool:
        """Mark a push as sent."""
        pass

    @abstractmethod
    async def get_last_push_time(
        self,
        user_id: int,
    ) -> datetime | None:
        """Get the timestamp of last sent push for a user (for throttling)."""
        pass

    @abstractmethod
    async def delete_old_pushes(
        self,
        days: int = 7,
    ) -> int:
        """Delete sent pushes older than N days. Returns count deleted."""
        pass

    # -------------------------------------------------------------------------
    # Synced Torrents CRUD
    # -------------------------------------------------------------------------

    @abstractmethod
    async def track_torrent(
        self,
        user_id: int,
        torrent_hash: str,
        torrent_name: str,
        seedbox_path: str | None = None,
        size_bytes: int | None = None,
    ) -> SyncedTorrent:
        """Track a torrent sent to seedbox."""
        pass

    @abstractmethod
    async def update_torrent_status(
        self,
        torrent_hash: str,
        status: str,
        synced_at: datetime | None = None,
        local_path: str | None = None,
    ) -> bool:
        """Update torrent sync status."""
        pass

    @abstractmethod
    async def get_downloading_torrents(self) -> list[SyncedTorrent]:
        """Get all torrents with 'downloading' status."""
        pass

    @abstractmethod
    async def get_torrents_by_status(self, status: str) -> list[SyncedTorrent]:
        """Get all torrents with a given status."""
        pass

    @abstractmethod
    async def get_pending_sync_torrents(
        self,
        user_id: int | None = None,
    ) -> list[SyncedTorrent]:
        """Get torrents that are seeding (ready for sync)."""
        pass

    @abstractmethod
    async def get_user_by_torrent_hash(
        self,
        torrent_hash: str,
    ) -> User | None:
        """Get user who owns a torrent (for notifications)."""
        pass

    @abstractmethod
    async def get_user_by_torrent_name(
        self,
        name: str,
    ) -> User | None:
        """Get user who owns a torrent by name substring match (for notifications)."""
        pass

    @abstractmethod
    async def mark_torrent_deleted(
        self,
        torrent_hash: str,
    ) -> bool:
        """Mark a torrent as deleted from seedbox."""
        pass

    # -------------------------------------------------------------------------
    # Library Index
    # -------------------------------------------------------------------------

    @abstractmethod
    async def save_library_index(
        self,
        category: str,
        items_json: str,
    ) -> None:
        """Save (upsert) library index for a category (movies/tv)."""
        pass

    @abstractmethod
    async def get_library_index(
        self,
        category: str,
    ) -> str | None:
        """Get library index JSON for a category."""
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
            # Migration 12: Core memory blocks table (MemGPT-style)
            """
            CREATE TABLE IF NOT EXISTS core_memory_blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                block_name TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                max_chars INTEGER NOT NULL DEFAULT 500,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, block_name)
            );
            CREATE INDEX IF NOT EXISTS idx_core_memory_user_id ON core_memory_blocks(user_id);
            """,
            # Migration 13: Conversation sessions table
            """
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                message_count INTEGER DEFAULT 0,
                summary TEXT,
                key_learnings TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON conversation_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_status ON conversation_sessions(status);
            """,
            # Migration 14: Memory notes table (Zettelkasten-style)
            """
            CREATE TABLE IF NOT EXISTS memory_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                keywords TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TEXT NOT NULL,
                last_accessed TEXT NOT NULL,
                access_count INTEGER DEFAULT 0,
                archived_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_memory_notes_user_id ON memory_notes(user_id);
            CREATE INDEX IF NOT EXISTS idx_memory_notes_source ON memory_notes(source);
            CREATE INDEX IF NOT EXISTS idx_memory_notes_archived ON memory_notes(archived_at);
            """,
            # Migration 15: Add release_date and last_checked to monitors
            """
            ALTER TABLE monitors ADD COLUMN release_date TEXT;
            ALTER TABLE monitors ADD COLUMN last_checked TEXT;
            """,
            # Migration 16: Add found_data to monitors (stores magnet, size, seeds when found)
            """
            ALTER TABLE monitors ADD COLUMN found_data TEXT;
            """,
            # Migration 17: Add TV series episode tracking to monitors
            """
            ALTER TABLE monitors ADD COLUMN season_number INTEGER;
            ALTER TABLE monitors ADD COLUMN episode_number INTEGER;
            ALTER TABLE monitors ADD COLUMN tracking_mode TEXT DEFAULT 'season';
            """,
            # Migration 18: Downloads table for tracking user downloads
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tmdb_id INTEGER,
                media_type TEXT,
                title TEXT NOT NULL,
                season INTEGER,
                episode INTEGER,
                quality TEXT,
                source TEXT,
                magnet_hash TEXT,
                downloaded_at TEXT NOT NULL,
                followed_up INTEGER DEFAULT 0,
                rating REAL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_downloads_user_id ON downloads(user_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_followed_up ON downloads(followed_up);
            CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(downloaded_at);
            """,
            # Migration 19: Pending pushes table for proactive notifications
            """
            CREATE TABLE IF NOT EXISTS pending_pushes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                push_type TEXT NOT NULL,
                priority INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pending_pushes_user_id ON pending_pushes(user_id);
            CREATE INDEX IF NOT EXISTS idx_pending_pushes_sent_at ON pending_pushes(sent_at);
            CREATE INDEX IF NOT EXISTS idx_pending_pushes_priority ON pending_pushes(priority);
            """,
            # Migration 20: Add default_search_source to preferences
            """
            ALTER TABLE preferences ADD COLUMN default_search_source TEXT DEFAULT 'auto';
            """,
            # Migration 21: Synced torrents table for seedbox tracking
            """
            CREATE TABLE IF NOT EXISTS synced_torrents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                torrent_hash TEXT NOT NULL,
                torrent_name TEXT NOT NULL,
                seedbox_path TEXT,
                local_path TEXT,
                size_bytes INTEGER,
                status TEXT DEFAULT 'downloading',
                synced_at TEXT,
                deleted_from_seedbox_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, torrent_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_synced_torrents_user_id ON synced_torrents(user_id);
            CREATE INDEX IF NOT EXISTS idx_synced_torrents_hash ON synced_torrents(torrent_hash);
            CREATE INDEX IF NOT EXISTS idx_synced_torrents_status ON synced_torrents(status);
            """,
            # Migration 22: Add tmdb_enrichment_failed to watched table
            """
            ALTER TABLE watched ADD COLUMN tmdb_enrichment_failed BOOLEAN DEFAULT FALSE;
            """,
            # Migration 23: Library index table for NAS file browsing
            """
            CREATE TABLE IF NOT EXISTS library_index (
                category TEXT PRIMARY KEY,
                items_json TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
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
        default_search_source: str | None = None,
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
        if default_search_source is not None:
            updates.append("default_search_source = ?")
            params.append(default_search_source)

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
            claude_model=row.get("claude_model", "claude-sonnet-4-5-20250929"),
            thinking_budget=row.get("thinking_budget", 5120),
            default_search_source=row.get("default_search_source", "auto"),
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

    async def is_watched_by_title(self, user_id: int, title: str) -> bool:
        """Check if user has watched content by title (case-insensitive)."""
        cursor = await self.db.execute(
            "SELECT 1 FROM watched WHERE user_id = ? AND lower(title) = lower(?) LIMIT 1",
            (user_id, title),
        )
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
            review=row["review"],
            watched_at=datetime.fromisoformat(row["watched_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def get_watched_without_tmdb_data(
        self,
        limit: int = 50,
    ) -> list[WatchedItem]:
        """Get watched items that don't have TMDB data (for enrichment)."""
        cursor = await self.db.execute(
            """
            SELECT * FROM watched
            WHERE tmdb_id IS NULL AND title IS NOT NULL
            AND (tmdb_enrichment_failed IS NULL OR tmdb_enrichment_failed = FALSE)
            ORDER BY watched_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_watched(row) for row in rows]

    async def mark_tmdb_enrichment_failed(self, watched_id: int) -> None:
        """Mark a watched item as failed for TMDB enrichment."""
        await self.db.execute(
            "UPDATE watched SET tmdb_enrichment_failed = TRUE WHERE id = ?",
            (watched_id,),
        )
        await self.db.commit()

    async def update_watched_tmdb_data(
        self,
        watched_id: int,
        tmdb_id: int,
        director: str | None = None,
    ) -> bool:
        """Update TMDB data for a watched item."""
        # Note: director is stored in a separate table or memory, not in watched
        # We only update tmdb_id here
        await self.db.execute(
            "UPDATE watched SET tmdb_id = ? WHERE id = ?",
            (tmdb_id, watched_id),
        )
        await self.db.commit()
        return True

    async def clear_watched(self, user_id: int) -> int:
        """Delete all watched items for a user."""
        cursor = await self.db.execute("DELETE FROM watched WHERE user_id = ?", (user_id,))
        await self.db.commit()
        return cursor.rowcount or 0

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

    async def clear_watchlist(self, user_id: int) -> int:
        """Delete all watchlist items for a user."""
        cursor = await self.db.execute("DELETE FROM watchlist WHERE user_id = ?", (user_id,))
        await self.db.commit()
        return cursor.rowcount or 0

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
        tracking_mode: str = "season",
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> Monitor:
        """Create a release monitor."""
        now = datetime.now(UTC)

        cursor = await self.db.execute(
            """
            INSERT INTO monitors
                (user_id, title, tmdb_id, media_type, quality, auto_download, status,
                 release_date, tracking_mode, season_number, episode_number, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                title,
                tmdb_id,
                media_type,
                quality,
                1 if auto_download else 0,
                release_date.isoformat() if release_date else None,
                tracking_mode,
                season_number,
                episode_number,
                now.isoformat(),
            ),
        )
        await self.db.commit()

        monitor_id = cursor.lastrowid
        if monitor_id is None:
            raise RuntimeError("Failed to create monitor")

        logger.info(
            "monitor_created",
            user_id=user_id,
            title=title,
            tracking_mode=tracking_mode,
            season=season_number,
            episode=episode_number,
        )

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
            tracking_mode=tracking_mode,
            season_number=season_number,
            episode_number=episode_number,
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
        found_data: dict[str, Any] | None = None,
    ) -> Monitor | None:
        """Update monitor status and optionally store found release data."""
        found_str = found_at.isoformat() if found_at else None
        found_data_str = json.dumps(found_data) if found_data else None

        await self.db.execute(
            "UPDATE monitors SET status = ?, found_at = ?, found_data = ? WHERE id = ?",
            (status, found_str, found_data_str, monitor_id),
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
        # Parse found_data JSON if present
        found_data = None
        if "found_data" in row and row["found_data"]:
            found_data = json.loads(row["found_data"])

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
            release_date=datetime.fromisoformat(row["release_date"])
            if "release_date" in row and row["release_date"]
            else None,
            last_checked=datetime.fromisoformat(row["last_checked"])
            if "last_checked" in row and row["last_checked"]
            else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            found_data=found_data,
            # TV series episode tracking
            tracking_mode=row["tracking_mode"]
            if "tracking_mode" in row and row["tracking_mode"]
            else "season",
            season_number=row.get("season_number", None),
            episode_number=row.get("episode_number", None),
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

    # -------------------------------------------------------------------------
    # Core Memory Blocks CRUD Implementation
    # -------------------------------------------------------------------------

    async def get_core_memory_block(
        self,
        user_id: int,
        block_name: str,
    ) -> CoreMemoryBlock | None:
        """Get a specific core memory block."""
        cursor = await self.db.execute(
            "SELECT * FROM core_memory_blocks WHERE user_id = ? AND block_name = ?",
            (user_id, block_name),
        )
        row = await cursor.fetchone()
        return self._row_to_core_memory_block(row) if row else None

    async def get_all_core_memory_blocks(
        self,
        user_id: int,
    ) -> list[CoreMemoryBlock]:
        """Get all core memory blocks for a user."""
        cursor = await self.db.execute(
            "SELECT * FROM core_memory_blocks WHERE user_id = ? ORDER BY block_name",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_core_memory_block(row) for row in rows]

    async def update_core_memory_block(
        self,
        user_id: int,
        block_name: str,
        content: str,
    ) -> CoreMemoryBlock:
        """Update a core memory block (creates if not exists)."""
        now = datetime.now(UTC).isoformat()
        max_chars = CORE_MEMORY_BLOCKS.get(block_name, {}).get("max_chars", 500)

        # Truncate content if exceeds max
        if len(content) > max_chars:
            content = content[:max_chars]

        cursor = await self.db.execute(
            """
            INSERT INTO core_memory_blocks (user_id, block_name, content, max_chars, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, block_name) DO UPDATE SET
                content = excluded.content,
                updated_at = excluded.updated_at
            """,
            (user_id, block_name, content, max_chars, now),
        )
        await self.db.commit()

        block_id = cursor.lastrowid
        if block_id is None or block_id == 0:
            cursor = await self.db.execute(
                "SELECT id FROM core_memory_blocks WHERE user_id = ? AND block_name = ?",
                (user_id, block_name),
            )
            row = await cursor.fetchone()
            block_id = row["id"] if row else 0

        return CoreMemoryBlock(
            id=block_id,
            user_id=user_id,
            block_name=block_name,
            content=content,
            max_chars=max_chars,
            updated_at=datetime.fromisoformat(now),
        )

    async def initialize_core_memory_blocks(
        self,
        user_id: int,
    ) -> list[CoreMemoryBlock]:
        """Initialize all core memory blocks for a new user."""
        now = datetime.now(UTC).isoformat()
        blocks: list[CoreMemoryBlock] = []

        for block_name, config in CORE_MEMORY_BLOCKS.items():
            max_chars = config["max_chars"]
            cursor = await self.db.execute(
                """
                INSERT OR IGNORE INTO core_memory_blocks
                    (user_id, block_name, content, max_chars, updated_at)
                VALUES (?, ?, '', ?, ?)
                """,
                (user_id, block_name, max_chars, now),
            )

            block_id = cursor.lastrowid or 0
            if block_id == 0:
                cursor = await self.db.execute(
                    "SELECT id FROM core_memory_blocks WHERE user_id = ? AND block_name = ?",
                    (user_id, block_name),
                )
                row = await cursor.fetchone()
                block_id = row["id"] if row else 0

            blocks.append(
                CoreMemoryBlock(
                    id=block_id,
                    user_id=user_id,
                    block_name=block_name,
                    content="",
                    max_chars=max_chars,
                    updated_at=datetime.fromisoformat(now),
                )
            )

        await self.db.commit()
        logger.info("core_memory_blocks_initialized", user_id=user_id)
        return blocks

    def _row_to_core_memory_block(self, row: Any) -> CoreMemoryBlock:
        """Convert database row to CoreMemoryBlock model."""
        return CoreMemoryBlock(
            id=row["id"],
            user_id=row["user_id"],
            block_name=row["block_name"],
            content=row["content"],
            max_chars=row["max_chars"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # -------------------------------------------------------------------------
    # Conversation Sessions CRUD Implementation
    # -------------------------------------------------------------------------

    async def get_active_session(
        self,
        user_id: int,
    ) -> ConversationSession | None:
        """Get the active session for a user."""
        cursor = await self.db.execute(
            "SELECT * FROM conversation_sessions WHERE user_id = ? AND status = 'active' ORDER BY started_at DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_session(row) if row else None

    async def create_session(
        self,
        user_id: int,
    ) -> ConversationSession:
        """Create a new conversation session."""
        now = datetime.now(UTC).isoformat()

        cursor = await self.db.execute(
            """
            INSERT INTO conversation_sessions (user_id, started_at, status)
            VALUES (?, ?, 'active')
            """,
            (user_id, now),
        )
        await self.db.commit()

        session_id = cursor.lastrowid
        if session_id is None:
            raise RuntimeError("Failed to create session")

        logger.info("session_created", user_id=user_id, session_id=session_id)

        return ConversationSession(
            id=session_id,
            user_id=user_id,
            started_at=datetime.fromisoformat(now),
            status="active",
        )

    async def end_session(
        self,
        session_id: int,
        summary: str | None = None,
        key_learnings: list[str] | None = None,
    ) -> ConversationSession | None:
        """End a session and optionally add summary."""
        now = datetime.now(UTC).isoformat()
        learnings_json = json.dumps(key_learnings or [])

        await self.db.execute(
            """
            UPDATE conversation_sessions
            SET ended_at = ?, summary = ?, key_learnings = ?, status = 'ended'
            WHERE id = ?
            """,
            (now, summary, learnings_json, session_id),
        )
        await self.db.commit()

        cursor = await self.db.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_session(row) if row else None

    async def increment_session_message_count(
        self,
        session_id: int,
    ) -> None:
        """Increment the message count for a session."""
        await self.db.execute(
            "UPDATE conversation_sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
        await self.db.commit()

    async def get_recent_sessions(
        self,
        user_id: int,
        limit: int = 10,
        days: int = 30,
    ) -> list[ConversationSession]:
        """Get recent sessions for a user."""
        cutoff = (datetime.now(UTC) - __import__("datetime").timedelta(days=days)).isoformat()

        cursor = await self.db.execute(
            """
            SELECT * FROM conversation_sessions
            WHERE user_id = ? AND started_at >= ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (user_id, cutoff, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_session(row) for row in rows]

    def _row_to_session(self, row: Any) -> ConversationSession:
        """Convert database row to ConversationSession model."""
        return ConversationSession(
            id=row["id"],
            user_id=row["user_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            message_count=row["message_count"],
            summary=row["summary"],
            key_learnings=json.loads(row["key_learnings"] or "[]"),
            status=row["status"],
        )

    # -------------------------------------------------------------------------
    # Memory Notes CRUD Implementation
    # -------------------------------------------------------------------------

    async def create_memory_note(
        self,
        user_id: int,
        content: str,
        source: str,
        keywords: list[str] | None = None,
        confidence: float = 0.5,
    ) -> MemoryNote:
        """Create a new memory note."""
        now = datetime.now(UTC).isoformat()
        keywords_json = json.dumps(keywords or [])

        cursor = await self.db.execute(
            """
            INSERT INTO memory_notes
                (user_id, content, source, keywords, confidence, created_at, last_accessed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, content, source, keywords_json, confidence, now, now),
        )
        await self.db.commit()

        note_id = cursor.lastrowid
        if note_id is None:
            raise RuntimeError("Failed to create memory note")

        logger.info("memory_note_created", user_id=user_id, source=source)

        return MemoryNote(
            id=note_id,
            user_id=user_id,
            content=content,
            source=source,
            keywords=keywords or [],
            confidence=confidence,
            created_at=datetime.fromisoformat(now),
            last_accessed=datetime.fromisoformat(now),
        )

    async def get_memory_notes(
        self,
        user_id: int,
        source: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[MemoryNote]:
        """Get memory notes for a user."""
        query = "SELECT * FROM memory_notes WHERE user_id = ?"
        params: list[Any] = [user_id]

        if source:
            query += " AND source = ?"
            params.append(source)

        if not include_archived:
            query += " AND archived_at IS NULL"

        query += " ORDER BY last_accessed DESC LIMIT ?"
        params.append(limit)

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_memory_note(row) for row in rows]

    async def search_memory_notes(
        self,
        user_id: int,
        query: str,
        limit: int = 10,
    ) -> list[MemoryNote]:
        """Search memory notes by keywords or content (word-level matching)."""
        words = [w.strip() for w in query.split() if len(w.strip()) >= 3]
        if not words:
            words = [query]

        # Build OR conditions for each word
        conditions = []
        params: list[object] = [user_id]
        for word in words:
            conditions.append("(content LIKE ? OR keywords LIKE ?)")
            params.extend([f"%{word}%", f"%{word}%"])

        where_clause = " OR ".join(conditions)
        params.append(limit)

        cursor = await self.db.execute(
            f"""
            SELECT * FROM memory_notes
            WHERE user_id = ? AND archived_at IS NULL
                AND ({where_clause})
            ORDER BY confidence DESC, access_count DESC
            LIMIT ?
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_note(row) for row in rows]

    async def update_memory_note_access(
        self,
        note_id: int,
    ) -> None:
        """Update last_accessed and increment access_count."""
        now = datetime.now(UTC).isoformat()
        await self.db.execute(
            """
            UPDATE memory_notes
            SET last_accessed = ?, access_count = access_count + 1
            WHERE id = ?
            """,
            (now, note_id),
        )
        await self.db.commit()

    async def archive_memory_note(
        self,
        note_id: int,
    ) -> bool:
        """Archive a memory note."""
        now = datetime.now(UTC).isoformat()
        cursor = await self.db.execute(
            "UPDATE memory_notes SET archived_at = ? WHERE id = ?",
            (now, note_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def get_notes_for_archival(
        self,
        user_id: int,
        age_days: int = 90,
        min_access_count: int = 3,
    ) -> list[MemoryNote]:
        """Get notes that should be considered for archival."""
        cutoff = (datetime.now(UTC) - __import__("datetime").timedelta(days=age_days)).isoformat()

        cursor = await self.db.execute(
            """
            SELECT * FROM memory_notes
            WHERE user_id = ?
                AND archived_at IS NULL
                AND created_at < ?
                AND access_count < ?
            ORDER BY access_count ASC, last_accessed ASC
            """,
            (user_id, cutoff, min_access_count),
        )
        rows = await cursor.fetchall()
        return [self._row_to_memory_note(row) for row in rows]

    def _row_to_memory_note(self, row: Any) -> MemoryNote:
        """Convert database row to MemoryNote model."""
        return MemoryNote(
            id=row["id"],
            user_id=row["user_id"],
            content=row["content"],
            source=row["source"],
            keywords=json.loads(row["keywords"] or "[]"),
            confidence=row["confidence"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_accessed=datetime.fromisoformat(row["last_accessed"]),
            access_count=row["access_count"],
            archived_at=datetime.fromisoformat(row["archived_at"]) if row["archived_at"] else None,
        )

    # -------------------------------------------------------------------------
    # Downloads CRUD Implementation
    # -------------------------------------------------------------------------

    async def add_download(
        self,
        user_id: int,
        title: str,
        tmdb_id: int | None = None,
        media_type: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        quality: str | None = None,
        source: str | None = None,
        magnet_hash: str | None = None,
    ) -> Download:
        """Record a download event."""
        now = datetime.now(UTC).isoformat()

        cursor = await self.db.execute(
            """
            INSERT INTO downloads (user_id, tmdb_id, media_type, title, season, episode,
                                   quality, source, magnet_hash, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                tmdb_id,
                media_type,
                title,
                season,
                episode,
                quality,
                source,
                magnet_hash,
                now,
            ),
        )
        await self.db.commit()

        download_id = cursor.lastrowid
        if download_id is None:
            raise RuntimeError("Failed to create download record")

        logger.info("download_recorded", user_id=user_id, title=title, source=source)

        return Download(
            id=download_id,
            user_id=user_id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            season=season,
            episode=episode,
            quality=quality,
            source=source,
            magnet_hash=magnet_hash,
            downloaded_at=datetime.fromisoformat(now),
            followed_up=0,
            rating=None,
        )

    async def get_downloads(
        self,
        user_id: int,
        limit: int = 50,
    ) -> list[Download]:
        """Get user's download history."""
        cursor = await self.db.execute(
            """
            SELECT * FROM downloads
            WHERE user_id = ?
            ORDER BY downloaded_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_download(row) for row in rows]

    async def get_pending_followups(
        self,
        days: int = 3,
    ) -> list[Download]:
        """Get downloads that need follow-up (older than N days, not followed up)."""
        cutoff = (datetime.now(UTC) - __import__("datetime").timedelta(days=days)).isoformat()

        cursor = await self.db.execute(
            """
            SELECT * FROM downloads
            WHERE followed_up = 0
                AND downloaded_at < ?
            ORDER BY downloaded_at ASC
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_download(row) for row in rows]

    async def mark_followup_sent(
        self,
        download_id: int,
    ) -> bool:
        """Mark follow-up as sent."""
        cursor = await self.db.execute(
            "UPDATE downloads SET followed_up = 1 WHERE id = ?",
            (download_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def mark_followup_answered(
        self,
        download_id: int,
        rating: float | None = None,
    ) -> bool:
        """Mark follow-up as answered with optional rating."""
        if rating is not None:
            cursor = await self.db.execute(
                "UPDATE downloads SET followed_up = 2, rating = ? WHERE id = ?",
                (rating, download_id),
            )
        else:
            cursor = await self.db.execute(
                "UPDATE downloads SET followed_up = 2 WHERE id = ?",
                (download_id,),
            )
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def get_download(
        self,
        download_id: int,
    ) -> Download | None:
        """Get a single download by ID."""
        cursor = await self.db.execute(
            "SELECT * FROM downloads WHERE id = ?",
            (download_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_download(row) if row else None

    def _row_to_download(self, row: Any) -> Download:
        """Convert database row to Download model."""
        return Download(
            id=row["id"],
            user_id=row["user_id"],
            tmdb_id=row["tmdb_id"],
            media_type=row["media_type"],
            title=row["title"],
            season=row["season"],
            episode=row["episode"],
            quality=row["quality"],
            source=row["source"],
            magnet_hash=row["magnet_hash"],
            downloaded_at=datetime.fromisoformat(row["downloaded_at"]),
            followed_up=row["followed_up"],
            rating=row["rating"],
        )

    # -------------------------------------------------------------------------
    # Pending Pushes CRUD Implementation
    # -------------------------------------------------------------------------

    async def create_pending_push(
        self,
        user_id: int,
        push_type: str,
        priority: int,
        content: dict[str, Any],
    ) -> PendingPush:
        """Create a pending push notification."""
        now = datetime.now(UTC).isoformat()

        cursor = await self.db.execute(
            """
            INSERT INTO pending_pushes (user_id, push_type, priority, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, push_type, priority, json.dumps(content), now),
        )
        await self.db.commit()

        push_id = cursor.lastrowid
        if push_id is None:
            raise RuntimeError("Failed to create pending push")

        logger.info("pending_push_created", user_id=user_id, push_type=push_type, priority=priority)

        return PendingPush(
            id=push_id,
            user_id=user_id,
            push_type=push_type,
            priority=priority,
            content=content,
            created_at=datetime.fromisoformat(now),
            sent_at=None,
        )

    async def get_pending_pushes(
        self,
        user_id: int | None = None,
        push_type: str | None = None,
    ) -> list[PendingPush]:
        """Get pending pushes, optionally filtered by user or type."""
        query = "SELECT * FROM pending_pushes WHERE sent_at IS NULL"
        params: list[Any] = []

        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        if push_type is not None:
            query += " AND push_type = ?"
            params.append(push_type)

        query += " ORDER BY priority ASC, created_at ASC"

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [self._row_to_pending_push(row) for row in rows]

    async def get_highest_priority_push(
        self,
        user_id: int,
    ) -> PendingPush | None:
        """Get the highest priority unsent push for a user."""
        cursor = await self.db.execute(
            """
            SELECT * FROM pending_pushes
            WHERE user_id = ? AND sent_at IS NULL
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_pending_push(row) if row else None

    async def mark_push_sent(
        self,
        push_id: int,
    ) -> bool:
        """Mark a push as sent."""
        now = datetime.now(UTC).isoformat()
        cursor = await self.db.execute(
            "UPDATE pending_pushes SET sent_at = ? WHERE id = ?",
            (now, push_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def get_last_push_time(
        self,
        user_id: int,
    ) -> datetime | None:
        """Get the timestamp of last sent push for a user (for throttling)."""
        cursor = await self.db.execute(
            """
            SELECT MAX(sent_at) as last_sent FROM pending_pushes
            WHERE user_id = ? AND sent_at IS NOT NULL
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row and row["last_sent"]:
            return datetime.fromisoformat(row["last_sent"])
        return None

    async def delete_old_pushes(
        self,
        days: int = 7,
    ) -> int:
        """Delete sent pushes older than N days. Returns count deleted."""
        cutoff = (datetime.now(UTC) - __import__("datetime").timedelta(days=days)).isoformat()
        cursor = await self.db.execute(
            "DELETE FROM pending_pushes WHERE sent_at IS NOT NULL AND sent_at < ?",
            (cutoff,),
        )
        await self.db.commit()
        return cursor.rowcount if cursor.rowcount else 0

    def _row_to_pending_push(self, row: Any) -> PendingPush:
        """Convert database row to PendingPush model."""
        return PendingPush(
            id=row["id"],
            user_id=row["user_id"],
            push_type=row["push_type"],
            priority=row["priority"],
            content=json.loads(row["content"])
            if isinstance(row["content"], str)
            else row["content"],
            created_at=datetime.fromisoformat(row["created_at"]),
            sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
        )

    # -------------------------------------------------------------------------
    # Synced Torrents CRUD
    # -------------------------------------------------------------------------

    async def track_torrent(
        self,
        user_id: int,
        torrent_hash: str,
        torrent_name: str,
        seedbox_path: str | None = None,
        size_bytes: int | None = None,
    ) -> SyncedTorrent:
        """Track a torrent sent to seedbox."""
        now = datetime.now(UTC).isoformat()
        cursor = await self.db.execute(
            """
            INSERT INTO synced_torrents (user_id, torrent_hash, torrent_name, seedbox_path, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, torrent_hash) DO UPDATE SET
                torrent_name = excluded.torrent_name,
                seedbox_path = excluded.seedbox_path,
                size_bytes = excluded.size_bytes,
                status = 'downloading'
            RETURNING *
            """,
            (user_id, torrent_hash, torrent_name, seedbox_path, size_bytes, now),
        )
        row = await cursor.fetchone()
        await self.db.commit()

        if row:
            return self._row_to_synced_torrent(row)

        # Fallback for older SQLite versions without RETURNING
        cursor = await self.db.execute(
            "SELECT * FROM synced_torrents WHERE user_id = ? AND torrent_hash = ?",
            (user_id, torrent_hash),
        )
        row = await cursor.fetchone()
        return self._row_to_synced_torrent(row)

    async def update_torrent_status(
        self,
        torrent_hash: str,
        status: str,
        synced_at: datetime | None = None,
        local_path: str | None = None,
    ) -> bool:
        """Update torrent sync status."""
        synced_at_str = synced_at.isoformat() if synced_at else None
        cursor = await self.db.execute(
            """
            UPDATE synced_torrents SET status = ?, synced_at = ?, local_path = ?
            WHERE torrent_hash = ?
            """,
            (status, synced_at_str, local_path, torrent_hash),
        )
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    async def get_downloading_torrents(self) -> list[SyncedTorrent]:
        """Get all torrents with 'downloading' status."""
        cursor = await self.db.execute(
            "SELECT * FROM synced_torrents WHERE status = 'downloading' ORDER BY created_at",
        )
        rows = await cursor.fetchall()
        return [self._row_to_synced_torrent(row) for row in rows]

    async def get_torrents_by_status(self, status: str) -> list[SyncedTorrent]:
        """Get all torrents with a given status."""
        cursor = await self.db.execute(
            "SELECT * FROM synced_torrents WHERE status = ? ORDER BY created_at",
            (status,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_synced_torrent(row) for row in rows]

    async def get_pending_sync_torrents(
        self,
        user_id: int | None = None,
    ) -> list[SyncedTorrent]:
        """Get torrents that are seeding (ready for sync)."""
        if user_id:
            cursor = await self.db.execute(
                "SELECT * FROM synced_torrents WHERE user_id = ? AND status = 'seeding' ORDER BY created_at",
                (user_id,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM synced_torrents WHERE status = 'seeding' ORDER BY created_at",
            )
        rows = await cursor.fetchall()
        return [self._row_to_synced_torrent(row) for row in rows]

    async def get_user_by_torrent_hash(
        self,
        torrent_hash: str,
    ) -> User | None:
        """Get user who owns a torrent (for notifications)."""
        cursor = await self.db.execute(
            """
            SELECT u.* FROM users u
            JOIN synced_torrents st ON u.id = st.user_id
            WHERE st.torrent_hash = ?
            """,
            (torrent_hash,),
        )
        row = await cursor.fetchone()
        return self._row_to_user(row) if row else None

    async def get_user_by_torrent_name(
        self,
        name: str,
    ) -> User | None:
        """Get user who owns a torrent by name substring match."""
        import re

        words = re.split(r"[\s._]+", name.strip())
        pattern = "%" + "%".join(w for w in words if w) + "%"
        cursor = await self.db.execute(
            """
            SELECT u.* FROM users u
            JOIN synced_torrents st ON u.id = st.user_id
            WHERE st.torrent_name LIKE ? COLLATE NOCASE
            AND st.status IN ('seeding', 'downloading')
            ORDER BY st.created_at DESC
            LIMIT 1
            """,
            (pattern,),
        )
        row = await cursor.fetchone()
        return self._row_to_user(row) if row else None

    async def mark_torrent_deleted(
        self,
        torrent_hash: str,
    ) -> bool:
        """Mark a torrent as deleted from seedbox."""
        now = datetime.now(UTC).isoformat()
        cursor = await self.db.execute(
            "UPDATE synced_torrents SET status = 'deleted', deleted_from_seedbox_at = ? WHERE torrent_hash = ?",
            (now, torrent_hash),
        )
        await self.db.commit()
        return cursor.rowcount > 0 if cursor.rowcount else False

    def _row_to_synced_torrent(self, row: Any) -> SyncedTorrent:
        """Convert database row to SyncedTorrent model."""
        return SyncedTorrent(
            id=row["id"],
            user_id=row["user_id"],
            torrent_hash=row["torrent_hash"],
            torrent_name=row["torrent_name"],
            seedbox_path=row["seedbox_path"],
            local_path=row["local_path"],
            size_bytes=row["size_bytes"],
            status=row["status"] or "downloading",
            synced_at=datetime.fromisoformat(row["synced_at"]) if row["synced_at"] else None,
            deleted_from_seedbox_at=datetime.fromisoformat(row["deleted_from_seedbox_at"])
            if row["deleted_from_seedbox_at"]
            else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # -------------------------------------------------------------------------
    # Library Index
    # -------------------------------------------------------------------------

    async def save_library_index(
        self,
        category: str,
        items_json: str,
    ) -> None:
        """Save (upsert) library index for a category."""
        now = datetime.now(UTC).isoformat()
        await self.db.execute(
            """
            INSERT INTO library_index (category, items_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(category) DO UPDATE SET items_json = excluded.items_json, updated_at = excluded.updated_at
            """,
            (category, items_json, now),
        )
        await self.db.commit()

    async def get_library_index(
        self,
        category: str,
    ) -> str | None:
        """Get library index JSON for a category."""
        cursor = await self.db.execute(
            "SELECT items_json FROM library_index WHERE category = ?",
            (category,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


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

        logger.debug("postgres_connected")

    async def close(self) -> None:
        """Close database connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.debug("postgres_disconnected")

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
            # Migration 12: Core memory blocks table (MemGPT-style)
            """
            CREATE TABLE IF NOT EXISTS core_memory_blocks (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                block_name TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                max_chars INTEGER NOT NULL DEFAULT 500,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(user_id, block_name)
            );
            CREATE INDEX IF NOT EXISTS idx_core_memory_user_id ON core_memory_blocks(user_id);
            """,
            # Migration 13: Conversation sessions table
            """
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended_at TIMESTAMPTZ,
                message_count INTEGER DEFAULT 0,
                summary TEXT,
                key_learnings JSONB DEFAULT '[]',
                status TEXT DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON conversation_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_status ON conversation_sessions(status);
            """,
            # Migration 14: Memory notes table (Zettelkasten-style)
            """
            CREATE TABLE IF NOT EXISTS memory_notes (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                keywords JSONB DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_accessed TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                access_count INTEGER DEFAULT 0,
                archived_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_memory_notes_user_id ON memory_notes(user_id);
            CREATE INDEX IF NOT EXISTS idx_memory_notes_source ON memory_notes(source);
            CREATE INDEX IF NOT EXISTS idx_memory_notes_archived ON memory_notes(archived_at);
            """,
            # Migration 15: Add release_date and last_checked to monitors
            """
            ALTER TABLE monitors ADD COLUMN IF NOT EXISTS release_date TIMESTAMPTZ;
            ALTER TABLE monitors ADD COLUMN IF NOT EXISTS last_checked TIMESTAMPTZ;
            """,
            # Migration 16: Add found_data to monitors (stores magnet, size, seeds when found)
            """
            ALTER TABLE monitors ADD COLUMN IF NOT EXISTS found_data TEXT;
            """,
            # Migration 17: Add TV series episode tracking to monitors
            """
            ALTER TABLE monitors ADD COLUMN IF NOT EXISTS season_number INTEGER;
            ALTER TABLE monitors ADD COLUMN IF NOT EXISTS episode_number INTEGER;
            ALTER TABLE monitors ADD COLUMN IF NOT EXISTS tracking_mode TEXT DEFAULT 'season';
            """,
            # Migration 18: Downloads table for tracking user downloads
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tmdb_id INTEGER,
                media_type TEXT,
                title TEXT NOT NULL,
                season INTEGER,
                episode INTEGER,
                quality TEXT,
                source TEXT,
                magnet_hash TEXT,
                downloaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                followed_up INTEGER DEFAULT 0,
                rating REAL
            );
            CREATE INDEX IF NOT EXISTS idx_downloads_user_id ON downloads(user_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_followed_up ON downloads(followed_up);
            CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(downloaded_at);
            """,
            # Migration 19: Pending pushes table for proactive notifications
            """
            CREATE TABLE IF NOT EXISTS pending_pushes (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                push_type TEXT NOT NULL,
                priority INTEGER NOT NULL,
                content JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sent_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_pending_pushes_user_id ON pending_pushes(user_id);
            CREATE INDEX IF NOT EXISTS idx_pending_pushes_sent_at ON pending_pushes(sent_at);
            CREATE INDEX IF NOT EXISTS idx_pending_pushes_priority ON pending_pushes(priority);
            """,
            # Migration 20: Add default_search_source to preferences
            """
            ALTER TABLE preferences ADD COLUMN IF NOT EXISTS default_search_source TEXT DEFAULT 'auto';
            """,
            # Migration 21: Synced torrents table for seedbox tracking
            """
            CREATE TABLE IF NOT EXISTS synced_torrents (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                torrent_hash TEXT NOT NULL,
                torrent_name TEXT NOT NULL,
                seedbox_path TEXT,
                local_path TEXT,
                size_bytes BIGINT,
                status TEXT DEFAULT 'downloading',
                synced_at TIMESTAMPTZ,
                deleted_from_seedbox_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, torrent_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_synced_torrents_user_id ON synced_torrents(user_id);
            CREATE INDEX IF NOT EXISTS idx_synced_torrents_hash ON synced_torrents(torrent_hash);
            CREATE INDEX IF NOT EXISTS idx_synced_torrents_status ON synced_torrents(status);
            """,
            # Migration 22: Add tmdb_enrichment_failed to watched table
            """
            ALTER TABLE watched ADD COLUMN IF NOT EXISTS tmdb_enrichment_failed BOOLEAN DEFAULT FALSE;
            """,
            # Migration 23: Library index table for NAS file browsing
            """
            CREATE TABLE IF NOT EXISTS library_index (
                category TEXT PRIMARY KEY,
                items_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
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
        default_search_source: str | None = None,
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
        if default_search_source is not None:
            updates.append(f"default_search_source = ${param_idx}")
            params.append(default_search_source)
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
            thinking_budget=row.get("thinking_budget", 5120),
            default_search_source=row.get("default_search_source", "auto"),
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

    async def is_watched_by_title(self, user_id: int, title: str) -> bool:
        """Check if user has watched content by title (case-insensitive)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM watched WHERE user_id = $1 AND lower(title) = lower($2) LIMIT 1",
                user_id,
                title,
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

    async def get_watched_without_tmdb_data(
        self,
        limit: int = 50,
    ) -> list[WatchedItem]:
        """Get watched items that don't have TMDB data (for enrichment)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM watched
                WHERE tmdb_id IS NULL AND title IS NOT NULL
                AND (tmdb_enrichment_failed IS NULL OR tmdb_enrichment_failed = FALSE)
                ORDER BY watched_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [self._row_to_watched(row) for row in rows]

    async def mark_tmdb_enrichment_failed(self, watched_id: int) -> None:
        """Mark a watched item as failed for TMDB enrichment."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE watched SET tmdb_enrichment_failed = TRUE WHERE id = $1",
                watched_id,
            )

    async def update_watched_tmdb_data(
        self,
        watched_id: int,
        tmdb_id: int,
        director: str | None = None,
    ) -> bool:
        """Update TMDB data for a watched item."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE watched SET tmdb_id = $1 WHERE id = $2",
                tmdb_id,
                watched_id,
            )
        return True

    async def clear_watched(self, user_id: int) -> int:
        """Delete all watched items for a user."""
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM watched WHERE user_id = $1", user_id)
        return int(result.split()[-1])

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

    async def clear_watchlist(self, user_id: int) -> int:
        """Delete all watchlist items for a user."""
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM watchlist WHERE user_id = $1", user_id)
        return int(result.split()[-1])

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
        tracking_mode: str = "season",
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> Monitor:
        """Create a release monitor."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO monitors
                    (user_id, title, tmdb_id, media_type, quality, auto_download,
                     release_date, tracking_mode, season_number, episode_number)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
                """,
                user_id,
                title,
                tmdb_id,
                media_type,
                quality,
                auto_download,
                release_date,
                tracking_mode,
                season_number,
                episode_number,
            )

        logger.info(
            "monitor_created",
            user_id=user_id,
            title=title,
            tracking_mode=tracking_mode,
            season=season_number,
            episode=episode_number,
        )
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
        found_data: dict[str, Any] | None = None,
    ) -> Monitor | None:
        """Update monitor status and optionally store found release data."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE monitors
                SET status = $1, found_at = $2, found_data = $3
                WHERE id = $4 RETURNING *""",
                status,
                found_at,
                json.dumps(found_data) if found_data else None,
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
        # Parse found_data JSON if present
        found_data = None
        if row.get("found_data"):
            found_data = json.loads(row["found_data"])

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
            found_data=found_data,
            # TV series episode tracking
            tracking_mode=row.get("tracking_mode") or "season",
            season_number=row.get("season_number"),
            episode_number=row.get("episode_number"),
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

    # -------------------------------------------------------------------------
    # Core Memory Blocks CRUD Implementation
    # -------------------------------------------------------------------------

    async def get_core_memory_block(
        self,
        user_id: int,
        block_name: str,
    ) -> CoreMemoryBlock | None:
        """Get a specific core memory block."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM core_memory_blocks WHERE user_id = $1 AND block_name = $2",
                user_id,
                block_name,
            )
        return self._row_to_core_memory_block(row) if row else None

    async def get_all_core_memory_blocks(
        self,
        user_id: int,
    ) -> list[CoreMemoryBlock]:
        """Get all core memory blocks for a user."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM core_memory_blocks WHERE user_id = $1 ORDER BY block_name",
                user_id,
            )
        return [self._row_to_core_memory_block(row) for row in rows]

    async def update_core_memory_block(
        self,
        user_id: int,
        block_name: str,
        content: str,
    ) -> CoreMemoryBlock:
        """Update a core memory block (creates if not exists)."""
        max_chars = CORE_MEMORY_BLOCKS.get(block_name, {}).get("max_chars", 500)

        # Truncate content if exceeds max
        if len(content) > max_chars:
            content = content[:max_chars]

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO core_memory_blocks (user_id, block_name, content, max_chars)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT(user_id, block_name) DO UPDATE SET
                    content = EXCLUDED.content,
                    updated_at = NOW()
                RETURNING *
                """,
                user_id,
                block_name,
                content,
                max_chars,
            )

        return self._row_to_core_memory_block(row)

    async def initialize_core_memory_blocks(
        self,
        user_id: int,
    ) -> list[CoreMemoryBlock]:
        """Initialize all core memory blocks for a new user."""
        blocks: list[CoreMemoryBlock] = []

        async with self.pool.acquire() as conn:
            for block_name, config in CORE_MEMORY_BLOCKS.items():
                max_chars = config["max_chars"]
                row = await conn.fetchrow(
                    """
                    INSERT INTO core_memory_blocks (user_id, block_name, content, max_chars)
                    VALUES ($1, $2, '', $3)
                    ON CONFLICT(user_id, block_name) DO NOTHING
                    RETURNING *
                    """,
                    user_id,
                    block_name,
                    max_chars,
                )

                if row is None:
                    # Already exists, fetch it
                    row = await conn.fetchrow(
                        "SELECT * FROM core_memory_blocks WHERE user_id = $1 AND block_name = $2",
                        user_id,
                        block_name,
                    )

                if row:
                    blocks.append(self._row_to_core_memory_block(row))

        logger.info("core_memory_blocks_initialized", user_id=user_id)
        return blocks

    def _row_to_core_memory_block(self, row: Any) -> CoreMemoryBlock:
        """Convert database row to CoreMemoryBlock model."""
        return CoreMemoryBlock(
            id=row["id"],
            user_id=row["user_id"],
            block_name=row["block_name"],
            content=row["content"],
            max_chars=row["max_chars"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------------
    # Conversation Sessions CRUD Implementation
    # -------------------------------------------------------------------------

    async def get_active_session(
        self,
        user_id: int,
    ) -> ConversationSession | None:
        """Get the active session for a user."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM conversation_sessions WHERE user_id = $1 AND status = 'active' ORDER BY started_at DESC LIMIT 1",
                user_id,
            )
        return self._row_to_session(row) if row else None

    async def create_session(
        self,
        user_id: int,
    ) -> ConversationSession:
        """Create a new conversation session."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversation_sessions (user_id, status)
                VALUES ($1, 'active')
                RETURNING *
                """,
                user_id,
            )

        logger.info("session_created", user_id=user_id, session_id=row["id"])
        return self._row_to_session(row)

    async def end_session(
        self,
        session_id: int,
        summary: str | None = None,
        key_learnings: list[str] | None = None,
    ) -> ConversationSession | None:
        """End a session and optionally add summary."""
        learnings_json = json.dumps(key_learnings or [])

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE conversation_sessions
                SET ended_at = NOW(), summary = $2, key_learnings = $3::jsonb, status = 'ended'
                WHERE id = $1
                RETURNING *
                """,
                session_id,
                summary,
                learnings_json,
            )

        return self._row_to_session(row) if row else None

    async def increment_session_message_count(
        self,
        session_id: int,
    ) -> None:
        """Increment the message count for a session."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversation_sessions SET message_count = message_count + 1 WHERE id = $1",
                session_id,
            )

    async def get_recent_sessions(
        self,
        user_id: int,
        limit: int = 10,
        days: int = 30,
    ) -> list[ConversationSession]:
        """Get recent sessions for a user."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM conversation_sessions
                WHERE user_id = $1 AND started_at >= NOW() - INTERVAL '1 day' * $2
                ORDER BY started_at DESC
                LIMIT $3
                """,
                user_id,
                days,
                limit,
            )
        return [self._row_to_session(row) for row in rows]

    def _row_to_session(self, row: Any) -> ConversationSession:
        """Convert database row to ConversationSession model."""
        key_learnings = row["key_learnings"] or []
        if isinstance(key_learnings, str):
            key_learnings = json.loads(key_learnings)

        return ConversationSession(
            id=row["id"],
            user_id=row["user_id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            message_count=row["message_count"],
            summary=row["summary"],
            key_learnings=key_learnings,
            status=row["status"],
        )

    # -------------------------------------------------------------------------
    # Memory Notes CRUD Implementation
    # -------------------------------------------------------------------------

    async def create_memory_note(
        self,
        user_id: int,
        content: str,
        source: str,
        keywords: list[str] | None = None,
        confidence: float = 0.5,
    ) -> MemoryNote:
        """Create a new memory note."""
        keywords_json = json.dumps(keywords or [])

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO memory_notes
                    (user_id, content, source, keywords, confidence)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                RETURNING *
                """,
                user_id,
                content,
                source,
                keywords_json,
                confidence,
            )

        logger.info("memory_note_created", user_id=user_id, source=source)
        return self._row_to_memory_note(row)

    async def get_memory_notes(
        self,
        user_id: int,
        source: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[MemoryNote]:
        """Get memory notes for a user."""
        query = "SELECT * FROM memory_notes WHERE user_id = $1"
        params: list[Any] = [user_id]
        param_idx = 2

        if source:
            query += f" AND source = ${param_idx}"
            params.append(source)
            param_idx += 1

        if not include_archived:
            query += " AND archived_at IS NULL"

        query += f" ORDER BY last_accessed DESC LIMIT ${param_idx}"
        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [self._row_to_memory_note(row) for row in rows]

    async def search_memory_notes(
        self,
        user_id: int,
        query: str,
        limit: int = 10,
    ) -> list[MemoryNote]:
        """Search memory notes by keywords or content (word-level matching)."""
        words = [w.strip() for w in query.split() if len(w.strip()) >= 3]
        if not words:
            words = [query]

        # Build OR conditions for each word
        conditions = []
        params: list[object] = [user_id]
        for word in words:
            idx = len(params) + 1
            conditions.append(f"(content ILIKE ${idx} OR keywords::text ILIKE ${idx})")
            params.append(f"%{word}%")

        where_clause = " OR ".join(conditions)
        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT * FROM memory_notes
                WHERE user_id = $1 AND archived_at IS NULL
                    AND ({where_clause})
                ORDER BY confidence DESC, access_count DESC
                LIMIT ${len(params)}
                """,
                *params,
            )
        return [self._row_to_memory_note(row) for row in rows]

    async def update_memory_note_access(
        self,
        note_id: int,
    ) -> None:
        """Update last_accessed and increment access_count."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memory_notes
                SET last_accessed = NOW(), access_count = access_count + 1
                WHERE id = $1
                """,
                note_id,
            )

    async def archive_memory_note(
        self,
        note_id: int,
    ) -> bool:
        """Archive a memory note."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE memory_notes SET archived_at = NOW() WHERE id = $1",
                note_id,
            )
        return result == "UPDATE 1"

    async def get_notes_for_archival(
        self,
        user_id: int,
        age_days: int = 90,
        min_access_count: int = 3,
    ) -> list[MemoryNote]:
        """Get notes that should be considered for archival."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM memory_notes
                WHERE user_id = $1
                    AND archived_at IS NULL
                    AND created_at < NOW() - INTERVAL '1 day' * $2
                    AND access_count < $3
                ORDER BY access_count ASC, last_accessed ASC
                """,
                user_id,
                age_days,
                min_access_count,
            )
        return [self._row_to_memory_note(row) for row in rows]

    def _row_to_memory_note(self, row: Any) -> MemoryNote:
        """Convert database row to MemoryNote model."""
        keywords = row["keywords"] or []
        if isinstance(keywords, str):
            keywords = json.loads(keywords)

        return MemoryNote(
            id=row["id"],
            user_id=row["user_id"],
            content=row["content"],
            source=row["source"],
            keywords=keywords,
            confidence=row["confidence"],
            created_at=row["created_at"],
            last_accessed=row["last_accessed"],
            access_count=row["access_count"],
            archived_at=row["archived_at"],
        )

    # -------------------------------------------------------------------------
    # Downloads CRUD Implementation
    # -------------------------------------------------------------------------

    async def add_download(
        self,
        user_id: int,
        title: str,
        tmdb_id: int | None = None,
        media_type: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        quality: str | None = None,
        source: str | None = None,
        magnet_hash: str | None = None,
    ) -> Download:
        """Record a download event."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO downloads (user_id, tmdb_id, media_type, title, season, episode,
                                       quality, source, magnet_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
                """,
                user_id,
                tmdb_id,
                media_type,
                title,
                season,
                episode,
                quality,
                source,
                magnet_hash,
            )

        logger.info("download_recorded", user_id=user_id, title=title, source=source)
        return self._row_to_download(row)

    async def get_downloads(
        self,
        user_id: int,
        limit: int = 50,
    ) -> list[Download]:
        """Get user's download history."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM downloads
                WHERE user_id = $1
                ORDER BY downloaded_at DESC
                LIMIT $2
                """,
                user_id,
                limit,
            )
        return [self._row_to_download(row) for row in rows]

    async def get_pending_followups(
        self,
        days: int = 3,
    ) -> list[Download]:
        """Get downloads that need follow-up (older than N days, not followed up)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM downloads
                WHERE followed_up = 0
                    AND downloaded_at < NOW() - INTERVAL '1 day' * $1
                ORDER BY downloaded_at ASC
                """,
                days,
            )
        return [self._row_to_download(row) for row in rows]

    async def mark_followup_sent(
        self,
        download_id: int,
    ) -> bool:
        """Mark follow-up as sent."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE downloads SET followed_up = 1 WHERE id = $1",
                download_id,
            )
        return result == "UPDATE 1"

    async def mark_followup_answered(
        self,
        download_id: int,
        rating: float | None = None,
    ) -> bool:
        """Mark follow-up as answered with optional rating."""
        async with self.pool.acquire() as conn:
            if rating is not None:
                result = await conn.execute(
                    "UPDATE downloads SET followed_up = 2, rating = $1 WHERE id = $2",
                    rating,
                    download_id,
                )
            else:
                result = await conn.execute(
                    "UPDATE downloads SET followed_up = 2 WHERE id = $1",
                    download_id,
                )
        return result == "UPDATE 1"

    async def get_download(
        self,
        download_id: int,
    ) -> Download | None:
        """Get a single download by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM downloads WHERE id = $1",
                download_id,
            )
        return self._row_to_download(row) if row else None

    def _row_to_download(self, row: Any) -> Download:
        """Convert database row to Download model."""
        return Download(
            id=row["id"],
            user_id=row["user_id"],
            tmdb_id=row["tmdb_id"],
            media_type=row["media_type"],
            title=row["title"],
            season=row["season"],
            episode=row["episode"],
            quality=row["quality"],
            source=row["source"],
            magnet_hash=row["magnet_hash"],
            downloaded_at=row["downloaded_at"],
            followed_up=row["followed_up"],
            rating=row["rating"],
        )

    # -------------------------------------------------------------------------
    # Pending Pushes CRUD Implementation
    # -------------------------------------------------------------------------

    async def create_pending_push(
        self,
        user_id: int,
        push_type: str,
        priority: int,
        content: dict[str, Any],
    ) -> PendingPush:
        """Create a pending push notification."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pending_pushes (user_id, push_type, priority, content)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                user_id,
                push_type,
                priority,
                json.dumps(content),
            )

        logger.info("pending_push_created", user_id=user_id, push_type=push_type, priority=priority)
        return self._row_to_pending_push(row)

    async def get_pending_pushes(
        self,
        user_id: int | None = None,
        push_type: str | None = None,
    ) -> list[PendingPush]:
        """Get pending pushes, optionally filtered by user or type."""
        query = "SELECT * FROM pending_pushes WHERE sent_at IS NULL"
        params: list[Any] = []
        param_idx = 1

        if user_id is not None:
            query += f" AND user_id = ${param_idx}"
            params.append(user_id)
            param_idx += 1
        if push_type is not None:
            query += f" AND push_type = ${param_idx}"
            params.append(push_type)

        query += " ORDER BY priority ASC, created_at ASC"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [self._row_to_pending_push(row) for row in rows]

    async def get_highest_priority_push(
        self,
        user_id: int,
    ) -> PendingPush | None:
        """Get the highest priority unsent push for a user."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM pending_pushes
                WHERE user_id = $1 AND sent_at IS NULL
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """,
                user_id,
            )
        return self._row_to_pending_push(row) if row else None

    async def mark_push_sent(
        self,
        push_id: int,
    ) -> bool:
        """Mark a push as sent."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE pending_pushes SET sent_at = NOW() WHERE id = $1",
                push_id,
            )
        return result == "UPDATE 1"

    async def get_last_push_time(
        self,
        user_id: int,
    ) -> datetime | None:
        """Get the timestamp of last sent push for a user (for throttling)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT MAX(sent_at) as last_sent FROM pending_pushes
                WHERE user_id = $1 AND sent_at IS NOT NULL
                """,
                user_id,
            )
        if row and row["last_sent"]:
            return row["last_sent"]
        return None

    async def delete_old_pushes(
        self,
        days: int = 7,
    ) -> int:
        """Delete sent pushes older than N days. Returns count deleted."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM pending_pushes WHERE sent_at IS NOT NULL AND sent_at < NOW() - INTERVAL '1 day' * $1",
                days,
            )
        # Parse result like "DELETE 5"
        if result and result.startswith("DELETE"):
            parts = result.split()
            if len(parts) == 2:
                return int(parts[1])
        return 0

    def _row_to_pending_push(self, row: Any) -> PendingPush:
        """Convert database row to PendingPush model."""
        content = row["content"]
        if isinstance(content, str):
            content = json.loads(content)

        return PendingPush(
            id=row["id"],
            user_id=row["user_id"],
            push_type=row["push_type"],
            priority=row["priority"],
            content=content,
            created_at=row["created_at"],
            sent_at=row["sent_at"],
        )

    # -------------------------------------------------------------------------
    # Synced Torrents CRUD
    # -------------------------------------------------------------------------

    async def track_torrent(
        self,
        user_id: int,
        torrent_hash: str,
        torrent_name: str,
        seedbox_path: str | None = None,
        size_bytes: int | None = None,
    ) -> SyncedTorrent:
        """Track a torrent sent to seedbox."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO synced_torrents (user_id, torrent_hash, torrent_name, seedbox_path, size_bytes)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT(user_id, torrent_hash) DO UPDATE SET
                    torrent_name = EXCLUDED.torrent_name,
                    seedbox_path = EXCLUDED.seedbox_path,
                    size_bytes = EXCLUDED.size_bytes,
                    status = 'downloading'
                RETURNING *
                """,
                user_id,
                torrent_hash,
                torrent_name,
                seedbox_path,
                size_bytes,
            )
        return self._row_to_synced_torrent(row)

    async def update_torrent_status(
        self,
        torrent_hash: str,
        status: str,
        synced_at: datetime | None = None,
        local_path: str | None = None,
    ) -> bool:
        """Update torrent sync status."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE synced_torrents SET status = $1, synced_at = $2, local_path = $3
                WHERE torrent_hash = $4
                """,
                status,
                synced_at,
                local_path,
                torrent_hash,
            )
        return result == "UPDATE 1"

    async def get_downloading_torrents(self) -> list[SyncedTorrent]:
        """Get all torrents with 'downloading' status."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM synced_torrents WHERE status = 'downloading' ORDER BY created_at",
            )
        return [self._row_to_synced_torrent(row) for row in rows]

    async def get_torrents_by_status(self, status: str) -> list[SyncedTorrent]:
        """Get all torrents with a given status."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM synced_torrents WHERE status = $1 ORDER BY created_at",
                status,
            )
        return [self._row_to_synced_torrent(row) for row in rows]

    async def get_pending_sync_torrents(
        self,
        user_id: int | None = None,
    ) -> list[SyncedTorrent]:
        """Get torrents that are seeding (ready for sync)."""
        async with self.pool.acquire() as conn:
            if user_id:
                rows = await conn.fetch(
                    "SELECT * FROM synced_torrents WHERE user_id = $1 AND status = 'seeding' ORDER BY created_at",
                    user_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM synced_torrents WHERE status = 'seeding' ORDER BY created_at",
                )
        return [self._row_to_synced_torrent(row) for row in rows]

    async def get_user_by_torrent_hash(
        self,
        torrent_hash: str,
    ) -> User | None:
        """Get user who owns a torrent (for notifications)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT u.* FROM users u
                JOIN synced_torrents st ON u.id = st.user_id
                WHERE st.torrent_hash = $1
                """,
                torrent_hash,
            )
        return self._row_to_user(row) if row else None

    async def get_user_by_torrent_name(
        self,
        name: str,
    ) -> User | None:
        """Get user who owns a torrent by name substring match.

        Normalizes separators (dots, underscores, spaces) to wildcards
        so 'The Rehearsal S01' matches 'The.Rehearsal.S01E02...'.
        """
        import re

        # Build LIKE pattern: split on separators, join with %
        words = re.split(r"[\s._]+", name.strip())
        pattern = "%" + "%".join(w for w in words if w) + "%"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT u.* FROM users u
                JOIN synced_torrents st ON u.id = st.user_id
                WHERE st.torrent_name ILIKE $1
                AND st.status IN ('seeding', 'downloading')
                ORDER BY st.created_at DESC
                LIMIT 1
                """,
                pattern,
            )
        return self._row_to_user(row) if row else None

    async def mark_torrent_deleted(
        self,
        torrent_hash: str,
    ) -> bool:
        """Mark a torrent as deleted from seedbox."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE synced_torrents SET status = 'deleted', deleted_from_seedbox_at = NOW() WHERE torrent_hash = $1",
                torrent_hash,
            )
        return result == "UPDATE 1"

    def _row_to_synced_torrent(self, row: Any) -> SyncedTorrent:
        """Convert database row to SyncedTorrent model."""
        return SyncedTorrent(
            id=row["id"],
            user_id=row["user_id"],
            torrent_hash=row["torrent_hash"],
            torrent_name=row["torrent_name"],
            seedbox_path=row["seedbox_path"],
            local_path=row["local_path"],
            size_bytes=row["size_bytes"],
            status=row["status"] or "downloading",
            synced_at=row["synced_at"],
            deleted_from_seedbox_at=row["deleted_from_seedbox_at"],
            created_at=row["created_at"],
        )

    # -------------------------------------------------------------------------
    # Library Index
    # -------------------------------------------------------------------------

    async def save_library_index(
        self,
        category: str,
        items_json: str,
    ) -> None:
        """Save (upsert) library index for a category."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO library_index (category, items_json, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (category) DO UPDATE SET items_json = $2, updated_at = NOW()
                """,
                category,
                items_json,
            )

    async def get_library_index(
        self,
        category: str,
    ) -> str | None:
        """Get library index JSON for a category."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT items_json FROM library_index WHERE category = $1",
                category,
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
