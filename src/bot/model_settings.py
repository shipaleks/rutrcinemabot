"""AI model settings management for the Telegram bot.

This module provides:
- /model command to configure AI model and thinking level
- Inline keyboard for model selection
- Persistent storage of user preferences

Available models (all 4.5 generation):
- Haiku 4.5: Fast, cheap, good for simple tasks
- Sonnet 4.5: Balanced speed/quality (default)
- Opus 4.5: Best quality, slower

Thinking levels:
- Off: Standard responses (default)
- Low: 1K thinking tokens
- Medium: 5K thinking tokens
- High: 10K thinking tokens
"""

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from src.user.storage import get_storage

logger = structlog.get_logger(__name__)

# Available Claude models (all 4.5 generation)
MODELS = {
    "haiku": {
        "id": "claude-haiku-4-5-20251001",
        "name": "Haiku 4.5",
        "description": "Быстрый, дешёвый",
        "emoji": "",
    },
    "sonnet": {
        "id": "claude-sonnet-4-5-20250929",
        "name": "Sonnet 4.5",
        "description": "Баланс скорости и качества",
        "emoji": "",
    },
    "opus": {
        "id": "claude-opus-4-5-20251101",
        "name": "Opus 4.5",
        "description": "Лучшее качество, медленнее",
        "emoji": "",
    },
}

# Thinking budget options (0 = disabled)
THINKING_LEVELS = {
    "off": {"budget": 0, "name": "Выкл", "description": "Стандартные ответы"},
    "low": {"budget": 1024, "name": "Низкий", "description": "1K токенов"},
    "medium": {"budget": 5120, "name": "Средний", "description": "5K токенов"},
    "high": {"budget": 10240, "name": "Высокий", "description": "10K токенов"},
}


def get_model_keyboard(current_model: str, current_thinking: int) -> InlineKeyboardMarkup:
    """Create model settings keyboard.

    Args:
        current_model: Current model ID.
        current_thinking: Current thinking budget.

    Returns:
        InlineKeyboardMarkup with model and thinking options.
    """
    keyboard = []

    # Model selection header
    keyboard.append([InlineKeyboardButton("-- Модель --", callback_data="model_noop")])

    # Model buttons
    model_row = []
    for key, model in MODELS.items():
        is_selected = current_model == model["id"]
        label = f"[{model['name']}]" if is_selected else model["name"]
        model_row.append(InlineKeyboardButton(label, callback_data=f"model_set_{key}"))
    keyboard.append(model_row)

    # Thinking level header
    keyboard.append([InlineKeyboardButton("-- Thinking --", callback_data="model_noop")])

    # Thinking buttons
    thinking_row = []
    for key, level in THINKING_LEVELS.items():
        is_selected = current_thinking == level["budget"]
        label = f"[{level['name']}]" if is_selected else level["name"]
        thinking_row.append(InlineKeyboardButton(label, callback_data=f"model_think_{key}"))
    keyboard.append(thinking_row)

    # Close button
    keyboard.append([InlineKeyboardButton("Закрыть", callback_data="model_close")])

    return InlineKeyboardMarkup(keyboard)


def get_status_text(model_id: str, thinking_budget: int) -> str:
    """Get status text for current settings.

    Args:
        model_id: Current model ID.
        thinking_budget: Current thinking budget.

    Returns:
        Formatted status string.
    """
    # Find model name
    model_name = "Unknown"
    model_desc = ""
    for model in MODELS.values():
        if model["id"] == model_id:
            model_name = model["name"]
            model_desc = model["description"]
            break

    # Find thinking level name
    thinking_name = "Custom"
    thinking_desc = ""
    for level in THINKING_LEVELS.values():
        if level["budget"] == thinking_budget:
            thinking_name = level["name"]
            thinking_desc = level["description"]
            break

    return (
        f"**AI Model Settings**\n\n"
        f"Модель: **{model_name}**\n"
        f"_{model_desc}_\n\n"
        f"Thinking: **{thinking_name}**\n"
        f"_{thinking_desc}_\n\n"
        f"Выберите настройки:"
    )


async def model_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /model command.

    Shows current AI model settings and options to change them.

    Args:
        update: Telegram update object.
        context: Callback context.
    """
    user = update.effective_user
    if not user or not update.message:
        return

    logger.info("model_command", user_id=user.id)

    # Get current settings
    current_model = "claude-sonnet-4-5-20250929"  # default
    current_thinking = 5120  # default

    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                prefs = await storage.get_preferences(db_user.id)
                if prefs:
                    current_model = prefs.claude_model
                    current_thinking = prefs.thinking_budget
    except Exception as e:
        logger.warning("failed_to_get_model_settings", error=str(e))

    await update.message.reply_text(
        get_status_text(current_model, current_thinking),
        reply_markup=get_model_keyboard(current_model, current_thinking),
        parse_mode="Markdown",
    )


async def model_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle model settings callbacks.

    Args:
        update: Telegram update object.
        context: Callback context.
    """
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    user = update.effective_user
    if not user:
        return

    callback_data = query.data
    logger.info("model_callback", user_id=user.id, callback=callback_data)

    # Handle close
    if callback_data == "model_close":
        await query.edit_message_text("Настройки сохранены.")
        return

    # Handle noop (header clicks)
    if callback_data == "model_noop":
        return

    # Get current settings
    current_model = "claude-sonnet-4-5-20250929"
    current_thinking = 0

    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if not db_user:
                db_user, _ = await storage.get_or_create_user(
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                )

            prefs = await storage.get_preferences(db_user.id)
            if prefs:
                current_model = prefs.claude_model
                current_thinking = prefs.thinking_budget

            # Handle model change
            if callback_data.startswith("model_set_"):
                model_key = callback_data.replace("model_set_", "")
                if model_key in MODELS:
                    current_model = MODELS[model_key]["id"]
                    await storage.update_preferences(
                        db_user.id,
                        claude_model=current_model,
                    )
                    logger.info(
                        "model_changed",
                        user_id=user.id,
                        model=model_key,
                    )

            # Handle thinking change
            elif callback_data.startswith("model_think_"):
                think_key = callback_data.replace("model_think_", "")
                if think_key in THINKING_LEVELS:
                    current_thinking = THINKING_LEVELS[think_key]["budget"]
                    await storage.update_preferences(
                        db_user.id,
                        thinking_budget=current_thinking,
                    )
                    logger.info(
                        "thinking_changed",
                        user_id=user.id,
                        thinking=think_key,
                        budget=current_thinking,
                    )

    except Exception as e:
        logger.error("model_callback_failed", error=str(e))
        await query.edit_message_text(f"Ошибка: {e}")
        return

    # Update message with new settings
    try:
        await query.edit_message_text(
            get_status_text(current_model, current_thinking),
            reply_markup=get_model_keyboard(current_model, current_thinking),
            parse_mode="Markdown",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def get_model_handlers() -> list:
    """Get handlers for model settings.

    Returns:
        List of handlers to register.
    """
    return [
        CommandHandler("model", model_command_handler),
        CallbackQueryHandler(model_callback_handler, pattern="^model_"),
    ]


async def get_user_model_settings(telegram_id: int) -> tuple[str, int]:
    """Get user's AI model settings.

    Args:
        telegram_id: Telegram user ID.

    Returns:
        Tuple of (model_id, thinking_budget).
    """
    default_model = "claude-sonnet-4-5-20250929"
    default_thinking = 5120

    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(telegram_id)
            if db_user:
                prefs = await storage.get_preferences(db_user.id)
                if prefs:
                    return prefs.claude_model, prefs.thinking_budget
    except Exception as e:
        logger.warning("failed_to_get_model_settings", telegram_id=telegram_id, error=str(e))

    return default_model, default_thinking
