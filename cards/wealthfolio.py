"""Card transactions from Wealthfolio (https://wealthfolio.app), for people who already keep their card accounts there.

Each person connects their own Wealthfolio on the Settings page: its address and a personal access token made in
Wealthfolio (Settings, Agent access; scopes accounts:read and activities:read). The token is stored encrypted and is
only used to read. They then say which Wealthfolio account is which card.

The import reads the activities of those accounts for a rolling window (62 days unless changed) and replaces this
source's lines in that window with what it finds, so a line corrected or deleted in Wealthfolio is corrected or gone
here on the next run:

  WITHDRAWAL            a purchase
  CREDIT                a refund or rebate (a negative line: gives cap room back)
  anything else         not spend (card payments arrive as transfers or deposits), left out
  bank charges          interest, late and finance charges, conversion fees: left out. Annual fees are kept: the fee
                        tracker reads them.

What the bank's line does not say is worked out from its text: an online shop, a merchant abroad, the currency. The
merchant category comes from the app's shop list, then from the model when one is set up. Nothing is ever written to
Wealthfolio.

Lines live under their own statement (wf-<person>). One source speaks for a card (pipeline.py): a card a feeder
(POST /api/feed) already sends in the window is left alone until the person gives it to Wealthfolio on the Settings
page; an uploaded statement or an alert for the same card and month is hidden behind these lines.

The window is only what is kept in step. A line older than it is never removed for being old, and the history
import (history(), once, from the Settings page or `python -m cards wealthfolio --person ID --since DATE`) reads
everything older, a month at a time.

The savings boost: a person may name one Wealthfolio cash account whose average balance sets a card's boosted rate;
the import then works out that account's average balance for the month from the running balance on its activities.

Logs and the stored run note hold counts only, never a merchant or an amount."""
import datetime
import hashlib
import json
import re
import urllib.error
import urllib.request

from collections import defaultdict

from psycopg.types.json import Jsonb

from . import __version__, catalogue, config, crypto, pipeline, statements

SCOPES = {"get_accounts": "accounts:read", "search_activities": "activities:read"}
DAYS = 62
FIELDS = pipeline.FIELDS
ONLINE, FOREIGN_PLACE, FCY, BANK_CHARGE = pipeline.ONLINE, pipeline.FOREIGN_PLACE, pipeline.FCY, pipeline.BANK_CHARGE


# ---------------------------------------------------------------- what Wealthfolio's answers look like ----------------------------------------------------------------
# Everything this module assumes about Wealthfolio's field names is in the next four functions.
#
# Seen on a real self-hosted Wealthfolio (November 2026), read with a token that has accounts:read + activities:read:
#   tools       get_accounts, search_activities, get_cash_balances (and others the app does not use). There is no
#               valuation history on such a token, so balances are taken from the activities themselves.
#   account     id, name, accountType (CREDIT_CARD, CASH, SECURITIES, CRYPTOCURRENCY), currency, isActive
#   activity    id (uuid), date ("2026-11-02T00:00:00+00:00"), activityType, amount (always positive: the type gives
#               the direction), currency, notes (free text), cashBalance (the account's running balance after the
#               row), status, sourceSystem and more.
#   on a card   WITHDRAWAL = a purchase (also the bank's own charges, told apart by their text), CREDIT = a refund,
#               TRANSFER_IN = a payment to the card.
# The merchant is the note. A note may carry a remark after " | " (whoever fills Wealthfolio decides): the part before
# it is the merchant; there may be several remarks, each after its own " | ". These are understood, as a convention
# anyone's importer may follow:
#   "alert ..."                  the line is not confirmed by a statement yet (kept as an estimate)
#   "... statement YYYY-MM"      the line was on that month's statement (tells the card's statement period)
#   "USD 20.00"                  a remark that is exactly a currency code and an amount: the statement printed this
#                                foreign amount -> a purchase in that currency
#   "alert, estimated from USD 20.00 ..."   an alert in a foreign currency, its S$ amount estimated until the statement
# A currency named in the note always wins over what the shop's text or place suggests.
SPEND, REFUND = "WITHDRAWAL", "CREDIT"     # every other activity type on a card account is left out
CARD_TYPE, CASH_TYPE = "CREDIT_CARD", "CASH"
TAG_STATEMENT = re.compile(r"\bstatement\b.*?\b(20\d\d-\d\d)\b", re.I)
TAG_FOREIGN = re.compile(r"^([A-Z]{3}) ([\d,]+\.\d{2})$")                               # the whole remark
TAG_ESTIMATED = re.compile(r"\bestimated from ([A-Z]{3}) ([\d,]+\.\d{2})\b")


def fields(a):
    """One activity -> {"id", "date", "kind", "amount", "text", "tag", "balance", "alert", "currency", "fcy_amount"}."""
    note = pipeline.squash(a.get("notes") or a.get("comment") or "")
    tags = [t.strip() for t in note.split(" | ")[1:] if t.strip()]
    tag = " | ".join(tags)
    cur = famt = None
    for t in tags:
        m = TAG_FOREIGN.match(t) or (TAG_ESTIMATED.search(t) if t.lower().startswith("alert") else None)
        if m and m.group(1) != "SGD":
            cur, famt = m.group(1), float(m.group(2).replace(",", ""))
            break
    try:
        bal = float(a["cashBalance"]) if a.get("cashBalance") is not None else None
    except (TypeError, ValueError):
        bal = None
    return {"id": a.get("id"), "date": a.get("date") or a.get("activityDate"), "kind": a.get("activityType"),
            "amount": a.get("amount"), "text": note, "tag": tag, "balance": bal,   # the clean-up takes the merchant from the note
            "alert": any(t.lower().startswith("alert") for t in tags), "currency": cur, "fcy_amount": famt}


def page_of(r):
    """One search_activities answer -> (activities, number of pages)."""
    if not isinstance(r, dict):
        return [], 1
    try:
        last = int(r.get("totalPages") or 1)
    except (TypeError, ValueError):
        last = 1
    return r.get("activities") or [], last


def account_of(a):
    """One account -> {"id", "name", "type"}."""
    return {"id": str(a.get("id")), "name": str(a.get("name") or "")[:80], "type": str(a.get("accountType") or "")[:24]}


def balance_now(got, account_id):
    """get_cash_balances' answer -> this account's balance today, or None when the answer is not in a shape known
    here (the last activity's running balance is used then)."""
    rows = got.get("balances") or got.get("accounts") or [] if isinstance(got, dict) else got if isinstance(got, list) else []
    for x in rows:
        if isinstance(x, dict) and str(x.get("accountId") or x.get("id") or "") == str(account_id):
            for k in ("balance", "cashBalance", "amount", "total"):
                try:
                    if x.get(k) is not None:
                        return float(x[k])
                except (TypeError, ValueError):
                    pass
    return None


class Problem(Exception):
    """Something the person can act on; the text is shown to them as it is."""


def endpoint(address):
    """The /mcp address for what the person typed (the address they open Wealthfolio at)."""
    a = (address or "").strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", a, re.I):
        raise Problem("The address must start with http:// or https://, for example http://wealthfolio:8088.")
    return a if a.lower().endswith("/mcp") else a + "/mcp"


class Client:
    """Wealthfolio's /mcp endpoint: JSON-RPC over HTTP; an answer may come as JSON or as a server-sent event."""

    def __init__(self, address, token, timeout=60):
        self.url, self.token, self.timeout = endpoint(address), token, timeout
        self.session, self._id = None, 0
        if not token:
            raise Problem("No access token is stored.")
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "sg-miles-advisor", "version": __version__}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, msg):
        h = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self.session = r.headers.get("Mcp-Session-Id") or self.session
                raw = r.read().decode("utf-8", "replace")
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as ex:
            if ex.code == 401:
                raise Problem("Wealthfolio refused the access token. It may have been revoked or mistyped: make a new one "
                              "in Wealthfolio (Settings, Agent access) and save it here.") from None
            if ex.code == 403:
                raise Problem("Wealthfolio accepted the token but did not allow the request. The token needs the scopes "
                              "accounts:read and activities:read.") from None
            if ex.code in (404, 405):
                raise Problem("Something answers at that address, but not Wealthfolio's agent access (/mcp). Check the "
                              "address, and that this Wealthfolio is the self-hosted web version with Agent access.") from None
            raise Problem(f"Wealthfolio answered with an error (HTTP {ex.code}).") from None
        except (urllib.error.URLError, OSError, ValueError) as ex:
            why = getattr(ex, "reason", None)
            raise Problem(f"Could not reach Wealthfolio at that address ({type(why or ex).__name__}). Check the address and "
                          "that this machine can reach it.") from None
        if not raw.strip():
            return None
        try:
            if "text/event-stream" in ctype:
                for line in raw.splitlines():
                    if line.startswith("data:") and line[5:].strip():
                        d = json.loads(line[5:])
                        if d.get("id") == msg.get("id"):
                            return d
                return None
            return json.loads(raw)
        except ValueError:
            raise Problem("Something answers at that address, but not in the way Wealthfolio's agent access does.") from None

    def _rpc(self, method, params):
        self._id += 1
        d = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if not isinstance(d, dict) or "error" in d or "result" not in d:
            err = (d or {}).get("error") if isinstance(d, dict) else None
            raise _refusal(params.get("name"), json.dumps(err) if err else "") if method == "tools/call" else \
                Problem("Wealthfolio did not answer the way its agent access should.")
        return d["result"]

    def tools(self):
        return [t.get("name") for t in self._rpc("tools/list", {}).get("tools", [])]

    def call(self, tool, args=None):
        r = self._rpc("tools/call", {"name": tool, "arguments": args or {}})
        texts = [c.get("text", "") for c in r.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
        if r.get("isError"):
            raise _refusal(tool, " ".join(texts))
        if r.get("structuredContent") is not None:
            return r["structuredContent"]
        try:
            return json.loads(texts[0]) if texts else None
        except ValueError:
            return None

    def accounts(self):
        """[{"id", "name", "type"}], in Wealthfolio's order."""
        got = self.call("get_accounts") or {}
        rows = got.get("accounts", []) if isinstance(got, dict) else got
        return [account_of(a) for a in rows or [] if isinstance(a, dict) and a.get("id")]

    def activity_pages(self, account_id, max_pages=2000):
        """The account's activities a page (200) at a time, so a long history is never held whole."""
        page = 1
        while True:
            rows, last = page_of(self.call("search_activities", {"accountId": account_id, "page": page, "pageSize": 200}) or {})
            yield rows
            if page >= last or page >= max_pages or not rows:
                return
            page += 1

    def activities(self, account_id, max_pages=200):
        return [a for rows in self.activity_pages(account_id, max_pages) for a in rows]

    def balances(self, account_id):
        """[(date, running balance after that activity)] of a cash account, oldest first, plus today's balance when
        Wealthfolio says it."""
        rows = []
        for page in self.activity_pages(account_id):
            for a in page:
                f = fields(a)
                d = statements.to_date(f["date"])
                if d and f["balance"] is not None:
                    rows.append((d, f["balance"]))
        if len(rows) > 1 and rows[0][0] > rows[-1][0]:
            rows.reverse()                      # the answer came newest first
        rows.sort(key=lambda r: r[0])           # stable: the order within a day is kept
        try:
            now = balance_now(self.call("get_cash_balances") or {}, account_id)
        except Problem:
            now = None
        return rows, now


def _refusal(tool, said):
    """A tool that would not run. Words about permission mean a scope is missing; the scope to tick is named."""
    scope = SCOPES.get(tool)
    if scope and re.search(r"scope|permission|forbidden|not allowed|unauthori[sz]ed|access denied|insufficient", said or "", re.I):
        return Problem(f"The token works but lacks the scope {scope}. Make a new token in Wealthfolio with accounts:read and "
                       "activities:read ticked, and save it here.")
    return Problem(f"Wealthfolio could not answer ({tool or 'request'}).")


def test(address, token):
    """Try the connection the way the import would. -> (ok, sentence, accounts or None). Reads account names and at most
    one activity; changes nothing."""
    try:
        c = Client(address, token, timeout=20)
        have = set(c.tools())
        missing = [s for t, s in SCOPES.items() if t not in have]
        if missing:
            return False, ("The token works but lacks the scope " + " and ".join(missing) + ". Make a new token in "
                           "Wealthfolio with accounts:read and activities:read ticked, and save it here."), None
        accounts = c.accounts()
        if accounts:
            c.call("search_activities", {"accountId": accounts[0]["id"], "page": 1, "pageSize": 1})
    except Problem as ex:
        return False, str(ex), None
    n = len(accounts)
    if not n:
        return True, "Connected, but this Wealthfolio has no accounts yet.", accounts
    return True, f"Connected. Wealthfolio has {n} account{'' if n == 1 else 's'}; say below which ones are cards.", accounts


# ---------------------------------------------------------------- activities -> lines ----------------------------------------------------------------
place_of = pipeline.place_of


def lines_from(activities, card, since, until):
    """Wealthfolio activities of ONE card account -> ([line], counts). A line: id, date, card, merchant, amount (+ spend,
    - refund), fcy, currency, online, channel. Pure: no database, no network. The clean-up is the shared one
    (pipeline.clean), so a statement line with the same text comes out the same."""
    out, counts = [], {"payments and other": 0, "bank charges": 0}
    for a in activities or []:
        f = fields(a)
        d = statements.to_date(f["date"])
        if not d or d < since or d > until or not f["id"]:
            continue
        if f["kind"] not in (SPEND, REFUND):
            counts["payments and other"] += 1      # card payments (transfers in), deposits, interest, fees typed as such
            continue
        try:
            amt = round(abs(float(f["amount"] or 0)), 2)
        except (TypeError, ValueError):
            continue
        if not amt:
            continue
        raw = {"merchant": f["text"], "amount": amt if f["kind"] == SPEND else -amt, "est": f["alert"]}
        if f["currency"]:      # the note names the currency: that is a fact, not a guess from the shop's text
            raw.update(currency=f["currency"], fcy=True)
        c = pipeline.clean(raw)
        if c is None:
            counts["bank charges"] += 1
            continue
        ln = {"id": str(f["id"]), "date": d, "card": card, "tag": f["tag"], **c}
        if f["fcy_amount"]:
            ln["fcy_amount"] = f["fcy_amount"]
        out.append(ln)
    return out, counts


def place(conn, lines, shops=None):
    """Merchant categories for the lines (pipeline.place). -> how many purchases stayed without one."""
    return pipeline.place(conn, lines, shops)


# ---------------------------------------------------------------- the store ----------------------------------------------------------------
def source_id(person):
    return statements.wf_id(person)


def line_id(person, activity_id):
    return hashlib.sha256(f"{person}|wf|{activity_id}".encode()).hexdigest()[:32]


def fed_cards(conn, person, since, until):
    """Cards a feeder already sends lines for in the window: the import leaves those alone, unless the person gave
    the card to Wealthfolio (Settings)."""
    rows = conn.execute("SELECT DISTINCT card_id FROM stmt_lines WHERE statement_id = %s AND txn_date BETWEEN %s AND %s",
                        (statements.feed_id(person), since, until)).fetchall()
    return {r["card_id"] for r in rows if r["card_id"]}


def take_over(conn, person_row, cards):
    """Give these cards to Wealthfolio: the feeder's lines of them are removed and a feeder that still sends them is
    no longer listened to for them. -> how many feeder lines were removed."""
    n = 0
    for card in sorted(cards):
        n += pipeline.set_source(conn, person_row, card, "wealthfolio")
        person_row = conn.execute("SELECT * FROM people WHERE id = %s", (person_row["id"],)).fetchone()
    return n


def _have(conn, sid):
    return {r["id"]: r for r in conn.execute("SELECT id, card_id, txn_date, data_enc FROM stmt_lines WHERE statement_id = %s", (sid,)).fetchall()}


def _upsert(conn, person, sid, lines, have):
    """Write lines that are new or changed; every value encrypted. -> (ids kept, new, updated)"""
    keep, new, updated = set(), 0, 0
    for ln in lines:
        lid = line_id(person, ln["id"])
        keep.add(lid)
        data = {k: ln.get(k) for k in FIELDS}
        if ln.get("fcy_amount"):
            data["fcy_amount"] = ln["fcy_amount"]   # only when known, so lines stored before this stay byte-equal
        old = have.get(lid)
        if old and old["card_id"] == ln["card"] and old["txn_date"] == ln["date"] and \
                crypto.dec_json(conn, old["data_enc"], f"stmt_lines.data_enc:{lid}") == data:
            continue   # as it was: not written again
        blob = crypto.enc_json(conn, data, f"stmt_lines.data_enc:{lid}")
        conn.execute("INSERT INTO stmt_lines (id, statement_id, person, card_id, section, txn_date, data_enc) VALUES "
                     "(%s,%s,%s,%s,'wealthfolio',%s,%s) ON CONFLICT (id) DO UPDATE SET card_id = EXCLUDED.card_id, txn_date = "
                     "EXCLUDED.txn_date, data_enc = EXCLUDED.data_enc", (lid, sid, person, ln["card"], ln["date"], blob))
        new, updated = new + (old is None), updated + (old is not None)
    return keep, new, updated


def _tally(conn, sid):
    n = conn.execute("SELECT count(*) n, min(txn_date) a, max(txn_date) b FROM stmt_lines WHERE statement_id = %s", (sid,)).fetchone()
    conn.execute("UPDATE statements SET n_lines = %s, period_from = %s, period_to = %s, checked = true WHERE id = %s",
                 (n["n"], n["a"], n["b"], sid))


def store(conn, person, lines, since, until, cards, mapped=None):
    """Bring this source's lines of `cards` in [since, until] in step with `lines`; every value encrypted.
    Removed: a line of those cards inside the window that did not arrive (it is gone upstream), and every line of a
    card that is no longer matched to an account (`mapped` = the cards that still are). A line older than the window
    is never removed for being old: history stays.
    -> {"new", "updated", "removed"}"""
    crypto.ready(conn)
    sid = source_id(person)
    conn.execute("INSERT INTO statements (id, person, status) VALUES (%s,%s,'ready') ON CONFLICT (id) DO NOTHING", (sid, person))
    have = _have(conn, sid)
    keep, new, updated = _upsert(conn, person, sid, lines, have)
    gone = [r["id"] for r in have.values() if r["id"] not in keep and (
        (r["card_id"] in cards and since <= r["txn_date"] <= until) or (mapped is not None and r["card_id"] not in mapped))]
    if gone:
        conn.execute("DELETE FROM stmt_lines WHERE id = ANY(%s)", (gone,))
    _tally(conn, sid)
    return {"new": new, "updated": updated, "removed": len(gone)}


# ---------------------------------------------------------------- settings + the run ----------------------------------------------------------------
def mapping(cfg, held=None):
    """{Wealthfolio account id: card id} as saved; with `held`, only cards the person still holds."""
    try:
        m = json.loads(cfg.get("wf_map") or "{}")
    except ValueError:
        m = {}
    return {str(a): c for a, c in m.items() if c and (held is None or c in held)} if isinstance(m, dict) else {}


def known_accounts(cfg):
    try:
        rows = json.loads(cfg.get("wf_accounts") or "[]")
    except ValueError:
        rows = []
    return [a for a in rows if isinstance(a, dict) and a.get("id")] if isinstance(rows, list) else []


def window(cfg, today=None):
    try:
        days = max(7, min(400, int(cfg.get("wf_days") or DAYS)))
    except ValueError:
        days = DAYS
    until = today or datetime.date.today()
    return until - datetime.timedelta(days=days), until


def suggest(accounts, cards):
    """{account id: card id} where the account's name plainly names one held card. cards: [{"id", "name"}]."""
    out = {}
    typed = any(a.get("type") == CARD_TYPE for a in accounts)
    for a in accounts:
        if typed and a.get("type") != CARD_TYPE:
            continue                       # Wealthfolio says which accounts are cards: the others are not offered a match
        scored = sorted(((catalogue.name_sim(a["name"], c["name"]), c["id"]) for c in cards), reverse=True)
        if scored and scored[0][0] >= 0.6 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
            out[a["id"]] = scored[0][1]
    return out


def declined(cfg):
    try:
        d = json.loads(cfg.get("wf_declined") or "{}")
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def new_matches(accounts, cards, amap, no=None, has_lines=()):
    """Accounts to match by themselves: an account no card is matched to, whose name plainly names a held card that
    has no account and no transactions yet (a card added after Wealthfolio was set up), for someone who already imports
    a card from Wealthfolio. An account the person saved as "not a card" while that card was already held stays as
    they left it. -> {account id: card id}"""
    no = no or {}
    if not amap:
        return {}
    taken = set(amap.values()) | set(has_lines)
    free_accounts = [a for a in accounts if a["id"] not in amap]
    free_cards = [c for c in cards if c["id"] not in taken]
    if not free_accounts or not free_cards:
        return {}
    out = {}
    for acct, card in suggest(free_accounts, free_cards).items():
        if card in no.get(acct, ()) or card in out.values():
            continue
        out[acct] = card
    return out


def refresh_matches(conn, pid, cfg, client, held):
    """Each run: list Wealthfolio's accounts again (a new one shows without "Save and test") and match new cards.
    -> the accounts matched now ({account id: card id}); never stops the import."""
    try:
        accounts = client.accounts()
    except Exception:
        return {}
    if accounts:
        config.put(conn, "wf_accounts", json.dumps(accounts), pid)
    cards = conn.execute("SELECT c.id, c.name FROM cards c WHERE c.id = ANY(%s) ORDER BY c.name", (list(held) or [""],)).fetchall()
    amap = mapping(cfg)
    used = {r["card_id"] for r in conn.execute("SELECT DISTINCT card_id FROM stmt_lines WHERE person = %s AND card_id = ANY(%s)",
                                               (pid, list(held) or [""])).fetchall()}
    new = new_matches(accounts or known_accounts(cfg), cards, mapping(cfg, set(held)), declined(cfg), used)
    if new:
        amap.update(new)
        config.put(conn, "wf_map", json.dumps(amap), pid)
    return new


def suggest_boost(label, cash):
    """The one cash account whose name plainly fits a boost's linked account ("UOB Lady's Savings balance"), or None
    when none or more than one could be it. cash: [{"id", "name"}]."""
    want = set(pipeline.words(re.sub(r"\b(balance|account|average|monthly)\b", " ", label or "", flags=re.I)))
    if not want:
        return None
    scored = []
    for a in cash:
        have = set(pipeline.words(a.get("name") or ""))
        if have and len(want & have) >= max(2, len(want) - 1):
            scored.append((len(want & have) / len(want | have), a["id"]))
    scored.sort(reverse=True)
    if not scored or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15):
        return None
    return scored[0][1]


def note(conn, person, counts=None, error=None):
    """Remember the last run for the Settings page: when, counts, or the sentence that says what went wrong."""
    at = datetime.datetime.now().astimezone().isoformat(timespec="minutes")
    config.put(conn, "wf_last", json.dumps({"at": at, "counts": counts or {}, "error": error}), person)


def last(cfg):
    try:
        d = json.loads(cfg.get("wf_last") or "")
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _json(text, kind):
    try:
        d = json.loads(text or "")
    except ValueError:
        return kind()
    return d if isinstance(d, kind) else kind()


def boost_choice(cfg):
    """(Wealthfolio account id, boost id) the person named for the savings boost, or (None, None)."""
    d = _json(cfg.get("wf_boost"), dict)
    return (str(d["account"]), str(d["boost"])) if d.get("account") and d.get("boost") else (None, None)


def month_average(rows, now, today):
    """The month-to-date average day-end balance, from dated running balances (oldest first; the last row of a day is
    that day's end; days without a row carry the last balance; `now` is today's balance when known). None when the
    account has no balance on or before the month's first day and none in the month."""
    start = today.replace(day=1)
    by, before = {}, None
    for d, bal in rows:
        if d < start:
            before = bal
        elif d <= today:
            by[d] = bal
    if now is not None:
        by[today] = now
    last_seen, vals, day = before, [], start
    while day <= today:
        last_seen = by.get(day, last_seen)
        if last_seen is not None:
            vals.append(last_seen)
        day += datetime.timedelta(days=1)
    return round(sum(vals) / len(vals), 2) if vals else None


def read_boost(conn, pid, cfg, client, today=None):
    """The month's average day-end balance of the account named for the savings boost -> the person's boost amount
    (where a feeder's figure goes). Worked out from the running balance Wealthfolio keeps on each activity of that
    account, so no extra token scope is needed. Never raises: an account without balances leaves the amount as it
    is. -> a counts key."""
    account, bid = boost_choice(cfg)
    if not account:
        return None
    today = today or datetime.date.today()
    try:
        rows, now = client.balances(account)
    except Problem:
        return "savings balance not read"
    avg = month_average(rows, now, today)
    if avg is None or not conn.execute("SELECT 1 FROM card_boosts WHERE id::text = %s", (bid,)).fetchone():
        return "savings balance not read"
    p = conn.execute("SELECT boost_amounts, boost_auto FROM people WHERE id = %s", (pid,)).fetchone()
    amounts, auto = dict(p["boost_amounts"]), dict(p["boost_auto"])
    amounts[bid] = avg
    auto[bid] = {"month": today.strftime("%Y-%m"), "source": "wealthfolio"}
    conn.execute("UPDATE people SET boost_amounts = %s, boost_auto = %s WHERE id = %s", (Jsonb(amounts), Jsonb(auto), pid))
    return "savings balance read"


def close_days(lines):
    """{card: day of the month} estimated from notes that name a statement month: the latest purchase date carrying
    the newest such remark is at or just before the day that statement closed. A rough figure, used only until a
    statement file, a feeder or the person gives a better one."""
    latest = defaultdict(dict)   # card -> {statement month: latest date}
    for ln in lines:
        m = TAG_STATEMENT.search(ln.get("tag") or "")
        if m:
            cur = latest[ln["card"]].get(m.group(1))
            if cur is None or ln["date"] > cur:
                latest[ln["card"]][m.group(1)] = ln["date"]
    out = {}
    for card, months in latest.items():
        days = [months[k].day for k in sorted(months)[-3:]]
        out[card] = max(days) if max(days) - min(days) <= 6 else days[-1]
    return out


def run_person(conn, person, cfg=None, today=None, client=None):
    """One import for one person. -> counts (also stored for their Settings page). Raises Problem with a sentence for
    the person when it cannot run; that sentence is stored too."""
    pid = person["id"]
    cfg = cfg or config.load(conn, pid)
    try:
        held = set(person["held_cards"] or [])
        if not (cfg.get("wf_url") and cfg.get("wf_token")):
            raise Problem("Wealthfolio is not connected.")
        c = client or Client(cfg["wf_url"], cfg["wf_token"])
        matched = refresh_matches(conn, pid, cfg, c, held)   # a card added since: its account, when the name says it
        amap = dict(mapping(cfg, held), **matched)
        if not amap and not boost_choice(cfg)[0]:
            raise Problem("No Wealthfolio account is matched to a card yet.")
        since, until = window(cfg, today)
        fed = fed_cards(conn, pid, since, until)
        owner = pipeline.chosen(person)
        lines, counts = [], {"new": 0, "updated": 0, "removed": 0, "could not place": 0, "payments and other": 0,
                             "bank charges": 0, "cards left to the feeder": 0, "cards left to another source": 0}
        done = set()
        for account, card in sorted(amap.items()):
            mine = owner.get(card)
            if mine and mine != "wealthfolio":
                counts["cards left to another source"] += 1   # the person gave this card to mail or a feeder
                continue
            if not mine and card in fed:
                counts["cards left to the feeder"] += 1
                continue
            got, extra = lines_from(c.activities(account), card, since, until)
            lines += got
            done.add(card)
            for k, v in extra.items():
                counts[k] += v
        counts["could not place"] = place(conn, lines)
        if matched:
            counts["accounts matched to a new card"] = len(matched)
        counts.update(store(conn, pid, lines, since, until, done, set(amap.values())))
        for card, day in close_days(lines).items():
            pipeline.set_close_day(conn, pid, card, day, "note")
        said = read_boost(conn, pid, cfg, c, today)
        if said:
            counts[said] = 1
        note(conn, pid, counts)
        conn.commit()
        return counts
    except Problem as ex:
        conn.rollback()
        note(conn, pid, error=str(ex))
        conn.commit()
        raise


# ---------------------------------------------------------------- history: once, everything older than the window ----------------------------------------------------------------
def history_note(conn, person, state, counts=None, error=None, skipped=None, at_month=None, oldest=None):
    """Progress and result of the history import, for the Settings page (counts and month names only)."""
    at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    config.put(conn, "wf_hist", json.dumps({"state": state, "at": at, "counts": counts or {}, "error": error,
                                            "skipped": (skipped or [])[:60], "month": at_month,
                                            "oldest": oldest.isoformat() if oldest else None}), person)


def history_state(cfg):
    d = _json(cfg.get("wf_hist"), dict)
    return d or None


def history(conn, person, since=None, cfg=None, today=None, client=None):
    """Read everything Wealthfolio has that is older than the rolling window (from `since`, or from the start).

    One account at a time, its activities a page at a time; the lines are written a month at a time, each month in
    its own commit, so a long history neither times out nor sits in memory, and a run that stops can be started
    again: line ids are the activity ids, so nothing is ever written twice.

    Nothing is counted twice either. A card the person gave to another source is skipped. For the rest, a month is
    filled only when that card has no lines from another source (a statement, an alert, a feeder) in it; the months
    left alone are reported. A month that is read again is kept in step with Wealthfolio: a line that is gone there, or
    is now recognised as the bank's own charge, is removed; nothing is removed for being old.
    -> counts (also stored, with the skipped months, for the Settings page)."""
    pid = person["id"]
    cfg = cfg or config.load(conn, pid)
    counts = {"new": 0, "updated": 0, "months read": 0, "months left to another source": 0, "cards left to another source": 0,
              "payments and other": 0, "bank charges": 0, "could not place": 0}
    skipped, oldest = [], None
    try:
        amap = mapping(cfg, set(person["held_cards"] or []))
        if not (cfg.get("wf_url") and cfg.get("wf_token")):
            raise Problem("Wealthfolio is not connected.")
        if not amap:
            raise Problem("No Wealthfolio account is matched to a card yet.")
        until = window(cfg, today)[0] - datetime.timedelta(days=1)   # the window itself belongs to the regular import
        since = since or datetime.date.min
        if since > until:
            raise Problem("That start date is inside the days the regular import already keeps in step.")
        crypto.ready(conn)
        sid = source_id(pid)
        conn.execute("INSERT INTO statements (id, person, status) VALUES (%s,%s,'ready') ON CONFLICT (id) DO NOTHING", (sid, pid))
        owner = pipeline.chosen(person)
        other = {(r["card_id"], r["m"]) for r in conn.execute(
            "SELECT DISTINCT card_id, to_char(txn_date, 'YYYY-MM') m FROM stmt_lines WHERE person = %s AND statement_id <> %s "
            "AND card_id = ANY(%s) AND txn_date <= %s", (pid, sid, list(set(amap.values())), until)).fetchall()}
        c = client or Client(cfg["wf_url"], cfg["wf_token"])
        shops = pipeline.ShopIndex(conn)
        history_note(conn, pid, "running", counts)
        conn.commit()
        for account, card in sorted(amap.items()):
            if owner.get(card, "wealthfolio") != "wealthfolio":
                counts["cards left to another source"] += 1
                continue
            by_month = defaultdict(list)
            for page in c.activity_pages(account):
                got, extra = lines_from(page, card, since, until)
                for k, v in extra.items():
                    counts[k] += v
                for ln in got:
                    by_month[ln["date"].strftime("%Y-%m")].append(ln)
                    oldest = ln["date"] if oldest is None or ln["date"] < oldest else oldest
            have = _have(conn, sid)
            # months already stored for this card are read again too, so a line that is no longer a purchase upstream
            # (removed there, or now recognised as the bank's own charge) leaves the record
            stored = {r["txn_date"].strftime("%Y-%m") for r in have.values()
                      if r["card_id"] == card and since <= r["txn_date"] <= until}
            for month in sorted(set(by_month) | stored):
                lines = by_month.pop(month, [])
                if (card, month) in other:
                    counts["months left to another source"] += 1
                    skipped.append(f"{card} {month}")
                    continue
                counts["could not place"] += place(conn, lines, shops)
                kept, new, updated = _upsert(conn, pid, sid, lines, have)
                gone = [r["id"] for r in have.values() if r["card_id"] == card and r["id"] not in kept
                        and since <= r["txn_date"] <= until and r["txn_date"].strftime("%Y-%m") == month]
                if gone:
                    conn.execute("DELETE FROM stmt_lines WHERE id = ANY(%s)", (gone,))
                    counts["removed"] = counts.get("removed", 0) + len(gone)
                counts["new"], counts["updated"], counts["months read"] = counts["new"] + new, counts["updated"] + updated, counts["months read"] + 1
                _tally(conn, sid)
                history_note(conn, pid, "running", counts, skipped=skipped, at_month=month)
                conn.commit()   # a month at a time
        history_note(conn, pid, "done", counts, skipped=skipped, oldest=oldest)
        conn.commit()
        return dict(counts, skipped=skipped, oldest=oldest.isoformat() if oldest else None)
    except Problem as ex:
        conn.rollback()
        history_note(conn, pid, "failed", counts, error=str(ex), skipped=skipped)
        conn.commit()
        raise


def start_history(pid, since=None):
    """The history import in the background (the Settings page's button). Its progress is in the person's wf_hist."""
    import threading
    from . import db

    def work():
        with db.connect() as conn:
            try:
                person = conn.execute("SELECT * FROM people WHERE id = %s", (pid,)).fetchone()
                history(conn, person, since)
            except Problem:
                pass
            except Exception as ex:
                conn.rollback()
                print(f"wealthfolio history {pid}: {type(ex).__name__}", flush=True)
                try:
                    history_note(conn, pid, "failed", error=f"The history import stopped unexpectedly ({type(ex).__name__}). It can be started again.")
                    conn.commit()
                except Exception:
                    conn.rollback()
    threading.Thread(target=work, daemon=True).start()


def run(only=None, since=None, history_too=False):
    """The import for everyone who has it switched on (or one person). One person's failure never stops the others.
    With history_too (the command line's --since / --all, one person): that person's history import first.
    Prints counts only."""
    from collections import Counter
    from . import db
    total, people_done, failed = Counter(), 0, 0
    with db.connect() as conn:
        crypto.ready(conn)
        rows = conn.execute("SELECT * FROM people ORDER BY id").fetchall()
        for p in rows:
            if only and p["id"] != only:
                continue
            try:
                cfg = config.load(conn, p["id"])
                if cfg.get("wf_on") != "1" and not only:
                    continue
                if history_too and only:
                    h = history(conn, p, since, cfg)
                    h.pop("skipped", None)
                    old = h.pop("oldest", None)
                    if old:
                        print(f"wealthfolio history: oldest purchase found {old}", flush=True)
                    print("wealthfolio history: " + (", ".join(f"{k} {v}" for k, v in sorted(h.items()) if v) or "nothing older than the window"), flush=True)
                total.update(run_person(conn, p, cfg))
                people_done += 1
            except crypto.Unavailable:
                raise
            except Problem:
                failed += 1
            except Exception as ex:
                failed += 1
                conn.rollback()
                try:
                    note(conn, p["id"], error=f"The import stopped unexpectedly ({type(ex).__name__}).")
                    conn.commit()
                except Exception:
                    conn.rollback()
                print(f"wealthfolio {p['id']}: {type(ex).__name__}", flush=True)
    said = ", ".join(f"{k} {v}" for k, v in sorted(total.items()) if v) or "nothing new"
    print(f"wealthfolio: {people_done} imported, {failed} failed; {said}", flush=True)
    return 1 if failed and not people_done and only else 0
