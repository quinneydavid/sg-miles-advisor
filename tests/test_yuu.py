"""DBS yuu: 2.78 mpd at yuu shops always; 10 mpd in a month with S$800 on the card AND 4 different yuu shops; the
two tiers share one bonus limit (DBS yuu Card T&C, updated 30 Oct 2026). Rules come from the real cards.json."""
import datetime
import json
import pathlib

from cards import engine, replay

CJ = json.loads((pathlib.Path(__file__).parent.parent / "cards" / "data" / "cards.json").read_text())
YUU = CJ["cards"]["dbs_yuu_visa_card"]
CARDS = {
    "yuu": {"id": "yuu", "kind": "miles", "base_sgd": YUU["base_mpd"]["sgd"], "base_fcy": YUU["base_mpd"]["fcy"],
            "annual_fee": 0, "bank_id": "dbs", "depth": "full"},
    "vant": {"id": "vant", "kind": "miles", "base_sgd": 1.4, "base_fcy": 2.2, "annual_fee": 0, "bank_id": "dbs", "depth": "full"},
}
RULES = {"yuu": [{"bucket": b["id"], "label": b["label"], "rate": b["mpd"], "match": b["match"], "cap": b.get("cap"),
                  "cap_period": b.get("period") or "none", "min_spend": b.get("min_spend"), "promo_until": None,
                  "depth": "full"} for b in YUU["buckets"]]}
D = lambda d: datetime.date(2026, 10, d)


def base():
    return replay.Wallet(CARDS, RULES, {}, {}, {})


def line(d, amt, merchant, mcc="5411"):
    return {"date": D(d), "amount": amt, "card_id": "yuu", "online": False, "mcc": mcc, "merchant": merchant, "fcy": False}


def test_statement_names_match():
    m = YUU["buckets"][1]["match"]
    for name in ["COLD STORAGE - JELITA", "GIANT-TAMPINES", "FOODPANDA SINGAPORE", "GOJEK SINGAPORE", "BUS/MRT 123456789",
                 "7-ELEVEN 1234", "CS FRESH GREAT WORLD", "GUARDIAN HEALTH & BEAUTY", "Cold Storage", "foodpanda", "CHAGEE APP"]:
        assert engine.applies(m, {"merchant": name, "pay": "mobile"})[0], name
    for name in ["NTUC FAIRPRICE", "COLD STORAGE ON THE GO ESSO", "GUARDIAN CHANGI AIRPORT T3", "SINGTEL DASH"]:
        assert not engine.applies(m, {"merchant": name, "pay": "mobile"})[0], name


def test_no_minimum_pays_2_78():
    _, tot, _ = replay.run(base(), [line(3, 100, "COLD STORAGE - JELITA")], ["yuu"], ["yuu"])
    assert round(tot["actual"], 2) == round(100 * 2.78, 2)


def test_800_and_four_shops_pays_10():
    lines = [line(1, 300, "COLD STORAGE - JELITA"), line(2, 100, "GIANT-TAMPINES"), line(3, 50, "GUARDIAN HEALTH"),
             line(4, 50, "BUS/MRT 123"), line(5, 350, "AMAZON SG", mcc="5999")]
    _, tot, _ = replay.run(base(), lines, ["yuu"], ["yuu"])
    assert round(tot["actual"], 2) == round(500 * 10 + 350 * 0.14, 2)


def test_800_but_three_shops_stays_2_78():
    lines = [line(1, 300, "COLD STORAGE - JELITA"), line(2, 100, "GIANT-TAMPINES"), line(3, 50, "GUARDIAN HEALTH"),
             line(5, 400, "AMAZON SG", mcc="5999")]
    _, tot, _ = replay.run(base(), lines, ["yuu"], ["yuu"])
    assert round(tot["actual"], 2) == round(450 * 2.78 + 400 * 0.14, 2)


def test_one_bonus_limit_for_both_tiers():
    # target met; S$1,000 at yuu shops: 822.86 at 10 mpd, the rest at the base rate (not 2.78 on top)
    lines = [line(1, 700, "COLD STORAGE"), line(2, 100, "GIANT"), line(3, 100, "GUARDIAN"), line(4, 100, "7-ELEVEN")]
    _, tot, _ = replay.run(base(), lines, ["yuu"], ["yuu"])
    assert round(tot["actual"], 1) == round(822.86 * 10 + (1000 - 822.86) * 0.14, 1)


def test_progress_counts_shops():
    lines = [line(1, 300, "COLD STORAGE - JELITA"), line(2, 600, "GIANT-TAMPINES")]
    p = replay.progress(base(), lines, ["yuu"], D(20))
    full = next(x for x in p if x["bucket"] == "yuu_full")
    assert full["min_left"] == 0 and full["shops_left"] == 2 and full["shops_have"] == ["Cold Storage", "Giant"]


def test_which_card_wording_inputs():
    room = {"yuu_full": 822.86, "yuu": 3200, "__min__": {"yuu_full": 0}, "__shops__": {"yuu_full": 2}}
    rate, rule, _, extra = engine.rate_card(CARDS["yuu"], RULES["yuu"], set(), {"merchant": "Cold Storage", "pay": "mobile"},
                                            room=room)
    assert rule["bucket"] == "yuu_full" and extra["locked"] and extra["shops_left"] == 2
    room["__shops__"]["yuu_full"] = 0
    room["yuu_full"] = 0   # target met and the 10 mpd limit used: no 2.78 either, base only
    rate, rule, _, _ = engine.rate_card(CARDS["yuu"], RULES["yuu"], set(), {"merchant": "Cold Storage", "pay": "mobile"}, room=room)
    assert rule is None and rate == 0.14


def test_seeded_and_looked_up(pg):
    conn, _ = pg
    rows = conn.execute("SELECT bucket, rate, min_spend, cap FROM card_rules WHERE card_id = 'dbs_yuu_visa_card' AND valid_to IS NULL "
                        "ORDER BY bucket").fetchall()
    assert [(r["bucket"], float(r["rate"])) for r in rows] == [("yuu", 2.78), ("yuu_full", 10.0)]
    card = conn.execute("SELECT depth, base_sgd, programme_id FROM cards WHERE id = 'dbs_yuu_visa_card'").fetchone()
    assert card["depth"] == "full" and float(card["base_sgd"]) == 0.14 and card["programme_id"] == "YUU"
    p = {"mcc": "5411", "pay": "mobile", "fcy": False, "merchant": "COLD STORAGE - JELITA", "today": datetime.date.today()}
    yuu = next(x for x in engine.rank(conn, p, held={"dbs_yuu_visa_card"}) if x["card"]["id"] == "dbs_yuu_visa_card")
    assert yuu["rate"] == 10.0 and yuu["extra"]["locked"] and yuu["extra"]["min_shops"] == 4


def test_singapore_only():
    m = YUU["buckets"][1]["match"]
    assert not engine.applies(m, {"merchant": "GIANT JOHOR BAHRU", "pay": "mobile", "fcy": True})[0]
