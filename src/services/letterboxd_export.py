"""Letterboxd export CSV parser.

Parses the ZIP export from Letterboxd (Settings → Import & Export → Export Your Data)
to extract user's film history, ratings, and reviews for profile building.

Export contains:
- ratings.csv: Date, Name, Year, Letterboxd URI, Rating (0.5-5.0)
- watched.csv: Date, Name, Year, Letterboxd URI
- reviews.csv: Date, Name, Year, Letterboxd URI, Rating, Review
- diary.csv: Date, Name, Year, Letterboxd URI, Rating, Rewatch, Tags, Watched Date
- watchlist.csv: Date, Name, Year, Letterboxd URI
"""

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class LetterboxdFilmEntry:
    """Single film entry from Letterboxd export."""

    name: str
    year: int | None
    letterboxd_uri: str
    rating: float | None = None  # 0.5 to 5.0
    review: str | None = None
    watched_date: datetime | None = None
    logged_date: datetime | None = None
    rewatch: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass
class LetterboxdExportAnalysis:
    """Analysis results from Letterboxd export."""

    total_watched: int = 0
    total_rated: int = 0
    total_reviews: int = 0

    # Films by rating
    favorites: list[LetterboxdFilmEntry] = field(default_factory=list)  # 4.5-5.0
    loved: list[LetterboxdFilmEntry] = field(default_factory=list)  # 4.0
    liked: list[LetterboxdFilmEntry] = field(default_factory=list)  # 3.0-3.5
    disliked: list[LetterboxdFilmEntry] = field(default_factory=list)  # 1.0-2.0
    hated: list[LetterboxdFilmEntry] = field(default_factory=list)  # 0.5

    # Review samples for style analysis
    review_samples: list[str] = field(default_factory=list)

    # Computed stats
    average_rating: float | None = None
    rating_distribution: dict[str, int] = field(default_factory=dict)

    # Watchlist
    watchlist: list[LetterboxdFilmEntry] = field(default_factory=list)


class LetterboxdExportParser:
    """Parser for Letterboxd data export ZIP files."""

    def __init__(self):
        self.films: dict[str, LetterboxdFilmEntry] = {}  # uri -> entry

    def parse_zip(self, zip_data: bytes) -> LetterboxdExportAnalysis:
        """Parse Letterboxd export ZIP file.

        Args:
            zip_data: Raw bytes of the ZIP file

        Returns:
            Analysis of the user's film history
        """
        logger.info("parsing_letterboxd_export", size=len(zip_data))

        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                file_list = zf.namelist()
                logger.info("export_files", files=file_list)

                # Parse in order: watched, ratings, reviews, diary
                if "watched.csv" in file_list:
                    self._parse_watched(zf.read("watched.csv").decode("utf-8"))

                if "ratings.csv" in file_list:
                    self._parse_ratings(zf.read("ratings.csv").decode("utf-8"))

                if "reviews.csv" in file_list:
                    self._parse_reviews(zf.read("reviews.csv").decode("utf-8"))

                if "diary.csv" in file_list:
                    self._parse_diary(zf.read("diary.csv").decode("utf-8"))

                if "watchlist.csv" in file_list:
                    self._parse_watchlist(zf.read("watchlist.csv").decode("utf-8"))

        except zipfile.BadZipFile as e:
            logger.error("invalid_zip", error=str(e))
            raise ValueError("Invalid ZIP file") from e

        return self._analyze()

    def _parse_watched(self, csv_content: str) -> None:
        """Parse watched.csv."""
        reader = csv.DictReader(io.StringIO(csv_content))
        count = 0

        for row in reader:
            uri = row.get("Letterboxd URI", "")
            if not uri:
                continue

            if uri not in self.films:
                self.films[uri] = LetterboxdFilmEntry(
                    name=row.get("Name", ""),
                    year=self._parse_year(row.get("Year")),
                    letterboxd_uri=uri,
                    logged_date=self._parse_date(row.get("Date")),
                )
            count += 1

        logger.info("parsed_watched", count=count)

    def _parse_ratings(self, csv_content: str) -> None:
        """Parse ratings.csv."""
        reader = csv.DictReader(io.StringIO(csv_content))
        count = 0

        for row in reader:
            uri = row.get("Letterboxd URI", "")
            if not uri:
                continue

            rating = self._parse_rating(row.get("Rating"))

            if uri in self.films:
                self.films[uri].rating = rating
            else:
                self.films[uri] = LetterboxdFilmEntry(
                    name=row.get("Name", ""),
                    year=self._parse_year(row.get("Year")),
                    letterboxd_uri=uri,
                    rating=rating,
                    logged_date=self._parse_date(row.get("Date")),
                )
            count += 1

        logger.info("parsed_ratings", count=count)

    def _parse_reviews(self, csv_content: str) -> None:
        """Parse reviews.csv."""
        reader = csv.DictReader(io.StringIO(csv_content))
        count = 0

        for row in reader:
            uri = row.get("Letterboxd URI", "")
            if not uri:
                continue

            review = row.get("Review", "")

            if uri in self.films:
                self.films[uri].review = review
                if not self.films[uri].rating:
                    self.films[uri].rating = self._parse_rating(row.get("Rating"))
            else:
                self.films[uri] = LetterboxdFilmEntry(
                    name=row.get("Name", ""),
                    year=self._parse_year(row.get("Year")),
                    letterboxd_uri=uri,
                    rating=self._parse_rating(row.get("Rating")),
                    review=review,
                    logged_date=self._parse_date(row.get("Date")),
                )
            count += 1

        logger.info("parsed_reviews", count=count)

    def _parse_diary(self, csv_content: str) -> None:
        """Parse diary.csv."""
        reader = csv.DictReader(io.StringIO(csv_content))
        count = 0

        for row in reader:
            uri = row.get("Letterboxd URI", "")
            if not uri:
                continue

            watched_date = self._parse_date(row.get("Watched Date"))
            rewatch = row.get("Rewatch", "").lower() == "yes"
            tags = [t.strip() for t in row.get("Tags", "").split(",") if t.strip()]

            if uri in self.films:
                self.films[uri].watched_date = watched_date
                self.films[uri].rewatch = rewatch
                self.films[uri].tags = tags
                if not self.films[uri].rating:
                    self.films[uri].rating = self._parse_rating(row.get("Rating"))
            else:
                self.films[uri] = LetterboxdFilmEntry(
                    name=row.get("Name", ""),
                    year=self._parse_year(row.get("Year")),
                    letterboxd_uri=uri,
                    rating=self._parse_rating(row.get("Rating")),
                    watched_date=watched_date,
                    rewatch=rewatch,
                    tags=tags,
                    logged_date=self._parse_date(row.get("Date")),
                )
            count += 1

        logger.info("parsed_diary", count=count)

    def _parse_watchlist(self, csv_content: str) -> None:
        """Parse watchlist.csv."""
        reader = csv.DictReader(io.StringIO(csv_content))
        count = 0

        for row in reader:
            uri = row.get("Letterboxd URI", "")
            if not uri:
                continue

            # Store watchlist separately, don't mix with watched films
            if uri not in self.films:
                self.films[uri] = LetterboxdFilmEntry(
                    name=row.get("Name", ""),
                    year=self._parse_year(row.get("Year")),
                    letterboxd_uri=uri,
                    logged_date=self._parse_date(row.get("Date")),
                )
            # Mark as watchlist item by not having a watched date
            count += 1

        logger.info("parsed_watchlist", count=count)

    def _parse_year(self, year_str: str | None) -> int | None:
        """Parse year string to int."""
        if not year_str:
            return None
        try:
            return int(year_str)
        except ValueError:
            return None

    def _parse_rating(self, rating_str: str | None) -> float | None:
        """Parse rating string to float."""
        if not rating_str:
            return None
        try:
            return float(rating_str)
        except ValueError:
            return None

    def _parse_date(self, date_str: str | None) -> datetime | None:
        """Parse date string."""
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return None

    def _analyze(self) -> LetterboxdExportAnalysis:
        """Analyze parsed films."""
        analysis = LetterboxdExportAnalysis()

        rated_films = []

        for film in self.films.values():
            # Count watched (has logged_date or watched_date)
            if film.logged_date or film.watched_date:
                analysis.total_watched += 1

            # Count and categorize ratings
            if film.rating is not None:
                analysis.total_rated += 1
                rated_films.append(film)

                # Categorize by rating
                if film.rating >= 4.5:
                    analysis.favorites.append(film)
                elif film.rating >= 4.0:
                    analysis.loved.append(film)
                elif film.rating >= 3.0:
                    analysis.liked.append(film)
                elif film.rating >= 1.5:
                    analysis.disliked.append(film)
                else:  # 0.5 - 1.0
                    analysis.hated.append(film)

                # Rating distribution
                rating_key = str(film.rating)
                analysis.rating_distribution[rating_key] = (
                    analysis.rating_distribution.get(rating_key, 0) + 1
                )

            # Collect reviews
            if film.review:
                analysis.total_reviews += 1
                # Store samples for style analysis (max 20)
                if len(analysis.review_samples) < 20:
                    analysis.review_samples.append(film.review)

        # Sort by rating (highest first for favorites, lowest first for disliked)
        analysis.favorites.sort(key=lambda f: (f.rating or 0, f.name), reverse=True)
        analysis.loved.sort(key=lambda f: (f.rating or 0, f.name), reverse=True)
        analysis.hated.sort(key=lambda f: (f.rating or 0, f.name))
        analysis.disliked.sort(key=lambda f: (f.rating or 0, f.name))

        # Calculate average rating
        if rated_films:
            analysis.average_rating = sum(f.rating for f in rated_films if f.rating) / len(
                rated_films
            )

        # Build watchlist (films without watched_date or logged_date that suggest watching)
        # Actually, watchlist.csv items are separate, we need to track them differently
        # For now, films that were in watchlist.csv but not watched
        for film in self.films.values():
            if not film.watched_date and not film.rating and not film.review:
                # Likely a watchlist item
                analysis.watchlist.append(film)

        logger.info(
            "analysis_complete",
            total_watched=analysis.total_watched,
            total_rated=analysis.total_rated,
            favorites=len(analysis.favorites),
            hated=len(analysis.hated),
            reviews=analysis.total_reviews,
        )

        return analysis


def format_analysis_for_profile(analysis: LetterboxdExportAnalysis) -> str:
    """Format analysis results for user profile.

    Args:
        analysis: Letterboxd export analysis

    Returns:
        Markdown-formatted section for profile
    """
    sections = []

    # Stats
    sections.append(
        f"**Letterboxd Stats:** {analysis.total_watched} watched, {analysis.total_rated} rated"
    )
    if analysis.average_rating:
        sections.append(f"**Average Rating:** {analysis.average_rating:.1f}/5")

    # Top favorites (limit to 10)
    if analysis.favorites:
        top_favorites = analysis.favorites[:10]
        films_list = ", ".join(f"{f.name} ({f.year})" if f.year else f.name for f in top_favorites)
        sections.append(f"**Favorites (4.5-5 stars):** {films_list}")

    # Loved films (limit to 10)
    if analysis.loved:
        top_loved = analysis.loved[:10]
        films_list = ", ".join(f"{f.name} ({f.year})" if f.year else f.name for f in top_loved)
        sections.append(f"**Highly Rated (4 stars):** {films_list}")

    # Disliked/hated (important for recommendations)
    if analysis.hated or analysis.disliked:
        disliked_all = analysis.hated + analysis.disliked[:5]
        films_list = ", ".join(
            f"{f.name} ({f.year})" if f.year else f.name for f in disliked_all[:10]
        )
        sections.append(f"**Disliked:** {films_list}")

    return "\n".join(sections)


def extract_review_style(reviews: list[str]) -> str | None:
    """Analyze review samples to describe writing style.

    Args:
        reviews: List of review texts

    Returns:
        Description of review style or None
    """
    if not reviews:
        return None

    # Basic analysis
    total_length = sum(len(r) for r in reviews)
    avg_length = total_length / len(reviews)

    # Detect language (simple heuristic)
    russian_chars = sum(1 for r in reviews for c in r if "\u0400" <= c <= "\u04ff")
    english_chars = sum(1 for r in reviews for c in r if c.isascii() and c.isalpha())

    language = "Russian" if russian_chars > english_chars else "English"

    # Describe style
    if avg_length < 100:
        length_desc = "short, concise"
    elif avg_length < 500:
        length_desc = "medium-length"
    else:
        length_desc = "detailed, lengthy"

    return (
        f"{language}, {length_desc} reviews ({len(reviews)} samples, avg {int(avg_length)} chars)"
    )
