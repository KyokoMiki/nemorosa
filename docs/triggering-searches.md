# Triggering Searches

Nemorosa can immediately search for cross-seeds when torrents finish downloading by adding an on-completion script to your torrent client (or the Sonarr/Radarr import script) that calls Nemorosa's HTTP API.

## Overview

By configuring the client, you can automatically send webhooks to nemorosa when downloads complete, providing immediate cross-seeding without waiting for scheduled jobs.

## Setting Up Your Torrent Client

### Transmission

1. Create `transmission-nemorosa.sh`, replacing `<BASE_URL>` and `<API_KEY>` with the correct values:
    ```bash
    #!/bin/sh
    curl -XPOST <BASE_URL>/api/webhook?infohash=$TR_TORRENT_HASH \
    -H "Authorization: Bearer <API_KEY>"
    ```

2. Make it executable:
    ```bash
    chmod +x transmission-nemorosa.sh
    ```

3. In Settings > Transfers > Management, select the script in the "Call script when download completes" menu item.

### qBittorrent

1. Go to Tools > Options > Downloads.
2. Enable Run external program on torrent completion, replacing `<BASE_URL>` and `<API_KEY>` with the correct values:
    ```bash
    curl -XPOST <BASE_URL>/api/webhook?infohash=%I \
    -H "Authorization: Bearer <API_KEY>"
    ```

### Deluge

1. Create a file called `deluge-nemorosa.sh`, replacing `<BASE_URL>` and `<API_KEY>` with the correct values:
    ```bash
    #!/bin/bash
    infohash=$1
    name=$2
    path=$3
    curl -XPOST <BASE_URL>/api/webhook?infohash=$infohash \
    -H "Authorization: Bearer <API_KEY>"
    ```

2. Make the script executable:
    ```bash
    chmod +x deluge-nemorosa.sh
    ```

3. In Deluge:

    - Enable the Execute plugin
    - Under Add Command, select the event Torrent Complete and input: `/path/to/deluge-nemorosa.sh` - Press Add and Apply
    - Restart your Deluge client/daemon

### rTorrent

1. Create a script named `rtorrent-nemorosa.sh`, replacing `<BASE_URL>` and `<API_KEY>` with the correct values:
    ```bash
    #!/bin/sh
    curl -XPOST <BASE_URL>/api/webhook?infohash=$2 \
    -H "Authorization: Bearer <API_KEY>"
    ```

2. Make it executable:
    ```bash
    chmod +x rtorrent-nemorosa.sh
    ```

3. Add to `.rtorrent.rc`:
    ```bash
    echo 'method.insert=d.data_path,simple,"if=(d.is_multi_file),(cat,(d.directory),/),(cat,(d.directory),/,(d.name))"' >> ~/.rtorrent.rc
    echo 'method.set_key=event.download.finished,nemorosa,"execute={/full/path/to/rtorrent-nemorosa.sh,$d.name=,$d.hash=,$d.data_path=}"' >> ~/.rtorrent.rc
    ```

    **Note:** Replace `/full/path/to/rtorrent-nemorosa.sh` with the absolute path to your script.

4. Restart rTorrent
