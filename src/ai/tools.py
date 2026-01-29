"""Tool definitions for Claude API integration.

This module defines all available tools that Claude can use to help users
find movies, TV shows, and manage their media preferences.

Tools follow the Anthropic tool definition format with JSON schema validation.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# Type alias for tool handler functions
ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


# =============================================================================
# Tool Definitions (JSON Schema format for Claude API)
# =============================================================================

RUTRACKER_SEARCH_TOOL = {
    "name": "rutracker_search",
    "description": (
        "Поиск фильмов и сериалов на торрент-трекере Rutracker. "
        "Возвращает список раздач с названием, размером, количеством сидов и magnet-ссылкой. "
        "Используй для поиска русскоязычного контента или раздач с русской озвучкой.\n\n"
        "⚠️ ВАЖНО: ВСЕГДА передавай параметр quality из предпочтений пользователя!\n"
        "Если в профиле указано '4K' или 'максимальное качество' — передавай quality='4K'.\n"
        "Если '1080p' — передавай quality='1080p'. НЕ игнорируй предпочтения!\n\n"
        "При пустых результатах (status='no_results'):\n"
        "1. Попробуй упростить запрос — убери качество и категорию\n"
        "2. Попробуй piratebay_search с английским названием\n"
        "3. Попробуй оригинальное название фильма на английском"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Название фильма или сериала для поиска",
            },
            "quality": {
                "type": "string",
                "description": "Качество видео: 1080p, 4K, 720p и т.д.",
                "enum": ["720p", "1080p", "4K", "2160p", "HDR"],
            },
            "category": {
                "type": "string",
                "description": "Категория контента",
                "enum": ["movie", "tv_show", "anime", "documentary"],
            },
        },
        "required": ["query"],
    },
}

PIRATEBAY_SEARCH_TOOL = {
    "name": "piratebay_search",
    "description": (
        "Поиск торрентов на PirateBay. "
        "Используй как fallback если Rutracker недоступен или не найдены нужные раздачи. "
        "Возвращает международные релизы.\n\n"
        "⚠️ ВАЖНО: ВСЕГДА передавай параметр quality из предпочтений пользователя!\n"
        "Если в профиле указано '4K' или 'максимальное качество' — передавай quality='4K'.\n\n"
        "ВАЖНО: Используй АНГЛИЙСКОЕ название фильма для поиска на PirateBay."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Название фильма или сериала на английском",
            },
            "quality": {
                "type": "string",
                "description": "Качество видео",
                "enum": ["720p", "1080p", "4K", "2160p"],
            },
            "min_seeds": {
                "type": "integer",
                "description": "Минимальное количество сидов для фильтрации",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

TMDB_SEARCH_TOOL = {
    "name": "tmdb_search",
    "description": (
        "Поиск фильмов и сериалов в базе данных TMDB (The Movie Database). "
        "Возвращает метаданные: название, год, описание, рейтинг, постер. "
        "Используй для получения информации о фильме перед поиском на трекерах."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Название фильма или сериала для поиска",
            },
            "year": {
                "type": "integer",
                "description": "Год выпуска для уточнения поиска",
            },
            "media_type": {
                "type": "string",
                "description": "Тип контента: movie или tv",
                "enum": ["movie", "tv"],
            },
            "language": {
                "type": "string",
                "description": "Язык результатов (ISO 639-1)",
                "default": "ru-RU",
            },
        },
        "required": ["query"],
    },
}

TMDB_PERSON_SEARCH_TOOL = {
    "name": "tmdb_person_search",
    "description": (
        "Поиск персоны (актёра, режиссёра, сценариста) по имени в TMDB. "
        "КРИТИЧЕСКИ ВАЖНО: твои знания о TMDB ID персон НЕВЕРНЫ! "
        "ВСЕГДА вызывай этот инструмент чтобы получить правильный ID персоны "
        "перед созданием entity ссылки (p_ID). Без этого ссылки будут СЛОМАНЫ!"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Имя персоны для поиска (например 'Ari Aster' или 'Тимоти Шаламе')",
            },
        },
        "required": ["query"],
    },
}

TMDB_CREDITS_TOOL = {
    "name": "tmdb_credits",
    "description": (
        "Получение информации о съёмочной группе и актёрах фильма из TMDB. "
        "Возвращает режиссёра, актёров, сценаристов и других участников с их TMDB ID."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tmdb_id": {
                "type": "integer",
                "description": "ID фильма или сериала в TMDB",
            },
            "media_type": {
                "type": "string",
                "description": "Тип контента: movie или tv",
                "enum": ["movie", "tv"],
            },
        },
        "required": ["tmdb_id", "media_type"],
    },
}

TMDB_TV_DETAILS_TOOL = {
    "name": "tmdb_tv_details",
    "description": (
        "Получение детальной информации о сериале из TMDB. "
        "Возвращает количество сезонов, статус (в производстве/завершён), "
        "информацию о следующем эпизоде (дата выхода, номер сезона/эпизода). "
        "Используй перед созданием монитора чтобы узнать актуальный сезон."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tmdb_id": {
                "type": "integer",
                "description": "ID сериала в TMDB",
            },
        },
        "required": ["tmdb_id"],
    },
}

TMDB_BATCH_ENTITY_SEARCH_TOOL = {
    "name": "tmdb_batch_entity_search",
    "description": (
        "Пакетный поиск нескольких персон и фильмов ОДНИМ вызовом. "
        "ИСПОЛЬЗУЙ ЭТОТ ИНСТРУМЕНТ когда нужно получить TMDB ID для нескольких entity сразу — "
        "например, при формировании ответа про новости с множеством упоминаний людей и фильмов. "
        "Возвращает словарь с ID для каждого найденного имени/названия. "
        "Один вызов этого инструмента заменяет 10-15 вызовов tmdb_search/tmdb_person_search."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "people": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Список имён персон для поиска (актёры, режиссёры, сценаристы)",
            },
            "movies": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Список названий фильмов для поиска",
            },
            "tv_shows": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Список названий сериалов для поиска",
            },
        },
        "required": [],
    },
}

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Поиск актуальной информации в интернете через Yandex Search. "
        "ИСПОЛЬЗУЙ для проверки свежих фактов, которых может не быть в TMDB или новостных RSS: "
        "- Номинанты/победители премий (Оскар, Золотой глобус, etc.)\n"
        "- Свежие анонсы фильмов и сериалов\n"
        "- Актуальные новости о персонах (новые проекты режиссёров/актёров)\n"
        "- Даты релизов и премьер\n"
        "- Любая информация, требующая проверки в реальном времени\n\n"
        "⚠️ ВАЖНО: ВСЕГДА добавляй ТЕКУЩИЙ ГОД в запрос для актуальных событий!\n"
        "Смотри 'Сегодняшняя дата' в системном промпте.\n"
        "Пример: 'Oscar 2025 nominations', НЕ просто 'Oscar nominations'.\n\n"
        "Возвращает топ результатов поиска с заголовками, описаниями и ссылками."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Поисковый запрос (лучше на английском для международных тем)",
            },
            "max_results": {
                "type": "integer",
                "description": "Максимум результатов (по умолчанию 5)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

KINOPOISK_SEARCH_TOOL = {
    "name": "kinopoisk_search",
    "description": (
        "Поиск фильмов в базе Кинопоиска. "
        "Возвращает рейтинг Кинопоиска, описание на русском, информацию о российском прокате."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Название фильма (на русском или английском)",
            },
            "year": {
                "type": "integer",
                "description": "Год выпуска для уточнения поиска",
            },
        },
        "required": ["query"],
    },
}

GET_USER_PROFILE_TOOL = {
    "name": "get_user_profile",
    "description": (
        "Получение профиля пользователя с его предпочтениями. "
        "Возвращает предпочитаемое качество видео, язык аудио, любимые жанры."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Telegram ID пользователя",
            },
        },
        "required": ["user_id"],
    },
}

SEEDBOX_DOWNLOAD_TOOL = {
    "name": "seedbox_download",
    "description": (
        "Отправка magnet-ссылки на seedbox для скачивания. "
        "Если seedbox не настроен, возвращает magnet-ссылку пользователю напрямую."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "magnet": {
                "type": "string",
                "description": "Magnet-ссылка для скачивания",
            },
            "name": {
                "type": "string",
                "description": "Название раздачи для отображения",
            },
            "user_id": {
                "type": "integer",
                "description": "Telegram ID пользователя",
            },
        },
        "required": ["magnet", "user_id"],
    },
}

# =============================================================================
# Extended Tool Definitions (Phase 1-6)
# =============================================================================

READ_USER_PROFILE_TOOL = {
    "name": "read_user_profile",
    "description": (
        "Чтение полного профиля пользователя в формате Markdown. "
        "Профиль содержит предпочтения, контекст просмотра, стиль общения, "
        "блоклист и историю значимых взаимодействий. "
        "Используй в начале каждого разговора для персонализации ответов."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID (не Telegram ID)",
            },
        },
        "required": ["user_id"],
    },
}

UPDATE_USER_PROFILE_TOOL = {
    "name": "update_user_profile",
    "description": (
        "Обновление секции профиля пользователя. "
        "Используй для записи важной информации о пользователе: "
        "предпочтения в общении, контекст просмотра, значимые взаимодействия."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "section": {
                "type": "string",
                "description": "Секция профиля для обновления",
                "enum": [
                    "watch_context",
                    "communication_style",
                    "explicit_instructions",
                    "notable_interactions",
                    "conversation_highlights",
                ],
            },
            "content": {
                "type": "string",
                "description": "Новое содержимое секции",
            },
        },
        "required": ["user_id", "section", "content"],
    },
}

ADD_TO_WATCHLIST_TOOL = {
    "name": "add_to_watchlist",
    "description": (
        "Добавление фильма или сериала в список 'хочу посмотреть'. "
        "Сохраняет TMDB ID для последующей проверки доступности."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "tmdb_id": {
                "type": "integer",
                "description": "TMDB ID фильма/сериала",
            },
            "media_type": {
                "type": "string",
                "description": "Тип контента",
                "enum": ["movie", "tv"],
            },
            "title": {
                "type": "string",
                "description": "Название для отображения",
            },
            "year": {
                "type": "integer",
                "description": "Год выпуска",
            },
            "priority": {
                "type": "integer",
                "description": "Приоритет (выше = важнее)",
                "default": 0,
            },
            "notes": {
                "type": "string",
                "description": "Заметки пользователя",
            },
        },
        "required": ["user_id", "tmdb_id", "media_type", "title"],
    },
}

REMOVE_FROM_WATCHLIST_TOOL = {
    "name": "remove_from_watchlist",
    "description": "Удаление из списка 'хочу посмотреть'.",
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "tmdb_id": {
                "type": "integer",
                "description": "TMDB ID для удаления",
            },
        },
        "required": ["user_id", "tmdb_id"],
    },
}

GET_WATCHLIST_TOOL = {
    "name": "get_watchlist",
    "description": "Получение списка 'хочу посмотреть' пользователя.",
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "media_type": {
                "type": "string",
                "description": "Фильтр по типу",
                "enum": ["movie", "tv"],
            },
            "limit": {
                "type": "integer",
                "description": "Максимум записей",
                "default": 20,
            },
        },
        "required": ["user_id"],
    },
}

MARK_WATCHED_TOOL = {
    "name": "mark_watched",
    "description": (
        "Отметить фильм/сериал как просмотренный. Автоматически удаляет из watchlist если был там."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "tmdb_id": {
                "type": "integer",
                "description": "TMDB ID",
            },
            "media_type": {
                "type": "string",
                "description": "Тип контента",
                "enum": ["movie", "tv"],
            },
            "title": {
                "type": "string",
                "description": "Название",
            },
            "year": {
                "type": "integer",
                "description": "Год",
            },
            "rating": {
                "type": "number",
                "description": "Оценка пользователя (1-10)",
            },
            "review": {
                "type": "string",
                "description": "Короткий отзыв",
            },
        },
        "required": ["user_id", "tmdb_id", "media_type", "title"],
    },
}

RATE_CONTENT_TOOL = {
    "name": "rate_content",
    "description": "Поставить или обновить оценку просмотренному контенту.",
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "tmdb_id": {
                "type": "integer",
                "description": "TMDB ID",
            },
            "rating": {
                "type": "number",
                "description": "Оценка (1-10)",
            },
            "review": {
                "type": "string",
                "description": "Отзыв",
            },
        },
        "required": ["user_id", "tmdb_id", "rating"],
    },
}

GET_WATCH_HISTORY_TOOL = {
    "name": "get_watch_history",
    "description": "Получение истории просмотров пользователя.",
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "media_type": {
                "type": "string",
                "description": "Фильтр по типу",
                "enum": ["movie", "tv"],
            },
            "limit": {
                "type": "integer",
                "description": "Максимум записей",
                "default": 20,
            },
        },
        "required": ["user_id"],
    },
}

ADD_TO_BLOCKLIST_TOOL = {
    "name": "add_to_blocklist",
    "description": (
        "Добавить в блоклист (не рекомендовать или никогда не упоминать). "
        "Можно блокировать по названию, франшизе, жанру или создателю."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "block_type": {
                "type": "string",
                "description": "Тип блокировки",
                "enum": ["title", "franchise", "genre", "person"],
            },
            "block_value": {
                "type": "string",
                "description": "Что блокировать (название, жанр, имя)",
            },
            "block_level": {
                "type": "string",
                "description": "Уровень блокировки",
                "enum": ["dont_recommend", "never_mention"],
                "default": "dont_recommend",
            },
            "notes": {
                "type": "string",
                "description": "Примечания (например: 'кроме психологических')",
            },
        },
        "required": ["user_id", "block_type", "block_value"],
    },
}

GET_BLOCKLIST_TOOL = {
    "name": "get_blocklist",
    "description": "Получение блоклиста пользователя.",
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "block_type": {
                "type": "string",
                "description": "Фильтр по типу",
                "enum": ["title", "franchise", "genre", "person"],
            },
        },
        "required": ["user_id"],
    },
}

CREATE_MONITOR_TOOL = {
    "name": "create_monitor",
    "description": (
        "Создать мониторинг релиза. "
        "Бот будет периодически проверять доступность и уведомит, когда найдёт.\n\n"
        "Для сериалов (media_type='tv') можно указать режим отслеживания:\n"
        "- 'season': отслеживать весь сезон целиком (по умолчанию)\n"
        "- 'episode': отслеживать конкретный эпизод (нужно указать season_number и episode_number)\n\n"
        "ВАЖНО: Перед созданием мониторинга ОБЯЗАТЕЛЬНО спроси у пользователя:\n"
        "1. Какое качество нужно (720p, 1080p, 4K)?\n"
        "2. Для сериалов: отслеживать весь сезон или конкретный эпизод?"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "title": {
                "type": "string",
                "description": "Название для поиска",
            },
            "tmdb_id": {
                "type": "integer",
                "description": "TMDB ID (опционально)",
            },
            "media_type": {
                "type": "string",
                "description": "Тип контента",
                "enum": ["movie", "tv"],
                "default": "movie",
            },
            "quality": {
                "type": "string",
                "description": "Желаемое качество. ОБЯЗАТЕЛЬНО уточни у пользователя перед созданием!",
                "enum": ["720p", "1080p", "4K"],
            },
            "auto_download": {
                "type": "boolean",
                "description": "Автоматически скачивать при нахождении",
                "default": False,
            },
            "tracking_mode": {
                "type": "string",
                "description": "Режим отслеживания для сериалов: season (весь сезон) или episode (конкретный эпизод)",
                "enum": ["season", "episode"],
                "default": "season",
            },
            "season_number": {
                "type": "integer",
                "description": "Номер сезона (для режима episode или отслеживания конкретного сезона)",
            },
            "episode_number": {
                "type": "integer",
                "description": "Номер эпизода (только для режима episode)",
            },
        },
        "required": ["user_id", "title", "quality"],
    },
}

GET_MONITORS_TOOL = {
    "name": "get_monitors",
    "description": "Получение активных мониторингов релизов.",
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "status": {
                "type": "string",
                "description": "Фильтр по статусу",
                "enum": ["active", "found", "cancelled"],
            },
        },
        "required": ["user_id"],
    },
}

CANCEL_MONITOR_TOOL = {
    "name": "cancel_monitor",
    "description": "Отменить мониторинг релиза.",
    "input_schema": {
        "type": "object",
        "properties": {
            "monitor_id": {
                "type": "integer",
                "description": "ID мониторинга",
            },
        },
        "required": ["monitor_id"],
    },
}

GET_CREW_STATS_TOOL = {
    "name": "get_crew_stats",
    "description": (
        "Получение статистики по создателям контента (режиссёры, операторы и т.д.). "
        "Показывает кого пользователь смотрел больше всего и с какими оценками."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "role": {
                "type": "string",
                "description": "Фильтр по роли",
                "enum": ["director", "cinematographer", "composer", "writer", "actor"],
            },
            "min_films": {
                "type": "integer",
                "description": "Минимум фильмов для включения в статистику",
                "default": 2,
            },
        },
        "required": ["user_id"],
    },
}

LETTERBOXD_SYNC_TOOL = {
    "name": "letterboxd_sync",
    "description": (
        "Импорт данных из Letterboxd через RSS. Может импортировать watchlist и/или "
        "историю просмотров (diary) с оценками. Требуется Letterboxd username пользователя."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "letterboxd_username": {
                "type": "string",
                "description": "Letterboxd username (из URL профиля letterboxd.com/USERNAME)",
            },
            "sync_watchlist": {
                "type": "boolean",
                "description": "Импортировать watchlist",
                "default": True,
            },
            "sync_diary": {
                "type": "boolean",
                "description": "Импортировать историю просмотров с оценками",
                "default": True,
            },
            "diary_limit": {
                "type": "integer",
                "description": "Максимум записей из дневника (по умолчанию 10000 - все записи)",
                "default": 10000,
            },
        },
        "required": ["user_id", "letterboxd_username"],
    },
}


# =============================================================================
# Proactive Features Tools
# =============================================================================

GET_INDUSTRY_NEWS_TOOL = {
    "name": "get_industry_news",
    "description": (
        "Поиск актуальных новостей киноиндустрии по ключевым словам. "
        "Сканирует RSS-ленты Deadline, Variety, IndieWire, Hollywood Reporter.\n\n"
        "ВАЖНО:\n"
        "- Оскар/Academy Awards: keywords=['Oscar', 'Academy', 'nomination']\n"
        "- Фестивали: keywords=['Cannes', 'Venice', 'Berlin', 'Sundance']\n"
        "- Режиссёры: keywords=['Director Name', 'Имя']\n"
        "- Используй АНГЛИЙСКИЕ ключевые слова — новости на английском\n"
        "- Новости НЕ содержат TMDB ID — используй tmdb_batch_entity_search\n\n"
        "🚨 ПОСЛЕ получения результатов ты ОБЯЗАН:\n"
        "1. Вызвать tmdb_batch_entity_search для ВСЕХ людей и фильмов из новостей\n"
        "2. Оформить ВСЕ имена и названия как кликабельные ссылки\n"
        "3. Персонализировать: выделить новости по интересам пользователя из core_memory\n"
        "4. Проверить актуальность: если фильм 'анонсирован' — проверь через tmdb_search, не вышел ли он уже"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ключевые слова на АНГЛИЙСКОМ (Oscar, Cannes, имена режиссёров)",
            },
            "hours": {
                "type": "integer",
                "description": "За сколько часов искать новости (по умолчанию 72)",
                "default": 72,
            },
            "max_results": {
                "type": "integer",
                "description": "Максимум результатов (по умолчанию 5)",
                "default": 5,
            },
        },
        "required": ["keywords"],
    },
}

GET_RECENT_NEWS_TOOL = {
    "name": "get_recent_news",
    "description": (
        "Получить последние новости киноиндустрии БЕЗ фильтрации. "
        "Используй для обзора новостей ('какие новости в мире кино').\n\n"
        "🚨 ПОСЛЕ получения результатов ты ОБЯЗАН:\n"
        "1. Вызвать tmdb_batch_entity_search для ВСЕХ людей и фильмов из новостей\n"
        "2. Оформить ВСЕ имена и названия как кликабельные ссылки\n"
        "3. Персонализировать: сначала покажи новости, релевантные интересам из core_memory\n"
        "4. Проверить актуальность: сверяй с сегодняшней датой, не пересказывай устаревшее как новое"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hours": {
                "type": "integer",
                "description": "За сколько часов (по умолчанию 24)",
                "default": 24,
            },
            "max_results": {
                "type": "integer",
                "description": "Максимум результатов (по умолчанию 10)",
                "default": 10,
            },
        },
        "required": [],
    },
}

GET_HIDDEN_GEM_TOOL = {
    "name": "get_hidden_gem",
    "description": (
        "Генерирует персонализированную рекомендацию 'скрытой жемчужины' — "
        "малоизвестного фильма, который идеально подойдёт пользователю. "
        "Анализирует профиль и историю просмотров. "
        "Критерии: не блокбастер, не топ-250 IMDb, соответствует вкусам. "
        "Используй когда пользователь просит что-то необычное, неизвестное, не мейнстрим."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
        },
        "required": ["user_id"],
    },
}

GET_DIRECTOR_UPCOMING_TOOL = {
    "name": "get_director_upcoming",
    "description": (
        "Получение списка предстоящих фильмов режиссёра. "
        "Показывает анонсированные проекты в производстве или пост-продакшене."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "director_name": {
                "type": "string",
                "description": "Имя режиссёра для поиска",
            },
        },
        "required": ["director_name"],
    },
}


# =============================================================================
# Memory System Tools (MemGPT-style)
# =============================================================================

READ_CORE_MEMORY_TOOL = {
    "name": "read_core_memory",
    "description": (
        "Чтение core memory блоков пользователя. Core memory — это структурированная память, "
        "которая всегда доступна в контексте разговора. Блоки:\n"
        "- identity: базовая информация (имя, язык, дата регистрации)\n"
        "- preferences: предпочтения по контенту (качество, язык, жанры)\n"
        "- watch_context: контекст просмотра (оборудование, с кем смотрит)\n"
        "- active_context: текущий контекст (что сейчас смотрит, планы)\n"
        "- style: стиль общения\n"
        "- instructions: явные инструкции пользователя\n"
        "- blocklist: что не рекомендовать\n"
        "- learnings: автоматически выявленные паттерны\n\n"
        "Используй этот инструмент, чтобы проверить, что уже известно о пользователе, "
        "ПЕРЕД добавлением новой информации."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "block_name": {
                "type": "string",
                "description": "Конкретный блок для чтения. Если не указан, возвращает все блоки.",
                "enum": [
                    "identity",
                    "preferences",
                    "watch_context",
                    "active_context",
                    "style",
                    "instructions",
                    "blocklist",
                    "learnings",
                ],
            },
        },
        "required": ["user_id"],
    },
}

UPDATE_CORE_MEMORY_TOOL = {
    "name": "update_core_memory",
    "description": (
        "Обновление блока core memory пользователя.\n\n"
        "КОГДА обновлять:\n"
        "- Явная просьба: 'Запомни, что я ненавижу хорроры'\n"
        "- Значительное изменение: 'Купил 4K телевизор'\n"
        "- Активный контекст: 'Начал смотреть Breaking Bad'\n\n"
        "КОГДА НЕ обновлять:\n"
        "- Одноразовые предпочтения: 'сегодня хочу комедию'\n"
        "- Уже сохранённая информация (сначала проверь через read_core_memory)\n"
        "- Временный контекст без значения\n\n"
        "ВАЖНО: Для блоков instructions и blocklist СПРОСИ подтверждение: "
        "'Запомнить это как постоянное правило?'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "block_name": {
                "type": "string",
                "description": "Блок для обновления",
                "enum": [
                    "preferences",
                    "watch_context",
                    "active_context",
                    "style",
                    "instructions",
                    "blocklist",
                ],
            },
            "content": {
                "type": "string",
                "description": "Новое содержимое блока",
            },
            "operation": {
                "type": "string",
                "description": "Тип операции: replace (заменить), append (добавить), merge (объединить без дублей)",
                "enum": ["replace", "append", "merge"],
                "default": "replace",
            },
        },
        "required": ["user_id", "block_name", "content"],
    },
}

SEARCH_MEMORY_NOTES_TOOL = {
    "name": "search_memory_notes",
    "description": (
        "Поиск по recall memory — заметкам из прошлых разговоров, "
        "импортированным данным Letterboxd, выявленным паттернам. "
        "Используй для поиска информации, которая могла быть упомянута ранее."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "query": {
                "type": "string",
                "description": "Поисковый запрос (по ключевым словам или содержимому)",
            },
            "limit": {
                "type": "integer",
                "description": "Максимум результатов",
                "default": 10,
            },
        },
        "required": ["user_id", "query"],
    },
}

CREATE_MEMORY_NOTE_TOOL = {
    "name": "create_memory_note",
    "description": (
        "Создание заметки в recall memory. Используй для сохранения важной информации "
        "из разговора, которая не подходит для core memory блоков.\n"
        "Примеры: 'Упоминал, что смотрит фильмы с женой по выходным', "
        "'Любит фильмы про путешествия во времени'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "integer",
                "description": "Internal user ID",
            },
            "content": {
                "type": "string",
                "description": "Содержимое заметки",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ключевые слова для поиска",
            },
        },
        "required": ["user_id", "content"],
    },
}


# =============================================================================
# Collection of all tools
# =============================================================================

ALL_TOOLS: list[dict[str, Any]] = [
    # Core search tools
    RUTRACKER_SEARCH_TOOL,
    PIRATEBAY_SEARCH_TOOL,
    TMDB_SEARCH_TOOL,
    TMDB_PERSON_SEARCH_TOOL,
    TMDB_CREDITS_TOOL,
    TMDB_TV_DETAILS_TOOL,
    TMDB_BATCH_ENTITY_SEARCH_TOOL,
    KINOPOISK_SEARCH_TOOL,
    # Memory system tools (replaced legacy profile tools)
    READ_CORE_MEMORY_TOOL,
    UPDATE_CORE_MEMORY_TOOL,
    SEARCH_MEMORY_NOTES_TOOL,
    CREATE_MEMORY_NOTE_TOOL,
    # Download tools
    SEEDBOX_DOWNLOAD_TOOL,
    # Watchlist tools
    ADD_TO_WATCHLIST_TOOL,
    REMOVE_FROM_WATCHLIST_TOOL,
    GET_WATCHLIST_TOOL,
    # Watch history & ratings
    MARK_WATCHED_TOOL,
    RATE_CONTENT_TOOL,
    GET_WATCH_HISTORY_TOOL,
    # Blocklist tools
    ADD_TO_BLOCKLIST_TOOL,
    GET_BLOCKLIST_TOOL,
    # Monitoring tools
    CREATE_MONITOR_TOOL,
    GET_MONITORS_TOOL,
    CANCEL_MONITOR_TOOL,
    # Analytics tools
    GET_CREW_STATS_TOOL,
    # External service sync
    LETTERBOXD_SYNC_TOOL,
    # Proactive features
    GET_INDUSTRY_NEWS_TOOL,
    GET_RECENT_NEWS_TOOL,
    GET_HIDDEN_GEM_TOOL,
    GET_DIRECTOR_UPCOMING_TOOL,
    # Web search for current information
    WEB_SEARCH_TOOL,
]

# Legacy profile tools kept for backward compatibility (handler registration)
# but no longer sent to Claude API to reduce token usage.
LEGACY_PROFILE_TOOLS: list[dict[str, Any]] = [
    GET_USER_PROFILE_TOOL,
    READ_USER_PROFILE_TOOL,
    UPDATE_USER_PROFILE_TOOL,
]


def get_tool_definitions() -> list[dict[str, Any]]:
    """Get all tool definitions for Claude API.

    Returns:
        List of tool definitions in Anthropic format.
    """
    return ALL_TOOLS.copy()


def get_tool_by_name(name: str) -> dict[str, Any] | None:
    """Get a specific tool definition by name.

    Args:
        name: Name of the tool to retrieve.

    Returns:
        Tool definition dict or None if not found.
    """
    for tool in ALL_TOOLS:
        if tool["name"] == name:
            return tool
    return None


# =============================================================================
# Tool Executor
# =============================================================================


class ToolExecutor:
    """Executes tool calls by routing to appropriate handlers.

    The executor maintains a registry of handler functions for each tool.
    When a tool call is made, it validates the input and routes to the
    correct handler.

    Example:
        executor = ToolExecutor()
        executor.register_handler("tmdb_search", tmdb_search_handler)
        result = await executor.execute("tmdb_search", {"query": "Dune"})
    """

    def __init__(self) -> None:
        """Initialize the tool executor with empty handler registry."""
        self._handlers: dict[str, ToolHandler] = {}
        logger.info("tool_executor_initialized")

    def register_handler(self, tool_name: str, handler: ToolHandler) -> None:
        """Register a handler function for a tool.

        Args:
            tool_name: Name of the tool (must match tool definition name).
            handler: Async function that takes input dict and returns string result.
        """
        if get_tool_by_name(tool_name) is None:
            logger.warning(
                "registering_unknown_tool",
                tool_name=tool_name,
            )

        self._handlers[tool_name] = handler
        logger.debug(
            "tool_handler_registered",
            tool_name=tool_name,
        )

    def register_handlers(self, handlers: dict[str, ToolHandler]) -> None:
        """Register multiple handlers at once.

        Args:
            handlers: Dict mapping tool names to handler functions.
        """
        for tool_name, handler in handlers.items():
            self.register_handler(tool_name, handler)

    def has_handler(self, tool_name: str) -> bool:
        """Check if a handler is registered for a tool.

        Args:
            tool_name: Name of the tool.

        Returns:
            True if handler exists, False otherwise.
        """
        return tool_name in self._handlers

    def get_registered_tools(self) -> list[str]:
        """Get list of all registered tool names.

        Returns:
            List of tool names with registered handlers.
        """
        return list(self._handlers.keys())

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a tool call.

        Args:
            tool_name: Name of the tool to execute.
            tool_input: Input parameters for the tool.

        Returns:
            String result from the tool execution.

        Raises:
            ValueError: If no handler is registered for the tool.
            Exception: Any exception raised by the handler.
        """
        logger.info(
            "executing_tool",
            tool_name=tool_name,
            input_keys=list(tool_input.keys()),
        )

        if tool_name not in self._handlers:
            error_msg = f"No handler registered for tool: {tool_name}"
            logger.error(
                "tool_handler_not_found",
                tool_name=tool_name,
            )
            raise ValueError(error_msg)

        handler = self._handlers[tool_name]

        try:
            result = await handler(tool_input)
            logger.info(
                "tool_execution_success",
                tool_name=tool_name,
                result_length=len(result),
            )
            return result
        except Exception as e:
            logger.error(
                "tool_execution_failed",
                tool_name=tool_name,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    async def __call__(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Make executor callable for use with ClaudeClient.

        This allows the executor to be passed directly to ClaudeClient
        as the tool_executor parameter.

        Args:
            tool_name: Name of the tool to execute.
            tool_input: Input parameters for the tool.

        Returns:
            String result from the tool execution.
        """
        return await self.execute(tool_name, tool_input)


# =============================================================================
# Stub handlers for tools (actual implementations in separate modules)
# =============================================================================


async def stub_handler(tool_input: dict[str, Any]) -> str:
    """Stub handler for testing - returns input as JSON.

    Args:
        tool_input: Input parameters.

    Returns:
        JSON representation of the input with stub marker.
    """
    return json.dumps(
        {
            "status": "stub",
            "message": "Handler not implemented yet",
            "received_input": tool_input,
        },
        ensure_ascii=False,
    )


def create_executor_with_stubs() -> ToolExecutor:
    """Create a ToolExecutor with stub handlers for all tools.

    Useful for testing the integration before actual handlers are implemented.

    Returns:
        ToolExecutor with stub handlers registered.
    """
    executor = ToolExecutor()
    for tool in ALL_TOOLS:
        executor.register_handler(tool["name"], stub_handler)
    return executor


# =============================================================================
# Tool validation helpers
# =============================================================================


def validate_tool_input(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Validate tool input against its JSON schema.

    Args:
        tool_name: Name of the tool.
        tool_input: Input parameters to validate.

    Returns:
        List of validation error messages (empty if valid).
    """
    tool = get_tool_by_name(tool_name)
    if tool is None:
        return [f"Unknown tool: {tool_name}"]

    schema = tool.get("input_schema", {})
    errors: list[str] = []

    # Check required fields
    required = schema.get("required", [])
    for field in required:
        if field not in tool_input:
            errors.append(f"Missing required field: {field}")

    # Check field types
    properties = schema.get("properties", {})
    for field, value in tool_input.items():
        if field not in properties:
            continue  # Allow extra fields

        prop_schema = properties[field]
        expected_type = prop_schema.get("type")

        if expected_type == "string" and not isinstance(value, str):
            errors.append(f"Field '{field}' must be a string")
        elif expected_type == "integer" and not isinstance(value, int):
            errors.append(f"Field '{field}' must be an integer")
        elif expected_type == "boolean" and not isinstance(value, bool):
            errors.append(f"Field '{field}' must be a boolean")

        # Check enum values
        if "enum" in prop_schema and value not in prop_schema["enum"]:
            errors.append(f"Field '{field}' must be one of: {', '.join(prop_schema['enum'])}")

    return errors
