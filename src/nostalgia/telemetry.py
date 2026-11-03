"""HTTP proxy that records successful HLS segment requests for the channel."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .paths import Paths
from .playback import PlaybackStore, ScheduleMap, parse_hls_segment_times

LOG = logging.getLogger("nostalgia.telemetry")
SEGMENT_NAME = re.compile(r"live\d+\.ts\Z")
#: The kinds the Rotation report counts. Everything else is still mapped, so a
#: segment landing on a bump is explained rather than reported as a mismatch.
COUNTED_KINDS = frozenset({"episode", "ad"})
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _segment_request(path: str) -> tuple[str, str] | None:
    parts = urlsplit(path).path.strip("/").split("/")
    if len(parts) < 3 or not parts[-2].isdigit():
        return None
    if not SEGMENT_NAME.fullmatch(parts[-1]):
        return None
    return parts[-2], parts[-1]


def _segment_timestamp(hls_dir: Path, channel: str, filename: str) -> int | None:
    """The wall-clock start of *filename*, from whichever playlist still lists it.

    ``live.m3u8`` is a short sliding window, so a client that buffers ahead can ask
    for a segment that has already fallen out of it. ``ffmpeg.m3u8`` keeps the whole
    session, so it is the fallback rather than the first choice.
    """
    for name in ("live.m3u8", "ffmpeg.m3u8"):
        playlist = hls_dir / channel / name
        timestamps = _playlist_times(playlist)
        if timestamps is not None and filename in timestamps:
            return timestamps[filename]
    return None


#: Parsed playlists, keyed by path. ``ffmpeg.m3u8`` grows for the whole session, so
#: re-parsing it on every segment request would get slower the longer the channel
#: runs; the mtime is what makes a cached copy safe to reuse.
_PLAYLIST_CACHE: dict[Path, tuple[int, dict[str, int]]] = {}
_PLAYLIST_LOCK = threading.Lock()


def _playlist_times(playlist: Path) -> dict[str, int] | None:
    try:
        mtime_ns = playlist.stat().st_mtime_ns
    except OSError:
        return None
    with _PLAYLIST_LOCK:
        cached = _PLAYLIST_CACHE.get(playlist)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]
    try:
        timestamps = parse_hls_segment_times(playlist.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        LOG.error("Could not read HLS playlist %s: %s", playlist, exc)
        return None
    with _PLAYLIST_LOCK:
        _PLAYLIST_CACHE[playlist] = (mtime_ns, timestamps)
    return timestamps


def _handler(upstream: str, hls_dir: Path, schedule: ScheduleMap, store: PlaybackStore):
    class ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._forward()

        def do_HEAD(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def do_OPTIONS(self) -> None:
            self._forward()

        def _forward(self) -> None:
            self._headers_sent = False
            segment = _segment_request(self.path) if self.command == "GET" else None
            segment_time = (
                _segment_timestamp(hls_dir, *segment) if segment is not None else None
            )
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_HEADERS
                and name.lower() not in {"content-length", "accept-encoding"}
            }
            headers["Accept-Encoding"] = "identity"
            request = Request(
                f"{upstream.rstrip('/')}{self.path}",
                data=body,
                headers=headers,
                method=self.command,
            )
            response = None
            try:
                try:
                    response = urlopen(request, timeout=300)
                except HTTPError as exc:
                    response = exc
                self.send_response(response.status)
                self._headers_sent = True
                response_headers = {
                    name.lower(): value for name, value in response.headers.items()
                }
                connection_tokens = {
                    token.strip().lower()
                    for token in response_headers.get("connection", "").split(",")
                }
                for name, value in response.headers.items():
                    lower = name.lower()
                    if lower not in HOP_HEADERS and lower not in connection_tokens:
                        self.send_header(name, value)
                if "content-length" not in response_headers:
                    self.close_connection = True
                    self.send_header("Connection", "close")
                self.end_headers()

                if (
                    response.status == 200
                    and self.command == "GET"
                    and segment is not None
                    and segment_time is not None
                ):
                    # Recorded once the streamer has answered 200, before the body is
                    # relayed: the observation is "this segment was served", and doing
                    # it here keeps it off the client's critical path.
                    self._record_segment(segment[0], segment_time)
                if self.command != "HEAD":
                    while chunk := response.read(64 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                LOG.debug("Client disconnected while receiving %s", self.path)
            except URLError as exc:
                LOG.error("Upstream request failed for %s: %s", self.path, exc)
                if not getattr(self, "_headers_sent", False):
                    self.send_error(502, "streamer upstream unavailable")
            except OSError:
                LOG.exception("Proxy request failed for %s", self.path)
                if not getattr(self, "_headers_sent", False):
                    self.send_error(502, "streamer proxy failed")
            finally:
                if response is not None:
                    response.close()

        def _record_segment(self, channel: str, timestamp_ms: int) -> None:
            try:
                item = schedule.find(channel, timestamp_ms)
            except (OSError, ValueError, TypeError) as exc:
                LOG.error("Could not resolve schedule item for %s: %s", self.path, exc)
                return
            if item is None:
                LOG.warning(
                    "No scheduled item matches HLS segment %s at %s",
                    self.path,
                    timestamp_ms,
                )
                return
            if item["kind"] not in COUNTED_KINDS:
                # A bump or ident: the segment is accounted for, it just is not
                # something the rotation report counts.
                return
            now_ms = int(time.time() * 1000)
            try:
                created = store.record_request(item, requested_ms=now_ms)
            except sqlite3.Error:
                LOG.exception("Could not store playback observation for %s", self.path)
                return
            if created:
                LOG.info(
                    "Observed channel %s item start: %s (%s)",
                    channel,
                    item["item_id"],
                    item["asset_key"],
                )

        def log_message(self, format: str, *args: object) -> None:
            LOG.debug("client %s - %s", self.client_address[0], format % args)

    return ProxyHandler


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    paths = Paths.discover()
    upstream = os.environ.get(
        "NOSTALGIA_STREAMER_URL", "http://ersatztv:8409"
    )
    hls_dir = Path(os.environ.get("NOSTALGIA_HLS_DIR", "/app/hls"))
    schedule = ScheduleMap(paths.data_dir / "rotation-map.json")
    store = PlaybackStore(paths.catalog)
    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("TELEMETRY_PORT", "8409"))),
        _handler(upstream, hls_dir, schedule, store),
    )
    server.daemon_threads = True
    LOG.info("HLS telemetry proxy listening on port %s; upstream %s", server.server_port, upstream)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
