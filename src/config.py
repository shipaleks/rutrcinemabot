"""Configuration management using pydantic-settings.

All environment variables are loaded and validated here.
Sensitive data (tokens, passwords) are marked as sensitive to prevent logging.
"""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All sensitive fields use SecretStr to prevent accidental logging.
    Required fields will raise ValidationError if not provided.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Required: Telegram Bot Configuration
    telegram_bot_token: SecretStr = Field(
        ...,
        description="Telegram bot token from @BotFather",
    )

    # Required: AI Configuration
    anthropic_api_key: SecretStr = Field(
        ...,
        description="Anthropic API key for Claude",
    )

    # Required: Media APIs
    tmdb_api_key: SecretStr = Field(
        ...,
        description="The Movie Database API key",
    )

    kinopoisk_api_token: SecretStr = Field(
        ...,
        description="Kinopoisk unofficial API token",
    )

    # Required: Security
    encryption_key: SecretStr = Field(
        ...,
        description="Fernet encryption key for sensitive user data",
    )

    # Database Configuration (Postgres preferred, SQLite fallback)
    database_url: SecretStr | None = Field(
        default=None,
        description="PostgreSQL connection URL (e.g., postgresql://user:pass@host:5432/db)",
    )

    # Optional: Rutracker Authentication
    rutracker_username: str | None = Field(
        default=None,
        description="Rutracker username for authentication (optional)",
    )

    rutracker_password: SecretStr | None = Field(
        default=None,
        description="Rutracker password for authentication (optional)",
    )

    # Optional: Seedbox Configuration
    seedbox_host: str | None = Field(
        default=None,
        description="Seedbox host URL (optional)",
    )

    seedbox_user: str | None = Field(
        default=None,
        description="Seedbox username (optional)",
    )

    seedbox_password: SecretStr | None = Field(
        default=None,
        description="Seedbox password (optional)",
    )

    # Optional: Letterboxd API Configuration
    letterboxd_client_id: str | None = Field(
        default=None,
        description="Letterboxd API client ID (requires API approval)",
    )

    letterboxd_client_secret: SecretStr | None = Field(
        default=None,
        description="Letterboxd API client secret",
    )

    letterboxd_redirect_uri: str = Field(
        default="https://localhost/callback",
        description="Letterboxd OAuth redirect URI",
    )

    # Application Configuration
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    environment: str = Field(
        default="production",
        description="Environment name (development, production)",
    )

    cache_ttl: int = Field(
        default=3600,
        description="Cache TTL in seconds",
        ge=0,
    )

    webhook_url: str | None = Field(
        default=None,
        description="Webhook URL for Telegram (auto-configured on Koyeb)",
    )

    webhook_path: str = Field(
        default="/webhook",
        description="Webhook path",
    )

    port: int = Field(
        default=8000,
        description="Port for webhook server",
        ge=1,
        le=65535,
    )

    health_port: int = Field(
        default=8080,
        description="Port for health check server (separate from webhook)",
        ge=1,
        le=65535,
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is one of the standard levels."""
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_upper = v.upper()
        if v_upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v_upper

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        """Validate environment is development or production."""
        allowed = {"development", "production"}
        v_lower = v.lower()
        if v_lower not in allowed:
            raise ValueError(f"environment must be one of {allowed}")
        return v_lower

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.environment == "production"

    @property
    def has_rutracker_credentials(self) -> bool:
        """Check if Rutracker credentials are configured."""
        return all(
            [
                self.rutracker_username,
                self.rutracker_password,
            ]
        )

    @property
    def has_seedbox(self) -> bool:
        """Check if seedbox is configured."""
        return all(
            [
                self.seedbox_host,
                self.seedbox_user,
                self.seedbox_password,
            ]
        )

    @property
    def has_database_url(self) -> bool:
        """Check if external database (Postgres) is configured."""
        return self.database_url is not None

    @property
    def has_letterboxd(self) -> bool:
        """Check if Letterboxd API is configured."""
        return all(
            [
                self.letterboxd_client_id,
                self.letterboxd_client_secret,
            ]
        )

    def get_safe_dict(self) -> dict[str, str | int | None]:
        """Get configuration as dict with sensitive values masked.

        Returns:
            Dictionary with SecretStr values shown as '***'
        """
        result = {}
        for field_name in self.model_fields:
            value = getattr(self, field_name)

            # Mask SecretStr values
            if isinstance(value, SecretStr):
                result[field_name] = "***"
            # Handle None values
            elif value is None:
                result[field_name] = None
            # Regular values
            else:
                result[field_name] = value

        return result


# Global settings instance
settings = Settings()
