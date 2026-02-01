"""Entity card formatting for Telegram.

This module provides functions to format person, movie, and TV show cards
with photos and captions for display in Telegram when users click entity links.
"""

import structlog

from src.config import settings
from src.media.tmdb import TMDBClient

logger = structlog.get_logger(__name__)

BOT_USERNAME = settings.bot_username


def _person_link(name: str, person_id: int) -> str:
    """Format a person as a clickable link."""
    return f'<a href="https://t.me/{BOT_USERNAME}?start=p_{person_id}">{name}</a>'


def _movie_link(title: str, movie_id: int) -> str:
    """Format a movie as a clickable link."""
    return f'<a href="https://t.me/{BOT_USERNAME}?start=m_{movie_id}">{title}</a>'


def _tv_link(name: str, tv_id: int) -> str:
    """Format a TV show as a clickable link."""
    return f'<a href="https://t.me/{BOT_USERNAME}?start=t_{tv_id}">{name}</a>'


async def format_person_card(person_id: int) -> tuple[str, str | None]:
    """Format a person card with photo URL and caption.

    Args:
        person_id: TMDB person ID

    Returns:
        Tuple of (caption, photo_url). photo_url may be None if no image available.

    Raises:
        TMDBNotFoundError: Person not found
    """
    async with TMDBClient() as tmdb:
        person = await tmdb.get_person(person_id)
        credits = await tmdb.get_person_movie_credits(person_id)

    # Prioritize work based on what the person is known for
    known_for = person.get("known_for_department", "Acting")
    cast_works = credits.get("cast", [])
    crew_works = credits.get("crew", [])

    # For directors/writers/producers, prioritize crew work; for actors, prioritize cast
    if known_for in ("Directing", "Writing", "Production"):
        # Filter crew to main roles (Director, Writer, Producer, etc.)
        main_crew = [
            w
            for w in crew_works
            if w.get("job")
            in ("Director", "Writer", "Screenplay", "Producer", "Executive Producer")
        ]
        primary_works = main_crew if main_crew else crew_works
        secondary_works = cast_works
    else:
        primary_works = cast_works
        secondary_works = crew_works

    # Combine with primary first, deduplicate by movie ID
    all_works = primary_works + secondary_works
    seen_ids: set[int] = set()
    unique_works = []
    for work in all_works:
        work_id = work.get("id")
        if work_id and work_id not in seen_ids:
            seen_ids.add(work_id)
            unique_works.append(work)

    top_movies = sorted(unique_works, key=lambda x: x.get("popularity", 0), reverse=True)[:5]

    # Build caption
    caption = f"<b>{person['name']}</b>\n"

    if person.get("known_for_department"):
        dept_ru = {
            "Acting": "Актёр",
            "Directing": "Режиссёр",
            "Writing": "Сценарист",
            "Production": "Продюсер",
            "Camera": "Оператор",
            "Editing": "Монтажёр",
            "Sound": "Звук",
            "Art": "Художник",
            "Crew": "Съёмочная группа",
        }.get(person["known_for_department"], person["known_for_department"])
        caption += f"{dept_ru}\n"

    if person.get("birthday"):
        birthday = person["birthday"]
        if person.get("deathday"):
            caption += f"{birthday} — {person['deathday']}\n"
        else:
            caption += f"{birthday}\n"

    if person.get("place_of_birth"):
        caption += f"{person['place_of_birth']}\n"

    if person.get("biography"):
        bio = person["biography"]
        if len(bio) > 400:
            bio = bio[:400].rsplit(" ", 1)[0] + "..."
        caption += f"\n{bio}\n"

    if top_movies:
        caption += "\n<b>Известные работы:</b>\n"
        for m in top_movies:
            title = m.get("title", "")
            movie_id = m.get("id")
            year = m.get("release_date", "")[:4] or "?"
            role = m.get("character") or m.get("job") or ""
            # Make movie title a clickable link
            title_text = _movie_link(title, movie_id) if movie_id else title
            if role:
                caption += f"• {title_text} ({year}) — {role}\n"
            else:
                caption += f"• {title_text} ({year})\n"

    # Get photo URL
    photo_url = None
    if person.get("profile_path"):
        photo_url = f"https://image.tmdb.org/t/p/w500{person['profile_path']}"

    logger.info("person_card_formatted", person_id=person_id, name=person["name"])
    return caption, photo_url


async def format_movie_card(movie_id: int) -> tuple[str, str | None]:
    """Format a movie card with poster URL and caption.

    Args:
        movie_id: TMDB movie ID

    Returns:
        Tuple of (caption, photo_url). photo_url may be None if no poster available.

    Raises:
        TMDBNotFoundError: Movie not found
    """
    async with TMDBClient() as tmdb:
        movie = await tmdb.get_movie(movie_id)
        credits = await tmdb.get_movie_credits(movie_id)

    directors = credits.get_directors()
    cast = credits.get_top_cast(5)

    # Build caption
    caption = f"<b>{movie.title}</b>"
    if movie.get_year():
        caption += f" ({movie.get_year()})"
    caption += "\n"

    if movie.vote_average:
        caption += f"TMDB: {movie.vote_average:.1f}/10\n"

    if movie.runtime:
        hours = movie.runtime // 60
        mins = movie.runtime % 60
        if hours > 0:
            caption += f"{hours} ч {mins} мин\n"
        else:
            caption += f"{mins} мин\n"

    genres = movie.get_genre_names()[:3]
    if genres:
        caption += f"{', '.join(genres)}\n"

    if directors:
        director_links = ", ".join(_person_link(d.name, d.id) for d in directors[:2])
        caption += f"\n<b>Режиссёр:</b> {director_links}\n"

    if cast:
        actor_links = ", ".join(_person_link(a.name, a.id) for a in cast[:4])
        caption += f"<b>В ролях:</b> {actor_links}\n"

    if movie.overview:
        overview = movie.overview
        if len(overview) > 400:
            overview = overview[:400].rsplit(" ", 1)[0] + "..."
        caption += f"\n{overview}"

    logger.info("movie_card_formatted", movie_id=movie_id, title=movie.title)
    return caption, movie.get_poster_url("w500")


async def format_tv_card(tv_id: int) -> tuple[str, str | None]:
    """Format a TV show card with poster URL and caption.

    Args:
        tv_id: TMDB TV show ID

    Returns:
        Tuple of (caption, photo_url). photo_url may be None if no poster available.

    Raises:
        TMDBNotFoundError: TV show not found
    """
    async with TMDBClient() as tmdb:
        show = await tmdb.get_tv_show(tv_id)
        credits = await tmdb.get_tv_credits(tv_id)

    cast = credits.get_top_cast(5)

    # Build caption
    caption = f"<b>{show.name}</b>"
    if show.get_year():
        caption += f" ({show.get_year()})"
    caption += "\n"

    if show.vote_average:
        caption += f"TMDB: {show.vote_average:.1f}/10\n"

    # Seasons and episodes info
    seasons_text = f"{show.number_of_seasons} сезон"
    if show.number_of_seasons > 1 and show.number_of_seasons < 5:
        seasons_text = f"{show.number_of_seasons} сезона"
    elif show.number_of_seasons >= 5:
        seasons_text = f"{show.number_of_seasons} сезонов"

    episodes_text = f"{show.number_of_episodes} серий"
    if show.number_of_episodes == 1:
        episodes_text = "1 серия"
    elif 2 <= show.number_of_episodes <= 4:
        episodes_text = f"{show.number_of_episodes} серии"

    caption += f"{seasons_text}, {episodes_text}\n"

    if show.status:
        status_ru = {
            "Returning Series": "Продолжается",
            "Ended": "Завершён",
            "Canceled": "Отменён",
            "In Production": "В производстве",
            "Planned": "Запланирован",
        }.get(show.status, show.status)
        caption += f"Статус: {status_ru}\n"

    genres = show.get_genre_names()[:3]
    if genres:
        caption += f"{', '.join(genres)}\n"

    if cast:
        actor_links = ", ".join(_person_link(a.name, a.id) for a in cast[:4])
        caption += f"\n<b>В ролях:</b> {actor_links}\n"

    if show.overview:
        overview = show.overview
        if len(overview) > 400:
            overview = overview[:400].rsplit(" ", 1)[0] + "..."
        caption += f"\n{overview}"

    logger.info("tv_card_formatted", tv_id=tv_id, name=show.name)
    return caption, show.get_poster_url("w500")
