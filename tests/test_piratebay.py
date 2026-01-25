"""Tests for PirateBay search module.

This module tests the PirateBay torrent search functionality including
HTML parsing, magnet link extraction, quality detection, and error handling.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.search.piratebay import (
    PIRATEBAY_MIRRORS,
    PirateBayClient,
    PirateBayResult,
    PirateBayUnavailableError,
    build_magnet_link,
    detect_quality,
    extract_magnet_link,
    parse_size,
    search_piratebay,
    search_with_fallback,
)

# =============================================================================
# Sample HTML Fixtures
# =============================================================================

SAMPLE_SEARCH_HTML = """
<!DOCTYPE html>
<html>
<head><title>PirateBay Search</title></head>
<body>
<table id="searchResult">
    <thead>
        <tr>
            <th>Type</th>
            <th>Name</th>
            <th>Uploaded</th>
            <th>Size</th>
            <th>Seeders</th>
            <th>Leechers</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td class="vertTh">
                <a href="/browse/207">Video (HD - Movies)</a>
            </td>
            <td>
                <div class="detName">
                    <a href="/torrent/123456" class="detLink">Dune.2021.1080p.BluRay.x264</a>
                </div>
                <a href="magnet:?xt=urn:btih:ABCD1234567890ABCD1234567890ABCD12345678&dn=Dune.2021.1080p.BluRay.x264" title="Download this torrent using magnet">
                    <img src="/static/img/icon-magnet.gif" alt="Magnet link">
                </a>
                <font class="detDesc">
                    Uploaded 01-15, Size 4.37 GiB, ULed by trusted_uploader
                </font>
            </td>
            <td>01-15</td>
            <td>4.37 GiB</td>
            <td>1500</td>
            <td>200</td>
        </tr>
        <tr>
            <td class="vertTh">
                <a href="/browse/201">Video (Movies)</a>
            </td>
            <td>
                <div class="detName">
                    <a href="/torrent/789012" class="detLink">Dune.2021.720p.WEB-DL.x264</a>
                </div>
                <a href="magnet:?xt=urn:btih:EFGH5678901234EFGH5678901234EFGH56789012&dn=Dune.2021.720p.WEB-DL.x264">
                    <img src="/static/img/icon-magnet.gif">
                </a>
                <font class="detDesc">
                    Uploaded 01-10, Size 2.1 GiB, ULed by another_uploader
                </font>
            </td>
            <td>01-10</td>
            <td>2.1 GiB</td>
            <td>800</td>
            <td>50</td>
        </tr>
        <tr>
            <td class="vertTh">
                <a href="/browse/207">Video (HD - Movies)</a>
            </td>
            <td>
                <div class="detName">
                    <a href="/torrent/345678" class="detLink">Dune.2021.2160p.4K.UHD.HDR.x265</a>
                </div>
                <a href="magnet:?xt=urn:btih:IJKL9012345678IJKL9012345678IJKL90123456&dn=Dune.2021.2160p.4K.UHD.HDR.x265">
                    <img src="/static/img/icon-magnet.gif">
                </a>
                <font class="detDesc">
                    Uploaded 01-20, Size 15.2 GiB, ULed by premium_uploader
                </font>
            </td>
            <td>01-20</td>
            <td>15.2 GiB</td>
            <td>300</td>
            <td>100</td>
        </tr>
    </tbody>
</table>
</body>
</html>
"""

SAMPLE_EMPTY_HTML = """
<!DOCTYPE html>
<html>
<head><title>PirateBay Search</title></head>
<body>
<table id="searchResult">
    <thead>
        <tr><th>Type</th><th>Name</th></tr>
    </thead>
    <tbody>
    </tbody>
</table>
<div>No hits. Try adding an asterisk (*) to your search</div>
</body>
</html>
"""

SAMPLE_CLOUDFLARE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Just a moment...</title></head>
<body>
<div class="cf-browser-verification">
    Checking your browser before accessing Cloudflare challenge page.
</div>
</body>
</html>
"""


# =============================================================================
# Tests for Helper Functions
# =============================================================================


class TestDetectQuality:
    """Tests for detect_quality function."""

    def test_detect_720p(self):
        assert detect_quality("Movie.2021.720p.BluRay") == "720p"
        assert detect_quality("Movie.720i.HDTV") == "720p"
        assert detect_quality("Movie HD 720") == "720p"

    def test_detect_1080p(self):
        assert detect_quality("Movie.2021.1080p.BluRay") == "1080p"
        assert detect_quality("Movie.1080i.HDTV") == "1080p"
        assert detect_quality("Movie.Full-HD.x264") == "1080p"
        assert detect_quality("Movie.FHD.WEB-DL") == "1080p"

    def test_detect_4k(self):
        assert detect_quality("Movie.2021.4K.UHD") == "4K"
        assert detect_quality("Movie.UHD.2160p") in ("4K", "2160p")
        assert detect_quality("Movie.Ultra-HD.x265") == "4K"

    def test_detect_2160p(self):
        # 2160p is now unified with 4K
        assert detect_quality("Movie.2160p.BluRay") == "4K"

    def test_detect_hdr(self):
        assert detect_quality("Movie.2021.HDR.x265") == "HDR"
        assert detect_quality("Movie.HDR10.Plus") == "HDR"
        assert detect_quality("Movie.Dolby-Vision.x265") == "HDR"

    def test_no_quality_detected(self):
        assert detect_quality("Movie.2021.CAM") is None
        assert detect_quality("Movie.DVDRip") is None
        assert detect_quality("Random Title") is None


class TestParseSize:
    """Tests for parse_size function."""

    def test_parse_gb(self):
        size, size_bytes = parse_size("4.37 GB")
        assert size == "4.37 GB"
        assert size_bytes == pytest.approx(4.37 * 1024**3, rel=0.01)

    def test_parse_gib(self):
        size, size_bytes = parse_size("4.37 GiB")
        assert size == "4.37 GiB"
        assert size_bytes == pytest.approx(4.37 * 1024**3, rel=0.01)

    def test_parse_mb(self):
        size, size_bytes = parse_size("700 MB")
        assert size == "700 MB"
        assert size_bytes == 700 * 1024**2

    def test_parse_tb(self):
        size, size_bytes = parse_size("1.5 TB")
        assert size == "1.5 TB"
        assert size_bytes == pytest.approx(1.5 * 1024**4, rel=0.01)

    def test_parse_with_comma(self):
        size, size_bytes = parse_size("4,37 GB")
        assert size_bytes == pytest.approx(4.37 * 1024**3, rel=0.01)

    def test_parse_invalid(self):
        size, size_bytes = parse_size("N/A")
        assert size == "N/A"
        assert size_bytes == 0


class TestBuildMagnetLink:
    """Tests for build_magnet_link function."""

    def test_basic_magnet(self):
        magnet = build_magnet_link("ABCD1234567890ABCD1234567890ABCD12345678")
        assert magnet.startswith("magnet:?xt=urn:btih:")
        assert "ABCD1234567890ABCD1234567890ABCD12345678" in magnet
        assert "&tr=" in magnet  # Has trackers

    def test_magnet_with_name(self):
        magnet = build_magnet_link("ABCD1234567890ABCD1234567890ABCD12345678", "Test Movie 2021")
        assert "&dn=" in magnet
        assert "Test" in magnet or "Test%20" in magnet


class TestExtractMagnetLink:
    """Tests for extract_magnet_link function."""

    def test_extract_from_anchor(self):
        from bs4 import BeautifulSoup

        html = '<div><a href="magnet:?xt=urn:btih:ABCD1234">Magnet</a></div>'
        soup = BeautifulSoup(html, "lxml")
        element = soup.select_one("div")
        assert element is not None
        magnet = extract_magnet_link(element)
        assert magnet.startswith("magnet:")

    def test_no_magnet_found(self):
        from bs4 import BeautifulSoup

        html = '<div><a href="http://example.com">Link</a></div>'
        soup = BeautifulSoup(html, "lxml")
        element = soup.select_one("div")
        assert element is not None
        magnet = extract_magnet_link(element)
        assert magnet == ""


# =============================================================================
# Tests for PirateBayResult Model
# =============================================================================


class TestPirateBayResult:
    """Tests for PirateBayResult Pydantic model."""

    def test_create_result(self):
        result = PirateBayResult(
            title="Test Movie 2021",
            size="4.5 GiB",
            size_bytes=4831838208,
            seeds=100,
            leeches=20,
            magnet="magnet:?xt=urn:btih:ABC123",
            quality="1080p",
        )
        assert result.title == "Test Movie 2021"
        assert result.seeds == 100
        assert result.quality == "1080p"

    def test_to_display_string(self):
        result = PirateBayResult(
            title="Test Movie",
            size="4.5 GiB",
            seeds=100,
            quality="1080p",
        )
        display = result.to_display_string()
        assert "Test Movie" in display
        assert "1080p" in display
        assert "4.5 GiB" in display

    def test_to_display_string_no_quality(self):
        result = PirateBayResult(
            title="Test Movie",
            size="4.5 GiB",
            seeds=0,
        )
        display = result.to_display_string()
        assert "Test Movie" in display
        assert "S:?" in display


# =============================================================================
# Tests for PirateBayClient
# =============================================================================


class TestPirateBayClient:
    """Tests for PirateBayClient class."""

    def test_init_default(self):
        client = PirateBayClient()
        assert "thepiratebay" in client.base_url
        assert client.timeout == 30.0
        assert client._client is None

    def test_init_custom_url(self):
        client = PirateBayClient(base_url="https://custom-mirror.com/")
        assert client.base_url == "https://custom-mirror.com"  # Trailing slash removed

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with PirateBayClient() as client:
            assert client._client is not None
        assert client._client is None

    def test_client_property_not_initialized(self):
        client = PirateBayClient()
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = client.client

    @pytest.mark.asyncio
    async def test_parse_search_results(self):
        client = PirateBayClient()
        results = client._parse_search_results(SAMPLE_SEARCH_HTML)
        assert len(results) == 3
        assert results[0].title == "Dune.2021.1080p.BluRay.x264"
        assert results[0].quality == "1080p"
        assert results[0].magnet.startswith("magnet:")

    @pytest.mark.asyncio
    async def test_parse_empty_results(self):
        client = PirateBayClient()
        results = client._parse_search_results(SAMPLE_EMPTY_HTML)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_with_mock(self):
        with patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with PirateBayClient() as client:
                results = await client.search("Dune 2021")

            assert len(results) == 3
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_with_min_seeds_filter(self):
        with patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with PirateBayClient() as client:
                results = await client.search("Dune 2021", min_seeds=1000)

            # Only the first result has >= 1000 seeds
            assert len(results) == 1
            assert results[0].seeds >= 1000

    @pytest.mark.asyncio
    async def test_search_sorted_by_seeds(self):
        with patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with PirateBayClient() as client:
                results = await client.search("Dune 2021")

            # Results should be sorted by seeds descending
            for i in range(len(results) - 1):
                assert results[i].seeds >= results[i + 1].seeds


# =============================================================================
# Tests for Error Handling
# =============================================================================


class TestErrorHandling:
    """Tests for error handling.

    Note: Since the client now uses API first, we need to mock _search_api
    to fail first, then _fetch_page also fails with the specific error.
    """

    @pytest.mark.asyncio
    async def test_cloudflare_protection(self):
        with (
            patch.object(
                PirateBayClient,
                "_search_api",
                new_callable=AsyncMock,
                side_effect=PirateBayUnavailableError("API unavailable"),
            ),
            patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_fetch.side_effect = PirateBayUnavailableError("Cloudflare protection")

            async with PirateBayClient() as client:
                with pytest.raises(PirateBayUnavailableError, match="Cloudflare"):
                    await client.search("test")

    @pytest.mark.asyncio
    async def test_connection_error(self):
        with (
            patch.object(
                PirateBayClient,
                "_search_api",
                new_callable=AsyncMock,
                side_effect=PirateBayUnavailableError("API unavailable"),
            ),
            patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_fetch.side_effect = PirateBayUnavailableError("Cannot connect")

            async with PirateBayClient() as client:
                with pytest.raises(PirateBayUnavailableError):
                    await client.search("test")

    @pytest.mark.asyncio
    async def test_api_error_returns_fallback(self):
        """Test that API errors fall back to HTML scraping."""
        with (
            patch.object(
                PirateBayClient,
                "_search_api",
                new_callable=AsyncMock,
                side_effect=PirateBayUnavailableError("API unavailable"),
            ),
            patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with PirateBayClient() as client:
                results = await client.search("test")

            # Should have fallen back to HTML and got results
            assert len(results) > 0


# =============================================================================
# Tests for Convenience Functions
# =============================================================================


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    @pytest.mark.asyncio
    async def test_search_piratebay(self):
        with patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            results = await search_piratebay("Dune 2021")

            assert len(results) == 3
            assert all(isinstance(r, PirateBayResult) for r in results)

    @pytest.mark.asyncio
    async def test_search_with_fallback_first_success(self):
        with patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            results = await search_with_fallback("Dune 2021")

            assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_with_fallback_retry_mirrors(self):
        call_count = 0

        async def mock_fetch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise PirateBayUnavailableError(f"Mirror {call_count} unavailable")
            return SAMPLE_SEARCH_HTML

        with patch.object(PirateBayClient, "_fetch_page", side_effect=mock_fetch):
            results = await search_with_fallback("Dune 2021")

            assert len(results) == 3
            # Should have tried multiple mirrors
            assert call_count >= 3

    @pytest.mark.asyncio
    async def test_search_with_fallback_all_fail(self):
        async def mock_fetch(*args, **kwargs):
            raise PirateBayUnavailableError("Mirror unavailable")

        with (
            patch.object(PirateBayClient, "_fetch_page", side_effect=mock_fetch),
            pytest.raises(PirateBayUnavailableError, match="All PirateBay mirrors"),
        ):
            await search_with_fallback("Dune 2021")


# =============================================================================
# Tests for Category Filtering
# =============================================================================


class TestCategoryFiltering:
    """Tests for category filtering.

    Note: Since the client now uses API first (which doesn't support categories),
    we mock _search_api to fail so it falls back to HTML scraping with categories.
    """

    @pytest.mark.asyncio
    async def test_video_category(self):
        with (
            patch.object(
                PirateBayClient,
                "_search_api",
                new_callable=AsyncMock,
                side_effect=PirateBayUnavailableError("API unavailable"),
            ),
            patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with PirateBayClient() as client:
                await client.search("test", category="video")

            # Check URL was constructed with video category (200)
            call_args = mock_fetch.call_args
            assert "/200" in call_args[0][0] or "200" in str(call_args)

    @pytest.mark.asyncio
    async def test_movies_category(self):
        with (
            patch.object(
                PirateBayClient,
                "_search_api",
                new_callable=AsyncMock,
                side_effect=PirateBayUnavailableError("API unavailable"),
            ),
            patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with PirateBayClient() as client:
                await client.search("test", category="movies")

            # Check URL was constructed with movies category (201)
            call_args = mock_fetch.call_args
            assert "/201" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_tv_category(self):
        with (
            patch.object(
                PirateBayClient,
                "_search_api",
                new_callable=AsyncMock,
                side_effect=PirateBayUnavailableError("API unavailable"),
            ),
            patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with PirateBayClient() as client:
                await client.search("test", category="tv")

            # Check URL was constructed with TV category (205)
            call_args = mock_fetch.call_args
            assert "/205" in call_args[0][0]


# =============================================================================
# Tests for Magnet Link Validation
# =============================================================================


class TestMagnetLinkValidation:
    """Tests for magnet link parsing and validation."""

    @pytest.mark.asyncio
    async def test_magnet_links_are_valid(self):
        with patch.object(PirateBayClient, "_fetch_page", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_SEARCH_HTML

            async with PirateBayClient() as client:
                results = await client.search("Dune 2021")

            for result in results:
                if result.magnet:
                    assert result.magnet.startswith("magnet:?xt=urn:btih:")

    def test_magnet_contains_info_hash(self):
        magnet = build_magnet_link("ABCD1234567890ABCD1234567890ABCD12345678", "Test")
        assert "ABCD1234567890ABCD1234567890ABCD12345678" in magnet

    def test_magnet_contains_trackers(self):
        magnet = build_magnet_link("ABCD1234567890ABCD1234567890ABCD12345678")
        assert "&tr=" in magnet
        assert "opentrackr" in magnet or "tracker" in magnet.lower()


# =============================================================================
# Tests for Constants
# =============================================================================


class TestConstants:
    """Tests for module constants."""

    def test_mirrors_list_not_empty(self):
        assert len(PIRATEBAY_MIRRORS) > 0

    def test_mirrors_are_valid_urls(self):
        for mirror in PIRATEBAY_MIRRORS:
            assert mirror.startswith("https://")
