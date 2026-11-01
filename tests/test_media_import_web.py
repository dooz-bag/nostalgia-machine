"""Website flow for reviewing and exporting mixed-video segments."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nostalgia.media_review import Analysis, MediaImportError


def _client(paths, config):
    fastapi = pytest.importorskip("fastapi.testclient")
    from nostalgia.web.app import create_app

    return fastapi.TestClient(create_app(paths, config))


def test_import_page_is_linked_and_explains_local_staging(paths, config, catalog):
    client = _client(paths, config)
    home = client.get("/")
    page = client.get("/imports")

    assert home.status_code == 200
    assert 'href="/imports"' in home.text
    assert page.status_code == 200
    assert "media/imports/" in page.text
    assert "No videos are staged yet" in page.text


def test_import_page_lists_supported_staged_video(paths, config):
    folder = paths.media_root / "imports"
    folder.mkdir(parents=True)
    (folder / "authorized tape.mp4").write_bytes(b"video")

    response = _client(paths, config).get("/imports")

    assert response.status_code == 200
    assert "authorized tape.mp4" in response.text
    assert 'action="/imports/analyze"' in response.text


def test_analysis_redirects_to_review_page_and_shows_cut_candidates(
    paths, config, monkeypatch
):
    import nostalgia.web.app as web_app

    folder = paths.media_root / "imports"
    folder.mkdir(parents=True)
    source = folder / "mixed tape.mp4"
    source.write_bytes(b"video")
    analysis = Analysis(
        token="12345678-1234-5678-1234-567812345678",
        source_name=source.name,
        duration=70,
        cut_points=(0, 12.5, 35, 70),
        source_size=5,
        source_mtime_ns=source.stat().st_mtime_ns,
    )
    monkeypatch.setattr(web_app, "analyze_source", lambda *_: analysis)
    monkeypatch.setattr(web_app, "load_analysis", lambda *_: (analysis, source))
    client = _client(paths, config)

    response = client.post(
        "/imports/analyze", data={"filename": source.name}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/imports/review/{analysis.token}"
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert 'src="/imports/source/mixed%20tape.mp4"' in page.text
    assert 'data-time="12.500"' in page.text
    assert "Mark clip start at playhead" in page.text
    assert "Export approved segments" in page.text


def test_video_stream_only_serves_files_from_import_folder(paths, config):
    folder = paths.media_root / "imports"
    folder.mkdir(parents=True)
    source = folder / "review.mp4"
    source.write_bytes(b"video bytes")

    response = _client(paths, config).get("/imports/source/review.mp4")

    assert response.status_code == 200
    assert response.content == b"video bytes"
    assert response.headers["content-type"] == "video/mp4"


def test_export_passes_approved_segments_to_importer_and_rescans_catalog(
    paths, config, catalog, monkeypatch
):
    import nostalgia.pipeline as pipeline
    import nostalgia.web.app as web_app

    source = paths.media_root / "imports" / "mixed.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")
    analysis = Analysis(
        token="12345678-1234-5678-1234-567812345678",
        source_name=source.name,
        duration=70,
        cut_points=(0, 10, 70),
        source_size=5,
        source_mtime_ns=source.stat().st_mtime_ns,
    )
    output = paths.media("commercials", "2003", "clip.mp4")
    seen = {}
    monkeypatch.setattr(web_app, "load_analysis", lambda *_: (analysis, source))
    def fake_export(*args, **kwargs):
        seen["export"] = (args, kwargs)
        return [output]

    monkeypatch.setattr(web_app, "export_segments", fake_export)
    monkeypatch.setattr(pipeline, "scan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_app, "queue_deploy", lambda *_args, **_kwargs: "1 day staged")
    client = _client(paths, config)

    response = client.post(
        "/imports/export",
        data={
            "token": analysis.token,
            "segments": json.dumps(
                [{"start": 10, "end": 40, "label": "commercial"}]
            ),
            "ad_year": "2003",
            "bump_subject": "neutral",
            "bump_era": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/imports?imported=1&source=mixed.mp4&built=1+day+staged"
    )
    args, kwargs = seen["export"]
    assert args[:3] == (paths, analysis.token, [{"start": 10, "end": 40, "label": "commercial"}])
    assert kwargs["ad_year"] == "2003"
    assert kwargs["known_subjects"] == set(config.shows)


def test_invalid_review_data_is_reported_without_breaking_the_page(
    paths, config, monkeypatch
):
    import nostalgia.web.app as web_app

    source = paths.media_root / "imports" / "mixed.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")
    analysis = Analysis(
        token="12345678-1234-5678-1234-567812345678",
        source_name=source.name,
        duration=70,
        cut_points=(0, 10, 70),
        source_size=5,
        source_mtime_ns=source.stat().st_mtime_ns,
    )
    monkeypatch.setattr(web_app, "load_analysis", lambda *_: (analysis, source))

    def reject_segments(*_args, **_kwargs):
        raise MediaImportError("Segment 1 is invalid.")

    monkeypatch.setattr(web_app, "export_segments", reject_segments)
    response = _client(paths, config).post(
        "/imports/export",
        data={
            "token": analysis.token,
            "segments": json.dumps(["not an object"]),
            "ad_year": "",
            "bump_subject": "neutral",
            "bump_era": "",
        },
    )

    assert response.status_code == 400
    assert "Segment 1 is invalid." in response.text
