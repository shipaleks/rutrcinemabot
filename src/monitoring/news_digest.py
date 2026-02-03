"""Personalized news digest generator.

Collects data from TMDB, web search, and user profile to generate
Claude-powered personalized cinema news digests.

Two formats:
- Daily: short evening briefing (3-5 topics)
- Weekly: comprehensive digest like a podcast episode (7-10 topics)
"""

import hashlib
import json
from datetime import date
from typing import TYPE_CHECKING, Any

import structlog

from src.config import settings
from src.user.storage import Download, get_storage

if TYPE_CHECKING:
    from telegram import Bot

logger = structlog.get_logger(__name__)

# Digest delivery settings
DAILY_DIGEST_HOUR = 19  # 19:00 in user's timezone
WEEKLY_DIGEST_DAYS = (1, 4)  # Tuesday and Friday (0=Monday)
WEEKLY_DIGEST_HOUR = 19


async def collect_digest_data(user_id: int) -> dict[str, Any]:
    """Collect raw data for digest generation.

    Fetches trending movies, premieres, digital releases, anniversaries,
    and user-specific context.

    Args:
        user_id: Internal user ID

    Returns:
        Dict with all raw data for Claude to compose the digest
    """
    from src.media.tmdb import TMDBClient
    from src.user.memory import CoreMemoryManager

    data: dict[str, Any] = {}
    today = date.today()

    async with get_storage() as storage:
        # User profile context
        memory_manager = CoreMemoryManager(storage)
        blocks = await memory_manager.get_all_blocks(user_id)
        profile_context = ""
        for block in blocks:
            if block.content:
                profile_context += f"\n{block.block_name}: {block.content}"
        data["user_profile"] = profile_context

        # Recent watch history
        watched = await storage.get_watched(user_id, limit=20)
        data["recent_watched"] = [
            {"title": w.title, "rating": w.rating, "media_type": w.media_type} for w in watched
        ]

        # Recent unreviewed downloads (for natural follow-up)
        downloads = await storage.get_recent_unreviewed_downloads(user_id, days=14)
        data["unreviewed_downloads"] = [
            {
                "id": d.id,
                "title": d.title,
                "media_type": d.media_type,
                "downloaded_at": d.downloaded_at.isoformat(),
                "season": d.season,
                "episode": d.episode,
            }
            for d in downloads
        ]

        # Watchlist
        watchlist = await storage.get_watchlist(user_id, limit=10)
        data["watchlist"] = [{"title": w.title, "media_type": w.media_type} for w in watchlist]

        # Blocklist (to avoid mentioning)
        blocklist = await storage.get_blocklist(user_id)
        data["blocklist"] = [
            {"type": b.block_type, "value": b.block_value, "level": b.block_level}
            for b in blocklist
        ]

        # User preferences
        prefs = await storage.get_preferences(user_id)
        if prefs:
            data["preferences"] = {
                "quality": prefs.video_quality,
                "audio_language": prefs.audio_language,
                "genres": prefs.preferred_genres,
            }

    # TMDB data
    try:
        async with TMDBClient() as tmdb:
            data["trending"] = await tmdb.get_trending("all", "day")
            data["now_playing"] = await tmdb.get_now_playing()
            data["upcoming"] = await tmdb.get_upcoming_movies()
            data["recently_digital"] = await tmdb.get_recently_released_digital()

            # Anniversary movies for today
            month_day = today.strftime("%m-%d")
            data["anniversaries"] = await tmdb.discover_anniversary_movies(month_day)
    except Exception as e:
        logger.warning("digest_tmdb_data_failed", error=str(e))
        data.setdefault("trending", [])
        data.setdefault("now_playing", [])
        data.setdefault("upcoming", [])
        data.setdefault("recently_digital", [])
        data.setdefault("anniversaries", [])

    # Web search for industry news
    try:
        from src.services.news import NewsService

        async with NewsService() as news_service:
            news_items = await news_service.get_relevant_news(
                keywords=["Oscar", "Golden Globe", "Cannes", "кино", "сериалы", "Netflix", "A24"],
                hours=48,
                max_results=10,
            )
            data["industry_news"] = [
                {
                    "title": n.title,
                    "description": n.description[:200],
                    "source": n.source,
                }
                for n in news_items
            ]
    except Exception as e:
        logger.debug("digest_news_fetch_failed", error=str(e))
        data["industry_news"] = []

    return data


async def generate_digest(
    user_id: int,
    telegram_id: int,
    digest_type: str = "daily",
) -> tuple[str, list[Download]] | None:
    """Generate a personalized digest using Claude.

    Args:
        user_id: Internal user ID
        telegram_id: Telegram user ID (for entity links)
        digest_type: "daily" or "weekly"

    Returns:
        Tuple of (digest HTML text, downloads mentioned for follow-up marking)
        or None if generation fails
    """
    import anthropic

    data = await collect_digest_data(user_id)

    if digest_type == "daily":
        prompt = _build_daily_prompt(data, telegram_id)
    else:
        prompt = _build_weekly_prompt(data, telegram_id)

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

        if digest_type == "weekly":
            # Weekly digest uses Opus with extended thinking for deeper analysis
            message = await client.messages.create(
                model="claude-opus-4-5-20251101",
                max_tokens=16000,
                thinking={
                    "type": "enabled",
                    "budget_tokens": 10000,
                },
                messages=[{"role": "user", "content": prompt}],
            )
        else:
            # Daily digest uses Sonnet for speed
            message = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )

        response = ""
        for block in message.content:
            if hasattr(block, "text"):
                response += block.text

        if not response.strip():
            logger.warning("digest_empty_response", user_id=user_id)
            return None

        # Convert markdown links to HTML for Telegram
        from src.bot.streaming import _markdown_to_telegram_html

        html_text = _markdown_to_telegram_html(response)

        # Track which downloads were mentioned
        mentioned_downloads = []
        for d in data.get("unreviewed_downloads", []):
            if d["title"].lower() in response.lower():
                # Find the actual Download object
                async with get_storage() as storage:
                    downloads = await storage.get_recent_unreviewed_downloads(user_id)
                    for dl in downloads:
                        if dl.id == d["id"]:
                            mentioned_downloads.append(dl)
                            break

        return html_text, mentioned_downloads

    except Exception as e:
        logger.exception("digest_generation_failed", user_id=user_id, error=str(e))
        return None


def _build_daily_prompt(data: dict[str, Any], telegram_id: int) -> str:
    """Build the prompt for daily digest generation."""
    bot_username = settings.bot_username
    today = date.today()

    return f"""Ты — ведущий вечернего кинобрифинга. Стиль: неформальный, информативный, без восторгов и канцеляризмов.

Напиши короткий дайджест (3-5 тем).

## Контекст пользователя (для фильтрации, НЕ для упоминания в каждом предложении)
Профиль: {data.get("user_profile", "Нет данных")}
Blocklist (НЕ упоминай!): {json.dumps(data.get("blocklist", []), ensure_ascii=False)}

## Данные

### Тренды дня
{json.dumps(data.get("trending", [])[:10], ensure_ascii=False)}

### Сейчас в кино
{json.dumps(data.get("now_playing", [])[:8], ensure_ascii=False)}

### Появилось в цифре
{json.dumps(data.get("recently_digital", [])[:8], ensure_ascii=False)}

### Памятные даты
{json.dumps(data.get("anniversaries", []), ensure_ascii=False)}

### Новости индустрии
{json.dumps(data.get("industry_news", []), ensure_ascii=False)}

### Недавние скачивания (без отзыва)
{json.dumps(data.get("unreviewed_downloads", []), ensure_ascii=False)}

## Правила

Дата: {today.isoformat()}

1. Выбери 3-5 объективно интересных тем из данных выше
2. Пиши как новостной дайджест, а НЕ как персональные рекомендации
3. НЕ НАДО в каждом пункте писать «учитывая ваши вкусы» или «вам понравится». Профиль нужен только чтобы отфильтровать неинтересное и адаптировать глубину подачи
4. Персонализация должна быть НЕВИДИМОЙ — выбор тем, а не их подача
5. Entity-ссылки: <a href="https://t.me/{bot_username}?start=m_TMDB_ID">Название</a> для фильмов, t_ для сериалов
6. Если есть памятная дата — включи (это изюминка)
7. Для цифровых релизов — отметь «уже можно скачать»
8. Если есть скачивания без отзыва — можно ОДИН РАЗ мимоходом спросить в конце
9. Формат: Telegram HTML (<b>, <i>, <a href>). НЕ используй Markdown
10. Эмодзи только структурные: 📰 🎬 📺 💿 📅. Не для эмоций
11. Максимум 1500 символов"""


def _build_weekly_prompt(data: dict[str, Any], telegram_id: int) -> str:
    """Build the prompt for weekly digest generation."""
    bot_username = settings.bot_username
    today = date.today()

    return f"""Ты — ведущий еженедельного кинодайджеста в стиле подкастов The Town / The Big Picture:
обстоятельный, с контекстом и авторским мнением, но без занудства. Это кураторский обзор недели.

## Контекст пользователя (для фильтрации и глубины, НЕ для упоминания)
Профиль: {data.get("user_profile", "Нет данных")}
Недавно смотрел: {json.dumps(data.get("recent_watched", [])[:10], ensure_ascii=False)}
Blocklist (НЕ упоминай!): {json.dumps(data.get("blocklist", []), ensure_ascii=False)}

## Данные

### Тренды недели
{json.dumps(data.get("trending", []), ensure_ascii=False)}

### Сейчас в кино
{json.dumps(data.get("now_playing", []), ensure_ascii=False)}

### Скоро выходит
{json.dumps(data.get("upcoming", []), ensure_ascii=False)}

### Появилось в цифре
{json.dumps(data.get("recently_digital", []), ensure_ascii=False)}

### Памятные даты
{json.dumps(data.get("anniversaries", []), ensure_ascii=False)}

### Новости индустрии
{json.dumps(data.get("industry_news", []), ensure_ascii=False)}

### Скачивания без отзыва
{json.dumps(data.get("unreviewed_downloads", []), ensure_ascii=False)}

## Правила

Дата: {today.isoformat()}

1. 7-10 тем, объективно значимых для киноиндустрии
2. Структура:
   - Короткое приветствие
   - 📰 <b>Главное за неделю</b> — крупные индустриальные новости
   - 🎬 <b>Премьеры</b> — что вышло в кино/стримингах
   - 💿 <b>Появилось в цифре</b> — можно скачать
   - 📅 <b>Дата в истории</b> — юбилей фильма, повод пересмотреть
   - 🎯 <b>Рекомендация</b> — одна персональная рекомендация в конце

3. ВАЖНО: Персонализация должна быть НЕВИДИМОЙ
   - Профиль используй для ВЫБОРА тем и глубины подачи
   - НЕ пиши «учитывая ваш интерес к X» или «вам как любителю Y»
   - Исключение: секция «Рекомендация» может быть явно персональной

4. Entity-ссылки: <a href="https://t.me/{bot_username}?start=m_TMDB_ID">Название</a> для фильмов, t_ для сериалов
5. Можно иметь мнение — это авторский дайджест, не нейтральная лента новостей
6. Если есть скачивания без отзыва — мимоходом спроси в конце
7. Формат: Telegram HTML (<b>, <i>, <a href>). НЕ Markdown
8. Эмодзи только структурные. Максимум 3500 символов"""


def compute_content_hash(data: dict[str, Any]) -> str:
    """Compute a hash of digest content to avoid duplicates."""
    # Use trending + anniversaries + news as key differentiators
    key_items = []
    for item in data.get("trending", [])[:5]:
        key_items.append(str(item.get("id", "")))
    for item in data.get("anniversaries", [])[:3]:
        key_items.append(str(item.get("id", "")))
    for item in data.get("industry_news", [])[:3]:
        key_items.append(item.get("title", "")[:50])

    content = "|".join(key_items)
    return hashlib.md5(content.encode()).hexdigest()[:16]


async def send_digest(
    bot: "Bot",
    user_id: int,
    telegram_id: int,
    digest_type: str = "daily",
) -> bool:
    """Generate and send a personalized digest to a user.

    Args:
        bot: Telegram Bot instance
        user_id: Internal user ID
        telegram_id: Telegram chat ID
        digest_type: "daily" or "weekly"

    Returns:
        True if digest was sent successfully
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    result = await generate_digest(user_id, telegram_id, digest_type)
    if not result:
        logger.warning("digest_generation_returned_none", user_id=user_id)
        return False

    html_text, mentioned_downloads = result

    # Add frequency selection buttons if this is the first digest
    async with get_storage() as storage:
        last_time = await storage.get_last_digest_time(user_id, "daily")
        is_first = last_time is None

        # Also check weekly
        if is_first:
            last_weekly = await storage.get_last_digest_time(user_id, "weekly")
            is_first = last_weekly is None

    keyboard = None
    if is_first:
        buttons = [
            [
                InlineKeyboardButton("📬 Ежедневно", callback_data="digest_freq_daily"),
                InlineKeyboardButton("📋 2 р/нед", callback_data="digest_freq_weekly"),
                InlineKeyboardButton("🔕 Отключить", callback_data="digest_freq_none"),
            ]
        ]
        keyboard = InlineKeyboardMarkup(buttons)

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=html_text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        # Fallback: strip HTML and send plain
        import re

        plain = re.sub(r"<[^>]+>", "", html_text)
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=plain,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("digest_send_failed", user_id=user_id, error=str(e))
            return False

    # Record digest history
    async with get_storage() as storage:
        data = await collect_digest_data(user_id)
        content_hash = compute_content_hash(data)
        await storage.add_digest_history(user_id, digest_type, content_hash)

        # Mark mentioned downloads as followed up
        for dl in mentioned_downloads:
            await storage.mark_followup_sent(dl.id)

    logger.info(
        "digest_sent",
        user_id=user_id,
        telegram_id=telegram_id,
        digest_type=digest_type,
        downloads_mentioned=len(mentioned_downloads),
    )
    return True
