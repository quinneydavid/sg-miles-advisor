"""People from configuration, the single-user door, and the two ways of wrapping the data keys."""
import base64
import os

import pytest

from cards import access, crypto, people


class _Conn:
    """Just enough of a connection for crypto._load and access: the encryption_keys table and one people row."""

    def __init__(self, person=None):
        self.keys, self.person, self.rows = [], person, []

    def execute(self, sql, args=()):
        if sql.startswith("INSERT INTO encryption_keys"):
            self.keys.append({"version": 1, "wrapped": args[0]})
        elif "FROM encryption_keys" in sql:
            self.rows = list(self.keys)
        elif "FROM people" in sql:
            self.rows = [self.person] if self.person and self.person["id"] == args[0] else []
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in ("VAULT_ADDR", "VAULT_TOKEN", "CARDS_LOCAL_KEY", "CARDS_KEY_FILE", "CARDS_PEOPLE", "CARDS_SINGLE_USER"):
        monkeypatch.delenv(var, raising=False)
    crypto._keys.clear()
    crypto._raw.clear()
    yield
    crypto._keys.clear()
    crypto._raw.clear()


def test_people_come_from_the_environment():
    env = {"CARDS_PEOPLE": "alice:Alice Tan, bob , Bad Id:x, alice:Again"}
    assert people.configured(env) == [("alice", "Alice Tan"), ("bob", "Bob")]
    assert people.configured({"CARDS_SINGLE_USER": "Me"}) == [("me", "Me")]
    assert people.configured({}) == []


def test_tokens_name_their_person():
    env = {"CARDS_FEED_TOKEN_ALICE": "feed-a", "CARDS_BOT_TOKEN_ALICE": "bot-a", "CARDS_BOT_TOKEN_BOB": "bot-b",
           "CARDS_BOT_TOKEN_": "nobody", "CARDS_FEED_TOKEN_EMPTY": ""}
    bearer = lambda t: {"Authorization": "Bearer " + t}
    assert people.token_person(bearer("feed-a"), "CARDS_FEED_TOKEN_", env) == "alice"
    assert people.token_person(bearer("bot-b"), "CARDS_BOT_TOKEN_", env) == "bob"
    assert people.token_person(bearer("bot-a"), "CARDS_FEED_TOKEN_", env) is None      # a bot token can't feed
    assert people.token_person(bearer("nobody"), "CARDS_BOT_TOKEN_", env) is None
    assert people.token_person(bearer(""), "CARDS_FEED_TOKEN_", env) is None
    assert people.token_person({}, "CARDS_BOT_TOKEN_", env) is None


def test_single_user_needs_no_header_and_beats_one(monkeypatch):
    conn = _Conn({"id": "me", "email_hmac": None})
    assert access.internal_person(conn, {}) is None                                    # no proxy header, no single user
    monkeypatch.setenv("CARDS_SINGLE_USER", "me")
    assert access.internal_person(conn, {})["id"] == "me"
    assert access.internal_person(conn, {"Remote-User": "someone-else"})["id"] == "me"


def test_local_key_round_trip_and_survives_a_restart(monkeypatch):
    monkeypatch.setenv("CARDS_LOCAL_KEY", base64.b64encode(os.urandom(32)).decode())
    conn = _Conn()
    blob = crypto.encrypt(conn, b"S$12.30 at a shop", "stmt_lines.data_enc:1")
    assert b"12.30" not in blob and conn.keys[0]["wrapped"].startswith("local:v1:")
    mark = crypto.hmac_hex(conn, "x", b"y")
    crypto._keys.clear()
    crypto._raw.clear()                                                                # a new process: unwrap again
    assert crypto.decrypt(conn, blob, "stmt_lines.data_enc:1") == b"S$12.30 at a shop"
    assert crypto.hmac_hex(conn, "x", b"y") == mark
    with pytest.raises(Exception):
        crypto.decrypt(conn, blob, "stmt_lines.data_enc:2")                            # bound to its row


def test_a_different_local_key_opens_nothing(monkeypatch):
    monkeypatch.setenv("CARDS_LOCAL_KEY", base64.b64encode(os.urandom(32)).decode())
    conn = _Conn()
    crypto.encrypt(conn, b"x", "a")
    crypto._keys.clear()
    crypto._raw.clear()
    monkeypatch.setenv("CARDS_LOCAL_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(crypto.Unavailable):
        crypto.ready(conn)


def test_key_file_is_made_once_and_never_replaced(monkeypatch, tmp_path):
    path = tmp_path / "cards.key"
    monkeypatch.setenv("CARDS_KEY_FILE", str(path))
    conn = _Conn()
    blob = crypto.encrypt(conn, b"x", "a")
    assert len(base64.b64decode(path.read_text())) == 32 and (path.stat().st_mode & 0o777) == 0o600
    crypto._keys.clear()
    crypto._raw.clear()
    assert crypto.decrypt(conn, blob, "a") == b"x"
    crypto._keys.clear()
    crypto._raw.clear()
    path.unlink()                                                                      # key lost, data still there
    with pytest.raises(crypto.Unavailable):
        crypto.ready(conn)
    assert not path.exists()


def test_no_key_source_means_no_personal_data():
    with pytest.raises(crypto.Unavailable):
        crypto.encrypt(_Conn(), b"x", "a")


def test_bad_local_key_is_refused(monkeypatch):
    monkeypatch.setenv("CARDS_LOCAL_KEY", "too-short")
    with pytest.raises(crypto.Unavailable):
        crypto.ready(_Conn())


def test_vault_wraps_when_configured_and_refuses_local_keys(monkeypatch):
    dek = os.urandom(32)
    calls = []

    def fake(path, body, token=None):
        calls.append((path, token))
        if path.startswith("transit/datakey"):
            return {"data": {"ciphertext": "vault:v1:abc"}}
        assert body == {"ciphertext": "vault:v1:abc"}
        return {"data": {"plaintext": base64.b64encode(dek).decode()}}

    monkeypatch.setenv("VAULT_ADDR", "https://vault.example")
    monkeypatch.setenv("VAULT_TOKEN", "t")
    monkeypatch.setenv("VAULT_TRANSIT_KEY", "mykey")
    monkeypatch.setattr(crypto, "_vault", fake)
    conn = _Conn()
    blob = crypto.encrypt(conn, b"x", "a")
    assert crypto.decrypt(conn, blob, "a") == b"x"
    assert calls == [("transit/datakey/wrapped/mykey", "t"), ("transit/decrypt/mykey", "t")]
    crypto._keys.clear()
    crypto._raw.clear()
    conn.keys[0]["wrapped"] = "local:v1:AAAA"
    with pytest.raises(crypto.Unavailable):
        crypto.ready(conn)
