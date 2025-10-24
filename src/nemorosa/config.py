"""Nemorosa configuration processing module."""

import secrets
import sys
from enum import Enum
from pathlib import Path

import msgspec
from humanfriendly import parse_timespan
from platformdirs import user_config_dir

from . import logger

APPNAME = "nemorosa"


class LinkType(Enum):
    """Link type enumeration."""

    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    REFLINK = "reflink"
    REFLINK_OR_COPY = "reflink_or_copy"


class LogLevel(Enum):
    """Log level enumeration."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def validate_string_list(value: list[str] | None, field_name: str) -> None:
    """Validate string list field.

    Args:
        value: String list to validate or None
        field_name: Field name for error messages

    Raises:
        ValueError: Raised when validation fails
    """
    if value is not None:
        if len(value) == 0:
            raise ValueError(f"{field_name} cannot be an empty list")
        for item in value:
            if not item.strip():
                raise ValueError(f"{field_name} cannot contain empty strings")


class LinkingConfig(msgspec.Struct):
    """File linking configuration."""

    enable_linking: bool = False
    link_dirs: list[str] = msgspec.field(default_factory=list)
    link_type: LinkType = LinkType.HARDLINK

    def __post_init__(self):
        # Validate link_dirs when linking is enabled
        if self.enable_linking:
            if not self.link_dirs:
                raise ValueError("link_dirs must be specified when linking is enabled")

            for item in self.link_dirs:
                if not item.strip():
                    raise ValueError("link_dirs cannot contain empty strings")


class GlobalConfig(msgspec.Struct):
    """Global configuration."""

    loglevel: LogLevel = LogLevel.INFO
    no_download: bool = False
    exclude_mp3: bool = True
    check_trackers: list[str] | None = msgspec.field(
        default_factory=lambda: ["flacsfor.me", "home.opsfet.ch", "52dic.vip"]
    )
    check_music_only: bool = True
    auto_start_torrents: bool = True

    def __post_init__(self):
        # Validate check_trackers
        validate_string_list(self.check_trackers, "check_trackers")


class DownloaderConfig(msgspec.Struct):
    """Downloader configuration."""

    client: str = ""
    label: str | None = "nemorosa"
    tags: list[str] | None = None

    def __post_init__(self):
        if not self.client or not self.client.strip():
            raise ValueError("Downloader client URL is required")

        # Validate client URL format
        if not self.client.startswith(("deluge://", "transmission+", "qbittorrent+", "rtorrent+")):
            raise ValueError(f"Invalid client URL format: {self.client}")

        # Validate tags content
        validate_string_list(self.tags, "tags")

        if self.label is not None and not self.label.strip():
            self.label = None


class ServerConfig(msgspec.Struct):
    """Server configuration."""

    host: str | None = None
    port: int = 8256
    api_key: str | None = None
    search_cadence: str | None = "1 day"  # Will be parsed to seconds
    cleanup_cadence: str = "1 day"  # Will be parsed to seconds

    def __post_init__(self):
        # Validate port range
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ValueError(f"Server port must be an integer between 1 and 65535, got: {self.port}")

        # Validate search_cadence
        if self.search_cadence is not None:
            try:
                search_seconds = int(parse_timespan(self.search_cadence))
                if search_seconds <= 0:
                    raise ValueError(f"search_cadence must be greater than 0, got: {search_seconds} seconds")
                self.search_cadence = str(search_seconds)
            except Exception as e:
                raise ValueError(f"Invalid search_cadence '{self.search_cadence}': {e}") from e

        # Validate cleanup_cadence
        try:
            cleanup_seconds = int(parse_timespan(self.cleanup_cadence))
            if cleanup_seconds <= 0:
                raise ValueError(f"cleanup_cadence must be greater than 0, got: {cleanup_seconds} seconds")
            self.cleanup_cadence = str(cleanup_seconds)
        except Exception as e:
            raise ValueError(f"Invalid cleanup_cadence '{self.cleanup_cadence}': {e}") from e


class TargetSiteConfig(msgspec.Struct):
    """Target site configuration."""

    server: str = ""
    api_key: str | None = None
    cookie: str | None = None

    def __post_init__(self):
        if not self.server:
            raise ValueError("Target site server URL is required")

        # Validate server URL format
        if not self.server.startswith(("http://", "https://")):
            raise ValueError(f"Invalid server URL format: {self.server}")

        # At least one of api_key or cookie is required
        if not self.api_key and not self.cookie:
            raise ValueError(f"Target site '{self.server}' must have either api_key or cookie")

        self.server = self.server.rstrip("/")


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

        # Validate rtorrent client requires enable_linking
        if self.downloader.client.startswith("rtorrent+") and not self.linking.enable_linking:
            raise ValueError("rtorrent client requires enable linking")


def get_user_config_path() -> Path:
    """Get configuration file path in user config directory.

    Returns:
        Path: Configuration file path.
    """
    config_dir = user_config_dir(APPNAME)
    return Path(config_dir) / "config.yml"


def find_config_path(config_path: str | None = None) -> Path:
    """Find configuration file path.

    Args:
        config_path: Specified configuration file path, if None uses user config directory.

    Returns:
        Absolute path of the configuration file.
    """
    # Determine the path to check
    path_to_check = Path(config_path) if config_path else get_user_config_path()

    # Check if the path exists and return absolute path
    if path_to_check.exists():
        return path_to_check
    else:
        logger.warning("Configuration file not found. Creating default configuration...")

        # Create default configuration file
        created_path = create_default_config(path_to_check)
        logger.success(f"Default configuration created at: {created_path}")
        logger.info("Please edit the configuration file with your settings and run nemorosa again.")
        logger.info("You can also specify a custom config path with: nemorosa --config /path/to/config.yml")

        # Exit program
        sys.exit(0)


def setup_config(config_path: Path) -> NemorosaConfig:
    """Set up and load configuration.

    Args:
        config_path: Configuration file path.

    Returns:
        NemorosaConfig instance.

    Raises:
        ValueError: Raised when configuration loading or validation fails.
    """
    try:
        # Parse configuration file directly to NemorosaConfig using msgspec
        config = msgspec.yaml.decode(config_path.read_bytes(), type=NemorosaConfig)

        logger.info(f"Configuration loaded successfully from: {config_path}")

        return config

    except msgspec.ValidationError as e:
        raise ValueError(f"Configuration validation error in '{config_path}': {e}") from e
    except Exception as e:
        raise ValueError(f"Error reading config file '{config_path}': {e}") from e


def create_default_config(target_path: Path) -> Path:
    """Create default configuration file.

    Args:
        target_path: Target path for the configuration file.

    Returns:
        Created configuration file path.
    """
    # Create parent directory if it doesn't exist
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Default configuration content
    default_config = f"""# Nemorosa Configuration File

global:
  # Global settings
  loglevel: info # Log level: debug, info, warning, error, critical
  no_download: false # Whether to only check without downloading
  exclude_mp3: true # Whether to exclude MP3 format files
  check_trackers: # List of trackers to check, set to null to check all
    - "flacsfor.me"
    - "home.opsfet.ch"
    - "52dic.vip"
  check_music_only: true # Whether to check music files only
  auto_start_torrents: true # Whether to automatically start torrents after successful injection

linking:
  # File linking configuration
  enable_linking: false # Whether to enable file linking
  link_dirs: [] # List of directories to create links in
  link_type: "hardlink" # Type of link: symlink, hardlink, reflink, reflink_or_copy

server:
  # Web server settings
  host: null # Server host address, null means listen on all interfaces
  port: 8256 # Server port
  api_key: "{secrets.token_urlsafe(32)}" # API key for accessing web interface
  # Scheduled job settings (optional, set to null to disable)
  search_cadence: "1 day" # How often to run search job (e.g., "1 day", "6 hours", "30 minutes")
  cleanup_cadence: "1 day" # How often to run cleanup job

downloader:
  # Downloader settings
  # Supported downloader formats:

  # transmission+http://user:pass@host:port/transmission/rpc?torrents_dir=/path/to/session/
  # deluge://username:password@host:port/?torrents_dir=/path/to/session/
  # qbittorrent+http://username:password@host:port/?torrents_dir=/path/to/session/
  # qbittorrent+http://username:password@host:port  # For qBittorrent 4.5.0+, torrents_dir is not needed

  # For Windows: Use forward slashes (/) in torrents_dir path
  # Example: ?torrents_dir=C:/Users/username/AppData/Local/qBittorrent/BT_backup

  client: ""
  label: "nemorosa" # Download label
  # Optional tags list, only for qBittorrent and Transmission.
  # For qBittorrent, tags work with label
  # For Transmission, default to use tags, if tags is null, use [label] as fallback
  tags: null

target_site:
  # Target site settings
  - server: "https://redacted.sh"
    api_key: "your_api_key_here"
  - server: "https://orpheus.network"
    api_key: "your_api_key_here"
  - server: "https://dicmusic.com"
    cookie: "your_cookie_here" # For sites that don't support API, use cookie instead
"""

    target_path.write_text(default_config, encoding="utf-8")

    return target_path


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

    actual_config_path = find_config_path(config_path)
    cfg = setup_config(actual_config_path)
