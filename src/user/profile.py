"""Profile system for managing user markdown profiles.

This module provides:
- ProfileManager class for loading and updating user profiles
- Default profile template generation
- Section-based profile updates
- Migration from existing preferences to profile format

The profile.md system serves as Claude's "institutional memory" about the user,
containing preferences, watch context, communication style, and notable interactions.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.user.storage import BaseStorage, Preference, User

logger = structlog.get_logger()


# =============================================================================
# Profile Template
# =============================================================================


DEFAULT_PROFILE_TEMPLATE = """# User Profile

## Basic Info
- **Name**: {display_name}
- **Language**: {language}
- **Member Since**: {created_at}

## Content Preferences

### Video Quality
- **Preferred**: {video_quality}
- **Accept Lower Quality**: When unavailable

### Audio & Subtitles
- **Audio Language**: {audio_language}
- **Subtitle Language**: {subtitle_language}

### Genres
- **Favorites**: {preferred_genres}
- **Avoid**: {excluded_genres}

## Watch Context

### Equipment
- Primary device: Not specified
- Audio setup: Not specified

### Viewing Partners
- Usually watches: Solo

## Communication Style
- **Verbosity**: Standard (adjust based on context)
- **Language**: Russian preferred
- **Tone**: Friendly but concise

## Explicit Instructions
- None specified yet

## Blocklist
- None specified yet

## Notable Interactions
- Profile created on {created_at}

## Conversation History Highlights
- New user, no history yet
"""


# =============================================================================
# Profile Section Templates
# =============================================================================


SECTION_TEMPLATES = {
    "basic_info": """## Basic Info
- **Name**: {display_name}
- **Language**: {language}
- **Member Since**: {created_at}""",
    "content_preferences": """## Content Preferences

### Video Quality
- **Preferred**: {video_quality}
- **Accept Lower Quality**: When unavailable

### Audio & Subtitles
- **Audio Language**: {audio_language}
- **Subtitle Language**: {subtitle_language}

### Genres
- **Favorites**: {preferred_genres}
- **Avoid**: {excluded_genres}""",
    "watch_context": """## Watch Context

### Equipment
- Primary device: {primary_device}
- Audio setup: {audio_setup}

### Viewing Partners
- Usually watches: {viewing_partners}""",
    "communication_style": """## Communication Style
- **Verbosity**: {verbosity}
- **Language**: {language}
- **Tone**: {tone}""",
    "explicit_instructions": """## Explicit Instructions
{instructions}""",
    "blocklist": """## Blocklist
{blocklist_items}""",
    "notable_interactions": """## Notable Interactions
{interactions}""",
    "conversation_highlights": """## Conversation History Highlights
{highlights}""",
    "letterboxd_import": """## Letterboxd Import
{letterboxd_data}""",
    "favorite_films": """## Favorite Films
{favorites}""",
    "disliked_films": """## Disliked Films
{disliked}""",
}


# =============================================================================
# Profile Manager
# =============================================================================


class ProfileManager:
    """Manager for user markdown profiles.

    Handles loading, creating, and updating user profiles with Claude-readable
    markdown format. Profiles serve as the bot's memory about each user.
    """

    def __init__(self, storage: "BaseStorage"):
        """Initialize profile manager.

        Args:
            storage: Storage backend instance
        """
        self._storage = storage

    async def get_profile(self, user_id: int) -> str | None:
        """Get user's markdown profile.

        Args:
            user_id: Internal user ID

        Returns:
            Profile markdown string or None if not found
        """
        profile = await self._storage.get_profile(user_id)
        if profile:
            return profile.profile_md
        return None

    async def get_or_create_profile(
        self,
        user_id: int,
        user: "User | None" = None,
        preferences: "Preference | None" = None,
    ) -> str:
        """Get existing profile or create a new one.

        Args:
            user_id: Internal user ID
            user: Optional User object for template generation
            preferences: Optional Preference object for template generation

        Returns:
            Profile markdown string
        """
        existing = await self.get_profile(user_id)
        if existing:
            return existing

        # Create default profile
        if user is None:
            user = await self._storage.get_user(user_id)

        if preferences is None:
            preferences = await self._storage.get_preferences(user_id)

        profile_md = self.create_default_profile(user, preferences)
        await self._storage.update_profile(user_id, profile_md)

        logger.info("profile_created", user_id=user_id)
        return profile_md

    def create_default_profile(
        self,
        user: "User | None" = None,
        preferences: "Preference | None" = None,
    ) -> str:
        """Create a default profile from user data and preferences.

        Args:
            user: User object
            preferences: User preferences

        Returns:
            Default profile markdown string
        """
        # Default values
        display_name = "Unknown User"
        language = "ru"
        created_at = datetime.now(UTC).strftime("%Y-%m-%d")
        video_quality = "1080p"
        audio_language = "Russian"
        subtitle_language = "None"
        preferred_genres = "Not specified"
        excluded_genres = "None"

        # Override with actual user data
        if user:
            display_name = user.display_name
            language = user.language_code or "ru"
            created_at = user.created_at.strftime("%Y-%m-%d")

        # Override with actual preferences
        if preferences:
            video_quality = preferences.video_quality or "1080p"
            audio_language = self._format_language(preferences.audio_language)
            subtitle_language = self._format_language(preferences.subtitle_language) or "None"
            preferred_genres = (
                ", ".join(preferences.preferred_genres)
                if preferences.preferred_genres
                else "Not specified"
            )
            excluded_genres = (
                ", ".join(preferences.excluded_genres) if preferences.excluded_genres else "None"
            )

        return DEFAULT_PROFILE_TEMPLATE.format(
            display_name=display_name,
            language=language,
            created_at=created_at,
            video_quality=video_quality,
            audio_language=audio_language,
            subtitle_language=subtitle_language,
            preferred_genres=preferred_genres,
            excluded_genres=excluded_genres,
        )

    async def update_profile(self, user_id: int, profile_md: str) -> str:
        """Update user's full profile.

        Args:
            user_id: Internal user ID
            profile_md: New profile markdown content

        Returns:
            Updated profile markdown string
        """
        profile = await self._storage.update_profile(user_id, profile_md)
        logger.info("profile_updated", user_id=user_id)
        return profile.profile_md

    async def update_section(
        self,
        user_id: int,
        section: str,
        content: str,
    ) -> str:
        """Update a specific section of the profile.

        Args:
            user_id: Internal user ID
            section: Section name (e.g., "watch_context", "notable_interactions")
            content: New content for the section

        Returns:
            Updated profile markdown string
        """
        profile_md = await self.get_or_create_profile(user_id)

        # Find and replace the section
        section_header = self._get_section_header(section)
        if not section_header:
            # For unknown sections, generate header from section name and add anyway
            logger.warning("unknown_section_adding_anyway", section=section)
            section_header = f"## {section.replace('_', ' ').title()}"

        # Parse profile and find section boundaries
        lines = profile_md.split("\n")
        new_lines: list[str] = []
        in_target_section = False
        section_replaced = False

        for line in lines:
            if line.strip().startswith("## "):
                if in_target_section:
                    # End of target section, add new content
                    new_lines.append(content)
                    new_lines.append("")
                    in_target_section = False
                    section_replaced = True

                if line.strip() == section_header:
                    in_target_section = True
                    continue

            if not in_target_section:
                new_lines.append(line)

        # If section was the last one
        if in_target_section:
            new_lines.append(content)
            section_replaced = True

        # If section wasn't found, append it
        if not section_replaced:
            new_lines.append("")
            new_lines.append(content)

        updated_md = "\n".join(new_lines)
        return await self.update_profile(user_id, updated_md)

    async def append_to_section(
        self,
        user_id: int,
        section: str,
        content: str,
    ) -> str:
        """Append content to a specific section.

        Args:
            user_id: Internal user ID
            section: Section name
            content: Content to append

        Returns:
            Updated profile markdown string
        """
        profile_md = await self.get_or_create_profile(user_id)

        section_header = self._get_section_header(section)
        if not section_header:
            logger.warning("unknown_section", section=section)
            return profile_md

        lines = profile_md.split("\n")
        new_lines: list[str] = []
        in_target_section = False
        content_appended = False

        for line in lines:
            new_lines.append(line)

            if line.strip().startswith("## "):
                if in_target_section and not content_appended:
                    # End of target section, insert content before new section
                    new_lines.insert(-1, content)
                    content_appended = True

                in_target_section = line.strip() == section_header

        # If section was the last one
        if in_target_section and not content_appended:
            new_lines.append(content)

        updated_md = "\n".join(new_lines)
        return await self.update_profile(user_id, updated_md)

    async def add_notable_interaction(
        self,
        user_id: int,
        interaction: str,
    ) -> str:
        """Add a notable interaction to the profile.

        Args:
            user_id: Internal user ID
            interaction: Description of the interaction

        Returns:
            Updated profile markdown string
        """
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
        entry = f"- [{timestamp}] {interaction}"
        return await self.append_to_section(user_id, "notable_interactions", entry)

    async def add_conversation_highlight(
        self,
        user_id: int,
        highlight: str,
    ) -> str:
        """Add a conversation highlight to the profile.

        Args:
            user_id: Internal user ID
            highlight: Description of the highlight

        Returns:
            Updated profile markdown string
        """
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
        entry = f"- [{timestamp}] {highlight}"
        return await self.append_to_section(user_id, "conversation_highlights", entry)

    async def update_watch_context(
        self,
        user_id: int,
        primary_device: str | None = None,
        audio_setup: str | None = None,
        viewing_partners: str | None = None,
    ) -> str:
        """Update watch context section.

        Args:
            user_id: Internal user ID
            primary_device: Primary viewing device
            audio_setup: Audio setup description
            viewing_partners: Who they usually watch with

        Returns:
            Updated profile markdown string
        """
        profile_md = await self.get_or_create_profile(user_id)

        # Extract current values
        current_device = self._extract_value(profile_md, "Primary device:") or "Not specified"
        current_audio = self._extract_value(profile_md, "Audio setup:") or "Not specified"
        current_partners = self._extract_value(profile_md, "Usually watches:") or "Solo"

        # Use new values or keep current
        device = primary_device or current_device
        audio = audio_setup or current_audio
        partners = viewing_partners or current_partners

        content = SECTION_TEMPLATES["watch_context"].format(
            primary_device=device,
            audio_setup=audio,
            viewing_partners=partners,
        )

        return await self.update_section(user_id, "watch_context", content)

    async def update_communication_style(
        self,
        user_id: int,
        verbosity: str | None = None,
        language: str | None = None,
        tone: str | None = None,
    ) -> str:
        """Update communication style section.

        Args:
            user_id: Internal user ID
            verbosity: Communication verbosity level
            language: Preferred language
            tone: Communication tone

        Returns:
            Updated profile markdown string
        """
        profile_md = await self.get_or_create_profile(user_id)

        # Extract current values
        current_verbosity = self._extract_value(profile_md, "Verbosity:") or "Standard"
        current_language = self._extract_value(profile_md, "Language:") or "Russian"
        current_tone = self._extract_value(profile_md, "Tone:") or "Friendly but concise"

        # Use new values or keep current
        verb = verbosity or current_verbosity
        lang = language or current_language
        t = tone or current_tone

        content = SECTION_TEMPLATES["communication_style"].format(
            verbosity=verb,
            language=lang,
            tone=t,
        )

        return await self.update_section(user_id, "communication_style", content)

    async def sync_from_preferences(self, user_id: int) -> str:
        """Sync profile content preferences from preferences table.

        Args:
            user_id: Internal user ID

        Returns:
            Updated profile markdown string
        """
        preferences = await self._storage.get_preferences(user_id)
        if not preferences:
            return await self.get_or_create_profile(user_id)

        # Ensure profile exists
        await self.get_or_create_profile(user_id)

        # Update content preferences section
        preferred_genres = (
            ", ".join(preferences.preferred_genres)
            if preferences.preferred_genres
            else "Not specified"
        )
        excluded_genres = (
            ", ".join(preferences.excluded_genres) if preferences.excluded_genres else "None"
        )

        content = SECTION_TEMPLATES["content_preferences"].format(
            video_quality=preferences.video_quality or "1080p",
            audio_language=self._format_language(preferences.audio_language),
            subtitle_language=self._format_language(preferences.subtitle_language) or "None",
            preferred_genres=preferred_genres,
            excluded_genres=excluded_genres,
        )

        return await self.update_section(user_id, "content_preferences", content)

    async def sync_blocklist(self, user_id: int) -> str:
        """Sync blocklist section from blocklist table.

        Args:
            user_id: Internal user ID

        Returns:
            Updated profile markdown string
        """
        blocklist = await self._storage.get_blocklist(user_id)

        if not blocklist:
            content = "## Blocklist\n- None specified yet"
        else:
            items: list[str] = []
            for item in blocklist:
                level = (
                    "(don't recommend)"
                    if item.block_level == "dont_recommend"
                    else "(never mention)"
                )
                notes = f" - {item.notes}" if item.notes else ""
                items.append(f"- **{item.block_type.title()}**: {item.block_value} {level}{notes}")
            content = "## Blocklist\n" + "\n".join(items)

        return await self.update_section(user_id, "blocklist", content)

    def _get_section_header(self, section: str) -> str | None:
        """Get the markdown header for a section.

        Args:
            section: Section name

        Returns:
            Markdown header string or None if unknown section
        """
        headers = {
            "basic_info": "## Basic Info",
            "content_preferences": "## Content Preferences",
            "watch_context": "## Watch Context",
            "communication_style": "## Communication Style",
            "explicit_instructions": "## Explicit Instructions",
            "blocklist": "## Blocklist",
            "notable_interactions": "## Notable Interactions",
            "conversation_highlights": "## Conversation History Highlights",
            "letterboxd_import": "## Letterboxd Import",
            "favorite_films": "## Favorite Films",
            "disliked_films": "## Disliked Films",
        }
        return headers.get(section)

    def _extract_value(self, profile_md: str, key: str) -> str | None:
        """Extract a value from the profile by key.

        Args:
            profile_md: Profile markdown content
            key: Key to search for (e.g., "Primary device:")

        Returns:
            Value string or None if not found
        """
        for line in profile_md.split("\n"):
            if key in line:
                # Extract value after the key
                parts = line.split(key, 1)
                if len(parts) > 1:
                    return parts[1].strip()
        return None

    def _format_language(self, lang_code: str | None) -> str:
        """Format language code to human-readable name.

        Args:
            lang_code: Language code (e.g., "ru", "en")

        Returns:
            Human-readable language name
        """
        if not lang_code:
            return "Not specified"

        languages = {
            "ru": "Russian",
            "en": "English",
            "de": "German",
            "fr": "French",
            "es": "Spanish",
            "it": "Italian",
            "ja": "Japanese",
            "ko": "Korean",
            "zh": "Chinese",
            "pt": "Portuguese",
        }
        return languages.get(lang_code.lower(), lang_code)


# =============================================================================
# Utility Functions
# =============================================================================


def get_profile_summary(profile_md: str) -> str:
    """Extract a brief summary from the profile for system prompt injection.

    Args:
        profile_md: Full profile markdown

    Returns:
        Condensed summary string
    """
    summary_parts: list[str] = []

    lines = profile_md.split("\n")
    for line in lines:
        # Extract key preference values
        if (
            "**Preferred**:" in line
            or "**Audio Language**:" in line
            or "**Favorites**:" in line
            and "Not specified" not in line
            or "**Verbosity**:" in line
        ):
            summary_parts.append(line.strip().replace("- ", "").replace("**", ""))

    return " | ".join(summary_parts) if summary_parts else "No specific preferences set"


def extract_blocklist_items(profile_md: str) -> list[dict[str, str]]:
    """Extract blocklist items from profile markdown.

    Args:
        profile_md: Full profile markdown

    Returns:
        List of blocklist items as dicts
    """
    items: list[dict[str, str]] = []
    in_blocklist = False

    for line in profile_md.split("\n"):
        if "## Blocklist" in line:
            in_blocklist = True
            continue
        if line.startswith("## ") and in_blocklist:
            break
        if in_blocklist and line.startswith("- **"):
            # Parse blocklist entry
            try:
                # Format: - **Type**: Value (level)
                type_start = line.index("**") + 2
                type_end = line.index("**", type_start)
                block_type = line[type_start:type_end].lower()

                value_start = line.index(":") + 1
                # Find level marker
                if "(don't recommend)" in line:
                    value_end = line.index("(don't recommend)")
                    level = "dont_recommend"
                elif "(never mention)" in line:
                    value_end = line.index("(never mention)")
                    level = "never_mention"
                else:
                    value_end = len(line)
                    level = "dont_recommend"

                value = line[value_start:value_end].strip()

                items.append(
                    {
                        "type": block_type,
                        "value": value,
                        "level": level,
                    }
                )
            except (ValueError, IndexError):
                continue

    return items
