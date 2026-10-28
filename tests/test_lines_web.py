"""The Lines page: a model proposes, the truth guard filters, a person approves.

Two gates stand between a model's sentence and a card on air, and both are tested
here through the real routes: nothing a model writes may reach the bank without a
click, and a sentence that lies about the moment it airs never reaches the page.
The model is faked; whether it is running is an ordinary state, not an error.
"""

from __future__ import annotations

import pytest

from nostalgia import bumplines as bl
from nostalgia.web import lines as lines_web


def _client(paths, config):
    fastapi = pytest.importorskip("fastapi.testclient")
    from nostalgia.web.app import create_app

    return fastapi.TestClient(create_app(paths, config), follow_redirects=False)


def _bank_text(paths) -> str:
    path = paths.config_dir / "bump-lines.yaml"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@pytest.fixture()
def model(monkeypatch):
    """A model that is running and answers with whatever ``reply`` holds."""
    reply = {"text": ""}
    monkeypatch.setattr(bl, "find_ollama", lambda preferred=None, timeout=3.0: ("http://model", ["fake"]))
    monkeypatch.setattr(bl, "ask_model", lambda *a, **k: reply["text"])
    return reply


def _first_show(config) -> str:
    return next(sid for sid, show in config.shows.items() if show.enable)


def test_the_page_renders_with_no_model_and_says_so(paths, config, monkeypatch):
    def down(preferred=None, timeout=3.0):
        raise bl.ModelError("nothing listening")

    monkeypatch.setattr(bl, "find_ollama", down)
    response = _client(paths, config).get("/lines")
    assert response.status_code == 200
    assert "not running" in response.text and "Ask the model" in response.text


def test_the_page_lists_an_approved_line_with_the_show_filled_in(paths, config, model):
    bl.append_line(paths.config_dir / "bump-lines.yaml", "NEXT", "up next: {show}. be there.")
    body = _client(paths, config).get("/lines").text
    assert "The bank" in body and "up next: " in body and "{show}" not in body.split("The bank")[1]


def test_generating_queues_what_passes_and_reports_what_the_guard_refused(paths, config, model):
    show = _first_show(config)
    model["text"] = "up next: {show}. nobody asked.\nyou just watched {show}. regret it.\n"
    client = _client(paths, config)

    response = client.post("/lines/generate", data={"position": "NEXT", "show": show, "count": "5"})
    assert response.status_code == 303 and "/lines?saved=" in response.headers["location"]
    assert "refused" in response.headers["location"], "the lie about the past was caught"

    body = client.get("/lines").text
    assert "Waiting for approval (1)" in body
    assert "nobody asked" in body
    assert "regret it" not in body, "a NEXT card must never claim you just watched"
    assert "refused by the truth guard" in client.get(response.headers["location"]).text
    assert _bank_text(paths).count("nobody asked") == 0, "queued is not approved"


def test_nothing_reaches_the_bank_until_it_is_approved(paths, config, model):
    show = _first_show(config)
    model["text"] = "up next: {show}. stay put.\n"
    client = _client(paths, config)
    client.post("/lines/generate", data={"position": "NEXT", "show": show, "count": "3"})

    store = bl.CandidateStore(paths.data_dir / "bump-line-candidates.json")
    (candidate,) = store.pending()
    assert "stay put" not in _bank_text(paths)

    response = client.post("/lines/approve", data={"id": candidate.id})
    assert response.status_code == 303 and "saved=approved" in response.headers["location"]
    assert "stay put" in _bank_text(paths)
    assert not store.pending()
    assert bl.load_bank(paths.config_dir / "bump-lines.yaml").for_position("NEXT", show)


def test_rejecting_discards_without_touching_the_bank(paths, config, model):
    model["text"] = "up next: {show}. stay put.\n"
    client = _client(paths, config)
    client.post("/lines/generate", data={"position": "NEXT", "show": "", "count": "3"})
    (candidate,) = bl.CandidateStore(paths.data_dir / "bump-line-candidates.json").pending()

    assert client.post("/lines/reject", data={"id": candidate.id}).status_code == 303
    assert "stay put" not in _bank_text(paths)
    assert not bl.CandidateStore(paths.data_dir / "bump-line-candidates.json").pending()


def test_approve_all_and_discard_all(paths, config, model):
    model["text"] = "up next: {show}. one.\nup next: {show}. two.\nup next: {show}. three.\n"
    client = _client(paths, config)
    client.post("/lines/generate", data={"position": "NEXT", "show": "", "count": "5"})
    assert "Waiting for approval (3)" in client.get("/lines").text

    client.post("/lines/clear")
    assert "Nothing is waiting" in client.get("/lines").text

    client.post("/lines/generate", data={"position": "NEXT", "show": "", "count": "5"})
    response = client.post("/lines/approve-all")
    assert "approved+3" in response.headers["location"]
    assert all(word in _bank_text(paths) for word in ("one.", "two.", "three."))


def test_an_already_approved_line_is_not_queued_again(paths, config, model):
    model["text"] = "up next: {show}. stay put.\n"
    client = _client(paths, config)
    client.post("/lines/generate", data={"position": "NEXT", "show": "", "count": "3"})
    client.post("/lines/approve-all")
    client.post("/lines/generate", data={"position": "NEXT", "show": "", "count": "3"})
    (_, store) = lines_web._files(paths)
    assert not store.pending(), "asking again must not re-offer a line already in the bank"
    assert _bank_text(paths).count("stay put") == 1


def test_a_missing_model_is_a_message_not_a_server_error(paths, config, monkeypatch):
    def down(preferred=None, timeout=3.0):
        raise bl.ModelError("Ollama is not reachable")

    monkeypatch.setattr(bl, "find_ollama", down)
    response = _client(paths, config).post("/lines/generate", data={"position": "NEXT", "count": "3"})
    assert response.status_code == 303
    assert "/lines?error=" in response.headers["location"] and "reachable" in response.headers["location"]


def test_a_stale_click_is_a_message_not_a_server_error(paths, config):
    client = _client(paths, config)
    for action in ("approve", "reject"):
        response = client.post(f"/lines/{action}", data={"id": "not-there"})
        assert response.status_code == 303 and "error=" in response.headers["location"]


def test_an_unknown_action_and_a_bad_position_are_refused(paths, config, model):
    client = _client(paths, config)
    assert client.post("/lines/frobnicate").status_code == 404
    response = client.post("/lines/generate", data={"position": "SIDEWAYS", "count": "3"})
    assert "error=" in response.headers["location"]
    response = client.post("/lines/generate", data={"position": "NEXT", "show": "no-such-show"})
    assert "error=" in response.headers["location"]


def test_the_lines_tab_is_in_the_navigation(paths, config):
    assert 'href="/lines"' in _client(paths, config).get("/").text
