"""No login (CARDS_SINGLE_USER): the internal door answers only under this machine's names or CARDS_HOSTS."""
import threading
import urllib.error
import urllib.request

import pytest

from cards import access, web

ME = {"CARDS_SINGLE_USER": "me"}


def test_host_names_accepted_without_a_login():
    ok = lambda host, env=ME: access.host_ok({"Host": host}, env)
    for host in ("127.0.0.1", "127.0.0.1:8080", "localhost", "LOCALHOST:8080", "[::1]", "[::1]:8080"):
        assert ok(host), host
    for host in ("evil.example", "evil.example:8080", "127.0.0.1.evil.example", "localhost.evil.example:8080", "127.0.0.1:8080@evil.example",
                 "localhost:abc", "10.0.0.5:8080", "[::2]:8080", ""):
        assert not ok(host), host
    assert not access.host_ok({}, ME)                                       # no Host header at all
    named = {**ME, "CARDS_HOSTS": "cards.example, App:8080 ,"}
    assert ok("cards.example", named) and ok("cards.example:443", named) and ok("app:8080", named) and ok("localhost", named)
    assert not ok("other.example", named)
    assert ok("evil.example", {}) and ok("evil.example", {"CARDS_HOSTS": "cards.example"})   # a proxy signs people in: not checked


@pytest.fixture
def door(monkeypatch):
    monkeypatch.setenv("CARDS_SINGLE_USER", "me")
    monkeypatch.delenv("CARDS_HOSTS", raising=False)
    srv = web.Server(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def ask(srv, path, host=None, data=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", data=data, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as ex:
        return ex.code


def test_a_rebound_name_is_refused_and_healthz_stays_open(door, monkeypatch):
    assert ask(door, "/app.css") == 200 and ask(door, "/app.css", "localhost:8080") == 200
    assert ask(door, "/app.css", "evil.example:8080") == 421 and ask(door, "/", "evil.example") == 421
    assert ask(door, "/settings", "evil.example", data=b"x=1") == 421
    assert ask(door, "/healthz", "evil.example") == 200 and ask(door, "/healthz", "app:8080") == 200
    monkeypatch.setenv("CARDS_HOSTS", "cards.example")
    assert ask(door, "/app.css", "cards.example") == 200 and ask(door, "/app.css", "evil.example") == 421
    monkeypatch.delenv("CARDS_SINGLE_USER")                                 # behind a signing-in proxy: any name
    assert ask(door, "/app.css", "evil.example") == 200


def test_the_public_door_is_not_checked_by_name(door):
    door.public = True
    assert ask(door, "/app.css", "cards.example") == 200
