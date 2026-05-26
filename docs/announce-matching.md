# Announce Matching

Announce matching allows Nemorosa to automatically process torrent announces from IRC announces or RSS feeds. This functionality is achieved through integration with autobrr.

## Overview

When a new torrent is announced on a tracker, Nemorosa can:

1. **Receive the announce** via webhook from autobrr
2. **Search by album name** in the local client database to quickly determine if there's a potential match
3. If a torrent matching the album name is found, **fetch torrent info from API** to extract the file list without downloading the torrent file
4. **Find matching torrents** in your existing torrents by comparing file lists
5. **Validate coexistence** - skip processing if the incoming torrent cannot coexist with your local torrent (same tracker)
6. If a valid match is found, **download the torrent** and **inject it automatically**

The process is optimized to only download torrent files after finding a match, reducing unnecessary downloads and improving efficiency. Additionally, the coexistence validation prevents accidentally downloading torrents that cannot coexist with your existing uploads.

## Setting Up Autobrr

Configure autobrr to send announces to Nemorosa:

1. Create a filter and name it (e.g., `nemorosa`).

2. Select the indexers you want to use. Since this process will download and inject torrents when matches are found, **ONLY select sites where you want to cross-seed**. Do not select all sites.

3. Set a really high `priority` to ensure it's always higher than your other filters.

4. Go to the `External` tab and add a new `External` filter, replacing `<BASE_URL>` and `<API_KEY>` with the correct values:

    - **Type:** `Webhook`
    - **Host:** `http://<BASE_URL>/api/announce`
    - **Headers:** `Authorization=Bearer <API_KEY>`
    - **HTTP Method:** `POST`
    - **Expected http status:** `200`
    - **Data (JSON):**
        ```json
        {
        "name": {{ toRawJson .TorrentName }},
        "link": "{{ .TorrentUrl }}",
        "album": {{ toRawJson .Title }}
        }
        ```

5. Go to the `Actions` tab and create a Test action. This is required for the webhook to work.

6. Finally, make sure the filter is enabled and you're all set.