import datetime

from cards import replay

D = lambda d: datetime.date(2026, 10, d)
CARDS = {
    "pref": {"id": "pref", "kind": "miles", "base_sgd": 0.4, "base_fcy": 0.4, "annual_fee": 0, "bank_id": "uob", "depth": "full"},
    "vant": {"id": "vant", "kind": "miles", "base_sgd": 1.4, "base_fcy": 2.2, "annual_fee": 0, "bank_id": "dbs", "depth": "full"},
    "yuu": {"id": "yuu", "kind": "cashback", "base_sgd": 0.0, "base_fcy": 0.0, "annual_fee": 0, "bank_id": "dbs", "depth": "headline"},
}
RULES = {
    "pref": [{"bucket": "online", "label": "Online", "rate": 4, "match": {"channel": "online"}, "cap": 600,
              "cap_period": "calendar", "min_spend": None, "promo_until": None}],
    "yuu": [{"bucket": "w1", "label": "yuu", "rate": 10, "match": {"mcc": ["5411"]}, "cap": None,
             "cap_period": "calendar", "min_spend": 800, "promo_until": None}],
}


def base():
    return replay.Wallet(CARDS, RULES, {}, {}, {})


def line(d, amt, card, online=True, mcc="5999"):
    return {"date": D(d), "amount": amt, "card_id": card, "online": online, "mcc": mcc, "merchant": "X", "fcy": False}


def test_cap_overflow_on_actual_card():
    res, tot, over = replay.run(base(), [line(1, 500, "pref"), line(5, 300, "pref")], ["pref", "vant"], ["pref", "vant"])
    assert tot["actual"] == 500 * 4 + 100 * 4 + 200 * 0.4
    assert over[0]["over_spend"] == 200 and over[0]["hit"] == D(5)
    # S$300 with S$100 of cap left: still best on Preferred (100x4 + 200x0.4 = 480 vs Vantage 420)
    assert res[1]["wallet_card"] == "pref"
    res, _, _ = replay.run(base(), [line(1, 500, "pref"), line(5, 600, "pref")], ["pref", "vant"], ["pref", "vant"])
    assert res[1]["wallet_card"] == "vant"   # S$600: 100x4 + 500x0.4 = 600 vs Vantage 840


def test_minimum_spend_not_met_pays_base():
    lines = [line(3, 200, "yuu", online=False, mcc="5411")]
    res, tot, _ = replay.run(base(), lines, ["yuu", "vant"], ["yuu", "vant"])
    assert tot["actual"] == 0          # yuu: S$200 < S$800 minimum -> no 10%, base 0
    assert res[0]["wallet_card"] == "vant"


def test_minimum_spend_met_pays_bonus():
    lines = [line(3, 500, "yuu", online=False, mcc="5411"), line(9, 400, "yuu", online=False, mcc="5411")]
    _, tot, _ = replay.run(base(), lines, ["yuu"], ["yuu"])
    assert round(tot["actual"]) == round(900 * 10 / 1.5)


def test_statement_periods():
    assert replay.statement_end(datetime.date(2026, 10, 10), 14) == datetime.date(2026, 10, 14)
    assert replay.statement_end(datetime.date(2026, 10, 20), 14) == datetime.date(2026, 11, 14)
    assert replay.statement_end(datetime.date(2026, 12, 20), 14) == datetime.date(2027, 1, 14)
    assert replay.statement_end(datetime.date(2026, 2, 10), 31) == datetime.date(2026, 2, 28)
    r = {"cap_period": "statement"}
    assert replay.period_key(r, datetime.date(2026, 10, 20), 14) == "s2026-11-14"
    assert replay.period_key(r, datetime.date(2026, 10, 20)) == "2026-10"   # closing day unknown: calendar month
    assert replay.period_bounds(r, datetime.date(2026, 10, 20), 14) == (datetime.date(2026, 10, 15), datetime.date(2026, 11, 14))


def test_statement_cap_resets_on_closing_day():
    rules = {"sig": [{"bucket": "local", "label": "Contactless", "rate": 4, "match": {"channel": "contactless"}, "cap": 1200,
                      "cap_period": "statement", "min_spend": None, "promo_until": None}]}
    cards = {"sig": dict(CARDS["pref"], id="sig")}
    w = replay.Wallet(cards, rules, {}, {}, {}, {"sig": 14})
    lines = [line(10, 1200, "sig", online=False), line(16, 500, "sig", online=False)]   # 16 Oct = next statement period
    _, tot, over = replay.run(w, lines, ["sig"], ["sig"])
    assert tot["actual"] == 1700 * 4 and not over


def test_progress_panel():
    p = replay.progress(base(), [line(3, 250, "pref"), line(4, 100, "yuu", online=False, mcc="5411")], ["pref", "yuu"], D(20))
    pref = next(x for x in p if x["card"] == "pref")
    assert pref["used"] == 250 and pref["left"] == 350 and pref["days_left"] == 11
    yuu = next(x for x in p if x["card"] == "yuu")
    assert yuu["min_left"] == 700


def test_progress_counts_spend_before_minimum_is_met():
    rules = {"sig": [{"bucket": "local", "label": "Contactless", "rate": 4, "match": {"channel": "contactless"}, "cap": 1200,
                      "cap_period": "statement", "min_spend": 1000, "promo_until": None}]}
    w = replay.Wallet({"sig": dict(CARDS["pref"], id="sig")}, rules, {}, {}, {}, {"sig": 14})
    p = replay.progress(w, [line(3, 380, "sig", online=False), line(5, 120, "sig", online=False)], ["sig"], D(9))
    assert p[0]["used"] == 500 and p[0]["left"] == 700 and p[0]["min_left"] == 500


def test_worth_getting_per_month():
    cards = dict(CARDS, rev={"id": "rev", "kind": "miles", "base_sgd": 0.4, "base_fcy": 0.4, "annual_fee": None,
                             "bank_id": "hsbc", "depth": "headline"})
    rules = dict(RULES, rev=[{"bucket": "w1", "label": "Online", "rate": 4, "match": {"channel": "online"}, "cap": 1000,
                              "cap_period": "calendar", "min_spend": None, "promo_until": None}])
    w = replay.Wallet(cards, rules, {}, {}, {})
    lines = [line(3, 800, "vant"), dict(line(4, 500, "vant"), date=datetime.date(2026, 11, 4))]
    wg = replay.worth_getting(w, lines, ["vant"], ["vant", "rev"], 2)
    assert wg[0]["card"] == "rev" and wg[0]["fee"] is None
    assert wg[0]["per_month"] == {"2026-10": round(800 * (4 - 1.4)), "2026-11": round(500 * (4 - 1.4))}
    assert wg[0]["with_month"]["2026-10"] == 3200


def test_goal_eta():
    from cards import goals
    t = datetime.date(2026, 11, 10)
    assert goals.when(0, 1000, t) == "now"
    assert goals.when(30440, 10000, t) == "Feb 2027"   # 3.044 months x 30.44 d = ~93 d -> 11 Feb 2027
    assert goals.when(1000, 0, t) == "never at this rate"


def test_alternatives_follow_cap_room():
    lines = [line(1, 600, "pref"), line(5, 300, "pref")]   # the first fills Preferred's S$600 online cap
    res, _, _ = replay.run(base(), lines, ["pref", "vant"], ["pref", "vant", "yuu"])
    assert [c for _, c in res[0]["wallet_alts"]] == ["pref", "vant"]
    assert [c for _, c in res[1]["wallet_alts"]] == ["vant", "pref"]   # cap used up: Vantage first now
    assert res[1]["over"]["amount"] == 300


def test_notify_alert_keys_are_stable():
    from cards import notify
    assert notify.PURCHASES and all(len(p) == 4 for p in notify.PURCHASES)


def test_in_store_follows_how_the_person_pays():
    """A phone-only bonus (UOB Preferred style) pays for Apple Pay and not for a tap of the physical card."""
    cards = {"ppv": {"id": "ppv", "kind": "miles", "base_sgd": 0.4, "base_fcy": 0.4, "annual_fee": 0, "bank_id": "uob", "depth": "full"}}
    rules = {"ppv": [{"bucket": "mobile", "label": "Mobile contactless", "rate": 4, "match": {"channel": "mobile"}, "cap": 600,
                      "cap_period": "calendar", "min_spend": None, "promo_until": None}]}
    ln = line(3, 100, "ppv", online=False, mcc="5411")
    assert replay.Wallet(cards, rules, {}, {}, {}, store="mobile").earn("ppv", ln)[0] == 400
    assert replay.Wallet(cards, rules, {}, {}, {}, store="tap").earn("ppv", ln)[0] == 40
    assert replay.in_store({"pay_method": "mobile"}) == "mobile" and replay.in_store({"pay_method": None}) == "mobile"
    assert replay.in_store({"pay_method": "tap"}) == "tap"


def test_tip_counts_cap_room_taken_from_other_purchases():
    """A S$700 purchase on Vantage looks better on Preferred (S$600 cap). If Preferred's cap was really filled by other
    purchases anyway, switching gains nothing overall: no tip. If the cap was free, the tip says how much is over it."""
    big = {**line(1, 700, "vant"), "merchant": "ACME SUBSCRIPTION"}
    full = [big, line(5, 600, "pref")]
    res, tot, _ = replay.run(base(), full, ["pref", "vant"], ["pref", "vant"])
    assert res[0]["wallet_card"] == "pref" and res[0]["wallet_over"]["amount"] == 100
    assert replay.tips(res, base(), off=tot["_off"]) == []
    res, tot, _ = replay.run(base(), [big], ["pref", "vant"], ["pref", "vant"])
    (t,) = replay.tips(res, base(), off=tot["_off"])
    assert t["best"] == "pref" and t["over"] == 100 and t["cap"] == 600
    assert round(t["gain"]) == round(600 * 4 + 100 * 0.4 - 700 * 1.4)


def test_split_the_part_over_the_cap():
    """S$700 on Preferred (S$600 cap): the S$100 over the cap earns 0.4 there and 1.4 on Vantage -> split, +100 miles."""
    big = {**line(1, 700, "vant"), "merchant": "ACME SUBSCRIPTION"}
    res, tot, _ = replay.run(base(), [big], ["pref", "vant"], ["pref", "vant"])
    sp = res[0]["wallet_over"]["split"]
    assert sp["card"] == "vant" and sp["amount"] == 100 and round(sp["extra"]) == 100
    (t,) = replay.tips(res, base(), off=tot["_off"], held=["pref", "vant"])
    assert t["split"]["on_best"] == 600 and t["split"]["card"] == "vant" and t["used_before"] == 0


def test_right_card_on_the_day_uses_the_real_caps():
    """Preferred's cap really was full when the second purchase went on Vantage: on the day, Vantage was right."""
    lines = [line(1, 600, "pref"), line(5, 300, "vant")]
    res, _, _ = replay.run(base(), lines, ["pref", "vant"], ["pref", "vant"])
    assert res[1]["real_alts"][0][1] == "vant"
    assert res[0]["cap_state"] == {"label": "Online", "cap": 600.0, "used": 600.0}


def test_minimum_spend_ladder():
    """Maybank World style: foreign spend earns 2.8 from S$800 on the card that month and 3.2 from S$4,000."""
    from cards import engine
    card = {"id": "mw", "kind": "miles", "base_sgd": 0.4, "base_fcy": 0.4, "annual_fee": 0, "bank_id": "maybank", "depth": "full"}
    rules = [{"bucket": "fcy", "label": "FCY 800", "rate": 2.8, "match": {"fcy": True}, "cap": None, "cap_period": "calendar",
              "min_spend": 800, "promo_until": None},
             {"bucket": "fcy_4000", "label": "FCY 4000", "rate": 3.2, "match": {"fcy": True}, "cap": None, "cap_period": "calendar",
              "min_spend": 4000, "promo_until": None}]
    w = lambda: replay.Wallet({"mw": card}, {"mw": rules}, {"mw": {"4900"}}, {}, {})
    f = lambda d, amt, mcc="5812": {**line(d, amt, "mw", online=False, mcc=mcc), "fcy": True}
    assert replay.run(w(), [f(1, 500)], ["mw"], ["mw"])[1]["actual"] == 500 * 0.4
    assert replay.run(w(), [f(1, 500), f(9, 400)], ["mw"], ["mw"])[1]["actual"] == 900 * 2.8
    assert round(replay.run(w(), [f(1, 2500), f(9, 1600)], ["mw"], ["mw"])[1]["actual"], 6) == 4100 * 3.2
    # utilities are excluded on the card: they earn nothing and don't count toward the S$800
    assert replay.run(w(), [f(1, 500), f(9, 400, "4900")], ["mw"], ["mw"])[1]["actual"] == 500 * 0.4
    # Pay: nothing spent yet -> aim for the first tier; S$800 reached -> 2.8, not the tier still locked; S$4,000 -> 3.2
    p = {"mcc": "5812", "pay": "mobile", "fcy": True, "merchant": "", "today": D(1)}
    rate = lambda room: engine.rate_card(card, rules, set(), p, room=room)[0]
    assert rate(None) == 2.8
    assert rate({"__min__": {"fcy": 300, "fcy_4000": 3500}}) == 2.8
    assert rate({"__min__": {"fcy": 0, "fcy_4000": 3100}}) == 2.8
    assert rate({"__min__": {"fcy": 0, "fcy_4000": 0}}) == 3.2
