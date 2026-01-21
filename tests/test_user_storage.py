"""Tests for user storage module.

Tests cover:
- User CRUD operations
- Credential encryption/decryption
- Preferences management
- Watch history tracking
- Database migrations
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src.user.storage import (
    CredentialType,
    EncryptionHelper,
    Preference,
    User,
    UserStorage,
    WatchedItem,
    get_user_storage,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def encryption_key() -> str:
    """Generate a valid Fernet key for testing."""
    return Fernet.generate_key().decode()


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Create a temporary database path."""
    return tmp_path / "test_users.db"


@pytest.fixture
async def storage(temp_db_path: Path, encryption_key: str) -> UserStorage:
    """Create a user storage instance for testing."""
    storage = UserStorage(temp_db_path, encryption_key)
    await storage.connect()
    yield storage
    await storage.close()


@pytest.fixture
async def storage_no_encryption(temp_db_path: Path) -> UserStorage:
    """Create a user storage instance without encryption."""
    storage = UserStorage(temp_db_path)
    await storage.connect()
    yield storage
    await storage.close()


@pytest.fixture
async def sample_user(storage: UserStorage) -> User:
    """Create a sample user for testing."""
    return await storage.create_user(
        telegram_id=123456789,
        username="testuser",
        first_name="Test",
        last_name="User",
        language_code="en",
    )


# =============================================================================
# User Model Tests
# =============================================================================


class TestUserModel:
    """Tests for User Pydantic model."""

    def test_user_display_name_full(self):
        """Test display name with first and last name."""
        user = User(
            id=1,
            telegram_id=123,
            username="test",
            first_name="John",
            last_name="Doe",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert user.display_name == "John Doe"

    def test_user_display_name_first_only(self):
        """Test display name with first name only."""
        user = User(
            id=1,
            telegram_id=123,
            username="test",
            first_name="John",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert user.display_name == "John"

    def test_user_display_name_username(self):
        """Test display name with username only."""
        user = User(
            id=1,
            telegram_id=123,
            username="test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert user.display_name == "@test"

    def test_user_display_name_telegram_id(self):
        """Test display name falls back to telegram_id."""
        user = User(
            id=1,
            telegram_id=123,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert user.display_name == "User 123"


# =============================================================================
# Encryption Tests
# =============================================================================


class TestEncryptionHelper:
    """Tests for encryption helper."""

    def test_encrypt_decrypt_roundtrip(self, encryption_key: str):
        """Test encrypt and decrypt work together."""
        helper = EncryptionHelper(encryption_key)
        original = "secret_token_12345"

        encrypted = helper.encrypt(original)
        decrypted = helper.decrypt(encrypted)

        assert decrypted == original
        assert encrypted != original

    def test_encrypt_produces_different_output(self, encryption_key: str):
        """Test that encryption produces different ciphertext each time."""
        helper = EncryptionHelper(encryption_key)
        original = "secret_token"

        encrypted1 = helper.encrypt(original)
        encrypted2 = helper.encrypt(original)

        # Fernet includes timestamp, so encryptions differ
        assert encrypted1 != encrypted2

    def test_accepts_bytes_key(self):
        """Test encryption helper accepts bytes key."""
        key = Fernet.generate_key()
        helper = EncryptionHelper(key)

        encrypted = helper.encrypt("test")
        decrypted = helper.decrypt(encrypted)

        assert decrypted == "test"


# =============================================================================
# Migration Tests
# =============================================================================


class TestMigrations:
    """Tests for database migrations."""

    @pytest.mark.asyncio
    async def test_migrations_applied_on_connect(self, temp_db_path: Path):
        """Test all migrations are applied when connecting."""
        storage = UserStorage(temp_db_path)
        await storage.connect()

        # Check tables exist
        cursor = await storage.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]

        assert "users" in tables
        assert "credentials" in tables
        assert "preferences" in tables
        assert "watched" in tables
        assert "_migrations" in tables
        # New memory system tables
        assert "core_memory_blocks" in tables
        assert "conversation_sessions" in tables
        assert "memory_notes" in tables

        await storage.close()


# =============================================================================
# User CRUD Tests
# =============================================================================


class TestUserCRUD:
    """Tests for user CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_user(self, storage: UserStorage):
        """Test creating a new user."""
        user = await storage.create_user(
            telegram_id=111222333,
            username="newuser",
            first_name="New",
            last_name="User",
            language_code="ru",
        )

        assert user.id is not None
        assert user.telegram_id == 111222333
        assert user.username == "newuser"
        assert user.first_name == "New"
        assert user.last_name == "User"
        assert user.language_code == "ru"
        assert user.is_active is True

    @pytest.mark.asyncio
    async def test_create_user_minimal(self, storage: UserStorage):
        """Test creating user with minimal info."""
        user = await storage.create_user(telegram_id=999888777)

        assert user.telegram_id == 999888777
        assert user.username is None
        assert user.language_code == "ru"  # Default

    @pytest.mark.asyncio
    async def test_create_user_also_creates_preferences(self, storage: UserStorage):
        """Test that creating user also creates default preferences."""
        user = await storage.create_user(telegram_id=444555666)

        prefs = await storage.get_preferences(user.id)
        assert prefs is not None
        assert prefs.user_id == user.id
        assert prefs.video_quality == "1080p"

    @pytest.mark.asyncio
    async def test_get_user_by_id(self, storage: UserStorage, sample_user: User):
        """Test getting user by internal ID."""
        user = await storage.get_user(sample_user.id)

        assert user is not None
        assert user.id == sample_user.id
        assert user.telegram_id == sample_user.telegram_id

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id(self, storage: UserStorage, sample_user: User):
        """Test getting user by Telegram ID."""
        user = await storage.get_user_by_telegram_id(sample_user.telegram_id)

        assert user is not None
        assert user.telegram_id == sample_user.telegram_id

    @pytest.mark.asyncio
    async def test_get_user_not_found(self, storage: UserStorage):
        """Test getting non-existent user returns None."""
        user = await storage.get_user(99999)
        assert user is None

    @pytest.mark.asyncio
    async def test_get_or_create_user_creates(self, storage: UserStorage):
        """Test get_or_create creates new user."""
        user, created = await storage.get_or_create_user(
            telegram_id=777888999,
            username="createduser",
        )

        assert created is True
        assert user.telegram_id == 777888999

    @pytest.mark.asyncio
    async def test_get_or_create_user_gets_existing(self, storage: UserStorage, sample_user: User):
        """Test get_or_create returns existing user."""
        user, created = await storage.get_or_create_user(
            telegram_id=sample_user.telegram_id,
        )

        assert created is False
        assert user.id == sample_user.id

    @pytest.mark.asyncio
    async def test_get_or_create_updates_info(self, storage: UserStorage, sample_user: User):
        """Test get_or_create updates user info if changed."""
        user, created = await storage.get_or_create_user(
            telegram_id=sample_user.telegram_id,
            username="newusername",
            first_name="NewFirst",
        )

        assert created is False
        assert user.username == "newusername"
        assert user.first_name == "NewFirst"

    @pytest.mark.asyncio
    async def test_update_user(self, storage: UserStorage, sample_user: User):
        """Test updating user profile."""
        updated = await storage.update_user(
            sample_user.id,
            username="updatedname",
            language_code="de",
        )

        assert updated is not None
        assert updated.username == "updatedname"
        assert updated.language_code == "de"

    @pytest.mark.asyncio
    async def test_update_user_not_found(self, storage: UserStorage):
        """Test updating non-existent user returns None."""
        updated = await storage.update_user(99999, username="test")
        assert updated is None

    @pytest.mark.asyncio
    async def test_delete_user(self, storage: UserStorage, sample_user: User):
        """Test deleting a user."""
        deleted = await storage.delete_user(sample_user.id)
        assert deleted is True

        user = await storage.get_user(sample_user.id)
        assert user is None

    @pytest.mark.asyncio
    async def test_delete_user_not_found(self, storage: UserStorage):
        """Test deleting non-existent user returns False."""
        deleted = await storage.delete_user(99999)
        assert deleted is False

    @pytest.mark.asyncio
    async def test_list_users(self, storage: UserStorage):
        """Test listing users."""
        # Create multiple users
        await storage.create_user(telegram_id=1001)
        await storage.create_user(telegram_id=1002)
        await storage.create_user(telegram_id=1003)

        users = await storage.list_users(limit=10)
        assert len(users) >= 3

    @pytest.mark.asyncio
    async def test_list_users_pagination(self, storage: UserStorage):
        """Test listing users with pagination."""
        # Create users
        for i in range(5):
            await storage.create_user(telegram_id=2000 + i)

        page1 = await storage.list_users(limit=2, offset=0)
        page2 = await storage.list_users(limit=2, offset=2)

        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id


# =============================================================================
# Credentials Tests
# =============================================================================


class TestCredentials:
    """Tests for encrypted credential storage."""

    @pytest.mark.asyncio
    async def test_store_and_retrieve_credential(self, storage: UserStorage, sample_user: User):
        """Test storing and retrieving encrypted credential."""
        await storage.store_credential(
            user_id=sample_user.id,
            credential_type=CredentialType.TRAKT_TOKEN,
            value="secret_oauth_token",
        )

        value = await storage.get_credential(sample_user.id, CredentialType.TRAKT_TOKEN)
        assert value == "secret_oauth_token"

    @pytest.mark.asyncio
    async def test_credential_encrypted_in_db(self, storage: UserStorage, sample_user: User):
        """Test that credentials are actually encrypted in database."""
        original = "my_secret_token"
        await storage.store_credential(
            user_id=sample_user.id,
            credential_type=CredentialType.TRAKT_TOKEN,
            value=original,
        )

        # Read raw value from database
        cursor = await storage.db.execute(
            "SELECT encrypted_value FROM credentials WHERE user_id = ?",
            (sample_user.id,),
        )
        row = await cursor.fetchone()

        # Should not contain plain text
        assert original not in row["encrypted_value"]

    @pytest.mark.asyncio
    async def test_store_credential_upsert(self, storage: UserStorage, sample_user: User):
        """Test that storing credential updates existing."""
        await storage.store_credential(
            user_id=sample_user.id,
            credential_type=CredentialType.TRAKT_TOKEN,
            value="first_value",
        )
        await storage.store_credential(
            user_id=sample_user.id,
            credential_type=CredentialType.TRAKT_TOKEN,
            value="second_value",
        )

        value = await storage.get_credential(sample_user.id, CredentialType.TRAKT_TOKEN)
        assert value == "second_value"

    @pytest.mark.asyncio
    async def test_get_credential_not_found(self, storage: UserStorage, sample_user: User):
        """Test getting non-existent credential returns None."""
        value = await storage.get_credential(sample_user.id, CredentialType.SEEDBOX_PASSWORD)
        assert value is None

    @pytest.mark.asyncio
    async def test_credential_expiration(self, storage: UserStorage, sample_user: User):
        """Test expired credentials return None."""
        expired = datetime.now(UTC) - timedelta(hours=1)

        await storage.store_credential(
            user_id=sample_user.id,
            credential_type=CredentialType.TRAKT_TOKEN,
            value="expired_token",
            expires_at=expired,
        )

        value = await storage.get_credential(sample_user.id, CredentialType.TRAKT_TOKEN)
        assert value is None

    @pytest.mark.asyncio
    async def test_delete_credential(self, storage: UserStorage, sample_user: User):
        """Test deleting a credential."""
        await storage.store_credential(
            user_id=sample_user.id,
            credential_type=CredentialType.TRAKT_TOKEN,
            value="to_delete",
        )

        deleted = await storage.delete_credential(sample_user.id, CredentialType.TRAKT_TOKEN)
        assert deleted is True

        value = await storage.get_credential(sample_user.id, CredentialType.TRAKT_TOKEN)
        assert value is None

    @pytest.mark.asyncio
    async def test_list_credentials(self, storage: UserStorage, sample_user: User):
        """Test listing credential types for user."""
        await storage.store_credential(sample_user.id, CredentialType.TRAKT_TOKEN, "token")
        await storage.store_credential(sample_user.id, CredentialType.SEEDBOX_PASSWORD, "pass")

        types = await storage.list_credentials(sample_user.id)
        assert CredentialType.TRAKT_TOKEN in types
        assert CredentialType.SEEDBOX_PASSWORD in types

    @pytest.mark.asyncio
    async def test_store_credential_requires_encryption(self, storage_no_encryption: UserStorage):
        """Test storing credential fails without encryption key."""
        user = await storage_no_encryption.create_user(telegram_id=12345)

        with pytest.raises(RuntimeError, match="Encryption key not configured"):
            await storage_no_encryption.store_credential(
                user.id, CredentialType.TRAKT_TOKEN, "value"
            )


# =============================================================================
# Preferences Tests
# =============================================================================


class TestPreferences:
    """Tests for user preferences."""

    @pytest.mark.asyncio
    async def test_get_default_preferences(self, storage: UserStorage, sample_user: User):
        """Test getting default preferences."""
        prefs = await storage.get_preferences(sample_user.id)

        assert prefs is not None
        assert prefs.video_quality == "1080p"
        assert prefs.audio_language == "ru"
        assert prefs.auto_download is False

    @pytest.mark.asyncio
    async def test_update_preferences(self, storage: UserStorage, sample_user: User):
        """Test updating preferences."""
        prefs = await storage.update_preferences(
            user_id=sample_user.id,
            video_quality="4K",
            audio_language="en",
            preferred_genres=["sci-fi", "action"],
        )

        assert prefs is not None
        assert prefs.video_quality == "4K"
        assert prefs.audio_language == "en"
        assert prefs.preferred_genres == ["sci-fi", "action"]

    @pytest.mark.asyncio
    async def test_update_preferences_partial(self, storage: UserStorage, sample_user: User):
        """Test partial preference update."""
        # First update
        await storage.update_preferences(
            user_id=sample_user.id,
            video_quality="720p",
        )

        # Second update (should keep video_quality)
        prefs = await storage.update_preferences(
            user_id=sample_user.id,
            auto_download=True,
        )

        assert prefs.video_quality == "720p"
        assert prefs.auto_download is True

    @pytest.mark.asyncio
    async def test_preferences_genres_storage(self, storage: UserStorage, sample_user: User):
        """Test genres are stored as JSON arrays."""
        genres = ["drama", "comedy", "thriller"]
        excluded = ["horror", "romance"]

        prefs = await storage.update_preferences(
            user_id=sample_user.id,
            preferred_genres=genres,
            excluded_genres=excluded,
        )

        assert prefs.preferred_genres == genres
        assert prefs.excluded_genres == excluded


# =============================================================================
# Watched Items Tests
# =============================================================================


class TestWatchedItems:
    """Tests for watch history."""

    @pytest.mark.asyncio
    async def test_add_watched_movie(self, storage: UserStorage, sample_user: User):
        """Test adding movie to watch history."""
        item = await storage.add_watched(
            user_id=sample_user.id,
            media_type="movie",
            title="Inception",
            tmdb_id=27205,
            year=2010,
            rating=9.0,
        )

        assert item.id is not None
        assert item.media_type == "movie"
        assert item.title == "Inception"
        assert item.tmdb_id == 27205
        assert item.rating == 9.0

    @pytest.mark.asyncio
    async def test_add_watched_tv_episode(self, storage: UserStorage, sample_user: User):
        """Test adding TV episode to watch history."""
        item = await storage.add_watched(
            user_id=sample_user.id,
            media_type="tv",
            title="Breaking Bad",
            tmdb_id=1396,
            year=2008,
            season=1,
            episode=1,
        )

        assert item.media_type == "tv"
        assert item.season == 1
        assert item.episode == 1

    @pytest.mark.asyncio
    async def test_get_watched_history(self, storage: UserStorage, sample_user: User):
        """Test getting watch history."""
        # Add multiple items
        await storage.add_watched(sample_user.id, "movie", "Movie 1", tmdb_id=1)
        await storage.add_watched(sample_user.id, "movie", "Movie 2", tmdb_id=2)
        await storage.add_watched(sample_user.id, "tv", "TV Show", tmdb_id=3)

        all_items = await storage.get_watched(sample_user.id)
        assert len(all_items) == 3

        movies = await storage.get_watched(sample_user.id, media_type="movie")
        assert len(movies) == 2

    @pytest.mark.asyncio
    async def test_is_watched_by_tmdb_id(self, storage: UserStorage, sample_user: User):
        """Test checking if content is watched by TMDB ID."""
        await storage.add_watched(sample_user.id, "movie", "Test Movie", tmdb_id=12345)

        assert await storage.is_watched(sample_user.id, tmdb_id=12345) is True
        assert await storage.is_watched(sample_user.id, tmdb_id=99999) is False

    @pytest.mark.asyncio
    async def test_is_watched_by_kinopoisk_id(self, storage: UserStorage, sample_user: User):
        """Test checking if content is watched by Kinopoisk ID."""
        await storage.add_watched(sample_user.id, "movie", "Russian Movie", kinopoisk_id=654321)

        assert await storage.is_watched(sample_user.id, kinopoisk_id=654321) is True
        assert await storage.is_watched(sample_user.id, kinopoisk_id=111111) is False

    @pytest.mark.asyncio
    async def test_delete_watched(self, storage: UserStorage, sample_user: User):
        """Test deleting watched item."""
        item = await storage.add_watched(sample_user.id, "movie", "To Delete", tmdb_id=999)

        deleted = await storage.delete_watched(item.id)
        assert deleted is True

        items = await storage.get_watched(sample_user.id)
        assert all(i.id != item.id for i in items)


# =============================================================================
# Context Manager Tests
# =============================================================================


class TestContextManager:
    """Tests for context manager functionality."""

    @pytest.mark.asyncio
    async def test_storage_context_manager(self, temp_db_path: Path, encryption_key: str):
        """Test storage works as async context manager."""
        async with UserStorage(temp_db_path, encryption_key) as storage:
            user = await storage.create_user(telegram_id=555666777)
            assert user.telegram_id == 555666777

        # Should be closed after context
        assert storage._db is None

    @pytest.mark.asyncio
    async def test_get_user_storage_helper(self, temp_db_path: Path, encryption_key: str):
        """Test get_user_storage convenience function."""
        async with get_user_storage(temp_db_path, encryption_key) as storage:
            user = await storage.create_user(telegram_id=888999000)
            assert user is not None

    @pytest.mark.asyncio
    async def test_db_property_raises_when_not_connected(self, temp_db_path: Path):
        """Test accessing db property raises when not connected."""
        storage = UserStorage(temp_db_path)

        with pytest.raises(RuntimeError, match="Database not connected"):
            _ = storage.db


# =============================================================================
# Data Model Tests
# =============================================================================


class TestDataModels:
    """Tests for Pydantic data models."""

    def test_credential_type_enum(self):
        """Test CredentialType enum values."""
        assert CredentialType.TRAKT_TOKEN.value == "trakt_token"
        assert CredentialType.SEEDBOX_PASSWORD.value == "seedbox_password"

    def test_preference_defaults(self):
        """Test Preference model defaults."""
        pref = Preference(
            id=1,
            user_id=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        assert pref.video_quality == "1080p"
        assert pref.audio_language == "ru"
        assert pref.preferred_genres == []
        assert pref.excluded_genres == []
        assert pref.auto_download is False
        assert pref.notification_enabled is True

    def test_watched_item_model(self):
        """Test WatchedItem model."""
        item = WatchedItem(
            id=1,
            user_id=1,
            media_type="movie",
            title="Test",
            tmdb_id=123,
            watched_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )

        assert item.media_type == "movie"
        assert item.tmdb_id == 123
        assert item.season is None
        assert item.episode is None
