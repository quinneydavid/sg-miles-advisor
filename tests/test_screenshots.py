"""Screenshots of the bank's app in the drop folder: two reads that must agree, the card from the screenshot or the
file name, and only new rows added (no double counting against alerts, earlier screenshots or statements)."""
import datetime
import io
import json
import os

import pytest

from cards import crypto, ingest, llm, screenshots, statements

D = datetime.date
DAY = D.today()


def png():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (40, 80), "white").save(out, "PNG")
    return out.getvalue()


def row(days_ago, text, amount, **k):
    return {"date": (DAY - datetime.timedelta(days=days_ago)).isoformat(), "description": text, "amount": amount,
            "currency": "SGD", "pending": False, **k}


class Model:
    """Stands in for the vision model: the first read gives `rows`, the second the amounts (or `amounts`)."""

    def __init__(self, monkeypatch):
        self.rows, self.last4, self.amounts = [], None, None
        monkeypatch.setattr(llm, "key", lambda: "k")
        monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
        monkeypatch.setattr(llm, "chat", self.chat)

    def chat(self, content, max_tokens=800, timeout=240):
        prompt = content[-1]["text"]
        if prompt.startswith("This is a screenshot"):
            return json.dumps({"transactions": True, "bank": "Bank", "kind": "card transactions", "card_name": "Card",
                               "last4": self.last4, "rows": self.rows})
        amounts = self.amounts if self.amounts is not None else [r["amount"] for r in self.rows]
        return json.dumps({"amounts": amounts})


@pytest.fixture
def alice(pg, monkeypatch, tmp_path):
    conn, ids = pg
    st = ingest.settings(conn, "alice")
    st["last4"] = {"1111": ids[0], "2222": ids[1]}
    ingest.save_settings(conn, "alice", st)
    conn.commit()
    monkeypatch.setattr(ingest, "INBOX", str(tmp_path))
    (tmp_path / "alice").mkdir()
    return conn, ids, Model(monkeypatch), tmp_path / "alice"


def drop(folder, name="shot.png"):
    (folder / name).write_bytes(png())


def lines(conn, card=None, section=None):
    rows = conn.execute("SELECT id, card_id, section, txn_date, data_enc FROM stmt_lines WHERE person = 'alice' ORDER BY txn_date, id").fetchall()
    out = [dict(crypto.dec_json(conn, r["data_enc"], f"stmt_lines.data_enc:{r['id']}"), card=r["card_id"], section=r["section"],
                date=r["txn_date"]) for r in rows]
    return [x for x in out if (card is None or x["card"] == card) and (section is None or x["section"] == section)]


def run(conn):
    from collections import Counter
    stats = Counter()
    ingest.run_files(conn, "alice", stats)
    return stats


def receipt(folder, sub, name="shot.png"):
    return (folder / sub / (name + ".txt")).read_text()


def test_claim_pairs_closest_first_and_each_line_once():
    a, b = D(2026, 11, 10), D(2026, 11, 12)
    # two equal coffees on one day against one held coffee: one is new
    assert screenshots.claim([(a, 450), (a, 450)], [(a, 450)]) == [0, None]
    # the closer held line is taken, even when listed second
    assert screenshots.claim([(b, 1000)], [(a, 1000), (b, 1000)]) == [1]
    # too far apart, or the other side (a refund), is not the same purchase
    assert screenshots.claim([(a, 1000)], [(a + datetime.timedelta(days=4), 1000)]) == [None]
    assert screenshots.claim([(a, -1000)], [(a, 1000)]) == [None]


def test_a_screenshot_adds_its_rows_and_an_overlapping_one_only_the_new(alice):
    conn, ids, model, folder = alice
    model.last4 = "1111"
    model.rows = [row(1, "KOPI CORNER", 4.5), row(1, "KOPI CORNER", 4.5), row(2, "SUPERMARKET", 62.3)]
    drop(folder)
    stats = run(conn)
    assert stats["screenshot read"] == 1 and stats["screenshot rows added"] == 3
    assert (folder / "done" / "shot.png").exists() and "3 added" in receipt(folder, "done")
    got = lines(conn, ids[0], "screenshot")
    assert sorted(x["amount"] for x in got) == [4.5, 4.5, 62.3] and got[0]["currency"] == "SGD"
    # the next screenshot shows the newest purchase above the three already held
    model.rows = [row(0, "TAXI", 18.2)] + model.rows
    drop(folder, "later.png")
    stats = run(conn)
    assert stats["screenshot rows added"] == 1 and stats["screenshot rows already there"] == 3
    assert len(lines(conn, ids[0])) == 4
    assert "already there" in receipt(folder, "done", "later.png")


def test_rows_an_alert_already_brought_are_not_added_again(alice):
    conn, ids, model, folder = alice
    ingest.store_alerts(conn, "alice", [{"date": DAY - datetime.timedelta(days=3), "card": ids[0], "merchant": "KOPI CORNER",
                                         "amount": 4.5, "currency": "SGD", "fcy": False, "online": None, "est": False, "mid": "m1"}])
    model.last4, model.rows = "1111", [row(1, "Kopi Corner Pte", 4.5), row(1, "BOOKSHOP", 30.0)]
    drop(folder)
    stats = run(conn)
    assert stats["screenshot rows added"] == 1 and stats["screenshot rows already there"] == 1
    assert sorted(x["amount"] for x in lines(conn, ids[0])) == [4.5, 30.0]


def test_an_alert_that_arrives_after_the_screenshot_is_left_out(alice):
    conn, ids, model, folder = alice
    model.last4, model.rows = "1111", [row(1, "KOPI CORNER", 4.5)]
    drop(folder)
    run(conn)
    alert = lambda amount, mid: {"date": DAY - datetime.timedelta(days=2), "card": ids[0], "merchant": "KOPI CORNER",
                                 "amount": amount, "currency": "SGD", "fcy": False, "online": None, "est": False, "mid": mid}
    keep, dropped = screenshots.drop_seen_alerts(conn, "alice", [alert(4.5, "a"), alert(4.5, "b"), alert(9.0, "c")])
    assert dropped == 1 and [a["mid"] for a in keep] == ["b", "c"]   # the second 4.50 is another purchase
    keep, dropped = screenshots.drop_seen_alerts(conn, "alice", [dict(alert(4.5, "d"), card=ids[1])])
    assert dropped == 0                                                # another card


def test_two_reads_that_disagree_add_nothing(alice):
    conn, ids, model, folder = alice
    model.last4, model.rows, model.amounts = "1111", [row(1, "KOPI CORNER", 4.5), row(2, "SUPERMARKET", 62.3)], [4.5, 63.2]
    drop(folder)
    stats = run(conn)
    assert stats["screenshot failed"] == 1 and not lines(conn)
    assert (folder / "failed" / "shot.png").exists() and "didn't agree" in receipt(folder, "failed")


def test_the_file_name_names_the_card_when_the_screenshot_does_not(alice):
    conn, ids, model, folder = alice
    model.last4, model.rows = None, [row(1, "KOPI CORNER", 4.5)]
    drop(folder, "UOB 2222.png")
    run(conn)
    assert [x["card"] for x in lines(conn)] == [ids[1]]
    drop(folder, "no number.png")
    stats = run(conn)
    assert stats["screenshot failed"] == 1 and "Rename the file" in receipt(folder, "failed", "no number.png")
    model.last4 = "9999"
    drop(folder, "unknown.png")
    run(conn)
    assert "ending 9999" in receipt(folder, "failed", "unknown.png")


def test_pending_foreign_future_and_old_rows_are_skipped(alice):
    conn, ids, model, folder = alice
    model.last4 = "1111"
    model.rows = [row(0, "HOLD", 20.0, pending=True), row(1, "SHOP ABROAD", 50.0, currency="USD"), row(-2, "LATER", 7.0),
                  row(200, "LONG AGO", 8.0), row(1, "KOPI CORNER", 4.5), row(3, "REFUND SHOP", -12.0)]
    drop(folder)
    stats = run(conn)
    assert stats["screenshot rows added"] == 2 and stats["screenshot rows skipped"] == 4
    assert sorted(x["amount"] for x in lines(conn)) == [-12.0, 4.5]
    text = receipt(folder, "done")
    assert "pending" in text and "not S$" in text and "after the screenshot" in text and "days old" in text


def test_heic_and_unreadable_files_go_to_failed(alice):
    conn, ids, model, folder = alice
    (folder / "IMG_1.HEIC").write_bytes(b"\x00\x00\x00 ftypheic")
    (folder / "broken.png").write_bytes(b"not a picture")
    stats = run(conn)
    assert stats["screenshot failed"] == 2
    assert "PNG or JPEG" in receipt(folder, "failed", "IMG_1.HEIC") and "PNG or JPEG" in receipt(folder, "failed", "broken.png")


def test_a_statement_hides_the_screenshot_rows_of_its_card_and_month(alice, monkeypatch):
    conn, ids, model, folder = alice
    model.last4, model.rows = "1111", [row(0, "KOPI CORNER", 4.5)]
    drop(folder)
    run(conn)
    assert len(statements.load_lines(conn, "alice")) == 1
    monkeypatch.setattr(statements, "pdf_text", lambda data, pw: "x" * 300)
    monkeypatch.setattr(llm, "read_statement", lambda text: {"bank": "Bank", "period_to": DAY.isoformat(), "sections": [
        {"name": "Card", "last4": "1111", "lines": [{"date": DAY.isoformat(), "description": "KOPI CORNER", "amount": 4.5}]}]})
    statements.start(conn, "alice", b"%PDF-1", "", None, wait=True)
    shown = statements.load_lines(conn, "alice")
    assert len(shown) == 1 and shown[0]["statement_id"] != "alerts-alice"


def test_claim_takes_a_fee_on_top_at_the_same_shop_only():
    a = D(2026, 11, 10)
    shop, other = screenshots.words("HOTEL SAKURA TOKYO PTE LTD"), screenshots.words("RAMEN HOUSE TOKYO")
    assert screenshots.words("Sakura Singapore Pte Ltd") == {"SAKURA"} and "TOKYO" not in shop
    # the alert said S$200.00, the app shows S$202.00 two days later: the same purchase
    assert screenshots.claim([(a + datetime.timedelta(days=2), 20200, shop)], [(a, 20000, shop)]) == [0]
    # small amounts may differ by up to S$2, large ones by 4%
    assert screenshots.claim([(a, 650, shop)], [(a, 450, shop)]) == [0]
    assert screenshots.claim([(a, 105000, shop)], [(a, 100000, shop)]) == [None]
    # another shop, the other side, or too far apart: not the same
    assert screenshots.claim([(a, 20200, other)], [(a, 20000, shop)]) == [None]   # both in Tokyo, still two shops
    assert screenshots.claim([(a, -20200, shop)], [(a, -20000, shop)]) == [0]
    assert screenshots.claim([(a, -20200, shop)], [(a, 20000, shop)]) == [None]
    assert screenshots.claim([(a + datetime.timedelta(days=6), 20200, shop)], [(a, 20000, shop)]) == [None]
    # an exact amount wins over a near one
    assert screenshots.claim([(a, 20200, shop)], [(a, 20000, shop), (a, 20200, other)]) == [1]


def test_an_alert_before_the_fee_takes_the_posted_amount(alice):
    conn, ids, model, folder = alice
    ingest.store_alerts(conn, "alice", [{"date": DAY - datetime.timedelta(days=3), "card": ids[0], "merchant": "HOTEL SAKURA TOKYO",
                                         "amount": 200.0, "currency": "SGD", "fcy": False, "online": None, "est": False, "mid": "m1"}])
    model.last4, model.rows = "1111", [row(1, "HOTEL SAKURA TOKYO JP", 202.0)]
    drop(folder)
    stats = run(conn)
    assert stats["screenshot rows added"] == 0 and stats["screenshot amounts corrected"] == 1
    assert [x["amount"] for x in lines(conn, ids[0])] == [202.0]
    assert "the alert said 200.00" in receipt(folder, "done")


def test_an_alert_before_the_fee_after_the_screenshot_is_left_out(alice):
    conn, ids, model, folder = alice
    model.last4, model.rows = "1111", [row(1, "HOTEL SAKURA TOKYO JP", 202.0)]
    drop(folder)
    run(conn)
    alert = {"date": DAY - datetime.timedelta(days=3), "card": ids[0], "merchant": "HOTEL SAKURA", "amount": 200.0,
             "currency": "SGD", "fcy": False, "online": None, "est": False, "mid": "a"}
    assert screenshots.drop_seen_alerts(conn, "alice", [alert])[1] == 1
    assert screenshots.drop_seen_alerts(conn, "alice", [dict(alert, merchant="RAMEN HOUSE")])[1] == 0


def test_rows_above_the_first_date_heading_are_skipped(alice):
    conn, ids, model, folder = alice
    model.last4, model.rows = "1111", [{"date": None, "description": "CUT OFF ROW", "amount": 9.0, "currency": "SGD",
                                        "pending": False}, row(1, "KOPI CORNER", 4.5)]
    drop(folder)
    stats = run(conn)
    assert stats["screenshot rows added"] == 1 and "no date" in receipt(folder, "done")


def test_the_prompt_says_a_heading_dates_the_rows_below_it():
    assert "rows below it" in screenshots.READ and '"date": null' in screenshots.READ
