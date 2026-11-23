"""Unit tests for GazelleJSONAPI.search_torrent_by_filename result parsing."""

from unittest.mock import AsyncMock

import pytest

from nemorosa.trackers.gazelle_json import GazelleJSONAPI
from nemorosa.trackers.registry import TRACKER_REGISTRY

pytestmark = pytest.mark.anyio


BROWSE_RESPONSE = {
    "status": "success",
    "response": {
        "results": [
            {
                # Non-music category: flat group, torrent fields on the group itself
                "groupId": 2665241,
                "groupName": "Frank Herbert - Heretics of Dune",
                "torrentId": 5924695,
                "category": "Audiobooks",
                "fileCount": 214,
                "size": 2925340989,
            },
            {
                # Music group: torrents nested under the group
                "groupId": 1,
                "groupName": "Some Album",
                "torrents": [{"torrentId": 42, "size": 1000, "remasterTitle": "CD"}],
            },
            # Flat group without a torrentId is still skipped
            {"groupId": 2, "groupName": "no torrent id"},
        ]
    },
}


async def test_search_by_filename_keeps_flat_groups():
    cls, spec = TRACKER_REGISTRY["https://redacted.sh"]
    assert cls is GazelleJSONAPI
    api = GazelleJSONAPI("https://redacted.sh", spec)
    api.api = AsyncMock(return_value=BROWSE_RESPONSE)
    try:
        results = await api.search_torrent_by_filename("Heretics of Dune")
    finally:
        await api.close()

    got = sorted((r.torrent_id, r.size, r.title) for r in results)
    assert got == [
        (42, 1000, "Some Album - CD"),
        (5924695, 2925340989, "Frank Herbert - Heretics of Dune"),
    ]
