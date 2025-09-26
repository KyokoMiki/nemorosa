"""Command line interface for nemorosa."""

import argparse
import http.cookies
import sys

import requests.cookies
from colorama import init

from . import api, config, db, logger, torrent_client
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


def setup_target_sites(app_logger):
    """Set up target sites configuration.

    Args:
        app_logger: Application logger instance.

    Returns:
        list: List of target site configurations.
    """
    target_sites = []

    # Get target_site configuration from config object
    if config.cfg.target_sites:
        for site_config in config.cfg.target_sites:
            site_cookies = None
            if site_config.cookie:
                simple_cookie = http.cookies.SimpleCookie(site_config.cookie)
                site_cookies = requests.cookies.RequestsCookieJar()
                site_cookies.update(simple_cookie)  # pyright: ignore[reportArgumentType]

            target_sites.append(
                {
                    "server": site_config.server,
                    "api_key": site_config.api_key,
                    "cookies": site_cookies,
                }
            )
    else:
        app_logger.critical(
            "No target sites configured in config file. Please add 'target_site' section to your config.yml"
        )
        sys.exit(1)

    return target_sites


def setup_api_connections(target_sites, app_logger):
    """Establish API connections.

    Args:
        target_sites (list): List of target site configurations.
        app_logger: Application logger instance.

    Returns:
        list: List of established API connections.
    """
    app_logger.section("===== Establishing API Connections =====")
    target_apis = []

    for i, site in enumerate(target_sites):
        app_logger.debug(f"Connecting to target site {i + 1}/{len(target_sites)}: {site['server']}")
        try:
            api_instance = api.get_api_instance(server=site["server"], api_key=site["api_key"], cookies=site["cookies"])
            target_apis.append(api_instance)
            app_logger.success(f"API connection established for {site['server']}")
        except Exception as e:
            app_logger.error(f"API connection failed for {site['server']}: {str(e)}")
            # Continue processing other sites, don't exit program

    if not target_apis:
        app_logger.critical("No API connections were successful. Exiting.")
        sys.exit(1)

    app_logger.success(f"Successfully connected to {len(target_apis)} target site(s)")
    return target_apis


def main():
    """Main function."""
    # Initialize colorama
    init(autoreset=True)

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

    # Set up target sites
    target_sites = setup_target_sites(app_logger)

    # Establish API connections
    target_apis = setup_api_connections(target_sites, app_logger)

    # Set global target_apis
    api.set_target_apis(target_apis)

    try:
        app_logger.section("===== Connecting to Torrent Client =====")
        app_logger.debug("Connecting to torrent client at %s...", args.client)
        app_torrent_client = torrent_client.create_torrent_client(args.client)
        torrent_client.set_torrent_client(app_torrent_client)
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
        elif args.torrent:
            # Single torrent mode
            processor = NemorosaCore()
            app_logger.debug(f"Processing single torrent: {args.torrent}")
            result = processor.process_single_torrent(args.torrent)

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
            processor = NemorosaCore()
            processor.retry_undownloaded_torrents()
        elif args.process_completed_matches:
            # Post-process injected torrents only
            processor = NemorosaCore()
            processor.post_process_injected_torrents()
        else:
            # Normal torrent processing flow
            processor = NemorosaCore()
            processor.process_torrents()
            processor.retry_undownloaded_torrents()
            processor.post_process_injected_torrents()
    except Exception as e:
        app_logger.critical("Error connecting to torrent client: %s", e)
        sys.exit(1)

    app_logger.section("===== Nemorosa Finished =====")


if __name__ == "__main__":
    main()
