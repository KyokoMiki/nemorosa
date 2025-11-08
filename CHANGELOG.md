# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased](https://github.com/KyokoMiki/nemorosa/compare/0.4.1...HEAD)

## [0.4.1](https://github.com/KyokoMiki/nemorosa/compare/0.4.0...0.4.1) - 2025-11-08

### Changed

- **Password Redaction in Logs**: Changed log output to automatically redact passwords from URLs, preventing sensitive credentials from appearing in log messages and improving security

### Fixed

- **Docker Entrypoint Parameter Passing**: Fixed parameter passing issue in Docker entrypoint script
- **Database Operations During Shutdown**: Protected database operations from cancellation during shutdown by wrapping cache sync operations with `asyncio.shield()`, ensuring clean exit during shutdown operations

### What's Changed

* fix(clients): protect database operations from cancellation during shutdown by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/44
* feat(logging): redact passwords from URLs in log messages by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/45

**Full Changelog**: https://github.com/KyokoMiki/nemorosa/compare/0.4.0...0.4.1

## [0.4.0](https://github.com/KyokoMiki/nemorosa/compare/0.3.0...0.4.0) - 2025-11-07

### Added

- **Directory Creation Mode Configuration**: Added `dir_mode` configuration option for file linking, allowing customization of directory permission mode when creating link directories (default: 775). This enables proper write permissions for torrent clients
- **Docker Timezone Configuration**: Added support for timezone configuration via `TZ` environment variable in Docker containers

### Changed

- **Announce API Endpoint**: Changed `/api/announce` endpoint to accept `album` (string) instead of `torrentdata` (base64 encoded torrent data). Torrent files are now only downloaded after finding an album name match in the local client, reducing unnecessary downloads (**BREAKING CHANGE**: Update the `Data (JSON)` section in autobrr's External filter webhook configuration. Please refer to the [wiki](https://github.com/KyokoMiki/nemorosa/wiki/Announce-Matching#setting-up-autobrr) for details)
- **Album Name Matching**: Added album name matching logic before downloading torrent files. The system now searches for matching torrents by album keywords in the local client before proceeding with torrent file downloads
- **Empty Client Initialization**: Allow initialization of new clients without existing torrents.

### What's Changed

* build(deps): bump apscheduler from 3.11.0 to 3.11.1 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/35
* build(deps): bump fastapi from 0.119.1 to 0.121.0 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/36
* build(deps): bump ruff from 0.14.1 to 0.14.3 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/37
* feat(core)!: require album name match for announce matching torrent downloads by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/38
* feat(docker): support timezone configuration via TZ by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/39
* feat(filelinking): add directory creation mode configuration option by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/40
* feat(init): allow new clients without torrents by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/41

**Full Changelog**: https://github.com/KyokoMiki/nemorosa/compare/0.3.0...0.4.0

## [0.3.0](https://github.com/KyokoMiki/nemorosa/compare/0.2.1...0.3.0) - 2025-10-25

### Added

- **rTorrent Client Support**: Added support for rTorrent client. rTorrent connects via XMLRPC. Because rTorrent does not support file renaming, `enable_linking` must be enabled to use it
- **Torrent Tags Support**: Added tags support for torrent injection. For qBittorrent, tags are used together with label. For Transmission, tags are used by default and override label; if tags is null, [label] is used as fallback

### Changed

- **API Parameter Naming**: The parameter `infoHash` in `/api/webhook` endpoint has been changed to `infohash` (**BREAKING CHANGE**: Update any scripts or documentation that reference the old parameter name)
- **Unified Logging Format**: All logs now follow uvicorn style for consistency across the application
- **Retry Logic Refactoring**: Refactored retry logic. The `undownloaded_torrents` table is no longer used and can be safely ignored

### Fixed

- **File Suffix Matching**: Fixed suffix matching logic and improved search accuracy
- **rTorrent XML Security**: Moved defusedxml monkey_patch to class initialization for better security handling
- **Deluge Timeout**: Increased Deluge client timeout to 60 seconds to avoid timeout issues
- **Retry Undownloaded Torrents**: Fixed an issue where file mapping information could not be obtained when retrying matched torrents that failed to download

### What's Changed

* feat(clients): add rTorrent client support by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/21
* feat(downloader): add tags support for torrent injection by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/22
* fix(clients): improve deluge timeout and rtorrent initialization by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/23
* build(deps): bump uvloop from 0.21.0 to 0.22.1 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/25
* build(deps): bump winloop from 0.2.3 to 0.3.1 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/26
* build(deps): bump ruff from 0.14.0 to 0.14.1 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/27
* build(deps): bump fastapi from 0.119.0 to 0.119.1 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/28
* feat!: refactor retry mechanism and initialization by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/30

**Full Changelog**: https://github.com/KyokoMiki/nemorosa/compare/0.2.1...0.3.0

## [0.2.1](https://github.com/KyokoMiki/nemorosa/compare/0.2.0...0.2.1) - 2025-10-14

### Added

- **File Linking Support**: Added support for hardlinks, symlinks, and reflinks. In previous versions, torrents with files having the same name but different sizes were considered conflicting and treated as non-matches. With linking enabled, these torrents can now be properly handled and added to the downloader. When using reflink, torrents with files that don't have completely matching pieces (e.g., modified metadata) are additionally allowed as matches, enabling cross-seeding through reflink's copy-on-write functionality
- **Structured API Response Models**: Added Pydantic models for API responses (ProcessResponse, JobResponse) to provide consistent, well-documented API interfaces
- **Torrent Information Caching**: Implemented database caching for torrent information from the client to significantly improve search performance

### Changed

- **Clearer API Responses**: API responses now have a clearer structure and include proper HTTP status codes based on processing results, allowing simple determination of cross-seeding success through HTTP status codes
- **Initialization Process Restructured**: Refactored initialization process to include an additional check on startup to determine if the local torrent information cache needs automatic rebuilding

### Fixed

- **Reverse Search Performance**: Fixed excessive reverse search time that was causing announce matching requests to timeout after running for a period of time

### Performance

- **Database Migration to SQLAlchemy**: Migrated from sqlite3 to SQLAlchemy with aiosqlite for better async support
- **Announce Matching Optimization**: Cached torrent information from the client in the database to improve announce matching performance. Processing efficiency can now match the frequency of IRC announces

### What's Changed

* build(deps): bump ruff from 0.13.2 to 0.13.3 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/11
* build(deps): bump winloop from 0.2.2 to 0.2.3 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/12
* feat: enhance linking workflow and optimize reverse matching performance by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/13
* perf: optimize torrent operations with database caching and improve API structure by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/14
* build(deps): bump reflink-copy from 0.3.2 to 0.3.3 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/15
* build(deps): bump platformdirs from 4.4.0 to 4.5.0 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/16
* build(deps): bump fastapi from 0.118.0 to 0.119.0 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/17
* build(deps): bump ruff from 0.13.3 to 0.14.0 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/18

**Full Changelog**: https://github.com/KyokoMiki/nemorosa/compare/0.2.0...0.2.1

## [0.2.0](https://github.com/KyokoMiki/nemorosa/compare/0.1.0...0.2.0) - 2025-10-01

### Added

- **Torrent Verification Tracking**: Added functionality to poll torrent client for torrent information, automatically track verification status after injection and begin post-processing
- **Auto Start Torrents Configuration**: Added `auto_start_torrents` global configuration option - when set to false, injected torrents will not automatically start

### Changed

- **Post-processing No Longer Requires Manual Execution**: By default, after torrent injection, the system will automatically track the verification process and begin post-processing
- **CLI Option Renamed**: CLI option `--process-completed-matches` has been renamed to `--post-process` (**BREAKING CHANGE**: Update any scripts or documentation that reference the old option name)

### Performance

- **Docker Image Optimization**: Significantly reduced Docker image size through multi-stage builds
- **Async Gazelle API**: Converted Gazelle API to async implementation using httpx instead of synchronous requests, improving concurrent performance
- **Field Selection Optimization**: Added field selection functionality for torrent clients, supporting on-demand retrieval of specific field information for improved performance

### What's Changed

* build(deps): bump beautifulsoup4 from 4.13.5 to 4.14.2 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/6
* build(deps): bump pyyaml from 6.0.2 to 6.0.3 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/7
* build(deps): bump fastapi from 0.117.1 to 0.118.0 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/8
* build(deps): bump ruff from 0.13.1 to 0.13.2 by @dependabot[bot] in https://github.com/KyokoMiki/nemorosa/pull/9
* feat: add torrent client polling and verification tracking system by @KyokoMiki in https://github.com/KyokoMiki/nemorosa/pull/10

**Full Changelog**: https://github.com/KyokoMiki/nemorosa/compare/0.1.0...0.2.0

## [0.1.0](https://github.com/KyokoMiki/nemorosa/compare/0.0.1...0.1.0) - 2025-09-24

### Added

- **Full Search**: Search all torrents in torrent client
- **Single Torrent Search**: Process individual torrents by infohash for cross-seeding
- **Advanced Partial Matching**: Handle cases like different block sizes, missing artwork, or modified covers
- **Automatic File Mapping**: Automatically rename folders and files to match existing content
- **Torrent Injection**: Seamlessly inject matched torrents into supported clients
- **Web Server**: HTTP API and webhook support for integration with other tools and automation
- **Scheduled Jobs**: Automated search and cleanup tasks with configurable intervals
- **Announce Matching**: Automatically match cross-seeds from IRC announces or RSS feeds
- **Triggering Searches**: Enable immediate cross-seed searches when torrents finish downloading
- **Post Process**: Automated post-processing of previous injected torrents
- **Retry Undownloaded**: Retry failed downloads and undownloaded torrents

**Full Changelog**: https://github.com/KyokoMiki/nemorosa/commits/0.1.0
