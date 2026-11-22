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


def test_visa_signature_base_rate_shows_minimum_progress():
    cid = "uob_visa_signature"
    w, _, _ = wallet(cid)
    lines = [line(cid, 1, 300, pay="tap"), line(cid, 2, 15.45, mcc="5411", pay="mobile"),
             line(cid, 3, 100, fcy=True, pay="tap"), line(cid, 4, 50, pay="online")]
    res, _, _ = replay.run(w, lines, [cid], [cid])
    ms = res[1]["min_state"]
    assert ms["min"] == 1000 and round(ms["spent"], 2) == 315.45 and round(ms["left"], 2) == 684.55   # contactless only
    assert ms["rate"] == 4.0 and ms["end"] == datetime.date(2026, 10, 31)
    assert res[2]["min_state"]["spent"] == 100                       # overseas counts toward its own S$1,000
    assert "min_state" not in res[3]                                 # online never had a bonus to lose
    lines[0]["amount"] = 1000
    res, _, _ = replay.run(w, lines, [cid], [cid])
    assert "min_state" not in res[1]                                 # reached: the bonus is paid


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


def test_maybank_world_10x_shops():
    w, card, rules = wallet("maybank_world")
    m = {r["bucket"]: r["match"] for r in rules}
    shop = lambda name, pay="mobile", fcy=False: {"merchant": name, "pay": pay, "fcy": fcy, "mcc": "5812"}
    for name in ["IMPERIAL TREASURE FINE TEOCHEW", "LES AMIS PTE LTD", "SUSHI JIN", "BATH & BODY WORKS ION", "NSRCC CHANGI", "TMCC TAMPINES"]:
        assert engine.applies(m["tenx"], shop(name))[0], name
    assert not engine.applies(m["tenx"], shop("IMPERIAL TREASURE", pay="online"))[0]       # never online
    assert not engine.applies(m["tenx"], shop("LINOLEUM WORLD"))[0]
    assert engine.applies(m["tenx_rws"], shop("COACH RWS LUXURY GALLERIA"))[0]
    assert not engine.applies(m["tenx_rws"], shop("COACH ION ORCHARD"))[0]                  # RWS only
    assert not engine.applies(m["petrol"], {"mcc": "5541", "pay": "online", "fcy": False})[0]
    assert engine.applies(m["petrol"], {"mcc": "5542", "pay": "tap", "fcy": False})[0]
    rate, rule, _, _ = engine.rate_card(card, rules, set(), shop("IMPERIAL TREASURE SUPER PEKING DUCK"))
    assert rate == 4.0 and rule["bucket"] == "tenx"


def test_spend_page_shows_minimum_progress(pg, monkeypatch):
    from cards import cache, llm, spendweb, statements
    conn, ids = pg
    cid = "uob_visa_signature"
    assert cid in ids
    conn.execute("UPDATE people SET held_cards = %s WHERE id = 'alice'", ([cid],))
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    day = datetime.date.today().isoformat()
    statements.feed(conn, "alice", {"lines": [
        {"id": "1", "date": day, "card": cid, "merchant": "DON DON DONKI", "amount": 15.45, "channel": "mobile", "mcc": "5411"}]})
    conn.commit()
    me = conn.execute("SELECT * FROM people WHERE id = 'alice'").fetchone()
    _, body = spendweb.lines_page(conn, {}, me)
    assert "Base rate: the 4 miles per S$" in body and "So far S$15 of S$1,000, S$985 more to go" in body
    assert "earns 62 miles" in body and "S$985 more for the 4 per S$ bonus" in body


def test_ocbc_rewards_shopee_card_payment_earns_the_promo_shopee_pay_does_not():
    w, card, rules = wallet("ocbc_titanium")
    shop = lambda name: {"merchant": name, "pay": "online", "fcy": False, "mcc": "5262", "today": D(9)}
    rate, rule, _, _ = engine.rate_card(card, rules, set(), shop("SHOPEE SINGAPORE MP"))
    assert rate == 6.0 and rule["bucket"] == "six"
    assert engine.rate_card(card, rules, set(), shop("SHOPEEPAY*KOPI SHOP"))[1] is None   # base only


def test_a_card_cardup_does_not_take_is_never_suggested_there():
    from cards import cache
    no, _ = cache.merchant_exclusions()
    assert no["maybank_xl"]["refused"] == ["CARDUP"]
    w, card, rules = wallet("maybank_xl")
    p = {"merchant": "CARDUP PTE LTD", "pay": "online", "fcy": False, "mcc": "7399", "today": D(9)}
    rate, rule, note, _ = engine.rate_card(card, rules, set(), p, excluded_merchants=no["maybank_xl"])
    assert rate == 0 and rule is None and "not accepted" in note
    w.excl_merch = no
    assert w.earn("maybank_xl", line("maybank_xl", 9, 500, merchant="CARDUP PTE LTD", mcc="7399"))[0] == 0
    assert w.earn("maybank_xl", line("maybank_xl", 9, 500, merchant="UNIQLO", mcc="5651"))[0] > 0


def test_shop_names_that_earn_nothing_and_those_that_still_earn():
    from cards import cache
    no, common = cache.merchant_exclusions()
    excl = lambda cid: {m for spec in CJ[cid].get("exclude_mcc", []) for m in store.expand_mcc(spec)}
    p = lambda name, mcc, pay="online": {"merchant": name, "pay": pay, "fcy": False, "mcc": mcc, "today": D(9)}

    def rate(cid, name, mcc, pay="online"):
        w, card, rules = wallet(cid)
        return engine.rate_card(card, rules, excl(cid), p(name, mcc, pay), excluded_merchants=no.get(cid))

    # DBS Vantage: CardUp earns the 1.4 base; CardUp charges one code (7399) for every payment, rent included
    assert rate("dbs_vantage", "CARDUP PTE LTD", "7399")[0] == 1.4
    from cards import pipeline

    class NoShops:
        def find(self, text): return {"mccs": ["6513"], "source": "wiki", "channel": "online"}
    rent = [{"merchant": "CARDUP RENT PAYMENT", "amount": 3000}]
    pipeline.place(None, rent, NoShops(), model=False)
    assert rent[0]["mcc"] == "7399"
    # wallet top-ups earn nothing on cards that use the common set
    r = rate("dbs_vantage", "SHOPEEPAY TOPUP", "5262")
    assert r[0] == 0 and r[2] == "no points at this shop"
    assert rate("dbs_vantage", "EZ-LINK TOP UP", "4111")[0] == 0
    # Amaze passes the shop through and still earns; ipaymy is not on the common list any more
    assert rate("maybank_xl", "AMAZE* UNIQLO", "5651")[0] > 0.4
    assert rate("dbs_vantage", "IPAYMY", "7399")[0] == 1.4
    # DBS Woman's World: CardUp keeps the base rate, no online bonus
    r = rate("dbs_womens_world", "CARDUP PTE LTD", "7399")
    assert r[1] is None and r[0] == CJ["dbs_womens_world"]["base_mpd"]["sgd"]
    # UOB PayPal: no bank document names it, so it is not applied
    assert "uob_preferred_platinum" not in no or not any("PAYPAL" in m for m in no["uob_preferred_platinum"]["none"])
    assert not any("AMAZE" in m or "IPAYMY" in m for m in common)
