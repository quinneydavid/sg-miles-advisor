"""Annual fees: what each held card costs, when the fee next falls due, whether the bank's own terms say it can be
waived, and whether this person's spend meets that condition.

Where each fact comes from (shown on the page, never mixed up):
  the fee        what you pay (Settings)  >  the catalogue's list price  >  the bank's document (T&C check)
  the due date   the last fee charge seen on your statements or feed + one year  >  the month you gave in Settings
  the waiver     only what the bank's own document states (card_tc_checks 'fee_waiver', with its sentence quoted).
                 Most Singapore banks state none: waivers are then at the bank's discretion, on request.
  your spend     your own lines on that card, since the last fee charge (the membership year so far) or, when no
                 charge has been seen, over the months on file."""
import calendar
import datetime
import re

from . import crypto

FEE_RE = re.compile(r"\bANNUAL\s*(CARD\s*|MEMBERSHIP\s*)?FEE|\bMEMBERSHIP\s*FEE|\bANNUAL\s*SERVICE\s*FEE", re.I)
REV_RE = re.compile(r"REVERS|WAIVE|REFUND|REBATE|\bCR\b", re.I)
MONTHS = [calendar.month_name[i] for i in range(1, 13)]


def is_fee(text):
    return bool(text and FEE_RE.search(text))


def store_feed(conn, person, fees):
    """Fee charges / reversals from the person's feeder (feed field `fees`): replace the stored list."""
    valid = {r["id"] for r in conn.execute("SELECT id FROM cards").fetchall()}
    out = []
    for f in fees or []:
        try:
            d = datetime.date.fromisoformat(str(f.get("date"))[:10])
            amt = round(float(f.get("amount")), 2)
        except (TypeError, ValueError):
            continue
        if f.get("card") in valid and abs(amt) < 5000:
            out.append({"card": f["card"], "date": d.isoformat(), "amount": amt, "text": str(f.get("text") or "")[:80]})
    conn.execute("UPDATE people SET fees_enc = %s WHERE id = %s",
                 (crypto.enc_json(conn, out, f"people.fees_enc:{person}"), person))
    return len(out)


def events(conn, person, fee_lines=()):
    """Every fee charge (+) and reversal (-) known for the person: the feed's list + fee lines on uploaded statements.
    -> {card id: [{"date": date, "amount": float, "text": str}] oldest first}"""
    out = {}
    row = conn.execute("SELECT fees_enc FROM people WHERE id = %s", (person,)).fetchone()
    stored = crypto.dec_json(conn, row["fees_enc"], f"people.fees_enc:{person}") if row and row["fees_enc"] else []
    seen = set()
    for f in stored:
        k = (f["card"], f["date"], f["amount"])
        if k not in seen:
            seen.add(k)
            out.setdefault(f["card"], []).append({"date": datetime.date.fromisoformat(f["date"]), "amount": f["amount"], "text": f.get("text", "")})
    for ln in fee_lines:
        amt = ln["amount"] if not (REV_RE.search(ln.get("merchant") or "") and ln["amount"] > 0) else -ln["amount"]
        k = (ln["card_id"], ln["date"].isoformat(), round(amt, 2))
        if ln["card_id"] and k not in seen:
            seen.add(k)
            out.setdefault(ln["card_id"], []).append({"date": ln["date"], "amount": round(amt, 2), "text": ln.get("merchant") or ""})
    for v in out.values():
        v.sort(key=lambda f: f["date"])
    return out


def bank_terms(conn, ids):
    """{card id: {"fee": check row or None, "waiver": check row or None}} from the bank-document checks."""
    out = {}
    try:
        rows = conn.execute("SELECT card_id, subject, status, theirs, quote, url, data FROM card_tc_checks WHERE card_id = ANY(%s) "
                            "AND subject IN ('annual_fee', 'fee_waiver') AND status <> 'not_found'", (list(ids) or [""],)).fetchall()
    except Exception:
        conn.rollback()
        return out
    for r in rows:
        out.setdefault(r["card_id"], {})["fee" if r["subject"] == "annual_fee" else "waiver"] = r
    return out


def add_year(d):
    try:
        return d.replace(year=d.year + 1)
    except ValueError:   # 29 Feb
        return d.replace(year=d.year + 1, day=28)


def next_month(month, today):
    """The next first-of-month for a month number: this year if it hasn't started yet, else next year."""
    d = datetime.date(today.year, month, 1)
    return d if d >= today.replace(day=1) else datetime.date(today.year + 1, month, 1)


def waiver_progress(w, lines, last_charge, due, today):
    """Does this person's spend on the card meet a spend-based waiver? -> dict or None when the rule isn't spend-based.
    Counted from the last fee charge (the membership year so far); with no charge seen, over the months on file."""
    data = (w or {}).get("data") or {}
    if data.get("kind") != "spend" or not data.get("amount"):
        return None
    need = float(data["amount"])
    start = last_charge if last_charge else (min((ln["date"] for ln in lines), default=None))
    if start is None:
        return {"need": need, "status": "unknown", "spent": 0.0, "days": 0}
    start = max(start, today - datetime.timedelta(days=365))
    spent = sum(ln["amount"] for ln in lines if ln["date"] >= start)
    days = max(1, (today - start).days)
    pace = spent / days * 365
    if spent >= need:
        status = "met"
    elif days < 21:
        status = "unknown"   # too little to project from
    else:
        status = "on_pace" if pace >= need else "short"
    return {"need": need, "spent": spent, "pace": pace, "days": days, "since": start, "status": status,
            "left": max(0.0, need - spent), "by": due}


def tracker(conn, person, cat, lines, fee_lines, today=None):
    """One row per held card, soonest fee first.
    row: card, fee, fee_src ('yours'|'charged'|'list'|'bank'|None), last (event or None), waived_last (bool), due (date or None),
         due_src ('statement'|'you'|None), days (to due), waiver (bank check row or None), bank_fee (check row or None),
         progress (waiver_progress or None)"""
    today = today or datetime.date.today()
    held = [h for h in person["held_cards"] if h in cat["cards"]]
    ev = events(conn, person["id"], fee_lines)
    terms = bank_terms(conn, held)
    mine = person.get("card_fees") or {}
    dates = person.get("fee_dates") or {}
    out = []
    for cid in held:
        c = cat["cards"][cid]
        t = terms.get(cid, {})
        bank_fee = ((t.get("fee") or {}).get("data") or {}).get("fee")
        if mine.get(cid) is not None:
            fee, src = float(mine[cid]), "yours"
        elif c["annual_fee"] is not None:
            fee, src = float(c["annual_fee"]), "list"
        elif bank_fee is not None:
            fee, src = float(bank_fee), "bank"
        else:
            fee, src = None, None
        charges = [f for f in ev.get(cid, []) if f["amount"] > 0]
        last = charges[-1] if charges else None
        if last and src != "yours":   # what the bank actually charged beats any list price
            fee, src = float(last["amount"]), "charged"
        waived_last = bool(last and any(f["amount"] < 0 and 0 <= (f["date"] - last["date"]).days <= 120 for f in ev.get(cid, [])))
        due, due_src = None, None
        if last:
            due = add_year(last["date"])
            while due < today:
                due = add_year(due)
            due_src = "statement"
        elif str((dates.get(cid) or {}).get("month", "")).isdigit() and 1 <= int(dates[cid]["month"]) <= 12:
            due, due_src = next_month(int(dates[cid]["month"]), today), "you"
        card_lines = [ln for ln in lines if ln["card_id"] == cid and ln["amount"] > 0]
        # the waiver chase: the bank charged the fee and hasn't given it back. Asking is the person's job; the app
        # says when, remembers that they asked, and watches the statements for the reversal.
        note = dates.get(cid) or {}
        mark = note.get("outcome") if last and note.get("for") == last["date"].isoformat() else None   # their word, for THIS charge
        asked = note.get("asked") if last and note.get("for") == last["date"].isoformat() else None
        since = (today - last["date"]).days if last else None
        if waived_last or mark == "waived":
            chase = "waived"
        elif mark == "paid":
            chase = "paid"
        elif last and since <= 330:   # until the next fee is nearly due: an unreversed fee stays on the list until it is marked
            chase = "asked" if asked else "ask"
        else:
            chase = None
        out.append({"card": cid, "fee": fee, "fee_src": src, "last": last, "waived_last": waived_last or mark == "waived", "due": due,
                    "chase": chase, "asked": asked, "since": since,
                    "due_src": due_src, "days": (due - today).days if due else None, "waiver": t.get("waiver"),
                    "bank_fee": t.get("fee"), "progress": waiver_progress(t.get("waiver"), card_lines, last["date"] if last else None, due, today)})
    out.sort(key=lambda r: (r["due"] is None, r["due"] or today, cat["cards"][r["card"]]["name"]))
    return out


def effective(rows):
    """{card id: S$ a year the card really costs} where the statements (or the person) settle it: 0 when the last fee
    was reversed, the charged amount when it was paid. Everything else is left to the bank's document / list price."""
    out = {}
    for r in rows:
        if r.get("chase") == "waived":
            out[r["card"]] = 0.0
        elif r.get("chase") == "paid" and r["last"]:
            out[r["card"]] = float(r["last"]["amount"])
    return out


def mark(person, cid, action, rows, today=None):
    """New fee_dates entry for a button on the Fees page ('asked' | 'waived' | 'paid' | 'reset'), tied to the charge it
    is about so next year's fee starts clean. Returns the whole fee_dates dict to store."""
    today = today or datetime.date.today()
    dates = dict(person.get("fee_dates") or {})
    r = next((x for x in rows if x["card"] == cid), None)
    if not r or not r["last"]:
        return dates
    cur = dict(dates.get(cid) or {})
    key = r["last"]["date"].isoformat()
    if cur.get("for") != key:
        cur = {k: v for k, v in cur.items() if k == "month"}
        cur["for"] = key
    if action == "asked":
        cur["asked"] = today.isoformat()
    elif action in ("waived", "paid"):
        cur["outcome"] = action
    elif action == "reset":
        cur = {k: v for k, v in cur.items() if k == "month"}
    dates[cid] = cur
    return dates


# Where each bank takes a fee-waiver request, from the bank's own help pages (read 10 Nov 2026). The app can't send the
# request itself: it needs the person's own bank login.
HOW_TO_ASK = {
    "dbs": {"steps": "In digibank, open the digibot chat, type \"Fee Waiver\", authenticate, then pick Credit Cards Fee Waiver, the fee and the card. "
                     "Or call 1800 111 1111 and press 1, then 1.",
            "after": "DBS replies by email or SMS within 3 working days.",
            "url": "https://www.dbs.com.sg/personal/support/card-waive-credit-card-cashline-charges.html"},
    "uob": {"steps": "In the UOB TMRW app: Accounts, pick the card, Waive fees, select the fee, Next, swipe to confirm. Or call 1800 222 2121.",
            "after": "UOB only takes requests this way, only for a fee in the last 3 statements, and its answer is final. It's processed by the next working day.",
            "url": "https://www.uob.com.sg/personal/cards/services/card-fee-waiver.page"},
    "ocbc": {"steps": "In the OCBC app: More, Card Services, Request fee waiver, pick the card and submit. On the website: Customer service, Credit card fee waiver.",
             "after": "",
             "url": "https://www.ocbc.com/personal-banking/digital-banking/step-by-step-guides/cards-management/card-fee-waiver"},
    "maybank": {"steps": "Call 1800 629 2265 (24 hours, automated): key in the card number and your mobile number. One call per card.",
                "after": "Ask at least 3 working days before the statement is due. Maybank answers by SMS within 7 working days.",
                "url": "https://info.maybank2u.com.sg/personal/customer-service/faq/faq-creditcard-phone.aspx"},
    "citibank": {"steps": "Ask in the Citi Mobile app or Citibank Online, or call CitiPhone on 6225 5225 (8am to 8pm). Citi doesn't publish the in-app steps.",
                 "after": "",
                 "url": "https://www.citibank.com.sg/content/cgcpc/sg/prelogin/www-citibank-com-sg/fees-and-rates.html"},
}
BEFORE_YOU_ASK = {
    "citibank_premiermiles": "Paying this fee gives 10,000 Citi Miles; Citi takes them back if the fee is waived. Decide which you'd rather have.",
    "dbs_vantage": "DBS says this fee isn't waived; paying it gives 12,500 DBS Points (25,000 miles).",
}


def how(card_id):
    """{steps, after, url, before} for the card's bank, or None when we haven't read that bank's guide."""
    h = HOW_TO_ASK.get(str(card_id or "").split("_")[0])
    return {**h, "before": BEFORE_YOU_ASK.get(card_id, "")} if h else None


def how_to_ask(card_id):
    h = how(card_id)
    return h["steps"] if h else ""


def prompts(rows):
    """What needs the person now, most urgent first: [{row, kind}] with kind = 'ask' (a fee was charged and no waiver
    asked yet), 'chase' (asked over five weeks ago, nothing reversed) or 'soon' (a fee is due within 30 days)."""
    out = []
    for r in rows:
        if r.get("chase") == "ask":
            out.append({"row": r, "kind": "ask"})
        elif r.get("chase") == "asked" and r.get("asked") and (datetime.date.today() - datetime.date.fromisoformat(r["asked"])).days > 35:
            out.append({"row": r, "kind": "chase"})
        elif r.get("chase") not in ("asked", "paid") and r.get("fee") and r.get("days") is not None and 0 <= r["days"] <= 30:
            out.append({"row": r, "kind": "soon"})
    order = {"ask": 0, "chase": 1, "soon": 2}
    return sorted(out, key=lambda x: (order[x["kind"]], x["row"].get("days") or 0))


def advice(r):
    """One plain sentence on what to do about this card's fee (plus where to ask, when the bank's guide is on file)."""
    text = _advice(r)
    how = how_to_ask(r.get("card"))
    return f"{text} {how}" if how and "ask" in text.lower() and r.get("chase") != "asked" else text


def _advice(r):
    p, w = r["progress"], r["waiver"]
    kind = ((w or {}).get("data") or {}).get("kind")
    last = r.get("last")
    if r.get("chase") == "ask":
        cant = " The bank's terms say it can't be waived, so decide whether the card is worth it." if kind == "not_waivable" else ""
        return (f"S${last['amount']:,.2f} was charged on {last['date']:%d %b}. Ask the bank for a waiver now (app, phone or chat); "
                f"it's easiest in the first weeks.{cant}")
    if r.get("chase") == "asked":
        wait = (datetime.date.today() - datetime.date.fromisoformat(r["asked"])).days
        return (f"You asked on {datetime.date.fromisoformat(r['asked']):%d %b}. No reversal on your statements yet"
                + (": it's been over a month, chase the bank or decide whether to keep the card." if wait > 35 else "; it usually shows within a statement or two."))
    if r.get("chase") == "paid":
        return f"You paid S${last['amount']:,.2f} for this year. The Advisor counts that against the card."
    if r["fee"] == 0:
        return "Nothing to pay."
    if p:
        if p["status"] == "met":
            return f"You've spent S${p['spent']:,.0f} on it, past the S${p['need']:,.0f} the bank asks for: the fee should be waived."
        if p["status"] == "on_pace":
            return (f"S${p['spent']:,.0f} so far, on pace for S${p['pace']:,.0f} a year against the S${p['need']:,.0f} needed: "
                    "keep it up and the fee should be waived.")
        if p["status"] == "short":
            return (f"S${p['spent']:,.0f} so far, on pace for S${p['pace']:,.0f} a year: S${p['left']:,.0f} short of the "
                    f"S${p['need']:,.0f} the bank asks for" + (f" by {p['by']:%d %b %Y}" if p.get("by") else "") + ". Ask for a waiver or expect the fee.")
        return f"The bank waives it with S${p['need']:,.0f} of spend a year; there isn't enough spend on file yet to say if you'll make it."
    if kind == "not_waivable":
        return f"The bank says this fee can't be waived ({w['theirs'].rstrip('.')})." if w.get("theirs") else "The bank says this fee can't be waived."
    if kind == "points":
        pts = ((w or {}).get("data") or {}).get("points")
        return (f"The bank lets you pay it with points instead of cash ({w.get('theirs', '').rstrip('.')})." if not pts else
                f"The bank waives it for {pts:,.0f} points instead of cash. Otherwise ask for a waiver when the fee posts.")
    if kind == "first_year":
        return "The bank states a first-year waiver only. After that, ask for a waiver when the fee posts."
    if kind == "automatic":
        return "The bank's terms say the fee is waived."
    if kind == "on_request":
        return "The bank's terms say a waiver is on request: call or use the app when the fee posts."
    if r["waived_last"]:
        return "The last fee was waived. Ask again when the next one posts: the app will tell you when it does."
    return "No waiver rule from the bank's documents is on file: treat it as at the bank's discretion and ask when the fee posts."
