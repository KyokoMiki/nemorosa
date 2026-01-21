"""Command line interface for nemorosa."""

import sys
from argparse import ArgumentParser

import anyio

from . import config, logger
from .api import cleanup_api, get_target_apis, init_api
from .client_instance import get_torrent_client, init_torrent_client
from .core import get_core, init_core
from .db import cleanup_database, get_database, init_database
from .notifier import get_notifier, init_notifier
from .scheduler import init_job_manager
from .webserver import run_webserver


def setup_argument_parser():
    """Set up command line argument parser.

    Returns:
        ArgumentParser: Configured argument parser.
    """
    parser = ArgumentParser(
        description=(
            "Music torrent cross-seeding tool with automatic file mapping "
            "and seamless injection"
        )
    )

    # Operation modes
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-s",
        "--server",
        action="store_true",
        help="start nemorosa in server mode",
    )
    mode_group.add_argument(
        "-t",
        "--torrent",
        type=str,
        help="process a single torrent by infohash",
    )
    mode_group.add_argument(
        "-r",
        "--retry-undownloaded",
        action="store_true",
        help="retry downloading torrents from undownloaded_torrents table",
    )
    mode_group.add_argument(
        "-p",
        "--post-process",
        action="store_true",
        help="post-process injected torrents",
    )
    mode_group.add_argument(
        "--test-notification",
        action="store_true",
        help="send a test notification to configured services",
    )

    # Global options
    global_group = parser.add_argument_group("Global options")
    global_group.add_argument(
        "-l",
        "--loglevel",
        metavar="LOGLEVEL",
        choices=["debug", "info", "warning", "error", "critical"],
        help="loglevel for log file",
    )
    global_group.add_argument(
        "--config",
        help="Path to YAML configuration file",
    )
    global_group.add_argument(
        "--no-download",
        action="store_true",
        help="if set, don't download .torrent files, only save URLs",
    )

    # Torrent client options
    client_group = parser.add_argument_group("Torrent client options")
    client_group.add_argument(
        "--client",
        help="Torrent client URL (e.g. transmission+http://user:pass@localhost:9091)",
    )

    # Server options
    server_group = parser.add_argument_group("Server options")
    server_group.add_argument(
        "--host",
        help="server host",
    )
    server_group.add_argument(
        "--port",
        type=int,
        help="server port",
    )

    return parser


def setup_config(config_path):
    """Set up configuration.

    Args:
        config_path: Path to configuration file (or None for auto-detection).
    """

    # Use new configuration processing module to initialize global config
    try:
        config.init_config(config_path)
        logger.info("Configuration loaded successfully")
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        logger.error("Please check your configuration file and try again")
        sys.exit(1)


def override_config_with_args(args):
    """Override configuration with command line arguments.

    Args:
        args: Parsed command line arguments.
    """
    # Override loglevel if specified
    if args.loglevel is not None:
        config.cfg.global_config.loglevel = config.LogLevel(args.loglevel)

    # Override no_download if specified
    if args.no_download:
        config.cfg.global_config.no_download = True

    # Override client if specified
    if args.client is not None:
        config.cfg.downloader.client = args.client

    # Override server host if specified
    if args.host is not None:
        config.cfg.server.host = args.host

    # Override server port if specified
    if args.port is not None:
        config.cfg.server.port = args.port


async def async_init():
    """Initialize core components asynchronously.

    This function is used by both CLI and webserver modes to set up the application.
    """
    logger.debug("Initializing database...")
    # Initialize database
    await init_database()
    logger.info("Database initialized successfully")

    # Initialize torrent client
    logger.debug(
        "Connecting to torrent client at %s...",
        logger.redact_url_password(config.cfg.downloader.client),
    )
    await init_torrent_client(config.cfg.downloader.client)
    logger.info("Successfully connected to torrent client")

    # Check if client URL has changed and rebuild cache if needed
    current_client_url = config.cfg.downloader.client
    database = get_database()
    cached_client_url = await database.get_metadata("client_url")

    if cached_client_url != current_client_url:
        logger.debug(
            "Client URL changed from %s to %s",
            logger.redact_url_password(cached_client_url)
            if cached_client_url
            else "None",
            logger.redact_url_password(current_client_url),
        )
        logger.info("Rebuilding client torrents cache...")

        # Get all torrents from the new client
        app_torrent_client = get_torrent_client()
        all_torrents = await app_torrent_client.get_torrents(
            fields=["hash", "name", "total_size", "files", "trackers", "download_dir"]
        )

        # Rebuild cache
        await app_torrent_client.rebuild_client_torrents_cache(all_torrents)
        logger.success(
            "Rebuilt cache with %s torrents from new client", len(all_torrents)
        )

        # Update cached client URL
        await database.set_metadata("client_url", current_client_url)

    # Initialize API connections
    await init_api(config.cfg.target_sites)
    logger.info(
        "API connections established for %s target sites", len(get_target_apis())
    )

    # Initialize core processor
    await init_core()
    logger.debug("Core processor initialized")

    # Initialize and start scheduler
    await init_job_manager()

    # Initialize notifier
    init_notifier(config.cfg.global_config.notification_urls)


def main():
    """Main function."""
    # Step 1: Setup event loop
    logger.init_logger()

    # Step 2: Parse command line arguments
    parser = setup_argument_parser()
    args = parser.parse_args()

    # Step 3: Load configuration
    setup_config(args.config)

    # Step 4: Override configuration with command line arguments
    override_config_with_args(args)

    # Step 5: Update log level with final loglevel from config
    logger.set_log_level(config.cfg.global_config.loglevel)

    # Log configuration summary
    logger.section("===== Configuration Summary =====")
    logger.debug("Config file: %s", args.config or "auto-detected")
    logger.debug("No download: %s", config.cfg.global_config.no_download)
    logger.debug("Log level: %s", config.cfg.global_config.loglevel)
    logger.debug(
        "Client URL: %s", logger.redact_url_password(config.cfg.downloader.client)
    )
    check_trackers = config.cfg.global_config.check_trackers
    logger.debug(
        "CHECK_TRACKERS: %s",
        check_trackers if check_trackers else "All trackers allowed",
    )

    # Display target sites configuration
    logger.debug("Target sites configured: %d", len(config.cfg.target_sites))
    for i, site in enumerate(config.cfg.target_sites, 1):
        logger.debug("  Site %d: %s", i, site.server)

    logger.section("===== Nemorosa Starting =====")

    # Decide operation based on command line arguments
    if args.server:
        # Server mode
        run_webserver()
    else:
        anyio.run(_async_main, args, backend_options={"use_uvloop": True})

    logger.section("===== Nemorosa Finished =====")


async def _async_main(args):
    """Async main function for non-server operations."""

    try:
        # Initialize core components (database, API connections, scheduler)
        await async_init()

        # Get processor instance
        processor = get_core()

        if args.torrent:
            # Single torrent mode
            logger.debug("Processing single torrent: %s", args.torrent)
            result = await processor.process_single_torrent(args.torrent)

            # Print result
            logger.debug("Processing result: %s", result.status)
            logger.debug("Message: %s", result.message)
        elif args.retry_undownloaded:
            # Re-download undownloaded torrents
            await processor.retry_undownloaded_torrents()
        elif args.post_process:
            # Post-process injected torrents only
            await processor.post_process_injected_torrents()
        elif args.test_notification:
            # Test notification
            if config.cfg.global_config.notification_urls:
                success = await get_notifier().send_test()
                if success:
                    logger.success("Test notification sent successfully")
                else:
                    logger.error("Failed to send test notification")
            else:
                logger.warning("No notification URLs configured")
        else:
            # Normal torrent processing flow
            await processor.process_torrents()
    finally:
        # Wait for torrent monitoring to complete all tracked torrents
        client = get_torrent_client()
        if client and client.monitoring:
            logger.debug(
                "Stopping torrent monitoring and waiting for tracked "
                "torrents to complete..."
            )
            await client.wait_for_monitoring_completion()

        # Close all API client sessions
        await cleanup_api()

        # Cleanup database
        await cleanup_database()
