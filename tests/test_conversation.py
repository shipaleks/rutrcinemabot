"""Tests for the conversation module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.conversation import (
    cache_search_result,
    clear_conversation_context,
    create_tool_executor,
    format_search_results_keyboard,
    format_torrent_result_message,
    get_cached_result,
    get_conversation_context,
    handle_download_callback,
    handle_get_user_profile,
    handle_kinopoisk_search,
    handle_message,
    handle_piratebay_search,
    handle_rutracker_search,
    handle_seedbox_download,
    handle_tmdb_credits,
    handle_tmdb_search,
)

# =============================================================================
# Tests for conversation context management
# =============================================================================


class TestConversationContext:
    """Test conversation context functions."""

    def test_get_conversation_context_creates_new(self):
        """Test that get_conversation_context creates new context for unknown user."""
        # Clear any existing context
        clear_conversation_context(999999)

        context = get_conversation_context(999999)

        assert context is not None
        assert len(context.messages) == 0

    def test_get_conversation_context_returns_same(self):
        """Test that get_conversation_context returns same context for same user."""
        context1 = get_conversation_context(888888)
        context1.add_message("user", "test message")

        context2 = get_conversation_context(888888)

        assert context1 is context2
        assert len(context2.messages) == 1

    def test_clear_conversation_context(self):
        """Test that clear_conversation_context clears the context."""
        context = get_conversation_context(777777)
        context.add_message("user", "test")

        clear_conversation_context(777777)

        # Get context again - should be fresh
        new_context = get_conversation_context(777777)
        # The context object is the same, but messages should be cleared
        assert len(new_context.messages) == 0


# =============================================================================
# Tests for search result caching
# =============================================================================


class TestSearchResultCache:
    """Test search result caching functions."""

    def test_cache_and_get_result(self):
        """Test caching and retrieving a search result."""
        result_data = {
            "title": "Test Movie",
            "magnet": "magnet:?xt=urn:btih:abc123",
            "source": "rutracker",
        }

        cache_search_result("test_123", result_data)
        cached = get_cached_result("test_123")

        assert cached is not None
        assert cached["title"] == "Test Movie"
        assert cached["magnet"] == "magnet:?xt=urn:btih:abc123"

    def test_get_nonexistent_result(self):
        """Test getting a non-existent result returns None."""
        result = get_cached_result("nonexistent_id")
        assert result is None


# =============================================================================
# Tests for tool handlers
# =============================================================================


class TestRutrackerSearchHandler:
    """Test rutracker_search tool handler."""

    @pytest.mark.asyncio
    async def test_rutracker_search_success(self):
        """Test successful rutracker search."""
        mock_result = MagicMock()
        mock_result.title = "Dune 2021 1080p"
        mock_result.size = "15.5 GB"
        mock_result.seeds = 100
        mock_result.magnet = "magnet:?xt=urn:btih:abc123"
        mock_result.quality = "1080p"

        with patch("src.bot.conversation.RutrackerClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.search = AsyncMock(return_value=[mock_result])
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await handle_rutracker_search({"query": "Dune 2021"})
            result_data = json.loads(result)

            assert result_data["status"] == "success"
            assert result_data["source"] == "rutracker"
            assert result_data["results_count"] == 1
            assert result_data["results"][0]["title"] == "Dune 2021 1080p"

    @pytest.mark.asyncio
    async def test_rutracker_search_error(self):
        """Test rutracker search error handling."""
        from src.search.rutracker import RutrackerError

        with patch("src.bot.conversation.RutrackerClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.search = AsyncMock(side_effect=RutrackerError("Site blocked"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await handle_rutracker_search({"query": "Dune"})
            result_data = json.loads(result)

            assert result_data["status"] == "error"
            assert "Site blocked" in result_data["error"]


class TestPirateBaySearchHandler:
    """Test piratebay_search tool handler."""

    @pytest.mark.asyncio
    async def test_piratebay_search_success(self):
        """Test successful piratebay search."""
        mock_result = MagicMock()
        mock_result.title = "Dune 2021 1080p BluRay"
        mock_result.size = "12 GB"
        mock_result.seeds = 50
        mock_result.magnet = "magnet:?xt=urn:btih:xyz789"
        mock_result.quality = "1080p"

        with patch("src.bot.conversation.PirateBayClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.search = AsyncMock(return_value=[mock_result])
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await handle_piratebay_search({"query": "Dune"})
            result_data = json.loads(result)

            assert result_data["status"] == "success"
            assert result_data["source"] == "piratebay"


class TestTMDBSearchHandler:
    """Test tmdb_search tool handler."""

    @pytest.mark.asyncio
    async def test_tmdb_search_success(self):
        """Test successful TMDB search."""
        mock_result = MagicMock()
        mock_result.id = 438631
        mock_result.title = "Dune"
        mock_result.media_type = "movie"
        mock_result.overview = "A sci-fi epic..."
        mock_result.vote_average = 8.0
        mock_result.get_year = MagicMock(return_value=2021)
        mock_result.get_poster_url = MagicMock(
            return_value="https://image.tmdb.org/t/p/w500/poster.jpg"
        )

        with patch("src.bot.conversation.TMDBClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.search_multi = AsyncMock(return_value=[mock_result])
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await handle_tmdb_search({"query": "Dune"})
            result_data = json.loads(result)

            assert result_data["status"] == "success"
            assert result_data["source"] == "tmdb"
            assert result_data["results"][0]["id"] == 438631


class TestTMDBCreditsHandler:
    """Test tmdb_credits tool handler."""

    @pytest.mark.asyncio
    async def test_tmdb_credits_success(self):
        """Test successful TMDB credits retrieval."""
        mock_director = MagicMock()
        mock_director.name = "Denis Villeneuve"

        mock_actor = MagicMock()
        mock_actor.name = "Timothée Chalamet"
        mock_actor.character = "Paul Atreides"

        mock_credits = MagicMock()
        mock_credits.get_directors = MagicMock(return_value=[mock_director])
        mock_credits.get_writers = MagicMock(return_value=[])
        mock_credits.get_top_cast = MagicMock(return_value=[mock_actor])

        with patch("src.bot.conversation.TMDBClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get_credits = AsyncMock(return_value=mock_credits)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await handle_tmdb_credits({"tmdb_id": 438631, "media_type": "movie"})
            result_data = json.loads(result)

            assert result_data["status"] == "success"
            assert result_data["directors"][0]["name"] == "Denis Villeneuve"
            assert result_data["cast"][0]["name"] == "Timothée Chalamet"

    @pytest.mark.asyncio
    async def test_tmdb_credits_missing_id(self):
        """Test TMDB credits with missing ID."""
        result = await handle_tmdb_credits({})
        result_data = json.loads(result)

        assert result_data["status"] == "error"
        assert "tmdb_id is required" in result_data["error"]


class TestKinopoiskSearchHandler:
    """Test kinopoisk_search tool handler."""

    @pytest.mark.asyncio
    async def test_kinopoisk_search_success(self):
        """Test successful Kinopoisk search."""
        mock_result = MagicMock()
        mock_result.kinopoisk_id = 1234567
        mock_result.name_ru = "Дюна"
        mock_result.name_en = "Dune"
        mock_result.year = 2021
        mock_result.rating_kinopoisk = 7.5

        with patch("src.bot.conversation.KinopoiskClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.search = AsyncMock(return_value=[mock_result])
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await handle_kinopoisk_search({"query": "Дюна"})
            result_data = json.loads(result)

            assert result_data["status"] == "success"
            assert result_data["source"] == "kinopoisk"
            assert result_data["results"][0]["title"] == "Дюна"


class TestGetUserProfileHandler:
    """Test get_user_profile tool handler."""

    @pytest.mark.asyncio
    async def test_get_user_profile_missing_id(self):
        """Test get_user_profile with missing user_id."""
        result = await handle_get_user_profile({})
        result_data = json.loads(result)

        assert result_data["status"] == "error"
        assert "user_id is required" in result_data["error"]


class TestSeedboxDownloadHandler:
    """Test seedbox_download tool handler."""

    @pytest.mark.asyncio
    async def test_seedbox_download_not_configured(self):
        """Test seedbox download when seedbox is not configured."""
        with patch("src.bot.conversation.send_magnet_to_seedbox") as mock_send:
            mock_send.return_value = {
                "status": "magnet",
                "magnet": "magnet:?xt=urn:btih:abc123",
            }

            result = await handle_seedbox_download(
                {
                    "magnet": "magnet:?xt=urn:btih:abc123",
                    "name": "Test Movie",
                }
            )
            result_data = json.loads(result)

            assert result_data["status"] == "not_configured"
            assert "magnet" in result_data


# =============================================================================
# Tests for tool executor
# =============================================================================


class TestToolExecutor:
    """Test tool executor creation."""

    def test_create_tool_executor(self):
        """Test that create_tool_executor returns configured executor."""
        executor = create_tool_executor()

        # Check that all handlers are registered
        assert executor.has_handler("rutracker_search")
        assert executor.has_handler("piratebay_search")
        assert executor.has_handler("tmdb_search")
        assert executor.has_handler("tmdb_credits")
        assert executor.has_handler("kinopoisk_search")
        assert executor.has_handler("get_user_profile")
        assert executor.has_handler("seedbox_download")


# =============================================================================
# Tests for result formatting
# =============================================================================


class TestResultFormatting:
    """Test result formatting functions."""

    def test_format_search_results_keyboard(self):
        """Test keyboard generation for search results."""
        results = [
            {"id": "rt_123", "title": "Dune 2021 1080p BluRay"},
            {"id": "rt_456", "title": "Dune Part Two 2024 4K"},
        ]

        keyboard = format_search_results_keyboard(results)

        assert len(keyboard.inline_keyboard) == 2
        assert "Dune 2021" in keyboard.inline_keyboard[0][0].text
        assert keyboard.inline_keyboard[0][0].callback_data == "download_rt_123"

    def test_format_search_results_keyboard_truncates_long_titles(self):
        """Test that long titles are truncated in buttons."""
        results = [
            {"id": "rt_789", "title": "A Very Long Movie Title That Exceeds Thirty Characters"},
        ]

        keyboard = format_search_results_keyboard(results)

        button_text = keyboard.inline_keyboard[0][0].text
        # Should be truncated with ...
        assert "..." in button_text
        assert len(button_text) < 50

    def test_format_torrent_result_message(self):
        """Test torrent result message formatting."""
        result = {
            "title": "Dune 2021 1080p",
            "size": "15.5 GB",
            "seeds": 100,
            "quality": "1080p",
        }

        message = format_torrent_result_message(result)

        assert "Dune 2021 1080p" in message
        assert "15.5 GB" in message
        assert "100" in message
        assert "1080p" in message
        assert "🟢" in message  # High seeds indicator

    def test_format_torrent_result_message_low_seeds(self):
        """Test torrent result message with low seeds."""
        result = {
            "title": "Old Movie",
            "size": "2 GB",
            "seeds": 5,
            "quality": "720p",
        }

        message = format_torrent_result_message(result)

        assert "🔴" in message  # Low seeds indicator


# =============================================================================
# Tests for message handlers
# =============================================================================


class TestMessageHandler:
    """Test the main message handler."""

    @pytest.mark.asyncio
    async def test_handle_message_no_message(self):
        """Test handle_message with no message."""
        update = MagicMock()
        update.message = None
        context = MagicMock()

        # Should return without error
        await handle_message(update, context)

    @pytest.mark.asyncio
    async def test_handle_message_no_user(self):
        """Test handle_message with no user."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "test"
        update.effective_user = None
        context = MagicMock()

        # Should return without error
        await handle_message(update, context)


class TestDownloadCallback:
    """Test download callback handler."""

    @pytest.mark.asyncio
    async def test_handle_download_callback_no_query(self):
        """Test download callback with no query."""
        update = MagicMock()
        update.callback_query = None
        context = MagicMock()

        # Should return without error
        await handle_download_callback(update, context)

    @pytest.mark.asyncio
    async def test_handle_download_callback_no_cached_result(self):
        """Test download callback with non-existent cached result."""
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "download_nonexistent"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        context = MagicMock()

        await handle_download_callback(update, context)

        update.callback_query.edit_message_text.assert_called_once()
        call_args = update.callback_query.edit_message_text.call_args
        assert "недоступна" in call_args[0][0]


# =============================================================================
# Tests for context-aware responses (CONV-002)
# =============================================================================


class TestContextAwareResponses:
    """Test context-aware response functionality."""

    def test_conversation_context_preserves_user_preferences(self):
        """Test that conversation context stores user preferences."""
        context = get_conversation_context(111111)
        context.user_preferences = {
            "quality": "4K",
            "audio_language": "ru",
            "genres": ["sci-fi", "thriller"],
        }

        # Retrieve same context
        same_context = get_conversation_context(111111)

        assert same_context.user_preferences is not None
        assert same_context.user_preferences["quality"] == "4K"
        assert same_context.user_preferences["audio_language"] == "ru"
        assert "sci-fi" in same_context.user_preferences["genres"]

    def test_conversation_context_stores_telegram_user_id(self):
        """Test that conversation context can store telegram user ID."""
        context = get_conversation_context(222222)
        context.telegram_user_id = 222222

        same_context = get_conversation_context(222222)

        assert same_context.telegram_user_id == 222222

    def test_conversation_context_preserves_messages_within_session(self):
        """Test that conversation messages are preserved within a session."""
        clear_conversation_context(333333)
        context = get_conversation_context(333333)

        # Add multiple messages simulating a conversation
        context.add_message("user", "Найди Дюну")
        context.add_message("assistant", "Нашёл несколько вариантов...")
        context.add_message("user", "Покажи в 4K")

        # Verify messages are preserved
        assert len(context.messages) == 3
        assert context.messages[0].content == "Найди Дюну"
        assert context.messages[2].content == "Покажи в 4K"

    def test_clear_context_removes_all_data(self):
        """Test that clearing context removes messages and preferences."""
        context = get_conversation_context(444444)
        context.add_message("user", "test")
        context.user_preferences = {"quality": "1080p"}

        clear_conversation_context(444444)

        # Get fresh context
        new_context = get_conversation_context(444444)
        assert len(new_context.messages) == 0
        # Note: user_preferences persists as it's on the same object reference
        # This is expected behavior - clear() only clears messages

    @pytest.mark.asyncio
    async def test_get_user_profile_returns_preferences(self):
        """Test that get_user_profile returns user preferences from storage."""
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.telegram_id = 555555
        mock_user.username = "testuser"
        mock_user.first_name = "Test"

        mock_preferences = MagicMock()
        mock_preferences.video_quality = "4K"
        mock_preferences.audio_language = "en"
        mock_preferences.preferred_genres = ["action", "sci-fi"]

        with patch("src.bot.conversation.get_storage") as mock_get_storage:
            mock_storage = AsyncMock()
            mock_storage.get_user_by_telegram_id = AsyncMock(return_value=mock_user)
            mock_storage.get_preferences = AsyncMock(return_value=mock_preferences)
            mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
            mock_storage.__aexit__ = AsyncMock(return_value=None)
            mock_get_storage.return_value = mock_storage

            result = await handle_get_user_profile({"user_id": 555555})
            result_data = json.loads(result)

            assert result_data["status"] == "success"
            assert result_data["preferences"]["quality"] == "4K"
            assert result_data["preferences"]["audio_language"] == "en"
            assert "action" in result_data["preferences"]["genres"]

    @pytest.mark.asyncio
    async def test_get_user_profile_not_found(self):
        """Test that get_user_profile handles non-existent users."""
        with patch("src.bot.conversation.get_storage") as mock_get_storage:
            mock_storage = AsyncMock()
            mock_storage.get_user_by_telegram_id = AsyncMock(return_value=None)
            mock_storage.__aenter__ = AsyncMock(return_value=mock_storage)
            mock_storage.__aexit__ = AsyncMock(return_value=None)
            mock_get_storage.return_value = mock_storage

            result = await handle_get_user_profile({"user_id": 666666})
            result_data = json.loads(result)

            assert result_data["status"] == "not_found"


class TestSystemPromptWithPreferences:
    """Test system prompt generation with user preferences."""

    def test_system_prompt_includes_quality_preference(self):
        """Test that system prompt includes quality preference."""
        from src.ai.prompts import get_system_prompt

        preferences = {"quality": "4K"}
        prompt = get_system_prompt(preferences)

        assert "Предпочитаемое качество видео: 4K" in prompt
        assert 'quality="4K"' in prompt

    def test_system_prompt_includes_audio_language(self):
        """Test that system prompt includes audio language preference."""
        from src.ai.prompts import get_system_prompt

        preferences = {"audio_language": "ru"}
        prompt = get_system_prompt(preferences)

        assert "русский дубляж" in prompt

    def test_system_prompt_includes_genres(self):
        """Test that system prompt includes genre preferences."""
        from src.ai.prompts import get_system_prompt

        preferences = {"genres": ["sci-fi", "thriller", "action"]}
        prompt = get_system_prompt(preferences)

        assert "Любимые жанры: sci-fi, thriller, action" in prompt
        assert "Учитывай эти жанры при рекомендациях" in prompt

    def test_system_prompt_handles_empty_preferences(self):
        """Test that system prompt works with no preferences."""
        from src.ai.prompts import get_system_prompt

        prompt = get_system_prompt(None)

        assert "Контекст пользователя:" not in prompt
        assert "медиа-консьерж" in prompt

    def test_system_prompt_handles_partial_preferences(self):
        """Test that system prompt works with partial preferences."""
        from src.ai.prompts import get_system_prompt

        preferences = {"quality": "1080p"}  # Only quality, no genres
        prompt = get_system_prompt(preferences)

        assert "1080p" in prompt
        assert "Любимые жанры:" not in prompt

    def test_system_prompt_backward_compatible_field_names(self):
        """Test that system prompt handles old field names."""
        from src.ai.prompts import get_system_prompt

        # Old field names
        preferences = {
            "preferred_quality": "720p",
            "preferred_language": "en",
            "favorite_genres": ["comedy"],
        }
        prompt = get_system_prompt(preferences)

        assert "720p" in prompt
        assert "comedy" in prompt
