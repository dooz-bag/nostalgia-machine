"""The address handed to a TV has to be one the TV can actually reach.

ErsatzTV builds its playlist from the address it was requested by, so the URL you
type into a player becomes the URL the player then fetches streams from. The emit
summary used to say ``localhost``, which works perfectly on the machine running
the channel and fails on every television in the house.
"""

from __future__ import annotations

import pytest

from nostalgia.cli import _looks_like_lan, lan_urls


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "192.168.1.5",
        "192.168.0.254",
        "10.0.0.1",
        "172.15.0.1",  # just below the Docker/WSL block
        "172.32.0.1",  # just above it
        "8.8.8.8",  # routable, so not loopback or link-local
    ],
)
def test_addresses_a_tv_could_reach(address: str) -> None:
    assert _looks_like_lan(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # the machine itself
        "172.21.32.1",  # WSL host-only switch
        "172.28.208.1",  # Hyper-V default switch
        "169.254.1.1",  # link-local: no DHCP, nothing to route to
        "not-an-address",
        "1.2.3",
    ],
)
def test_addresses_a_tv_could_not_reach(address: str) -> None:
    assert not _looks_like_lan(address)


def test_lan_urls_never_offers_localhost() -> None:
    """The regression this file exists for.

    Empty is an acceptable answer -- inside a container the host's LAN address is
    genuinely not knowable -- but a *wrong* address is not, because it is the one
    a television will be handed.
    """
    for url in lan_urls():
        assert "localhost" not in url
        assert "127.0.0.1" not in url


def test_lan_urls_point_at_the_playlist() -> None:
    for url in lan_urls():
        assert url.startswith("http://")
        assert url.endswith("/channels.m3u")


def test_the_port_is_configurable() -> None:
    assert all(":8409/" in url for url in lan_urls())
    assert all(":9000/" in url for url in lan_urls(port=9000))


def test_an_unreachable_host_offers_nothing_rather_than_a_lie(monkeypatch) -> None:
    """In a container there is no LAN address, so nothing is offered."""
    monkeypatch.setattr("nostalgia.cli._default_route_address", lambda: None)
    monkeypatch.setattr("nostalgia.cli._local_ipv4_addresses", lambda: ["172.18.0.3"])
    assert lan_urls() == []


def test_etv_port_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv("ETV_PORT", "8555")
    assert all(":8555/" in url for url in lan_urls())
    monkeypatch.setenv("ETV_PORT", "not-a-port")
    assert all(":8409/" in url for url in lan_urls())
