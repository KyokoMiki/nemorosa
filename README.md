<div align="center"><img width="250px" src="https://cdn.jsdelivr.net/gh/KyokoMiki/nemorosa@main/src/nemorosa/static/nemorosa.svg"/></div>

<h1 align="center">nemorosa</h1>

<div align="center">

[![PyPI version](https://badgen.net/pypi/v/nemorosa)](https://pypi.org/project/nemorosa/)
[![PyPI - Python Version](https://badgen.net/pypi/python/nemorosa)](https://pypi.org/project/nemorosa/)
[![CI](https://github.com/KyokoMiki/nemorosa/actions/workflows/release.yml/badge.svg)](https://github.com/KyokoMiki/nemorosa/actions/workflows/release.yml)
[![License](https://badgen.net/github/license/KyokoMiki/nemorosa)](https://github.com/KyokoMiki/nemorosa/blob/main/LICENSE)

</div>

`nemorosa` is a specialized cross-seeding tool designed specifically for music torrents, designed to work alongside [cross-seed](https://github.com/cross-seed/cross-seed).

Compared to existing music torrent cross-seeding tools, nemorosa offers the strongest matching capabilities and the widest range of supported trackers, and the most natural and human-friendly user experience.

Unlike traditional music torrent cross-seeding tools that can only match torrents with identical hashes, nemorosa excels at partial matching and automatic file mapping, enabling cross-seeding from **any source site** to Gazelle-based trackers.

## Features

- **Natural User Experience**: Automatically reads torrents from your client, finds cross-seeding opportunities, and seamlessly injects matched torrents
- **Advanced Partial Matching**: Handles cases like different block sizes, missing artwork, or modified covers that would fail with traditional tools
- **Automatic File Mapping**: Automatically renames folders and files to match your existing content, including handling zero-width spaces and other encoding issues seamlessly, entirely through torrent client's built-in renaming functionality without hard/soft links, so you don't need to worry about linking problems.
- **Wide Site Support**: Supports cross-seeding from **any source site** (including non-Gazelle trackers, public trackers, or any other torrent source) to Gazelle-based target trackers:
  - **GazelleJSONAPI**: RED/OPS/DIC (modern Gazelle with API support)
  - **Gazelle (Legacy)**: LZTR/Libble (legacy Gazelle with parser support)
- **Web Server Mode**: HTTP API and webhook support for integration with other tools and automation
  - **Scheduled Jobs**: Automated search and cleanup tasks with configurable intervals
  - **Announce Matching**: Automatically matches cross-seeds from IRC announces or RSS feeds by processing torrent announces from autobrr.
  - **Triggering Searches**: Enables immediate cross-seed searches when torrents finish downloading by adding on-completion scripts to your torrent client that call nemorosa's HTTP API.
- **Smart Retry System**: Automatically retry failed downloads and track undownloaded torrents
- **Multi-Client Support**: Works with Transmission, qBittorrent, and Deluge

## Prerequisites

- Python 3.11+
- One of the supported torrent clients with remote access enabled:
  - **Transmission**
  - **qBittorrent**
  - **Deluge**

  **Note**: If using Transmission, Deluge, or qBittorrent versions prior to 4.5.0, nemorosa needs access to the client's torrents directory. When running in Docker, ensure you map the torrents directory to the nemorosa container.
- Access to Gazelle-based target trackers for cross-seeding (**source sites can be ANY type**):
  - **GazelleJSONAPI**: RED, OPS, DIC
  - **Gazelle (Legacy)**: LZTR, Libble
- Valid API key or cookie for target tracker authentication

## Usage

For detailed usage instructions, please refer to our [Wiki](https://github.com/KyokoMiki/nemorosa/wiki).

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the GPL-3.0 License - see the [LICENSE](https://github.com/KyokoMiki/nemorosa/blob/main/LICENSE) file for details.
