from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nostalgia import cli
from nostalgia.catalog import Catalog
from nostalgia.pipeline import BuildError, doctor


def test_health_checks_playable_schedule_without_loading_config(
    paths, monkeypatch
):
    now_ms = 1_800_000_000_000
    zone = timezone(timedelta(hours=9))
    start = datetime.fromtimestamp((now_ms - 60_000) / 1000, tz=zone)
    finish = datetime.fromtimestamp((now_ms + 60_000) / 1000, tz=zone)
    paths.playout_dir.mkdir(parents=True, exist_ok=True)
    (paths.playout_dir / "active.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "20261030-0001-ep1",
                        "start": start.isoformat(),
                        "finish": finish.isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.time, "time", lambda: now_ms / 1000)
    (paths.config_dir / "channel.yaml").write_text("broken: [", encoding="utf-8")

    assert cli.main(["--root", str(paths.root), "health"]) == 0


def test_doctor_surfaces_failed_emit_even_when_a_previous_emit_succeeded(
    config, catalog, paths
):
    catalog.set_json_state(
        "scheduler_emit",
        {
            "last_attempt_ms": 1_800_000_000_000,
            "last_success_ms": 1_799_000_000_000,
            "last_error": "BuildError: media could not be planned",
        },
    )

    findings = [finding for finding in doctor(config, catalog, paths) if finding[1] == "scheduler"]

    assert any("last emit failed" in message for _level, _area, message in findings)
    assert any("last successful emit" in message for _level, _area, message in findings)


def test_failed_live_emit_is_persisted_for_doctor(paths, config, monkeypatch):
    def fail_build(*_args, **_kwargs):
        raise BuildError("no episodes")

    monkeypatch.setattr(cli, "build", fail_build)
    args = SimpleNamespace(
        date=None, days=None, no_cooldowns=False, no_generated=False
    )

    with pytest.raises(BuildError, match="no episodes"):
        cli._run_build(args, config, paths, emit=True, show=False, quiet=False)

    with Catalog(paths.catalog) as catalog:
        status = catalog.get_json_state("scheduler_emit")

    assert status is not None
    assert status["last_error"] == "BuildError: no episodes"
    assert status["last_success_ms"] is None
