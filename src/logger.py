"""Structured logging configuration using structlog.

This module sets up JSON-formatted logging for production deployment on Koyeb,
with console-friendly formatting for local development.
"""

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from src.config import settings


def add_log_level(_logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Add log level to the event dict."""
    if method_name == "warn":
        # Structlog uses "warn", but we want "warning"
        event_dict["level"] = "warning"
    else:
        event_dict["level"] = method_name
    return event_dict


def censor_sensitive_data(
    _logger: logging.Logger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Censor sensitive data from log events.

    Removes or masks fields that may contain tokens, passwords, or API keys.
    """
    sensitive_keys = {
        "token",
        "password",
        "api_key",
        "secret",
        "authorization",
        "auth",
        "credentials",
        "session",
        "cookie",
        "encryption_key",
    }

    def _censor_value(key: str, value: Any) -> Any:
        """Censor a single value based on key name."""
        key_lower = key.lower()
        # Check if key contains any sensitive keyword
        if any(sensitive in key_lower for sensitive in sensitive_keys):
            return "***"
        if isinstance(value, dict):
            return {k: _censor_value(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [_censor_value(key, item) if isinstance(item, dict) else item for item in value]
        return value

    # Create censored copy of event dict
    censored: EventDict = {}
    for key, value in event_dict.items():
        censored[key] = _censor_value(key, value)
    return censored


def configure_logging() -> None:
    """Configure structlog for the application.

    Sets up different output formats based on environment:
    - Production (Koyeb): JSON format for log aggregation
    - Development: Console-friendly colored output
    """
    # Configure stdlib logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper()),
    )

    # Common processors for all environments
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        censor_sensitive_data,
    ]

    # Choose renderer based on environment
    if settings.is_production:
        # JSON output for Koyeb log aggregation
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        # Console-friendly colored output for development
        renderer = structlog.dev.ConsoleRenderer(
            colors=True,
            exception_formatter=structlog.dev.plain_traceback,
        )

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the formatter for stdlib logging
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, settings.log_level.upper()))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a configured logger instance.

    Args:
        name: Optional logger name. If not provided, uses the caller's module name.

    Returns:
        Configured structlog logger instance.

    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("user_login", user_id=123, ip="192.168.1.1")
    """
    return structlog.get_logger(name)


# Configure logging on module import
configure_logging()
