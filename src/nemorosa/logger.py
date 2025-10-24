"""
Logging module for nemorosa.
Provides colored logging functionality with custom log levels and formatters.
"""

import logging
import sys
from enum import Enum

import click
from uvicorn.logging import DefaultFormatter

from .config import LogLevel


class LogColor(Enum):
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


# Global logger instance
_logger_instance: logging.Logger | None = None


def init_logger(loglevel: LogLevel = LogLevel.INFO) -> None:
    """Initialize global logger instance.

    Should be called once during application startup.

    Args:
        loglevel: Log level enum
    """
    global _logger_instance

    # Get or create nemorosa logger
    logger = logging.getLogger("nemorosa")

    # Set log level
    logger.setLevel(loglevel.value.upper())

    # Remove existing handlers to avoid duplicate logs
    logger.handlers.clear()

    # Create console handler with colored formatter
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DefaultFormatter(fmt="%(levelprefix)s %(message)s"))
    logger.addHandler(handler)

    # Prevent propagation to avoid duplicate logs
    logger.propagate = False

    # Store as global instance
    _logger_instance = logger


def set_log_level(loglevel: LogLevel) -> None:
    """Update log level of initialized logger.

    Args:
        loglevel: Log level enum

    Raises:
        RuntimeError: If logger has not been initialized.
    """
    logger = get_logger()
    logger.setLevel(loglevel.value.upper())


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
    get_logger().info(click.style(str(msg), fg=LogColor.SUCCESS.value), *args, **kwargs)


def header(msg, *args, **kwargs):
    get_logger().info(click.style(str(msg), fg=LogColor.HEADER.value), *args, **kwargs)


def section(msg, *args, **kwargs):
    get_logger().info(click.style(str(msg), fg=LogColor.SECTION.value), *args, **kwargs)


def prompt(msg, *args, **kwargs):
    get_logger().info(click.style(str(msg), fg=LogColor.PROMPT.value), *args, **kwargs)


def error(msg, *args, **kwargs):
    get_logger().error(click.style(str(msg), fg=LogColor.ERROR.value), *args, **kwargs)


def critical(msg, *args, **kwargs):
    get_logger().critical(click.style(str(msg), fg=LogColor.CRITICAL.value), *args, **kwargs)


def debug(msg, *args, **kwargs):
    get_logger().debug(click.style(str(msg), fg=LogColor.DEBUG.value), *args, **kwargs)


def warning(msg, *args, **kwargs):
    get_logger().warning(click.style(str(msg), fg=LogColor.WARNING.value), *args, **kwargs)


def info(msg, *args, **kwargs):
    """Log info message with default color (no styling applied)."""
    get_logger().info(msg, *args, **kwargs)


def exception(msg, *args, **kwargs):
    """Log exception message with traceback and error color."""
    get_logger().exception(click.style(str(msg), fg=LogColor.ERROR.value), *args, **kwargs)
