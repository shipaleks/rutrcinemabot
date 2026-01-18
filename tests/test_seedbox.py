"""Unit tests for seedbox client module."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.seedbox.client import (
    DelugeClient,
    QBittorrentClient,
    SeedboxAuthError,
    SeedboxConnectionError,
    SeedboxError,
    SeedboxTorrentError,
    SeedboxType,
    TorrentInfo,
    TorrentStatus,
    TransmissionClient,
    create_seedbox_client,
    detect_seedbox_type,
    extract_magnet_hash,
    get_torrent_status,
    is_seedbox_configured,
    send_magnet_to_seedbox,
)

# ============================================================================
# TorrentInfo Tests
# ============================================================================


class TestTorrentInfo:
    """Tests for TorrentInfo class."""

    def test_init(self):
        """Test TorrentInfo initialization."""
        info = TorrentInfo(
            hash="abc123",
            name="Test Torrent",
            status=TorrentStatus.DOWNLOADING,
            progress=0.5,
            size=1000000,
            downloaded=500000,
        )
        assert info.hash == "abc123"
        assert info.name == "Test Torrent"
        assert info.status == TorrentStatus.DOWNLOADING
        assert info.progress == 0.5
        assert info.size == 1000000
        assert info.downloaded == 500000

    def test_progress_percent(self):
        """Test progress_percent property."""
        info = TorrentInfo(
            hash="abc123",
            name="Test",
            status=TorrentStatus.DOWNLOADING,
            progress=0.75,
        )
        assert info.progress_percent == 75.0

    def test_is_complete_with_progress(self):
        """Test is_complete with 100% progress."""
        info = TorrentInfo(
            hash="abc123",
            name="Test",
            status=TorrentStatus.DOWNLOADING,
            progress=1.0,
        )
        assert info.is_complete is True

    def test_is_complete_with_seeding_status(self):
        """Test is_complete with seeding status."""
        info = TorrentInfo(
            hash="abc123",
            name="Test",
            status=TorrentStatus.SEEDING,
            progress=0.5,  # Even partial progress
        )
        assert info.is_complete is True

    def test_is_not_complete(self):
        """Test is_complete returns False for incomplete torrents."""
        info = TorrentInfo(
            hash="abc123",
            name="Test",
            status=TorrentStatus.DOWNLOADING,
            progress=0.5,
        )
        assert info.is_complete is False

    def test_repr(self):
        """Test TorrentInfo __repr__."""
        info = TorrentInfo(
            hash="abc123",
            name="Test",
            status=TorrentStatus.DOWNLOADING,
            progress=0.5,
        )
        repr_str = repr(info)
        assert "abc123" in repr_str
        assert "Test" in repr_str
        assert "50.0%" in repr_str


# ============================================================================
# TorrentStatus Tests
# ============================================================================


class TestTorrentStatus:
    """Tests for TorrentStatus enum."""

    def test_all_statuses(self):
        """Test all status values exist."""
        assert TorrentStatus.DOWNLOADING.value == "downloading"
        assert TorrentStatus.SEEDING.value == "seeding"
        assert TorrentStatus.PAUSED.value == "paused"
        assert TorrentStatus.CHECKING.value == "checking"
        assert TorrentStatus.QUEUED.value == "queued"
        assert TorrentStatus.ERROR.value == "error"
        assert TorrentStatus.UNKNOWN.value == "unknown"


# ============================================================================
# SeedboxType Tests
# ============================================================================


class TestSeedboxType:
    """Tests for SeedboxType enum."""

    def test_all_types(self):
        """Test all client types exist."""
        assert SeedboxType.TRANSMISSION.value == "transmission"
        assert SeedboxType.QBITTORRENT.value == "qbittorrent"
        assert SeedboxType.DELUGE.value == "deluge"


# ============================================================================
# Exception Tests
# ============================================================================


class TestExceptions:
    """Tests for seedbox exceptions."""

    def test_seedbox_error_inheritance(self):
        """Test exception hierarchy."""
        assert issubclass(SeedboxAuthError, SeedboxError)
        assert issubclass(SeedboxConnectionError, SeedboxError)
        assert issubclass(SeedboxTorrentError, SeedboxError)

    def test_exception_messages(self):
        """Test exception messages."""
        error = SeedboxAuthError("Invalid credentials")
        assert str(error) == "Invalid credentials"


# ============================================================================
# Helper Function Tests
# ============================================================================


class TestDetectSeedboxType:
    """Tests for detect_seedbox_type function."""

    def test_transmission_by_name(self):
        """Test detection by transmission in URL."""
        assert detect_seedbox_type("http://transmission.example.com") == SeedboxType.TRANSMISSION

    def test_transmission_by_port(self):
        """Test detection by port 9091."""
        assert detect_seedbox_type("http://example.com:9091") == SeedboxType.TRANSMISSION

    def test_qbittorrent_by_name(self):
        """Test detection by qbittorrent in URL."""
        assert detect_seedbox_type("http://qbittorrent.example.com") == SeedboxType.QBITTORRENT

    def test_qbittorrent_by_port(self):
        """Test detection by port 8080."""
        assert detect_seedbox_type("http://example.com:8080") == SeedboxType.QBITTORRENT

    def test_deluge_by_name(self):
        """Test detection by deluge in URL."""
        assert detect_seedbox_type("http://deluge.example.com") == SeedboxType.DELUGE

    def test_deluge_by_port(self):
        """Test detection by port 8112."""
        assert detect_seedbox_type("http://example.com:8112") == SeedboxType.DELUGE

    def test_default_to_transmission(self):
        """Test default to Transmission for unknown URLs."""
        assert detect_seedbox_type("http://example.com") == SeedboxType.TRANSMISSION


class TestExtractMagnetHash:
    """Tests for extract_magnet_hash function."""

    def test_hex_hash(self):
        """Test extraction of hex hash."""
        magnet = "magnet:?xt=urn:btih:abc123def456&dn=test"
        assert extract_magnet_hash(magnet) == "abc123def456"

    def test_lowercase(self):
        """Test hash is converted to lowercase."""
        magnet = "magnet:?xt=urn:btih:ABC123DEF456&dn=test"
        assert extract_magnet_hash(magnet) == "abc123def456"

    def test_base32_hash(self):
        """Test extraction of base32 hash."""
        # 32-char base32 hash (20 bytes = 32 chars in base32, 40 chars in hex)
        # This is a valid base32-encoded 20-byte hash
        magnet = "magnet:?xt=urn:btih:CDEFGHIJKLMNOPQRSTUVWXYZ234567AB&dn=test"
        result = extract_magnet_hash(magnet)
        assert len(result) == 40  # 20 bytes = 40 hex chars

    def test_no_hash(self):
        """Test empty string for invalid magnet."""
        assert extract_magnet_hash("not-a-magnet") == ""

    def test_empty_string(self):
        """Test empty string input."""
        assert extract_magnet_hash("") == ""


class TestCreateSeedboxClient:
    """Tests for create_seedbox_client function."""

    def test_transmission(self):
        """Test creating Transmission client."""
        client = create_seedbox_client(
            "http://example.com",
            "user",
            "pass",
            SeedboxType.TRANSMISSION,
        )
        assert isinstance(client, TransmissionClient)

    def test_qbittorrent(self):
        """Test creating qBittorrent client."""
        client = create_seedbox_client(
            "http://example.com",
            "user",
            "pass",
            SeedboxType.QBITTORRENT,
        )
        assert isinstance(client, QBittorrentClient)

    def test_deluge(self):
        """Test creating Deluge client."""
        client = create_seedbox_client(
            "http://example.com",
            "user",
            "pass",
            SeedboxType.DELUGE,
        )
        assert isinstance(client, DelugeClient)

    def test_auto_detect(self):
        """Test auto-detection of client type."""
        client = create_seedbox_client(
            "http://qbittorrent.example.com:8080",
            "user",
            "pass",
        )
        assert isinstance(client, QBittorrentClient)


# ============================================================================
# TransmissionClient Tests
# ============================================================================


class TestTransmissionClient:
    """Tests for TransmissionClient."""

    def test_init(self):
        """Test Transmission client initialization."""
        client = TransmissionClient(
            "http://example.com:9091",
            "user",
            "pass",
        )
        assert client.host == "http://example.com:9091"
        assert client.username == "user"
        assert client.password == "pass"
        assert client.rpc_path == "/transmission/rpc"

    def test_init_strips_trailing_slash(self):
        """Test trailing slash is stripped from host."""
        client = TransmissionClient("http://example.com:9091/", "user", "pass")
        assert client.host == "http://example.com:9091"

    @pytest.mark.asyncio
    async def test_authenticate_success(self):
        """Test successful Transmission authentication."""
        client = TransmissionClient("http://example.com:9091", "user", "pass")

        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_response.headers = {"X-Transmission-Session-Id": "test-session-id"}

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client._client = httpx.AsyncClient()

            await client.authenticate()

            assert client._session_id == "test-session-id"

    @pytest.mark.asyncio
    async def test_authenticate_invalid_credentials(self):
        """Test authentication with invalid credentials."""
        client = TransmissionClient("http://example.com:9091", "user", "wrong")

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client._client = httpx.AsyncClient()

            with pytest.raises(SeedboxAuthError, match="Invalid Transmission credentials"):
                await client.authenticate()

    @pytest.mark.asyncio
    async def test_authenticate_connection_error(self):
        """Test authentication with connection error."""
        client = TransmissionClient("http://example.com:9091", "user", "pass")

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.ConnectError("Connection refused")
            client._client = httpx.AsyncClient()

            with pytest.raises(SeedboxConnectionError, match="Failed to connect"):
                await client.authenticate()

    def test_map_status(self):
        """Test Transmission status mapping."""
        client = TransmissionClient("http://example.com", "user", "pass")

        assert client._map_status(0) == TorrentStatus.PAUSED
        assert client._map_status(4) == TorrentStatus.DOWNLOADING
        assert client._map_status(6) == TorrentStatus.SEEDING
        assert client._map_status(-1) == TorrentStatus.UNKNOWN


# ============================================================================
# QBittorrentClient Tests
# ============================================================================


class TestQBittorrentClient:
    """Tests for QBittorrentClient."""

    def test_init(self):
        """Test qBittorrent client initialization."""
        client = QBittorrentClient(
            "http://example.com:8080",
            "user",
            "pass",
        )
        assert client.host == "http://example.com:8080"
        assert client.api_path == "/api/v2"

    @pytest.mark.asyncio
    async def test_authenticate_success(self):
        """Test successful qBittorrent authentication."""
        client = QBittorrentClient("http://example.com:8080", "user", "pass")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "Ok."
        mock_response.cookies = {"SID": "test-session"}

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client._client = httpx.AsyncClient()

            await client.authenticate()

            assert client._cookies == {"SID": "test-session"}

    @pytest.mark.asyncio
    async def test_authenticate_invalid_credentials(self):
        """Test authentication with invalid credentials."""
        client = QBittorrentClient("http://example.com:8080", "user", "wrong")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "Fails."

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client._client = httpx.AsyncClient()

            with pytest.raises(SeedboxAuthError, match="Invalid qBittorrent credentials"):
                await client.authenticate()

    def test_extract_hash_from_magnet_hex(self):
        """Test hash extraction from magnet with hex hash."""
        client = QBittorrentClient("http://example.com", "user", "pass")
        magnet = "magnet:?xt=urn:btih:ABC123DEF456789012345678901234567890ABCD&dn=test"
        result = client._extract_hash_from_magnet(magnet)
        assert result == "abc123def456789012345678901234567890abcd"

    def test_map_status(self):
        """Test qBittorrent status mapping."""
        client = QBittorrentClient("http://example.com", "user", "pass")

        assert client._map_status("downloading") == TorrentStatus.DOWNLOADING
        assert client._map_status("uploading") == TorrentStatus.SEEDING
        assert client._map_status("pausedDL") == TorrentStatus.PAUSED
        assert client._map_status("error") == TorrentStatus.ERROR
        assert client._map_status("unknown_state") == TorrentStatus.UNKNOWN


# ============================================================================
# DelugeClient Tests
# ============================================================================


class TestDelugeClient:
    """Tests for DelugeClient."""

    def test_init(self):
        """Test Deluge client initialization."""
        client = DelugeClient(
            "http://example.com:8112",
            "user",
            "pass",
        )
        assert client.host == "http://example.com:8112"
        assert client.api_path == "/json"

    @pytest.mark.asyncio
    async def test_authenticate_success(self):
        """Test successful Deluge authentication."""
        client = DelugeClient("http://example.com:8112", "user", "pass")

        login_response = MagicMock()
        login_response.status_code = 200
        login_response.json.return_value = {"result": True, "error": None}
        login_response.cookies = {"_session_id": "test"}

        connected_response = MagicMock()
        connected_response.status_code = 200
        connected_response.json.return_value = {"result": True}
        connected_response.cookies = {}

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = [login_response, connected_response]
            client._client = httpx.AsyncClient()

            await client.authenticate()

            assert "_session_id" in client._cookies

    @pytest.mark.asyncio
    async def test_authenticate_invalid_password(self):
        """Test authentication with invalid password."""
        client = DelugeClient("http://example.com:8112", "user", "wrong")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": False, "error": None}
        mock_response.cookies = {}

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client._client = httpx.AsyncClient()

            with pytest.raises(SeedboxAuthError, match="Invalid Deluge password"):
                await client.authenticate()

    def test_map_status(self):
        """Test Deluge status mapping."""
        client = DelugeClient("http://example.com", "user", "pass")

        assert client._map_status("Downloading") == TorrentStatus.DOWNLOADING
        assert client._map_status("Seeding") == TorrentStatus.SEEDING
        assert client._map_status("Paused") == TorrentStatus.PAUSED
        assert client._map_status("Error") == TorrentStatus.ERROR
        assert client._map_status("Unknown") == TorrentStatus.UNKNOWN


# ============================================================================
# Convenience Function Tests
# ============================================================================


class TestIsSeedboxConfigured:
    """Tests for is_seedbox_configured function."""

    def test_configured(self):
        """Test when seedbox is configured."""
        with patch("src.seedbox.client.settings") as mock_settings:
            mock_settings.has_seedbox = True
            assert is_seedbox_configured() is True

    def test_not_configured(self):
        """Test when seedbox is not configured."""
        with patch("src.seedbox.client.settings") as mock_settings:
            mock_settings.has_seedbox = False
            assert is_seedbox_configured() is False


class TestSendMagnetToSeedbox:
    """Tests for send_magnet_to_seedbox function."""

    @pytest.mark.asyncio
    async def test_seedbox_not_configured(self):
        """Test sending magnet when seedbox not configured."""
        with patch("src.seedbox.client.is_seedbox_configured", return_value=False):
            result = await send_magnet_to_seedbox("magnet:?xt=urn:btih:abc123&dn=test")

            assert result["status"] == "magnet"
            assert result["magnet"].startswith("magnet:")
            assert "hash" in result
            assert "message" in result

    @pytest.mark.asyncio
    async def test_successful_send(self):
        """Test successful magnet send."""
        mock_client = AsyncMock()
        mock_client.add_magnet = AsyncMock(return_value="abc123")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_password = MagicMock()
        mock_password.get_secret_value.return_value = "password"

        with (
            patch("src.seedbox.client.is_seedbox_configured", return_value=True),
            patch("src.seedbox.client.settings") as mock_settings,
            patch("src.seedbox.client.create_seedbox_client", return_value=mock_client),
        ):
            mock_settings.seedbox_host = "http://example.com"
            mock_settings.seedbox_user = "user"
            mock_settings.seedbox_password = mock_password

            result = await send_magnet_to_seedbox("magnet:?xt=urn:btih:abc123&dn=test")

            assert result["status"] == "sent"
            assert result["hash"] == "abc123"
            assert result["seedbox"] == "http://example.com"

    @pytest.mark.asyncio
    async def test_auth_error(self):
        """Test handling auth error."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(side_effect=SeedboxAuthError("Invalid"))
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_password = MagicMock()
        mock_password.get_secret_value.return_value = "password"

        with (
            patch("src.seedbox.client.is_seedbox_configured", return_value=True),
            patch("src.seedbox.client.settings") as mock_settings,
            patch("src.seedbox.client.create_seedbox_client", return_value=mock_client),
        ):
            mock_settings.seedbox_host = "http://example.com"
            mock_settings.seedbox_user = "user"
            mock_settings.seedbox_password = mock_password

            result = await send_magnet_to_seedbox("magnet:?xt=urn:btih:abc123&dn=test")

            assert result["status"] == "error"
            assert "авторизации" in result["error"]
            assert result["magnet"].startswith("magnet:")


class TestGetTorrentStatus:
    """Tests for get_torrent_status function."""

    @pytest.mark.asyncio
    async def test_seedbox_not_configured(self):
        """Test getting status when seedbox not configured."""
        with patch("src.seedbox.client.is_seedbox_configured", return_value=False):
            result = await get_torrent_status("abc123")

            assert result["status"] == "error"
            assert "не настроен" in result["error"]

    @pytest.mark.asyncio
    async def test_torrent_found(self):
        """Test getting status for existing torrent."""
        mock_info = TorrentInfo(
            hash="abc123",
            name="Test Torrent",
            status=TorrentStatus.DOWNLOADING,
            progress=0.5,
            size=1000000,
            downloaded=500000,
        )

        mock_client = AsyncMock()
        mock_client.get_torrent_status = AsyncMock(return_value=mock_info)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_password = MagicMock()
        mock_password.get_secret_value.return_value = "password"

        with (
            patch("src.seedbox.client.is_seedbox_configured", return_value=True),
            patch("src.seedbox.client.settings") as mock_settings,
            patch("src.seedbox.client.create_seedbox_client", return_value=mock_client),
        ):
            mock_settings.seedbox_host = "http://example.com"
            mock_settings.seedbox_user = "user"
            mock_settings.seedbox_password = mock_password

            result = await get_torrent_status("abc123")

            assert result["status"] == "found"
            assert result["hash"] == "abc123"
            assert result["name"] == "Test Torrent"
            assert result["progress"] == 50.0

    @pytest.mark.asyncio
    async def test_torrent_not_found(self):
        """Test getting status for non-existing torrent."""
        mock_client = AsyncMock()
        mock_client.get_torrent_status = AsyncMock(return_value=None)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_password = MagicMock()
        mock_password.get_secret_value.return_value = "password"

        with (
            patch("src.seedbox.client.is_seedbox_configured", return_value=True),
            patch("src.seedbox.client.settings") as mock_settings,
            patch("src.seedbox.client.create_seedbox_client", return_value=mock_client),
        ):
            mock_settings.seedbox_host = "http://example.com"
            mock_settings.seedbox_user = "user"
            mock_settings.seedbox_password = mock_password

            result = await get_torrent_status("abc123")

            assert result["status"] == "not_found"
            assert result["hash"] == "abc123"


# ============================================================================
# Context Manager Tests
# ============================================================================


class TestContextManager:
    """Tests for async context manager behavior."""

    @pytest.mark.asyncio
    async def test_client_property_raises_without_context(self):
        """Test client property raises error outside context."""
        client = TransmissionClient("http://example.com", "user", "pass")

        with pytest.raises(RuntimeError, match="must be used as async context manager"):
            _ = client.client

    @pytest.mark.asyncio
    async def test_context_creates_and_closes_client(self):
        """Test context manager creates and closes HTTP client."""
        client = TransmissionClient("http://example.com", "user", "pass")

        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_response.headers = {"X-Transmission-Session-Id": "test"}

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            async with client:
                assert client._client is not None
                # Can access client property
                _ = client.client

        # After exiting context
        assert client._client is None
