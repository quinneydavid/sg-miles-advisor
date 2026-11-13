"""One source per card, one clean-up for every source: the same line through each route, how a line was paid,
statement closing days, award prices, rules as they were. The tests with `pg` run on a real Postgres (pgserver)."""
import datetime
import json


from cards import awards, cache, config, crypto, ingest, llm, pipeline, replay, setupweb, spendweb, statements, wealthfolio as wf

D = datetime.date
TODAY = D.today()
SAME = ("merchant", "amount", "currency", "fcy", "online", "channel", "channel_src", "mcc", "mcc_src")


# ---------------------------------------------------------------- the clean-up itself ----------------------------------------------------------------
def test_clean_reads_what_the_source_does_not_state():
    c = pipeline.clean({"merchant": "  RIVERSIDE   HOTEL BANGKOK TH | a remark", "amount": 100})
    assert c["merchant"] == "RIVERSIDE HOTEL BANGKOK TH" and c["fcy"] and c["currency"] == "THB" and c["online"] is None
    c = pipeline.clean({"merchant": "SHOPEE SINGAPORE", "amount": 5})
    assert c["online"] and (c["channel"], c["channel_src"]) == ("online", "text") and not c["fcy"] and c["currency"] == "SGD"
    c = pipeline.clean({"merchant": "DON QUIJOTE | JPY 12,000.00", "amount": 100})
    assert c["fcy"] and c["currency"] == "JPY"
    for charge in ("LATE PAYMENT CHARGE", "FINANCE CHARGE", "CCY CONVERSION FEE SGD 123.45", "PAYMENT - THANK YOU"):
        assert pipeline.clean({"merchant": charge, "amount": 9}) is None


def test_clean_keeps_what_the_source_states():
    c = pipeline.clean({"merchant": "HOTEL BANGKOK TH", "amount": 100, "fcy": False})          # a feeder that knows
    assert not c["fcy"] and c["currency"] == "SGD"
    c = pipeline.clean({"merchant": "SOMEWHERE", "amount": 100, "currency": "usd"})
    assert c["fcy"] and c["currency"] == "USD"
    c = pipeline.clean({"merchant": "SHOPEE SINGAPORE", "amount": 5, "channel": "contactless"})    # the bank says a tap
    assert c["online"] is False and (c["channel"], c["channel_src"]) == ("contactless", "source")
    c = pipeline.clean({"merchant": "CORNER SHOP", "amount": 5, "online": True, "mcc": 5411, "mcc_src": "bank"})
    assert (c["channel"], c["channel_src"], c["mcc"], c["mcc_src"]) == ("online", "source", "5411", "bank")


def test_the_shop_of_a_line():
    assert pipeline.shop_key("GRAB*A-8812345 SINGAPORE SG") == pipeline.shop_key("Grab* A-771") == "GRAB"
    assert pipeline.shop_key("COLD STORAGE-VIVO SINGAPORE") == "COLD STORAGE" and pipeline.shop_key("12345 SG") == ""


# ---------------------------------------------------------------- how a line was paid ----------------------------------------------------------------
def test_how_a_line_was_paid_follows_the_order():
    line = lambda **k: {"merchant": "COLD STORAGE VIVO", "date": D(2026, 11, 5), **k}
    mem = {"COLD STORAGE": {"ch": "tap", "since": "2026-11-01"}}
    assert pipeline.paid(line(channel="mobile", channel_src="source"), mem) == ("mobile", "bank")       # the source states it
    assert pipeline.paid(line(), mem) == ("tap", "you")                                                    # the saved answer
    assert pipeline.paid(line(channel="online", channel_src="text", online=True), mem) == ("tap", "you")  # beats the text
    assert pipeline.paid(line(channel="online", channel_src="text", online=True), {}) == ("online", "text")
    assert pipeline.paid(line(), {}) == (None, "setting")                                                  # the usual way
    assert pipeline.paid(line(channel="contactless", channel_src="source"), {}) == (None, "bank")         # a tap: phone or card
    assert pipeline.paid(line(channel="contactless", channel_src="source"), mem) == ("tap", "you")        # ... the answer says which
    assert pipeline.paid(line(date=D(2026, 10, 31)), mem) == (None, "setting")                             # before the open month
    assert pipeline.paid(line(channel="online"), {}) == ("online", "text")                                 # lines stored before 0.8.0


def test_an_answer_counts_from_the_open_month_or_the_lines_month():
    mem = {}
    assert pipeline.remember(mem, "COLD STORAGE VIVO", "tap", D(2026, 11, 20), today=D(2026, 11, 25)) == "COLD STORAGE"
    assert mem["COLD STORAGE"]["since"] == "2026-11-01"
    pipeline.remember(mem, "COLD STORAGE ION", "mobile", D(2026, 9, 14), today=D(2026, 11, 25))    # a line of an older month
    assert mem["COLD STORAGE"] == {"ch": "mobile", "since": "2026-09-01", "name": "COLD STORAGE ION"}
    pipeline.remember(mem, "COLD STORAGE", "", None)
    assert mem == {} and pipeline.remember(mem, "1234", "tap") is None


def test_the_replay_uses_how_the_line_was_paid():
    rule = {"bucket": "b", "rate": 4, "match": {"channel": "mobile"}, "cap": None, "period": "month", "promo_until": None,
            "min_spend": None, "valid_from": D(2026, 1, 1), "valid_to": None}
    card = {"id": "c", "kind": "miles", "base_sgd": 0.4, "base_fcy": 0.4}
    w = replay.Wallet({"c": card}, {"c": [rule]}, {}, {}, {}, store="mobile")
    line = {"merchant": "X", "amount": 100.0, "date": D(2026, 11, 5), "mcc": "5411"}
    assert w.earn("c", line)[0] == 400                               # the person's usual way: phone
    assert w.earn("c", dict(line, pay="tap"))[0] == 40               # this shop: the physical card, so no phone bonus
    assert w.earn("c", dict(line, pay="online", online=True))[0] == 40


def test_a_purchase_before_a_rule_change_is_valued_with_the_rule_of_its_time():
    mk = lambda rate, a, b: {"bucket": "dining", "rate": rate, "match": {"mcc": ["5812"]}, "cap": None, "period": "month",
                             "promo_until": None, "min_spend": None, "valid_from": a, "valid_to": b, "card_id": "c"}
    oldest, old, cur = mk(10, D(2025, 1, 1), D(2026, 3, 1)), mk(6, D(2026, 3, 1), D(2026, 9, 1)), mk(4, D(2026, 9, 1), None)
    every = [dict(r, _a=None if r is oldest else r["valid_from"], _b=r["valid_to"]) for r in (oldest, old, cur)]
    card = {"id": "c", "kind": "miles", "base_sgd": 0.4, "base_fcy": 0.4}
    w = replay.Wallet({"c": card}, {"c": [cur]}, {}, {}, {}, hist={"c": (D(2026, 9, 1), every)})
    line = lambda d: {"merchant": "X", "amount": 100.0, "date": d, "mcc": "5812"}
    assert w.earn("c", line(D(2026, 10, 1)))[0] == 400       # today's rule
    assert w.earn("c", line(D(2026, 5, 1)))[0] == 600        # the version in force that day
    assert w.earn("c", line(D(2024, 6, 1)))[0] == 1000       # before the first record: the earliest version known
    plain = replay.Wallet({"c": card}, {"c": [cur]}, {}, {}, {})
    assert plain.earn("c", line(D(2026, 5, 1)))[0] == 400    # no recorded change: today's rules, and the page says so


# ---------------------------------------------------------------- with a database ----------------------------------------------------------------
class Wealthfolio:
    """A stand-in client: activities per account, the way wealthfolio.Client hands them over."""

    def __init__(self, activities):
        self.acts = activities

    def activities(self, account, max_pages=200):
        return list(self.acts.get(account, []))

    def activity_pages(self, account, max_pages=2000):
        yield list(self.acts.get(account, []))

    def balances(self, account):
        return [], None


def stored(conn, sid):
    rows = conn.execute("SELECT id, card_id, txn_date, data_enc FROM stmt_lines WHERE statement_id = %s ORDER BY txn_date, id", (sid,)).fetchall()
    return [dict(crypto.dec_json(conn, r["data_enc"], f"stmt_lines.data_enc:{r['id']}"), card=r["card_id"], date=r["txn_date"]) for r in rows]


def person(conn):
    return conn.execute("SELECT * FROM people WHERE id = 'alice'").fetchone()


TEXTS = [("SHOPEE SINGAPORE", 25.0), ("RIVERSIDE HOTEL BANGKOK TH", 310.5), ("NTUC FAIRPRICE VIVO", 42.1), ("SOME UNKNOWN PLACE", 9.9),
         ("SINGAPORE AIRLINES SIN", 880.0)]


def test_the_same_line_comes_out_the_same_through_every_route(pg, monkeypatch):
    conn, ids = pg
    a, b, c, d = ids[:4]
    conn.execute("INSERT INTO merchants (slug, name, mccs, source, channel) VALUES ('ntuc', 'NTUC FairPrice', '{5411}', 'wiki', 'in_person'), "
                 "('shopee', 'Shopee', '{5262}', 'wiki', 'online'), ('sia', 'Singapore Airlines', '{3075}', 'wiki', 'both')")
    conn.commit()
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    day = TODAY - datetime.timedelta(days=3)
    # 1. Wealthfolio
    config.put(conn, "wf_url", "http://wf.example", "alice")
    config.put(conn, "wf_token", "t", "alice")
    config.put(conn, "wf_map", json.dumps({"acc": a}), "alice")
    conn.commit()
    client = Wealthfolio({"acc": [{"id": f"w{i}", "date": day.isoformat() + "T00:00:00+00:00", "activityType": "WITHDRAWAL", "amount": amt,
                                   "notes": text} for i, (text, amt) in enumerate(TEXTS)]})
    wf.run_person(conn, person(conn), client=client, today=TODAY)
    # 2. a feeder that states nothing but the text
    statements.feed(conn, "alice", {"from": day.isoformat(), "to": day.isoformat(), "lines": [
        {"id": f"f{i}", "date": day.isoformat(), "card": b, "merchant": text, "amount": amt} for i, (text, amt) in enumerate(TEXTS)]})
    # 3. a statement file
    monkeypatch.setattr(statements, "pdf_text", lambda data, pw: "x" * 300)
    monkeypatch.setattr(llm, "read_statement", lambda text: {"bank": "Bank", "period_to": day.isoformat(), "sections": [
        {"name": "Card", "last4": "1111", "lines": [{"date": day.isoformat(), "description": t, "amount": amt} for t, amt in TEXTS]}]})
    st = ingest.settings(conn, "alice")
    st["last4"] = {"1111": c}
    ingest.save_settings(conn, "alice", st)
    conn.commit()
    sid, _ = statements.start(conn, "alice", b"%PDF-1", "", None, wait=True)
    # 4. alert mails
    ingest.store_alerts(conn, "alice", [{"date": day, "card": d, "merchant": t, "amount": amt, "currency": "SGD", "fcy": False,
                                         "online": None, "est": False, "mid": str(i)} for i, (t, amt) in enumerate(TEXTS)])
    key = lambda x: x["amount"]
    routes = [sorted(stored(conn, s), key=key) for s in ("wf-alice", "feed-alice", sid, "alerts-alice")]
    assert [len(r) for r in routes] == [5, 5, 5, 5]
    for other in routes[1:]:
        for mine, theirs in zip(routes[0], other):
            assert {k: mine[k] for k in SAME} == {k: theirs[k] for k in SAME}
    by = {x["merchant"]: x for x in routes[0]}
    assert by["SHOPEE SINGAPORE"]["online"] and by["SHOPEE SINGAPORE"]["mcc"] == "5262"
    assert by["RIVERSIDE HOTEL BANGKOK TH"]["fcy"] and by["RIVERSIDE HOTEL BANGKOK TH"]["currency"] == "THB"
    assert by["NTUC FAIRPRICE VIVO"]["mcc"] == "5411" and by["SOME UNKNOWN PLACE"]["mcc"] is None
    assert by["SINGAPORE AIRLINES SIN"]["online"]                      # a ticket: bought online
    # the statement's own closing date became its card's closing day
    me = person(conn)
    assert me["statement_days"][c] == day.day and me["statement_days_src"][c] == "statement"


def test_bank_charges_never_become_spend_whatever_brings_them(pg, monkeypatch):
    conn, ids = pg
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    day = TODAY.isoformat()
    res = statements.feed(conn, "alice", {"from": day, "to": day, "lines": [
        {"id": "1", "date": day, "card": ids[0], "merchant": "LATE PAYMENT CHARGE", "amount": 100},
        {"id": "2", "date": day, "card": ids[0], "merchant": "KOPITIAM", "amount": 5, "mcc": "5814", "mcc_src": "bank", "channel": "contactless"}]})
    assert res["added"] == 1
    (ln,) = stored(conn, "feed-alice")
    assert (ln["mcc"], ln["mcc_src"], ln["channel"], ln["channel_src"]) == ("5814", "bank", "contactless", "source")


def test_the_closing_day_keeps_the_best_source(pg):
    conn, ids = pg
    c = ids[0]
    say = lambda day, src: pipeline.set_close_day(conn, "alice", c, day, src)
    assert say(20, "note") and person(conn)["statement_days"][c] == 20
    assert say(22, "feed") and say(23, "statement") and not say(21, "feed") and not say(19, "note")
    assert person(conn)["statement_days"][c] == 23
    assert say(25, "you") and not say(24, "statement")
    assert (person(conn)["statement_days"][c], person(conn)["statement_days_src"][c]) == (25, "you")
    # the feeder may still send it: a typed value stays
    statements.feed(conn, "alice", {"lines": [], "statement_days": {c: 9, ids[1]: 12}})
    me = person(conn)
    assert me["statement_days"] == {c: 25, ids[1]: 12} and me["statement_days_src"][ids[1]] == "feed"
    # typed away: the calendar month again, until a source says
    assert say(None, "you") and c not in person(conn)["statement_days"]
    # the Settings form: only a field the person changed counts as typed
    form = {f"day_{ids[1]}": ["12"], f"was_{ids[1]}": ["12"], f"day_{c}": ["7"], f"was_{c}": [""]}
    setupweb.post_closing(conn, form, person(conn))
    me = person(conn)
    assert me["statement_days_src"] == {ids[1]: "feed", c: "you"} and me["statement_days"][c] == 7
    assert "typed here" in setupweb.closing_section(conn, me) and "from the feeder" in setupweb.closing_section(conn, me)


def test_a_card_moves_between_sources_and_each_path_respects_it(pg, monkeypatch):
    conn, ids = pg
    a, b = ids[:2]
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    day = TODAY.isoformat()
    feed = lambda: statements.feed(conn, "alice", {"from": day, "to": day, "lines": [
        {"id": "1", "date": day, "card": a, "merchant": "KOPITIAM", "amount": 5}, {"id": "2", "date": day, "card": b, "merchant": "KOPITIAM", "amount": 6}]})
    feed()
    config.put(conn, "wf_url", "http://wf.example", "alice")
    config.put(conn, "wf_token", "t", "alice")
    config.put(conn, "wf_map", json.dumps({"acc": a}), "alice")
    config.put(conn, "wf_on", "1", "alice")
    conn.commit()
    client = Wealthfolio({"acc": [{"id": "w1", "date": day, "activityType": "WITHDRAWAL", "amount": 5, "notes": "KOPITIAM"}]})
    assert wf.run_person(conn, person(conn), client=client)["cards left to the feeder"] == 1
    eff = pipeline.effective(conn, person(conn), {a})
    assert eff[a] == ("feed", False) and eff[ids[2]] == ("mail", False)
    page = setupweb.sources_section(conn, person(conn))
    assert "Automatic (now: Feeder)" in page and "Wealthfolio" in page
    # the person gives the first card to Wealthfolio
    monkeypatch.setattr(wf, "Client", lambda url, token: client)
    ok, said = setupweb.post_sources(conn, {f"src_{a}": ["wealthfolio"]}, person(conn))
    assert ok and "1 line(s) of the old source were removed" in said and "Imported from Wealthfolio: 1 new" in said
    assert [x["card"] for x in stored(conn, "wf-alice")] == [a] and [x["card"] for x in stored(conn, "feed-alice")] == [b]
    # a feeder that still sends that card is not listened to for it
    res = feed()
    assert res["left to another source"] == 1 and [x["card"] for x in stored(conn, "feed-alice")] == [b]
    assert [(ln["card_id"], ln["statement_id"]) for ln in statements.load_lines(conn, "alice")] == [(a, "wf-alice"), (b, "feed-alice")]
    # a card that is not matched to an account cannot be given to Wealthfolio
    ok, said = setupweb.post_sources(conn, {f"src_{a}": ["wealthfolio"], f"src_{b}": ["wealthfolio"]}, person(conn))
    assert not ok and "not matched to a Wealthfolio account" in said
    # back to automatic, then to mail: the automatic sources' lines of it go, statement files would stay
    ok, said = setupweb.post_sources(conn, {f"src_{a}": ["mail"]}, person(conn))
    assert ok and stored(conn, "wf-alice") == [] and person(conn)["card_sources"] == {a: "mail"}
    assert wf.run_person(conn, person(conn), client=client)["cards left to another source"] == 1


def test_someone_with_only_mail_sees_nothing_to_choose(pg):
    conn, ids = pg
    page = setupweb.sources_section(conn, person(conn))
    assert "Nothing to choose" in page and "<select" not in page


def test_an_answer_on_a_purchase_is_remembered_for_the_shop(pg, monkeypatch):
    conn, ids = pg
    a = ids[0]
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    this, last = TODAY.replace(day=1), TODAY.replace(day=1) - datetime.timedelta(days=5)
    statements.feed(conn, "alice", {"lines": [
        {"id": "1", "date": this.isoformat(), "card": a, "merchant": "COLD STORAGE VIVO", "amount": 50},
        {"id": "2", "date": this.isoformat(), "card": a, "merchant": "COLD STORAGE ION SINGAPORE", "amount": 60},
        {"id": "3", "date": last.isoformat(), "card": a, "merchant": "COLD STORAGE VIVO", "amount": 70},
        {"id": "4", "date": this.isoformat(), "card": a, "merchant": "KOPITIAM", "amount": 5, "channel": "mobile"}]})
    lines = {ln["amount"]: ln for ln in cache.lines(conn, "alice")}
    assert lines[50]["pay"] is None and lines[5]["pay"] == "mobile"
    back = spendweb.post_paid(conn, {"line": [lines[50]["id"]], "ch": ["tap"], "back": ["/spend/lines?month=2026-11"]}, person(conn))
    assert back == "/spend/lines?month=2026-11"
    lines = {ln["amount"]: ln for ln in cache.lines(conn, "alice")}                  # the cache saw the change
    assert (lines[50]["pay"], lines[50]["paid_src"]) == ("tap", "you") and lines[60]["pay"] == "tap"    # the same shop, this month
    assert lines[70]["pay"] is None                                                   # last month is closed: left as it was
    assert lines[5]["pay"] == "mobile"                                                # the bank's word stays
    # stored encrypted, shown on Settings, and can be forgotten
    raw = conn.execute("SELECT settings_enc FROM people WHERE id = 'alice'").fetchone()["settings_enc"]
    assert b"COLD STORAGE" not in bytes(raw)
    page = setupweb.paid_section(conn, person(conn))
    assert "1 shop(s) remembered" in page and "Contactless card" in page
    setupweb.post_paid(conn, {"forget": ["COLD STORAGE"]}, person(conn))
    assert cache.lines(conn, "alice")[0]["pay"] is None and "0 shop(s) remembered" in setupweb.paid_section(conn, person(conn))
    # a bad return address is not followed
    assert spendweb.post_paid(conn, {"line": ["x"], "ch": ["tap"], "back": ["https://elsewhere.example/"]}, person(conn)) == "/spend/lines"


def test_award_prices_come_from_the_chart_and_a_feeders_fresh_rows_stay(pg):
    conn, ids = pg
    row = lambda: conn.execute("SELECT * FROM award_prices WHERE dest = 'BKK' AND cabin = 'J'").fetchone()
    chart = awards.prices("BKK", "J")
    assert row()["saver"] == chart["saver"] and row()["city"] == "Bangkok" and row()["seen_dates"] is None
    assert conn.execute("SELECT count(DISTINCT dest) n FROM award_prices").fetchone()["n"] == len(awards.chart()["airport_zone"])
    # a feeder's row with seat dates is newer knowledge: the chart fill leaves it for 30 days
    statements.feed(conn, "alice", {"lines": [], "awards": {"destinations": [
        {"airport": "BKK", "city": "Bangkok", "region": "Asia", "cabins": {"J": {"saver": 24000, "advantage": 40000, "seen_dates": 9, "next": "2027-01-05"}}}]}})
    awards.fill(conn)
    assert row()["saver"] == 24000 and row()["seen_dates"] == 9
    conn.execute("UPDATE award_prices SET updated_at = now() - interval '40 days' WHERE dest = 'BKK' AND cabin = 'J'")
    awards.fill(conn)
    assert row()["saver"] == chart["saver"]


def test_the_pages_say_which_rules_value_past_months(pg):
    conn, ids = pg
    assert "Past months are valued with today's rules" in spendweb.rules_note(conn)
    r = conn.execute("SELECT * FROM card_rules WHERE valid_to IS NULL LIMIT 1").fetchone()
    conn.execute("INSERT INTO card_rules (card_id, bucket, label, match, rate, valid_from, valid_to) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (r["card_id"], r["bucket"], r["label"], json.dumps(r["match"]), 9, D(2020, 1, 1), r["valid_from"]))
    conn.commit()
    cache._cat.update(stamp=None, at=0.0, data=None)
    assert "for the 1 card(s) whose changes the catalogue recorded" in spendweb.rules_note(conn)
    hist = cache.catalogue(conn)["hist"][r["card_id"]]
    assert hist[0] == r["valid_from"] and {x["_b"] for x in hist[1] if x["bucket"] == r["bucket"]} == {None, r["valid_from"]}


def test_history_lengthens_what_the_advisor_reads(pg, monkeypatch):
    conn, ids = pg
    a = ids[0]
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    config.put(conn, "wf_url", "http://wf.example", "alice")
    config.put(conn, "wf_token", "t", "alice")
    config.put(conn, "wf_map", json.dumps({"acc": a}), "alice")
    conn.commit()
    first = (TODAY.replace(day=1) - datetime.timedelta(days=200)).replace(day=10)
    acts, d, i = [], first, 0
    while d < TODAY:
        acts.append({"id": f"h{i}", "date": d.isoformat(), "activityType": "WITHDRAWAL", "amount": 100 + i, "notes": "KOPITIAM"})
        d, i = (d.replace(day=1) + datetime.timedelta(days=32)).replace(day=10), i + 1
    client = Wealthfolio({"acc": acts})
    wf.run_person(conn, person(conn), client=client)
    before = len({ln["date"].strftime("%Y-%m") for ln in cache.lines(conn, "alice")})
    got = wf.history(conn, person(conn), client=client)
    months = {ln["date"].strftime("%Y-%m") for ln in cache.lines(conn, "alice")}
    assert before <= 3 and len(months) == len(acts) >= 6 and got["oldest"] == first.isoformat()
    from cards import advise
    use, counted = advise.recent(cache.lines(conn, "alice"), TODAY)
    assert counted >= 3                 # enough complete months for the advisor's "too little spend on file" warning to clear


def test_the_pages_render_with_everything_on(pg, monkeypatch):
    from cards import web
    conn, ids = pg
    a = ids[0]
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    day = TODAY.isoformat()
    statements.feed(conn, "alice", {"lines": [{"id": "1", "date": day, "card": a, "merchant": "COLD STORAGE VIVO", "amount": 50},
                                               {"id": "2", "date": day, "card": a, "merchant": "KOPITIAM", "amount": 5, "channel": "mobile"}],
                                    "statement_days": {a: 12}})
    config.put(conn, "wf_url", "http://wf.example", "alice")
    config.put(conn, "wf_token", "t", "alice")
    config.put(conn, "wf_map", json.dumps({"acc": a}), "alice")
    config.put(conn, "wf_accounts", json.dumps([{"id": "acc", "name": "Card", "type": "CREDIT_CARD"}, {"id": "sav", "name": "Savings", "type": "CASH"},
                                                {"id": "ib", "name": "Broker", "type": "SECURITIES"}]), "alice")
    bid = conn.execute("INSERT INTO card_boosts (card_id, label, requirement, tiers) VALUES (%s, 'Savings balance', 'deposit', '[[1000, 2]]') RETURNING id",
                       (a,)).fetchone()["id"]
    conn.commit()
    me = person(conn)
    title, body = web.settings(conn, {}, me)
    for want in ("Where each card&#x27;s transactions come from" if False else "Where each card's transactions come from", "Statement closing days",
                 "How each shop is paid", "Older purchases", "Import history", "Savings balance for a boosted rate", "from the feeder"):
        assert want in body, want
    wfpart = setupweb.wealthfolio_section(conn, me)
    boost_form = wfpart[wfpart.index("Savings balance for a boosted rate"):]
    assert ">Savings<" in boost_form and ">Broker<" not in boost_form and ">Card<" not in boost_form      # cash accounts only
    assert setupweb.post_wealthfolio(conn, {"action": ["boost"], "boost": [str(bid)], "boost_account": ["sav"]}, me) is None
    assert wf.boost_choice(config.load(conn, "alice")) == ("sav", str(bid))
    # the transactions page: the question on a purchase whose bank did not say, none where it did
    title, body = spendweb.lines_page(conn, {}, me)
    assert body.count("How was this paid?") == 1 and "Remember for this shop" in body and "paid: Phone or watch (the bank&#x27;s line says so)" in body
    assert "Past months are valued with today's rules" in body
    # a history import that cannot start says why; a bad date too
    ok, said = setupweb.post_wealthfolio(conn, {"action": ["history"], "since": ["soon"]}, me)
    assert not ok and "not a date" in said
    started = []
    monkeypatch.setattr(wf, "start_history", lambda pid, since: started.append((pid, since)))
    ok, said = setupweb.post_wealthfolio(conn, {"action": ["history"], "since": ["2025-01-01"]}, me)
    assert ok and started == [("alice", D(2025, 1, 1))]
    ok, said = setupweb.post_wealthfolio(conn, {"action": ["history"]}, me)
    assert not ok and "already running" in said and "History import running" in setupweb.wealthfolio_section(conn, me)
