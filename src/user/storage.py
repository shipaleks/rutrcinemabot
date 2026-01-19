"""User profile storage with SQLite and encrypted credentials.

This module provides:
- SQLite-based persistent storage for user data
- Encrypted storage for OAuth tokens and sensitive credentials
- CRUD operations for user profiles, preferences, and watch history
- Database migrations for schema evolution

Usage:
    async with UserStorage("data/users.db") as storage:
        # Create user
        user = await storage.create_user(telegram_id=123456)

        # Store encrypted credential
        await storage.store_credential(
            user_id=user.id,
            credential_type=CredentialType.TRAKT_TOKEN,
            value="oauth_token_here"
        )

        # Update preferences
        await storage.update_preferences(
            user_id=user.id,
            video_quality="1080p",
            audio_language="ru"
        )
"""

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import aiosqlite
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
    watched_at: datetime
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
# Database Migrations
# =============================================================================


# Migration definitions: (version, name, sql)
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "create_users_table",
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
    ),
    (
        2,
        "create_credentials_table",
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
    ),
    (
        3,
        "create_preferences_table",
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
    ),
    (
        4,
        "create_watched_table",
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
            watched_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_watched_user_id ON watched(user_id);
        CREATE INDEX IF NOT EXISTS idx_watched_tmdb_id ON watched(tmdb_id);
        CREATE INDEX IF NOT EXISTS idx_watched_kinopoisk_id ON watched(kinopoisk_id);
        """,
    ),
    (
        5,
        "create_migrations_table",
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """,
    ),
]


class MigrationManager:
    """Database migration manager."""

    def __init__(self, db: aiosqlite.Connection):
        """Initialize migration manager.

        Args:
            db: Active database connection
        """
        self._db = db

    async def get_current_version(self) -> int:
        """Get current migration version.

        Returns:
            Current version number (0 if no migrations applied)
        """
        # Check if _migrations table exists
        cursor = await self._db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='_migrations'
            """
        )
        if await cursor.fetchone() is None:
            return 0

        cursor = await self._db.execute("SELECT MAX(version) FROM _migrations")
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0

    async def apply_migrations(self) -> list[str]:
        """Apply pending migrations.

        Returns:
            List of applied migration names
        """
        current_version = await self.get_current_version()
        applied: list[str] = []

        # Sort migrations by version
        sorted_migrations = sorted(MIGRATIONS, key=lambda m: m[0])

        for version, name, sql in sorted_migrations:
            if version <= current_version:
                continue

            logger.info(
                "applying_migration",
                version=version,
                name=name,
            )

            # Execute migration SQL
            await self._db.executescript(sql)

            # Record migration (only if _migrations table exists)
            if version >= 5 or current_version >= 5:
                await self._db.execute(
                    """
                    INSERT INTO _migrations (version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (version, name, datetime.now(UTC).isoformat()),
                )

            await self._db.commit()
            applied.append(name)

            logger.info(
                "migration_applied",
                version=version,
                name=name,
            )

        return applied


# =============================================================================
# User Storage
# =============================================================================


class UserStorage:
    """SQLite-based user profile storage with encryption support.

    Usage:
        async with UserStorage("data/users.db", encryption_key) as storage:
            user = await storage.create_user(telegram_id=123456)
    """

    def __init__(
        self,
        db_path: str | Path,
        encryption_key: str | bytes | None = None,
    ):
        """Initialize user storage.

        Args:
            db_path: Path to SQLite database file
            encryption_key: Optional Fernet key for encrypting credentials
        """
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._encryption: EncryptionHelper | None = None

        if encryption_key:
            self._encryption = EncryptionHelper(encryption_key)

    async def __aenter__(self) -> "UserStorage":
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

    async def connect(self) -> None:
        """Open database connection and initialize schema."""
        # Ensure parent directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # Enable foreign keys
        await self._db.execute("PRAGMA foreign_keys = ON")

        # Apply migrations
        migration_manager = MigrationManager(self._db)
        applied = await migration_manager.apply_migrations()

        if applied:
            logger.info(
                "migrations_applied",
                count=len(applied),
                migrations=applied,
            )

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        """Get active database connection.

        Raises:
            RuntimeError: If not connected
        """
        if self._db is None:
            raise RuntimeError("Database not connected. Use 'async with' or call connect()")
        return self._db

    # -------------------------------------------------------------------------
    # User CRUD
    # -------------------------------------------------------------------------

    async def create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = "ru",
    ) -> User:
        """Create a new user.

        Args:
            telegram_id: Telegram user ID
            username: Telegram username
            first_name: User's first name
            last_name: User's last name
            language_code: User's language code

        Returns:
            Created User object

        Raises:
            aiosqlite.IntegrityError: If user with telegram_id already exists
        """
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

        # Also create default preferences
        await self.db.execute(
            """
            INSERT INTO preferences (user_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (user_id, now, now),
        )
        await self.db.commit()

        logger.info(
            "user_created",
            user_id=user_id,
            telegram_id=telegram_id,
        )

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
        """Get user by internal ID.

        Args:
            user_id: Internal user ID

        Returns:
            User object or None if not found
        """
        cursor = await self.db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_user(row)

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        """Get user by Telegram ID.

        Args:
            telegram_id: Telegram user ID

        Returns:
            User object or None if not found
        """
        cursor = await self.db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_user(row)

    async def get_or_create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = "ru",
    ) -> tuple[User, bool]:
        """Get existing user or create new one.

        Args:
            telegram_id: Telegram user ID
            username: Telegram username
            first_name: User's first name
            last_name: User's last name
            language_code: User's language code

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

    async def update_user(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_active: bool | None = None,
    ) -> User | None:
        """Update user profile.

        Args:
            user_id: Internal user ID
            username: New username (optional)
            first_name: New first name (optional)
            last_name: New last name (optional)
            language_code: New language code (optional)
            is_active: New active status (optional)

        Returns:
            Updated User object or None if not found
        """
        # Build update query dynamically
        updates: list[str] = []
        params: list[str | int | None] = []

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
        """Delete user and all related data.

        Args:
            user_id: Internal user ID

        Returns:
            True if user was deleted, False if not found
        """
        cursor = await self.db.execute(
            "DELETE FROM users WHERE id = ?",
            (user_id,),
        )
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
        """List users with pagination.

        Args:
            active_only: Only return active users
            limit: Maximum number of users to return
            offset: Number of users to skip

        Returns:
            List of User objects
        """
        query = "SELECT * FROM users"
        params: list[int] = []

        if active_only:
            query += " WHERE is_active = 1"

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()

        return [self._row_to_user(row) for row in rows]

    def _row_to_user(self, row: aiosqlite.Row) -> User:
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
    # Credentials CRUD (Encrypted)
    # -------------------------------------------------------------------------

    async def store_credential(
        self,
        user_id: int,
        credential_type: CredentialType,
        value: str,
        expires_at: datetime | None = None,
    ) -> Credential:
        """Store an encrypted credential.

        Args:
            user_id: Internal user ID
            credential_type: Type of credential
            value: Plain text credential value
            expires_at: Optional expiration datetime

        Returns:
            Created Credential object (with encrypted value)

        Raises:
            RuntimeError: If encryption is not configured
        """
        if self._encryption is None:
            raise RuntimeError("Encryption key not configured")

        encrypted_value = self._encryption.encrypt(value)
        now = datetime.now(UTC).isoformat()
        expires_str = expires_at.isoformat() if expires_at else None

        # Use INSERT OR REPLACE to handle updates
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
            # Get the existing credential ID after update
            cursor = await self.db.execute(
                """
                SELECT id FROM credentials
                WHERE user_id = ? AND credential_type = ?
                """,
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
        """Get decrypted credential value.

        Args:
            user_id: Internal user ID
            credential_type: Type of credential

        Returns:
            Decrypted credential value or None if not found

        Raises:
            RuntimeError: If encryption is not configured
            InvalidToken: If decryption fails
        """
        if self._encryption is None:
            raise RuntimeError("Encryption key not configured")

        cursor = await self.db.execute(
            """
            SELECT encrypted_value, expires_at FROM credentials
            WHERE user_id = ? AND credential_type = ?
            """,
            (user_id, credential_type.value),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        # Check expiration
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
        """Delete a credential.

        Args:
            user_id: Internal user ID
            credential_type: Type of credential

        Returns:
            True if credential was deleted, False if not found
        """
        cursor = await self.db.execute(
            """
            DELETE FROM credentials
            WHERE user_id = ? AND credential_type = ?
            """,
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
        """List credential types for a user (not values).

        Args:
            user_id: Internal user ID

        Returns:
            List of credential types the user has stored
        """
        cursor = await self.db.execute(
            "SELECT credential_type FROM credentials WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()

        return [CredentialType(row["credential_type"]) for row in rows]

    # -------------------------------------------------------------------------
    # Preferences CRUD
    # -------------------------------------------------------------------------

    async def get_preferences(self, user_id: int) -> Preference | None:
        """Get user preferences.

        Args:
            user_id: Internal user ID

        Returns:
            Preference object or None if not found
        """
        cursor = await self.db.execute(
            "SELECT * FROM preferences WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return self._row_to_preference(row)

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
    ) -> Preference | None:
        """Update user preferences.

        Args:
            user_id: Internal user ID
            video_quality: Preferred video quality (720p, 1080p, 4K)
            audio_language: Preferred audio language
            subtitle_language: Preferred subtitle language
            preferred_genres: List of preferred genres
            excluded_genres: List of excluded genres
            auto_download: Auto-download enabled
            notification_enabled: Notifications enabled

        Returns:
            Updated Preference object or None if user not found
        """
        # Check if preferences exist
        existing = await self.get_preferences(user_id)
        if existing is None:
            return None

        # Build update query dynamically
        updates: list[str] = []
        params: list[str | int] = []

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

    def _row_to_preference(self, row: aiosqlite.Row) -> Preference:
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
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # -------------------------------------------------------------------------
    # Watched Items CRUD
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
        watched_at: datetime | None = None,
    ) -> WatchedItem:
        """Add item to watch history.

        Args:
            user_id: Internal user ID
            media_type: "movie" or "tv"
            title: Content title
            tmdb_id: TMDB ID
            kinopoisk_id: Kinopoisk ID
            year: Release year
            season: Season number (for TV)
            episode: Episode number (for TV)
            rating: User's rating (1-10)
            watched_at: When it was watched (defaults to now)

        Returns:
            Created WatchedItem object
        """
        now = datetime.now(UTC)
        watched_at = watched_at or now

        cursor = await self.db.execute(
            """
            INSERT INTO watched
                (user_id, media_type, tmdb_id, kinopoisk_id, title, year,
                 season, episode, rating, watched_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                watched_at.isoformat(),
                now.isoformat(),
            ),
        )
        await self.db.commit()

        item_id = cursor.lastrowid
        if item_id is None:
            raise RuntimeError("Failed to create watched item")

        logger.info(
            "watched_added",
            user_id=user_id,
            media_type=media_type,
            title=title,
        )

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
        """Get user's watch history.

        Args:
            user_id: Internal user ID
            media_type: Filter by type ("movie" or "tv")
            limit: Maximum items to return
            offset: Number of items to skip

        Returns:
            List of WatchedItem objects, newest first
        """
        query = "SELECT * FROM watched WHERE user_id = ?"
        params: list[int | str] = [user_id]

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
        """Check if user has watched content.

        Args:
            user_id: Internal user ID
            tmdb_id: TMDB ID to check
            kinopoisk_id: Kinopoisk ID to check

        Returns:
            True if content is in watch history
        """
        if tmdb_id is None and kinopoisk_id is None:
            return False

        conditions = ["user_id = ?"]
        params: list[int] = [user_id]

        if tmdb_id:
            conditions.append("tmdb_id = ?")
            params.append(tmdb_id)
        if kinopoisk_id:
            conditions.append("kinopoisk_id = ?")
            params.append(kinopoisk_id)

        # Use OR for ID matching (either ID matches)
        if tmdb_id and kinopoisk_id:
            query = """
                SELECT 1 FROM watched
                WHERE user_id = ? AND (tmdb_id = ? OR kinopoisk_id = ?)
                LIMIT 1
            """
            params = [user_id, tmdb_id, kinopoisk_id]
        else:
            query = f"SELECT 1 FROM watched WHERE {' AND '.join(conditions)} LIMIT 1"

        cursor = await self.db.execute(query, params)
        return await cursor.fetchone() is not None

    async def delete_watched(self, item_id: int) -> bool:
        """Delete item from watch history.

        Args:
            item_id: Watched item ID

        Returns:
            True if item was deleted, False if not found
        """
        cursor = await self.db.execute(
            "DELETE FROM watched WHERE id = ?",
            (item_id,),
        )
        await self.db.commit()

        return cursor.rowcount > 0 if cursor.rowcount else False

    def _row_to_watched(self, row: aiosqlite.Row) -> WatchedItem:
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
            watched_at=datetime.fromisoformat(row["watched_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# =============================================================================
# Convenience Functions
# =============================================================================


@asynccontextmanager
async def get_user_storage(
    db_path: str | Path = "data/users.db",
    encryption_key: str | bytes | None = None,
) -> AsyncIterator[UserStorage]:
    """Get user storage instance as context manager.

    Args:
        db_path: Path to SQLite database file
        encryption_key: Optional Fernet key for encrypting credentials

    Yields:
        UserStorage instance
    """
    storage = UserStorage(db_path, encryption_key)
    async with storage:
        yield storage
