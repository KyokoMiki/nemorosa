# Nemorosa

![Python Version](https://img.shields.io/badge/python-3.7%2B-blue)
[![License](https://img.shields.io/badge/license-gplv3-green)](LICENSE)

Nemorosa is a specialized tool that scans your Transmission torrent client to find matching torrents on Gazelle-based private trackers (like RED/OPS/DIC) for cross-seeding purposes. It automatically downloads matching .torrent files, renames files to match your existing content, and adds them to Transmission with minimal configuration.

## Prerequisites

- Python 3.7+
- Transmission client with RPC enabled
- Access to Gazelle-based private tracker (RED/OPS/DIC)
- Valid API key or cookie for authentication

## Installation

1. Clone the repository:
```bash
git clone https://github.com/KyokoMiki/nemorosa.git
cd nemorosa
```

2. Install required dependencies:
```bash
pip install -r requirements.txt
```

## Configuration

Before running Nemorosa, ensure your Transmission client has RPC enabled. You'll need your Transmission RPC URL which follows this format:

`transmission+http://username:password@host:port`

Example: `transmission+http://admin:password123@localhost:9091`

## Usage

```bash
python nemorosa.py --transmission "transmission+http://user:pass@localhost:9091" \
    --server "https://server" \
    --tracker "tracker" \
    [--api-key YOUR_API_KEY] [--cookie YOUR_COOKIE_STRING] [OPTIONS]
```

### Required Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `--transmission` | Transmission RPC URL | `transmission+http://user:pass@localhost:9091` |
| `--server` | Site URL | `` |
| `--tracker` | Tracker URL | `` |

### Authentication Options
You must provide **either** an API key or a cookie string for authentication:

| Option | Description |
|--------|-------------|
| `--api-key` | API key for tracker authentication |
| `--cookie` | Cookie string for tracker authentication |

### Additional Options

| Option | Description | Default |
|--------|-------------|---------|
| `--result-dir` | Output directory for scan results | `./results` |
| `--no-download` | Only save URLs, don't download torrents | `False` |
| `--loglevel` | Log verbosity (debug/info/warning/error/critical) | `info` |

### Example Workflows

**Basic usage with API key:**
```bash
python nemorosa.py \
    --transmission "transmission+http://admin:password@localhost:9091" \
    --server "" \
    --tracker "" \
    --api-key "YOUR_API_KEY_HERE"
```

**Using cookie authentication:**
```bash
python nemorosa.py \
    --transmission "transmission+http://admin:password@localhost:9091" \
    --server "" \
    --tracker "" \
    --cookie "session=YOUR_SESSION_COOKIE; user=YOUR_USER_COOKIE"
```

**Custom result directory:**
```bash
python nemorosa.py \
    --transmission "YOUR_RPC_URL" \
    --server "" \
    --tracker "" \
    --api-key "YOUR_API_KEY" \
    --result-dir ~/nemorosa-results
```

**Debug mode with verbose output:**
```bash
python nemorosa.py \
    --transmission "YOUR_RPC_URL" \
    --server "" \
    --tracker "" \
    --api-key "YOUR_API_KEY" \
    --loglevel debug
```

## Output Files

Nemorosa generates several JSON files in your result directory:

- `transmission_scan_history.json`: Records processed torrent hashes
- `transmission_result_url.json`: Stores found torrent IDs
- `transmission_result_mapping.json`: Maps local torrent names to tracker IDs
- `transmission_result_url_undownloaded.json`: Failed downloads with metadata for manual retrieval

## How It Works

1. **Connect** to Transmission via RPC
2. **Scan** for eligible torrents with specific trackers
3. **Search** tracker API for matching torrents using:
   - File size comparisons
   - Filename patterns
   - File content matching
4. **Download** matching .torrent files
5. **Rename** files to match your existing content
6. **Add** to Transmission with proper labels
7. **Verify** torrent contents
