"""
Logging module for nemorosa.
Provides colored logging functionality with custom log levels and formatters.
"""

import logging
import sys
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import click
from uvicorn.logging import DefaultFormatter

if TYPE_CHECKING:
    from .config import LogLevel

# Constants for URL password redaction
REDACTED_MSG = "[REDACTED]"


class LogColor(StrEnum):
    """Log color enumeration for custom log types.

    Note: INFO messages use the default color (no styling applied).
    """

    SUCCESS = "green"
    HEADER = "bright_blue"
    SECTION = "blue"
    PROMPT = "magenta"
    DEBUG = "cyan"
    WARNING = "yellow"
    ERROR = "red"
    CRITICAL = "bright_red"


def redact_url_password(url_str: str) -> str:
    """Redact password from URL.

    This function attempts to parse the URL and if it contains a password,
    returns a new URL with the password replaced by [REDACTED].

    Args:
        url_str: The URL string that may contain a password.

    Returns:
        str: URL with password redacted, or original URL if no password.
    """
    try:
        parsed_url = urlparse(url_str)
        if not parsed_url.password:
            return url_str

        # Build new netloc with redacted password
        # netloc format: [username[:password]@]host[:port]
        # Start with username and redacted password
        auth_part = f"{parsed_url.username}:{REDACTED_MSG}"

        # Build host part
        host_part = parsed_url.hostname or ""

        # Add port only if explicitly specified
        if parsed_url.port:
            host_part = f"{host_part}:{parsed_url.port}"

        new_netloc = f"{auth_part}@{host_part}"
        # Replace netloc in the parsed URL
        redacted_parsed = parsed_url._replace(netloc=new_netloc)
        return redacted_parsed.geturl()

    except Exception:
        # Ignore any parsing errors and return original URL
        return url_str


# Global logger instance
_logger_instance: logging.Logger | None = None


def init_logger(loglevel: "LogLevel | None" = None) -> None:
    """Initialize global logger instance.

    Should be called once during application startup.

    Args:
        loglevel: Log level enum
    """
    global _logger_instance

    # Get or create nemorosa logger
    logger = logging.getLogger("nemorosa")

    # Set log level
    logger.setLevel(loglevel.upper() if loglevel else logging.INFO)

    # Remove existing handlers to avoid duplicate logs
    logger.handlers.clear()

    # Create console handler with colored formatter
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        DefaultFormatter(
            fmt="%(asctime)s | %(levelprefix)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    logger.addHandler(handler)

    # Prevent propagation to avoid duplicate logs
    logger.propagate = False

    # Store as global instance
    _logger_instance = logger


def set_log_level(loglevel: "LogLevel") -> None:
    """Update log level of initialized logger.

    Args:
        loglevel: Log level enum

    Raises:
        RuntimeError: If logger has not been initialized.
    """
    logger = get_logger()
    logger.setLevel(loglevel.upper())


def get_logger() -> logging.Logger:
    """Get global logger instance.

    Must be called after init_logger() has been invoked.

    Returns:
        logging.Logger: Logger instance.

    Raises:
        RuntimeError: If logger has not been initialized.
    """
    if _logger_instance is None:
        raise RuntimeError("Logger not initialized. Call init_logger() first.")
    return _logger_instance


# Convenience functions for colored logging
def success(msg, *args, **kwargs):
    get_logger().info(click.style(str(msg), fg=LogColor.SUCCESS), *args, **kwargs)


def header(msg, *args, **kwargs):
    get_logger().info(click.style(str(msg), fg=LogColor.HEADER), *args, **kwargs)


def section(msg, *args, **kwargs):
    get_logger().info(click.style(str(msg), fg=LogColor.SECTION), *args, **kwargs)


def prompt(msg, *args, **kwargs):
    get_logger().info(click.style(str(msg), fg=LogColor.PROMPT), *args, **kwargs)


def error(msg, *args, **kwargs):
    get_logger().error(click.style(str(msg), fg=LogColor.ERROR), *args, **kwargs)


def critical(msg, *args, **kwargs):
    get_logger().critical(click.style(str(msg), fg=LogColor.CRITICAL), *args, **kwargs)


def debug(msg, *args, **kwargs):
    get_logger().debug(click.style(str(msg), fg=LogColor.DEBUG), *args, **kwargs)


def warning(msg, *args, **kwargs):
    get_logger().warning(click.style(str(msg), fg=LogColor.WARNING), *args, **kwargs)


def info(msg, *args, **kwargs):
    """Log info message with default color (no styling applied)."""
    get_logger().info(msg, *args, **kwargs)


def exception(msg, *args, **kwargs):
    """Log exception message with traceback and error color."""
    get_logger().exception(click.style(str(msg), fg=LogColor.ERROR), *args, **kwargs)
