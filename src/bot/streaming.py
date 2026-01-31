"""Streaming message updates for Telegram bot.

This module provides functionality to stream AI-generated responses to users
by progressively updating Telegram messages as content is generated.
"""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator

import structlog
from telegram import Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)


def _markdown_to_telegram_html(text: str) -> str:
    """Convert standard markdown to Telegram-safe HTML.

    Handles: links, bold, italic, inline code, code blocks.
    """
    # Fenced code blocks ```...``` → <pre>...</pre>
    text = re.sub(
        r"```(?:\w*)\n?(.*?)```",
        lambda m: f"<pre>{_escape_html(m.group(1))}</pre>",
        text,
        flags=re.DOTALL,
    )

    # Inline code `...` → <code>...</code>
    text = re.sub(r"`([^`]+?)`", lambda m: f"<code>{_escape_html(m.group(1))}</code>", text)

    # Links [text](url) → <a href="url">text</a>
    text = re.sub(r"\[([^\]]+?)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)

    # Bold **text** or __text__ → <b>text</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # Italic _text_ (but not inside URLs or HTML tags)
    text = re.sub(r"(?<![<\w/])_([^_]+?)_(?![>\w])", r"<i>\1</i>", text)

    # Italic *text* (single asterisk, not double)
    return re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", text)


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class StreamingMessageHandler:
    """Handles streaming message updates for progressive content delivery.

    This class manages the process of sending and updating Telegram messages
    as content is being generated, providing a better user experience for
    long-running AI responses.
    """

    def __init__(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        update_interval: float = 0.5,
        min_update_length: int = 20,
    ):
        """Initialize the streaming message handler.

        Args:
            update: Telegram update object
            context: Callback context
            update_interval: Minimum seconds between message updates
            min_update_length: Minimum characters before first update
        """
        self.update = update
        self.context = context
        self.update_interval = update_interval
        self.min_update_length = min_update_length
        self.message: Message | None = None
        self.last_update_time = 0.0
        self.accumulated_text = ""
        self.last_sent_text = ""
        self.is_typing = False
        self._typing_task: asyncio.Task | None = None

    async def start_typing(self) -> None:
        """Start showing typing indicator to the user.

        Telegram requires typing indicators to be re-sent every 5 seconds.
        """
        if self.is_typing or not self.update.effective_chat:
            return

        self.is_typing = True
        self._typing_task = asyncio.create_task(self._typing_loop())
        logger.debug(
            "typing_started",
            chat_id=self.update.effective_chat.id,
        )

    async def stop_typing(self) -> None:
        """Stop showing typing indicator."""
        self.is_typing = False
        if self._typing_task:
            self._typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._typing_task
            self._typing_task = None

        if self.update.effective_chat:
            logger.debug(
                "typing_stopped",
                chat_id=self.update.effective_chat.id,
            )

    async def _typing_loop(self) -> None:
        """Internal loop to keep typing indicator active."""
        try:
            while self.is_typing and self.update.effective_chat:
                await self.update.effective_chat.send_action(ChatAction.TYPING)
                await asyncio.sleep(4)  # Re-send every 4 seconds (expires in 5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("typing_loop_error", error=str(e))

    async def send_initial_message(self, text: str = "...") -> Message:
        """Send the initial message that will be updated.

        Args:
            text: Initial placeholder text

        Returns:
            The sent message object
        """
        if not self.update.message:
            raise ValueError("Update has no message")

        self.message = await self.update.message.reply_text(text)
        self.last_sent_text = text

        if self.update.effective_chat:
            logger.info(
                "initial_message_sent",
                chat_id=self.update.effective_chat.id,
                message_id=self.message.message_id,
            )

        return self.message

    async def update_message(self, text: str, force: bool = False) -> bool:
        """Update the message with new content.

        Args:
            text: New message text
            force: Force update even if interval hasn't elapsed

        Returns:
            True if message was updated, False otherwise
        """
        if not self.message:
            return False

        # Don't update if text hasn't changed
        if text == self.last_sent_text:
            return False

        # Check if enough time has passed since last update
        current_time = asyncio.get_event_loop().time()
        if not force and (current_time - self.last_update_time) < self.update_interval:
            return False

        try:
            await self.message.edit_text(
                _markdown_to_telegram_html(text),
                parse_mode=ParseMode.HTML,
            )
            self.last_sent_text = text
            self.last_update_time = current_time

            if self.update.effective_chat:
                logger.debug(
                    "message_updated",
                    chat_id=self.update.effective_chat.id,
                    message_id=self.message.message_id,
                    text_length=len(text),
                )

            return True

        except BadRequest as e:
            # Message is identical or other BadRequest error
            if "not modified" in str(e).lower():
                return False
            # Markdown parse error — retry without formatting
            if "can't parse" in str(e).lower():
                try:
                    await self.message.edit_text(text)
                    self.last_sent_text = text
                    self.last_update_time = current_time
                    return True
                except Exception:
                    pass
            logger.warning(
                "message_update_failed",
                error=str(e),
                message_id=self.message.message_id,
            )
            return False

        except RetryAfter as e:
            # Rate limited, wait and try again
            logger.warning(
                "rate_limited",
                retry_after=e.retry_after,
                message_id=self.message.message_id,
            )
            await asyncio.sleep(e.retry_after)
            return await self.update_message(text, force=True)

        except TimedOut:
            # Network timeout, don't fail the whole operation
            logger.warning(
                "update_timeout",
                message_id=self.message.message_id,
            )
            return False

        except Exception as e:
            logger.exception(
                "unexpected_update_error",
                error=str(e),
                message_id=self.message.message_id,
            )
            return False

    async def finalize_message(self, text: str) -> bool:
        """Send the final version of the message.

        Args:
            text: Final message text

        Returns:
            True if message was updated, False otherwise
        """
        # Always update with final text
        result = await self.update_message(text, force=True)

        if self.update.effective_chat:
            logger.info(
                "message_finalized",
                chat_id=self.update.effective_chat.id,
                message_id=self.message.message_id if self.message else None,
                final_length=len(text),
            )

        return result

    async def stream_text(
        self,
        text_iterator: AsyncIterator[str],
        initial_text: str = "Думаю...",
    ) -> str:
        """Stream text from an async iterator to the Telegram message.

        This is the main entry point for streaming responses. It handles:
        - Starting typing indicator
        - Sending initial message
        - Accumulating and updating text progressively
        - Finalizing with complete text
        - Error recovery

        Args:
            text_iterator: Async iterator yielding text chunks
            initial_text: Initial placeholder text

        Returns:
            The complete accumulated text
        """
        try:
            # Start typing indicator
            await self.start_typing()

            # Send initial message
            await self.send_initial_message(initial_text)

            # Accumulate and stream text
            async for chunk in text_iterator:
                self.accumulated_text += chunk

                # Update message if enough text accumulated and interval passed
                if len(self.accumulated_text) >= self.min_update_length:
                    await self.update_message(self.accumulated_text)

            # Stop typing and send final message
            await self.stop_typing()
            await self.finalize_message(self.accumulated_text)

            return self.accumulated_text

        except Exception as e:
            logger.exception(
                "stream_error",
                error=str(e),
                accumulated_length=len(self.accumulated_text),
            )

            # Stop typing on error
            await self.stop_typing()

            # Try to send what we have accumulated
            if self.accumulated_text:
                try:
                    if self.message:
                        await self.finalize_message(self.accumulated_text)
                    elif self.update.message:
                        await self.update.message.reply_text(self.accumulated_text)
                except Exception as fallback_error:
                    logger.error(
                        "fallback_send_failed",
                        error=str(fallback_error),
                    )

            # Re-raise the original error
            raise


async def send_streaming_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text_iterator: AsyncIterator[str],
    initial_text: str = "Думаю...",
) -> str:
    """Convenience function to send a streaming message.

    Args:
        update: Telegram update object
        context: Callback context
        text_iterator: Async iterator yielding text chunks
        initial_text: Initial placeholder text

    Returns:
        The complete accumulated text

    Example:
        >>> async def generate_text():
        ...     for chunk in ["Hello ", "world", "!"]:
        ...         yield chunk
        ...         await asyncio.sleep(0.1)
        >>>
        >>> await send_streaming_message(update, context, generate_text())
    """
    handler = StreamingMessageHandler(update, context)
    return await handler.stream_text(text_iterator, initial_text)
