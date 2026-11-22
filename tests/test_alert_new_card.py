"""A payment alert that arrives before its card is added: set aside, then read again once the card's last 4 digits are
saved, so the payment still counts."""
import collections
import datetime
import json
from email.message import EmailMessage

from cards import config, ingest, llm


class Box:
    """Stands in for the IMAP mailbox: one Citi alert mail."""

    def __init__(self, *a, **k):
        m = EmailMessage()
        m["From"], m["Subject"], m["Message-ID"] = "alerts@citi.example", "Card transaction alert", "<one@citi>"
        m.set_content("Your card ending 4321 was charged SGD 42.50 at SOME SHOP")
        self.raw = m.as_bytes()

    def login(self, *a): pass
    def logout(self): pass
    def select(self, *a, **k): return "OK", [b"1"]
    def search(self, *a): return "OK", [b"1"]

    def fetch(self, num, what):
        if "HEADER.FIELDS" in what:
            return "OK", [(b"1", b"Message-ID: <one@citi>\r\nFrom: alerts@citi.example\r\n\r\n")]
        return "OK", [(b"1", self.raw)]


def setup(monkeypatch, conn):
    real = config.load
    monkeypatch.setattr(ingest.config, "load", lambda c, p: dict(real(c, p), imap_host="imap.example", imap_user="u",
                                                                imap_password="pw", imap_folder="Banks"))
    monkeypatch.setattr(ingest.imaplib, "IMAP4_SSL", Box)
    monkeypatch.setattr(llm, "key", lambda: "k")
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    day = datetime.date.today().isoformat()
    monkeypatch.setattr(llm, "chat", lambda content, **k: json.dumps(
        {"alert": True, "date": day, "merchant": "SOME SHOP", "amount": 42.5, "currency": "SGD", "last4": "4321"}))


def lines(conn):
    return conn.execute("SELECT card_id FROM stmt_lines WHERE statement_id = 'alerts-alice'").fetchall()


def test_an_alert_before_the_card_was_added_counts_once_its_digits_are_saved(pg, monkeypatch):
    conn, ids = pg
    setup(monkeypatch, conn)
    stats = collections.Counter()
    ingest.run_mail(conn, "alice", stats)
    assert stats["alert without a known card"] == 1 and not lines(conn)

    ingest.run_mail(conn, "alice", stats)   # not read again while the card is still unknown
    assert stats["alert without a known card"] == 1

    st = ingest.settings(conn, "alice")
    st["last4"] = {"4321": ids[0]}
    ingest.save_settings(conn, "alice", st)
    assert ingest.retry_alerts_without_card(conn, "alice") == 1
    conn.commit()
    ingest.run_mail(conn, "alice", stats)
    assert [r["card_id"] for r in lines(conn)] == [ids[0]]

    ingest.run_mail(conn, "alice", stats)   # and only once
    assert len(lines(conn)) == 1
