"""Command line interface for nemorosa."""

import argparse
import sys

from colorama import init

from . import api, client_instance, config, db, logger, scheduler
from .core import NemorosaCore
from .webserver import run_webserver


def setup_argument_parser():
    """Set up command line argument parser.

    Returns:
        argparse.ArgumentParser: Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Music torrent cross-seeding tool with automatic file mapping and seamless injection"
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


def setup_logger_and_config(config_path):
    """Set up logger and configuration.

    Args:
        config_path: Path to configuration file (or None for auto-detection).

    Returns:
        logger: Application logger instance.
    """
    app_logger = logger.get_logger()

    # Use new configuration processing module to initialize global config
    try:
        config.init_config(config_path)
        app_logger.info("Configuration loaded successfully")
    except ValueError as e:
        app_logger.error(f"Configuration error: {e}")
        app_logger.error("Please check your configuration file and try again")
        sys.exit(1)

    return app_logger


def override_config_with_args(args):
    """Override configuration with command line arguments.

    Args:
        args: Parsed command line arguments.
    """
    # Override loglevel if specified
    if args.loglevel is not None:
        config.cfg.global_config.loglevel = args.loglevel

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
    """Initialize core components asynchronously (database, API connections, scheduler, torrent client).

    This function is used by both CLI and webserver modes to set up the application.
    """
    app_logger = logger.get_logger()

    # Initialize database tables
    database = db.get_database()
    await database.init_database()
    app_logger.info("Database initialized successfully")

    # Connect to torrent client
    app_logger.debug("Connecting to torrent client at %s...", config.cfg.downloader.client)
    app_torrent_client = client_instance.create_torrent_client(config.cfg.downloader.client)
    client_instance.set_torrent_client(app_torrent_client)
    app_logger.info("Successfully connected to torrent client")

    # Check if client URL has changed and rebuild cache if needed
    current_client_url = config.cfg.downloader.client
    cached_client_url = await database.get_metadata("client_url")

    if cached_client_url != current_client_url:
        app_logger.info(f"Client URL changed from {cached_client_url or 'none'} to {current_client_url}")
        app_logger.info("Rebuilding client torrents cache...")

        # Get all torrents from the new client
        all_torrents = app_torrent_client.get_torrents(
            fields=["hash", "name", "total_size", "files", "trackers", "download_dir"]
        )

        # Validate that the new client has torrents
        if not all_torrents:
            raise RuntimeError(f"New client at {current_client_url} has no torrents.")

        # Rebuild cache
        await app_torrent_client.rebuild_client_torrents_cache(all_torrents)
        app_logger.success(f"Rebuilt cache with {len(all_torrents)} torrents from new client")

        # Update cached client URL
        await database.set_metadata("client_url", current_client_url)

    # Setup API connections
    target_apis = await api.setup_api_connections(config.cfg.target_sites)
    api.set_target_apis(target_apis)
    app_logger.info(f"API connections established for {len(target_apis)} target sites")

    # Start scheduler
    job_manager = scheduler.get_job_manager()
    await job_manager.start_scheduler()


def main():
    """Main function."""
    # Initialize colorama
    init(autoreset=True)

    # Step 1: Parse command line arguments
    parser = setup_argument_parser()
    args = parser.parse_args()

    # Step 2: Load configuration
    setup_logger_and_config(args.config)

    # Step 3: Override configuration with command line arguments
    override_config_with_args(args)

    # Step 4: Set up global logger with final loglevel from config
    app_logger = logger.generate_logger(config.cfg.global_config.loglevel)
    logger.set_logger(app_logger)

    # Log configuration summary
    app_logger.section("===== Configuration Summary =====")
    app_logger.debug(f"Config file: {args.config or 'auto-detected'}")
    app_logger.debug(f"No download: {config.cfg.global_config.no_download}")
    app_logger.debug(f"Log level: {config.cfg.global_config.loglevel}")
    app_logger.debug(f"Client URL: {config.cfg.downloader.client}")
    check_trackers = config.cfg.global_config.check_trackers
    app_logger.debug(f"CHECK_TRACKERS: {check_trackers if check_trackers else 'All trackers allowed'}")

    # Display target sites configuration
    app_logger.debug(f"Target sites configured: {len(config.cfg.target_sites)}")
    for i, site in enumerate(config.cfg.target_sites, 1):
        app_logger.debug(f"  Site {i}: {site.server}")

    app_logger.section("===== Nemorosa Starting =====")

    try:
        # Decide operation based on command line arguments
        if args.server:
            # Server mode
            display_host = (
                config.cfg.server.host if config.cfg.server.host is not None else "all interfaces (IPv4/IPv6)"
            )
            app_logger.info(f"Starting server mode on {display_host}:{config.cfg.server.port}")

            run_webserver(
                host=config.cfg.server.host,
                port=config.cfg.server.port,
                log_level=config.cfg.global_config.loglevel,
            )
        else:
            # Non-server modes - use asyncio
            import asyncio

            asyncio.run(_async_main(args))
    except Exception as e:
        app_logger.critical("Error during initialization: %s", e)
        sys.exit(1)

    app_logger.section("===== Nemorosa Finished =====")


async def _async_main(args):
    """Async main function for non-server operations."""
    app_logger = logger.get_logger()

    try:
        # Initialize core components (database, API connections, scheduler)
        await async_init()

        # Create processor instance
        processor = NemorosaCore()

        if args.torrent:
            # Single torrent mode
            app_logger.debug(f"Processing single torrent: {args.torrent}")
            result = await processor.process_single_torrent(args.torrent)

            # Print result
            app_logger.debug(f"Processing result: {result['status']}")
            app_logger.debug(f"Message: {result['message']}")
            if result.get("torrent_name"):
                app_logger.debug(f"Torrent name: {result['torrent_name']}")
            if result.get("infohash"):
                app_logger.debug(f"Torrent infohash: {result['infohash']}")
            if result.get("existing_trackers"):
                app_logger.debug(f"Existing trackers: {result['existing_trackers']}")
            if result.get("stats"):
                stats = result["stats"]
                app_logger.debug(
                    f"Stats - Found: {stats.found}, Downloaded: {stats.downloaded}, Scanned: {stats.scanned}"
                )
        elif args.retry_undownloaded:
            # Re-download undownloaded torrents
            await processor.retry_undownloaded_torrents()
        elif args.post_process:
            # Post-process injected torrents only
            await processor.post_process_injected_torrents()
        else:
            # Normal torrent processing flow
            await processor.process_torrents()
    finally:
        # Wait for torrent monitoring to complete all tracked torrents
        client = client_instance.get_torrent_client()
        if client and client.monitoring:
            app_logger.debug("Stopping torrent monitoring and waiting for tracked torrents to complete...")
            await client.wait_for_monitoring_completion()

        await db.cleanup_database()
