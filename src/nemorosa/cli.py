"""Command line interface for nemorosa."""

import argparse
import atexit
import signal
import sys

from colorama import init

from . import api, client_instance, config, db, logger
from .core import NemorosaCore
from .webserver import run_webserver


class CustomHelpFormatter(argparse.HelpFormatter):
    """Custom help formatter."""

    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=80)

    def _format_action_invocation(self, action):
        if not action.option_strings or action.nargs == 0:
            return super()._format_action_invocation(action)
        default = self._get_default_metavar_for_optional(action)
        args_string = self._format_args(action, default)
        return ", ".join(action.option_strings) + " " + args_string


def cleanup_resources():
    """Cleanup resources on program exit."""
    try:
        app_logger = logger.get_logger()
        if app_logger:
            app_logger.debug("Cleaning up resources...")

        # Cleanup database
        db.cleanup_database()

        if app_logger:
            app_logger.debug("Resource cleanup completed")
    except Exception as e:
        # Use print to avoid logger issues during cleanup
        print(f"Warning: Error during cleanup: {e}")


def signal_handler(signum, frame):
    """Handle interrupt signals."""
    print(f"\nReceived signal {signum}, cleaning up...")
    cleanup_resources()
    sys.exit(0)


def setup_argument_parser(config_defaults):
    """Set up command line argument parser.

    Args:
        config_defaults (dict): Default configuration values.

    Returns:
        tuple: A tuple containing (pre_parser, parser).
    """
    # Step 1: Pre-parse to get config file path
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument(
        "--config",
        default=None,  # Let config module auto-find configuration file
        help="Path to YAML configuration file",
    )

    # Main parser
    parser = argparse.ArgumentParser(
        description="Music torrent cross-seeding tool with automatic file mapping and seamless injection",
        formatter_class=CustomHelpFormatter,
        parents=[pre_parser],  # Include pre-parser arguments
    )

    # torrent client option
    client_group = parser.add_argument_group("Torrent client options")
    client_group.add_argument(
        "--client",
        required=not config_defaults.get("client"),
        help="Torrent client URL (e.g. transmission+http://user:pass@localhost:9091)",
        default=config_defaults.get("client"),
    )

    # no download option
    parser.add_argument(
        "--no-download",
        action="store_true",
        default=config_defaults.get("no_download", False),
        help="if set, don't download .torrent files, only save URLs",
    )

    # retry undownloaded option
    parser.add_argument(
        "-r",
        "--retry-undownloaded",
        action="store_true",
        default=False,
        help="retry downloading torrents from undownloaded_torrents table",
    )

    # post-process injected torrents option
    parser.add_argument(
        "-p",
        "--post-process",
        action="store_true",
        default=False,
        help="post-process injected torrents",
    )

    # server mode option
    parser.add_argument(
        "-s",
        "--server",
        action="store_true",
        default=False,
        help="start nemorosa in server mode",
    )

    # single torrent option
    parser.add_argument(
        "-t",
        "--torrent",
        type=str,
        help="process a single torrent by infohash",
    )

    # server options
    server_group = parser.add_argument_group("Server options")
    server_group.add_argument(
        "--host",
        default=config_defaults.get("server_host", None),
        help=f"server host (default: {config_defaults.get('server_host', None)})",
    )
    server_group.add_argument(
        "--port",
        type=int,
        default=config_defaults.get("server_port", 8256),
        help=f"server port (default: {config_defaults.get('server_port', 8256)})",
    )

    # log level
    parser.add_argument(
        "-l",
        "--loglevel",
        metavar="LOGLEVEL",
        default=config_defaults.get("loglevel", "info"),
        choices=["debug", "info", "warning", "error", "critical"],
        help="loglevel for log file (default: %(default)s)",
    )

    return pre_parser, parser


def setup_logger_and_config(pre_args):
    """Set up logger and configuration.

    Args:
        pre_args: Pre-parsed arguments containing config file path.

    Returns:
        logger: Application logger instance.
    """
    app_logger = logger.get_logger()

    # Initialize database
    try:
        db.get_database()
        app_logger.info("Database initialized successfully")
    except Exception as e:
        app_logger.warning(f"Database initialization failed: {e}")

    # Use new configuration processing module to initialize global config
    try:
        config.init_config(pre_args.config)
        app_logger.info("Configuration loaded successfully")
    except ValueError as e:
        app_logger.error(f"Configuration error: {e}")
        app_logger.error("Please check your configuration file and try again")
        sys.exit(1)

    return app_logger


def main():
    """Main function."""
    # Initialize colorama
    init(autoreset=True)

    # Register cleanup handlers
    atexit.register(cleanup_resources)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Step 1: Pre-parse configuration
    pre_parser, parser = setup_argument_parser({})
    pre_args, _ = pre_parser.parse_known_args()

    # Set up logger and configuration
    app_logger = setup_logger_and_config(pre_args)

    # Merge configuration (command line arguments will override config file)
    config_defaults = {
        "loglevel": config.cfg.global_config.loglevel,
        "no_download": config.cfg.global_config.no_download,
        "client": config.cfg.downloader.client,
        "server_host": config.cfg.server.host,
        "server_port": config.cfg.server.port,
    }

    # Re-setup parser with configuration default values
    pre_parser, parser = setup_argument_parser(config_defaults)
    args = parser.parse_args()

    # Set up global logger
    app_logger = logger.generate_logger(args.loglevel)
    logger.set_logger(app_logger)

    # Log configuration summary
    app_logger.section("===== Configuration Summary =====")
    app_logger.debug(f"Config file: {pre_args.config or 'auto-detected'}")
    app_logger.debug(f"No download: {args.no_download}")
    app_logger.debug(f"Log level: {args.loglevel}")
    app_logger.debug(f"Client URL: {args.client}")
    check_trackers = config.cfg.global_config.check_trackers
    app_logger.debug(f"CHECK_TRACKERS: {check_trackers if check_trackers else 'All trackers allowed'}")

    # Display target sites configuration
    app_logger.debug(f"Target sites configured: {len(config.cfg.target_sites)}")
    for i, site in enumerate(config.cfg.target_sites, 1):
        app_logger.debug(f"  Site {i}: {site.server}")

    app_logger.section("===== Nemorosa Starting =====")

    try:
        app_logger.section("===== Connecting to Torrent Client =====")
        app_logger.debug("Connecting to torrent client at %s...", args.client)
        app_torrent_client = client_instance.create_torrent_client(args.client)
        client_instance.set_torrent_client(app_torrent_client)
        app_logger.success("Successfully connected to torrent client")

        # Decide operation based on command line arguments
        if args.server:
            # Server mode
            display_host = args.host if args.host is not None else "all interfaces (IPv4/IPv6)"
            app_logger.info(f"Starting server mode on {display_host}:{args.port}")

            run_webserver(
                host=args.host,
                port=args.port,
                log_level=args.loglevel,
            )
        else:
            # Non-server modes - use asyncio
            import asyncio

            asyncio.run(_async_main(args))
    except Exception as e:
        app_logger.critical("Error connecting to torrent client: %s", e)
        sys.exit(1)

    app_logger.section("===== Nemorosa Finished =====")


async def _async_main(args):
    """Async main function for non-server operations."""
    app_logger = logger.get_logger()

    try:
        # Establish API connections in async context
        target_apis = await api.setup_api_connections(config.cfg.target_sites)
        api.set_target_apis(target_apis)

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
                    f"Stats - Found: {stats.get('found', 0)}, "
                    f"Downloaded: {stats.get('downloaded', 0)}, "
                    f"Scanned: {stats.get('scanned', 0)}"
                )
        elif args.retry_undownloaded:
            # Re-download undownloaded torrents
            await processor.retry_undownloaded_torrents()
        elif args.post_process:
            # Post-process injected torrents only
            processor.post_process_injected_torrents()
        else:
            # Normal torrent processing flow
            await processor.process_torrents()
    finally:
        # Wait for torrent monitoring to complete all tracked torrents
        client = client_instance.get_torrent_client()
        if client and client._monitoring:
            app_logger.debug("Stopping torrent monitoring and waiting for tracked torrents to complete...")
            await client.stop_monitoring()
