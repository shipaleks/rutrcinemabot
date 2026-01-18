"""Tests for streaming message functionality."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from src.bot.streaming import StreamingMessageHandler, send_streaming_message


@pytest.fixture
def mock_update():
    """Create a mock Telegram update."""
    user = MagicMock(spec=User)
    user.id = 12345
    user.username = "testuser"
    user.first_name = "Test"

    chat = MagicMock(spec=Chat)
    chat.id = 67890
    chat.type = "private"

    message = MagicMock(spec=Message)
    message.message_id = 1
    message.from_user = user
    message.chat = chat
    message.reply_text = AsyncMock(return_value=message)

    update = MagicMock(spec=Update)
    update.update_id = 1
    update.message = message
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message

    return update


@pytest.fixture
def mock_context():
    """Create a mock callback context."""
    return MagicMock(spec=ContextTypes.DEFAULT_TYPE)


@pytest.mark.asyncio
async def test_send_initial_message(mock_update, mock_context):
    """Test that initial message is sent correctly."""
    handler = StreamingMessageHandler(mock_update, mock_context)

    message = await handler.send_initial_message("Test message")

    assert message is not None
    mock_update.message.reply_text.assert_called_once_with("Test message")
    assert handler.last_sent_text == "Test message"


@pytest.mark.asyncio
async def test_typing_indicator(mock_update, mock_context):
    """Test typing indicator starts and stops."""
    mock_update.effective_chat.send_action = AsyncMock()

    handler = StreamingMessageHandler(mock_update, mock_context)

    # Start typing
    await handler.start_typing()
    assert handler.is_typing is True

    # Give time for typing loop to run
    await asyncio.sleep(0.1)

    # Stop typing
    await handler.stop_typing()
    assert handler.is_typing is False

    # Verify send_action was called at least once
    mock_update.effective_chat.send_action.assert_called()


@pytest.mark.asyncio
async def test_update_message(mock_update, mock_context):
    """Test message update functionality."""
    handler = StreamingMessageHandler(mock_update, mock_context)

    # Create mock message
    mock_message = MagicMock(spec=Message)
    mock_message.message_id = 1
    mock_message.edit_text = AsyncMock()
    handler.message = mock_message

    # Update message
    result = await handler.update_message("Updated text", force=True)

    assert result is True
    mock_message.edit_text.assert_called_once()
    assert handler.last_sent_text == "Updated text"


@pytest.mark.asyncio
async def test_update_message_rate_limiting(mock_update, mock_context):
    """Test that message updates respect rate limiting."""
    handler = StreamingMessageHandler(
        mock_update,
        mock_context,
        update_interval=1.0,  # 1 second between updates
    )

    # Create mock message
    mock_message = MagicMock(spec=Message)
    mock_message.message_id = 1
    mock_message.edit_text = AsyncMock()
    handler.message = mock_message

    # First update should work
    result1 = await handler.update_message("Text 1", force=False)
    assert result1 is True

    # Immediate second update should be skipped due to rate limiting
    result2 = await handler.update_message("Text 2", force=False)
    assert result2 is False

    # Forced update should work
    result3 = await handler.update_message("Text 3", force=True)
    assert result3 is True


@pytest.mark.asyncio
async def test_stream_text_basic(mock_update, mock_context):
    """Test basic streaming text functionality."""

    async def generate_text():
        """Generate text chunks."""
        for chunk in ["Hello ", "world", "!"]:
            yield chunk
            await asyncio.sleep(0.01)

    # Mock message methods
    mock_message = MagicMock(spec=Message)
    mock_message.message_id = 1
    mock_message.edit_text = AsyncMock()
    mock_update.message.reply_text = AsyncMock(return_value=mock_message)
    mock_update.effective_chat.send_action = AsyncMock()

    handler = StreamingMessageHandler(mock_update, mock_context)
    result = await handler.stream_text(generate_text(), initial_text="Loading...")

    assert result == "Hello world!"
    assert handler.accumulated_text == "Hello world!"
    mock_update.message.reply_text.assert_called_once_with("Loading...")


@pytest.mark.asyncio
async def test_stream_text_error_handling(mock_update, mock_context):
    """Test streaming error handling."""

    async def failing_generator():
        """Generator that raises an error."""
        yield "Some text"
        raise ValueError("Test error")

    # Mock message methods
    mock_message = MagicMock(spec=Message)
    mock_message.message_id = 1
    mock_message.edit_text = AsyncMock()
    mock_update.message.reply_text = AsyncMock(return_value=mock_message)
    mock_update.effective_chat.send_action = AsyncMock()

    handler = StreamingMessageHandler(mock_update, mock_context)

    # Should raise the error but still send accumulated text
    with pytest.raises(ValueError, match="Test error"):
        await handler.stream_text(failing_generator())

    # Message should still be finalized with accumulated text
    assert handler.accumulated_text == "Some text"
    mock_message.edit_text.assert_called()


@pytest.mark.asyncio
async def test_send_streaming_message_convenience(mock_update, mock_context):
    """Test convenience function for streaming messages."""

    async def generate_text():
        """Generate text chunks."""
        for chunk in ["Test ", "streaming"]:
            yield chunk

    # Mock message methods
    mock_message = MagicMock(spec=Message)
    mock_message.message_id = 1
    mock_message.edit_text = AsyncMock()
    mock_update.message.reply_text = AsyncMock(return_value=mock_message)
    mock_update.effective_chat.send_action = AsyncMock()

    result = await send_streaming_message(
        mock_update, mock_context, generate_text(), initial_text="Thinking..."
    )

    assert result == "Test streaming"
    mock_update.message.reply_text.assert_called_once_with("Thinking...")


@pytest.mark.asyncio
async def test_finalize_message(mock_update, mock_context):
    """Test message finalization."""
    handler = StreamingMessageHandler(mock_update, mock_context)

    # Create mock message
    mock_message = MagicMock(spec=Message)
    mock_message.message_id = 1
    mock_message.edit_text = AsyncMock()
    handler.message = mock_message

    # Finalize message
    result = await handler.finalize_message("Final text")

    assert result is True
    mock_message.edit_text.assert_called()
    assert handler.last_sent_text == "Final text"


@pytest.mark.asyncio
async def test_no_duplicate_updates(mock_update, mock_context):
    """Test that identical text doesn't trigger updates."""
    handler = StreamingMessageHandler(mock_update, mock_context)

    # Create mock message
    mock_message = MagicMock(spec=Message)
    mock_message.message_id = 1
    mock_message.edit_text = AsyncMock()
    handler.message = mock_message
    handler.last_sent_text = "Same text"

    # Try to update with same text
    result = await handler.update_message("Same text", force=False)

    assert result is False
    mock_message.edit_text.assert_not_called()
