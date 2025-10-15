import logging
import sys
from enum import Enum

import click
from uvicorn.logging import DefaultFormatter


class LogColor(Enum):
    """Log color enumeration for custom log types.

    Note: INFO messages use the default color (no styling applied).
    """

    SUCCESS = "green"
    HEADER = "yellow"
    SECTION = "blue"
    PROMPT = "magenta"
    DEBUG = "cyan"
    WARNING = "yellow"
    ERROR = "red"
    CRITICAL = "bright_red"


def setup_logger(loglevel="info"):
    """Setup the nemorosa logger with uvicorn-style formatting and colors.

    Args:
        loglevel: Log level string (e.g., 'info', 'debug', 'warning')
    """
    # Get nemorosa logger
    logger = logging.getLogger("nemorosa")

    # Set log level
    logger.setLevel(loglevel.upper())

    # Remove existing handlers to avoid duplicate logs
    logger.handlers.clear()

    # Create console handler with colored formatter
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DefaultFormatter(fmt="%(levelprefix)s %(message)s"))
    logger.addHandler(handler)

    # Prevent propagation to avoid duplicate logs
    logger.propagate = False


# Cache logger instance for convenience functions
_logger = logging.getLogger("nemorosa")


# Convenience functions for colored logging
def success(msg, *args, **kwargs):
    _logger.info(click.style(str(msg), fg=LogColor.SUCCESS.value), *args, **kwargs)


def header(msg, *args, **kwargs):
    _logger.info(click.style(str(msg), fg=LogColor.HEADER.value), *args, **kwargs)


def section(msg, *args, **kwargs):
    _logger.info(click.style(str(msg), fg=LogColor.SECTION.value), *args, **kwargs)


def prompt(msg, *args, **kwargs):
    _logger.info(click.style(str(msg), fg=LogColor.PROMPT.value), *args, **kwargs)


def error(msg, *args, **kwargs):
    _logger.error(click.style(str(msg), fg=LogColor.ERROR.value), *args, **kwargs)


def critical(msg, *args, **kwargs):
    _logger.critical(click.style(str(msg), fg=LogColor.CRITICAL.value), *args, **kwargs)


def debug(msg, *args, **kwargs):
    _logger.debug(click.style(str(msg), fg=LogColor.DEBUG.value), *args, **kwargs)


def warning(msg, *args, **kwargs):
    _logger.warning(click.style(str(msg), fg=LogColor.WARNING.value), *args, **kwargs)


def info(msg, *args, **kwargs):
    """Log info message with default color (no styling applied)."""
    _logger.info(msg, *args, **kwargs)
