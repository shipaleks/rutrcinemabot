"""Tests for Rutracker search functionality."""

from unittest.mock import MagicMock, patch

import pytest

from src.search.rutracker import (
    ContentCategory,
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
        """Test 2160p quality detection."""
        assert detect_quality("Movie 2160p Remux") == "2160p"

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
# Enum Tests
# =============================================================================


class TestEnums:
    """Tests for quality and category enums."""

    def test_video_quality_values(self):
        """Test VideoQuality enum values."""
        assert VideoQuality.Q_720P.value == "720p"
        assert VideoQuality.Q_1080P.value == "1080p"
        assert VideoQuality.Q_4K.value == "4K"
        assert VideoQuality.Q_2160P.value == "2160p"
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

    def test_parse_error_is_rutracker_error(self):
        """Test that RutrackerParseError inherits from RutrackerError."""
        error = RutrackerParseError("Parse failed")
        assert isinstance(error, RutrackerError)
