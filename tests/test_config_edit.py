"""The settings page edits real files, so it must not damage them.

These YAML files are also edited by hand. A UI that rewrote them would make the
two ways of editing fight each other: comments stripped, keys reordered, a
quoted channel number quietly unquoted into an integer. The page is a *form over
the files*, and every test here is about that promise.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nostalgia.web.config_edit import (
    BOOL,
    BY_SLUG,
    SETTINGS,
    ConfigEditError,
    Field,
    read_values,
    save_values,
)


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """A throwaway copy of the real config, so a test can never edit the repo."""
    target = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", target)
    return target


# -- the promise: hand-edited files survive --------------------------------


def test_saving_preserves_comments(config_dir: Path) -> None:
    before = (config_dir / "bumps.yaml").read_text(encoding="utf-8")
    save_values(BY_SLUG["bumps"], config_dir, {"promo_share": "0.45"})
    after = (config_dir / "bumps.yaml").read_text(encoding="utf-8")

    assert "# [adult swim] did not name the current show" in after
    # The comment count is the real measure: a serializer that keeps one comment
    # and drops the rest would still pass a single substring check.
    assert after.count("#") == before.count("#")


def test_saving_touches_only_the_changed_key(config_dir: Path) -> None:
    """A one-key edit must not reformat the whole file."""
    before = (config_dir / "bumps.yaml").read_text(encoding="utf-8").splitlines()
    save_values(BY_SLUG["bumps"], config_dir, {"promo_share": "0.45"})
    after = (config_dir / "bumps.yaml").read_text(encoding="utf-8").splitlines()

    assert len(after) == len(before)
    differing = [pair for pair in zip(before, after) if pair[0] != pair[1]]
    assert len(differing) == 1
    assert "promo_share" in differing[0][1]


def test_a_quoted_string_stays_a_string(config_dir: Path) -> None:
    """`number: "1"` must not become the integer 1."""
    save_values(BY_SLUG["channel"], config_dir, {"name": "Renamed"})
    text = (config_dir / "channel.yaml").read_text(encoding="utf-8")
    assert 'number: "1"' in text


def test_saving_preserves_windows_line_endings(config_dir: Path) -> None:
    path = config_dir / "channel.yaml"
    original = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    path.write_bytes(original)

    save_values(BY_SLUG["channel"], config_dir, {"name": "Windows"})

    saved = path.read_bytes()
    assert b"\r\n" in saved
    assert b"\n" not in saved.replace(b"\r\n", b"")


def test_the_day_files_keep_their_dayparts(config_dir: Path) -> None:
    """Editing the day list must not lose the programming below it."""
    save_values(BY_SLUG["days/weekday"], config_dir, {"days": "mon tue wed"})
    text = (config_dir / "days" / "weekday.yaml").read_text(encoding="utf-8")
    assert "Overnight" in text
    assert "perfect-hair-forever" in text
    assert read_values(BY_SLUG["days/weekday"], config_dir)["days"] == ["mon", "tue", "wed"]


def test_a_missing_wrapper_is_recreated(config_dir: Path) -> None:
    """A hand-deleted `channel:` block should be repairable from the page."""
    path = config_dir / "channel.yaml"
    path.write_text("# nothing here\n", encoding="utf-8")
    save_values(BY_SLUG["channel"], config_dir, {"name": "Recovered"})
    assert read_values(BY_SLUG["channel"], config_dir)["name"] == "Recovered"


# -- reading ---------------------------------------------------------------


def test_every_registered_file_can_be_read(config_dir: Path) -> None:
    for entry in SETTINGS:
        values = read_values(entry, config_dir)
        assert values, f"{entry.slug} produced no fields"
        for spec in entry.all_fields():
            assert spec.key in values, f"{entry.slug}: {spec.key} missing"


def test_values_come_from_the_file_not_the_defaults(config_dir: Path) -> None:
    """A default is a fallback for a missing key, never an override."""
    values = read_values(BY_SLUG["channel"], config_dir)
    assert values["grid.timezone"] == "Asia/Tokyo"
    assert values["era_year"] == 2003


def test_dotted_keys_reach_nested_mappings(config_dir: Path) -> None:
    values = read_values(BY_SLUG["bumps"], config_dir)
    assert values["generated.enabled"] is True
    assert values["generated.max_share"] == 0.15


# -- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        (BOOL, "1", True),
        (BOOL, "", False),
        (BOOL, "on", True),
    ],
)
def test_booleans_round_trip(kind: str, raw: str, expected: bool) -> None:
    assert Field("x", "X", kind).coerce(raw) is expected


def test_an_unchecked_checkbox_means_false() -> None:
    """HTML omits an unticked checkbox entirely, so absence must mean "off"."""
    assert Field("x", "X", BOOL).coerce("") is False


def test_a_bad_number_is_refused() -> None:
    with pytest.raises(ConfigEditError):
        Field("x", "X").coerce  # noqa: B018 - the kind is what matters
        Field("x", "X", "int").coerce("not a number")


def test_out_of_range_is_refused() -> None:
    spec = Field("x", "X", "int", minimum=0, maximum=10)
    with pytest.raises(ConfigEditError, match="at most 10"):
        spec.coerce("11")
    with pytest.raises(ConfigEditError, match="at least 0"):
        spec.coerce("-1")


def test_an_unknown_choice_is_refused() -> None:
    spec = Field("x", "X", "choice", choices=(("a", "A"), ("b", "B")))
    assert spec.coerce("a") == "a"
    with pytest.raises(ConfigEditError):
        spec.coerce("c")


def test_a_failed_save_leaves_the_file_untouched(config_dir: Path) -> None:
    """Rejecting a submission must not write half of it."""
    before = (config_dir / "channel.yaml").read_text(encoding="utf-8")
    with pytest.raises(ConfigEditError):
        save_values(BY_SLUG["channel"], config_dir, {"name": "Changed", "horizon_days": "99"})
    assert (config_dir / "channel.yaml").read_text(encoding="utf-8") == before


def test_failed_atomic_replace_keeps_original_and_cleans_temp(config_dir, monkeypatch) -> None:
    import nostalgia.web.config_edit as config_edit

    path = config_dir / "channel.yaml"
    before = path.read_bytes()

    def fail_replace(_temporary, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_edit.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        save_values(BY_SLUG["channel"], config_dir, {"name": "Changed"})

    assert path.read_bytes() == before
    assert list(config_dir.glob(".channel.yaml.*.tmp")) == []


def test_clearing_an_optional_key_removes_it(config_dir: Path) -> None:
    """Blank means "unset", not "the empty string".

    Writing ``ollama_model: ''`` leaves a line that says nothing and uncomments a
    key the owner deliberately left out, so a blank optional text field deletes
    the key instead and lets the model default apply.
    """
    save_values(BY_SLUG["bumps"], config_dir, {"generated.ollama_model": "mistral"})
    assert read_values(BY_SLUG["bumps"], config_dir)["generated.ollama_model"] == "mistral"

    save_values(BY_SLUG["bumps"], config_dir, {"generated.ollama_model": ""})
    text = (config_dir / "bumps.yaml").read_text(encoding="utf-8")
    live = [line for line in text.splitlines() if line.strip().startswith("ollama_model:")]
    assert live == [], f"clearing left a live key behind: {live}"


def test_clearing_a_key_that_was_never_set_is_a_no_op(config_dir: Path) -> None:
    before = (config_dir / "bumps.yaml").read_text(encoding="utf-8")
    changed = save_values(BY_SLUG["bumps"], config_dir, {"generated.ollama_model": ""})
    assert changed == []
    assert (config_dir / "bumps.yaml").read_text(encoding="utf-8") == before


def test_an_option_nothing_reads_is_not_offered() -> None:
    """A control that changes nothing is worse than no control: it implies an effect.

    ``channel.test_card`` was read by the page shell and ``generated.style_pack``
    was read nowhere, so both were removed rather than left looking functional.
    """
    keys = {spec.key for entry in SETTINGS for spec in entry.all_fields()}
    assert "test_card" not in keys
    assert "generated.style_pack" not in keys


def test_advanced_groups_are_separate_from_the_main_ones() -> None:
    """The page is "what this channel is" first, tuning second."""
    main = [s for e in SETTINGS for s in e.sections if not s.advanced]
    advanced = [s for e in SETTINGS for s in e.sections if s.advanced]
    assert main and advanced
    titles = {s.title for s in main}
    assert "Identity" in titles and "Era" in titles
    assert {"Pod fitting", "Type", "Placement"} <= {s.title for s in advanced}


def test_unknown_keys_are_never_written(config_dir: Path) -> None:
    """A stray form field must not inject YAML the planner will later read."""
    save_values(BY_SLUG["channel"], config_dir, {"name": "Kept", "not_a_setting": "1"})
    text = (config_dir / "channel.yaml").read_text(encoding="utf-8")
    assert "not_a_setting" not in text
    assert "Kept" in text


# -- the registry is coherent ---------------------------------------------


def test_slugs_and_filenames_are_unique() -> None:
    """Two panels writing one file would silently overwrite each other."""
    keys = [(entry.slug, entry.filename, field.key) for entry in SETTINGS for field in entry.all_fields()]
    assert len(keys) == len(set(keys))


def test_every_field_has_a_label_and_a_sane_kind() -> None:
    for entry in SETTINGS:
        for spec in entry.all_fields():
            assert spec.label, f"{entry.slug}:{spec.key} has no label"
            assert spec.kind in ("text", "int", "float", "bool", "choice", "day", "list")


def test_every_choice_offers_at_least_two_options() -> None:
    for entry in SETTINGS:
        for spec in entry.all_fields():
            if spec.kind == "choice":
                assert len(spec.choices) >= 2, f"{entry.slug}:{spec.key}"


def test_the_editor_covers_every_core_config_file() -> None:
    """The page is meant to be the whole settings surface, not a sample of it."""
    covered = {entry.filename for entry in SETTINGS}
    for name in ("channel.yaml", "ads.yaml", "bumps.yaml", "watermark.yaml"):
        assert name in covered, f"{name} has settings but no page"
