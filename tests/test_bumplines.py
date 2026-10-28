"""What a generated card says, and the gates a line passes before it can air.

The failure these tests exist for is a model's first draft of a ``NEXT`` card:
"you are now watching: <show>". It is fluent, on-voice and false -- the card plays
*between* programmes, so nothing it can say about "now" is true. The renderer had
the same class of bug once ("WE NOW RETURN TO" after an episode had ended) and the
fix was a table of what each position may claim. :func:`check_line` is that table
applied to words, and it has to hold against exactly the sentences a model will
write, not only against ones written to pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nostalgia import bumplines as bl
from nostalgia.config import load_config

REPO = Path(__file__).resolve().parents[1]


# -- the truth guard ---------------------------------------------------------------


@pytest.mark.parametrize(
    "position, text",
    [
        # the model's real first draft
        ("NEXT", "you are now watching: {show}. try not to think about it."),
        ("NEXT", "you just watched something. {show} is next."),
        ("INTO", "that was {show}. it is over now."),
        ("INTO", "we now return to {show}."),
        ("AFTER", "next: {show}. we checked."),
        ("AFTER", "stay tuned. {show} has finished."),
        ("AFTER", "{show} is over. we will be back soon."),
        ("AFTER", "{show} has ended. coming up, more of it."),
        ("OUT", "that was {show}. thank you."),
        ("OUT", "{show} is next after this."),
        ("ANY", "next: more of this."),
        ("ANY", "you just watched a card."),
        ("ANY", "we now return to the program."),
    ],
)
def test_a_line_that_is_false_at_its_position_is_refused(position, text):
    assert bl.check_line(position, text), f"{position}: {text!r} should be refused"


@pytest.mark.parametrize(
    "position, text",
    [
        ("INTO", "stay tuned for {show}. it will be on shortly."),
        ("NEXT", "next: {show}. we checked. it is real."),
        ("AFTER", "you just watched {show}. no one is going to ask how you feel."),
        ("AFTER", "that was {show}. it is over now."),
        ("OUT", "we now return to {show}. nothing was missed."),
        ("OUT", "back to {show}. it did not wait for you."),
        ("ANY", "nothing is wrong. this is just what it looks like."),
    ],
)
def test_a_line_that_is_true_at_its_position_passes(position, text):
    assert bl.check_line(position, text) == []


def test_whole_words_only_so_innocent_text_is_not_refused():
    """`next` must not fire inside `nextdoor`, nor `return` inside `returnable`."""
    assert bl.check_line("AFTER", "{show} has ended. the nextdoor neighbours are fine.") == []
    assert bl.check_line("ANY", "a returnable bottle sits here.") == []


def test_the_position_is_case_insensitive():
    assert bl.check_line("next", "you are now watching {show}.")


def test_an_unknown_position_is_an_error_not_a_pass():
    assert bl.check_line("SIDEWAYS", "hello.")


@pytest.mark.parametrize("text", ["next: {show}!", "next: {show} #fun", "next: {show} 100% real", "back\\slash"])
def test_characters_that_break_the_voice_or_drawtext_are_refused(text):
    assert bl.check_line("NEXT", text)


def test_a_line_that_runs_on_is_refused():
    long = " ".join(["word"] * (bl.MAX_WORDS + 2))
    assert any("words" in p for p in bl.check_line("NEXT", f"{{show}} {long}"))


def test_the_show_placeholder_does_not_count_towards_length():
    just_under = " ".join(["word"] * bl.MAX_WORDS)
    assert bl.check_line("NEXT", f"{{show}} {just_under}") == []


def test_naming_another_show_literally_is_refused():
    """The only show a line may mention is the placeholder; a literal title would be
    wrong for every other show the line is rendered for."""
    problems = bl.check_line("NEXT", "next: Futurama.", other_titles=["Futurama", "Sealab 2021"])
    assert any("Futurama" in p for p in problems)
    assert bl.check_line("NEXT", "next: {show}.", other_titles=["Futurama"]) == []


def test_every_seed_line_passes_its_own_guard():
    """The shipped bank must obey the rules it enforces on everyone else's lines."""
    titles = [show.title for show in load_config(REPO / "config").shows.values()]
    bank = bl.load_bank(REPO / "config" / "bump-lines.yaml")
    assert bank.count() > 30
    failures = [
        (position, text, problems)
        for position, items in bank.lines.items()
        for text in items
        if (problems := bl.check_line(position, text, other_titles=titles))
    ]
    assert not failures, failures


def test_the_seed_bank_covers_every_position():
    bank = bl.load_bank(REPO / "config" / "bump-lines.yaml")
    for position in bl.POSITIONS:
        assert len(bank.lines.get(position, ())) >= 5, position


# -- the bank ----------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_a_missing_bank_is_empty_not_an_error(tmp_path: Path):
    assert not bl.load_bank(tmp_path / "nope.yaml")
    assert not bl.load_bank(None)


def test_lines_are_loaded_by_position_and_stripped(tmp_path: Path):
    bank = bl.load_bank(_write(tmp_path / "b.yaml", 'lines:\n  next:\n    - "  next: {show}.  "\n    - ""\n'))
    assert bank.lines["NEXT"] == ("next: {show}.",)


def test_a_show_specific_line_is_offered_only_to_its_show(tmp_path: Path):
    bank = bl.load_bank(
        _write(
            tmp_path / "b.yaml",
            "lines:\n  NEXT: ['general {show}.']\nper_show:\n  athf:\n    NEXT: ['the meat has opinions {show}.']\n",
        )
    )
    assert "the meat has opinions {show}." in bank.for_position("NEXT", "athf")
    assert "the meat has opinions {show}." not in bank.for_position("NEXT", "futurama")
    assert "general {show}." in bank.for_position("NEXT", "futurama")


def test_picking_is_deterministic_distinct_and_bounded(tmp_path: Path):
    bank = bl.load_bank(
        _write(tmp_path / "b.yaml", "lines:\n  NEXT: " + str([f"line {i} {{show}}." for i in range(10)]) + "\n")
    )
    first = bl.pick_lines(bank, "NEXT", 4, show="x", seed_parts=(2003,))
    assert first == bl.pick_lines(bank, "NEXT", 4, show="x", seed_parts=(2003,))
    assert len(set(first)) == 4
    assert bl.pick_lines(bank, "NEXT", 99) and len(bl.pick_lines(bank, "NEXT", 99)) == 10
    assert bl.pick_lines(bank, "AFTER", 3) == [], "no lines is no cards, not an error"
    assert first != bl.pick_lines(bank, "NEXT", 4, show="x", seed_parts=(2004,)), "seed matters"


def test_rendering_substitutes_the_show_and_leaves_general_lines_alone():
    assert bl.render_line("next: {show}.", "Futurama") == "next: Futurama."
    assert bl.render_line("nothing is wrong.", "Futurama") == "nothing is wrong."


def test_wrap_keeps_words_whole_and_respects_the_width():
    rows = bl.wrap("you just watched aqua teen hunger force. no one will ask", 20)
    assert all(len(row) <= 20 for row in rows)
    assert " ".join(rows) == "you just watched aqua teen hunger force. no one will ask"
    assert bl.wrap("supercalifragilistic", 5) == ["supercalifragilistic"], "a long word is not split"
    assert bl.wrap("", 10) == []


# -- writing approved lines --------------------------------------------------------


def test_appending_keeps_the_files_comments(tmp_path: Path):
    pytest.importorskip("ruamel.yaml")
    path = _write(tmp_path / "b.yaml", "# hand-written note\nlines:\n  NEXT:\n    - 'a {show}.'  # inline\n")
    assert bl.append_line(path, "NEXT", "b {show}.")
    text = path.read_text(encoding="utf-8")
    assert "# hand-written note" in text and "# inline" in text
    assert bl.load_bank(path).lines["NEXT"] == ("a {show}.", "b {show}.")


def test_appending_a_duplicate_is_refused_even_if_the_case_differs(tmp_path: Path):
    pytest.importorskip("ruamel.yaml")
    path = _write(tmp_path / "b.yaml", "lines:\n  NEXT:\n    - 'Next: {show}.'\n")
    assert not bl.append_line(path, "NEXT", "next:   {show}.")
    assert len(bl.load_bank(path).lines["NEXT"]) == 1


def test_appending_creates_the_file_and_show_specific_tables(tmp_path: Path):
    pytest.importorskip("ruamel.yaml")
    path = tmp_path / "new" / "b.yaml"
    assert bl.append_line(path, "AFTER", "that was {show}.", show="athf")
    assert bl.load_bank(path).per_show["athf"]["AFTER"] == ("that was {show}.",)


# -- the candidate queue -----------------------------------------------------------


def test_a_candidate_is_queued_once(tmp_path: Path):
    store = bl.CandidateStore(tmp_path / "c.json")
    assert store.add("NEXT", "next: {show}.")
    assert store.add("NEXT", "Next:  {show}.") is None, "the same line twice is one line"
    assert store.add("AFTER", "next: {show}."), "the same words at another position are distinct"
    assert len(store.pending()) == 2 and len(store.pending("next")) == 1


def test_removing_returns_what_was_removed_and_only_that(tmp_path: Path):
    store = bl.CandidateStore(tmp_path / "c.json")
    one = store.add("NEXT", "one {show}.")
    two = store.add("NEXT", "two {show}.")
    gone = store.remove(one.id)
    assert gone.text == "one {show}."
    assert [c.text for c in store.pending()] == ["two {show}."]
    assert store.remove("nope") is None
    assert store.clear() == 1 and not store.pending()


def test_a_corrupt_queue_file_reads_as_empty(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text("{ not json", encoding="utf-8")
    assert bl.CandidateStore(path).pending() == []


# -- talking to a model -------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        '{"lines": ["a line.", "another."]}',
        '["a line.", "another."]',
        'Sure! Here you go:\n{"lines": ["a line.", "another."]}\nHope that helps.',
        "- a line.\n- another.",
        "1. a line.\n2) another.",
    ],
)
def test_the_ways_a_model_gets_json_wrong_are_tolerated(reply):
    assert bl.parse_lines(reply) == ["a line.", "another."]


def test_a_reply_of_nothing_is_nothing():
    assert bl.parse_lines("") == [] and bl.parse_lines("   ") == []


def _bank(tmp_path: Path) -> bl.LineBank:
    return bl.load_bank(_write(tmp_path / "b.yaml", "lines:\n  NEXT: ['next: {show}. existing.']\n"))


def _propose(tmp_path, reply, *, position="NEXT", **kwargs):
    return bl.propose(
        position, bank=_bank(tmp_path), url="http://x", model="m", ask=lambda *a: reply, **kwargs
    )


def test_a_models_false_line_is_refused_with_the_reason(tmp_path: Path):
    """The headline case: the model writes 'you are now watching' on a NEXT card."""
    reply = '{"lines": ["you are now watching: Aqua Teen Hunger Force. try not to think about it.", "next: {show}. fine."]}'
    result = _propose(tmp_path, reply, show_title="Aqua Teen Hunger Force")
    assert result.accepted == ["next: {show}. fine."]
    (text, problems), = result.rejected
    assert "now watching" in text.lower(), "the rejected line is the one that lied"
    assert any("now watching" in p for p in problems), "and the reason names the phrase"
    assert any("false at that moment" in p for p in problems)


def test_a_title_written_out_is_turned_back_into_the_placeholder(tmp_path: Path):
    """Models ignore 'use {show}'. Reusable lines are worth more than a rejection."""
    result = _propose(
        tmp_path, '{"lines": ["next: Futurama. we looked into it."]}', show_title="Futurama"
    )
    assert result.accepted == ["next: {show}. we looked into it."]


def test_a_line_already_in_the_bank_is_not_proposed_again(tmp_path: Path):
    result = _propose(tmp_path, '{"lines": ["next: {show}. existing."]}')
    assert result.accepted == [] and result.rejected == []


def test_a_proposal_is_deduplicated_against_itself_and_the_queue(tmp_path: Path):
    result = _propose(
        tmp_path,
        '{"lines": ["next: {show}. fresh.", "Next: {show}. fresh.", "next: {show}. queued."]}',
        existing=["next: {show}. queued."],
    )
    assert result.accepted == ["next: {show}. fresh."]


def test_naming_a_different_show_is_caught_in_a_proposal(tmp_path: Path):
    result = _propose(
        tmp_path,
        '{"lines": ["next: Sealab 2021. wrong show."]}',
        show_title="Futurama",
        other_titles=["Futurama", "Sealab 2021"],
    )
    assert result.accepted == [] and result.rejected


def test_the_prompt_states_the_position_rule_and_the_voice(tmp_path: Path):
    prompt = bl.build_prompt("AFTER", examples=["that was {show}."], count=5, show_title="Futurama")
    assert "AFTER the show has ended" in prompt and "must NOT mention anything coming up" in prompt
    assert "deadpan" in prompt and "that was {show}." in prompt
    assert "Futurama" in prompt and bl.SHOW in prompt


def test_the_prompt_without_a_show_never_says_none():
    assert "None" not in bl.build_prompt("NEXT", examples=[], count=3, show_title=None)
    assert bl.SHOW in bl.build_prompt("NEXT", examples=[], count=3, show_title=None)


def test_the_any_prompt_forbids_naming_a_show():
    assert "Do not mention any show" in bl.build_prompt("ANY", examples=[], count=3)


def test_finding_ollama_tries_the_configured_address_then_the_usual_ones(monkeypatch):
    tried = []

    def fake(url, *, timeout=5.0):
        tried.append(url)
        if "host.docker.internal" in url:
            return ["m1"]
        raise bl.ModelError("down")

    monkeypatch.setattr(bl, "list_models", fake)
    url, models = bl.find_ollama("http://custom:1")
    assert url == "http://host.docker.internal:11434" and models == ["m1"]
    assert tried[0] == "http://custom:1", "the configured address is tried first"


def test_no_ollama_anywhere_is_one_clear_error(monkeypatch):
    def down(url, *, timeout=5.0):
        raise bl.ModelError("down")

    monkeypatch.setattr(bl, "list_models", down)
    with pytest.raises(bl.ModelError, match="Start Ollama"):
        bl.find_ollama("http://x")
