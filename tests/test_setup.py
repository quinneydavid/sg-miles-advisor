"""Settings kept in the app: storage, the admin rule, API tokens, the mailbox test and the mail filter file."""
import base64
import imaplib
import os
import xml.etree.ElementTree as ET

import pytest

from cards import config, crypto, ingest, mailhelp, setupweb


class Conn:
    """A dict-backed stand-in for the few statements the settings code runs."""

    def __init__(self, people=None, banks=()):
        self.settings, self.keys, self.rows = {}, [], []
        self.people = {p["id"]: p for p in (people or [])}
        self.banks = [{"id": i, "name": n} for i, n in banks]

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO encryption_keys"):
            self.keys.append({"version": 1, "wrapped": args[0]})
        elif "FROM encryption_keys" in s:
            self.rows = list(self.keys)
        elif s.startswith("SELECT key, value, secret FROM app_settings"):
            self.rows = [{"key": k, **v} for (sc, k), v in self.settings.items() if sc == args[0]]
        elif s.startswith("INSERT INTO app_settings (scope, key, value, secret)"):
            self.settings[(args[0], args[1])] = {"value": args[2], "secret": args[3]}
        elif s.startswith("INSERT INTO app_settings (scope, key, value) VALUES"):
            self.settings[(args[0], args[1])] = {"value": args[2], "secret": None}
        elif s.startswith("DELETE FROM app_settings WHERE scope = %s AND key"):
            self.settings.pop((args[0], args[1]), None)
        elif s.startswith("SELECT 1 FROM app_settings WHERE scope"):
            self.rows = [1] if (args[0], args[1]) in self.settings else []
        elif s.startswith("SELECT scope, value FROM app_settings WHERE key"):
            self.rows = [{"scope": sc, "value": v["value"]} for (sc, k), v in self.settings.items() if k == args[0]]
        elif s.startswith("UPDATE people SET admin = true WHERE id = %s AND NOT EXISTS"):
            self.rows = []
            if not any(p.get("admin") for p in self.people.values()):
                self.people[args[0]]["admin"] = True
                self.rows = [{"id": args[0]}]
        elif s.startswith("SELECT id, display, admin FROM people"):
            self.rows = sorted(self.people.values(), key=lambda p: p["id"])
        elif "FROM cards c JOIN banks b" in s:
            self.rows = list(self.banks)
        elif s.startswith("SELECT feed_at FROM people"):
            self.rows = [{"feed_at": None}]
        elif s.startswith("SELECT count(*) n FROM stmt_lines WHERE statement_id"):
            self.rows = [{"n": 0}]
        else:
            raise AssertionError("unexpected SQL: " + s)
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def commit(self):
        pass


def person(pid="alice", admin=False):
    return {"id": pid, "display": pid.capitalize(), "admin": admin, "held_cards": ["x"]}


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("CARDS_", "LLM_", "TELEGRAM_", "VAULT_")) or var in ("NOTIFY", "SEARX_URL"):
            monkeypatch.delenv(var)
    monkeypatch.setenv("CARDS_LOCAL_KEY", base64.b64encode(os.urandom(32)).decode())
    crypto._keys.clear()
    crypto._raw.clear()
    yield
    crypto._keys.clear()
    crypto._raw.clear()


# ---- storage ----
def test_a_secret_is_stored_encrypted_and_never_shown():
    conn = Conn([person()], banks=[("dbs", "DBS")])
    config.put(conn, "imap_password", "abcd efgh ijkl mnop", "alice")
    config.put(conn, "imap_user", "alice@example.com", "alice")
    config.put(conn, "llm_key", "sk-very-secret")
    row = conn.settings[("alice", "imap_password")]
    assert row["value"] is None and b"abcd efgh" not in row["secret"] and row["secret"].startswith(b"v1:")
    assert config.load(conn, "alice")["imap_password"] == "abcd efgh ijkl mnop"
    assert config.where(conn, "alice") == {"imap_password": "app", "wf_token": "", "aw_key": ""}
    page = setupweb.mailbox_section(conn, person())
    assert "abcd efgh" not in page and "alice@example.com" in page and ">set<" in page
    admin = setupweb.admin_page(conn, {}, person(admin=True))[1]
    assert "sk-very-secret" not in admin


def test_a_secret_cannot_be_read_with_another_scope():
    conn = Conn()
    config.put(conn, "imap_password", "one", "alice")
    conn.settings[("bob", "imap_password")] = conn.settings[("alice", "imap_password")]   # a row copied to another person
    with pytest.raises(Exception):
        config.load(conn, "bob")


def test_the_environment_is_only_a_starting_value(monkeypatch):
    conn = Conn()
    monkeypatch.setenv("LLM_URL", "http://env.example/v1/chat/completions")
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("CARDS_IMAP_ALICE_HOST", "imap.env.example")
    assert config.load(conn)["llm_url"] == "http://env.example/v1/chat/completions"
    assert config.load(conn)["llm_key"] == "env-key" and config.where(conn)["llm_key"] == "env"
    assert config.load(conn, "alice")["imap_host"] == "imap.env.example"
    config.put(conn, "llm_url", "http://app.example/v1/chat/completions")
    config.put(conn, "llm_key", "")                      # forgotten in the app: the environment does not come back
    assert config.load(conn)["llm_url"] == "http://app.example/v1/chat/completions"
    assert config.load(conn)["llm_key"] == "" and config.where(conn)["llm_key"] == ""
    assert config.load(conn)["tc_pause"] == "2.0"        # nothing stored, nothing in the environment: the default


def test_a_form_keeps_a_blank_secret_and_clears_an_unticked_switch():
    conn = Conn()
    config.save_form(conn, {"telegram_token": ["123:abc"], "telegram_chat": ["42"], "notify": ["1"]},
                     ("telegram_token", "telegram_chat", "notify"))
    config.save_form(conn, {"telegram_token": [""], "telegram_chat": ["43"]}, ("telegram_token", "telegram_chat", "notify"))
    got = config.load(conn)
    assert got["telegram_token"] == "123:abc" and got["telegram_chat"] == "43" and got["notify"] == "0"
    config.save_form(conn, {"forget_telegram_token": ["1"]}, ("telegram_token",))
    assert config.load(conn)["telegram_token"] == ""


def test_import_env_stores_names_not_values(monkeypatch):
    conn = Conn()
    conn.execute = _with_people(conn, ["alice"])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("CARDS_IMAP_ALICE_PASSWORD", "mail-secret")
    monkeypatch.setenv("CARDS_BOT_TOKEN_ALICE", "bot-secret")
    lines = config.import_env(conn)
    assert "app: telegram_token stored" in lines and "alice: imap_password stored" in lines
    assert not any("secret" in l or "123:abc" in l for l in lines)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("CARDS_BOT_TOKEN_ALICE")
    assert config.load(conn)["telegram_token"] == "123:abc"
    assert config.token_person(conn, {"Authorization": "Bearer bot-secret"}, "bot") == "alice"
    assert config.import_env(conn)[0].startswith("nothing to import")


def _with_people(conn, ids):
    plain = conn.execute

    def execute(sql, args=()):
        if sql.startswith("SELECT id FROM people"):
            conn.rows = [{"id": i} for i in ids]
            return conn
        if sql.startswith("INSERT INTO app_settings (scope, key, value) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING"):
            conn.settings.setdefault((args[0], args[1]), {"value": args[2], "secret": None})
            return conn
        return plain(sql, args)
    return execute


# ---- admins ----
def test_the_first_person_to_ask_becomes_the_admin():
    alice, bob = person("alice"), person("bob")
    conn = Conn([alice, bob])
    assert config.is_admin(conn, bob) is True            # nobody was admin: the one asking is now
    assert config.is_admin(conn, alice) is False
    assert conn.people["bob"]["admin"] is True and not conn.people["alice"].get("admin")


def test_only_an_admin_changes_app_settings():
    alice, bob = person("alice", admin=True), person("bob")
    conn = Conn([alice, bob])
    assert setupweb.post_admin(conn, "/admin/model", {"llm_url": ["http://x.example"]}, bob) == (False, "Only an admin can change these.")
    assert ("", "llm_url") not in conn.settings
    assert "Only an admin" in setupweb.admin_page(conn, {}, bob)[1]
    assert setupweb.post_admin(conn, "/admin/catalogue", {"tc_pause": ["fast"]}, alice)[0] is False
    assert setupweb.post_admin(conn, "/admin/catalogue", {"tc_pause": ["3"], "tc_model": ["1"]}, alice) is None
    assert config.load(conn)["tc_pause"] == "3" and config.load(conn)["tc_model"] == "1"


# ---- API tokens ----
def test_a_token_is_shown_once_and_kept_as_a_fingerprint():
    conn = Conn([person()])
    kind, token = setupweb.post_token(conn, {"kind": ["feed"], "action": ["new"]}, person())
    assert kind == "feed" and token not in str(conn.settings)
    bearer = lambda t: {"Authorization": "Bearer " + t}
    assert config.token_person(conn, bearer(token), "feed") == "alice"
    assert config.token_person(conn, bearer(token), "bot") is None          # a feed token is not a bot token
    assert config.token_person(conn, bearer("guess"), "feed") is None and config.token_person(conn, {}, "feed") is None
    _, newer = setupweb.post_token(conn, {"kind": ["feed"], "action": ["new"]}, person())
    assert config.token_person(conn, bearer(token), "feed") is None and config.token_person(conn, bearer(newer), "feed") == "alice"
    assert token not in setupweb.tokens_section(conn, person()) and newer in setupweb.tokens_section(conn, person(), ("feed", newer))
    setupweb.post_token(conn, {"kind": ["feed"], "action": ["drop"]}, person())
    assert config.token_person(conn, bearer(newer), "feed") is None


# ---- the mailbox test ----
class FakeIMAP:
    """imaplib.IMAP4_SSL as far as test_mailbox goes."""
    password, folders, down = "app-pass", ["INBOX", "Cards"], False
    login_error = "[AUTHENTICATIONFAILED] Invalid credentials (Failure)"

    def __init__(self, host, port, timeout=None):
        if FakeIMAP.down:
            raise OSError("no route")
        self.selected = None

    def login(self, user, password):
        if password != FakeIMAP.password:
            raise imaplib.IMAP4.error(FakeIMAP.login_error)

    def select(self, folder, readonly=False):
        assert readonly
        name = folder.strip('"')
        return ("OK", [b"3"]) if name in FakeIMAP.folders else ("NO", [b"no such mailbox"])

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" "{f}"'.encode() for f in FakeIMAP.folders]

    def search(self, charset, *criteria):
        return "OK", [b"1 2 3"]

    def logout(self):
        pass


def test_the_connection_test_says_what_failed(monkeypatch):
    monkeypatch.setattr(ingest.imaplib, "IMAP4_SSL", FakeIMAP)
    cfg = {"imap_host": "imap.example", "imap_port": "993", "imap_user": "alice@example.com", "imap_password": "app-pass",
           "imap_folder": "Cards"}
    assert ingest.test_mailbox(cfg) == (True, 'Connected. "Cards" holds 3 messages.')
    ok, said = ingest.test_mailbox({**cfg, "imap_password": "normal-password"})
    assert not ok and "sign-in was refused" in said and "normal-password" not in said
    monkeypatch.setattr(FakeIMAP, "login_error", "Application-specific password required")
    assert "wants an app password" in ingest.test_mailbox({**cfg, "imap_password": "x"})[1]
    ok, said = ingest.test_mailbox({**cfg, "imap_folder": "Bank mail"})
    assert not ok and 'no folder or label named "Bank mail"' in said and "Cards, INBOX" in said
    ok, said = ingest.test_mailbox({**cfg, "imap_password": ""})
    assert not ok and "the app password missing" in said
    monkeypatch.setattr(FakeIMAP, "down", True)
    ok, said = ingest.test_mailbox(cfg)
    assert not ok and "Could not reach imap.example on port 993" in said


def test_saving_the_mailbox_fills_in_the_provider_and_tests_it(monkeypatch):
    monkeypatch.setattr(ingest.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(FakeIMAP, "down", False)
    conn = Conn([person()], banks=[("dbs", "DBS")])
    note = setupweb.post_mailbox(conn, {"action": ["test"], "imap_provider": ["gmail"], "imap_user": ["alice@example.com"],
                                        "imap_password": ["app-pass"]}, person())
    assert note == (True, 'Connected. "Cards" holds 3 messages.')
    cfg = config.load(conn, "alice")
    assert (cfg["imap_host"], cfg["imap_port"], cfg["imap_folder"]) == ("imap.gmail.com", "993", "Cards")
    setupweb.post_mailbox(conn, {"action": ["provider"], "imap_provider": ["icloud"]}, person())
    assert config.load(conn, "alice")["imap_host"] == "imap.mail.me.com"
    setupweb.post_mailbox(conn, {"action": ["disconnect"]}, person())
    assert config.load(conn, "alice")["imap_password"] == "" and "No mailbox connected" in setupweb.mailbox_section(conn, person())


# ---- the filter file ----
ATOM, APPS = "{http://www.w3.org/2005/Atom}", "{http://schemas.google.com/apps/2006}"


def _filters(xml):
    root = ET.fromstring(xml)
    assert root.tag == ATOM + "feed"
    out = []
    for entry in root.findall(ATOM + "entry"):
        assert entry.find(ATOM + "category").get("term") == "filter"
        out.append({p.get("name"): p.get("value") for p in entry.findall(APPS + "property")})
    return out


def test_the_filter_file_holds_exactly_the_persons_banks():
    banks = [("dbs", "DBS"), ("uob", "UOB"), ("newbank", "A Bank Without Known Senders")]
    got = _filters(mailhelp.gmail_filters(banks, "Cards & Miles"))
    assert got == [{"from": "dbs.com OR dbs.com.sg OR posb.com.sg", "label": "Cards & Miles", "shouldNeverSpam": "true"},
                   {"from": "uob.com.sg OR uobgroup.com", "label": "Cards & Miles", "shouldNeverSpam": "true"}]
    assert all(f["shouldArchive"] == "true" for f in _filters(mailhelp.gmail_filters(banks, "Cards", skip_inbox=True)))
    assert mailhelp.senders(banks)[1] == ["A Bank Without Known Senders"]
    for f in got:   # every sender the filter names is one ingest accepts
        for domain in f["from"].split(" OR "):
            assert ingest.BANK_SENDERS.search(f"alerts@mail.{domain}")


def test_the_settings_page_offers_the_file_and_names_unknown_banks():
    conn = Conn([person()], banks=[("ocbc", "OCBC"), ("newbank", "New Bank")])
    page = setupweb.mailbox_section(conn, person())
    assert "/settings/mail-filters.xml" in page and "@ocbc.com" in page and "No sender address is known for: New Bank" in page
    name, xml = setupweb.filter_file(conn, person())
    assert name == "mailFilters.xml" and [f["from"] for f in _filters(xml)] == ["ocbc.com"]
    config.put(conn, "imap_provider", "fastmail", "alice")
    page = setupweb.mailbox_section(conn, person())
    assert "mail-filters.xml" not in page and "Create the rules by hand" in page
    config.put(conn, "imap_provider", "outlook", "alice")
    assert "name=imap_password" not in setupweb.mailbox_section(conn, person())      # nothing to connect: forwarding only
    assert setupweb.filter_file(Conn([person()], banks=[("newbank", "New Bank")]), person()) is None
