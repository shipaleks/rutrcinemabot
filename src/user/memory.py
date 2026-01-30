"""Memory management system for user profiles.

This module implements a MemGPT-style memory hierarchy:
- Core Memory: In-context blocks with character limits (always loaded)
- Recall Memory: Searchable session summaries and notes (on-demand)
- Archival Memory: Long-term storage with automatic pruning

Key Components:
- CoreMemoryManager: Manages structured memory blocks with compaction
- SessionManager: Tracks conversation sessions with timeout detection
- LearningDetector: Extracts patterns from user behavior
- MemoryArchiver: Handles automatic memory archival

Reference: https://memgpt.ai/ - Memory hierarchy and auto-compaction patterns
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from src.user.storage import (
    CORE_MEMORY_BLOCKS,
    ConversationSession,
    CoreMemoryBlock,
    MemoryNote,
)

if TYPE_CHECKING:
    from src.services.letterboxd_export import LetterboxdExportAnalysis
    from src.user.storage import BaseStorage, Preference, User, WatchedItem

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

SESSION_TIMEOUT_MINUTES = 30
COMPACTION_THRESHOLD = 0.70  # Trigger summarization at 70% capacity
LEARNING_MIN_FILMS = 5  # Minimum films by director to detect pattern
HIGH_RATING_THRESHOLD = 7.0  # Rating considered "high"


# =============================================================================
# Core Memory Manager
# =============================================================================


class CoreMemoryManager:
    """Manages core memory blocks with automatic compaction.

    Core memory blocks are always in-context during conversations.
    Each block has a character limit and is automatically truncated/summarized
    when approaching capacity.

    Blocks:
    - identity: User's basic info (system-managed)
    - preferences: Content preferences (agent-editable)
    - watch_context: Equipment, partners (agent-editable)
    - active_context: Currently watching, temporary context (auto-expire)
    - style: Communication preferences (agent-editable)
    - instructions: Explicit user rules (confirm before update)
    - blocklist: What to avoid (confirm before update)
    - learnings: Auto-detected patterns (system-managed)
    """

    def __init__(self, storage: "BaseStorage"):
        """Initialize core memory manager.

        Args:
            storage: Storage backend instance
        """
        self._storage = storage

    async def initialize_for_user(
        self,
        user: "User",
        preferences: "Preference | None" = None,
    ) -> list[CoreMemoryBlock]:
        """Initialize core memory blocks for a user.

        Creates empty blocks and populates identity from user data.

        Args:
            user: User object
            preferences: Optional preferences for initial content

        Returns:
            List of initialized blocks
        """
        await self._storage.initialize_core_memory_blocks(user.id)

        # Populate identity block
        identity_content = self._build_identity_content(user)
        await self._storage.update_core_memory_block(user.id, "identity", identity_content)

        # Populate preferences block if available
        if preferences:
            prefs_content = self._build_preferences_content(preferences)
            await self._storage.update_core_memory_block(user.id, "preferences", prefs_content)

        logger.info("core_memory_initialized", user_id=user.id)
        return await self._storage.get_all_core_memory_blocks(user.id)

    async def get_all_blocks(self, user_id: int) -> list[CoreMemoryBlock]:
        """Get all core memory blocks for a user.

        Args:
            user_id: Internal user ID

        Returns:
            List of all memory blocks
        """
        return await self._storage.get_all_core_memory_blocks(user_id)

    async def get_block(self, user_id: int, block_name: str) -> CoreMemoryBlock | None:
        """Get a specific memory block.

        Args:
            user_id: Internal user ID
            block_name: Name of the block

        Returns:
            Memory block or None if not found
        """
        return await self._storage.get_core_memory_block(user_id, block_name)

    def render_blocks_for_context(self, blocks: list[CoreMemoryBlock]) -> str:
        """Render memory blocks as markdown for Claude's context.

        Args:
            blocks: List of core memory blocks

        Returns:
            Markdown-formatted string for system prompt
        """
        if not blocks:
            return ""

        sections = ["## Core Memory (User Profile)\n"]

        # Map block names to display titles
        block_titles = {
            "identity": "Identity",
            "preferences": "Preferences",
            "watch_context": "Watch Context",
            "active_context": "Active Context",
            "style": "Communication Style",
            "instructions": "User Instructions",
            "blocklist": "Blocklist",
            "learnings": "Learned Patterns",
        }

        for block in blocks:
            if not block.content:
                continue

            title = block_titles.get(block.block_name, block.block_name.title())
            sections.append(f"### {title}\n{block.content}\n")

        return "\n".join(sections)

    async def update_block(
        self,
        user_id: int,
        block_name: str,
        content: str,
        operation: str = "replace",
    ) -> CoreMemoryBlock:
        """Update a memory block.

        Args:
            user_id: Internal user ID
            block_name: Name of the block to update
            content: New content
            operation: One of "replace", "append", "merge"

        Returns:
            Updated memory block

        Raises:
            ValueError: If block doesn't exist or operation not allowed
        """
        block_config = CORE_MEMORY_BLOCKS.get(block_name)
        if not block_config:
            raise ValueError(f"Unknown block: {block_name}")

        existing = await self._storage.get_core_memory_block(user_id, block_name)

        if operation == "replace":
            final_content = content
        elif operation == "append":
            existing_content = existing.content if existing else ""
            final_content = f"{existing_content}\n{content}".strip()

            # For learnings block, limit to 10 most recent entries
            if block_name == "learnings":
                lines = [line for line in final_content.split("\n") if line.strip()]
                if len(lines) > 10:
                    final_content = "\n".join(lines[-10:])  # Keep most recent 10
                    logger.debug("learnings_compacted", kept_entries=10)
        elif operation == "merge":
            # Deduplicate lines when merging
            existing_lines = set((existing.content if existing else "").split("\n"))
            new_lines = content.split("\n")
            merged = list(existing_lines)
            for line in new_lines:
                if line.strip() and line not in existing_lines:
                    merged.append(line)
            final_content = "\n".join(merged)
        else:
            raise ValueError(f"Unknown operation: {operation}")

        # Check if compaction needed
        max_chars = block_config["max_chars"]
        if len(final_content) > max_chars * COMPACTION_THRESHOLD:
            final_content = await self._compact_content(final_content, max_chars)

        return await self._storage.update_core_memory_block(user_id, block_name, final_content)

    async def _compact_content(self, content: str, max_chars: int) -> str:
        """Compact content to fit within limits.

        Simple strategy: truncate at word boundary.
        Future: Use Claude to summarize.

        Args:
            content: Content to compact
            max_chars: Maximum allowed characters

        Returns:
            Compacted content
        """
        if len(content) <= max_chars:
            return content

        # Simple truncation at word boundary
        truncated = content[: max_chars - 3]
        last_space = truncated.rfind(" ")
        if last_space > max_chars // 2:
            truncated = truncated[:last_space]

        return truncated + "..."

    def _build_identity_content(self, user: "User") -> str:
        """Build identity block content from user data.

        Args:
            user: User object

        Returns:
            Identity block content
        """
        parts = [f"Name: {user.display_name}"]

        if user.language_code:
            parts.append(f"Language: {user.language_code}")

        parts.append(f"Member since: {user.created_at.strftime('%Y-%m-%d')}")

        return "\n".join(parts)

    def _build_preferences_content(self, preferences: "Preference") -> str:
        """Build preferences block content.

        Args:
            preferences: User preferences

        Returns:
            Preferences block content
        """
        parts = []

        if preferences.video_quality:
            parts.append(f"Quality: {preferences.video_quality}")

        if preferences.audio_language:
            lang_map = {"ru": "Russian", "en": "English"}
            lang = lang_map.get(preferences.audio_language, preferences.audio_language)
            parts.append(f"Audio: {lang}")

        if preferences.subtitle_language:
            parts.append(f"Subtitles: {preferences.subtitle_language}")

        if preferences.preferred_genres:
            parts.append(f"Favorite genres: {', '.join(preferences.preferred_genres)}")

        if preferences.excluded_genres:
            parts.append(f"Avoid genres: {', '.join(preferences.excluded_genres)}")

        return "\n".join(parts) if parts else "No preferences set"

    async def render_for_prompt(self, user_id: int) -> str:
        """Render all core memory blocks as a string for system prompt.

        Args:
            user_id: Internal user ID

        Returns:
            Formatted string with all memory blocks
        """
        blocks = await self.get_all_blocks(user_id)

        if not blocks:
            return "No memory blocks initialized."

        parts = ["## Core Memory\n"]

        for block in blocks:
            if block.content.strip():
                # Title case and replace underscores
                title = block.block_name.replace("_", " ").title()
                usage = f"[{len(block.content)}/{block.max_chars}]"
                parts.append(f"### {title} {usage}")
                parts.append(block.content)
                parts.append("")

        return "\n".join(parts)


# =============================================================================
# Session Manager
# =============================================================================


class SessionManager:
    """Manages conversation sessions with automatic timeout detection.

    Sessions are automatically ended when:
    - No message received for 30 minutes (configurable)
    - User explicitly ends conversation

    On session end:
    - Summary is generated (optional)
    - Key learnings are extracted
    - Session is stored for recall
    """

    def __init__(self, storage: "BaseStorage"):
        """Initialize session manager.

        Args:
            storage: Storage backend instance
        """
        self._storage = storage

    async def get_or_create_session(self, user_id: int) -> ConversationSession:
        """Get active session or create new one.

        If the last message was more than SESSION_TIMEOUT_MINUTES ago,
        the old session is ended and a new one is created.

        Args:
            user_id: Internal user ID

        Returns:
            Active conversation session
        """
        active = await self._storage.get_active_session(user_id)

        if active:
            # Check if session has timed out
            cutoff = datetime.now(UTC) - timedelta(minutes=SESSION_TIMEOUT_MINUTES)

            # Use the session's last activity (approximate via started_at + message timing)
            # In a real implementation, we'd track last_message_at
            # For now, just use message_count as a proxy
            if active.started_at < cutoff and active.message_count > 0:
                # End the old session
                await self.end_session(
                    active.id,
                    summary=f"Session with {active.message_count} messages",
                )
                logger.info(
                    "session_timed_out",
                    user_id=user_id,
                    session_id=active.id,
                    message_count=active.message_count,
                )
                # Create new session
                return await self._storage.create_session(user_id)

            return active

        # No active session, create one
        return await self._storage.create_session(user_id)

    async def record_message(self, session_id: int) -> None:
        """Record that a message was sent in the session.

        Args:
            session_id: Session ID
        """
        await self._storage.increment_session_message_count(session_id)

    async def end_session(
        self,
        session_id: int,
        summary: str | None = None,
        key_learnings: list[str] | None = None,
    ) -> ConversationSession | None:
        """End a conversation session.

        Args:
            session_id: Session ID
            summary: Optional summary of the conversation
            key_learnings: Optional list of things learned

        Returns:
            Ended session or None if not found
        """
        return await self._storage.end_session(session_id, summary, key_learnings)

    async def get_recent_sessions(
        self,
        user_id: int,
        limit: int = 10,
        days: int = 30,
    ) -> list[ConversationSession]:
        """Get recent sessions for context.

        Args:
            user_id: Internal user ID
            limit: Maximum sessions to return
            days: Look back this many days

        Returns:
            List of recent sessions
        """
        return await self._storage.get_recent_sessions(user_id, limit, days)


# =============================================================================
# Learning Detector
# =============================================================================


class LearningDetector:
    """Detects patterns from user behavior and creates memory notes.

    Patterns detected:
    - Director affinity: 5+ films by same director with high ratings
    - Genre preferences: High average rating for a genre
    - Actor preferences: Frequently watched actors
    - Time patterns: When user typically watches
    """

    def __init__(self, storage: "BaseStorage"):
        """Initialize learning detector.

        Args:
            storage: Storage backend instance
        """
        self._storage = storage

    async def analyze_ratings(self, user_id: int) -> list[MemoryNote]:
        """Analyze user's ratings to detect crew patterns.

        Detects:
        - Director affinity: 5+ films by same director with high ratings
        - Actor preferences: 5+ films with same actor and high ratings
        - Cinematographer patterns: (if data available)

        Args:
            user_id: Internal user ID

        Returns:
            List of created memory notes
        """
        notes: list[MemoryNote] = []

        # 1. Analyze directors
        director_stats = await self._storage.get_crew_stats(
            user_id, role="director", min_films=LEARNING_MIN_FILMS
        )

        for stat in director_stats:
            if stat.avg_rating >= HIGH_RATING_THRESHOLD:
                content = f"Loves {stat.person_name}'s direction ({stat.films_count} films watched, avg rating {stat.avg_rating:.1f})"
                keywords = ["director", stat.person_name.lower(), "pattern"]

                # Check if we already have this note
                existing = await self._storage.search_memory_notes(
                    user_id, stat.person_name, limit=1
                )
                if not existing:
                    note = await self._storage.create_memory_note(
                        user_id=user_id,
                        content=content,
                        source="rating_pattern",
                        keywords=keywords,
                        confidence=0.8,  # High confidence for rating-based patterns
                    )
                    notes.append(note)
                    logger.info(
                        "learning_detected",
                        user_id=user_id,
                        pattern="director_affinity",
                        person=stat.person_name,
                    )

        # 2. Analyze actors
        actor_stats = await self._storage.get_crew_stats(
            user_id, role="actor", min_films=LEARNING_MIN_FILMS
        )

        for stat in actor_stats:
            if stat.avg_rating >= HIGH_RATING_THRESHOLD:
                content = f"Enjoys films with {stat.person_name} ({stat.films_count} films watched, avg rating {stat.avg_rating:.1f})"
                keywords = ["actor", stat.person_name.lower(), "pattern"]

                existing = await self._storage.search_memory_notes(
                    user_id, stat.person_name, limit=1
                )
                if not existing:
                    note = await self._storage.create_memory_note(
                        user_id=user_id,
                        content=content,
                        source="rating_pattern",
                        keywords=keywords,
                        confidence=0.75,  # Slightly lower confidence for actors
                    )
                    notes.append(note)
                    logger.info(
                        "learning_detected",
                        user_id=user_id,
                        pattern="actor_affinity",
                        person=stat.person_name,
                    )

        # 3. Analyze cinematographers (if we have enough data)
        try:
            cinematographer_stats = await self._storage.get_crew_stats(
                user_id,
                role="cinematographer",
                min_films=3,  # Lower threshold for rare role
            )

            for stat in cinematographer_stats:
                if stat.avg_rating >= HIGH_RATING_THRESHOLD:
                    content = f"Appreciates {stat.person_name}'s cinematography ({stat.films_count} films)"
                    keywords = ["cinematographer", stat.person_name.lower(), "pattern"]

                    existing = await self._storage.search_memory_notes(
                        user_id, stat.person_name, limit=1
                    )
                    if not existing:
                        note = await self._storage.create_memory_note(
                            user_id=user_id,
                            content=content,
                            source="rating_pattern",
                            keywords=keywords,
                            confidence=0.7,
                        )
                        notes.append(note)
        except Exception:
            # Cinematographer data may not be available
            pass

        if notes:
            logger.info(
                "ratings_analysis_complete",
                user_id=user_id,
                patterns_detected=len(notes),
            )

        return notes

    async def analyze_genre_patterns(
        self,
        user_id: int,
        watched_items: list["WatchedItem"],
    ) -> list[MemoryNote]:
        """Analyze genre patterns from watched history.

        Args:
            user_id: Internal user ID
            watched_items: List of watched items with ratings

        Returns:
            List of created memory notes
        """
        # This would require TMDB integration to get genres for each item
        # Placeholder for future implementation
        _ = user_id, watched_items  # Mark as intentionally unused
        return []

    async def analyze_letterboxd_data(
        self,
        user_id: int,
        analysis: "LetterboxdExportAnalysis",
    ) -> list[MemoryNote]:
        """Extract patterns from Letterboxd import data.

        Detects:
        - Favorites (highest rated films)
        - Year preferences (older vs newer films)
        - Rewatch patterns (films watched multiple times)
        - Rating habits (harsh critic vs generous rater)
        - Review style (if reviews present)

        Args:
            user_id: Internal user ID
            analysis: Parsed Letterboxd export analysis

        Returns:
            List of created memory notes
        """
        notes: list[MemoryNote] = []

        # 1. Summarize favorites count and patterns (avoid duplicating individual titles
        # which are already stored in core memory preferences block)
        if analysis.favorites:
            total_favs = len(analysis.favorites)
            # Summarize decade distribution of favorites
            decade_counts: dict[str, int] = {}
            for f in analysis.favorites:
                if f.year:
                    decade = f"{f.year // 10 * 10}s"
                    decade_counts[decade] = decade_counts.get(decade, 0) + 1
            top_decades = sorted(decade_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            decades_str = ", ".join(f"{d} ({c})" for d, c in top_decades)
            content = f"Has {total_favs} all-time favorite films (rated 4.5-5.0). Top decades: {decades_str}"
            existing = await self._storage.search_memory_notes(user_id, "favorite films", limit=1)
            if not existing:
                note = await self._storage.create_memory_note(
                    user_id=user_id,
                    content=content,
                    source="letterboxd",
                    keywords=["letterboxd", "favorites", "high-rating"],
                    confidence=0.9,
                )
                notes.append(note)
                logger.info(
                    "letterboxd_learning",
                    user_id=user_id,
                    pattern="favorites",
                    count=total_favs,
                )

        # 2. Films they strongly disliked (important for recommendations)
        if analysis.hated:
            hated_films = [f"{f.name} ({f.year})" for f in analysis.hated[:3]]
            content = f"Strongly disliked films: {', '.join(hated_films)}"
            existing = await self._storage.search_memory_notes(user_id, "disliked films", limit=1)
            if not existing:
                note = await self._storage.create_memory_note(
                    user_id=user_id,
                    content=content,
                    source="letterboxd",
                    keywords=["letterboxd", "disliked", "avoid"],
                    confidence=0.85,
                )
                notes.append(note)

        # 3. Analyze year preferences
        if analysis.total_rated >= 20:
            years = [f.year for f in analysis.favorites + analysis.loved if f.year]
            if years:
                avg_year = sum(years) / len(years)
                decade_counts: dict[str, int] = {}
                for year in years:
                    decade = f"{(year // 10) * 10}s"
                    decade_counts[decade] = decade_counts.get(decade, 0) + 1

                # Find dominant decades
                sorted_decades = sorted(decade_counts.items(), key=lambda x: x[1], reverse=True)
                if sorted_decades and sorted_decades[0][1] >= 5:
                    top_decade = sorted_decades[0][0]
                    content = (
                        f"Prefers films from {top_decade} (avg year of favorites: {int(avg_year)})"
                    )
                    existing = await self._storage.search_memory_notes(
                        user_id, "prefers films from", limit=1
                    )
                    if not existing:
                        note = await self._storage.create_memory_note(
                            user_id=user_id,
                            content=content,
                            source="letterboxd",
                            keywords=["letterboxd", "year-preference", top_decade],
                            confidence=0.7,
                        )
                        notes.append(note)
                        logger.info(
                            "letterboxd_learning",
                            user_id=user_id,
                            pattern="year_preference",
                            decade=top_decade,
                        )

        # 4. Rating habits analysis
        if analysis.average_rating and analysis.total_rated >= 20:
            if analysis.average_rating >= 4.0:
                rating_style = (
                    f"Generally generous rater (avg rating {analysis.average_rating:.1f}/5)"
                )
            elif analysis.average_rating <= 2.5:
                rating_style = f"Critical viewer with high standards (avg rating {analysis.average_rating:.1f}/5)"
            else:
                rating_style = f"Balanced rating style (avg {analysis.average_rating:.1f}/5)"

            existing = await self._storage.search_memory_notes(user_id, "rating", limit=1)
            if not existing:
                note = await self._storage.create_memory_note(
                    user_id=user_id,
                    content=rating_style,
                    source="letterboxd",
                    keywords=["letterboxd", "rating-style"],
                    confidence=0.75,
                )
                notes.append(note)

        # 5. Total stats summary
        if analysis.total_watched >= 50:
            content = f"Avid cinephile: {analysis.total_watched} films watched, {analysis.total_rated} rated on Letterboxd"
            existing = await self._storage.search_memory_notes(user_id, "cinephile", limit=1)
            if not existing:
                note = await self._storage.create_memory_note(
                    user_id=user_id,
                    content=content,
                    source="letterboxd",
                    keywords=["letterboxd", "stats", "cinephile"],
                    confidence=0.95,
                )
                notes.append(note)

        # 6. Watchlist size (indicates interest in discovery)
        if analysis.watchlist and len(analysis.watchlist) > 50:
            content = (
                f"Large watchlist ({len(analysis.watchlist)} films) - interested in film discovery"
            )
            existing = await self._storage.search_memory_notes(user_id, "watchlist", limit=1)
            if not existing:
                note = await self._storage.create_memory_note(
                    user_id=user_id,
                    content=content,
                    source="letterboxd",
                    keywords=["letterboxd", "watchlist", "discovery"],
                    confidence=0.8,
                )
                notes.append(note)

        logger.info(
            "letterboxd_analysis_complete",
            user_id=user_id,
            notes_created=len(notes),
            total_films=analysis.total_watched,
        )
        return notes

    async def create_manual_learning(
        self,
        user_id: int,
        content: str,
        keywords: list[str] | None = None,
        confidence: float = 0.6,
    ) -> MemoryNote:
        """Create a manual learning from conversation.

        Args:
            user_id: Internal user ID
            content: Learning content
            keywords: Optional keywords for searchability
            confidence: Confidence score (lower for conversation-based)

        Returns:
            Created memory note
        """
        return await self._storage.create_memory_note(
            user_id=user_id,
            content=content,
            source="conversation",
            keywords=keywords or [],
            confidence=confidence,
        )


# =============================================================================
# Memory Archiver
# =============================================================================


class MemoryArchiver:
    """Manages automatic archival of old memory notes.

    Notes are archived when:
    - Older than 90 days (configurable)
    - Access count below threshold (not frequently used)
    - Low confidence score

    High-confidence notes are preserved regardless of age.
    """

    ARCHIVE_AGE_DAYS = 90
    MIN_ACCESS_TO_KEEP = 3
    MIN_CONFIDENCE_TO_KEEP = 0.7

    def __init__(self, storage: "BaseStorage"):
        """Initialize memory archiver.

        Args:
            storage: Storage backend instance
        """
        self._storage = storage

    async def run_archival(self, user_id: int) -> int:
        """Run archival process for a user.

        Args:
            user_id: Internal user ID

        Returns:
            Number of notes archived
        """
        candidates = await self._storage.get_notes_for_archival(
            user_id,
            age_days=self.ARCHIVE_AGE_DAYS,
            min_access_count=self.MIN_ACCESS_TO_KEEP,
        )

        archived_count = 0
        for note in candidates:
            # Don't archive high-confidence notes
            if note.confidence >= self.MIN_CONFIDENCE_TO_KEEP:
                continue

            success = await self._storage.archive_memory_note(note.id)
            if success:
                archived_count += 1
                logger.info(
                    "note_archived",
                    user_id=user_id,
                    note_id=note.id,
                    source=note.source,
                )

        if archived_count > 0:
            logger.info(
                "archival_complete",
                user_id=user_id,
                archived_count=archived_count,
            )

        return archived_count

    async def run_archival_for_all_users(self) -> dict[int, int]:
        """Run archival for all users.

        Returns:
            Dict mapping user_id to number of archived notes
        """
        users = await self._storage.get_all_users()
        results: dict[int, int] = {}

        for user in users:
            count = await self.run_archival(user.id)
            if count > 0:
                results[user.id] = count

        return results


# =============================================================================
# Migration Helper
# =============================================================================


async def migrate_profile_to_core_memory(
    storage: "BaseStorage",
    user_id: int,
    profile_md: str,
) -> list[CoreMemoryBlock]:
    """Migrate old profile.md content to core memory blocks.

    Parses the markdown profile and distributes content to appropriate blocks.

    Args:
        storage: Storage backend instance
        user_id: Internal user ID
        profile_md: Old profile markdown content

    Returns:
        List of created/updated blocks
    """
    manager = CoreMemoryManager(storage)

    # Parse sections from profile
    sections = _parse_profile_sections(profile_md)

    blocks: list[CoreMemoryBlock] = []

    # Map old sections to new blocks
    section_mapping = {
        "Basic Info": "identity",
        "Content Preferences": "preferences",
        "Watch Context": "watch_context",
        "Communication Style": "style",
        "Explicit Instructions": "instructions",
        "Blocklist": "blocklist",
        "Notable Interactions": "learnings",
        "Conversation History Highlights": "learnings",
    }

    for section_name, content in sections.items():
        block_name = section_mapping.get(section_name)
        if block_name and content.strip():
            # For learnings, append instead of replace
            operation = "append" if block_name == "learnings" else "replace"
            try:
                block = await manager.update_block(user_id, block_name, content, operation)
                blocks.append(block)
            except ValueError as e:
                logger.warning(
                    "migration_block_error",
                    user_id=user_id,
                    section=section_name,
                    error=str(e),
                )

    logger.info(
        "profile_migrated_to_core_memory",
        user_id=user_id,
        blocks_count=len(blocks),
    )

    return blocks


def _parse_profile_sections(profile_md: str) -> dict[str, str]:
    """Parse markdown profile into sections.

    Args:
        profile_md: Profile markdown content

    Returns:
        Dict mapping section names to content
    """
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_content: list[str] = []

    for line in profile_md.split("\n"):
        if line.startswith("## "):
            # Save previous section
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()

            # Start new section
            current_section = line[3:].strip()
            current_content = []
        elif current_section:
            current_content.append(line)

    # Save last section
    if current_section:
        sections[current_section] = "\n".join(current_content).strip()

    return sections
