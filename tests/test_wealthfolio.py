"""The Wealthfolio connection: the client against a stand-in /mcp server (JSON and event-stream answers, pages, a
refused token, a token without a scope), which activities are spend, the window replace, no double count with a
feeder, the Settings section, and that nothing personal is printed."""
import base64
import datetime
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cards import config, crypto, ingest, setupweb, statements, wealthfolio as wf
from test_setup import Conn as SettingsConn

D = datetime.date
TODAY = D(2026, 11, 12)


# ---------------------------------------------------------------- a stand-in Wealthfolio ----------------------------------------------------------------
class FakeWealthfolio:
    """tokens: 'good' reads everything; 'accounts-only' has no activities:read; anything else is refused (401)."""

    def __init__(self, sse=False, page_size=None):
        self.accounts = [{"id": "acc-1", "name": "DBS Woman's World", "accountType": "CASH"},
                         {"id": "acc-2", "name": "UOB Preferred Platinum", "accountType": "CASH"},
                         {"id": "acc-3", "name": "Brokerage", "accountType": "SECURITIES"}]
        self.activities = {"acc-1": [], "acc-2": [], "acc-3": []}
        self.sse, self.page_size, self.calls, self.sessions = sse, page_size, [], set()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                token = (self.headers.get("Authorization") or "")[7:]
                if self.path != "/mcp":
                    return self._send(404, b"not here")
                if token not in ("good", "accounts-only"):
                    return self._send(401, b"unauthorized")
                method = body.get("method")
                if method == "notifications/initialized":
                    return self._send(202, b"")
                if method != "initialize" and self.headers.get("Mcp-Session-Id") not in fake.sessions:
                    return self._send(400, b"no session")
                result = fake.answer(method, body.get("params") or {}, token)
                msg = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), **result}).encode()
                headers = {}
                if method == "initialize":
                    sid = f"s{len(fake.sessions) + 1}"
                    fake.sessions.add(sid)
                    headers["Mcp-Session-Id"] = sid
                if fake.sse:
                    return self._send(200, b"event: message\ndata: " + msg + b"\n\n", "text/event-stream", headers)
                return self._send(200, msg, "application/json", headers)

            def _send(self, code, data, ctype="text/plain", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def answer(self, method, params, token):
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "wealthfolio"}}}
        tools = ["get_accounts"] + (["search_activities"] if token == "good" else [])
        if method == "tools/list":
            return {"result": {"tools": [{"name": t} for t in tools]}}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            self.calls.append((name, args))
            if name not in tools:
                return {"result": {"isError": True, "content": [{"type": "text", "text": "insufficient scope: activities:read"}]}}
            if name == "get_accounts":
                return {"result": {"structuredContent": {"accounts": self.accounts}}}
            rows = self.activities.get(args.get("accountId"), [])
            size = self.page_size or args.get("pageSize") or 200
            page = int(args.get("page") or 1)
            part = rows[(page - 1) * size:page * size]
            out = {"activities": part, "totalPages": max(1, -(-len(rows) // size))}
            # the tool's answer as text, the way a server without structured content sends it
            return {"result": {"content": [{"type": "text", "text": json.dumps(out)}]}}
        return {"error": {"code": -32601, "message": "no such method"}}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def act(i, date, kind, amount, text, **more):
    return {"id": f"a{i}", "date": date + "T00:00:00Z", "activityType": kind, "amount": amount, "comment": text, **more}


@pytest.fixture
def server():
    s = FakeWealthfolio()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("CARDS_", "LLM_", "VAULT_")):
            monkeypatch.delenv(var)
    monkeypatch.setenv("CARDS_LOCAL_KEY", base64.b64encode(os.urandom(32)).decode())
    crypto._keys.clear()
    crypto._raw.clear()
    yield
    crypto._keys.clear()
    crypto._raw.clear()


# ---------------------------------------------------------------- a stand-in database ----------------------------------------------------------------
class Conn(SettingsConn):
    """The settings stand-in plus the statements the import runs."""

    def __init__(self, people=None, shops=()):
        super().__init__(people)
        self.lines, self.stmts, self.shops, self.commits, self.boosts = {}, {}, list(shops), 0, []
        self.cards = [{"id": "dbs_ww", "name": "DBS Woman's World"}, {"id": "uob_pp", "name": "UOB Preferred Platinum"},
                      {"id": "citi_r", "name": "Citi Rewards"}]

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO statements (id, person, status)"):
            self.stmts.setdefault(args[0], {"person": args[1]})
        elif s.startswith("SELECT id, card_id, txn_date, data_enc FROM stmt_lines WHERE statement_id"):
            self.rows = [dict(v, id=k) for k, v in self.lines.items() if v["statement_id"] == args[0]]
        elif s.startswith("INSERT INTO stmt_lines"):
            lid, sid, person, card, date, blob = args
            self.lines[lid] = {"statement_id": sid, "person": person, "card_id": card, "txn_date": date, "data_enc": blob}
        elif s.startswith("DELETE FROM stmt_lines WHERE id = ANY"):
            for lid in args[0]:
                self.lines.pop(lid)
        elif s.startswith("SELECT count(*) n, min(txn_date) a, max(txn_date) b FROM stmt_lines"):
            ds = [v["txn_date"] for v in self.lines.values() if v["statement_id"] == args[0]]
            self.rows = [{"n": len(ds), "a": min(ds, default=None), "b": max(ds, default=None)}]
        elif s.startswith("UPDATE statements SET n_lines"):
            self.stmts[args[3]].update(n_lines=args[0])
        elif s.startswith("SELECT DISTINCT card_id FROM stmt_lines WHERE statement_id"):
            self.rows = [{"card_id": c} for c in {v["card_id"] for v in self.lines.values()
                                                 if v["statement_id"] == args[0] and args[1] <= v["txn_date"] <= args[2]}]
        elif s.startswith("SELECT id FROM stmt_lines WHERE statement_id = %s AND card_id = ANY"):
            self.rows = [{"id": k} for k, v in self.lines.items() if v["statement_id"] == args[0] and v["card_id"] in args[1]]
        elif s.startswith("SELECT id FROM stmt_lines WHERE statement_id = %s AND card_id = %s"):
            self.rows = [{"id": k} for k, v in self.lines.items() if v["statement_id"] == args[0] and v["card_id"] == args[1]]
        elif s.startswith("UPDATE statements SET n_lines = %s, period_from = %s, period_to = %s WHERE id"):
            self.stmts.setdefault(args[3], {}).update(n_lines=args[0])
        elif s.startswith("UPDATE people SET card_sources"):
            self.people[args[1]]["card_sources"] = args[0].obj
        elif s.startswith("SELECT statement_days, statement_days_src FROM people"):
            p = self.people[args[0]]
            self.rows = [{"statement_days": p.setdefault("statement_days", {}), "statement_days_src": p.setdefault("statement_days_src", {})}]
        elif s.startswith("UPDATE people SET statement_days = %s, statement_days_src"):
            self.people[args[2]].update(statement_days=args[0].obj, statement_days_src=args[1].obj)
        elif s.startswith("SELECT boost_amounts, boost_auto FROM people"):
            p = self.people[args[0]]
            self.rows = [{"boost_amounts": p.setdefault("boost_amounts", {}), "boost_auto": p.setdefault("boost_auto", {})}]
        elif s.startswith("UPDATE people SET boost_amounts = %s, boost_auto"):
            self.people[args[2]].update(boost_amounts=args[0].obj, boost_auto=args[1].obj)
        elif s.startswith("SELECT 1 FROM card_boosts WHERE id::text"):
            self.rows = [{"?column?": 1}] if args[0] in {str(b["id"]) for b in self.boosts} else []
        elif s.startswith("SELECT b.id, b.label, c.name card FROM card_boosts"):
            self.rows = list(self.boosts)
        elif s.startswith("SELECT DISTINCT card_id, to_char(txn_date, 'YYYY-MM') m FROM stmt_lines"):
            self.rows = [{"card_id": c, "m": m} for c, m in {(v["card_id"], v["txn_date"].strftime("%Y-%m")) for v in self.lines.values()
                                                                if v["statement_id"] != args[1] and v["card_id"] in args[2] and v["txn_date"] <= args[3]}]
        elif s.startswith("SELECT name, mccs, source, channel FROM merchants"):
            self.rows = list(self.shops)
        elif s.startswith("SELECT DISTINCT card_id FROM stmt_lines WHERE person = %s AND card_id = ANY"):
            self.rows = [{"card_id": c} for c in {v["card_id"] for v in self.lines.values() if v["person"] == args[0]
                                                  and v["card_id"] in args[1] and v["statement_id"] != args[2]}]
        elif s.startswith("SELECT c.id, c.name, c.bank_id FROM cards c WHERE c.id = ANY"):
            self.rows = [dict(c, bank_id=c["id"].split("_")[0].replace("uob", "uob")) for c in self.cards if c["id"] in args[0]]
        elif s.startswith("SELECT c.id, c.name FROM cards c WHERE c.id = ANY"):
            self.rows = [c for c in self.cards if c["id"] in args[0]]
        elif s.startswith("SELECT * FROM people ORDER BY id"):
            self.rows = sorted(self.people.values(), key=lambda p: p["id"])
        elif s.startswith("SELECT * FROM people WHERE id"):
            self.rows = [self.people[args[0]]] if args[0] in self.people else []
        else:
            return super().execute(sql, args)
        return self

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def stored(self, person="alice"):
        """The person's Wealthfolio lines, decrypted, oldest first."""
        sid = statements.wf_id(person)
        out = [dict(crypto.dec_json(self, v["data_enc"], f"stmt_lines.data_enc:{k}"), card=v["card_id"], date=v["txn_date"])
               for k, v in self.lines.items() if v["statement_id"] == sid]
        return sorted(out, key=lambda x: (x["date"], x["merchant"]))


def alice(**more):
    return {"id": "alice", "display": "Alice", "admin": False, "held_cards": ["dbs_ww", "uob_pp"], **more}


def connected(server, amap=None, **more):
    conn = Conn([alice()], shops=[{"name": "NTUC FairPrice", "mccs": ["5411"], "source": "wiki", "channel": "in_person"},
                                  {"name": "Shopee", "mccs": ["5262"], "source": "wiki", "channel": "online"},
                                  {"name": "Singapore Airlines", "mccs": ["3075"], "source": "wiki", "channel": "both"}])
    config.put(conn, "wf_url", server.url, "alice")
    config.put(conn, "wf_token", "good", "alice")
    config.put(conn, "wf_map", json.dumps(amap if amap is not None else {"acc-1": "dbs_ww", "acc-2": "uob_pp"}), "alice")
    config.put(conn, "wf_accounts", json.dumps([{"id": a["id"], "name": a["name"], "type": a["accountType"]} for a in server.accounts]), "alice")
    for k, v in more.items():
        config.put(conn, k, v, "alice")
    return conn


def run(conn, today=TODAY):
    return wf.run_person(conn, conn.people["alice"], today=today)


# ---------------------------------------------------------------- the connection test ----------------------------------------------------------------
def test_a_working_token_lists_the_accounts(server):
    ok, said, accounts = wf.test(server.url, "good")
    assert ok and "3 accounts" in said
    assert [a["name"] for a in accounts] == ["DBS Woman's World", "UOB Preferred Platinum", "Brokerage"]
    assert accounts[2]["type"] == "SECURITIES"


def test_the_address_may_be_given_with_or_without_mcp(server):
    assert wf.test(server.url + "/", "good")[0] and wf.test(server.url + "/mcp", "good")[0]
    assert wf.endpoint("https://wf.example") == "https://wf.example/mcp"
    with pytest.raises(wf.Problem):
        wf.endpoint("wf.example:8088")


def test_event_stream_answers_work_too():
    s = FakeWealthfolio(sse=True)
    try:
        s.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 12.5, "NTUC FAIRPRICE SINGAPORE SG")]
        ok, said, accounts = wf.test(s.url, "good")
        assert ok and len(accounts) == 3
        assert len(wf.Client(s.url, "good").activities("acc-1")) == 1
    finally:
        s.close()


def test_a_refused_token_says_so(server):
    ok, said, accounts = wf.test(server.url, "revoked")
    assert not ok and accounts is None and "refused the access token" in said


def test_a_missing_scope_is_named(server):
    ok, said, accounts = wf.test(server.url, "accounts-only")
    assert not ok and "activities:read" in said and "accounts:read" in said
    with pytest.raises(wf.Problem, match="lacks the scope activities:read"):
        wf.Client(server.url, "accounts-only").activities("acc-1")


def test_an_unreachable_or_wrong_address_says_so(server):
    port = server.httpd.server_address[1]
    server.close()
    ok, said, _ = wf.test(f"http://127.0.0.1:{port}", "good")
    assert not ok and "Could not reach Wealthfolio" in said
    other = FakeWealthfolio()
    try:
        ok, said, _ = wf.test(other.url + "/somewhere/else", "good")
        assert not ok and "not Wealthfolio's agent access" in said
    finally:
        other.close()


def test_every_page_of_an_account_is_read():
    s = FakeWealthfolio(page_size=2)
    try:
        s.activities["acc-1"] = [act(i, "2026-11-01", "WITHDRAWAL", 10 + i, f"SHOP {i}") for i in range(5)]
        got = wf.Client(s.url, "good").activities("acc-1")
        assert [a["id"] for a in got] == ["a0", "a1", "a2", "a3", "a4"]
        assert [c[1]["page"] for c in s.calls if c[0] == "search_activities"] == [1, 2, 3]
    finally:
        s.close()


# ---------------------------------------------------------------- activities -> lines ----------------------------------------------------------------
def test_purchases_refunds_payments_and_bank_charges():
    acts = [act(1, "2026-11-01", "WITHDRAWAL", 42.5, "NTUC FAIRPRICE SINGAPORE SG"),
            act(2, "2026-11-02", "CREDIT", 10.0, "SHOPEE SINGAPORE REFUND"),
            act(3, "2026-11-03", "TRANSFER_IN", 500.0, "PAYMENT - THANK YOU"),
            act(4, "2026-11-03", "DEPOSIT", 500.0, "GIRO PAYMENT"),
            act(5, "2026-11-04", "WITHDRAWAL", 3.2, "CCY CONVERSION FEE"),
            act(6, "2026-11-04", "WITHDRAWAL", 25.0, "LATE PAYMENT CHARGE"),
            act(7, "2026-11-05", "WITHDRAWAL", 196.2, "ANNUAL FEE"),
            act(8, "2026-08-01", "WITHDRAWAL", 9.0, "TOO OLD"),
            act(9, "2026-11-06", "WITHDRAWAL", -18.0, "NEGATIVE SIGN IN WEALTHFOLIO")]
    lines, counts = wf.lines_from(acts, "dbs_ww", D(2026, 9, 11), TODAY)
    assert [(ln["id"], ln["amount"]) for ln in lines] == [("a1", 42.5), ("a2", -10.0), ("a7", 196.2), ("a9", 18.0)]
    assert counts == {"payments and other": 2, "bank charges": 2}
    assert all(ln["card"] == "dbs_ww" and ln["est"] is False for ln in lines)


def test_online_foreign_and_currency_are_read_from_the_text():
    acts = [act(1, "2026-11-01", "WITHDRAWAL", 30, "AMAZON MKTP SG"),
            act(2, "2026-11-01", "WITHDRAWAL", 80, "RIVERSIDE HOTEL BANGKOK TH"),
            act(3, "2026-11-01", "WITHDRAWAL", 55, "SOME SHOP TOKYO | JPY 6,000.00"),
            act(4, "2026-11-01", "WITHDRAWAL", 12, "BANGKOK KITCHEN SINGAPORE SG"),
            act(5, "2026-11-01", "WITHDRAWAL", 20, "KOPITIAM")]
    by = {ln["id"]: ln for ln in wf.lines_from(acts, "c", D(2026, 10, 1), TODAY)[0]}
    assert by["a1"]["online"] is True and by["a1"]["channel"] == "online" and not by["a1"]["fcy"]
    assert by["a2"]["fcy"] and by["a2"]["currency"] == "THB" and by["a2"]["online"] is None
    assert by["a3"]["fcy"] and by["a3"]["currency"] == "JPY" and by["a3"]["merchant"] == "SOME SHOP TOKYO"
    assert not by["a4"]["fcy"] and by["a4"]["currency"] == "SGD"          # a Singapore shop with a foreign word in its name
    assert not by["a5"]["fcy"] and by["a5"]["online"] is None and by["a5"]["channel"] is None


# ---------------------------------------------------------------- the import ----------------------------------------------------------------
def test_an_import_stores_encrypted_lines_with_categories(server):
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 42.5, "NTUC FAIRPRICE SINGAPORE SG"),
                                  act(2, "2026-11-02", "WITHDRAWAL", 19.9, "SHOPEE SINGAPORE"),
                                  act(3, "2026-11-03", "WITHDRAWAL", 800, "SINGAPORE AIRLINES TICKET"),
                                  act(4, "2026-11-04", "WITHDRAWAL", 7, "UNHEARD OF SHOP")]
    server.activities["acc-2"] = [act(5, "2026-11-05", "CREDIT", 5, "NTUC FAIRPRICE REFUND")]
    server.activities["acc-3"] = [act(6, "2026-11-05", "WITHDRAWAL", 1000, "NOT A CARD ACCOUNT")]
    conn = connected(server)
    counts = run(conn)
    assert (counts["new"], counts["updated"], counts["removed"], counts["could not place"]) == (5, 0, 0, 1)
    got = conn.stored()
    assert [(x["merchant"], x["card"], x["mcc"], x["online"]) for x in got] == [
        ("NTUC FAIRPRICE SINGAPORE SG", "dbs_ww", "5411", None), ("SHOPEE SINGAPORE", "dbs_ww", "5262", True),
        ("SINGAPORE AIRLINES TICKET", "dbs_ww", "3075", True),       # an airline ticket is bought online
        ("UNHEARD OF SHOP", "dbs_ww", None, None), ("NTUC FAIRPRICE REFUND", "uob_pp", None, None)]
    assert got[-1]["amount"] == -5
    assert "NOT A CARD ACCOUNT" not in json.dumps(got, default=str)
    assert all(b"FAIRPRICE" not in v["data_enc"] and b"42.5" not in v["data_enc"] for v in conn.lines.values())
    assert {c[1]["accountId"] for c in server.calls if c[0] == "search_activities"} == {"acc-1", "acc-2"}
    last = wf.last(config.load(conn, "alice"))
    assert last["error"] is None and last["counts"]["new"] == 5


def test_a_second_run_writes_nothing_and_changes_follow(server):
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 42.5, "NTUC FAIRPRICE SINGAPORE SG"),
                                  act(2, "2026-11-02", "WITHDRAWAL", 19.9, "SHOPEE SINGAPORE | est"),
                                  act(3, "2026-11-03", "WITHDRAWAL", 7, "KOPITIAM")]
    conn = connected(server)
    run(conn)
    before = {k: v["data_enc"] for k, v in conn.lines.items()}
    again = run(conn)
    assert (again["new"], again["updated"], again["removed"]) == (0, 0, 0)
    assert {k: v["data_enc"] for k, v in conn.lines.items()} == before        # not even re-encrypted
    # upstream: the estimate becomes the final line (same activity, new amount), one line is deleted, one is added
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 42.5, "NTUC FAIRPRICE SINGAPORE SG"),
                                  act(2, "2026-11-04", "WITHDRAWAL", 20.35, "SHOPEE SINGAPORE"),
                                  act(4, "2026-11-05", "WITHDRAWAL", 3, "KOPITIAM")]
    third = run(conn)
    assert (third["new"], third["updated"], third["removed"]) == (1, 1, 1)
    got = conn.stored()
    assert [(x["merchant"], x["amount"], x["date"]) for x in got] == [
        ("NTUC FAIRPRICE SINGAPORE SG", 42.5, D(2026, 11, 1)), ("SHOPEE SINGAPORE", 20.35, D(2026, 11, 4)), ("KOPITIAM", 3, D(2026, 11, 5))]


def test_lines_older_than_the_window_stay(server):
    server.activities["acc-1"] = [act(1, "2026-09-20", "WITHDRAWAL", 10, "KOPITIAM")]
    conn = connected(server)
    run(conn, today=D(2026, 10, 1))
    server.activities["acc-1"] = []                      # Wealthfolio no longer lists it, but it is outside the window now
    later = run(conn, today=D(2026, 12, 31))
    assert later["removed"] == 0 and len(conn.stored()) == 1


def test_an_account_no_longer_matched_loses_its_lines(server):
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 10, "KOPITIAM")]
    server.activities["acc-2"] = [act(2, "2026-11-01", "WITHDRAWAL", 20, "KOPITIAM")]
    conn = connected(server)
    run(conn)
    config.put(conn, "wf_map", json.dumps({"acc-1": "dbs_ww"}), "alice")
    assert run(conn)["removed"] == 1
    assert [x["card"] for x in conn.stored()] == ["dbs_ww"]


def test_a_card_a_feeder_already_sends_is_left_alone(server):
    now = D.today()                                      # the Settings section looks at the real window
    server.activities["acc-1"] = [act(1, now.isoformat(), "WITHDRAWAL", 10, "KOPITIAM")]
    server.activities["acc-2"] = [act(2, now.isoformat(), "WITHDRAWAL", 20, "KOPITIAM")]
    conn = connected(server)
    run(conn, today=now)
    assert len(conn.stored()) == 2
    # a feeder starts sending the first card
    conn.lines["feedline"] = {"statement_id": statements.feed_id("alice"), "person": "alice", "card_id": "dbs_ww",
                              "txn_date": now, "data_enc": b""}
    before = len(server.calls)
    counts = run(conn, today=now)
    assert counts["cards left to the feeder"] == 1 and counts["removed"] == 0
    # the import's copy of that card stays stored (older months are history); it is the reader that lets one source
    # speak for a card and month (test_one_source_speaks_for_a_card_and_month)
    assert sorted(x["card"] for x in conn.stored()) == ["dbs_ww", "uob_pp"]
    assert [c[1]["accountId"] for c in server.calls[before:] if c[0] == "search_activities"] == ["acc-2"]
    page = setupweb.wealthfolio_section(conn, conn.people["alice"])
    assert "A feeder already sends the transactions of: DBS Woman&#x27;s World" in page


def test_cards_can_be_taken_over_from_a_feeder_that_stopped(server):
    now = D.today()
    server.activities["acc-1"] = [act(1, now.isoformat(), "WITHDRAWAL", 10, "KOPITIAM")]
    conn = connected(server, amap={"acc-1": "dbs_ww"})
    me = conn.people["alice"]
    fid = statements.feed_id("alice")
    conn.stmts[fid] = {"person": "alice"}
    for i, card in enumerate(["dbs_ww", "dbs_ww", "citi_r"]):
        conn.lines[f"feed{i}"] = {"statement_id": fid, "person": "alice", "card_id": card, "txn_date": now, "data_enc": b""}
    assert run(conn, today=now)["cards left to the feeder"] == 1 and conn.stored() == []
    assert "Take these cards over from the feeder" in setupweb.wealthfolio_section(conn, me)
    ok, said = setupweb.post_wealthfolio(conn, form(action="takeover"), me)
    assert ok and said.startswith("2 feeder lines removed. Imported from Wealthfolio: 1 new")
    assert [v["card_id"] for v in conn.lines.values() if v["statement_id"] == fid] == ["citi_r"]     # another card's feed stays
    assert [x["card"] for x in conn.stored()] == ["dbs_ww"]
    assert "Take these cards over" not in setupweb.wealthfolio_section(conn, me)


class Lines:
    """A stand-in for the reader's three queries."""

    def __init__(self, rows, sources=None):
        self.all, self.sources = rows, sources or {}

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self.rows = ([{"card_sources": self.sources}] if s.startswith("SELECT card_sources FROM people") else
                     [{"settings_enc": None}] if s.startswith("SELECT settings_enc FROM people") else self.all)
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def read(rows, sources=None):
    real, crypto.dec_json = crypto.dec_json, lambda conn, blob, aad: {"merchant": "X", "amount": 1}
    try:
        return [ln["id"] for ln in statements.load_lines(Lines(rows, sources), "alice")]
    finally:
        crypto.dec_json = real


mk = lambda i, sid, card, date: {"id": i, "statement_id": sid, "card_id": card, "txn_date": date, "data_enc": b""}


def test_wealthfolio_lines_hide_statement_lines_of_the_same_card_and_month():
    rows = [mk("w1", "wf-alice", "dbs_ww", D(2026, 11, 3)), mk("s1", "upload1", "dbs_ww", D(2026, 11, 3)),
            mk("s2", "upload1", "dbs_ww", D(2026, 10, 3)), mk("s3", "upload1", "uob_pp", D(2026, 11, 3)),
            mk("a1", "alerts-alice", "dbs_ww", D(2026, 11, 4))]
    assert read(rows) == ["w1", "s2", "s3"]


def test_one_source_speaks_for_a_card_and_month():
    rows = [mk("f1", "feed-alice", "dbs_ww", D(2026, 11, 3)), mk("w1", "wf-alice", "dbs_ww", D(2026, 11, 3)),
            mk("w0", "wf-alice", "dbs_ww", D(2026, 6, 3)),      # history: a month the feeder never sent
            mk("s1", "upload1", "dbs_ww", D(2026, 11, 5)), mk("w2", "wf-alice", "uob_pp", D(2026, 11, 4))]
    # nothing chosen: where both automatic sources hold a card's month, the feeder's lines count; history stays
    assert sorted(read(rows)) == ["f1", "w0", "w2"]
    # the person gave the card to Wealthfolio: the feeder's lines of it are not read
    assert sorted(read(rows, {"dbs_ww": "wealthfolio"})) == ["w0", "w1", "w2"]
    # ... or to mail and statement files: neither automatic source is read for it
    assert sorted(read(rows, {"dbs_ww": "mail"})) == ["s1", "w2"]


def test_what_stops_an_import_is_said_and_kept(server):
    conn = connected(server, amap={})
    with pytest.raises(wf.Problem, match="No Wealthfolio account is matched"):
        run(conn)
    assert "matched to a card" in wf.last(config.load(conn, "alice"))["error"]
    conn = connected(server)
    config.put(conn, "wf_token", "revoked", "alice")
    with pytest.raises(wf.Problem, match="refused the access token"):
        run(conn)
    page = setupweb.wealthfolio_section(conn, conn.people["alice"])
    assert "did not run" in page and "refused the access token" in page


def test_a_card_no_longer_held_is_not_imported(server):
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 10, "KOPITIAM")]
    conn = connected(server, amap={"acc-1": "citi_r"})
    with pytest.raises(wf.Problem):
        run(conn)
    assert conn.stored() == []


def test_ingest_runs_the_import_only_when_switched_on_and_never_raises(server):
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 10, "KOPITIAM")]
    conn = connected(server)
    assert ingest.run_wealthfolio(conn, "alice") == {}                       # connected but not switched on
    config.put(conn, "wf_on", "1", "alice")
    config.put(conn, "wf_days", "400", "alice")
    assert ingest.run_wealthfolio(conn, "alice") == {"wealthfolio lines new": 1}
    config.put(conn, "wf_token", "revoked", "alice")
    assert ingest.run_wealthfolio(conn, "alice") == {"wealthfolio import failed": 1}


def test_nothing_personal_is_printed_or_kept_in_the_run_note(server, capsys, monkeypatch):
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 4321.09, "VERY PRIVATE CLINIC")]
    conn = connected(server, wf_on="1", wf_days="400")

    class DB:
        def connect(self):
            return self

        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("cards.db.connect", DB().connect)
    assert wf.run() == 0
    out = capsys.readouterr().out
    assert "wealthfolio: 1 imported, 0 failed" in out and "new 1" in out
    note = conn.settings[("alice", "wf_last")]["value"]
    for secret in ("PRIVATE", "CLINIC", "4321", "good"):
        assert secret not in out and secret not in note


# ---------------------------------------------------------------- the Settings section ----------------------------------------------------------------
def form(**kw):
    return {k: [v] for k, v in kw.items()}


def test_connect_test_match_and_import_from_the_settings_page(server):
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 10, "KOPITIAM")]
    conn = Conn([alice()])
    me = conn.people["alice"]
    first = setupweb.wealthfolio_section(conn, me)
    assert "Not connected" in first and "accounts:read" in first and "activities:read" in first and "<details open>" in first
    ok, said = setupweb.post_wealthfolio(conn, form(action="test", wf_url=server.url, wf_token="good"), me)
    assert ok and "3 accounts" in said
    page = setupweb.wealthfolio_section(conn, me, (ok, said))
    assert "good" not in page.replace("good points", "") and ">set<" in page          # the token is never shown again
    assert "Brokerage" in page and "not a card" in page and "suggested from the name" in page
    # the two card accounts are suggested by name; the brokerage account is not
    assert page.count("selected") == 2
    assert setupweb.post_wealthfolio(conn, form(action="map", acct_0="acc-1", card_0="dbs_ww", acct_1="acc-2", acct_2="acc-3",
                                                wf_days="400", wf_on="1"), me) is None
    cfg = config.load(conn, "alice")
    assert wf.mapping(cfg) == {"acc-1": "dbs_ww"} and cfg["wf_on"] == "1" and cfg["wf_days"] == "400"
    ok, said = setupweb.post_wealthfolio(conn, form(action="import", acct_0="acc-1", card_0="dbs_ww", wf_days="400", wf_on="1"), me)
    assert ok and said == ("Imported: 1 new, 0 updated, 0 removed, 1 without a category; 1 held card(s) without a Wealthfolio "
                           "account (match them below).")
    assert "Last import" in setupweb.wealthfolio_section(conn, me)


def test_the_settings_page_refuses_what_makes_no_sense(server):
    conn = connected(server)
    me = conn.people["alice"]
    bad = setupweb.post_wealthfolio(conn, form(action="map", acct_0="acc-1", card_0="dbs_ww", acct_1="acc-2", card_1="dbs_ww"), me)
    assert bad == (False, "Not saved: two accounts were given the same card. One card is one account.")
    assert setupweb.post_wealthfolio(conn, form(action="map", acct_0="acc-1", card_0="dbs_ww", wf_days="3"), me)[0] is False
    assert setupweb.post_wealthfolio(conn, form(action="test", wf_url="wealthfolio:8088", wf_token="x"), me)[0] is False
    # a card that is not held cannot be chosen
    setupweb.post_wealthfolio(conn, form(action="map", acct_0="acc-1", card_0="citi_r"), me)
    assert wf.mapping(config.load(conn, "alice")) == {}


def test_disconnect_forgets_the_token(server):
    conn = connected(server, wf_on="1")
    ok, said = setupweb.post_wealthfolio(conn, form(action="disconnect"), conn.people["alice"])
    cfg = config.load(conn, "alice")
    assert ok and cfg["wf_token"] == "" and cfg["wf_url"] == "" and cfg["wf_on"] == "0"
    assert conn.settings[("alice", "wf_token")]["secret"] is None


# ---------------------------------------------------------------- the real field shapes, the savings balance, history ----------------------------------------------------------------
def note(i, date, kind, amount, notes, **more):
    """An activity the way a real Wealthfolio sends it: the text in `notes`, a time on the date, a running balance."""
    return {"id": f"n{i}", "date": date + "T00:00:00+00:00", "activityType": kind, "amount": amount, "notes": notes,
            "currency": "SGD", "status": "POSTED", **more}


def test_the_note_is_the_merchant_and_a_remark_after_the_bar_is_understood():
    acts = [note(1, "2026-11-02", "WITHDRAWAL", 80, "TRAVELSHOP.COM HARBOUR INTERNET HK | alert (the statement will confirm it)"),
            note(2, "2026-10-20", "WITHDRAWAL", 12, "BOOK NOOK CLEMENTI A | Some Bank statement 2026-10"),
            note(3, "2026-10-28", "WITHDRAWAL", 123.45, "CCY CONVERSION FEE SGD 123.45"),
            note(4, "2026-10-29", "TRANSFER_IN", 900, "PAYMENT VIA VISA DIRECT"),
            note(5, "2026-10-30", "CREDIT", 5, "SHOPEE SINGAPORE")]
    lines, counts = wf.lines_from(acts, "dbs_ww", D(2026, 9, 1), D(2026, 11, 30))
    by = {ln["id"]: ln for ln in lines}
    assert set(by) == {"n1", "n2", "n5"} and counts == {"payments and other": 1, "bank charges": 1}
    assert by["n1"]["merchant"] == "TRAVELSHOP.COM HARBOUR INTERNET HK" and by["n1"]["est"] and by["n1"]["online"] and by["n1"]["fcy"]
    assert by["n2"]["merchant"] == "BOOK NOOK CLEMENTI A" and not by["n2"]["est"] and by["n2"]["tag"].endswith("statement 2026-10")
    assert by["n5"]["amount"] == -5
    # the statement remark gives a first estimate of the card's closing day
    more = [dict(by["n2"], date=D(2026, 10, 24)), dict(by["n2"], date=D(2026, 10, 3))]
    assert wf.close_days(more) == {"dbs_ww": 24}


def test_only_card_accounts_are_offered_a_match_when_wealthfolio_types_them():
    accounts = [{"id": "c1", "name": "DBS Woman's World", "type": "CREDIT_CARD"}, {"id": "s1", "name": "DBS Woman's World", "type": "CASH"}]
    cards = [{"id": "dbs_ww", "name": "DBS Woman's World"}]
    assert wf.suggest(accounts, cards) == {"c1": "dbs_ww"}
    assert set(wf.suggest([dict(a, type="") for a in accounts], cards)) == {"c1", "s1"}    # no types: every account is a candidate


def test_the_month_average_comes_from_running_balances():
    rows = [(D(2026, 10, 20), 1000.0), (D(2026, 11, 3), 4000.0), (D(2026, 11, 3), 3000.0), (D(2026, 11, 9), 6000.0)]
    # 1-2 Nov carry October's 1000; 3-8 Nov the day's last row, 3000; 9-10 Nov 6000
    assert wf.month_average(rows, None, D(2026, 11, 10)) == round((2 * 1000 + 6 * 3000 + 2 * 6000) / 10, 2)
    assert wf.month_average(rows, 7000.0, D(2026, 11, 10)) == round((2 * 1000 + 6 * 3000 + 6000 + 7000) / 10, 2)
    assert wf.month_average([], None, D(2026, 11, 10)) is None


def test_the_savings_balance_is_read_without_another_scope(server):
    server.accounts.append({"id": "sav", "name": "Savings", "accountType": "CASH"})
    server.activities["sav"] = [note(1, "2026-10-01", "DEPOSIT", 500, "salary", cashBalance=2000),
                                note(2, "2026-11-04", "DEPOSIT", 500, "salary", cashBalance=5000)]
    server.activities["acc-1"] = [act(1, "2026-11-03", "WITHDRAWAL", 10, "KOPITIAM")]
    conn = connected(server, wf_boost=json.dumps({"account": "sav", "boost": "7"}))
    conn.boosts = [{"id": 7, "label": "Savings balance", "card": "DBS Woman's World"}]
    counts = run(conn)
    me = conn.people["alice"]
    assert counts["savings balance read"] == 1 and counts["new"] == 1
    days = (TODAY - TODAY.replace(day=1)).days + 1
    assert me["boost_amounts"]["7"] == round((3 * 2000 + (days - 3) * 5000) / days, 2) and me["boost_auto"]["7"]["source"] == "wealthfolio"
    # an account without balances changes nothing and stops nothing
    conn2 = connected(server, wf_boost=json.dumps({"account": "acc-2", "boost": "7"}))
    conn2.boosts = conn.boosts
    assert run(conn2)["savings balance not read"] == 1


def old(i, date, amount, text):
    return act(i, date, "WITHDRAWAL", amount, text)


def test_history_is_read_a_page_and_a_month_at_a_time_and_can_be_run_again():
    s = FakeWealthfolio(page_size=2)
    try:
        s.activities["acc-1"] = [old(1, "2025-01-10", 10, "KOPITIAM"), old(2, "2025-01-20", 11, "KOPITIAM"), old(3, "2025-03-05", 12, "NTUC FAIRPRICE"),
                                 old(4, "2026-02-01", 13, "KOPITIAM"), old(5, TODAY.isoformat(), 99, "KOPITIAM")]
        conn = connected(s, amap={"acc-1": "dbs_ww"})
        me = conn.people["alice"]
        got = wf.history(conn, me, today=TODAY)
        assert got["new"] == 4 and got["months read"] == 3 and got["oldest"] == "2025-01-10"
        assert len([c for c in s.calls if c[0] == "search_activities"]) == 3            # 5 activities, 2 to a page
        assert conn.commits >= 3 + 2                                                    # each month in its own commit
        assert [x["amount"] for x in conn.stored()] == [10, 11, 12, 13]                 # today's purchase is the window's
        state = wf.history_state(config.load(conn, "alice"))
        assert state["state"] == "done" and state["oldest"] == "2025-01-10"
        # again: the same lines, nothing new, nothing twice
        again = wf.history(conn, me, today=TODAY)
        assert again["new"] == 0 and again["updated"] == 0 and len(conn.stored()) == 4
        # from a date
        conn3 = connected(s, amap={"acc-1": "dbs_ww"})
        assert wf.history(conn3, conn3.people["alice"], since=D(2025, 3, 1), today=TODAY)["new"] == 2
    finally:
        s.close()


def test_the_regular_import_never_removes_a_line_for_being_older_than_the_window(server):
    server.activities["acc-1"] = [old(1, "2025-01-10", 10, "KOPITIAM"), old(2, TODAY.isoformat(), 20, "KOPITIAM")]
    conn = connected(server, amap={"acc-1": "dbs_ww"})
    wf.history(conn, conn.people["alice"], today=TODAY)
    assert run(conn)["new"] == 1 and len(conn.stored()) == 2
    # the window's purchase goes upstream: it is removed; the old one is not Wealthfolio's to forget by age
    server.activities["acc-1"] = [old(1, "2025-01-10", 10, "KOPITIAM")]
    assert run(conn)["removed"] == 1 and [x["amount"] for x in conn.stored()] == [10]
    # a shorter window later changes nothing either
    config.put(conn, "wf_days", "7", "alice")
    assert run(conn)["removed"] == 0 and len(conn.stored()) == 1


def test_history_fills_only_months_no_other_source_has_and_says_which_it_left(server):
    server.activities["acc-1"] = [old(1, "2025-01-10", 10, "KOPITIAM"), old(2, "2025-02-10", 11, "KOPITIAM")]
    server.activities["acc-2"] = [old(3, "2025-01-11", 12, "KOPITIAM")]
    conn = connected(server)
    # a statement already holds January of the first card
    conn.lines["s1"] = {"statement_id": "upload1", "person": "alice", "card_id": "dbs_ww", "txn_date": D(2025, 1, 5), "data_enc": b""}
    got = wf.history(conn, conn.people["alice"], today=TODAY)
    assert got["months left to another source"] == 1 and got["skipped"] == ["dbs_ww 2025-01"] and got["new"] == 2
    assert sorted((x["card"], x["date"].strftime("%Y-%m")) for x in conn.stored()) == [("dbs_ww", "2025-02"), ("uob_pp", "2025-01")]
    said = setupweb._hist_said(wf.history_state(config.load(conn, "alice")))
    assert "dbs_ww 2025-01" in said and "oldest purchase found is dated 2025-01-10" in said
    # a card the person gave to another source is skipped whole
    conn2 = connected(server)
    conn2.people["alice"]["card_sources"] = {"uob_pp": "mail"}
    got2 = wf.history(conn2, conn2.people["alice"], today=TODAY)
    assert got2["cards left to another source"] == 1 and {x["card"] for x in conn2.stored()} == {"dbs_ww"}


def test_a_card_given_to_another_source_is_not_imported_and_one_given_to_wealthfolio_is(server):
    now = D.today()
    server.activities["acc-1"] = [act(1, now.isoformat(), "WITHDRAWAL", 10, "KOPITIAM")]
    server.activities["acc-2"] = [act(2, now.isoformat(), "WITHDRAWAL", 20, "KOPITIAM")]
    conn = connected(server)
    conn.people["alice"]["card_sources"] = {"uob_pp": "mail", "dbs_ww": "wealthfolio"}
    conn.lines["feedline"] = {"statement_id": statements.feed_id("alice"), "person": "alice", "card_id": "dbs_ww", "txn_date": now, "data_enc": b""}
    counts = run(conn, today=now)
    assert counts["cards left to another source"] == 1 and counts["cards left to the feeder"] == 0
    assert [x["card"] for x in conn.stored()] == ["dbs_ww"]


def test_a_card_added_later_is_matched_to_its_new_account_by_itself(server):
    """Citi Rewards added in the app after Wealthfolio was set up, with an account of its own there: the next import
    lists the accounts again, matches it by name and brings in its purchases, earlier ones too."""
    server.activities["acc-1"] = [act(1, "2026-11-01", "WITHDRAWAL", 10, "KOPITIAM")]
    conn = connected(server, amap={"acc-1": "dbs_ww"})
    # UOB Preferred comes from its statements, not Wealthfolio (where an account is named after it too)
    conn.lines["stmt-1"] = {"statement_id": "upload-1", "person": "alice", "card_id": "uob_pp", "section": "x",
                            "txn_date": D(2026, 11, 2), "data_enc": b""}
    run(conn)
    server.accounts.append({"id": "acc-9", "name": "Citi Rewards Card", "accountType": "CASH"})
    server.activities["acc-9"] = [act(9, "2026-11-03", "WITHDRAWAL", 42.5, "SOME SHOP")]
    conn.people["alice"]["held_cards"] = ["dbs_ww", "uob_pp", "citi_r"]
    counts = run(conn)
    assert counts["accounts matched to a new card"] == 1 and counts["new"] == 1
    cfg = config.load(conn, "alice")
    assert wf.mapping(cfg) == {"acc-1": "dbs_ww", "acc-9": "citi_r"}
    assert "Citi Rewards Card" in cfg["wf_accounts"]                                   # listed without "Save and test"
    assert ("citi_r", 42.5) in [(x["card"], x["amount"]) for x in conn.stored()]
    assert "accounts matched to a new card" not in run(conn)                           # once
    # UOB Preferred has transactions already: not a new card, its Wealthfolio account is left alone
    assert "acc-2" not in wf.mapping(config.load(conn, "alice"))


def test_what_the_person_said_is_not_a_card_stays_so(server):
    conn = connected(server, amap={"acc-1": "dbs_ww"})
    conn.people["alice"]["held_cards"] = ["dbs_ww", "uob_pp", "citi_r"]
    form = lambda **kv: {k: [v] for k, v in kv.items()}
    assert setupweb.post_wealthfolio(conn, form(action="map", acct_0="acc-1", card_0="dbs_ww", acct_1="acc-2", acct_2="acc-3",
                                                wf_days="62"), conn.people["alice"]) is None
    server.accounts[1]["name"] = "Citi Rewards"                                       # renamed to look like a held card
    config.put(conn, "wf_on", "1", "alice")
    run(conn)
    assert wf.mapping(config.load(conn, "alice")) == {"acc-1": "dbs_ww"}
    # a card added after that "not a card" answer can still be matched to it
    assert wf.new_matches([{"id": "acc-2", "name": "Citi Rewards", "type": "CASH"}], [{"id": "citi_r", "name": "Citi Rewards"}],
                          {"acc-1": "dbs_ww"}, {"acc-2": ["dbs_ww", "uob_pp"]}) == {"acc-2": "citi_r"}
    # nobody matched anything yet: nothing is matched by itself
    assert wf.new_matches([{"id": "acc-2", "name": "Citi Rewards", "type": "CASH"}], [{"id": "citi_r", "name": "Citi Rewards"}], {}) == {}



def test_a_new_card_is_matched_by_its_last_four_digits_or_its_bank():
    citi = {"id": "citi_r", "name": "Citi Rewards", "bank_id": "citi"}
    ww = {"id": "dbs_ww", "name": "DBS Woman's World", "bank_id": "dbs"}
    done = {"acc-1": "dbs_ww"}
    acct = lambda i, name, t="CASH": {"id": i, "name": name, "type": t}
    # the account carries the last 4 digits saved on Settings
    assert wf.new_matches([acct("a", "Credit card 4321")], [ww, citi], done, last4={"4321": "citi_r"}) == {"a": "citi_r"}
    # named only after the bank: the one new card of that bank
    assert wf.new_matches([acct("a", "Citibank Card")], [ww, citi], done) == {"a": "citi_r"}
    assert wf.new_matches([acct("a", "CITI")], [ww, citi], done) == {"a": "citi_r"}
    # two accounts name the bank, or two new cards of it: not sure, left to the person
    assert wf.new_matches([acct("a", "Citibank Card"), acct("b", "Citi Old")], [ww, citi], done) == {}
    pm = {"id": "citi_pm", "name": "Citi PremierMiles", "bank_id": "citi"}
    assert wf.new_matches([acct("a", "Citibank Card")], [ww, citi, pm], done) == {}
    # Wealthfolio says which accounts are cards: a savings account named after the bank is not one
    assert wf.new_matches([acct("a", "Citibank Savings", "CASH"), acct("c", "Other", "CREDIT_CARD")], [ww, citi], done) == {}
    # a card with lines already (not a new card) is left alone; alert previews do not count (has_lines leaves them out)
    assert wf.new_matches([acct("a", "Citibank Card")], [ww, citi], done, has_lines={"citi_r"}) == {}
