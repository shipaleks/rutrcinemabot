"""Tests for user onboarding flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User
from telegram.ext import ContextTypes

from src.bot.onboarding import (
    WELCOME_MESSAGE,
    get_quality_keyboard,
    get_settings_keyboard,
    get_welcome_keyboard,
    onboarding_callback_handler,
    onboarding_start_handler,
    settings_callback_handler,
    settings_handler,
)

# =============================================================================
# Keyboard Tests
# =============================================================================


class TestKeyboards:
    """Test inline keyboard generation."""

    def test_get_welcome_keyboard(self):
        """Test welcome keyboard has setup and skip buttons."""
        keyboard = get_welcome_keyboard()
        assert isinstance(keyboard, InlineKeyboardMarkup)
        assert len(keyboard.inline_keyboard) == 2
        # First row: setup button
        assert keyboard.inline_keyboard[0][0].callback_data == "onboard_setup"
        # Second row: skip button
        assert keyboard.inline_keyboard[1][0].callback_data == "onboard_skip"

    def test_get_quality_keyboard(self):
        """Test quality selection keyboard has all options."""
        keyboard = get_quality_keyboard()
        assert isinstance(keyboard, InlineKeyboardMarkup)
        # First row: quality options
        quality_row = keyboard.inline_keyboard[0]
        callback_datas = [btn.callback_data for btn in quality_row]
        assert "onboard_quality_720p" in callback_datas
        assert "onboard_quality_1080p" in callback_datas
        assert "onboard_quality_4K" in callback_datas
        # Second row: back button
        assert keyboard.inline_keyboard[1][0].callback_data == "onboard_back_welcome"

    def test_get_settings_keyboard(self):
        """Test settings keyboard displays current values."""
        keyboard = get_settings_keyboard("4K", "en")
        assert isinstance(keyboard, InlineKeyboardMarkup)
        # Check that quality is displayed
        quality_btn = keyboard.inline_keyboard[0][0]
        assert "4K" in quality_btn.text
        # Check that audio is displayed
        audio_btn = keyboard.inline_keyboard[1][0]
        assert "English" in audio_btn.text


# =============================================================================
# Message Template Tests
# =============================================================================


class TestMessageTemplates:
    """Test message templates."""

    def test_welcome_message_format(self):
        """Test welcome message can be formatted."""
        message = WELCOME_MESSAGE.format(name="Test")
        assert "Test" in message
        assert "Media Concierge Bot" in message



# =============================================================================
# Handler Tests
# =============================================================================


@pytest.fixture
def mock_user():
    """Create a mock Telegram user."""
    user = MagicMock(spec=User)
    user.id = 12345
    user.username = "testuser"
    user.first_name = "Test"
    user.last_name = "User"
    user.language_code = "ru"
    return user


@pytest.fixture
def mock_message(mock_user):
    """Create a mock Telegram message."""
    message = MagicMock(spec=Message)
    message.from_user = mock_user
    message.chat = MagicMock(spec=Chat)
    message.chat.id = 12345
    message.reply_text = AsyncMock()
    return message


@pytest.fixture
def mock_update(mock_user, mock_message):
    """Create a mock Telegram update."""
    update = MagicMock(spec=Update)
    update.effective_user = mock_user
    update.message = mock_message
    return update


@pytest.fixture
def mock_context():
    """Create a mock context."""
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.user_data = {}
    return context


@pytest.fixture
def mock_callback_query(mock_user):
    """Create a mock callback query."""
    query = MagicMock(spec=CallbackQuery)
    query.from_user = mock_user
    query.data = "onboard_setup_start"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.delete_message = AsyncMock()
    return query


class TestOnboardingStartHandler:
    """Test /start command handler."""

    @pytest.mark.asyncio
    async def test_start_sends_welcome_message(self, mock_update, mock_context):
        """Test that /start sends welcome message with buttons."""
        with patch("src.bot.onboarding.UserStorage") as mock_storage:
            mock_storage_instance = MagicMock()
            mock_storage_instance.__aenter__ = AsyncMock(return_value=mock_storage_instance)
            mock_storage_instance.__aexit__ = AsyncMock()
            mock_storage_instance.get_or_create_user = AsyncMock(
                return_value=(MagicMock(id=1), True)
            )
            mock_storage.return_value = mock_storage_instance

            await onboarding_start_handler(mock_update, mock_context)

            # Check that reply_text was called
            mock_update.message.reply_text.assert_called_once()
            call_args = mock_update.message.reply_text.call_args
            assert "Test" in call_args[0][0] or call_args.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_start_creates_user_profile(self, mock_update, mock_context):
        """Test that /start creates user profile in database."""
        with patch("src.bot.onboarding.UserStorage") as mock_storage:
            mock_storage_instance = MagicMock()
            mock_storage_instance.__aenter__ = AsyncMock(return_value=mock_storage_instance)
            mock_storage_instance.__aexit__ = AsyncMock()
            mock_storage_instance.get_or_create_user = AsyncMock(
                return_value=(MagicMock(id=1), True)
            )
            mock_storage.return_value = mock_storage_instance

            await onboarding_start_handler(mock_update, mock_context)

            # Check that get_or_create_user was called
            mock_storage_instance.get_or_create_user.assert_called_once_with(
                telegram_id=12345,
                username="testuser",
                first_name="Test",
                last_name="User",
                language_code="ru",
            )

    @pytest.mark.asyncio
    async def test_start_handles_storage_error(self, mock_update, mock_context):
        """Test that /start handles storage errors gracefully."""
        with patch("src.bot.onboarding.UserStorage") as mock_storage:
            mock_storage.return_value.__aenter__ = AsyncMock(side_effect=Exception("DB error"))

            # Should not raise, should still send welcome message
            await onboarding_start_handler(mock_update, mock_context)
            mock_update.message.reply_text.assert_called()


class TestSettingsHandler:
    """Test /settings command handler."""

    @pytest.mark.asyncio
    async def test_settings_sends_settings_message(self, mock_update, mock_context):
        """Test that /settings sends settings message with buttons."""
        with patch("src.bot.onboarding.UserStorage") as mock_storage:
            mock_storage_instance = MagicMock()
            mock_storage_instance.__aenter__ = AsyncMock(return_value=mock_storage_instance)
            mock_storage_instance.__aexit__ = AsyncMock()
            mock_storage_instance.get_user_by_telegram_id = AsyncMock(return_value=MagicMock(id=1))
            mock_storage_instance.get_preferences = AsyncMock(
                return_value=MagicMock(video_quality="1080p", audio_language="ru")
            )
            mock_storage.return_value = mock_storage_instance

            await settings_handler(mock_update, mock_context)

            mock_update.message.reply_text.assert_called_once()


class TestOnboardingCallbackHandler:
    """Test callback query handlers for onboarding."""

    @pytest.mark.asyncio
    async def test_setup_start_callback(self, mock_update, mock_callback_query, mock_context):
        """Test setup start callback shows quality selection."""
        mock_update.callback_query = mock_callback_query
        mock_callback_query.data = "onboard_setup_start"

        await onboarding_callback_handler(mock_update, mock_context)

        mock_callback_query.answer.assert_called_once()
        mock_callback_query.edit_message_text.assert_called_once()
        call_args = mock_callback_query.edit_message_text.call_args
        assert "качество" in call_args[0][0].lower() or "качество" in str(call_args)

    @pytest.mark.asyncio
    async def test_skip_callback(self, mock_update, mock_callback_query, mock_context):
        """Test skip callback shows default settings message."""
        mock_update.callback_query = mock_callback_query
        mock_callback_query.data = "onboard_skip"

        await onboarding_callback_handler(mock_update, mock_context)

        mock_callback_query.answer.assert_called_once()
        mock_callback_query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_quality_selection_callback(self, mock_update, mock_callback_query, mock_context):
        """Test quality selection stores value and shows audio selection."""
        mock_update.callback_query = mock_callback_query
        mock_callback_query.data = "onboard_quality_1080p"

        await onboarding_callback_handler(mock_update, mock_context)

        assert mock_context.user_data.get("selected_quality") == "1080p"
        mock_callback_query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_audio_selection_callback(self, mock_update, mock_callback_query, mock_context):
        """Test audio selection stores value and shows genre selection."""
        mock_update.callback_query = mock_callback_query
        mock_callback_query.data = "onboard_audio_ru"

        await onboarding_callback_handler(mock_update, mock_context)

        assert mock_context.user_data.get("selected_audio") == "ru"
        mock_callback_query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_genre_selection_toggles(self, mock_update, mock_callback_query, mock_context):
        """Test genre selection toggles genres."""
        mock_update.callback_query = mock_callback_query
        mock_callback_query.data = "onboard_genre_scifi"
        mock_context.user_data["selected_genres"] = []

        await onboarding_callback_handler(mock_update, mock_context)

        assert "scifi" in mock_context.user_data["selected_genres"]

        # Toggle off
        await onboarding_callback_handler(mock_update, mock_context)
        assert "scifi" not in mock_context.user_data["selected_genres"]

    @pytest.mark.asyncio
    async def test_complete_saves_preferences(self, mock_update, mock_callback_query, mock_context):
        """Test complete callback saves preferences to database."""
        mock_update.callback_query = mock_callback_query
        mock_callback_query.data = "onboard_complete"
        mock_context.user_data = {
            "selected_quality": "4K",
            "selected_audio": "en",
            "selected_genres": ["scifi", "action"],
        }

        with patch("src.bot.onboarding.UserStorage") as mock_storage:
            mock_storage_instance = MagicMock()
            mock_storage_instance.__aenter__ = AsyncMock(return_value=mock_storage_instance)
            mock_storage_instance.__aexit__ = AsyncMock()
            mock_storage_instance.get_user_by_telegram_id = AsyncMock(return_value=MagicMock(id=1))
            mock_storage_instance.update_preferences = AsyncMock()
            mock_storage.return_value = mock_storage_instance

            await onboarding_callback_handler(mock_update, mock_context)

            mock_storage_instance.update_preferences.assert_called_once_with(
                user_id=1,
                video_quality="4K",
                audio_language="en",
                preferred_genres=["scifi", "action"],
            )


class TestSettingsCallbackHandler:
    """Test settings callback handlers."""

    @pytest.mark.asyncio
    async def test_settings_genre_toggle(self, mock_update, mock_callback_query, mock_context):
        """Test genre toggle in settings."""
        mock_update.callback_query = mock_callback_query
        mock_callback_query.data = "settings_genre_drama"
        mock_context.user_data["selected_genres"] = []

        await settings_callback_handler(mock_update, mock_context)

        assert "drama" in mock_context.user_data["selected_genres"]

    @pytest.mark.asyncio
    async def test_settings_save_genres(self, mock_update, mock_callback_query, mock_context):
        """Test save genres in settings."""
        mock_update.callback_query = mock_callback_query
        mock_callback_query.data = "settings_save_genres"
        mock_context.user_data = {"selected_genres": ["drama", "comedy"]}

        with patch("src.bot.onboarding.UserStorage") as mock_storage:
            mock_storage_instance = MagicMock()
            mock_storage_instance.__aenter__ = AsyncMock(return_value=mock_storage_instance)
            mock_storage_instance.__aexit__ = AsyncMock()
            mock_storage_instance.get_user_by_telegram_id = AsyncMock(return_value=MagicMock(id=1))
            mock_storage_instance.update_preferences = AsyncMock()
            mock_storage_instance.get_preferences = AsyncMock(
                return_value=MagicMock(video_quality="1080p", audio_language="ru")
            )
            mock_storage.return_value = mock_storage_instance

            await settings_callback_handler(mock_update, mock_context)

            mock_storage_instance.update_preferences.assert_called_once()


# =============================================================================
# Integration Tests
# =============================================================================


class TestOnboardingFlow:
    """Test complete onboarding flow."""

    @pytest.mark.asyncio
    async def test_full_onboarding_flow(self, mock_update, mock_callback_query, mock_context):
        """Test complete onboarding from start to finish."""
        # Start
        with patch("src.bot.onboarding.UserStorage") as mock_storage:
            mock_storage_instance = MagicMock()
            mock_storage_instance.__aenter__ = AsyncMock(return_value=mock_storage_instance)
            mock_storage_instance.__aexit__ = AsyncMock()
            mock_storage_instance.get_or_create_user = AsyncMock(
                return_value=(MagicMock(id=1), True)
            )
            mock_storage_instance.get_user_by_telegram_id = AsyncMock(return_value=MagicMock(id=1))
            mock_storage_instance.update_preferences = AsyncMock()
            mock_storage.return_value = mock_storage_instance

            # Step 1: /start
            await onboarding_start_handler(mock_update, mock_context)
            mock_update.message.reply_text.assert_called()

            # Step 2: Setup start
            mock_update.callback_query = mock_callback_query
            mock_callback_query.data = "onboard_setup_start"
            await onboarding_callback_handler(mock_update, mock_context)

            # Step 3: Quality selection
            mock_callback_query.data = "onboard_quality_4K"
            await onboarding_callback_handler(mock_update, mock_context)
            assert mock_context.user_data.get("selected_quality") == "4K"

            # Step 4: Audio selection
            mock_callback_query.data = "onboard_audio_en"
            await onboarding_callback_handler(mock_update, mock_context)
            assert mock_context.user_data.get("selected_audio") == "en"

            # Step 5: Genre selection
            mock_callback_query.data = "onboard_genre_scifi"
            await onboarding_callback_handler(mock_update, mock_context)
            assert "scifi" in mock_context.user_data.get("selected_genres", [])

            # Step 6: Complete
            mock_callback_query.data = "onboard_complete"
            await onboarding_callback_handler(mock_update, mock_context)

            # Verify preferences were saved
            mock_storage_instance.update_preferences.assert_called()
