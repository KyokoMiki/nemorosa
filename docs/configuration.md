# Configuration

Nemorosa uses a YAML configuration file to manage all settings. This page covers all available configuration options.

## Configuration File Location

The configuration file is located in your user config directory:

- **Windows**: `%LOCALAPPDATA%\nemorosa\nemorosa\config.yml`
- **macOS**: `~/Library/Application Support/nemorosa/config.yml`
- **Linux**: `~/.config/nemorosa/config.yml`

## Configuration Structure

```yaml
global:
  # Global settings
  loglevel: info
  no_download: false
  exclude_mp3: true
  check_trackers:
    - "flacsfor.me"
    - "home.opsfet.ch"
    - "52dic.vip"
  check_music_only: true
  auto_start_torrents: true

linking:
  # File linking configuration
  enable_linking: false
  link_dirs: []
  link_type: "hardlink"

server:
  # Web server settings
  host: null
  port: 8256
  api_key: "your-api-key"
  search_cadence: "1 day"
  cleanup_cadence: "1 day"

downloader:
  # Torrent client settings
  - type: "qbittorrent"
    url: "http://localhost:8080"
    username: "admin"
    password: "adminadmin"
    api_key: ""
    torrents_dir: ""
    label: "nemorosa"
    tags: null
    use_unified_labels: true
    duplicate_categories: false

target_site:
  # Target tracker settings
  - server: "https://redacted.sh"
    api_key: "your_api_key_here"
  - server: "https://orpheus.network"
    api_key: "your_api_key_here"
```

## Global Settings

### `loglevel`

Controls the verbosity of logging output.

**Options:**

- `debug` - Most verbose, includes all internal operations
- `info` - Standard logging (default)
- `warning` - Warnings and errors only
- `error` - Errors only
- `critical` - Critical errors only

```yaml
global:
  loglevel: info
```

### `no_download`

When enabled, Nemorosa will only find matches but not download torrent files.

```yaml
global:
  no_download: false
```

### `exclude_mp3`

Excludes torrents containing MP3 files from processing.

**Default:** `true`

```yaml
global:
  exclude_mp3: true
```

### `check_trackers`

Filters torrents to only process those from specified trackers. This filter is used to limit only torrents from these sites to be used as sources for searching target sites for cross-seeding opportunities. This is a restriction on the source, not the target.

**Options:**

- `null` - Check all trackers (default)
- List of tracker keywords (any torrent whose tracker contains the keyword will be matched, this is used to support both http and https trackers)

```yaml
global:
  check_trackers:
    - "flacsfor.me"
    - "home.opsfet.ch"
    - "52dic.vip"
```

### `check_music_only`

Only process torrents that contain music files.

**Default:** `true`

```yaml
global:
  check_music_only: true
```

### `auto_start_torrents`

Automatically start torrents after successful injection.

**Default:** `true`

```yaml
global:
  auto_start_torrents: true
```

### `notification_urls`

List of Apprise notification URLs for sending notifications about cross-seeding events.

**Default:** `[]` (no notifications)

**Supported events:**

- Scan completion summaries
- Successful torrent injections
- Failed injections
- Announce processing results

For available notification services and URL formats, see the [Apprise documentation](https://appriseit.com/services).

**Example:**
```yaml
global:
  notification_urls:
    - "ntfy://{token}@{hostname}/{topics}"
    - "mailto://userid:pass@domain.com"
    - "gotify://{hostname}/{token}"
```

## Linking Settings

### `enable_linking`

Enable file linking to avoid duplicate storage.

**Default:** `false`

**Note:** Required for rTorrent clients.

```yaml
linking:
  enable_linking: false
```

### `link_dirs`

List of directories to create links in.

**Required when** `enable_linking` is `true`.

```yaml
linking:
  link_dirs:
    - "/path/to/link/directory1"
    - "/path/to/link/directory2"
```

### `link_type`

Type of link to create.

**Options:**

- `symlink` - Symbolic link
- `hardlink` - Hard link (default)
- `reflink` - Reflink (copy-on-write)
- `reflink_or_copy` - Reflink if possible, otherwise copy

**Default:** `hardlink`

```yaml
linking:
  link_type: "hardlink"
```

## Server Settings

### `host`

Server host address for daemon mode.

**Options:**

- `null` - Listen on all interfaces (ipv4/ipv6) (default)
- Specific IP address (e.g., `127.0.0.1`)

```yaml
server:
  host: null
```

### `port`

Server port for HTTP API.

**Default:** `8256`

```yaml
server:
  port: 8256
```

### `api_key`

API key for HTTP API authentication.

**Options:**

- Default is a random string, automatically generated when creating config.yml
- `null` - No authentication

```yaml
server:
  api_key: "your-secure-random-api-key"
```

### `search_cadence`

How often to run the automatic search job.

**Format:** Human-readable time strings

- `"30 minutes"`
- `"2 hours"`
- `"1 day"`
- `"1 week"`

```yaml
server:
  search_cadence: "1 day"
```

### `cleanup_cadence`

How often to run the cleanup job.

**Default:** `"1 day"`

```yaml
server:
  cleanup_cadence: "1 day"
```

## Downloader Settings

The `downloader` key is a **list**, allowing you to configure one or more torrent clients. Each entry in the list represents a separate client connection.

### `type`

Torrent client type.

**Options:**

- `"qbittorrent"`
- `"transmission"`
- `"deluge"`
- `"rtorrent"`

```yaml
downloader:
  - type: "qbittorrent"
```

### `url`

Torrent client connection URL, without embedded credentials.

**Examples by client:**

- **qBittorrent**: `http://localhost:8080`
- **Transmission**: `http://localhost:9091/transmission/rpc`
- **Deluge**: `deluge://localhost:58846`
- **rTorrent**: `http://localhost:8080/plugins/rpc/rpc.php` or `scgi://localhost:5000`

**Note for rTorrent:** requires `enable_linking: true` in the linking section.

```yaml
downloader:
  - type: "transmission"
    url: "http://localhost:9091/transmission/rpc"
```

### `username`

Client username for authentication.

**Default:** `""` (empty string)

```yaml
downloader:
  - username: "admin"
```

### `password`

Client password for authentication.

**Default:** `""` (empty string)

```yaml
downloader:
  - password: "adminadmin"
```

### `api_key`

API key for qBittorrent authentication (qBittorrent 5.2.0+). This provides a stateless alternative to username/password authentication.

When `api_key` is set, it takes priority over `username`/`password`. You can generate an API key in qBittorrent via **Preferences > Web UI**.

**Default:** `""` (empty string, disabled)

**Note:** Only applicable to qBittorrent clients.

```yaml
downloader:
  - type: "qbittorrent"
    api_key: "your-qbittorrent-api-key"
```

### `torrents_dir`

The directory where your torrent client stores its `.torrent` files. This is required for:

- **Transmission**: All versions
- **Deluge**: All versions
- **rTorrent**: All versions
- **qBittorrent**: Versions prior to 4.5.0

**Common torrent directories by client:**

| Client | Linux | Windows | macOS |
|--------|-------|---------|-------|
| **Deluge** | `/home/<username>/.config/deluge/state` | `%APPDATA%/deluge/state` | Not officially supported |
| **Transmission** | `/home/<username>/.config/transmission/torrents` | `C:/Users/Username/AppData/Roaming/Transmission/torrents` | Unknown |
| **rTorrent** | Check `session.path` in `.rtorrent.rc` | Check `session.path` in `.rtorrent.rc` | Check `session.path` in `.rtorrent.rc` |
| **qBittorrent** | `/home/<username>/.local/share/data/qBittorrent/BT_backup` | `C:/Users/<username>/AppData/Local/qBittorrent/BT_backup` | `~/Library/Application Support/qBittorrent/BT_backup` |

**Important notes:**

- For Windows: Use forward slashes (`/`) in the path, e.g., `C:/Users/<username>/AppData/Local/qBittorrent/BT_backup`
- For qBittorrent 4.5.0+, `torrents_dir` can be left as an empty string or omitted
- If you don't know your client's torrent directory, check the client's configuration or data directory

```yaml
downloader:
  - torrents_dir: "/home/user/.config/transmission/torrents"
```

### `label`

Label/category to assign to injected torrents.

**Default:** `"nemorosa"`

```yaml
downloader:
  - label: "nemorosa"
```

### `tags`

Optional tags list for torrent clients. Only supported by qBittorrent and Transmission.

**Default:** `null`

**Behavior:**

- **qBittorrent**: Tags work together with label
- **Transmission**: Uses tags by default; if `tags` is `null`, falls back to using `[label]`

```yaml
downloader:
  - tags: null
  # Or specify custom tags:
  # - tags:
  #     - "music"
  #     - "cross-seed"
```

### `use_unified_labels`

Use unified label and tags from config file for all injected torrents.

**Default:** `true`

**Behavior:**

- When `true`: All injected torrents use the label/tags specified in the config
- When `false`: Falls back to `duplicate_categories` behavior (qBittorrent/Deluge only)

**Note:** This setting only affects qBittorrent and Deluge. Transmission and rTorrent always use unified labels regardless of this setting.

```yaml
downloader:
  - use_unified_labels: true
```

### `duplicate_categories`

qBittorrent/Deluge specific setting (only takes effect when `use_unified_labels` is `false`).

**Default:** `false`

**Behavior:**

- When `true`: Inject using the same labels/categories as the original torrent
    - Without linking: category will be set to `{original_category}.nemorosa`
    - With linking enabled: category stays as label, but `{original_category}.nemorosa` is added as tag
- When `false`: Use the label specified in config

**Example:** Original category "Music" → injected to "Music.nemorosa"

```yaml
downloader:
  - duplicate_categories: false
```

## Target Site Settings

Configure target trackers for cross-seeding.

### GazelleJSONAPI Sites

For modern Gazelle trackers with API support:

```yaml
target_site:
  - server: "https://redacted.sh"
    api_key: "your_api_key_here"
  - server: "https://orpheus.network"
    api_key: "your_api_key_here"
  - server: "https://gazellegames.net"
    api_key: "your_api_key_here"
```

### Gazelle (Legacy) Sites

For legacy Gazelle trackers that don't support API keys using cookie authentication:

```yaml
target_site:
  - server: "https://dicmusic.com"
    cookie: "your_cookie_here"
  - server: "https://libble.me"
    cookie: "your_cookie_here"
  - server: "https://lztr.me"
    cookie: "your_cookie_here"
  - server: "https://bemaniso.ws"
    cookie: "your_cookie_here"
  - server: "https://jpopsuki.eu"
    cookie: "your_cookie_here"
```