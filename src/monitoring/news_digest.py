"""Personalized news digest generator.

Collects data from TMDB, web search, and user profile to generate
Claude-powered personalized cinema news digests.

Two formats:
- Daily: short evening briefing (3-5 topics)
- Weekly: comprehensive digest like a podcast episode (7-10 topics)
"""

import hashlib
import json
from datetime import UTC, date, datetime
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
        # User profile context — use formatted rendering (same as conversation)
        memory_manager = CoreMemoryManager(storage)
        blocks = await memory_manager.get_all_blocks(user_id)
        data["user_profile"] = memory_manager.render_blocks_for_context(blocks)

        # Recent watch history
        watched = await storage.get_watched(user_id, limit=20)
        data["recent_watched"] = [
            {"title": w.title, "rating": w.rating, "media_type": w.media_type} for w in watched
        ]

        # Favorites: highly rated items (strongest taste signal)
        all_watched = await storage.get_watched(user_id, limit=100)
        data["favorites"] = [
            {
                "title": w.title,
                "rating": w.rating,
                "media_type": w.media_type,
                "year": w.year,
            }
            for w in all_watched
            if w.rating and w.rating >= 8
        ][:15]

        # Low-rated items (what to avoid)
        data["disliked"] = [
            {"title": w.title, "rating": w.rating, "media_type": w.media_type}
            for w in all_watched
            if w.rating and w.rating <= 4
        ][:10]

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

        # Recent digest topics (to avoid repetition in daily digests)
        recent_topics = await storage.get_recent_digest_topics(user_id, days=3, digest_type="daily")
        data["recent_topics"] = recent_topics

        # User preferences
        prefs = await storage.get_preferences(user_id)
        if prefs:
            data["preferences"] = {
                "quality": prefs.video_quality,
                "audio_language": prefs.audio_language,
                "genres": prefs.preferred_genres,
            }

        # Recently found monitors (releases that became available)
        try:
            found_monitors = await storage.get_monitors(user_id=user_id, status="found")
            # Include monitors found in the last 48 hours
            recent_found = []
            now = datetime.now(UTC)
            for m in found_monitors:
                if m.found_at:
                    found_at = m.found_at
                    if found_at.tzinfo is None:
                        found_at = found_at.replace(tzinfo=UTC)
                    if (now - found_at).total_seconds() < 172800:  # 48 hours
                        recent_found.append(
                            {
                                "title": m.title,
                                "media_type": m.media_type,
                                "quality": m.quality,
                                "found_at": found_at.isoformat(),
                                "source": m.found_data.get("source") if m.found_data else None,
                                "season": m.season_number,
                                "episode": m.episode_number,
                            }
                        )
            data["recently_found_monitors"] = recent_found
        except Exception as e:
            logger.warning("digest_monitors_data_failed", error=str(e))
            data["recently_found_monitors"] = []

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

    # Fetch industry news from RSS feeds
    try:
        from src.services.news import NewsService

        async with NewsService() as news_service:
            # First try keyword-filtered news
            news_items = await news_service.get_relevant_news(
                keywords=[
                    "Oscar",
                    "Emmy",
                    "Golden Globe",
                    "Cannes",
                    "premiere",
                    "Netflix",
                    "HBO",
                    "Disney",
                    "A24",
                    "box office",
                    "streaming",
                    "trailer",
                    "release",
                ],
                hours=48,
                max_results=15,
            )

            # If not enough results, get all recent news
            if len(news_items) < 5:
                logger.info("digest_news_few_results", count=len(news_items))
                all_news = await news_service.get_all_recent_news(hours=24, max_per_feed=5)
                # Combine and deduplicate by title
                existing_titles = {n.title.lower() for n in news_items}
                for item in all_news:
                    if item.title.lower() not in existing_titles:
                        news_items.append(item)
                        existing_titles.add(item.title.lower())
                        if len(news_items) >= 15:
                            break

            data["industry_news"] = [
                {
                    "title": n.title,
                    "description": n.description[:200] if n.description else "",
                    "source": n.source,
                    "date": n.published_at.isoformat() if n.published_at else None,
                }
                for n in news_items
            ]
            logger.info("digest_news_collected", count=len(data["industry_news"]))
    except Exception as e:
        logger.warning("digest_news_fetch_failed", error=str(e))
        data["industry_news"] = []

    return data


async def generate_digest(
    user_id: int,
    telegram_id: int,
    digest_type: str = "daily",
) -> tuple[str, list[Download], str | None] | None:
    """Generate a personalized digest using Claude.

    Args:
        user_id: Internal user ID
        telegram_id: Telegram user ID (for entity links)
        digest_type: "daily" or "weekly"

    Returns:
        Tuple of (digest HTML text, downloads mentioned, topics summary)
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
            # Weekly digest uses Opus with adaptive thinking for deeper analysis
            message = await client.messages.create(
                model="claude-opus-4-6",
                max_tokens=16000,
                thinking={"type": "adaptive"},
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

        # Extract topics summary for deduplication
        import re

        topics_summary = None
        topics_match = re.search(r"---TOPICS---\s*(.*?)\s*---END---", response, re.DOTALL)
        if topics_match:
            topics_summary = topics_match.group(1).strip()
            # Remove the topics block from the response
            response = re.sub(r"---TOPICS---.*?---END---", "", response, flags=re.DOTALL).strip()

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

        return html_text, mentioned_downloads, topics_summary

    except Exception as e:
        logger.exception("digest_generation_failed", user_id=user_id, error=str(e))
        return None


def _build_daily_prompt(data: dict[str, Any], telegram_id: int) -> str:
    """Build the prompt for daily digest generation."""
    bot_username = settings.bot_username
    today = date.today()
    prefs_json = json.dumps(data.get("preferences", {}), ensure_ascii=False)

    return f"""Ты — ведущий вечернего кинобрифинга. Стиль: неформальный, информативный, без восторгов и канцеляризмов.

Напиши короткий дайджест (3-5 тем).

## Контекст пользователя (для фильтрации и выбора тем, НЕ для упоминания)
{data.get("user_profile", "Нет данных")}

### Любимое (оценки 8+, это вкус пользователя)
{json.dumps(data.get("favorites", []), ensure_ascii=False)}

### Не понравилось (оценки 1-4, избегай похожего)
{json.dumps(data.get("disliked", []), ensure_ascii=False)}

### Недавно смотрел
{json.dumps(data.get("recent_watched", [])[:10], ensure_ascii=False)}

### Хочет посмотреть (watchlist)
{json.dumps(data.get("watchlist", []), ensure_ascii=False)}

### Предпочтения
{prefs_json}

Blocklist (НЕ упоминай!): {json.dumps(data.get("blocklist", []), ensure_ascii=False)}

### Темы из прошлых дайджестов (НЕ ПОВТОРЯЙ без нового контекста!)
{json.dumps(data.get("recent_topics", []), ensure_ascii=False)}

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

### Мониторинги: недавно найденные релизы
{json.dumps(data.get("recently_found_monitors", []), ensure_ascii=False)}

## Правила

Сегодня: {today.isoformat()}

⚠️ КРИТИЧЕСКИ ВАЖНО:
- ИСПОЛЬЗУЙ ТОЛЬКО данные из разделов выше
- НЕ ДОБАВЛЯЙ информацию из своей памяти — она устарела!
- Если в данных нет свежих новостей — НЕ ВЫДУМЫВАЙ их
- Любой сериал/фильм "в разработке" из твоей памяти может уже выйти — не упоминай такое без источника
- НЕ ПОВТОРЯЙ темы из прошлых дайджестов, если нет НОВОГО развития событий

1. Выбери 3-5 объективно интересных тем ТОЛЬКО из данных выше
2. Пиши как новостной дайджест, а НЕ как персональные рекомендации
3. НЕ НАДО в каждом пункте писать «учитывая ваши вкусы» или «вам понравится». Профиль нужен только чтобы отфильтровать неинтересное и адаптировать глубину подачи
4. Персонализация должна быть НЕВИДИМОЙ — выбор тем, а не их подача
5. Entity-ссылки: <a href="https://t.me/{bot_username}?start=m_TMDB_ID">Название</a> для фильмов, t_ для сериалов
6. Если есть памятная дата — включи (это изюминка)
7. Для цифровых релизов — отметь «уже можно скачать»
8. Если есть скачивания без отзыва — можно ОДИН РАЗ мимоходом спросить в конце
9. Если есть недавно найденные мониторинги — упомяни, что релиз стал доступен на трекере
10. Формат: Telegram HTML (<b>, <i>, <a href>). НЕ используй Markdown
11. Эмодзи только структурные: 📰 🎬 📺 💿 📅. Не для эмоций
12. Максимум 1500 символов

## ОБЯЗАТЕЛЬНО в конце добавь блок:
---TOPICS---
[список ключевых тем через запятую: названия фильмов, сериалов, персон, событий]
---END---"""


def _build_weekly_prompt(data: dict[str, Any], telegram_id: int) -> str:
    """Build the prompt for weekly digest generation."""
    bot_username = settings.bot_username
    today = date.today()
    prefs_json = json.dumps(data.get("preferences", {}), ensure_ascii=False)

    return f"""Ты — ведущий еженедельного кинодайджеста в стиле подкастов The Town / The Big Picture:
обстоятельный, с контекстом и авторским мнением, но без занудства. Это кураторский обзор недели.

## Контекст пользователя (для фильтрации, глубины и выбора тем)
{data.get("user_profile", "Нет данных")}

### Любимое (оценки 8+, это вкус пользователя)
{json.dumps(data.get("favorites", []), ensure_ascii=False)}

### Не понравилось (оценки 1-4, избегай похожего)
{json.dumps(data.get("disliked", []), ensure_ascii=False)}

### Недавно смотрел
{json.dumps(data.get("recent_watched", [])[:10], ensure_ascii=False)}

### Хочет посмотреть (watchlist)
{json.dumps(data.get("watchlist", []), ensure_ascii=False)}

### Предпочтения
{prefs_json}

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

### Мониторинги: недавно найденные релизы
{json.dumps(data.get("recently_found_monitors", []), ensure_ascii=False)}

## Правила

Сегодня: {today.isoformat()}

⚠️ КРИТИЧЕСКИ ВАЖНО:
- ИСПОЛЬЗУЙ ТОЛЬКО данные из разделов выше
- НЕ ДОБАВЛЯЙ информацию из своей памяти — она устарела!
- Если сериал/фильм был "в разработке" по твоим данным — НЕ упоминай, если нет в источниках выше
- Новости индустрии бери ТОЛЬКО из раздела "Новости индустрии"

1. 7-10 тем, объективно значимых для киноиндустрии, ТОЛЬКО из данных выше
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
7. Если есть недавно найденные мониторинги — упомяни, что релиз стал доступен
7. Формат: Telegram HTML (<b>, <i>, <a href>). НЕ Markdown
8. Эмодзи только структурные. Максимум 3500 символов

## ОБЯЗАТЕЛЬНО в конце добавь блок:
---TOPICS---
[список ключевых тем через запятую: названия фильмов, сериалов, персон, событий]
---END---"""


async def pick_download_for_feedback(user_id: int) -> Download | None:
    """Pick one recent download to ask for feedback.

    Filters out:
    - Downloads already asked about (followed_up > 0)
    - Adult content (checked via TMDB)

    Args:
        user_id: Internal user ID

    Returns:
        Download to ask about, or None if none eligible
    """
    from src.media.tmdb import TMDBClient

    async with get_storage() as storage:
        # Get downloads not yet asked about (followed_up = 0)
        downloads = await storage.get_recent_unreviewed_downloads(user_id, days=14)
        # Filter to only those never asked (followed_up = 0)
        not_asked = [d for d in downloads if d.followed_up == 0]

    if not not_asked:
        return None

    # Filter out adult content
    async with TMDBClient() as tmdb:
        for download in not_asked:
            if not download.tmdb_id:
                # No TMDB ID - skip, can't verify
                continue

            media_type = download.media_type or "movie"
            is_adult = await tmdb.is_adult_content(download.tmdb_id, media_type)

            if not is_adult:
                return download

    return None


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

    html_text, mentioned_downloads, topics_summary = result

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

    # Record digest history with topics for deduplication
    async with get_storage() as storage:
        data = await collect_digest_data(user_id)
        content_hash = compute_content_hash(data)
        await storage.add_digest_history(
            user_id, digest_type, content_hash, topics_summary=topics_summary
        )

        # Mark mentioned downloads as followed up
        for dl in mentioned_downloads:
            await storage.mark_followup_sent(dl.id)

    # Send feedback prompt for one download (if any eligible)
    feedback_download = await pick_download_for_feedback(user_id)
    if feedback_download:
        await _send_feedback_prompt(bot, telegram_id, feedback_download)
        # Mark as asked
        async with get_storage() as storage:
            await storage.mark_followup_sent(feedback_download.id)

    logger.info(
        "digest_sent",
        user_id=user_id,
        telegram_id=telegram_id,
        digest_type=digest_type,
        downloads_mentioned=len(mentioned_downloads),
        feedback_download=feedback_download.title if feedback_download else None,
    )
    return True


async def _send_feedback_prompt(bot: "Bot", telegram_id: int, download: Download) -> None:
    """Send feedback prompt for a specific download.

    Args:
        bot: Telegram Bot instance
        telegram_id: Telegram chat ID
        download: Download to ask about
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    title = download.title
    # Add season/episode info if present
    if download.season and download.episode:
        title = f"{title} (S{download.season:02d}E{download.episode:02d})"
    elif download.season:
        title = f"{title} (сезон {download.season})"

    text = f"Кстати, ты уже посмотрел <b>{title}</b>?"

    buttons = [
        [
            InlineKeyboardButton("👍", callback_data=f"dfb_like_{download.id}"),
            InlineKeyboardButton("👎", callback_data=f"dfb_dislike_{download.id}"),
            InlineKeyboardButton("👀 Ещё нет", callback_data=f"dfb_later_{download.id}"),
        ]
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.warning("feedback_prompt_send_failed", telegram_id=telegram_id, error=str(e))
