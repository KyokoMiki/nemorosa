"""Main entry point for nemorosa."""

import sys

from .cli import main


def setup_event_loop():
    """Setup the best available event loop for the current platform."""
    try:
        if sys.platform == "win32":
            import winloop  # type: ignore[import]

            winloop.install()
        else:
            import uvloop  # type: ignore[import]

            uvloop.install()
    except Exception as e:
        print(f"Event loop setup warning: {e}, using default asyncio")


if __name__ == "__main__":
    setup_event_loop()
    main()
