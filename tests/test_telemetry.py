"""The HLS proxy records a start only after a segment response succeeds."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from nostalgia.catalog import Catalog
from nostalgia.playback import (
    PlaybackStore,
    ScheduleMap,
    parse_hls_segment_times,
    write_schedule_map,
)
from nostalgia.telemetry import _handler, _segment_timestamp


def test_segment_timestamp_falls_back_to_the_full_session_playlist(
    tmp_path: Path,
) -> None:
    """A client buffering ahead asks for segments the sliding window dropped."""
    channel = tmp_path / "1"
    channel.mkdir()
    (channel / "live.m3u8").write_text(
        """#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-11-03T15:00:08.000+09:00
#EXTINF:4.000,
live000102.ts
""",
        encoding="utf-8",
    )
    (channel / "ffmpeg.m3u8").write_text(
        """#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-11-03T15:00:00.000+09:00
#EXTINF:4.000,
live000100.ts
#EXTINF:4.000,
live000101.ts
#EXTINF:4.000,
live000102.ts
""",
        encoding="utf-8",
    )

    assert _segment_timestamp(tmp_path, "1", "live000102.ts") == 1_793_685_608_000
    assert _segment_timestamp(tmp_path, "1", "live000100.ts") == 1_793_685_600_000
    assert _segment_timestamp(tmp_path, "1", "live000999.ts") is None


def test_proxy_records_segment_request_against_schedule_map(tmp_path: Path) -> None:
    segment_time = parse_hls_segment_times(
        """#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-11-03T15:00:00.000+09:00
#EXTINF:4.000,
live000100.ts
"""
    )["live000100.ts"]
    hls_dir = tmp_path / "hls"
    (hls_dir / "1").mkdir(parents=True)
    (hls_dir / "1" / "live.m3u8").write_text(
        """#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-11-03T15:00:00.000+09:00
#EXTINF:4.000,
live000100.ts
""",
        encoding="utf-8",
    )
    schedule_path = tmp_path / "rotation-map.json"
    write_schedule_map(
        schedule_path,
        [
            {
                "channel": "1",
                "item_id": "day-ep1",
                "asset_key": "show:S01E01",
                "kind": "episode",
                "start_ms": segment_time,
                "end_ms": segment_time + 30_000,
            }
        ],
    )
    catalog_path = tmp_path / "catalog.sqlite"
    with Catalog(catalog_path):
        pass
    store = PlaybackStore(catalog_path)

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = 404 if self.path.endswith("?fail=1") else 200
            body = b"not found" if status == 404 else b"segment data"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _handler(
            f"http://127.0.0.1:{upstream.server_port}",
            hls_dir,
            ScheduleMap(schedule_path),
            store,
        ),
    )
    for server in (upstream, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(
            f"http://127.0.0.1:{proxy.server_port}/session/1/live000100.ts",
            timeout=5,
        ) as response:
            assert response.status == 200
            assert response.read() == b"segment data"
        with pytest.raises(HTTPError) as failed:
            urlopen(
                f"http://127.0.0.1:{proxy.server_port}"
                "/session/1/live000100.ts?fail=1",
                timeout=5,
            )
        assert failed.value.code == 404

        report = store.airing_counts(
            "episode", since_ms=None, before_ms=int(time.time() * 1000) + 10_000
        )
        assert report["total_airings"] == 1
        assert report["items"][0]["key"] == "show:S01E01"
        assert report["items"][0]["segment_requests"] == 1
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
