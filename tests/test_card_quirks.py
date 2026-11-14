"""Card rules the engine needs special handling for (0.10.6), on the real cards.json rules:
own-spend minimums (UOB Visa Signature), a rate below the normal one (Maybank Man Utd), buckets that share one bonus
limit (OCBC Rewards), bonuses paid only through the bank's booking link (Citi / UOB Agoda, Kaligo), and catalogue
shop-name lists that must find the name inside statement text."""
import datetime
import json
import pathlib
import re

from cards import engine, replay, store

CJ = json.loads((pathlib.Path(__file__).parent.parent / "cards" / "data" / "cards.json").read_text())["cards"]
D = lambda d: datetime.date(2026, 10, d)


def wallet(cid):
    c = CJ[cid]
    cards = {cid: {"id": cid, "kind": "miles", "base_sgd": c["base_mpd"]["sgd"], "base_fcy": c["base_mpd"]["fcy"],
                   "annual_fee": 0, "bank_id": "x", "depth": "full"}}
    rules = {cid: [{"bucket": b["id"], "label": b.get("label"), "rate": b["mpd"], "match": b["match"], "cap": b.get("cap"),
                    "cap_period": b.get("period") or "none", "min_spend": b.get("min_spend"), "promo_until": None,
                    "depth": "full", "conditions": b.get("provisional")} for b in c["buckets"]]}
    return replay.Wallet(cards, rules, {}, {}, {}), cards[cid], rules[cid]


def line(cid, d, amt, merchant="SHOP", mcc="5999", fcy=False, pay="mobile"):
    return {"date": D(d), "amount": amt, "card_id": cid, "online": pay == "online", "mcc": mcc, "merchant": merchant,
            "fcy": fcy, "pay": pay}


def test_visa_signature_minimum_counts_own_kind_of_spend():
    w, _, _ = wallet("uob_visa_signature")
    lines = [line("uob_visa_signature", 1, 950, pay="tap"), line("uob_visa_signature", 2, 100, fcy=True, pay="tap")]
    _, tot, _ = replay.run(w, lines, ["uob_visa_signature"], ["uob_visa_signature"])
    assert round(tot["actual"], 2) == round(1050 * 0.4, 2)          # neither bucket reached its own S$1,000
    lines = [line("uob_visa_signature", 1, 1000, pay="tap"), line("uob_visa_signature", 2, 100, fcy=True, pay="tap")]
    _, tot, _ = replay.run(w, lines, ["uob_visa_signature"], ["uob_visa_signature"])
    assert round(tot["actual"], 2) == round(1000 * 4 + 100 * 0.4, 2)  # local reached it, foreign didn't


def test_manu_selected_categories_earn_less():
    w, card, rules = wallet("maybank_manu")
    _, tot, _ = replay.run(w, [line("maybank_manu", 3, 100, mcc="8011")], ["maybank_manu"], ["maybank_manu"])
    assert round(tot["actual"], 2) == 40.0                              # doctor in S$: 0.4, not 1.12
    _, tot, _ = replay.run(w, [line("maybank_manu", 3, 100, mcc="8011", fcy=True)], ["maybank_manu"], ["maybank_manu"])
    assert round(tot["actual"], 2) == 112.0                             # abroad: the normal 1.12
    rate, rule, note, _ = engine.rate_card(card, rules, set(), {"mcc": "8011", "pay": "mobile", "fcy": False, "match_day": "win"})
    assert rate == 0.4 and rule is None and note.startswith("lower rate here")   # no win-day bonus either
    rate, rule, _, _ = engine.rate_card(card, rules, set(), {"mcc": "5812", "pay": "mobile", "fcy": False, "match_day": "win"})
    assert rate == 2.8 and rule["bucket"] == "win"


def test_ocbc_six_uses_up_the_four_limit():
    w, _, _ = wallet("ocbc_titanium")
    cid = "ocbc_titanium"
    # S$500 at Shopee (6 mpd) takes S$778 of the S$1,110 4 mpd room; then S$600 of clothes: S$332 at 4, the rest base
    lines = [line(cid, 1, 500, merchant="SHOPEE SINGAPORE", mcc="5999", pay="online"), line(cid, 2, 600, merchant="UNIQLO", mcc="5651")]
    _, tot, _ = replay.run(w, lines, [cid], [cid])
    four_cap = next(b["cap"] for b in CJ[cid]["buckets"] if b["id"] == "four")
    room = four_cap - 500 * round(14 / 9, 4)
    assert round(tot["actual"], 1) == round(500 * 6 + room * 4 + (600 - room) * 0.4, 1)


def test_link_only_bonus_not_counted_on_past_spend():
    w, card, rules = wallet("citibank_premiermiles")
    _, tot, _ = replay.run(w, [line("citibank_premiermiles", 3, 300, merchant="AGODA.COM", mcc="7011", pay="online")],
                           ["citibank_premiermiles"], ["citibank_premiermiles"])
    assert round(tot["actual"], 2) == 360.0                             # 1.2 normal rate, not 7.2
    rate, rule, _, _ = engine.rate_card(card, rules, set(), {"merchant": "Agoda", "pay": "online", "fcy": False,
                                                            "today": datetime.date(2026, 11, 14)})
    assert rate == 7.2 and rule["match"]["link_only"]                  # Which card? still names it, with its condition


def test_name_pattern_finds_names_in_statement_text():
    rx = store.name_pattern(["7-Eleven", "Cold Storage", "Cheers"])
    for t in ["7-ELEVEN 1234 SINGAPORE", "COLD STORAGE - JELITA", "cheers"]:
        assert re.search(rx, t, re.I), t
    for t in ["CHEERSFUL CAFE", "XCOLD STORAGEX"]:
        assert not re.search(rx, t, re.I), t


def test_old_anchored_rules_rewritten(pg):
    conn, _ = pg
    from psycopg.types.json import Jsonb
    cid = "dbs_vantage"
    conn.execute("INSERT INTO card_rules (card_id, bucket, label, rate, match, depth, valid_from) VALUES (%s, 'wx', 'x', 3, %s, 'headline', current_date)",
                 (cid, Jsonb({"merchant": ["^(7\\-Eleven|Cold\\ Storage)$"]})))
    assert store.unanchor_name_rules(conn) >= 1
    m = conn.execute("SELECT match FROM card_rules WHERE card_id = %s AND bucket = 'wx'", (cid,)).fetchone()["match"]
    assert engine.applies(m, {"merchant": "COLD STORAGE - JELITA", "pay": "mobile"})[0]
    assert store.unanchor_name_rules(conn) == 0                        # once only
