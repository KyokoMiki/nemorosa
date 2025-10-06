"""Nemorosa configuration processing module."""

import secrets
import sys
from pathlib import Path

import humanfriendly
import msgspec
from platformdirs import user_config_dir

from . import logger

APPNAME = "nemorosa"

# Get project root directory
root_path = Path(__file__).parent.parent


class LinkingConfig(msgspec.Struct):
    """File linking configuration."""

    enable_linking: bool = False
    link_dirs: list[str] = msgspec.field(default_factory=list)
    link_type: str = "hardlink"

    def __post_init__(self):
        # Validate link_type
        valid_types = ["symlink", "hardlink", "reflink", "reflink_or_copy"]
        if self.link_type not in valid_types:
            raise ValueError(f"Invalid link_type '{self.link_type}'. Must be one of: {valid_types}")

        # Validate link_dirs when linking is enabled
        if self.enable_linking and not self.link_dirs:
            raise ValueError("link_dirs must be specified when linking is enabled")


class GlobalConfig(msgspec.Struct):
    """Global configuration."""

    loglevel: str = "info"
    no_download: bool = False
    exclude_mp3: bool = True
    check_trackers: list[str] | None = msgspec.field(
        default_factory=lambda: ["flacsfor.me", "home.opsfet.ch", "52dic.vip"]
    )
    check_music_only: bool = True
    auto_start_torrents: bool = True

    def __post_init__(self):
        # Validate log level
        valid_levels = ["debug", "info", "warning", "error", "critical"]
        if self.loglevel not in valid_levels:
            raise ValueError(f"Invalid loglevel '{self.loglevel}'. Must be one of: {valid_levels}")

        # Validate check_trackers is a non-empty list or None
        if self.check_trackers is not None:
            if not isinstance(self.check_trackers, list):
                raise ValueError("check_trackers must be a list or None")
            if len(self.check_trackers) == 0:
                raise ValueError("check_trackers must be a non-empty list or None")


class DownloaderConfig(msgspec.Struct):
    """Downloader configuration."""

    client: str = ""
    label: str = "nemorosa"

    def __post_init__(self):
        if not self.client:
            raise ValueError("Downloader client URL is required")

        # Validate client URL format
        if not self.client.startswith(("deluge://", "transmission+", "qbittorrent+")):
            raise ValueError(f"Invalid client URL format: {self.client}")

        # Validate label cannot be empty
        if not self.label or not self.label.strip():
            raise ValueError("Downloader label cannot be empty")


class ServerConfig(msgspec.Struct):
    """Server configuration."""

    host: str | None = None
    port: int = 8256
    api_key: str | None = None
    search_cadence: str | None = None  # Will be parsed to seconds via property
    cleanup_cadence: str = "1 day"  # Will be parsed to seconds via property

    def __post_init__(self):
        # Validate port range
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ValueError(f"Server port must be an integer between 1 and 65535, got: {self.port}")

        # Validate search_cadence
        if self.search_cadence is not None:
            try:
                search_seconds = humanfriendly.parse_timespan(self.search_cadence)
                if search_seconds <= 0:
                    raise ValueError(f"search_cadence must be greater than 0, got: {search_seconds} seconds")
            except Exception as e:
                raise ValueError(f"Invalid search_cadence '{self.search_cadence}': {e}") from e

        # Validate cleanup_cadence
        try:
            cleanup_seconds = humanfriendly.parse_timespan(self.cleanup_cadence)
            if cleanup_seconds <= 0:
                raise ValueError(f"cleanup_cadence must be greater than 0, got: {cleanup_seconds} seconds")
        except Exception as e:
            raise ValueError(f"Invalid cleanup_cadence '{self.cleanup_cadence}': {e}") from e

    @property
    def search_cadence_seconds(self) -> int:
        """Get search cadence in seconds."""
        if self.search_cadence is None:
            return 0
        return int(humanfriendly.parse_timespan(self.search_cadence))

    @property
    def cleanup_cadence_seconds(self) -> int:
        """Get cleanup cadence in seconds."""
        return int(humanfriendly.parse_timespan(self.cleanup_cadence))


class TargetSiteConfig(msgspec.Struct):
    """Target site configuration."""

    server: str = ""
    api_key: str | None = None
    cookie: str | None = None

    def __post_init__(self):
        if not self.server:
            raise ValueError("Target site server URL is required")

        # At least one of api_key or cookie is required
        if not self.api_key and not self.cookie:
            raise ValueError(f"Target site '{self.server}' must have either api_key or cookie")

        # Validate server URL format
        if not self.server.startswith(("http://", "https://")):
            raise ValueError(f"Invalid server URL format: {self.server}")


class NemorosaConfig(msgspec.Struct):
    """Nemorosa main configuration class."""

    global_config: GlobalConfig = msgspec.field(name="global", default_factory=GlobalConfig)
    downloader: DownloaderConfig = msgspec.field(default_factory=DownloaderConfig)
    server: ServerConfig = msgspec.field(default_factory=ServerConfig)
    target_sites: list[TargetSiteConfig] = msgspec.field(name="target_site", default_factory=list)
    linking: LinkingConfig = msgspec.field(default_factory=LinkingConfig)

    def __post_init__(self):
        # Validate target_sites
        if not isinstance(self.target_sites, list):
            raise ValueError("target_site must be a list")

        # Validate each target_site configuration
        for i, site in enumerate(self.target_sites):
            if not isinstance(site, TargetSiteConfig):
                raise ValueError(f"Error in target_site[{i}]: must be TargetSiteConfig instance")


def get_user_config_path() -> str:
    """Get configuration file path in user config directory.

    Returns:
        str: Configuration file path.
    """
    config_dir = user_config_dir(APPNAME)
    return str(Path(config_dir) / "config.yml")


def get_config_dir() -> str:
    """Get configuration file directory path.

    Returns:
        str: Configuration file directory path.
    """
    config_file_path = get_user_config_path()
    return str(Path(config_file_path).parent)


def find_config_path(config_path: str | None = None) -> str:
    """Find configuration file path.

    Args:
        config_path: Specified configuration file path, if None uses user config directory.

    Returns:
        Absolute path of the configuration file.

    Raises:
        FileNotFoundError: Raised when configuration file is not found.
    """
    if config_path:
        # Use specified configuration file path
        config_path_obj = Path(config_path)
        if config_path_obj.exists():
            return str(config_path_obj.absolute())
        else:
            raise FileNotFoundError(f"Specified config file not found: {config_path_obj}")

    # Only use user configuration directory
    user_config_path = Path(get_user_config_path())
    if user_config_path.exists():
        return str(user_config_path.absolute())

    raise FileNotFoundError(f"Config file not found at: {user_config_path}")


def setup_config(config_path: str | None = None) -> NemorosaConfig:
    """Set up and load configuration.

    Args:
        config_path: Configuration file path, if None auto-detect.

    Returns:
        NemorosaConfig instance.

    Raises:
        ValueError: Raised when configuration loading or validation fails.
    """
    actual_config_path = None
    try:
        # Find configuration file
        actual_config_path = find_config_path(config_path)

        # Parse configuration file directly to NemorosaConfig using msgspec
        with open(actual_config_path, "rb") as f:
            config = msgspec.yaml.decode(f.read(), type=NemorosaConfig)

        return config

    except FileNotFoundError as e:
        raise ValueError(f"Configuration file not found: {e}") from e
    except msgspec.ValidationError as e:
        raise ValueError(f"Configuration validation error in '{actual_config_path}': {e}") from e
    except Exception as e:
        raise ValueError(f"Error reading config file '{actual_config_path}': {e}") from e


def create_default_config(target_path: str | None = None) -> str:
    """Create default configuration file.

    Args:
        target_path: Target path, if None create in user config directory.

    Returns:
        Created configuration file path.
    """
    if target_path is None:
        target_path = get_user_config_path()

    target_path_obj = Path(target_path)
    target_path_obj.parent.mkdir(parents=True, exist_ok=True)

    # Default configuration content
    default_config = f"""# Nemorosa Configuration File

global:
  # Global settings
  loglevel: info  # Log level: debug, info, warning, error, critical
  no_download: false  # Whether to only check without downloading
  exclude_mp3: true  # Whether to exclude MP3 format files
  check_trackers:  # List of trackers to check, set to null to check all
    - "flacsfor.me"
    - "home.opsfet.ch" 
    - "52dic.vip"
  check_music_only: true  # Whether to check music files only
  auto_start_torrents: true  # Whether to automatically start torrents after successful injection

linking:
  # File linking configuration
  enable_linking: false  # Whether to enable file linking
  link_dirs: []  # List of directories to create links in
  link_type: "hardlink"  # Type of link: symlink, hardlink, reflink, reflink_or_copy

server:
  # Web server settings
  host: null  # Server host address, null means listen on all interfaces
  port: 8256  # Server port
  api_key: {secrets.token_urlsafe(32)}  # API key for accessing web interface
  # Scheduled job settings (optional, set to null to disable)
  search_cadence: "1 day"  # How often to run search job (e.g., "1 day", "6 hours", "30 minutes")
  cleanup_cadence: "1 day"  # How often to run cleanup job

downloader:
  # Downloader settings
  # Supported downloader formats:

  # transmission+http://user:pass@host:port/transmission/rpc?torrents_dir=/path/to/session/
  # deluge://username:password@host:port/?torrents_dir=/path/to/session/
  # qbittorrent+http://username:password@host:port/?torrents_dir=/path/to/session/
  # qbittorrent+http://username:password@host:port  # For qBittorrent 4.5.0+, torrents_dir is not needed

  # For Windows: Use forward slashes (/) in torrents_dir path
  # Example: ?torrents_dir=C:/Users/username/AppData/Local/qBittorrent/BT_backup

  client: null
  label: "nemorosa"  # Download label (cannot be empty)

target_site:
  # Target site settings
  - server: "https://redacted.sh"
    api_key: "your_api_key_here"
  - server: "https://orpheus.network"
    api_key: "your_api_key_here"
  - server: "https://dicmusic.com"
    cookie: "your_cookie_here" # For sites that don't support API, use cookie instead
"""

    with open(target_path_obj, "w", encoding="utf-8") as f:
        f.write(default_config)

    return str(target_path_obj)


# Global configuration object
cfg: NemorosaConfig


def init_config(config_path: str | None = None) -> None:
    """Initialize global configuration object.

    Args:
        config_path: Configuration file path, if None auto-detect.

    Raises:
        ValueError: Raised when configuration loading or validation fails.
    """
    global cfg

    try:
        cfg = setup_config(config_path)
        # Log successful configuration loading
        log = logger.get_logger()
        actual_config_path = find_config_path(config_path)
        log.info(f"Configuration loaded successfully from: {actual_config_path}")
    except ValueError as e:
        if "Configuration file not found" in str(e):
            # Configuration file doesn't exist, create default configuration file
            log = logger.get_logger()
            log.warning("Configuration file not found. Creating default configuration...")

            # Determine configuration file path
            default_config_path = config_path or get_user_config_path()

            # Create default configuration file
            created_path = create_default_config(default_config_path)
            log.success(f"Default configuration created at: {created_path}")
            log.info("Please edit the configuration file with your settings and run nemorosa again.")
            log.info("You can also specify a custom config path with: nemorosa --config /path/to/config.yml")

            # Exit program
            sys.exit(0)
        else:
            # Other configuration errors, re-raise
            raise
