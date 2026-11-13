"""Nothing needs a feeder: annual fees from the app's own lines, the points wallet (statement or typed balances,
AwardWallet), dates with award seats, match-day rates, the boost account suggestion, a feeder that goes quiet or is
stopped, and the currency a Wealthfolio note names. The tests with `pg` run on a real Postgres (pgserver)."""
import datetime
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cards import awards, cache, config, crypto, engine, fees, fixtures, goals, ingest, llm, outside, points, replay, setupweb, statements
from cards import wealthfolio as wf
from test_onesource import Wealthfolio, person, stored

D = datetime.date
TODAY = D.today()


class Service:
    """A stand-in for an outside service: answers GET with what `route(path, headers)` returns ((status, body dict))."""

    def __init__(self, route):
        outer = self
        self.calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.calls.append(self.path)
                status, body = route(self.path, self.headers)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def act(i, day, kind, amount, notes):
    return {"id": f"w{i}", "date": day.isoformat() + "T00:00:00+00:00", "activityType": kind, "amount": amount, "notes": notes}


def connect(conn, card):
    config.put(conn, "wf_url", "http://wf.example", "alice")
    config.put(conn, "wf_token", "t", "alice")
    config.put(conn, "wf_map", json.dumps({"acc": card}), "alice")
    conn.commit()


# ---------------------------------------------------------------- the currency a Wealthfolio note names ----------------------------------------------------------------
def test_a_note_names_the_foreign_currency_and_amount():
    day = TODAY - datetime.timedelta(days=2)
    got, _ = wf.lines_from([
        act(1, day, "WITHDRAWAL", 27.4, "TRAVELSHOP.COM HARBOUR INTERNET HK | Some Bank statement 2026-10 | USD 20.00"),
        act(2, day, "WITHDRAWAL", 13.9, "SOME CAFE | alert, estimated from EUR 9.50 (the statement will confirm it)"),
        act(3, day, "WITHDRAWAL", 8.0, "KOPITIAM VIVO | alert"),
        act(4, day, "WITHDRAWAL", 50.0, "RIVERSIDE HOTEL BANGKOK TH | Bank statement 2026-10"),          # no currency named: the place decides
        act(5, day, "WITHDRAWAL", 12.0, "HOTEL BANGKOK TH | Bank statement 2026-10 | USD 9.00"),         # named: it wins over the place
        act(6, day, "WITHDRAWAL", 5.0, "LOCAL SHOP | SGD 5.00"),
    ], "card", day, day)
    by = {x["id"]: x for x in got}
    assert (by["w1"]["fcy"], by["w1"]["currency"], by["w1"]["fcy_amount"], by["w1"]["est"]) == (True, "USD", 20.0, False)
    assert (by["w2"]["fcy"], by["w2"]["currency"], by["w2"]["fcy_amount"], by["w2"]["est"]) == (True, "EUR", 9.5, True)
    assert by["w3"]["est"] and not by["w3"]["fcy"] and "fcy_amount" not in by["w3"]
    assert by["w4"]["fcy"] and by["w4"]["currency"] == "THB" and "fcy_amount" not in by["w4"]
    assert by["w5"]["currency"] == "USD" and by["w5"]["fcy_amount"] == 9.0
    assert not by["w6"]["fcy"] and by["w6"]["currency"] == "SGD"
    assert by["w1"]["merchant"] == "TRAVELSHOP.COM HARBOUR INTERNET HK"
    # the statement month is still read when it is not the only remark
    assert wf.close_days([dict(x, card="card") for x in got if x["id"] == "w1"]) == {"card": day.day}


def test_an_estimate_gives_way_to_its_statement_line_and_the_answer_for_the_shop_stays(pg, monkeypatch):
    conn, ids = pg
    a = ids[0]
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    connect(conn, a)
    day = TODAY - datetime.timedelta(days=4)
    est = Wealthfolio({"acc": [act(1, day, "WITHDRAWAL", 13.9, "SOME CAFE PARIS | alert, estimated from EUR 9.50 (the statement will confirm it)")]})
    wf.run_person(conn, person(conn), client=est, today=TODAY)
    lines = cache.lines(conn, "alice")
    assert len(lines) == 1 and lines[0]["est"] and lines[0]["fcy_amount"] == 9.5 and lines[0]["currency"] == "EUR"
    from cards import pipeline, spendweb
    spendweb.post_paid(conn, {"line": [lines[0]["id"]], "ch": ["tap"], "back": ["/x"]}, person(conn))   # "I tapped the card there"
    # the statement arrives: the importer deleted the estimate and wrote the statement line under a new id
    real = Wealthfolio({"acc": [act(2, day, "WITHDRAWAL", 14.05, "SOME CAFE PARIS | Bank statement 2026-11 | EUR 9.50")]})
    counts = wf.run_person(conn, person(conn), client=real, today=TODAY)
    assert (counts["new"], counts["removed"]) == (1, 1)
    lines = cache.lines(conn, "alice")
    assert len(lines) == 1 and not lines[0]["est"] and lines[0]["amount"] == 14.05           # one line, never two
    assert lines[0]["pay"] == "tap" and lines[0]["paid_src"] == "you"                         # the answer went with the shop


# ---------------------------------------------------------------- annual fees ----------------------------------------------------------------
def test_annual_fees_come_from_the_apps_own_lines_and_never_count_as_spend(pg, monkeypatch):
    conn, ids = pg
    a = ids[0]
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    connect(conn, a)
    charged, back = TODAY - datetime.timedelta(days=200), TODAY - datetime.timedelta(days=170)
    client = Wealthfolio({"acc": [act(1, charged, "WITHDRAWAL", 196.2, "ANNUAL FEE | Bank statement"),
                                  act(2, back, "CREDIT", 196.2, "ANNUAL FEE REVERSAL | Bank statement"),
                                  act(3, charged, "WITHDRAWAL", 40.0, "NTUC FAIRPRICE | Bank statement")]})
    wf.history(conn, person(conn), None, client=client, today=TODAY)
    spend, fee = cache.lines(conn, "alice"), cache.fee_lines(conn, "alice")
    assert [ln["merchant"] for ln in spend] == ["NTUC FAIRPRICE"]                                # the fee is not spend
    assert sorted(ln["amount"] for ln in fee) == [-196.2, 196.2]
    ev = fees.events(conn, "alice", fee)[a]
    assert [(f["date"], f["amount"]) for f in ev] == [(charged, 196.2), (back, -196.2)]
    row = next(r for r in fees.tracker(conn, person(conn), cache.catalogue(conn), spend, fee, TODAY) if r["card"] == a)
    assert row["last"]["date"] == charged and row["waived_last"] and row["fee_src"] == "charged" and row["due"] > TODAY
    base, _ = replay.load(conn)
    res, tot, _ = replay.run(base, [ln for ln in spend if ln["amount"] > 0], [a], [a])
    assert tot["spend"] == 40.0


# ---------------------------------------------------------------- the points wallet ----------------------------------------------------------------
def programme_of(conn, card):
    return conn.execute("SELECT programme_id FROM cards WHERE id = %s", (card,)).fetchone()["programme_id"]


def test_the_wallet_is_worked_out_without_a_feeder(pg, monkeypatch):
    conn, ids = pg
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    card = next(c for c in ids if programme_of(conn, c) not in (None, "KF"))
    prog = programme_of(conn, card)
    conn.execute("UPDATE people SET held_cards = %s WHERE id = 'alice'", ([card],))
    conn.execute("INSERT INTO transfer_partners (programme_id, partner, ratio_from, ratio_to, block) VALUES (%s, 'KrisFlyer', 5000, 2000, 5000) "
                 "ON CONFLICT (programme_id, partner) DO UPDATE SET ratio_from = 5000, ratio_to = 2000, block = 5000", (prog,))
    conn.commit()
    assert points.wallet(conn, person(conn)) == (None, None)                                    # nothing known yet
    since = TODAY - datetime.timedelta(days=30)
    assert points.post(conn, {"programme": [prog], "balance": ["12,400"], "date": [since.isoformat()]}, person(conn)) is None
    assert points.post(conn, {"programme": ["KF"], "balance": ["30000"], "date": [since.isoformat()]}, person(conn)) is None
    wal, at = points.wallet(conn, person(conn))
    pool = wal["pools"][0]
    assert wal["own"] and (pool["programme"], pool["balance"], pool["kf"], pool["leftover"], pool["source"]) == (prog, 12400, 4000, 2400, "typed")
    assert wal["kf_account"] == 30000 and wal["total"] == 34000 and wal["kf_account_source"] == "typed"
    # points earned on that programme's cards after the date are added
    connect(conn, card)
    wf.run_person(conn, person(conn), client=Wealthfolio({"acc": [act(1, TODAY - datetime.timedelta(days=3), "WITHDRAWAL", 1000.0, "SOME SHOP")]}), today=TODAY)
    wal2, _ = points.wallet(conn, person(conn))
    assert wal2["pools"][0]["earned_since"] > 0 and wal2["pools"][0]["balance"] == 12400 + wal2["pools"][0]["earned_since"]
    # a statement printed later than what was typed wins; an older one does not
    stmt = {prog: {"balance": 20000.0, "date": TODAY.isoformat(), "bank": "Bank", "name": "Points", "expiry": None}}
    conn.execute("UPDATE people SET points_enc = %s WHERE id = 'alice'", (crypto.enc_json(conn, stmt, "people.points_enc:alice"),))
    conn.commit()
    wal3, _ = points.wallet(conn, person(conn))
    assert wal3["pools"][0]["balance"] == 20000 and wal3["pools"][0]["source"] == "Bank statement" and wal3["pools"][0]["kf"] == 8000
    # the pages show it the same way, and removing what was typed works
    title, body = points.page(conn, {}, person(conn))
    assert "KrisFlyer miles you could have now" in body and "Type a balance" in body and "feeder" not in body.split("<h2>")[0]
    assert "KrisFlyer miles you could have now" in goals.page(conn, {}, person(conn))[1]
    points.post(conn, {"programme": ["KF"], "action": ["remove"]}, person(conn))
    assert points.wallet(conn, person(conn))[0]["kf_account"] is None
    assert points.post(conn, {"programme": [prog], "balance": ["x"]}, person(conn))             # nonsense is refused with a sentence


def aw_route(key="k", user="77", balance="43210.0"):
    def route(path, headers):
        if headers.get("X-Authentication") != key:
            return 401, {}
        if not path.endswith(f"/connectedUser/{user}"):
            return 404, {}
        return 200, {"accounts": [{"code": "other", "balanceRaw": "1"},
                                  {"code": "singaporeair", "displayName": "Singapore Airlines (KrisFlyer)", "balanceRaw": balance,
                                   "lastRetrieveDate": "2026-11-12T01:00:00+00:00", "properties": [{"kind": 6, "value": "1,000 on 31 Dec"}]}]}
    return route


def test_awardwallet_answers_and_its_faults_are_named():
    svc = Service(aw_route())
    try:
        got = points.aw_fetch("k", "77", svc.url)
        assert (got["balance"], got["expiring"], got["source"]) == (43210, "1,000 on 31 Dec", "AwardWallet")
        for key, user, word in (("wrong", "77", "refused"), ("k", "78", "connected user")):
            with pytest.raises(ValueError, match=word):
                points.aw_fetch(key, user, svc.url)
    finally:
        svc.close()
    with pytest.raises(ValueError, match="could not be reached"):
        points.aw_fetch("k", "77", "http://127.0.0.1:1")


def test_awardwallet_keeps_the_krisflyer_account_current(pg):
    conn, ids = pg
    svc = Service(aw_route())
    try:
        assert points.aw_refresh(conn, "alice", api=svc.url) is None                            # not connected: nothing asked
        config.put(conn, "aw_key", "k", "alice")
        config.put(conn, "aw_user", "77", "alice")
        conn.commit()
        assert config.where(conn, "alice")["aw_key"] == "app" and "aw_key" not in str(config.stored(conn, "alice")["aw_key"]["value"])
        assert points.aw_refresh(conn, "alice", api=svc.url)["balance"] == 43210
        assert points.aw_refresh(conn, "alice", api=svc.url) is None and len(svc.calls) == 1    # asked recently: not again
        wal, _ = points.wallet(conn, person(conn))
        assert wal["kf_account"] == 43210 and wal["kf_account_source"] == "AwardWallet" and wal["kf_expiring"] == "1,000 on 31 Dec"
        # an outage keeps the last balance and says what went wrong on the Settings page
        config.put(conn, "aw_key", "wrong", "alice")
        conn.commit()
        assert points.aw_refresh(conn, "alice", force=True, api=svc.url) is None
        assert points.wallet(conn, person(conn))[0]["kf_account"] == 43210
        assert "refused" in setupweb.awardwallet_section(conn, person(conn)) and "wrong" not in setupweb.awardwallet_section(conn, person(conn))
    finally:
        svc.close()


# ---------------------------------------------------------------- dates with award seats ----------------------------------------------------------------
def seat_rows(now):
    fresh, old = now.isoformat(), (now - datetime.timedelta(days=60)).isoformat()
    row = lambda dest, date, j, avail, upd, origin="SIN": {
        "Route": {"OriginAirport": origin, "DestinationAirport": dest, "DestinationRegion": "Europe"}, "Date": date, "UpdatedAt": upd,
        "JMileageCost": str(j), "JAvailable": avail, "YMileageCost": "0", "YAvailable": False}
    return [row("LHR", "2027-03-02", 103500, True, fresh), row("LHR", "2027-02-01", 180000, True, fresh),
            row("LHR", "2027-01-05", 103500, True, old),              # seen too long ago: not a date with seats
            row("CDG", "2027-04-01", 103500, False, fresh), row("LHR", "2027-01-01", 90000, True, fresh, origin="KUL")]


def test_the_seat_scan_is_optional_and_fills_dates_with_seats(pg):
    conn, ids = pg
    now = datetime.datetime.now(datetime.timezone.utc)
    pages = {"": {"data": seat_rows(now)[:3], "hasMore": True, "cursor": "c2"}, "c2": {"data": seat_rows(now)[3:], "hasMore": False}}

    def route(path, headers):
        if headers.get("Partner-Authorization") != "sk":
            return 401, {}
        cursor = path.split("cursor=")[1].split("&")[0] if "cursor=" in path else ""
        return 200, pages[cursor]
    svc = Service(route)
    try:
        assert awards.seats_run(conn, api=svc.url) is None and not svc.calls                    # no key: nothing asked
        assert awards.seats_test("bad", svc.url) == (False, "seats.aero refused the key.")
        config.put(conn, "seats_key", "sk")
        conn.commit()
        got = awards.seats_run(conn, api=svc.url, pause=0)
        assert got == {"destinations": 2, "rows": 4, "pages": 2}                                # the row that does not leave SIN is ignored
        lhr = conn.execute("SELECT * FROM award_prices WHERE dest = 'LHR' AND cabin = 'J'").fetchone()
        assert (lhr["saver"], lhr["advantage"], lhr["seen_dates"], lhr["next_date"]) == (103500, 180000, 2, D(2027, 2, 1))
        cdg = conn.execute("SELECT * FROM award_prices WHERE dest = 'CDG' AND cabin = 'J'").fetchone()
        assert cdg["seen_dates"] == 0 and cdg["next_date"] is None and cdg["advantage"] is None
        assert json.loads(config.load(conn, "")["seats_at"])["destinations"] == 2
        awards.fill(conn)                                                                       # the chart leaves a fresh scan alone
        assert conn.execute("SELECT seen_dates FROM award_prices WHERE dest = 'LHR' AND cabin = 'J'").fetchone()["seen_dates"] == 2
    finally:
        svc.close()


# ---------------------------------------------------------------- match-day rates ----------------------------------------------------------------
def match(day, status, home, away, us_home=True):
    return {"competition": {"code": "PL"}, "utcDate": f"{day.isoformat()}T15:00:00Z", "status": status,
            "homeTeam": {"id": 66 if us_home else 1}, "awayTeam": {"id": 1 if us_home else 66}, "score": {"fullTime": {"home": home, "away": away}}}


def test_results_are_read_by_the_uk_start_date():
    d = D(2026, 3, 1)
    got = fixtures.parse([match(d, "FINISHED", 2, 1), match(D(2026, 3, 8), "FINISHED", 0, 0), match(D(2026, 3, 15), "FINISHED", 3, 1, us_home=False),
                          match(D(2026, 3, 22), "TIMED", None, None), match(D(2026, 3, 29), "POSTPONED", None, None),
                          dict(match(D(2026, 4, 5), "FINISHED", 5, 0), competition={"code": "FAC"}),
                          dict(match(d, "FINISHED", 1, 0), utcDate="2026-07-04T23:30:00Z")], 66, "PL")
    assert got == {d: "win", D(2026, 3, 8): "draw", D(2026, 3, 15): "loss", D(2026, 3, 22): "pending", D(2026, 3, 29): "other",
                   D(2026, 7, 5): "win"}                                                        # 23:30 UTC in July is the next day in the UK
    keys, per_month = fixtures.declared()
    assert keys and all(k.startswith("football-data/") for k in keys.values()) and any(n == 1 for n in per_month.values())


def manu(conn):
    card = next(iter(fixtures.declared()[0]))
    conn.execute("UPDATE people SET held_cards = %s WHERE id = 'alice'", ([card],))
    conn.commit()
    return card, fixtures.declared()[0][card]


def test_a_win_day_pays_the_win_rate_and_only_one_draw_day_a_month(pg):
    conn, ids = pg
    card, key = manu(conn)
    days = {"win": D(2026, 3, 1), "draw": D(2026, 3, 8), "draw2": D(2026, 3, 15), "loss": D(2026, 3, 22), "pending": D(2026, 3, 29)}
    for name, day in days.items():
        conn.execute("INSERT INTO match_results (fixtures, day, result) VALUES (%s,%s,%s)", (key, day, name.rstrip("2")))
    conn.commit()
    cache._cat.update(stamp=None, at=0.0, data=None)
    base, _ = replay.load(conn)
    c = base.cards[card]
    rate = {r["bucket"]: float(r["rate"]) for r in base.rules[card]}
    b = float(c["base_sgd"])
    line = lambda day, amt=100.0, i=[0]: (i.__setitem__(0, i[0] + 1), {"id": str(i[0]), "date": day, "card_id": card, "merchant": "SHOP", "amount": amt, "mcc": "5999"})[1]
    spend = [line(days["win"]), line(days["draw"]), line(days["draw"]), line(days["draw2"]), line(days["loss"]), line(days["pending"]),
             line(D(2026, 3, 3)), line(days["win"], 2500.0)]
    spend.sort(key=lambda x: (x["date"], int(x["id"])))
    res, tot, _ = replay.run(base, spend, [card], [card])
    got = {x["id"]: x for x in res}
    assert got["1"]["actual"] == pytest.approx(100 * rate["win"])
    assert got["2"]["actual"] == pytest.approx(100 * rate["draw"]) and got["3"]["actual"] == pytest.approx(100 * rate["draw"])   # same draw day
    assert got["4"]["actual"] == pytest.approx(100 * b)                                         # a second draw day that month: base
    assert got["5"]["actual"] == pytest.approx(100 * b) and got["7"]["actual"] == pytest.approx(100 * b)
    assert got["6"]["actual"] == pytest.approx(100 * b) and got["6"]["match_pending"] and not got["5"]["match_pending"]
    cap = float(next(r["cap"] for r in base.rules[card] if r["bucket"] == "win"))
    assert got["8"]["actual"] == pytest.approx((cap - 100) * rate["win"] + (2500 - (cap - 100)) * b)   # the cap is per match day
    # the Pay page's ranking says how today stands for that card
    for day, word in ((days["win"], "win today"), (days["pending"], "not in yet"), (D(2026, 3, 3), "no match today")):
        x = next(x for x in engine.rank(conn, {"mcc": "5999", "pay": "mobile", "fcy": False, "merchant": "", "today": day}, {card}) if x["card"]["id"] == card)
        assert word in x["extra"]["match"]
        assert x["rate"] == pytest.approx(rate["win"] if "win today" in word else b)


def test_match_results_are_fetched_only_with_a_key_and_not_too_often(pg):
    conn, ids = pg
    card, key = manu(conn)
    won = TODAY - datetime.timedelta(days=3)

    def route(path, headers):
        if headers.get("X-Auth-Token") != "fk":
            return 403, {}
        assert "/teams/66/matches" in path and "competitions=PL" in path
        return 200, {"matches": [match(won, "FINISHED", 1, 0)] if f"dateTo={(TODAY + datetime.timedelta(days=8)).isoformat()}" in path else []}
    svc = Service(route)
    try:
        assert fixtures.refresh(conn, TODAY, api=svc.url) is None and not svc.calls             # no key: nothing asked, base rate as before
        assert fixtures.test("bad", svc.url)[0] is False
        config.put(conn, "football_key", "fk")
        conn.commit()
        assert fixtures.test("fk", svc.url)[0] is True
        n_before = len(svc.calls)
        assert fixtures.refresh(conn, TODAY, api=svc.url) == 1
        assert len(svc.calls) - n_before >= 7                                                   # the first fetch reads a year back, 60 days at a time
        assert fixtures.load(conn) == {key: {won: "win"}}
        calls = len(svc.calls)
        assert fixtures.refresh(conn, TODAY, api=svc.url) is None and len(svc.calls) == calls   # done recently
        assert outside.refresh(conn) == {}                                                      # the scheduled runs: nothing due, nothing raised
    finally:
        svc.close()


# ---------------------------------------------------------------- the boost account ----------------------------------------------------------------
def test_the_boost_account_is_suggested_only_when_one_fits():
    cash = [{"id": "1", "name": "UOB One Account"}, {"id": "2", "name": "UOB Lady's Savings"}, {"id": "3", "name": "DBS Multiplier"}]
    assert wf.suggest_boost("UOB Lady's Savings balance", cash) == "2"
    assert wf.suggest_boost("UOB Lady's Savings balance", cash + [{"id": "4", "name": "UOB Lady's Savings (old)"}]) in ("2", None)
    assert wf.suggest_boost("UOB Lady's Savings balance", [cash[0], cash[2]]) is None
    assert wf.suggest_boost("", cash) is None


# ---------------------------------------------------------------- a feeder is optional ----------------------------------------------------------------
def test_a_quiet_feeder_stops_speaking_and_a_stopped_one_leaves_nothing_behind(pg, monkeypatch):
    conn, ids = pg
    a = ids[0]
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    bid = conn.execute("SELECT id FROM card_boosts LIMIT 1").fetchone()
    day = TODAY.isoformat()
    statements.feed(conn, "alice", {"lines": [{"id": "1", "date": day, "card": a, "merchant": "COLD STORAGE", "amount": 50}],
                                    "statement_days": {a: 12}, "wallet": {"total": 99000, "kf_account": 9000, "pools": []},
                                    "fees": [{"card": a, "date": day, "amount": 196.2, "text": "ANNUAL FEE"}]})
    assert points.feed_alive(conn, "alice") and points.wallet(conn, person(conn))[0]["total"] == 99000
    assert "last sent something" in setupweb.tokens_section(conn, person(conn)) and "Stop listening" in setupweb.tokens_section(conn, person(conn))
    # silent for more than three days: its wallet is no longer shown; what the person typed is
    conn.execute("UPDATE people SET feed_at = now() - interval '4 days' WHERE id = 'alice'")
    conn.commit()
    assert not points.feed_alive(conn, "alice") and points.wallet(conn, person(conn)) == (None, None)
    points.post(conn, {"programme": ["KF"], "balance": ["1000"], "date": [day]}, person(conn))
    assert points.wallet(conn, person(conn))[0]["total"] == 1000
    assert "no longer shown" in setupweb.tokens_section(conn, person(conn))
    # stop listening: its lines, wallet, fee list and closing day go, and its token with them
    kind, token = setupweb.post_token(conn, {"kind": ["feed"], "action": ["new"]}, person(conn))
    assert config.has_token(conn, "alice", "feed")
    assert setupweb.post_token(conn, {"kind": ["feed"], "action": ["stop"]}, person(conn)) is None
    me = person(conn)
    assert not config.has_token(conn, "alice", "feed") and not stored(conn, "feed-alice")
    assert me["wallet_enc"] is None and me["fees_enc"] is None and me["feed_at"] is None and a not in (me["statement_days"] or {})
    assert fees.events(conn, "alice", []) == {}
    assert points.wallet(conn, person(conn))[0]["total"] == 1000                                # what was typed stays


def test_a_closing_day_someone_typed_survives_stopping_the_feeder(pg):
    from cards import pipeline
    conn, ids = pg
    a, b = ids[:2]
    statements.feed(conn, "alice", {"lines": [], "statement_days": {a: 12, b: 20}})
    pipeline.set_close_day(conn, "alice", a, 15, "you")
    conn.commit()
    statements.stop_feed(conn, person(conn))
    conn.commit()
    assert person(conn)["statement_days"] == {a: 15}


def test_the_pages_render_with_the_new_sections(pg, monkeypatch):
    from cards import web
    conn, ids = pg
    card, key = manu(conn)
    conn.execute("UPDATE people SET admin = true WHERE id = 'alice'")
    conn.execute("INSERT INTO match_results (fixtures, day, result) VALUES (%s,%s,'pending')", (key, TODAY))
    conn.commit()
    cache._cat.update(stamp=None, at=0.0, data=None)
    me = person(conn)
    for fn in (web.settings, points.page, goals.page, setupweb.admin_page):
        title, body = fn(conn, {}, me)
        assert body
    assert "AwardWallet" in web.settings(conn, {}, me)[1]
    admin = setupweb.admin_page(conn, {}, me)[1]
    assert "football-data.org key" in admin and "seats.aero key" in admin
    assert setupweb.post_admin(conn, "/admin/services", {"football_key": ["fk"], "action": ["save"]}, me) is None
    assert config.where(conn, "")["football_key"] == "app" and "fk" not in setupweb.admin_page(conn, {}, me)[1]


def test_flights_is_its_own_section_and_cards_compare_side_by_side(pg):
    from cards import web
    conn, ids = pg
    cache._cat.update(stamp=None, at=0.0, data=None)
    me = person(conn)
    held = list(me["held_cards"])
    # the Flights tab, with the former goals page under it
    html = web.page("x", "y", "/goals", me)
    assert ">Flights<" in html and "href='/flights'" in html and web.section("/goals") == "flights" == web.section("/flights")
    title, body = goals.flights_page(conn, {"cabin": "Y", "trip": "return", "pax": "2"}, me)
    assert title == "Flights" and "Where can I go" in body
    if conn.execute("SELECT 1 FROM award_prices LIMIT 1").fetchone():
        assert "Save for it" in body and "places are within reach" in body
    # My cards and the compare page
    mine = web.catalogue(conn, {"mine": "1"}, me)[1]
    every = web.catalogue(conn, {}, me)[1]
    assert mine.count("name=c ") == len(held) and every.count("name=c ") >= len(held) and "form=cmp" in every
    assert "Pick two or more cards" in web.compare(conn, {}, me, [held[0]])[1]
    other = conn.execute("SELECT id FROM cards WHERE NOT (id = ANY(%s)) ORDER BY id LIMIT 1", (held,)).fetchone()["id"]
    side = web.compare(conn, {}, me, [held[0], other, "no-such-card"])[1]
    assert "Dining" in side and "Annual fee" in side and side.count(">remove<") == 2
    if len(held) > 1:
        assert web.compare(conn, {"mine": "1"}, me, [])[1].count(">remove<") == len(held)
