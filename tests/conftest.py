"""Shared test fixtures and configuration for nemorosa tests."""

import pytest

import nemorosa.logger as logger_module


def pytest_configure(config: pytest.Config) -> None:
    """Initialize logger once for all tests."""
    if getattr(logger_module, "_logger_instance", None) is None:
        logger_module.init_logger()
