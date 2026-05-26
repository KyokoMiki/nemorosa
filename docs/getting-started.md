# Getting Started

This guide will help you install and configure Nemorosa for the first time.

## Prerequisites

- Python 3.11+

- One of the supported torrent clients with remote access enabled:

    - **Transmission**
    - **qBittorrent**
    - **Deluge**
    - **rTorrent**

    **Note**: If using qBittorrent < 4.5.0, Transmission, Deluge, or rTorrent, `nemorosa` needs access to the client's torrents directory. When running in Docker, ensure you map the torrents directory to the `nemorosa` container.

- Access to Gazelle-based target trackers for cross-seeding (**source sites can be ANY type**):

    - **GazelleJSONAPI**: RED, OPS, DIC, Bemaniso
    - **GazelleGamesNet**: GazelleGames.net (GGn)
    - **Gazelle (Legacy)**: LZTR, Libble, JPopsuki

- Valid API key or cookie for target tracker authentication

## Installation

These steps use uv for installing the nemorosa package. pipx also works. Installing with pip is not recommended because uv (and pipx) manage python versions and isolate the nemorosa installation from the system python installation.

### Installing uv (if not already installed)

If you don't have uv installed, you can install it using one of these methods:

**On Windows:**
```powershell
# Using PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**On macOS and Linux:**
```bash
# Using curl
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Installing Nemorosa

#### Using uv (recommended)

```bash
uv tool install nemorosa
```

#### Using pipx

```bash
pipx install nemorosa
```

#### Using Docker

If you prefer to run Nemorosa in a containerized environment:

```bash
# Pull the latest image
docker pull ghcr.io/kyokomiki/nemorosa:latest

# Run with default configuration
docker run --rm -v ./data:/app/data -v /path/to/your/torrents:/app/torrents ghcr.io/kyokomiki/nemorosa

# Run in server mode
docker run -d --name nemorosa \
  -p 8256:8256 \
  -v /path/to/your/config:/app/data \
  -v /path/to/your/torrents:/app/torrents \
  ghcr.io/kyokomiki/nemorosa:latest --server
```

**Docker Compose example:**

```yaml
services:
  nemorosa:
    image: ghcr.io/kyokomiki/nemorosa:latest
    container_name: nemorosa
    # user: "1000:1000"
    volumes:
      - /path/to/your/config:/app/data
      - /path/to/your/torrents:/app/torrents  # Map your torrent client's torrents directory
    ports:
      - "8256:8256"
    command: "--server"
```

## First Run

When you run Nemorosa for the first time, it will automatically create a default configuration file and then exit.

The program will:

- Create a default configuration file in your user config directory
- Display the path to the configuration file
- The generated configuration file includes comments that you can reference for configuration.

## Basic Configuration

Edit the generated `config.yml` file with your settings. Most configuration options are documented with comments in the generated config file. For more detailed configuration options, see the [Configuration](configuration.md) page.

### Configure Torrent Client

The `torrents_dir` parameter points to the directory where your torrent client stores its `.torrent` files. This is required for:

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
- For qBittorrent 4.5.0+, the `torrents_dir` parameter is not needed
- If you don't know your client's torrent directory, check the client's configuration or data directory

## Running Nemorosa

### Server Mode

After configuring the config, you can run nemorosa. The recommended way to run Nemorosa is in server mode, which is also the default mode when running with Docker Compose. Server mode provides:

- **Automatic scheduling** - Periodically scans for cross-seeding opportunities
- **HTTP API** - Integration with external tools and automation
- **Webhook support** - Process announces from IRC, RSS feeds, or autobrr
- **Background processing** - Runs continuously without manual intervention

```bash
nemorosa -s
```

If you use Docker to run, it defaults to server mode. If you need to use other modes, please modify the corresponding parameters in the docker command or the command field in docker-compose.yml.

Other CLI usage includes the following:

### One-time Processing

Process all torrents in your client once. After processing, it will automatically retry downloading torrents that failed during the previous processing and perform post-processing.

```bash
nemorosa
```

### Process Single Torrent

To process a specific torrent by infohash:

```bash
nemorosa -t <infohash>
```

### Post-process Injected Torrents

Post-process is the process of determining whether torrents with non-100% progress are missing files (can coexist with local torrents) or have content conflicts with local torrents that need to be deleted after injecting torrents into the client.

If you process all torrents without parameters, post-processing will be automatically called after processing. Generally, you don't need to manually execute this.

```bash
nemorosa -p
```

### Retry Undownloaded Torrents

Retry downloading torrents that failed during the previous processing. This will be automatically called after processing by default. Generally, you don't need to manually execute this.

```bash
nemorosa -r
```

### Test Notifications

Send a test notification to verify your notification configuration:

```bash
nemorosa --test-notification
```

## Next Steps

Once you have the daemon up and running, here are a few additional features to get the most out of nemorosa:

- [Managing the daemon](managing-the-daemon.md) with Docker Compose or systemd for long-term use
- [Listening for announces](announce-matching.md) to immediately cross-seed when new torrents are announced
- [Configure torrent client](triggering-searches.md) to send cross-seeding webhooks to nemorosa when torrents complete downloading in the client
