"""Tests for Rutracker search functionality."""

from unittest.mock import MagicMock, patch

import pytest

from src.search.rutracker import (
    ContentCategory,
    RutrackerAuthError,
    RutrackerBlockedError,
    RutrackerCaptchaError,
    RutrackerClient,
    RutrackerError,
    RutrackerParseError,
    SearchResult,
    VideoQuality,
    build_magnet_link,
    detect_quality,
    extract_magnet_hash,
    parse_size,
    search_rutracker,
    search_with_fallback,
)

# =============================================================================
# Sample HTML for Testing
# =============================================================================

SAMPLE_SEARCH_HTML = """
<!DOCTYPE html>
<html>
<head><title>Rutracker Search Results</title></head>
<body>
<table id="tor-tbl">
<tbody>
<tr class="tCenter hl-tr">
    <td class="f-name"><a class="f" href="/forum/viewforum.php?f=7">Фильмы</a></td>
    <td><a class="tLink" href="viewtopic.php?t=12345" data-topic_id="12345">Dune (2021) 1080p BDRip x264</a></td>
    <td class="tor-size"><a class="dl-stub">4.37 GB</a></td>
    <td class="seedmed"><b class="seedmed">150</b></td>
    <td class="leechmed"><b class="leechmed">25</b></td>
</tr>
<tr class="tCenter hl-tr">
    <td class="f-name"><a class="f" href="/forum/viewforum.php?f=187">HD Video</a></td>
    <td><a class="tLink" href="viewtopic.php?t=67890" data-topic_id="67890">Dune (2021) 4K UHD HDR10 Remux</a></td>
    <td class="tor-size"><a class="dl-stub">65.2 GB</a></td>
    <td class="seedmed"><b class="seedmed">50</b></td>
    <td class="leechmed"><b class="leechmed">10</b></td>
</tr>
<tr class="tCenter hl-tr">
    <td class="f-name"><a class="f" href="/forum/viewforum.php?f=7">Фильмы</a></td>
    <td><a class="tLink" href="viewtopic.php?t=11111" data-topic_id="11111">Dune (2021) 720p WEB-DL</a></td>
    <td class="tor-size"><a class="dl-stub">2.1 GB</a></td>
    <td class="seedmed"><b class="seedmed">200</b></td>
    <td class="leechmed"><b class="leechmed">15</b></td>
</tr>
</tbody>
</table>
</body>
</html>
"""

SAMPLE_TOPIC_HTML_WITH_MAGNET = """
<!DOCTYPE html>
<html>
<head><title>Dune (2021)</title></head>
<body>
<h1 class="maintitle"><a>Dune (2021) 1080p BDRip x264</a></h1>
<a href="magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=Dune+2021">Download via magnet</a>
</body>
</html>
"""

SAMPLE_TOPIC_HTML_WITH_HASH = """
<!DOCTYPE html>
<html>
<head><title>Dune (2021)</title></head>
<body>
<h1 class="maintitle"><a>Dune (2021) 1080p BDRip x264</a></h1>
<a class="dl-stub" href="dl.php?t=12345" data-hash="ABCDEF0123456789ABCDEF0123456789ABCDEF01">Download</a>
</body>
</html>
"""

SAMPLE_CAPTCHA_HTML = """
<!DOCTYPE html>
<html>
<head><title>Captcha Required</title></head>
<body>
<div class="captcha">Please solve the captcha to continue</div>
</body>
</html>
"""

SAMPLE_BLOCKED_HTML = """
<!DOCTYPE html>
<html>
<head><title>Access Denied</title></head>
<body>
<div>Сайт заблокирован в вашем регионе</div>
</body>
</html>
"""

SAMPLE_EMPTY_RESULTS_HTML = """
<!DOCTYPE html>
<html>
<head><title>No Results</title></head>
<body>
<table id="tor-tbl"><tbody></tbody></table>
<div>Ничего не найдено</div>
</body>
</html>
"""

SAMPLE_LOGIN_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Login</title></head>
<body>
<form action="login.php" method="post">
<input type="text" name="login_username" />
<input type="password" name="login_password" />
<input type="submit" name="login" value="Вход" />
</form>
</body>
</html>
"""

SAMPLE_LOGIN_SUCCESS_HTML = """
<!DOCTYPE html>
<html>
<head><title>Index</title></head>
<body>
<a href="login.php?logout=1">Выход</a>
<div>Добро пожаловать!</div>
</body>
</html>
"""

SAMPLE_LOGIN_FAILED_PASSWORD_HTML = """
<!DOCTYPE html>
<html>
<head><title>Login Error</title></head>
<body>
<div class="error">Неверный пароль</div>
</body>
</html>
"""

SAMPLE_LOGIN_FAILED_USER_HTML = """
<!DOCTYPE html>
<html>
<head><title>Login Error</title></head>
<body>
<div class="error">Пользователь не найден</div>
</body>
</html>
"""


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestDetectQuality:
    """Tests for quality detection function."""

    def test_detect_720p(self):
        """Test 720p quality detection."""
        assert detect_quality("Movie 720p WEB-DL") == "720p"
        assert detect_quality("Film HD 720") == "720p"

    def test_detect_1080p(self):
        """Test 1080p quality detection."""
        assert detect_quality("Movie 1080p BluRay") == "1080p"
        assert detect_quality("Film Full HD") == "1080p"
        assert detect_quality("Movie FHD Remux") == "1080p"

    def test_detect_4k(self):
        """Test 4K quality detection."""
        assert detect_quality("Movie 4K UHD") == "4K"
        assert detect_quality("Film Ultra HD") == "4K"
        assert detect_quality("Movie UHD HDR") == "4K"

    def test_detect_2160p(self):
        """Test 2160p quality detection - now unified with 4K."""
        assert detect_quality("Movie 2160p Remux") == "4K"  # 2160p is unified with 4K

    def test_detect_hdr(self):
        """Test HDR quality detection."""
        assert detect_quality("Movie 4K HDR10") == "4K"  # 4K takes priority
        assert detect_quality("Movie Dolby Vision") == "HDR"

    def test_no_quality_detected(self):
        """Test when no quality is detected."""
        assert detect_quality("Movie Title") is None
        assert detect_quality("Some Random Text") is None


class TestParseSize:
    """Tests for size parsing function."""

    def test_parse_gb(self):
        """Test parsing GB sizes."""
        size, size_bytes = parse_size("4.37 GB")
        assert size == "4.37 GB"
        assert size_bytes == int(4.37 * 1024**3)

    def test_parse_mb(self):
        """Test parsing MB sizes."""
        size, size_bytes = parse_size("700 MB")
        assert size == "700 MB"
        assert size_bytes == int(700 * 1024**2)

    def test_parse_tb(self):
        """Test parsing TB sizes."""
        size, size_bytes = parse_size("1.5 TB")
        assert size == "1.5 TB"
        assert size_bytes == int(1.5 * 1024**4)

    def test_parse_comma_decimal(self):
        """Test parsing with comma as decimal separator."""
        size, size_bytes = parse_size("4,37 GB")
        assert size == "4,37 GB"
        assert size_bytes == int(4.37 * 1024**3)

    def test_parse_invalid_size(self):
        """Test parsing invalid size string."""
        size, size_bytes = parse_size("Unknown")
        assert size == "Unknown"
        assert size_bytes == 0

    def test_parse_gib(self):
        """Test parsing GiB sizes."""
        size, size_bytes = parse_size("4.0 GiB")
        assert size == "4.0 GiB"
        assert size_bytes == int(4.0 * 1024**3)


class TestExtractMagnetHash:
    """Tests for magnet hash extraction."""

    def test_extract_from_magnet(self):
        """Test extracting hash from magnet link."""
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Test"
        hash_result = extract_magnet_hash(magnet)
        assert hash_result == "0123456789ABCDEF0123456789ABCDEF01234567"

    def test_extract_raw_hash(self):
        """Test returning raw hash as-is."""
        raw_hash = "0123456789abcdef0123456789abcdef01234567"
        hash_result = extract_magnet_hash(raw_hash)
        assert hash_result == raw_hash.upper()


class TestBuildMagnetLink:
    """Tests for magnet link building."""

    def test_build_basic_magnet(self):
        """Test building basic magnet link."""
        magnet = build_magnet_link("0123456789ABCDEF0123456789ABCDEF01234567")
        assert magnet.startswith("magnet:?xt=urn:btih:")
        assert "0123456789ABCDEF0123456789ABCDEF01234567" in magnet
        assert "&tr=" in magnet  # Should have trackers

    def test_build_magnet_with_name(self):
        """Test building magnet link with display name."""
        magnet = build_magnet_link("0123456789ABCDEF0123456789ABCDEF01234567", name="Test Movie")
        assert "&dn=" in magnet
        assert "Test" in magnet


# =============================================================================
# SearchResult Model Tests
# =============================================================================


class TestSearchResult:
    """Tests for SearchResult model."""

    def test_create_result(self):
        """Test creating a search result."""
        result = SearchResult(
            title="Dune (2021) 1080p",
            size="4.5 GB",
            size_bytes=4831838208,
            seeds=100,
            leeches=20,
            magnet="magnet:?xt=urn:btih:...",
            topic_id=12345,
            quality="1080p",
            forum_name="Фильмы",
        )
        assert result.title == "Dune (2021) 1080p"
        assert result.seeds == 100
        assert result.quality == "1080p"

    def test_result_with_defaults(self):
        """Test search result with default values."""
        result = SearchResult(title="Test Movie", size="1 GB", topic_id=12345)
        assert result.seeds == 0
        assert result.leeches == 0
        assert result.magnet == ""
        assert result.quality is None

    def test_to_display_string(self):
        """Test display string formatting."""
        result = SearchResult(
            title="Dune (2021)",
            size="4.5 GB",
            seeds=100,
            topic_id=12345,
            quality="1080p",
        )
        display = result.to_display_string()
        assert "Dune (2021)" in display
        assert "[1080p]" in display
        assert "4.5 GB" in display
        assert "S:100" in display

    def test_to_display_string_no_quality(self):
        """Test display string without quality."""
        result = SearchResult(
            title="Test Movie",
            size="2 GB",
            seeds=0,
            topic_id=12345,
        )
        display = result.to_display_string()
        assert "[" not in display  # No quality brackets
        assert "S:?" in display  # No seeds indicator


# =============================================================================
# RutrackerClient Tests
# =============================================================================


class TestRutrackerClientInit:
    """Tests for RutrackerClient initialization."""

    def test_default_init(self):
        """Test default initialization."""
        client = RutrackerClient()
        assert client.base_url == "https://rutracker.org"
        assert client.timeout == 30.0

    def test_custom_init(self):
        """Test custom initialization."""
        client = RutrackerClient(base_url="https://rutracker.net/", timeout=60.0)
        assert client.base_url == "https://rutracker.net"  # Trailing slash removed
        assert client.timeout == 60.0


class TestRutrackerClientContextManager:
    """Tests for async context manager."""

    @pytest.mark.asyncio
    async def test_context_manager_enter_exit(self):
        """Test async context manager properly initializes and closes client."""
        client = RutrackerClient()

        assert client._client is None

        async with client as c:
            assert c._client is not None
            assert c._client == client._client

        # Client should be closed after exit
        assert client._client is None

    @pytest.mark.asyncio
    async def test_client_property_raises_outside_context(self):
        """Test that client property raises error when not in context."""
        client = RutrackerClient()

        with pytest.raises(RuntimeError, match="not initialized"):
            _ = client.client


class TestRutrackerClientParsing:
    """Tests for HTML parsing."""

    @pytest.mark.asyncio
    async def test_parse_search_results(self):
        """Test parsing search results HTML."""
        async with RutrackerClient() as client:
            results = client._parse_search_results(SAMPLE_SEARCH_HTML)

        assert len(results) == 3

        # Check first result (1080p)
        assert results[0].title == "Dune (2021) 1080p BDRip x264"
        assert results[0].topic_id == 12345
        assert results[0].seeds == 150
        assert results[0].quality == "1080p"
        assert results[0].forum_name == "Фильмы"

        # Check second result (4K)
        assert results[1].topic_id == 67890
        assert results[1].quality == "4K"

        # Check third result (720p)
        assert results[2].quality == "720p"
        assert results[2].seeds == 200

    @pytest.mark.asyncio
    async def test_parse_empty_results(self):
        """Test parsing empty search results."""
        async with RutrackerClient() as client:
            results = client._parse_search_results(SAMPLE_EMPTY_RESULTS_HTML)

        assert len(results) == 0


class TestRutrackerClientFetch:
    """Tests for HTTP fetching."""

    @pytest.mark.asyncio
    async def test_fetch_page_captcha_detection(self):
        """Test captcha detection."""
        with patch("httpx.AsyncClient.get") as mock_get:
            mock_response = MagicMock()
            mock_response.text = SAMPLE_CAPTCHA_HTML
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            async with RutrackerClient() as client:
                with pytest.raises(RutrackerCaptchaError, match="Captcha required"):
                    await client._fetch_page("http://test.com")

    @pytest.mark.asyncio
    async def test_fetch_page_blocked_detection(self):
        """Test blocked site detection."""
        with patch("httpx.AsyncClient.get") as mock_get:
            mock_response = MagicMock()
            mock_response.text = SAMPLE_BLOCKED_HTML
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            async with RutrackerClient() as client:
                with pytest.raises(RutrackerBlockedError, match="blocked"):
                    await client._fetch_page("http://test.com")


class TestRutrackerClientSearch:
    """Tests for search functionality."""

    @pytest.mark.asyncio
    async def test_search_basic(self):
        """Test basic search functionality."""
        with patch.object(RutrackerClient, "_fetch_page") as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with RutrackerClient() as client:
                results = await client.search("Dune 2021", fetch_magnets=False)

            assert len(results) == 3
            # Results should be sorted by seeds (descending)
            assert results[0].seeds >= results[1].seeds

    @pytest.mark.asyncio
    async def test_search_with_quality_filter(self):
        """Test search with quality filtering."""
        with patch.object(RutrackerClient, "_fetch_page") as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with RutrackerClient() as client:
                results = await client.search("Dune 2021", quality="1080p", fetch_magnets=False)

            # Should only return 1080p results
            assert len(results) == 1
            assert results[0].quality == "1080p"

    @pytest.mark.asyncio
    async def test_search_with_4k_filter(self):
        """Test search filtering by 4K quality."""
        with patch.object(RutrackerClient, "_fetch_page") as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with RutrackerClient() as client:
                results = await client.search("Dune 2021", quality="4K", fetch_magnets=False)

            assert len(results) == 1
            assert results[0].quality == "4K"


class TestRutrackerClientMagnetLink:
    """Tests for magnet link extraction."""

    @pytest.mark.asyncio
    async def test_get_magnet_from_page_with_magnet(self):
        """Test extracting magnet link from page."""
        with patch.object(RutrackerClient, "_fetch_page") as mock_fetch:
            mock_fetch.return_value = SAMPLE_TOPIC_HTML_WITH_MAGNET

            async with RutrackerClient() as client:
                magnet = await client.get_magnet_link(12345)

            assert magnet.startswith("magnet:?xt=urn:btih:")
            assert "0123456789ABCDEF" in magnet.upper()

    @pytest.mark.asyncio
    async def test_get_magnet_not_found(self):
        """Test error when magnet link not found."""
        with patch.object(RutrackerClient, "_fetch_page") as mock_fetch:
            mock_fetch.return_value = "<html><body>No magnet here</body></html>"

            async with RutrackerClient() as client:
                with pytest.raises(RutrackerError, match="Could not extract magnet"):
                    await client.get_magnet_link(12345)


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestSearchRutracker:
    """Tests for search_rutracker convenience function."""

    @pytest.mark.asyncio
    async def test_search_rutracker_function(self):
        """Test the convenience search function."""
        with patch.object(RutrackerClient, "_fetch_page") as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            await search_rutracker("Dune 2021")

            # Function should work through the client
            assert mock_fetch.called

    @pytest.mark.asyncio
    async def test_search_rutracker_with_quality(self):
        """Test search with quality parameter."""
        with patch.object(RutrackerClient, "_fetch_page") as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            results = await search_rutracker("Dune 2021", quality="720p")

            assert all(r.quality == "720p" for r in results if r.quality)


class TestSearchWithFallback:
    """Tests for search_with_fallback function."""

    @pytest.mark.asyncio
    async def test_fallback_on_blocked(self):
        """Test fallback to mirrors when blocked."""
        call_count = 0

        async def mock_search(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RutrackerBlockedError("Site blocked")
            return [SearchResult(title="Test", size="1 GB", topic_id=123, magnet="magnet:...")]

        with patch.object(RutrackerClient, "search", mock_search):
            results = await search_with_fallback("Test")

            assert len(results) == 1
            assert call_count == 2  # First failed, second succeeded

    @pytest.mark.asyncio
    async def test_captcha_does_not_fallback(self):
        """Test that captcha error doesn't trigger fallback."""

        async def mock_search(*_args, **_kwargs):
            raise RutrackerCaptchaError("Captcha required")

        with (
            patch.object(RutrackerClient, "search", mock_search),
            pytest.raises(RutrackerCaptchaError),
        ):
            await search_with_fallback("Test")


# =============================================================================
# Authentication Tests
# =============================================================================


class TestRutrackerClientCredentials:
    """Tests for client credentials handling."""

    def test_init_without_credentials(self):
        """Test initialization without credentials."""
        client = RutrackerClient()
        assert client._username is None
        assert client._password is None
        assert client.has_credentials is False

    def test_init_with_credentials(self):
        """Test initialization with credentials."""
        client = RutrackerClient(username="testuser", password="testpass")
        assert client._username == "testuser"
        assert client._password == "testpass"
        assert client.has_credentials is True

    def test_init_with_partial_credentials(self):
        """Test that partial credentials don't count as configured."""
        client = RutrackerClient(username="testuser")
        assert client.has_credentials is False

        client = RutrackerClient(password="testpass")
        assert client.has_credentials is False


class TestRutrackerClientAuthentication:
    """Tests for authentication functionality."""

    @pytest.mark.asyncio
    async def test_login_without_credentials_raises_error(self):
        """Test that login without credentials raises RutrackerAuthError."""
        async with RutrackerClient() as client:
            with pytest.raises(RutrackerAuthError, match="credentials not configured"):
                await client._login()

    @pytest.mark.asyncio
    async def test_login_success_with_redirect(self):
        """Test successful login with redirect response."""
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 302
            mock_response.headers = {"location": "/forum/index.php"}
            mock_post.return_value = mock_response

            async with RutrackerClient(username="user", password="pass") as client:
                result = await client._login()
                # Check inside context manager since __aexit__ resets _authenticated
                assert result is True
                assert client._authenticated is True

    @pytest.mark.asyncio
    async def test_login_success_with_logout_link(self):
        """Test successful login detected by logout link presence."""
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = SAMPLE_LOGIN_SUCCESS_HTML
            mock_response.cookies = {}
            mock_post.return_value = mock_response

            async with RutrackerClient(username="user", password="pass") as client:
                result = await client._login()
                # Check inside context manager since __aexit__ resets _authenticated
                assert result is True
                assert client._authenticated is True

    @pytest.mark.asyncio
    async def test_login_failed_wrong_password(self):
        """Test login failure with wrong password."""
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = SAMPLE_LOGIN_FAILED_PASSWORD_HTML
            mock_response.cookies = {}
            mock_post.return_value = mock_response

            async with RutrackerClient(username="user", password="wrongpass") as client:
                with pytest.raises(RutrackerAuthError, match="wrong password"):
                    await client._login()

    @pytest.mark.asyncio
    async def test_login_failed_user_not_found(self):
        """Test login failure with user not found."""
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = SAMPLE_LOGIN_FAILED_USER_HTML
            mock_response.cookies = {}
            mock_post.return_value = mock_response

            async with RutrackerClient(username="baduser", password="pass") as client:
                with pytest.raises(RutrackerAuthError, match="user not found"):
                    await client._login()

    @pytest.mark.asyncio
    async def test_login_captcha_required(self):
        """Test login failure when captcha is required."""
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = SAMPLE_CAPTCHA_HTML
            mock_response.cookies = {}
            mock_post.return_value = mock_response

            async with RutrackerClient(username="user", password="pass") as client:
                with pytest.raises(RutrackerCaptchaError, match="Captcha required"):
                    await client._login()

    @pytest.mark.asyncio
    async def test_ensure_authenticated_skips_if_already_authenticated(self):
        """Test that _ensure_authenticated skips login if already authenticated."""
        async with RutrackerClient(username="user", password="pass") as client:
            client._authenticated = True

            # Should not call _login
            with patch.object(client, "_login") as mock_login:
                await client._ensure_authenticated()
                mock_login.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_authenticated_skips_without_credentials(self):
        """Test that _ensure_authenticated does nothing without credentials."""
        async with RutrackerClient() as client:
            # Should not raise any error
            await client._ensure_authenticated()
            assert client._authenticated is False


class TestRutrackerClientLoginRedirect:
    """Tests for login redirect handling in _fetch_page."""

    @pytest.mark.asyncio
    async def test_fetch_page_redirect_to_login_with_credentials(self):
        """Test that redirect to login triggers re-authentication."""
        call_count = 0

        async def mock_get(url, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_response = MagicMock()
            if call_count == 1:
                # First call returns redirect to login
                mock_response.status_code = 302
                mock_response.headers = {"location": "/forum/login.php"}
            else:
                # After re-auth, return search results
                mock_response.status_code = 200
                mock_response.text = SAMPLE_SEARCH_HTML
                mock_response.raise_for_status = MagicMock()
            return mock_response

        with (
            patch("httpx.AsyncClient.get", side_effect=mock_get),
            patch("httpx.AsyncClient.post") as mock_post,
        ):
            # Mock successful login
            login_response = MagicMock()
            login_response.status_code = 302
            login_response.headers = {"location": "/forum/index.php"}
            mock_post.return_value = login_response

            async with RutrackerClient(username="user", password="pass") as client:
                html = await client._fetch_page("http://test.com/search")

            assert SAMPLE_SEARCH_HTML in html or "Dune" in html

    @pytest.mark.asyncio
    async def test_fetch_page_redirect_to_login_without_credentials(self):
        """Test that redirect to login raises error without credentials."""
        with patch("httpx.AsyncClient.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 302
            mock_response.headers = {"location": "/forum/login.php"}
            mock_get.return_value = mock_response

            async with RutrackerClient() as client:
                with pytest.raises(RutrackerAuthError, match="Authentication required"):
                    await client._fetch_page("http://test.com/search")

    @pytest.mark.asyncio
    async def test_fetch_page_login_page_200_without_credentials(self):
        """Test that login page content (200 response) raises error without credentials."""
        with patch("httpx.AsyncClient.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = SAMPLE_LOGIN_PAGE_HTML
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            async with RutrackerClient() as client:
                with pytest.raises(RutrackerAuthError, match="Authentication required"):
                    await client._fetch_page("http://test.com/search")


class TestSearchRutrackerWithCredentials:
    """Tests for search_rutracker with credentials."""

    @pytest.mark.asyncio
    async def test_search_rutracker_with_credentials(self):
        """Test search function passes credentials to client."""
        with patch.object(RutrackerClient, "_fetch_page") as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            results = await search_rutracker(
                "Dune 2021",
                username="testuser",
                password="testpass",
            )

            assert len(results) > 0


# =============================================================================
# Enum Tests
# =============================================================================


class TestEnums:
    """Tests for quality and category enums."""

    def test_video_quality_values(self):
        """Test VideoQuality enum values."""
        assert VideoQuality.Q_720P.value == "720p"
        assert VideoQuality.Q_1080P.value == "1080p"
        assert VideoQuality.Q_4K.value == "4K"  # Also matches 2160p, UHD
        assert VideoQuality.Q_HDR.value == "HDR"

    def test_content_category_values(self):
        """Test ContentCategory enum values."""
        assert ContentCategory.MOVIE.value == "movie"
        assert ContentCategory.TV_SHOW.value == "tv_show"
        assert ContentCategory.ANIME.value == "anime"
        assert ContentCategory.DOCUMENTARY.value == "documentary"


# =============================================================================
# Exception Tests
# =============================================================================


class TestExceptions:
    """Tests for custom exceptions."""

    def test_rutracker_error(self):
        """Test base RutrackerError."""
        error = RutrackerError("Test error")
        assert str(error) == "Test error"
        assert isinstance(error, Exception)

    def test_blocked_error_is_rutracker_error(self):
        """Test that RutrackerBlockedError inherits from RutrackerError."""
        error = RutrackerBlockedError("Blocked")
        assert isinstance(error, RutrackerError)

    def test_captcha_error_is_rutracker_error(self):
        """Test that RutrackerCaptchaError inherits from RutrackerError."""
        error = RutrackerCaptchaError("Captcha")
        assert isinstance(error, RutrackerError)

    def test_auth_error_is_rutracker_error(self):
        """Test that RutrackerAuthError inherits from RutrackerError."""
        error = RutrackerAuthError("Auth failed")
        assert isinstance(error, RutrackerError)

    def test_parse_error_is_rutracker_error(self):
        """Test that RutrackerParseError inherits from RutrackerError."""
        error = RutrackerParseError("Parse failed")
        assert isinstance(error, RutrackerError)
