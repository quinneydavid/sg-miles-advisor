"""The Cards web app. Five places: Pay (which card for this purchase), Wallet (this period's caps, points, goals), Spend
(what your spend earned), Advisor (keep / cancel / get, and a verdict on any card) and Cards (the catalogue, MCCs,
changes). Two doors (access.py): an internal one behind a forward-auth proxy, a public one behind Cloudflare
Access. Anyone not in `people` gets 403. Stdlib HTTP server, server-rendered HTML; the pages work without JS."""
import datetime
import email.parser
import email.policy
import gzip
import html
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from psycopg.types.json import Jsonb

from . import (__version__, access, advise, cache, config, crypto, db, engine, fees, goals, ingest, llm, look, people, points,
               setupweb, spendweb, statements, store)

e = lambda v: html.escape("" if v is None else str(v))


def num(v, d=2):
    if v is None:
        return ""
    f = float(v)
    return f"{f:,.{d}f}".rstrip("0").rstrip(".") if d else f"{f:,.0f}"


def money(v):
    return "" if v is None else f"S${float(v):,.0f}"


TABS = [("/", "Pay", "pay"), ("/wallet", "Wallet", "wallet"), ("/flights", "Flights", "flights"), ("/spend", "Spend", "spend"),
        ("/advisor", "Advisor", "advisor"), ("/ask", "Ask", "ask"), ("/catalogue", "Cards", "cards")]
SUB = {"wallet": [("/wallet", "This period"), ("/fees", "Fees"), ("/points", "Points")],
       "flights": [("/flights", "Where can I go"), ("/goals", "My flights")],
       "spend": [("/spend", "Summary"), ("/spend/lines", "Transactions"), ("/statements", "Statements")],
       "advisor": [("/advisor", "My wallet"), ("/advisor/card", "Should I get it?"), ("/simulate", "Try a wallet"),
                   ("/offers", "Sign-up bonuses")],
       "cards": [("/catalogue", "Cards"), ("/compare", "Compare"), ("/mcc", "By MCC"), ("/changes", "What changed")]}
SECTION = [("/card/", "cards"), ("/mcc", "cards"), ("/changes", "cards"), ("/catalogue", "cards"), ("/compare", "cards"),
           ("/flights", "flights"), ("/goals", "flights"), ("/advisor", "advisor"),
           ("/simulate", "advisor"), ("/ask", "ask"), ("/offers", "advisor"), ("/spend", "spend"), ("/statements", "spend"),
           ("/wallet", "wallet"), ("/fees", "wallet"), ("/points", "wallet"), ("/settings", ""), ("/admin", ""), ("/summary", ""),
           ("/merchant", "pay")]
ASSET = __version__   # /app.css?v= and /app.js?v=: cached for a year, a new version is a new URL


def section(path):
    if path == "/":
        return "pay"
    return next((sec for pre, sec in SECTION if path.startswith(pre)), "")


def page(title, body, path, person):
    sec = section(path)
    tabs = "".join(f"<a href='{h}'{' class=on aria-current=page' if k == sec else ''}>{look.ICONS[k]}<span>{t}</span></a>"
                   for h, t, k in TABS)
    subs = SUB.get(sec) or []
    cur = max((h for h, _ in subs if path == h or path.startswith(h + "/")), key=len, default=None)
    sub = ("<nav class=sub>" + "".join(f"<a href='{h}'{' class=on' if h == cur else ''}>{t}</a>" for h, t in subs) + "</nav>") if subs else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            f"initial-scale=1,viewport-fit=cover'><meta name=theme-color content='#f5f5f7' media='(prefers-color-scheme: light)'>"
            f"<meta name=theme-color content='#000' media='(prefers-color-scheme: dark)'><title>{e(title)} · Cards</title>"
            f"<link rel=manifest href=/manifest.webmanifest><link rel=apple-touch-icon href=/apple-touch-icon.png>"
            f"<meta name=apple-mobile-web-app-capable content=yes><meta name=mobile-web-app-capable content=yes>"
            f"<meta name=apple-mobile-web-app-title content=Cards><meta name=apple-mobile-web-app-status-bar-style content=default>"
            f"<meta name=format-detection content='telephone=no'>"
            f"<link rel=stylesheet href='/app.css?v={ASSET}'><script src='/theme.js?v={ASSET}'></script><script src='/app.js?v={ASSET}' defer></script></head><body>"
            f"<header><a class=brand href=/>Cards</a><span class=right><button type=button class=theme id=theme aria-label='Switch light / dark'>"
            f"{look.ICONS['moon']}{look.ICONS['sun']}</button><a class=who href=/settings aria-label=Settings>{e(person['display'])}"
            f"{look.ICONS['gear']}</a></span></header><nav class=tabs aria-label=Main>{tabs}</nav><main>{sub}{body}<p class=legal>Not financial advice. Not affiliated with any bank or card issuer. Check the "
            f"issuer's terms before you act.</p></main></body></html>")


def wallet_context(conn, person):
    """From the person's real spend (feed + uploads): this period's progress per capped / minimum-spend bucket and the
    cap room per card. None when there is no spend yet or encryption is unavailable. Worked out once per change of
    their lines, settings or the day."""
    from . import replay
    today = datetime.date.today()
    months = [(today.replace(day=1) - datetime.timedelta(days=d)).strftime("%Y-%m") for d in (0, 31)]
    try:
        lines = cache.lines(conn, person["id"], months)
    except crypto.Unavailable:
        return None
    if not lines:
        return None

    def work():
        base, _ = replay.load(conn, person["excluded_banks"], person["boost_amounts"], person["statement_days"], replay.in_store(person),
                                  person["held_cards"], person.get("cashback_too"))
        prog = replay.progress(base, lines, list(person["held_cards"]), today)
        room = {}
        for x in prog:
            if x["left"] is not None:
                room.setdefault(x["card"], {})[x["bucket"]] = x["left"]
            if x["min_left"] is not None:   # read by engine.rate_card: which minimum-spend tiers are reached
                room.setdefault(x["card"], {}).setdefault("__min__", {})[x["bucket"]] = x["min_left"]
            if x.get("shops_left") is not None:   # and how many different shops a min_shops tier still needs
                room.setdefault(x["card"], {}).setdefault("__shops__", {})[x["bucket"]] = x["shops_left"]
        return {"progress": prog, "room": room, "cards": base.cards}

    return cache.memo(conn, person["id"], ("ctx", cache.person_key(person), today), work)


def boost_warnings(conn, person):
    """A boost balance close above a tier (within S$500) or just under one: worth telling the person."""
    out = []
    for b in conn.execute("SELECT b.*, c.name card FROM card_boosts b JOIN cards c ON c.id = b.card_id WHERE b.card_id = ANY(%s)",
                          (list(person["held_cards"]) or [""],)).fetchall():
        have = person["boost_amounts"].get(str(b["id"]))
        if have is None:
            continue
        for thr, val in sorted(b["tiers"], key=lambda t: float(t[0])):
            gap = float(have) - float(thr)
            if 0 <= gap < 500:
                out.append(f"{b['label']}: S${float(have):,.0f}, only S${gap:,.0f} above the S${float(thr):,.0f} tier. "
                           f"Below it, {b['card']} loses {num(val)} mpd.")
            elif -1000 < gap < 0:
                out.append(f"{b['label']}: S${float(have):,.0f}, S${-gap:,.0f} short of the S${float(thr):,.0f} tier "
                           f"(+{num(val)} mpd on {b['card']}).")
    return out


def photos(conn):
    """card_id -> picture version (changes on every upload/re-pick, used in the /img URL)."""
    return cache.catalogue(conn)["photos"]


def bar(pct, cls=""):
    return f"<div class=bar><i{' class=' + cls if cls else ''} style='width:{max(0, min(100, round(pct)))}%'></i></div>"


def tc_counts(conn, cid=None):
    """{card id: {"match": n, "differs": n, "not_found": n}} from the bank T&C checks."""
    out = {}
    try:
        rows = conn.execute("SELECT card_id, status, count(*) n FROM card_tc_checks " + ("WHERE card_id = %s " if cid else "") +
                            "GROUP BY 1, 2", (cid,) if cid else ()).fetchall()
    except Exception:
        conn.rollback()
        return out
    for r in rows:
        out.setdefault(r["card_id"], {"match": 0, "differs": 0, "not_found": 0})[r["status"]] = r["n"]
    return out


def tc_chip(counts):
    if not counts:
        return ""
    if counts.get("differs"):
        return "<span class='chip warn'>differs from T&amp;C</span>"
    if counts.get("match"):
        return "<span class='chip ok'>T&amp;C checked</span>"
    return ""


def rule_text(r):
    unit = "%" if r.get("kind") == "cashback" else " mpd"
    bits = [f"<b>{num(r['rate'])}{unit}</b> {e(advise.label(r))}"]
    if r["cap"]:
        bits.append(f"cap {money(r['cap'])}/{e(r['cap_period'])}")
    if r["min_spend"]:
        bits.append(f"min {money(r['min_spend'])}")
    if r["promo_until"]:
        left = (r["promo_until"] - datetime.date.today()).days
        bits.append(f"<span class={'warn' if left <= 14 else 'muted'}>until {r['promo_until']:%d %b %Y}</span>")
    return " · ".join(bits)


def rule_condition(match):
    """What else a rule needs besides the MCC: 'online', 'contactless', 'mobile pay', 'FCY'."""
    if not isinstance(match, dict):
        return ""
    if "any_of" in match and "mcc" not in match:
        return ""   # the MCC alternative inside any_of stands alone
    ch = match.get("channel")
    if ch:
        return {"mobile": "mobile pay"}.get(ch, ch)
    if match.get("currency"):
        return " / ".join(match["currency"])
    if match.get("fcy") is True:
        return "FCY"
    return ""


# currencies a card pays its own rate on (Which card? shows them once "Overseas" is ticked); the rest = any other
CURRENCIES = [("IDR", "Indonesian rupiah"), ("MYR", "Malaysian ringgit"), ("THB", "Thai baht"), ("VND", "Vietnamese dong")]


# --- pages -----------------------------------------------------------------------------------------------------------

def wallet_page(conn, q, person):
    """Your cards and where each bonus cap and minimum stands this period."""
    cat = cache.catalogue(conn)
    held = [cat["cards"][h] for h in person["held_cards"] if h in cat["cards"]]
    ph = cat["photos"]
    body = "<h1>Wallet</h1>"
    strip = ""
    if held:
        strip = ("<h2>Your cards</h2><div class=wallet>" + "".join(
            f"<a href='/card/{e(r['id'])}'>{look.face(r, 'md', ph.get(r['id']))}</a>" for r in sorted(held, key=lambda r: (r["bank_id"], r["name"]))) + "</div>")
    else:
        body += "<div class=panel><p>No cards picked yet.</p><a class=btn href='/settings'>Choose the cards you hold</a></div>"
    body += fee_prompts(conn, person, back="/wallet", kinds=("ask", "chase"))
    ctx = wallet_context(conn, person)
    if ctx and ctx["progress"]:
        rows, quiet = [], []
        for x in sorted(ctx["progress"], key=lambda x: (x["left"] is None, (x["left"] or 0) / (x["cap"] or 1))):
            cc = ctx["cards"][x["card"]]
            pct = min(100, round(100 * x["used"] / x["cap"])) if x["cap"] else None
            what = []
            short_shops = x.get("shops_left") or 0
            unmet = bool(x["min_spend"] and (x["min_left"] or short_shops))
            lead = ""
            per_txt = 'statement month' if x['statement'] else 'month'
            if unmet and x["min_left"]:   # the first thing to know: without the minimum there is no bonus at all
                lead = (f"<small class=warn style='white-space:normal'>Spend {money(x['min_left'])} more on this card by {x['end']:%d %b} "
                        f"or it earns no bonus this {per_txt} (it needs {money(x['min_spend'])}).</small>")
            if short_shops:
                have = ", ".join(x["shops_have"]) or "none yet"
                todo = ", ".join(sh for sh in x["shops_all"] if sh not in x["shops_have"])
                lead += (f"<small class=warn style='white-space:normal'>Buy at {short_shops} more of its shops by {x['end']:%d %b} "
                         f"(it needs {x['shops_need']} different ones; so far: {e(have)}). Still open: {e(todo)}.</small>")
            if x["cap"]:
                what.append(f"{money(x['used'])} of the {money(x['cap'])} bonus limit used, at {num(x['rate'])} miles per S$")
            if x["min_spend"] and not unmet:
                what.append(f"{money(x['min_spend'])} minimum reached ✓")
            days = "today" if x["days_left"] <= 0 else ("1 day left" if x["days_left"] == 1 else f"{x['days_left']} days left")
            when = f"{'statement month' if x['statement'] else 'month'} ends {x['end']:%d %b} ({days})"
            right = (f"{money(x['min_left'])}<small>to spend</small>" if unmet and x["min_left"] else
                     f"{short_shops}<small>shops to go</small>" if unmet else
                     f"{money(x['left'])}<small>bonus room</small>" if x["cap"] else "✓")
            pct_cls = "hot" if (pct is not None and pct >= 90) or unmet else ""
            (rows if (x["used"] or x["card_total"]) else quiet).append(
                f"<a class='row wrapt' href='/spend/lines?month=all&card={e(cc['id'])}&since={x['start']:%Y-%m-%d}&until={x['end']:%Y-%m-%d}'>"
                f"{look.face(cc, 'sm', ph.get(cc['id']))}<span class=grow><b>{e(cc['name'])}</b>"
                f"{lead}<small style='white-space:normal'>{e(advise.label(x))}{' · ' + ' · '.join(what) if what else ''}</small>"
                f"<small>{when} · see the purchases ›</small>"
                f"{bar(pct, pct_cls) if pct is not None else ''}</span><span class=r>{right}</span></a>")
        body += "<h2>Bonuses this period</h2><div class=list>" + ("".join(rows) or "<div class=row>No bonus spend yet this period.</div>") + "</div>"
        if quiet:
            body += f"<details><summary>{len(quiet)} bonuses you haven't used this period</summary><div class=list>" + "".join(quiet) + "</div></details>"
    elif held:
        body += ("<div class=panel style='margin-top:14px'><p><b>No spend on file for this period yet.</b></p><p class=muted>Caps and "
                 "minimums appear here once your card lines arrive or you upload a statement.</p><a href=/statements>Statements →</a></div>")
    for w in boost_warnings(conn, person):
        body += f"<div class=hint>⚠️ {e(w)} <a href=/settings#boosts>Check your balances ›</a></div>"
    body += strip
    body += fee_prompts(conn, person, back="/wallet", kinds=("soon",))
    if held:
        body += "<p style='margin-top:22px'><a href=/advisor>Is every one of these earning its place? →</a></p>"
    return "Wallet", body


def fee_rows(conn, person):
    cat = cache.catalogue(conn)
    return cat, fees.tracker(conn, person, cat, cache.lines(conn, person["id"]), cache.fee_lines(conn, person["id"]))


def fee_prompts(conn, person, cat=None, rows=None, back="/fees", kinds=("ask", "chase", "soon")):
    """The fee that needs you now, said plainly, on Pay, Wallet and Fees: what was charged or is coming, how this bank
    takes a waiver request (its own guide), and the buttons that record what you did. '' when nothing is pending."""
    try:
        if rows is None:
            cat, rows = fee_rows(conn, person)
    except Exception:
        return ""
    out = []
    for x in fees.prompts(rows):
        r, kind = x["row"], x["kind"]
        if kind not in kinds:
            continue
        c = cat["cards"][r["card"]]
        h = fees.how(r["card"])
        amount = f"S${r['last']['amount']:,.2f}" if r.get("last") and kind != "soon" else (money(r["fee"]) if r["fee"] else "its annual fee")

        def btn(action, label, quiet=True):
            return (f"<form method=post action=/fees/mark style='display:inline'><input type=hidden name=card value='{e(r['card'])}'>"
                    f"<input type=hidden name=action value={action}><input type=hidden name=back value='{e(back)}'>"
                    f"<button class='{'quiet ' if quiet else ''}sm'>{label}</button></form> ")
        if kind == "ask":
            head = f"{e(c['name'])} charged {amount} on {r['last']['date']:%d %b}. Ask for a waiver?"
            acts = btn("asked", "I've asked", quiet=False) + btn("waived", "It was waived") + btn("paid", "I'm paying it")
        elif kind == "chase":
            head = f"{e(c['name'])}: you asked for a waiver on {datetime.date.fromisoformat(r['asked']):%d %b} and no reversal has shown. Chase it?"
            acts = btn("waived", "It was waived") + btn("paid", "I'm paying it") + btn("reset", "Start again")
        else:
            head = f"{e(c['name'])}: {amount} is due around {r['due']:%d %b} ({r['days']} days)."
            acts = ""
        p = r.get("progress")
        why = ""
        if p and p["status"] == "met":
            why = f"You've spent {money(p['spent'])}, past the {money(p['need'])} the bank asks for, so it should be waived anyway."
        elif p and p["status"] in ("short", "on_pace"):
            why = f"The bank waives it with {money(p['need'])} of spend; you're at {money(p['spent'])}."
        steps = ""
        if h:
            steps = (f"<p><b>How:</b> {e(h['steps'])} {e(h['after'])} <a href='{e(h['url'])}' target=_blank rel='noopener noreferrer'>"
                     f"{e({'citi': 'Citi', 'maybank': 'Maybank'}.get(c['bank_id'], c['bank_id'].upper()))}'s guide ↗</a></p>")
            if h["before"]:
                steps = f"<p class=warn>{e(h['before'])}</p>" + steps
        if kind == "soon":
            steps = (f"<p class=muted>{e(why) + ' ' if why else ''}Nothing to do yet: the app tells you when it's charged, and that is when "
                     "to ask." + "</p>") + (f"<details><summary>How to ask this bank</summary>{steps}</details>" if steps else "")
            why = ""
        out.append(f"<div class='panel fee {kind}'><div class=label>Annual fee</div><p class=say><b>{head}</b></p>"
                   f"{'<p class=muted>' + e(why) + '</p>' if why else ''}{steps}"
                   f"{'<div>' + acts + '</div>' if acts else ''}"
                   f"{'' if back == '/fees' else '<p><small><a href=/fees>All fees ›</a></small></p>'}</div>")
    return "".join(out)


def fees_page(conn, q, person):
    """Annual fees: what each card costs, when it's next due, what the bank's terms say about a waiver and whether
    your spend meets them."""
    try:
        cat, rows = fee_rows(conn, person)
    except crypto.Unavailable:
        return "Fees", "<h1>Annual fees</h1>" + spendweb.unavailable()
    if not rows:
        return "Fees", "<h1>Annual fees</h1><div class=panel><p>Pick the cards you hold first.</p><a class=btn href=/settings>Choose your cards</a></div>"
    ph = cat["photos"]
    known = [r for r in rows if r["fee"]]
    total = sum(r["fee"] for r in known)
    nxt = next((r for r in rows if r["due"] and r["fee"] != 0), None)
    undated = sum(1 for r in rows if not r["due"] and r["fee"] != 0)
    todo = [r for r in rows if r["chase"] in ("ask", "asked")]
    body = (f"<h1>Annual fees</h1><div class=grid><div class=tile><b>{money(total)}</b><span>a year if nothing is waived ({len(known)} cards with a fee)</span></div>"
            + (f"<div class=tile><b>{nxt['due']:%d %b}</b><span>next: {e(cat['cards'][nxt['card']]['name'])} · in {nxt['days']} days</span></div>" if nxt else
               "<div class=tile><b>?</b><span>no fee date known yet</span></div>") + "</div>")
    SRC = {"yours": "your figure", "charged": "last charged", "list": "list price", "bank": "bank's fee page"}

    def btn(cid, action, label, quiet=True):
        return (f"<form method=post action=/fees/mark style='display:inline'><input type=hidden name=card value='{e(cid)}'>"
                f"<input type=hidden name=action value={action}><button class='{'quiet ' if quiet else ''}sm'>{label}</button></form> ")
    items = []
    for r in rows:
        c = cat["cards"][r["card"]]
        fee = "not known" if r["fee"] is None else ("free" if r["fee"] == 0 else money(r["fee"]))
        if r["due"]:
            due = (f"{r['due']:%d %b %Y}" if r["due_src"] == "statement" else f"{r['due']:%B %Y}")
            how = (f"a year after the fee charged on {r['last']['date']:%d %b %Y}" + (", which was reversed" if r["waived_last"] else "")
                   if r["due_src"] == "statement" else "the month you gave")
            when = f"<small class=w>Next fee: <b>{due}</b> ({how})</small>"
        else:
            cur = str(((person.get("fee_dates") or {}).get(r["card"]) or {}).get("month", ""))
            opts = "<option value=''>month…</option>" + "".join(
                f"<option value={i + 1}{' selected' if cur == str(i + 1) else ''}>{m[:3]}</option>" for i, m in enumerate(fees.MONTHS))
            when = ("<small class=w>Next fee: not seen on a statement yet. It's picked up by itself when it posts.</small>"
                    f"<details><summary>I know the month</summary><form method=post action=/fees/mark class=f data-auto>"
                    f"<input type=hidden name=card value='{e(r['card'])}'><input type=hidden name=action value=month>"
                    f"<select name=month aria-label='Fee month'>{opts}</select><noscript><button class=sm>Save</button></noscript></form></details>")
        w = r["waiver"]
        rule = ""
        if w and w.get("quote"):
            rule = (f"<div class=q>“{e(advise.short(w['quote'], 260))}”" + (f" <a href='{e(w['url'])}' rel=noreferrer>bank document</a>" if w.get("url") else "") + "</div>")
        p = r["progress"]
        prog = ""
        if p and p["status"] != "unknown":
            pct = 100 * p["spent"] / p["need"] if p["need"] else 0
            prog = (f"<small class=w>{money(p['spent'])} of {money(p['need'])} since {p['since']:%d %b %Y}</small>"
                    + bar(pct, "" if p["status"] in ("met", "on_pace") else "hot"))
        chip, acts = "", ""
        if r["chase"] == "ask":
            chip = "<span class='chip warn'>ask for a waiver</span>"
            acts = btn(r["card"], "asked", "I've asked", quiet=False) + btn(r["card"], "waived", "It was waived") + btn(r["card"], "paid", "I'm paying it")
        elif r["chase"] == "asked":
            chip = "<span class='chip warn'>waiver asked</span>"
            acts = btn(r["card"], "waived", "It was waived") + btn(r["card"], "paid", "I'm paying it") + btn(r["card"], "reset", "Undo")
        elif r["chase"] == "waived":
            chip = "<span class='chip ok'>waived last time</span>"
        elif r["chase"] == "paid":
            chip = "<span class=chip>paid this year</span>"
            acts = btn(r["card"], "reset", "Undo")
        elif r["fee"] == 0:
            chip = "<span class='chip ok'>free</span>"
        elif p and p["status"] in ("met", "on_pace"):
            chip = f"<span class='chip ok'>{'waiver met' if p['status'] == 'met' else 'on pace for the waiver'}</span>"
        elif p and p["status"] == "short":
            chip = "<span class='chip warn'>short of the waiver</span>"
        elif r["days"] is not None and r["days"] <= 45:
            chip = f"<span class='chip warn'>due in {r['days']} d</span>"
        items.append(f"<div class='row wrapt' style='align-items:flex-start'><a href='/card/{e(c['id'])}' style='margin-top:4px'>{look.face(c, 'sm', ph.get(c['id']))}</a>"
                     f"<span class=grow><span style='display:flex;justify-content:space-between;gap:10px;align-items:baseline'><b>{e(c['name'])}</b>"
                     f"<b style='white-space:nowrap'>{fee}</b></span>{chip}<small style='display:inline'> {SRC.get(r['fee_src'], '')}</small>"
                     f"{when}{'<small class=w>' + e(fees.advice(r)) + '</small>' if (r['due'] or r['waiver'] or r['progress']) else ''}{prog}{rule}"
                     f"{'<div style=margin-top:8px>' + acts + '</div>' if acts else ''}</span></div>")
    body += fee_prompts(conn, person, cat, rows)
    body += "<div class=list>" + "".join(items) + "</div>"
    if undated:
        body += (f"<div class=hint>{undated} card(s) have no fee date yet: none has posted on a statement the app has seen. "
                 "It's picked up by itself when it does.</div>")
    body += ("<p class=muted style='margin-top:18px'><small>Waiver rules are quoted only from documents on the bank's own website. Where the "
             "bank writes none, a waiver is at its discretion. Spend is counted from the last fee charge, or over the months on file. "
             "The fee amount is the bank's; whether you actually paid is read from your statements (a reversal = waived). You get a reminder "
             "30 days before a fee is due and again when it's charged, so you can ask for the waiver.</small></p>")
    return "Fees", body


def catalogue(conn, q, person):
    bank, kind, text = q.get("bank", ""), q.get("kind", ""), q.get("q", "").strip()
    show_all = q.get("all") == "1"
    mine = q.get("mine") == "1"
    sql = ("SELECT c.*, b.name bank, (SELECT json_agg(json_build_object('rate', r.rate, 'label', coalesce(r.label, r.bucket),"
           " 'cap', r.cap) ORDER BY r.rate DESC) FROM card_rules r WHERE r.card_id = c.id AND r.valid_to IS NULL) rules,"
           " (SELECT max(bonus) FROM signup_offers s WHERE s.card_id = c.id AND s.active) offer"
           " FROM cards c JOIN banks b ON b.id = c.bank_id WHERE c.status = 'active'")
    args = []
    if bank:
        sql += " AND c.bank_id = %s"; args.append(bank)
    if kind:
        sql += " AND c.kind = %s"; args.append(kind)
    if text:
        sql += " AND c.name ILIKE %s"; args.append(f"%{text}%")
    if mine:
        sql += " AND c.id = ANY(%s)"; args.append(list(person["held_cards"]) or [""])
    elif not show_all and person["excluded_banks"]:
        sql += " AND NOT (c.bank_id = ANY(%s))"; args.append(person["excluded_banks"])
    rows = conn.execute(sql + " ORDER BY b.name, c.name", args).fetchall()
    banks = conn.execute("SELECT id, name FROM banks ORDER BY name").fetchall()
    opts = "".join(f"<option value={e(b['id'])} {'selected' if b['id'] == bank else ''}>{e(b['name'])}</option>" for b in banks)
    kopts = "".join(f"<option {'selected' if k == kind else ''}>{k}</option>" for k in ("miles", "cashback", "points"))
    n_mine = len(person["held_cards"] or [])
    body = ("<h1>Cards</h1><div class=seg style='max-width:340px'>"
            f"<label><a href=/catalogue style='display:block;text-decoration:none;color:inherit'><input type=radio disabled {'' if mine else 'checked'}><span>All cards</span></a></label>"
            f"<label><a href='/catalogue?mine=1' style='display:block;text-decoration:none;color:inherit'><input type=radio disabled {'checked' if mine else ''}><span>My cards ({n_mine})</span></a></label></div>"
            f"<form class=f data-auto>{'<input type=hidden name=mine value=1>' if mine else ''}<input class=search name=q type=search placeholder='Search cards' "
            f"value='{e(text)}'><select name=bank><option value=''>All banks</option>{opts}</select><select name=kind>"
            f"<option value=''>Any type</option>{kopts}</select></form>"
            "<form id=cmp method=get action=/compare></form>"
            "<p class=muted style='margin:8px 0'>Tick cards to put them side by side. "
            "<button form=cmp class='quiet sm'>Compare ticked</button>"
            + (" <a href='/compare?mine=1'>Compare all my cards →</a>" if n_mine > 1 else "") + "</p>")
    if person["excluded_banks"] and not show_all and not mine:
        body += f"<p class=muted>Hiding {', '.join(map(e, person['excluded_banks']))} (your settings). <a href='?all=1'>Show all</a></p>"
    held = set(person["held_cards"])
    ph = photos(conn)
    tcs = tc_counts(conn)
    items = []
    for r in rows:
        rules = r["rules"] or []
        unit = "%" if r["kind"] == "cashback" else " mpd"
        best = " · ".join(f"{num(x['rate'])}{unit} {e(x['label'])}" for x in rules[:2]) or (
            f"{num(r['base_sgd'])}{unit} everywhere" if r["base_sgd"] is not None else "rates not known yet")
        badges = ("<span class='chip ok'>yours</span>" if r["id"] in held else "") + \
                 (f"<span class=chip>{num(r['offer'], 0)} sign-up</span>" if r["offer"] else "") + \
                 (tc_chip(tcs.get(r["id"])) or ("<span class=chip>unchecked</span>" if r["depth"] != "full" else ""))
        items.append("<div style='display:flex;align-items:center;gap:6px'>"
                     f"<input type=checkbox form=cmp name=c value='{e(r['id'])}' aria-label='Compare {e(r['name'])}' "
                     f"style='flex:none;width:20px;height:20px;margin-left:10px'{' checked' if mine else ''}>"
                     f"<a class=row style='flex:1;min-width:0' href='/card/{e(r['id'])}'>{look.face(r, 'sm', ph.get(r['id']))}<span class=grow>"
                     f"<b>{e(r['name'])}</b> {badges}<small>{e(r['bank'])} · {best}</small></span>"
                     f"<span class='r muted' style='font-weight:400;font-size:14px'>{money(r['annual_fee'])}</span></a></div>")
    none = ("<div class=row>You have not picked your cards yet. <a href=/settings>Settings →</a></div>" if mine and not n_mine
            else "<div class=row>No cards</div>")
    body += (f"<div class=list>{''.join(items) or none}</div>"
             f"<p class=muted style='margin-top:12px'>{len(rows)} cards · miles per S$ (cashback in %)</p>")
    return "Catalogue", body


def compare(conn, q, person, picked=()):
    """Cards side by side: what each earns for the usual kinds of purchase (today's rules, the person's way of paying
    in shops, caps not counted), then fee, sign-up offer and where the points go. picked = card ids; mine=1 = held.
    Built to fit many cards in one window: the table uses the whole window width, narrow columns, the kind of
    purchase stays in view when it scrolls sideways on a phone."""
    held = set(person["held_cards"] or [])
    ids = list(dict.fromkeys(picked)) or (sorted(held) if q.get("mine") == "1" else [])
    cat = cache.catalogue(conn)
    cards = [cat["active"][i] for i in ids if i in cat["active"]][:24]
    body = "<h1>Compare cards</h1>"
    if len(cards) < 2:
        return "Compare", (body + "<div class=panel><p>Pick two or more cards to see them side by side.</p>"
                           "<p><a class=btn href=/catalogue>Choose cards →</a>"
                           + (" <a class='btn quiet' href='/compare?mine=1'>Compare all my cards</a>" if len(held) > 1 else "") + "</p></div>")
    from . import replay
    from .notify import PURCHASES
    cards.sort(key=lambda c: (c["id"] not in held, c["bank"], c["name"]))   # the person's own cards first
    ph = photos(conn)
    store = replay.in_store(person)
    qs = lambda keep: "&".join("c=" + urllib.parse.quote(c["id"]) for c in keep)
    short = lambda name, bank: name[len(bank):].strip() if name.lower().startswith(bank.lower() + " ") and len(name) > len(bank) + 3 else name
    head = "<th></th>" + "".join(
        f"<th>{look.face(c, 'sm', ph.get(c['id']))}<a href='/card/{e(c['id'])}' title='{e(c['name'])}'>{e(short(c['name'], c['bank']))}</a>"
        f"<br><span class=x>{e(c['bank'])}{' · yours' if c['id'] in held else ''}</span>"
        f"<br><a class=x href='/compare?{qs([x for x in cards if x['id'] != c['id']])}' aria-label='Remove {e(c['name'])}'>remove</a></th>"
        for c in cards)
    rows = []
    today = datetime.date.today()
    for label, mcc, pay, fcy in PURCHASES:
        pay = store if pay == "store" else pay
        ranked = {x["card"]["id"]: x for x in engine.rank(conn, {"mcc": mcc, "pay": pay, "fcy": fcy, "merchant": "", "today": today},
                                                          held, (), person["boost_amounts"])}
        got = [ranked.get(c["id"]) for c in cards]
        top = max((x["value"] for x in got if x), default=0)
        cells = []
        for x in got:
            if not x:
                cells.append("<td class=muted>–</td>")
                continue
            r = x["rule"]
            cap = x["extra"].get("cap", r["cap"] if r else None)
            bits = ([f"cap {money(cap)}"] if cap else []) + ([f"min {money(r['min_spend'])}"] if r and r["min_spend"] else [])
            why = advise.label(r) if r else ""
            best = x["value"] >= top - 1e-9 and top > 0
            unit = "%" if x["unit"] == "%" else ""
            cells.append(f"<td{' class=best' if best else ''} title='{e(why or x['note'])}'><b>{num(x['rate'])}{unit}</b>"
                         + (f"<small>{e(' · '.join(bits))}</small>" if bits else "") + "</td>")
        rows.append(f"<tr><th>{e(label)}</th>{''.join(cells)}</tr>")
    offer = {r["card_id"]: r["b"] for r in conn.execute(
        "SELECT card_id, max(bonus) b FROM signup_offers WHERE active AND card_id = ANY(%s) GROUP BY card_id", ([c["id"] for c in cards],)).fetchall()}
    progs = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM programmes").fetchall()}
    def line(title, f):
        return f"<tr><th>{title}</th>" + "".join(f"<td>{f(c)}</td>" for c in cards) + "</tr>"
    rows.append(line("Annual fee", lambda c: money(c["annual_fee"]) if c["annual_fee"] is not None else "<span class=muted>?</span>"))
    if offer:
        rows.append(line("Sign-up offer", lambda c: f"{num(offer[c['id']], 0)}" if offer.get(c["id"]) else "<span class=muted>–</span>"))
    rows.append(line("Points go to", lambda c: e(progs.get(c.get("programme_id"), c["kind"]))))
    how = {"mobile": "phone or watch", "tap": "the physical card"}[store]
    body += (f"<p class=muted>{len(cards)} cards, miles per S$ for each kind of purchase (a % is cashback), in shops paying by {how} "
             "(<a href=/settings#pay>change</a>). The best in each row is shaded. A cap or minimum spend is shown under the rate, not "
             "counted: what is left this month is on <a href=/wallet>Wallet</a>. Hover or press a rate to see which rule gives it.</p>"
             f"<div class='wrap cmpwrap'><table class=cmp><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
             f"<p style='margin-top:12px'><a class='btn quiet' href=/catalogue>Add or change cards →</a></p>")
    return "Compare", body


def earn_table(c, rules):
    """What the card earns, one row per kind of spend: everything else in S$, foreign currency, then each bonus (a
    tiered bonus gets a row per tier). Cap and minimum are columns on a wide screen and a line under the name on a phone."""
    cash = c["kind"] == "cashback"
    ppm = float(c["points_per_mile"]) if c.get("points_per_mile") and not cash and float(c["points_per_mile"]) != 1 else None
    prog = e(c["programme"] or "Points")

    def row(what, rate, cap="", mn="", notes=()):
        if rate is None:
            vals = "<td class=n>?</td>" + ("<td class=n>?</td>" if ppm else "")
        else:
            r = float(rate)
            vals = f"<td class=n><b>{num(r)}{'%' if cash else ''}</b></td>" + (f"<td class=n>{num(round(r * ppm, 2))}</td>" if ppm else "")
        limits = " · ".join(x for x in (f"cap {cap}" if cap else "", f"minimum {mn}" if mn else "") if x)
        small = "".join(f"<small class=w>{n}</small>" for n in notes if n) + (f"<small class=ph>{limits}</small>" if limits else "")
        return f"<tr><td>{what}{small}</td>{vals}<td class='n dk'>{cap or 'none'}</td><td class='n dk'>{mn or 'none'}</td></tr>"

    rows = [row("Everything else, in S$", c["base_sgd"], notes=["base rate"]), row("Foreign currency", c["base_fcy"], notes=["base rate"])]
    for r in rules:
        per = {"statement": "statement month", "calendar": "month", "membership_year": "year"}.get(r["cap_period"], r["cap_period"] or "")
        cap = f"{money(r['cap'])} a {e(per)}" if r["cap"] else ""
        cond = r["conditions"] if r["conditions"] and r["conditions"] != r["label"] else ""
        tiers = re.findall(r"S\$([\d,]+)\+ -> ([\d.]+) mpd", cond or "")
        cond = re.sub(r";?\s*tiers:.*$", "", cond or "")
        notes = [e(cond), f"until {r['promo_until']:%d %b %Y}" if r["promo_until"] else "", "unchecked" if r["depth"] != "full" else ""]
        mn = f"{money(r['min_spend'])} a month on the card" if r["min_spend"] else ""
        rows.append(row(e(advise.label(r)), r["rate"], cap, mn, notes))
        for thr, rate in tiers[1:]:
            rows.append(row(e(advise.label(r)), rate, cap, f"S${thr} a month on the card", ["higher tier"]))
    head = (f"<th>Spend</th><th class=n>{'Cashback' if cash else 'Miles per S$'}</th>" + ("<th class=n>Points per S$</th>" if ppm else "") +
            "<th class='n dk'>Cap</th><th class='n dk'>Minimum spend</th>")
    foot = ("" if len(rules) else "<p class=muted>No bonus rules known: every purchase earns the base rate.</p>")
    if ppm:
        foot += f"<p class=muted><small>Points are {prog}: {num(ppm)} of them make 1 mile.</small></p>"
    return f"<h2>What it earns</h2><div class=wrap><table class=earn><tr>{head}</tr>{''.join(rows)}</table></div>{foot}"


def card_page(conn, cid, person):
    c = conn.execute("SELECT c.*, b.name bank, p.name programme, p.points_per_mile FROM cards c JOIN banks b ON "
                     "b.id = c.bank_id LEFT JOIN programmes p ON p.id = c.programme_id WHERE c.id = %s", (cid,)).fetchone()
    if not c:
        return None
    rules = conn.execute("SELECT r.*, %s kind FROM card_rules r WHERE card_id = %s AND valid_to IS NULL ORDER BY rate DESC",
                         (c["kind"], cid)).fetchall()
    mine = cid in person["held_cards"]
    img = conn.execute("SELECT source, source_url, added_by, updated_at FROM card_images WHERE card_id = %s", (cid,)).fetchone()
    has_photo = str(int(img["updated_at"].timestamp())) if img else None
    if img and img["source"] == "search":
        dom = urllib.parse.urlsplit(img["source_url"] or "").netloc.removeprefix("www.")
        photo_note = (f"<p><small>Picture found online ({e(dom)}). <form method=post action=/card/image enctype=multipart/form-data "
                      f"style='display:inline'><input type=hidden name=card value='{e(cid)}'><input type=hidden name=remove value=1>"
                      f"<button class='quiet sm'>Wrong picture? Remove</button></form></small></p>")
    elif img:
        photo_note = (f"<p><small>Photo added by {e(img['added_by'] or 'the household')}. <form method=post action=/card/image "
                      f"enctype=multipart/form-data style='display:inline'><input type=hidden name=card value='{e(cid)}'>"
                      f"<input type=hidden name=remove value=1><button class='quiet sm'>Remove</button>"
                      f"</form></small></p>")
    else:
        photo_note = ""
    fee_now = c["annual_fee"] if c["annual_fee"] is not None else cache.catalogue(conn)["cards"].get(cid, {}).get("bank_fee")
    facts = [f"Annual fee {money(fee_now) if fee_now is not None else 'not known'} {e(c['fee_waiver'] or '')}"]
    ext = " target=_blank rel='noopener noreferrer'"
    listing = conn.execute("SELECT listing_url FROM banks WHERE id = %s", (c["bank_id"],)).fetchone()["listing_url"]
    more = []
    if c["url"]:
        more.append(f"<a class=chip href='{e(c['url'])}'{ext}>{e(c['bank'])}: this card ↗</a>")
    elif listing:
        more.append(f"<a class=chip href='{e(listing)}'{ext}>{e(c['bank'])}: all cards ↗</a>")
    if c.get("review_url"):
        more.append(f"<a class=chip href='{e(c['review_url'])}'{ext}>MileLion review ↗</a>")
    more.append(f"<a class=chip href='https://milelion.com/?s={urllib.parse.quote_plus(c['name'])}'{ext}>"
                f"{'More on' if c.get('review_url') else 'Search'} MileLion ↗</a>")
    if c["tc_url"]:
        more.append(f"<a class=chip href='{e(c['tc_url'])}'{ext}>Bank T&amp;C ↗</a>")
    more = f"<p class=links>{' '.join(more)}</p>"
    body = (f"<div class=pick>{look.face(c, 'lg', has_photo)}<div class=info><div class=label>{e(c['bank'])} · {e(c['kind'])}</div>"
            f"<div class=name>{e(c['name'])}</div><p class=muted>{e(c['programme'] or '')}"
            f"{(' · ' if c['programme'] else '') + '<span class=chip>unchecked</span>' if c['depth'] != 'full' else ''}</p><p><small>{' · '.join(facts)}</small></p>{more}"
            f"{photo_note}<form method=post action=/hold style='display:inline'><input type=hidden name=card value='{e(cid)}'><button"
            f"{' class=quiet' if mine else ''}>{'✓ In my wallet' if mine else 'Add to my wallet'}</button></form>"
            f"<details><summary>{'Change' if has_photo else 'Add'} photo</summary><form method=post action=/card/image "
            f"enctype=multipart/form-data class=f><input type=hidden name=card value='{e(cid)}'><input type=file name=image "
            f"accept='image/png,image/jpeg,image/webp' required><button>Upload</button>"
            "</form>"
            "<small>A photo or screenshot of the card front, up to 2 MB. Shared by the household.</small></details></div></div>")
    body += earn_table(c, rules)
    if c["notes"]:
        body += f"<details><summary>Notes on this card</summary><p class=muted>{e(c['notes'])}</p></details>"
    from . import advisorweb
    body += advisorweb.verdict_box(conn, cid=cid, person=person)
    body += boosts_section(conn, cid, person, rules)
    body += advisorweb.tc_section(conn, cid, {r["bucket"]: advise.label(r) for r in rules})
    # MCC table
    mrows = conn.execute(
        "SELECT m.mcc, m.effect, m.rate, r.bucket, r.label, r.rate rrate, mc.name FROM card_rule_mccs m JOIN card_rules r "
        "ON r.id = m.rule_id LEFT JOIN mcc_codes mc ON mc.mcc = m.mcc WHERE r.card_id = %s AND r.valid_to IS NULL "
        "ORDER BY m.effect, m.mcc", (cid,)).fetchall()
    excl = conn.execute("SELECT x.mcc, mc.name FROM card_mcc_exclusions x LEFT JOIN mcc_codes mc ON mc.mcc = x.mcc WHERE "
                        "card_id = %s AND valid_to IS NULL ORDER BY x.mcc", (cid,)).fetchall()
    unknown = [advise.label(r) for r in rules if not r["mcc_list_known"]]
    body += "<h2>MCCs</h2>"
    if unknown:
        body += f"<p class=muted>Matched by channel, merchant or category, not an MCC list: {', '.join(map(e, unknown))}.</p>"
    if mrows:
        body += (f"<details{' open' if len(mrows) <= 12 else ''}><summary>{len(mrows)} MCCs in its rules</summary><div class=wrap><table><tr><th>MCC</th><th>What</th><th>Rule</th><th>Effect</th><th class=n>Rate</th></tr>" +
                 "".join(f"<tr><td><a href='/mcc/{e(m['mcc'])}'>{e(m['mcc'])}</a></td><td>{e(m['name'] or '')}</td><td>"
                         f"{e(m['label'] or m['bucket'])}</td><td class={'ok' if m['effect'] == 'include' else 'warn'}>"
                         f"{e(m['effect'])}</td><td class=n>{num(m['rate'] if m['rate'] is not None else (m['rrate'] if m['effect'] == 'include' else None))}</td></tr>"
                         for m in mrows) + "</table></div></details>")
    if excl:
        chips, run = [], []

        def flush():
            if len(run) >= 6:   # a whole block the bank excludes (9000-9999 government): one chip, not a thousand
                chips.append(f"<span class=chip>{e(run[0]['mcc'])}–{e(run[-1]['mcc'])}</span>")
            else:
                chips.extend(f"<a class=chip href='/mcc/{e(x['mcc'])}' title='{e(x['name'] or '')}'>{e(x['mcc'])}</a>" for x in run)
            run.clear()

        for x in excl:
            if run and not (x["mcc"].isdigit() and run[-1]["mcc"].isdigit() and int(x["mcc"]) == int(run[-1]["mcc"]) + 1):
                flush()
            run.append(x)
        flush()
        body += f"<details><summary>{len(excl)} MCCs that earn nothing</summary><p>{' '.join(chips)}</p></details>"
    rep = conn.execute("SELECT * FROM mcc_reports WHERE card_id = %s ORDER BY reported DESC NULLS LAST LIMIT 50", (cid,)).fetchall()
    if rep:
        body += (f"<details><summary>{len(rep)} merchants reported on this card</summary><div class=wrap><table><tr><th>Merchant</th><th>MCC</th><th>Outcome</th><th>Source</th></tr>" +
                 "".join(f"<tr><td>{e(r['merchant'])}</td><td>{e(r['mcc'] or '')}</td><td>{e(r['outcome'])}</td><td>{e(r['source'])}</td></tr>"
                         for r in rep) + "</table></div></details>")
    offers = conn.execute("SELECT * FROM signup_offers WHERE (card_id = %s OR card_name = %s) AND active", (cid, c["name"])).fetchall()
    if offers:
        body += "<h2>Sign-up offer</h2>" + offers_table(offers, set(person["held_cards"]), cache.catalogue(conn))
    hist = conn.execute("SELECT * FROM catalogue_changes WHERE card_id = %s ORDER BY at DESC LIMIT 30", (cid,)).fetchall()
    if hist:
        body += f"<details><summary>History: {len(hist)} changes</summary>{changes_table(hist, card=False)}</details>"
    return c["name"], body


def boost_rows(conn, cid):
    return conn.execute("SELECT * FROM card_boosts WHERE card_id = %s ORDER BY id", (cid,)).fetchall()


def boosts_section(conn, cid, person, rules):
    """Boost tiers for this card, the person's own amount per boost, and a form to add a boost (shared)."""
    bs = boost_rows(conn, cid)
    amounts = person["boost_amounts"]
    labels = {r["bucket"]: (r["label"] or r["bucket"]) for r in rules}
    out = "<h2 id=boosts>Boosts</h2>"
    if not bs:
        out += "<p class=muted>No boost known for this card.</p>"
    for b in bs:
        where = ", ".join(labels.get(x, x) for x in b["applies_to"]) or "all bonus spend"
        have = amounts.get(str(b["id"]))
        val, thr = engine.tier_value(b["tiers"], have)
        tiers = "<div class=list>" + "".join(
            f"<div class=row><span class=grow>Keep S${float(t):,.0f}+ <small style='display:inline'>{e(b['requirement'] or '')}</small></span>"
            f"<span class=r>{'+' if b['mode'] == 'add' else ''}{num(v)} mpd{' ✓' if thr is not None and float(t) == thr else ''}</span></div>"
            for t, v in sorted(b["tiers"], key=lambda t: float(t[0]))) + "</div>"
        out += (f"<p><b>{e(b['label'])}</b> → {('adds to' if b['mode'] == 'add' else 'raises')} {e(where)}"
                f"{' · cap becomes ' + money(b['cap_override']) if b['cap_override'] else ''}"
                f"{' · <span class=chip>unchecked</span>' if b['depth'] != 'full' else ''}</p>{tiers}"
                f"<form method=post action=/boost/amount class=f style='margin-top:8px'><input type=hidden name=boost value={b['id']}>"
                f"<input type=hidden name=card value='{e(cid)}'><input name=amount inputmode=decimal placeholder='Your balance S$' "
                f"value='{'' if have is None else e(num(have, 0)).replace(',', '')}'><button>Save my balance</button></form>"
                f"<p class=muted>{'Your level: ' + ('+' if b['mode'] == 'add' else '') + num(val) + ' mpd' if val is not None else 'You are below the first tier.'}"
                + (f" Updated automatically from {'Wealthfolio' if person['boost_auto'][str(b['id'])].get('source') == 'wealthfolio' else 'a feeder'} "
                   f"({e(person['boost_auto'][str(b['id'])].get('month'))} average)."
                   if str(b["id"]) in person["boost_auto"] else "") + " Only you see your balance.</p>")
    out += ("<details><summary>Add a boost for this card</summary><form method=post action=/boost/new class=f "
            "style='flex-direction:column;margin-top:8px'>"
            f"<input type=hidden name=card value='{e(cid)}'><input name=label required maxlength=80 placeholder='What to hold, e.g. HSBC Everyday Global Account deposit'>"
            "<input name=requirement maxlength=80 placeholder='How it is measured, e.g. monthly average balance'>"
            "<input name=tiers required maxlength=200 placeholder='Tiers: amount=mpd, e.g. 10000=2, 50000=4'>"
            "<div class=seg><label><input type=radio name=mode value=add checked><span>Adds mpd</span></label>"
            "<label><input type=radio name=mode value=set><span>Rate becomes</span></label></div>"
            "<input name=cap inputmode=decimal placeholder='New cap S$ (optional)'>"
            "<input name=applies_to maxlength=120 placeholder='Only these rules (optional): " + e(", ".join(labels)) + "'>"
            "<button>Add boost</button></form></details>")
    return out


def mcc_page(conn, code, person):
    code = code.strip()
    if not code:
        body = "<h1>MCC lookup</h1><form class=f action=/mcc><input name=code placeholder='e.g. 5812' autofocus><button>Look up</button></form>"
        top = conn.execute("SELECT m.mcc, mc.name, count(DISTINCT r.card_id) n FROM card_rule_mccs m JOIN card_rules r ON "
                           "r.id = m.rule_id LEFT JOIN mcc_codes mc ON mc.mcc = m.mcc WHERE r.valid_to IS NULL AND "
                           "m.effect = 'include' GROUP BY m.mcc, mc.name ORDER BY n DESC, m.mcc LIMIT 60").fetchall()
        body += "<h2>MCCs most cards reward</h2><p>" + " ".join(
            f"<a class=chip href='/mcc/{e(t['mcc'])}'>{e(t['mcc'])} {e(t['name'] or '')} ({t['n']})</a>" for t in top) + "</p>"
        return "MCC", body
    name = conn.execute("SELECT name FROM mcc_codes WHERE mcc = %s", (code,)).fetchone()
    # per card: an include rule naming this MCC wins; a card-wide exclusion = 0; else base. A rule that also needs a
    # channel (online / contactless / mobile) or FCY is shown with that condition.
    cards = conn.execute("SELECT id, name, kind, base_sgd, bank_id, EXISTS (SELECT 1 FROM card_mcc_exclusions x WHERE "
                         "x.card_id = c.id AND x.mcc = %s AND x.valid_to IS NULL) excluded FROM cards c WHERE status = "
                         "'active'", (code,)).fetchall()
    hits = conn.execute("SELECT r.card_id, m.effect, coalesce(m.rate, r.rate) rate, coalesce(r.label, r.bucket) label, "
                        "r.cap, r.cap_period, r.match FROM card_rule_mccs m JOIN card_rules r ON r.id = m.rule_id WHERE "
                        "r.valid_to IS NULL AND m.mcc = %s", (code,)).fetchall()
    ex = set(person["excluded_banks"])
    rows = []
    for c in cards:
        if c["bank_id"] in ex:
            continue
        mine = [h for h in hits if h["card_id"] == c["id"]]
        base = 0.0 if c["excluded"] or any(h["effect"] == "exclude" for h in mine) else float(c["base_sgd"] or 0)
        why, rate, cond = ("<span class=warn>excluded</span>" if base == 0 else "base rate"), base, ""
        for h in sorted((h for h in mine if h["effect"] == "include"), key=lambda h: -float(h["rate"])):
            need = rule_condition(h["match"])
            if float(h["rate"]) > rate:
                rate, cond = float(h["rate"]), need
                why = e(h["label"]) + (f" (cap {money(h['cap'])})" if h["cap"] else "")
                if need:
                    why += f" <span class=chip>{e(need)} only</span> otherwise {num(base)}"
                break
        rows.append(dict(c, eff=rate, why=why, cond=cond))
    rows.sort(key=lambda r: (-r["eff"], r["cond"] != ""))
    held = set(person["held_cards"])
    body = (f"<h1>MCC {e(code)} {e(name['name'] if name else '')}</h1><p class=muted>Best local (SGD) rate per card for "
            "this MCC. Rules matched only by merchant name are not counted here.</p>"
            "<div class=wrap><table><tr><th>Card</th><th class=n>Rate</th><th>Why</th></tr>" +
            "".join(f"<tr><td><a href='/card/{e(r['id'])}'>{e(r['name'])}</a>{' <span class=chip>yours</span>' if r['id'] in held else ''}</td>"
                    f"<td class=n>{num(r['eff'])}</td><td>{r['why']}</td></tr>"
                    for r in rows) + "</table></div>")
    rep = conn.execute("SELECT r.*, c.name card FROM mcc_reports r LEFT JOIN cards c ON c.id = r.card_id WHERE r.mcc = %s "
                       "ORDER BY reported DESC NULLS LAST LIMIT 40", (code,)).fetchall()
    if rep:
        body += ("<h2>Merchants reported with this MCC</h2><div class=wrap><table><tr><th>Merchant</th><th>Card</th><th>Outcome</th></tr>" +
                 "".join(f"<tr><td>{e(r['merchant'])}</td><td>{e(r['card'] or '')}</td><td>{e(r['outcome'])}</td></tr>" for r in rep) +
                 "</table></div>")
    return f"MCC {code}", body


PAY = [("mobile", "Apple Pay"), ("tap", "Tap card"), ("online", "Online / in app"), ("insert", "Insert")]
METHODS = [("mobile", "Phone or watch only"), ("mixed", "Phone, sometimes the card"), ("tap", "The physical card")]


def pay_choices(person):
    """The ways to pay offered on Pay, and the default in a shop. Someone who never uses the physical card isn't shown
    'Tap card' or 'Insert' at all."""
    m = person.get("pay_method")
    if m == "mobile":
        return [x for x in PAY if x[0] in ("mobile", "online")], "mobile"
    return PAY, ("tap" if m == "tap" else "mobile")


def price(v):
    """The optional purchase price on Which card?: S$ > 0 and sane, else None."""
    try:
        a = round(float(str(v or "").replace(",", "").replace("$", "").strip()), 2)
    except ValueError:
        return None
    return a if 0 < a <= 1_000_000 else None


def earned(x):
    a = x["amt"]
    return f"S${a['earned']:,.2f}" if x["card"]["kind"] == "cashback" else f"{a['earned']:,.0f}"


def earned_unit(x):
    return "back" if x["card"]["kind"] == "cashback" else "miles"


def split_hint(b, rest, fcy):
    """The best card's bonus limit runs out part way through this purchase: if the shop splits the bill, the overflow
    is better on the next card. Only worth saying for 50+ miles."""
    a = b.get("amt")
    if not a or a["over"] <= 0.005 or b["card"]["kind"] == "cashback":
        return ""
    best, gain = None, 0.0
    for y in rest:
        if y["card"]["kind"] == "cashback":
            continue
        g = engine.for_amount(y, a["over"], fcy)["earned"] - a["over"] * a["base"]
        if g > gain:
            best, gain = y, g
    if best is None or gain < 50:
        return ""
    return (f"<div class=hint>💡 Split the bill: {money(a['bonus'])} on {e(b['card']['name'])} and {money(a['over'])} on "
            f"{e(best['card']['name'])} earns about {gain:,.0f} more miles than putting it all on one card.</div>")


def atome_slug(conn, name):
    """The Atome shop page for this shop name, or None. Atome's slugs are the name in lower case with dashes
    ('Marks & Spencer' -> marks-spencer); also tried: the name's first words ('Gain City Megastore' -> gain-city)."""
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("&", " ").replace("'", "")).split()
    words = [w for w in words if w not in ("singapore", "sg", "pte", "ltd")]
    keys = ["-".join(words[:n]) for n in range(len(words), 0, -1)]
    if not keys:
        return None
    rows = {r["slug"] for r in conn.execute("SELECT slug FROM atome_merchants WHERE slug = ANY(%s)", (keys,)).fetchall()}
    return next((k for k in keys if k in rows), None)


ATOME_TIP = ("Atome charges your card as an online purchase under shop type 5999, one third now and one third in each of "
             "the next two months. Pay the 2nd and 3rd parts yourself in the Atome app: some banks treat Atome's automatic "
             "billing as a recurring payment and give nothing.")


# cards whose bank terms reward MCC 5999 online but where readers report Atome payments not earning the bonus
# (The MileLion, "What's the best card to use for Atome?", 8 Feb 2026)
ATOME_MIXED = {"uob_preferred_platinum"}


def atome_panel(conn, person, slug, amt, direct, room, shop_mcc=None):
    """Paying with Atome instead. Atome changes the shop type: the card sees MCC 5999 online, not the shop's own MCC,
    so a shop type a card excludes or doesn't reward can earn the bonus through Atome. Without a price: the best rate
    through Atome against the best rate direct. With a price: the best card for each third (this month's bonus limit
    left for the 1st, the full limit for the next two months), against paying it all at once on the best card."""
    link = f"<a href='https://www.atome.sg/paylater-merchants/{e(slug)}' rel=noopener target=_blank>Atome lists this shop</a>"
    held = set(person["held_cards"])
    kind = conn.execute("SELECT name FROM mcc_codes WHERE mcc = %s", (shop_mcc or "",)).fetchone() if shop_mcc else None
    edu = ("<p class=hint>⚠️ Education shops: people report Atome payments to schools and classes not earning the "
           "bonus even though Atome's shop type is 5999.</p>") if shop_mcc and shop_mcc.startswith("82") else ""
    swap = ("Through Atome your card sees shop type 5999 (online shopping) instead of "
            + (f"{shop_mcc} {kind['name']}" if kind else f"shop type {shop_mcc}" if shop_mcc else "this shop's own type") + ".")
    if not amt:
        p0 = {"mcc": "5999", "pay": "online", "fcy": False, "currency": "", "merchant": "ATOME", "today": datetime.date.today()}
        r0 = [x for x in engine.rank(conn, p0, held, person["excluded_banks"], person["boost_amounts"], room)
              if x["held"] and x["card"]["kind"] != "cashback"]
        b0 = max(r0, key=lambda x: x["rate"], default=None)
        d0 = direct["rate"] if direct and direct["card"]["kind"] != "cashback" else None
        cmp = ""
        if b0:
            cmp = f" Best card that way: <b>{e(b0['card']['name'])}, {num(b0['rate'])} miles per S$</b>"
            if d0 is not None:
                cmp += f", against {num(d0)} paying direct with {e(direct['card']['name'])}"
            cmp += "."
        warn = ("<p class=hint>⚠️ " + e(b0["card"]["name"]) + ": the bank's terms count Atome's shop type, but people "
                "report Atome payments on this card not always earning the bonus.</p>") if b0 and b0["card"]["id"] in ATOME_MIXED else ""
        return (f"<div class=panel style='margin-top:16px'><p><b>Atome:</b> {link}: pay in 3, no interest. {e(swap)}{cmp} "
                f"Enter the price to see the miles for the whole purchase.</p><p class=muted><small>{e(ATOME_TIP)}</small></p>{warn}{edu}</div>")
    third = round(amt / 3, 2)
    p = {"mcc": "5999", "pay": "online", "fcy": False, "currency": "", "merchant": "ATOME", "today": datetime.date.today(),
         "amount": third}

    def best(rm):
        r = [x for x in engine.rank(conn, p, held, person["excluded_banks"], person["boost_amounts"], rm) if x["held"]
             and x["card"]["kind"] != "cashback"]
        return max(r, key=lambda x: x["amt"]["earned"], default=None)
    now, later = best(room), best(None)
    if not now or not later:
        return ""
    total = now["amt"]["earned"] + 2 * later["amt"]["earned"]
    same = now["card"]["id"] == later["card"]["id"]
    on = (f"all three on {e(now['card']['name'])}" if same else
          f"the 1st on {e(now['card']['name'])}, the next two on {e(later['card']['name'])}")
    has_d = bool(direct and direct.get("amt") and direct["card"]["kind"] != "cashback")
    d = direct["amt"]["earned"] if has_d else 0
    diff = total - d
    verdict = (f"<b>{diff:,.0f} more miles</b> than paying at once" if diff >= 1 else
               f"{-diff:,.0f} fewer miles than paying at once" if diff <= -1 else "the same miles as paying at once")
    mixed = [x["card"]["name"] for x in ((now,) if same else (now, later)) if x["card"]["id"] in ATOME_MIXED]
    warn = (f"<p class=hint>⚠️ {e(' and '.join(mixed))}: the bank's terms count Atome's shop type, but people report "
            "Atome payments on this card not always earning the bonus.</p>") if mixed else ""
    return (f"<div class=panel style='margin-top:16px'><p><b>Atome:</b> {link}. {e(swap)} 3 × {money(third)}, {on}: about "
            f"{total:,.0f} miles{', ' + verdict + ' on ' + e(direct['card']['name']) if has_d else ''}.</p>"
            f"<p class=muted><small>Plus Atome+ points (about 0.2 miles per S$). Next months' parts assume those months' "
            f"bonus limits are free. {e(ATOME_TIP)}</small></p>{warn}{edu}</div>")


def kris_hit(conn, name):
    """Kris+ shops for this shop name: (shown name, best KrisPay miles per S$, how many outlets) or None. Kris+ names
    carry the outlet in brackets; tried: the whole name, then its first words ('Esso Bukit Timah' -> esso)."""
    from .catalogue import kris_key
    words = kris_key(name).split("-")
    keys = ["-".join(words[:n]) for n in range(len(words), 0, -1) if words[0]]
    if not keys:
        return None
    rows = conn.execute("SELECT name, key, mpd FROM kris_merchants WHERE key = ANY(%s)", (keys,)).fetchall()
    for k in keys:
        mine = [r for r in rows if r["key"] == k]
        if mine:
            top = max(mine, key=lambda r: float(r["mpd"]))
            return (re.sub(r"\s*\(.*?\)", "", top["name"]).strip() or top["name"], float(top["mpd"]), len(mine),
                    min(float(r["mpd"]) for r in mine))
    return None


KRIS_TIP = ("Pay inside the Kris+ app (it uses Apple Pay or Google Pay there). The bank sees an online payment under "
            "the shop's own type, so a bonus for tapping your phone in a shop does not apply. KrisPay miles move to "
            "KrisFlyer 1:1 for free within 21 days; after that they are worth less and expire.")


def kris_panel(conn, person, hit, amt, p, direct, room):
    """Paying through Kris+: the card's miles for the same shop type paid online, plus the shop's KrisPay miles."""
    name, kmpd, n, low = hit
    held = set(person["held_cards"])
    q = dict(p, pay="online")
    r = [x for x in engine.rank(conn, q, held, person["excluded_banks"], person["boost_amounts"], room)
         if x["held"] and x["card"]["kind"] != "cashback"]
    b = max(r, key=lambda x: (x["amt"]["earned"] if amt else x["rate"]), default=None)
    rate_txt = f"{num(kmpd)} KrisPay mile{'' if kmpd == 1 else 's'} per S$" + (f" (outlets give {num(low)} to {num(kmpd)})" if n > 1 and low != kmpd else "")
    head = f"<b>Kris+:</b> {e(name)} is a Kris+ shop: {rate_txt} on top of your card's miles."
    if not b:
        return f"<div class=panel style='margin-top:16px'><p>{head}</p><p class=muted><small>{e(KRIS_TIP)}</small></p></div>"
    if amt:
        card_m = b["amt"]["earned"]
        total = card_m + kmpd * amt
        d = direct["amt"]["earned"] if direct and direct.get("amt") and direct["card"]["kind"] != "cashback" else None
        line = (f"Paying {money(amt)} through Kris+ with {e(b['card']['name'])}: about {card_m:,.0f} card miles + "
                f"{kmpd * amt:,.0f} KrisPay miles = <b>{total:,.0f} miles</b>")
        if d is not None:
            diff = total - d
            line += (f", {diff:,.0f} more than paying direct with {e(direct['card']['name'])}" if diff >= 1 else
                     f", {-diff:,.0f} fewer than paying direct with {e(direct['card']['name'])}" if diff <= -1 else
                     ", the same as paying direct")
    else:
        line = (f"Best card through Kris+: {e(b['card']['name'])}, {num(b['rate'])} + {num(kmpd)} = "
                f"<b>{num(b['rate'] + kmpd)} miles per S$</b>")
        if direct and direct["card"]["kind"] != "cashback":
            line += f", against {num(direct['rate'])} paying direct with {e(direct['card']['name'])}"
    return (f"<div class=panel style='margin-top:16px'><p>{head} {line}.</p>"
            f"<p class=muted><small>{e(KRIS_TIP)} Kris+ rates from MainlyMiles' monthly list.</small></p></div>")


def lookup(conn, q, person):
    """Which card? Shop name or MCC + how you pay -> best card in your wallet, then the best in the catalogue.
    Changing how you pay re-asks at once (app.js) and keeps the chosen shop + MCC."""
    text = q.get("q", "").strip()[:80]
    choices, in_shop = pay_choices(person)
    pay = q.get("pay") if q.get("pay") in dict(choices) else None
    fcy = q.get("fcy") == "1"
    cur = q.get("cur") if fcy and q.get("cur") in dict(CURRENCIES) else ""
    amt = price(q.get("amt"))
    merchant, mccs, matches = None, [], []
    if text and re.fullmatch(r"\d{4}", text):
        mccs = [text]
    elif text:
        if q.get("m"):
            merchant = conn.execute("SELECT * FROM merchants WHERE slug = %s", (q["m"],)).fetchone()
        if not merchant:
            matches = conn.execute("(SELECT * FROM merchants WHERE lower(name) = lower(%s) ORDER BY source <> 'household') UNION ALL "
                                   "(SELECT * FROM merchants WHERE name ILIKE %s AND lower(name) <> lower(%s) ORDER BY "
                                   "source <> 'household', length(name) LIMIT 8)", (text, f"%{text}%", text)).fetchall()
            if matches and (matches[0]["name"].lower() == text.lower() or len(matches) == 1):
                merchant = matches[0]
        if merchant:
            mccs = list(merchant["mccs"])
    if pay is None:   # the shop's own channel decides the default; in a shop, the way this person pays
        pay = "online" if merchant and merchant.get("channel") == "online" else in_shop
    chosen = q.get("mcc") if q.get("mcc") in mccs else (mccs[0] if mccs else None)
    hidden = (f"<input type=hidden name=m value='{e(merchant['slug'])}'>" if merchant else "") + \
             (f"<input type=hidden name=mcc value='{e(chosen)}'>" if merchant and chosen and len(mccs) > 1 else "")
    form = (f"<form class=stack method=get action=/ data-auto><input class=search type=search name=q value='{e(text)}' "
            "placeholder='Shop name: FairPrice, Shopee…' autocomplete=off enterkeyhint=search"
            f"{'' if text else ' autofocus'}>{hidden}<div class=seg>" +
            "".join(f"<label><input type=radio name=pay value={k} {'checked' if k == pay else ''}><span>{v}</span></label>"
                    for k, v in choices) +
            f"</div><label class=price>Price S$ <input name=amt type=text inputmode=decimal autocomplete=off enterkeyhint=go "
            f"data-go value='{e(f'{amt:,.2f}'.rstrip('0').rstrip('.') if amt else '')}' placeholder='optional'></label>"
            f"<label class=toggle><input type=checkbox name=fcy value=1 {'checked' if fcy else ''}> Overseas / foreign currency</label>" +
            ("<label class=toggle>Charged in <select name=cur><option value=''>another currency</option>" + "".join(
                f"<option value={k}{' selected' if k == cur else ''}>{v}</option>" for k, v in CURRENCIES) + "</select></label>"
             if fcy else "") + "</form>")
    body = "<h1>Which card?</h1>" + form
    if not text:
        body += fee_prompts(conn, person, back="/", kinds=("ask", "chase"))   # a fee merely coming up lives on Wallet
        if not person["held_cards"]:
            body += "<div class=panel><p>Pick the cards you hold first, so the answer uses your wallet.</p><a href=/settings>Choose your cards →</a></div>"
        return "Which card?", body
    held = set(person["held_cards"])
    ph = photos(conn)
    base_q = urllib.parse.urlencode({"q": text, "pay": pay, **({"fcy": "1"} if fcy else {}), **({"cur": cur} if cur else {}),
                                     **({"amt": f"{amt:g}"} if amt else {})})
    if not merchant and matches:
        body += "<h2>Which one?</h2><div class=list>" + "".join(
            f"<a class=row href='/?{base_q}&m={urllib.parse.quote(x['slug'])}'><span class=grow>{e(x['name'])}</span>"
            f"<span class=muted>{e(', '.join(x['mccs']) or 'MCC unknown')}</span></a>" for x in matches) + \
            f"<a class=row href='/merchant?name={urllib.parse.quote(text)}'><span class=grow>None of these: add “{e(text)}”</span>"\
            "<span class=muted>+</span></a></div>"
        return "Which card?", body
    p = {"mcc": chosen, "pay": pay, "fcy": fcy, "currency": cur, "merchant": merchant["name"] if merchant else text, "today": datetime.date.today(),
         "amount": amt}
    other_types = ""
    if merchant:
        ch = {"online": "Online", "in_person": "In store", "both": "Online + in store"}.get(merchant.get("channel") or "", "")
        kind = conn.execute("SELECT name FROM mcc_codes WHERE mcc = %s", (chosen or "",)).fetchone()
        bits = ([f"{e(kind['name'])} (shop type {e(chosen)})" if kind else f"shop type {e(chosen)}"] if chosen else ["shop type not known"]) + \
               ([ch] if ch else []) + (["added in this app"] if merchant["source"] == "household" else [])
        body += (f"<p class=muted><b style='color:var(--fg)'>{e(merchant['name'])}</b> · {' · '.join(bits)} · "
                 f"<a href='/merchant?slug={urllib.parse.quote(merchant['slug'])}'>Wrong? Fix it</a></p>")
        if len(mccs) > 1:
            kinds = {r["mcc"]: r["name"] for r in conn.execute("SELECT mcc, name FROM mcc_codes WHERE mcc = ANY(%s)", (mccs,)).fetchall()}
            other_types = (f"<details><summary>Different outlets of this shop are filed under {len(mccs)} shop types</summary>"
                           "<p class=muted><small>Banks decide the bonus by the shop type (MCC) the outlet is registered under. "
                           "Pick another if you know yours:</small></p><p class=links>" + "".join(
                f"<a class='chip{' ok' if c == chosen else ''}' href='/?{base_q}&m={urllib.parse.quote(merchant['slug'])}&mcc={c}'>"
                f"{c} {e((kinds.get(c) or '')[:28])}</a>" for c in mccs) + "</p></details>")
    elif chosen:
        n = conn.execute("SELECT name FROM mcc_codes WHERE mcc = %s", (chosen,)).fetchone()
        body += f"<p class=muted><b style='color:var(--fg)'>MCC {e(chosen)}</b> {e(n['name'] if n else '')}</p>"
    else:
        body += (f"<div class=panel><p><b>{e(text)}</b> isn't in the shop list yet, so its MCC is unknown. Only cards that "
                 f"name this shop count below.</p><a href='/merchant?name={urllib.parse.quote(text)}'>Add “{e(text)}” →</a></div>")
    ctx = wallet_context(conn, person)
    ranked = engine.rank(conn, p, held, person["excluded_banks"], person["boost_amounts"], ctx["room"] if ctx else None)
    mine = [x for x in ranked if x["held"]]
    # the same rate with no strings attached beats one that still needs a minimum spend
    mine.sort(key=lambda x: (-x["value"], bool(x["extra"].get("locked"))))
    if not person.get("cashback_too"):   # miles advice names miles cards; a cashback card the person holds still shows
        ranked = [x for x in ranked if x["held"] or x["card"]["kind"] != "cashback"]
    others = [x for x in ranked if not x["held"]]
    def shown(x):
        if x["rule"] is None and x["rate"] == 0 and x["note"] == "base rate" and x["card"]["base_sgd"] is None:
            return "?"
        return f"{num(x['rate'])}{x['unit']}"

    def rate_cell(x):
        if x.get("amt"):
            return f"{earned(x)}<small>{e(earned_unit(x))}</small>"
        t = shown(x)
        if t.endswith(" mpd"):
            return f"{t[:-4]}<small>miles per S$</small>"
        return f"{t[:-1]}%<small>cashback</small>" if t.endswith("%") else t

    progmap = {(pe["card"], pe["bucket"]): pe for pe in (ctx or {}).get("progress", [])}

    def stats(cells):
        return "<div class=stats>" + "".join(f"<div><small>{k}</small><b>{v}</b></div>" for k, v in cells) + "</div>"

    def meters(x, big=False):
        """Spent and left this period on the bonus this card would use, from the person's own transactions. big = the
        top pick: the numbers in boxes; otherwise one line each."""
        r = x["rule"]
        pe = progmap.get((x["card"]["id"], r["bucket"])) if r and x["held"] else None
        if not pe:
            return ""
        resets = (pe["end"] + datetime.timedelta(days=1)).strftime("%-d %b")
        per = "statement month" if pe.get("statement") else "month"
        out = ""
        if pe["cap"]:
            used, left, cap = pe["used"], pe["left"], pe["cap"]
            use = x.get("amt")
            now = (f" · this purchase uses {money(use['bonus'])} of it" if use and use["bonus"] > 0.005 else "")
            b_ = bar(100 * used / cap if cap else 0, 'hot' if left < 0.2 * cap else '')
            if big:
                out += (f"<p class=sh>Bonus limit this {per}</p>" + stats([("Spent", money(used)), ("Left", money(left)),
                        ("Limit", money(cap)), ("Resets", resets)]) + b_ +
                        (f"<p>This purchase uses {money(use['bonus'])} of what's left.</p>" if now else ""))
            else:
                out += (f"<p><b>{money(used)}</b> spent · <b>{money(left)}</b> left of the {money(cap)} bonus limit this {per}"
                        f"{now} · resets {resets}{b_}</p>")
        if pe["min_spend"]:
            done = pe["min_spend"] - pe["min_left"]
            txt = (f"Minimum {money(pe['min_spend'])} reached: <b>{money(pe['card_total'])}</b> spent this {per}"
                   if pe["min_left"] <= 0 else
                   f"Minimum {money(pe['min_spend'])}: <b>{money(done)}</b> spent · <b>{money(pe['min_left'])}</b> to go")
            if pe.get("shops_need") and pe.get("shops_left"):
                txt += f" · and {pe['shops_left']} more of its shops ({pe['shops_need']} different ones needed)"
            b_ = bar(100 * done / pe['min_spend'], 'acc')
            if big:
                out += (f"<p class=sh>Minimum spend this {per}: {money(pe['min_spend'])}</p>" + stats(
                    [("Spent", money(pe["card_total"] if pe["min_left"] <= 0 else done)),
                     ("To go", "reached ✓" if pe["min_left"] <= 0 else money(pe["min_left"]))]) + b_ +
                        (f"<p>{txt.split(' · and ', 1)[1].capitalize()}.</p>" if " · and " in txt else "") +
                        ("<p>This purchase reaches it.</p>" if x["extra"].get("unlocks") else ""))
            else:
                out += f"<p>{txt}{b_}</p>"
        return f"<div class=room>{out}</div>" if out else ""

    def why(x, room=False):
        """room=True: spent / left are shown as bars beside it, so they are left out here."""
        r = x["rule"]
        today_is = x["extra"].get("match")   # a card whose rate depends on today's match says how today stands
        a = x.get("amt")
        if not r:
            note = "base rate not known yet" if shown(x) == "?" else x["note"]
            if a and shown(x) != "?" and x["rate"]:
                note += f" · {shown(x)} on all of it"
            return f"{note} · {today_is}" if today_is else note
        bits = [advise.label(r)] + ([today_is] if today_is else [])
        if a and a["over"] > 0.005:
            unknown = not a["base"] and x["card"]["base_sgd"] is None
            bits.append(f"{money(a['bonus'])} at {num(x['rate'])}{x['unit'].strip()}, the other {money(a['over'])} is over "
                        "the bonus limit and earns " + ("the base rate (not known, counted as 0)" if unknown
                                                         else f"{num(a['base'])}{x['unit'].strip()}"))
        if x["extra"].get("unlocks"):
            bits.append(f"this purchase reaches its {money(r['min_spend'])} minimum")
        cap = x["extra"].get("cap", r["cap"])
        left = x["extra"].get("left")
        per = "statement month" if r["cap_period"] == "statement" else "month"
        if room:
            pass
        elif left is not None:
            bits.append(f"{money(left)} of its {money(cap)} bonus limit left this {per}")
        elif cap:
            bits.append(f"bonus on the first {money(cap)} a {per}")
        for bl in x["extra"].get("boosts", []):
            bits.append(f"boost: {bl}")
        if r["min_spend"] and not x["extra"].get("unlocks") and not room:
            ml = x["extra"].get("min_left")
            if not x["extra"].get("locked"):
                bits.append(f"{money(r['min_spend'])} minimum reached")
            elif ml:
                bits.append(f"only if you put {money(ml)} more on this card this {per} (it needs {money(r['min_spend'])})")
            elif ml is None:
                bits.append(f"only once {money(r['min_spend'])} is spent on this card in the {per}")
            else:
                bits.append(f"{money(r['min_spend'])} minimum reached")
            ns, sl = x["extra"].get("min_shops"), x["extra"].get("shops_left")
            if ns and x["extra"].get("locked"):
                bits.append(f"and {sl} more of its shops this {per} (it needs {ns} different ones)" if sl else
                            f"and purchases at {ns} different shops of its list in the {per}" if sl is None else "")
        if isinstance(r.get("match"), dict) and r["match"].get("link_only"):
            bits.append(r.get("conditions") or "only when booked through the bank's own link")
        if r["depth"] != "full":
            bits.append("not checked against the bank's terms")
        if x["extra"].get("used_up"):
            bits.append("bonus limit used up: " + ", ".join(x["extra"]["used_up"]))
        return " · ".join(bits)

    def row(x, extra=""):
        c = x["card"]
        m = meters(x)
        return (f"<a class='row wrapt' href='/card/{e(c['id'])}'>{look.face(c, 'sm', ph.get(c['id']))}<span class=grow><b>{e(c['name'])}</b>"
                f"<small style='white-space:normal'>{e(why(x, bool(m)))}{extra}</small>{m}</span><span class=r>{rate_cell(x)}</span></a>")

    direct = mine[0] if mine else None   # the best of the person's cards paying direct (kept for the Atome comparison)
    if mine and mine[0]["value"] == 0:
        body += ("<div class=panel style='margin-top:22px'><p><b>No bonus known here.</b></p><p class=muted>None of your cards "
                 "has a bonus for this shop or MCC, so use the one with the best everyday rate:</p></div>")
        mine.sort(key=lambda x: -float(x["card"]["base_sgd"] or 0))
        body += "<div class=list style='margin-top:14px'>" + "".join(row(x) for x in mine[:5]) + "</div>"
        mine = []
    elif mine:
        b = mine[0]
        rate_txt = shown(b)
        num_part, unit_part = (rate_txt[:-len(b["unit"])], b["unit"].strip()) if rate_txt.endswith(b["unit"]) else (rate_txt, "")
        unit_part = {"mpd": "miles per S$", "%": "% cashback"}.get(unit_part, unit_part)
        nb = b["extra"].get("next")
        hint = (f"<div class=hint>💡 With S${nb[1]:,.0f} in {e(nb[0])} this becomes {num(nb[2])} miles per S$. "
                f"<a href='/card/{e(b['card']['id'])}#boosts'>Set your balance</a></div>") if nb else ""
        if b.get("amt"):   # with a price: what this purchase earns, then the rate after any bonus limit
            num_part, unit_part = earned(b), earned_unit(b)
            sub = f"on {money(amt)}"
            if b["amt"]["over"] > 0.005 or b["extra"].get("unlocks"):
                sub += f" · works out at {num(b['amt']['eff'])} {'%' if b['card']['kind'] == 'cashback' else 'miles per S$'}"
        else:
            sub = ""
        body += (f"<div class='pick now'><a href='/card/{e(b['card']['id'])}'>{look.face(b['card'], 'lg', ph.get(b['card']['id']))}</a>"
                 f"<div class=info><div class=label>Use</div><div class=name>{e(b['card']['name'])}</div>"
                 f"<div class=rate>{num_part}<span>{unit_part}</span></div>{'<div class=ratesub>' + e(sub) + '</div>' if sub else ''}"
                 f"<p class=muted style='margin-top:8px'>{e(why(b, bool(meters(b))))}</p>{meters(b, True)}{hint}"
                 f"<p><small><a href='/card/{e(b['card']['id'])}'>About this card ›</a></small></p></div></div>")
        body += split_hint(b, mine[1:], fcy)
        if len(mine) > 1:
            body += "<h2>Your next best cards here</h2><div class=list>" + "".join(row(x) for x in mine[1:5]) + "</div>"
    else:
        body += "<div class=panel style='margin-top:22px'><a href=/settings>Choose your cards</a> to get an answer from your wallet.</div>"
    slug = atome_slug(conn, merchant["name"] if merchant else text) if not fcy and not re.fullmatch(r"\d{4}", text) else None
    if slug:
        body += atome_panel(conn, person, slug, amt, direct, ctx["room"] if ctx else None, chosen)
    hit = kris_hit(conn, merchant["name"] if merchant else text) if not fcy and not re.fullmatch(r"\d{4}", text) else None
    if hit:
        body += kris_panel(conn, person, hit, amt, p, direct, ctx["room"] if ctx else None)
    body += other_types
    for w in boost_warnings(conn, person):
        body += f"<div class=hint style='margin-top:12px'>⚠️ {e(w)}</div>"
    # every card for this purchase, best first (yours marked); "worth getting" = beats your best
    best_mine = mine[0]["value"] if mine else 0
    show_all = q.get("all") == "1"
    top = [x for x in ranked if x["value"] > 0]
    shown_list = top if show_all else top[:12]

    def top_row(i, x):
        c = x["card"]
        tag = ("<span class='chip ok'>yours</span>" if x["held"] else
               ("<span class=chip>worth getting</span>" if x["value"] > best_mine else ""))
        nb = x["extra"].get("next")
        extra = f" · up to {num(nb[2])} mpd with S${nb[1]:,.0f} in {e(nb[0])}" if nb and not x["held"] else ""
        return (f"<a class=row href='/card/{e(c['id'])}'><span class=muted style='width:22px;text-align:right'>{i}</span>"
                f"{look.face(c, 'sm', ph.get(c['id']))}<span class=grow><b>{e(c['name'])}</b> {tag}"
                f"<small style='white-space:normal'>{e(c['bank'])} · {e(why(x))}{extra}</small></span><span class=r>{rate_cell(x)}</span></a>")

    miles = [x for x in top if x["card"]["kind"] != "cashback"]
    miles.sort(key=lambda x: (-x["value"], not x["held"]))
    cash = sorted((x for x in top if x["card"]["kind"] == "cashback"), key=lambda x: -x["value"])
    where = (f"{'this MCC' if not merchant else 'this shop'}, paying by {dict(PAY)[pay]}{' overseas' if fcy else ''}"
             + (f", for {money(amt)} (after each of your cards' bonus limit left)" if amt else ""))
    if miles:
        lst = miles if show_all else miles[:12]
        better = sum(1 for x in miles if not x["held"] and x["value"] > best_mine)
        body += (f"<details{' open' if show_all else ''}><summary>Compare every miles card here"
                 f"{' (' + str(better) + ' you do not hold earn more)' if better else ''}</summary>"
                 f"<p class=muted>Ranked by {'miles earned' if amt else 'miles per S$'} for {where}. Your excluded banks are left out.</p><div class=list>" +
                 "".join(top_row(i + 1, x) for i, x in enumerate(lst)) + "</div>")
        if not show_all and len(miles) > 12:
            body += (f"<p style='margin-top:10px'><a href='/?{base_q}{'&m=' + urllib.parse.quote(merchant['slug']) if merchant else ''}"
                     f"&all=1'>Show all {len(miles)} →</a></p>")
        body += "</details>"
    if cash:
        lst = cash if show_all else cash[:5]
        body += ("<details><summary>Compare cashback cards here</summary><p class=muted>Cashback % for comparison (1% is about "
                 "0.67 miles per S$ at 1.5¢ a mile). Most have small monthly limits and minimum spends.</p><div class=list>" +
                 "".join(top_row(i + 1, x) for i, x in enumerate(lst)) + "</div></details>")
    if merchant:
        rep = conn.execute("SELECT r.*, c.name card FROM mcc_reports r LEFT JOIN cards c ON c.id = r.card_id WHERE "
                           "lower(r.merchant) = lower(%s) LIMIT 10", (merchant["name"],)).fetchall()
        if rep:
            body += "<h2>Reported by others</h2><div class=list>" + "".join(
                f"<div class=row><span class=grow><b>{e(r['card'] or '')}</b><small>{e((r['detail'] or r['outcome'])[:160])}</small></span></div>"
                for r in rep) + "</div>"
    body += "<p class=muted style='margin-top:28px'><small>" + ("Miles for the price are before each bank's own rounding (some count per S$1 or S$5 block). " if amt else "") + "Bonus limits are counted from your own spend this period. Shop types come from public reports and can differ by outlet or payment app.</small></p>"
    return "Which card?", body


CHANNELS = [("in_person", "In store"), ("online", "Online"), ("both", "Both")]


def merchant_form(conn, person, v, msg=""):
    """Add a shop or fix its MCC. Shared by the household; beats public data; the weekly refresh never overwrites it."""
    codes = conn.execute("SELECT mcc, name FROM mcc_codes ORDER BY mcc").fetchall()
    dl = "".join(f"<option value='{e(c['mcc'])}'>{e(c['name'])}</option>" for c in codes)
    ch = v.get("channel") or "in_person"
    name_mcc = conn.execute("SELECT name FROM mcc_codes WHERE mcc = %s", (v.get("mcc") or "",)).fetchone()
    body = (f"<h1>{'Fix' if v.get('slug') else 'Add'} a shop</h1>{msg}<form method=post action=/merchant class=f style='flex-direction:column'>"
            f"<input type=hidden name=slug value='{e(v.get('slug') or '')}'>"
            f"<label>Shop name<br><input name=name required maxlength=80 value='{e(v.get('name') or '')}' style='width:100%'></label>"
            "<div>How is it sold?</div><div class=seg>" +
            "".join(f"<label><input type=radio name=channel value={k} {'checked' if k == ch else ''}><span>{t}</span></label>" for k, t in CHANNELS) +
            f"</div><label>What do they sell? (optional, helps the AI)<br><input name=info maxlength=200 value='{e(v.get('info') or '')}' style='width:100%'></label>"
            f"<label>MCC (4 digits)<br><input name=mcc list=mccs inputmode=numeric pattern='\\d{{4}}' value='{e(v.get('mcc') or '')}' style='width:100%'></label>"
            f"{'<small>' + e(name_mcc['name']) + '</small>' if name_mcc else ''}<datalist id=mccs>{dl}</datalist>"
            "<div class=seg><button name=action value=suggest style='background:var(--chip);color:var(--fg)'>Suggest MCC (AI)</button>"
            "<button name=action value=save>Save</button></div></form>"
            "<p class=muted>The MCC is on your bank app's transaction details for some banks, or on heymax / check-mcc.sg. "
            "The AI suggestion is a guess from the name: check it if you can. Only the shop name and description go to the "
            "AI model this app is set up with.</p>")
    return ("Fix a shop" if v.get("slug") else "Add a shop"), body


def slugify(name):
    return "h-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "shop")


# --- bot API (read-only, Bearer token; internal door only) -----------------------------------------------------------

def feed_person(headers, conn=None):
    """Bearer feed token -> person (CARDS_FEED_TOKEN_<ID>). A feeder can only write its own person's lines."""
    return _token_person(headers, "feed", conn)


def _token_person(headers, kind, conn=None):
    """Bearer token -> person id: a token made on the Settings page, or one named in the environment."""
    if not (headers.get("Authorization") or "").startswith("Bearer "):
        return None
    if conn is not None:
        return config.token_person(conn, headers, kind)
    try:
        with db.connect() as c:
            return config.token_person(c, headers, kind)
    except Exception:
        return people.token_person(headers, f"CARDS_{kind.upper()}_TOKEN_")


def bot_person(headers, conn=None):
    """Bearer bot token -> person (CARDS_BOT_TOKEN_<ID>). Read-only."""
    return _token_person(headers, "bot", conn)


def api_lookup(conn, q, person):
    """Which card for a shop or MCC: the shop's MCC(s), the best of the person's cards (with boosts, caps shown) and the
    best cards anywhere. Same logic as the Which card? page."""
    text = (q.get("q") or "").strip()[:80]
    pay = q.get("pay") if q.get("pay") in dict(PAY) else None
    cur = str(q.get("cur") or "").upper()[:3]
    cur = cur if re.fullmatch(r"[A-Z]{3}", cur) and cur != "SGD" else ""
    fcy = q.get("fcy") in ("1", "true", "yes") or bool(cur)
    amt = price(q.get("amt") or q.get("amount"))
    merchant, mccs, matches = None, [], []
    if re.fullmatch(r"\d{4}", text):
        mccs = [text]
    elif text:
        matches = conn.execute("(SELECT * FROM merchants WHERE lower(name) = lower(%s) ORDER BY source <> 'household') UNION ALL "
                               "(SELECT * FROM merchants WHERE name ILIKE %s AND lower(name) <> lower(%s) ORDER BY "
                               "source <> 'household', length(name) LIMIT 8)", (text, f"%{text}%", text)).fetchall()
        if matches and (matches[0]["name"].lower() == text.lower() or len(matches) == 1):
            merchant = matches[0]
            mccs = list(merchant["mccs"])
    if pay is None:
        pay = "online" if merchant and merchant.get("channel") == "online" else pay_choices(person)[1]
    chosen = q.get("mcc") if q.get("mcc") in mccs else (mccs[0] if mccs else None)
    names = {r["mcc"]: r["name"] for r in conn.execute("SELECT mcc, name FROM mcc_codes WHERE mcc = ANY(%s)", (mccs or [""],)).fetchall()}
    out = {"query": text, "pay": pay, "fcy": fcy, "currency": cur or None, "amount": amt,
           "shop": ({"name": merchant["name"], "mccs": mccs, "channel": merchant.get("channel"),
                     "source": merchant["source"]} if merchant else None),
           "other_matches": [{"name": m["name"], "mccs": m["mccs"]} for m in matches if not merchant][:8],
           "mcc": chosen, "mcc_name": names.get(chosen), "mcc_note": (
               "this shop is seen under several MCCs; pass mcc=<code> to pick another" if len(mccs) > 1 else None)}
    if not merchant and not chosen and matches:
        out["answer"] = "several shops match: ask which one, or retry with the exact name"
        return out
    p = {"mcc": chosen, "pay": pay, "fcy": fcy, "currency": cur, "merchant": merchant["name"] if merchant else text, "today": datetime.date.today(),
         "amount": amt}
    ctx = wallet_context(conn, person) if amt else None   # a price needs this period's bonus limit left
    ranked = engine.rank(conn, p, set(person["held_cards"]), person["excluded_banks"], person["boost_amounts"],
                         ctx["room"] if ctx else None)

    def item(x):
        r = x["rule"]
        cap = x["extra"].get("cap", r["cap"] if r else None)
        return {"card": x["card"]["name"], "card_id": x["card"]["id"], "bank": x["card"]["bank"], "rate": round(x["rate"], 2),
                "unit": x["unit"].strip(), "held": x["held"], "why": (r["label"] or r["bucket"]) if r else x["note"],
                "cap": float(cap) if cap else None, "cap_period": r["cap_period"] if r else None,
                "min_spend": float(r["min_spend"]) if r and r["min_spend"] else None,
                "boosts": x["extra"].get("boosts", []), "checked": bool(r and r["depth"] == "full") or (r is None and x["card"]["depth"] == "full"),
                "next_boost": ({"hold": x["extra"]["next"][0], "amount": x["extra"]["next"][1], "rate": x["extra"]["next"][2]}
                               if x["extra"].get("next") else None),
                **({"for_price": {"earned": round(x["amt"]["earned"], 2), "at_bonus": x["amt"]["bonus"], "over_limit": x["amt"]["over"],
                                  "per_sgd": round(x["amt"]["eff"], 2), "unlocks_minimum": bool(x["extra"].get("unlocks"))}}
                   if x.get("amt") else {})}

    mine = [x for x in ranked if x["held"]]
    miles = sorted((x for x in ranked if x["card"]["kind"] != "cashback" and x["value"] > 0), key=lambda x: -x["value"])
    cash = sorted((x for x in ranked if x["card"]["kind"] == "cashback" and x["value"] > 0), key=lambda x: -x["value"]) \
        if person.get("cashback_too") else []
    out["use"] = item(mine[0]) if mine and mine[0]["value"] > 0 else None
    out["then"] = [item(x) for x in mine[1:4]]
    out["top_miles"] = [item(x) for x in miles[:8]]
    out["top_cashback"] = [item(x) for x in cash[:3]]
    sl = atome_slug(conn, merchant["name"] if merchant else text) if text and not re.fullmatch(r"\d{4}", text) else None
    out["atome"] = ({"listed": True, "url": f"https://www.atome.sg/paylater-merchants/{sl}", "how": ATOME_TIP} if sl
                    else {"listed": False})
    kh = kris_hit(conn, merchant["name"] if merchant else text) if text and not re.fullmatch(r"\d{4}", text) else None
    out["kris_plus"] = ({"listed": True, "shop": kh[0], "krispay_mpd": kh[1], "krispay_mpd_lowest_outlet": kh[3],
                         "how": KRIS_TIP} if kh else {"listed": False})
    out["assumes"] = (("for_price splits the price at each of your cards' bonus limit left this period (from your real spend; "
                       "the full limit if none is known), before bank rounding" if amt else "bonus cap still has room")
                      + "; MCCs are public reports and can differ by outlet or payment app")
    return out


def api_shop(conn, q, person):
    text = (q.get("q") or "").strip()[:80]
    rows = conn.execute("SELECT name, mccs, channel, source FROM merchants WHERE name ILIKE %s ORDER BY source <> 'household', "
                        "length(name) LIMIT 15", (f"%{text}%",)).fetchall() if text else []
    return {"query": text, "shops": [dict(r) for r in rows]}


def api_caps(conn, q, person):
    from . import notify
    return notify.caps(conn, person)


def api_wallet(conn, q, person):
    """The miles wallet a bot can quote: programme pools, the KrisFlyer account, the total, and where each came from."""
    wal, at = points.wallet(conn, person)
    return {"at": at.isoformat(timespec="minutes") if at else None, "source": None if not wal else ("app" if wal.get("own") else "feeder"),
            "wallet": wal}


def api_summary(conn, q, person):
    """The daily summary as text (what `notify summary` sends), worked out now."""
    from . import notify
    return {"at": datetime.datetime.now().astimezone().isoformat(timespec="minutes"), "text": notify.summary_text(conn, person)}


API = {"/api/lookup": api_lookup, "/api/shop": api_shop, "/api/caps": api_caps, "/api/wallet": api_wallet, "/api/summary": api_summary}


def summary_page(conn, q, person):
    """Preview of the daily summary and of the alerts the next run would send."""
    from . import notify
    row = conn.execute("SELECT value FROM meta WHERE key = 'summary:last'").fetchone()
    last = json.loads(row["value"]) if row else None
    try:
        now_text = notify.summary_text(conn, person)
        pending = notify.alerts(conn, person)
    except crypto.Unavailable:
        return "Summary", spendweb.unavailable()
    mode = "sent to Telegram" if config.flag("notify") else "preview only: nothing is sent (sending is switched off on the App page)"
    body = (f"<h1>Daily summary</h1><p class=lede>What the daily message would say right now. Mode: {e(mode)}.</p>"
            f"<div class=panel><pre style='white-space:pre-wrap;font:15px/1.5 inherit;margin:0'>{e(now_text)}</pre></div>")
    if last:
        body += (f"<h2>Last run ({e(last['at'])}, {'sent' if last['sent'] else 'not sent'})</h2><div class=panel><pre style='white-space:"
                 f"pre-wrap;font:14px/1.5 inherit;margin:0'>{e(last['text'])}</pre></div>")
    body += "<h2>Alerts the next run would send</h2>" + ("<div class=list>" + "".join(
        f"<div class=row><span class=grow>{e(t)}</span></div>" for _, t in pending) + "</div>" if pending else
        "<p class=muted>Nothing new.</p>")
    return "Summary", body


def offers_table(rows, held=(), cat=None):
    """Sign-up offers as rows: bonus, what it takes, and whether THIS person qualifies (someone who already holds a
    card from the bank usually doesn't get a new-to-bank bonus)."""
    today = datetime.date.today()
    banks = advise.held_banks(cat, held) if cat else set()
    out = []
    for o in rows:
        c = cat["cards"].get(o["card_id"]) if cat and o["card_id"] else None
        fit = (advise.offer_for(o, c["bank_id"], c.get("bank"), banks) if c else
               advise.offer_for(o, None, None, banks) if (o.get("terms") or {}).get("bank_id") else
               {"status": "full", "bonus": float(o["bonus"]), "note": "", "tag": ""})
        proof = ((f"<div class=q>“{e(advise.short(fit['quote'], 300))}”" + (f" <a href='{e(fit['url'])}' rel=noreferrer>bank's promotion terms</a>" if fit.get("url") else "") + "</div>")
                 if fit.get("quote") else "<small class=w>Not checked against the bank's promotion terms yet.</small>" if o["card_id"] not in held else "")
        left = (o["ends"] - today).days if o["ends"] else None
        ends = "" if o["ends"] is None else f"ends {o['ends']:%d %b %Y}"
        soon = f" <span class='chip warn'>{left} d left</span>" if left is not None and left <= 21 else ""
        if o["card_id"] in held:
            tag = " <span class='chip warn'>you hold this card</span>"
        elif fit["status"] in ("no", "unclear", "reduced"):
            tag = f" <span class='chip warn'>{e(fit['tag'])}</span>"
        elif fit["tag"]:
            tag = f" <span class='chip ok'>{e(fit['tag'])}</span>"
        else:
            tag = ""
        win = o["window_text"] if o["window_text"] and len(o["window_text"]) <= 60 and re.search(r"month|day|week", o["window_text"], re.I) else ""
        need = " · ".join(x for x in (f"spend {money(o['min_spend'])}" if o["min_spend"] else "", e(win), ends) if x)
        fee = e(advise.short(o["fee_text"], 60).rstrip(".")) if o["fee_text"] and o["fee_text"].strip().lower() != "none" else ""
        nofee = bool(o["fee_text"]) and o["fee_text"].strip().lower() == "none"
        amount = (f"{num(o['bonus'], 0)} {e(o['bonus_unit'])}" if fit["status"] == "full" else
                  f"{num(fit['bonus'], 0)} {e(o['bonus_unit'])} for you (new customers: {num(o['bonus'], 0)})" if fit["status"] == "reduced" else
                  f"<s>{num(o['bonus'], 0)} {e(o['bonus_unit'])}</s>")
        cpm = f"{num(o['cents_per_mile'])}¢<small>a mile</small>" if o["cents_per_mile"] is not None and fit["status"] == "full" else ""
        inner = (f"<span class=grow><b>{e(o['card_name'])}</b>{tag}{soon}<small class=w>{amount}{' · ' + need if need else ''}"
                 f"{' · fee ' + fee if fee else ' · no annual fee' if nofee else ''}</small>{'<small class=w>' + e(fit['note']) + '</small>' if fit['note'] else ''}{proof}</span>"
                 f"<span class=r>{cpm}</span>")
        if o["card_id"] and not fit.get("quote"):
            row = f"<a class='row wrapt' href='/advisor/card?card={urllib.parse.quote(o['card_id'])}'>{inner}</a>"
        else:
            go = f"<small class=w><a href='/advisor/card?card={urllib.parse.quote(o['card_id'])}'>Verdict on your spend →</a></small>" if o["card_id"] else ""
            row = f"<div class='row wrapt'>{inner.replace('</span><span class=r>', go + '</span><span class=r>', 1)}</div>"
        out.append((o["card_id"] in held or fit["status"] in ("no", "unclear"), row))
    yes = [r for skip, r in out if not skip]
    no = [r for skip, r in out if skip]
    html_ = "<div class=list>" + ("".join(yes) or "<div class=row>No offers you'd qualify for right now.</div>") + "</div>"
    if no:
        html_ += f"<details><summary>{len(no)} offers you probably can't get</summary><div class=list>{''.join(no)}</div></details>"
    return html_


def offers(conn, q, person):
    rows = conn.execute("SELECT s.* FROM signup_offers s LEFT JOIN cards c ON c.id = s.card_id WHERE s.active AND "
                        "(c.bank_id IS NULL OR NOT (c.bank_id = ANY(%s))) ORDER BY s.cents_per_mile NULLS LAST, s.bonus DESC",
                        (person["excluded_banks"],)).fetchall()
    return "Sign-up bonuses", ("<h1>Sign-up bonuses</h1><p class=muted>Cheapest miles first: the cost of each bonus mile when the minimum "
                               "spend goes through CardUp, fees included. Under about 1.5¢ is a good buy. Most welcome bonuses are for "
                               "people new to the bank: offers from banks you already hold a card with are counted only when the "
                               "offer says existing customers qualify.</p>"
                               + offers_table(rows, set(person["held_cards"]), cache.catalogue(conn)))


def clip(v, n=110):
    v = "" if v is None else str(v)
    return v if len(v) <= n else v[:n].rsplit(",", 1)[0] + " …"


def plain_value(v):
    """A stored value for people: a JSON match rule becomes a few words, not braces."""
    t = str(v or "")
    if t[:1] in "{[":
        try:
            d = json.loads(t)
        except ValueError:
            return clip(t)
        if isinstance(d, dict):
            bits = []
            n = len(set(re.findall(r"\b\d{4}\b", json.dumps(d.get("mcc") or d.get("any_of") or ""))))
            if n:
                bits.append(f"{n} shop types")
            if "channel" in json.dumps(d):
                bits.append("by how you pay")
            if "merchant" in json.dumps(d):
                bits.append("named shops")
            if d.get("fcy") is not None or '"fcy"' in json.dumps(d):
                bits.append("foreign currency" + (" (" + ", ".join(d["currency"]) + ")" if isinstance(d.get("currency"), list) else ""))
            return "rule: " + (", ".join(bits) or "changed")
        return f"{len(d)} items" if isinstance(d, list) else clip(t)
    return clip(t)


def changes_table(rows, card=True):
    """Changes as rows: what changed, old -> new (long MCC lists clipped), and when."""
    names = {}
    out = []
    for r in rows:
        who = ""
        if card and r["card_id"]:
            who = f"<a href='/card/{e(r['card_id'])}'>{e(r['card_id'].replace('_', ' '))}</a> · "
        was = f"{e(plain_value(r['old_value']))} → " if r["old_value"] else ""
        out.append(f"<div class='row wrapt'><span class=grow><b>{e(clip(r['what'], 70))}</b><small class=w>{who}{was}{e(plain_value(r['new_value']))}</small></span>"
                   f"<span class='r muted' style='font-weight:400;font-size:13px'>{r['at']:%d %b}</span></div>")
    return "<div class=list>" + ("".join(out) or "<div class=row>Nothing yet</div>") + "</div>"


def changes(conn, q, person):
    rows = conn.execute("SELECT * FROM catalogue_changes ORDER BY at DESC LIMIT 120").fetchall()
    return "Changes", "<h1>What changed</h1><p class=muted>The latest 120 changes to rates, caps, fees, offers and bank T&amp;C documents.</p>" + changes_table(rows)


def ingest_settings(conn, person):
    """Card last 4 digits (match alert mails + statement sections) and statement PDF passwords. Encrypted; passwords are
    never shown again."""
    try:
        st = ingest.settings(conn, person["id"])
    except crypto.Unavailable:
        return "<h2>Card numbers and PDF passwords</h2><p class=muted>Unavailable right now (encryption).</p>"
    by_card = {}
    for l4, cid in st["last4"].items():
        by_card.setdefault(cid, []).append(l4)
    held = conn.execute("SELECT id, name FROM cards WHERE id = ANY(%s) ORDER BY name", (list(person["held_cards"]) or [""],)).fetchall()
    rows = "".join(f"<div class=row><span class=grow>{e(c['name'])}</span><input name='l4_{e(c['id'])}' inputmode=numeric "
                   f"maxlength=14 placeholder='last 4 digits' value='{e(', '.join(by_card.get(c['id'], [])))}' style='width:9em'></div>"
                   for c in held)
    return ("<h2>Card numbers and PDF passwords</h2><p class=muted>Used to read your card mail and statements automatically: "
            "the last 4 digits match an alert or a statement section to the card (several = comma-separated, e.g. a replaced "
            "card). Stored encrypted.</p>"
            f"<form method=post action=/settings/ingest><div class=list>{rows or '<div class=row>Pick your cards first.</div>'}</div>"
            f"<p style='margin-top:12px'><input type=password name=pdf_password placeholder='Add a statement PDF password' autocomplete=off> "
            f"<button>Save</button></p><p class=muted><small>{len(st['pdf_passwords'])} PDF password(s) saved (never shown). "
            "Banks usually use your NRIC or date of birth.</small></p>"
            + ("<p><label class=chip><input type=checkbox name=clear_passwords value=1> forget all saved PDF passwords</label></p>"
               if st["pdf_passwords"] else "") + "</form>")


def fees_settings(conn, person):
    """Fees are worked out for the person (bank documents + their statements): nothing to type here any more."""
    return ("<h2 id=fees>Annual fees</h2><p class=muted>Worked out for you: the amount from the bank's own fee page, the date and "
            "whether it was waived from your statements. Asking the bank for a waiver is the one part that's yours, and "
            "<a href=/fees>Fees</a> tells you when and keeps track.</p>")


def boosts_settings(conn, person):
    held = list(person["held_cards"])
    bs = conn.execute("SELECT b.*, c.name card FROM card_boosts b JOIN cards c ON c.id = b.card_id WHERE b.card_id = ANY(%s) "
                      "ORDER BY c.name", (held,)).fetchall() if held else []
    if not bs:
        return ""
    rows = "".join(f"<div class=row><span><a href='/card/{e(b['card_id'])}#boosts'>{e(b['card'])}</a><br><small>{e(b['label'])}</small></span>"
                   f"<span class=r>{'S$' + num(person['boost_amounts'].get(str(b['id'])), 0) if person['boost_amounts'].get(str(b['id'])) is not None else 'not set'}</span></div>"
                   for b in bs)
    return f"<h2>Boosts on my cards</h2><p class=muted>Enter your balance on each card page; the lookup then uses your boosted rate.</p>{rows}"


def settings(conn, q, person, mail_note=None, token_made=None, wf_note=None, src_note=None, aw_note=None):
    saved = q.get("saved")
    held = set(person["held_cards"])
    cards = conn.execute("SELECT c.id, c.name, c.kind, b.name bank FROM cards c JOIN banks b ON b.id = c.bank_id WHERE "
                         "c.status = 'active' OR c.id = ANY(%s) ORDER BY b.name, c.name", (list(held),)).fetchall()
    groups, cur = [], None
    for c in cards:
        if c["bank"] != cur:
            cur = c["bank"]
            groups.append(f"<h3 style='font-size:14px;margin:14px 0 4px'>{e(cur)}</h3>")
        groups.append(f"<label class=chip><input type=checkbox name=card value={e(c['id'])} {'checked' if c['id'] in held else ''}> "
                      f"{e(c['name'])}</label> ")
    banks = conn.execute("SELECT id, name FROM banks ORDER BY name").fetchall()
    ex = set(person["excluded_banks"])
    boxes = "".join(f"<label class=chip><input type=checkbox name=exclude value={e(b['id'])} {'checked' if b['id'] in ex else ''}> "
                    f"{e(b['name'])}</label> " for b in banks)
    mine = ", ".join(e(c["name"]) for c in cards if c["id"] in held) or "none yet"
    return "Settings", (
        f"<h1>Settings for {e(person['display'])}</h1>{'<div class=hint>Saved.</div>' if saved else ''}"
        f"<h2>My cards ({len(held)})</h2><p class=muted>Only you see your list. You can also add or remove a card from its own page.</p>"
        f"<p><small>{mine}</small></p><details{'' if held else ' open'}><summary>Add or remove cards</summary>"
        f"<form method=post action=/settings/cards>{''.join(groups)}<p><button>Save my cards</button></p></form></details>"
        + fees_settings(conn, person) + setupweb.sources_section(conn, person, src_note)
        + setupweb.wealthfolio_section(conn, person, wf_note)
        + setupweb.mailbox_section(conn, person, mail_note) + ingest_settings(conn, person)
        + setupweb.closing_section(conn, person) + boosts_settings(conn, person) +
        "<h2 id=pay>How you pay in shops</h2><p class=muted>Sets the default on Pay and how your in-store spend is counted "
        "everywhere else. Some cards pay their bonus only for a phone or watch (UOB Preferred), others for any tap.</p>"
        "<form method=post action=/settings/pay data-auto><div class='seg tall'>" + "".join(
            f"<label><input type=radio name=method value={k} {'checked' if (person.get('pay_method') or 'mixed') == k else ''}><span>{t}</span></label>"
            for k, t in METHODS) + "</div><noscript><p><button>Save</button></p></noscript></form>"
        + setupweb.paid_section(conn, person) + setupweb.fixes_section(conn, person) +
        "<h2 id=cashback>Cashback cards</h2><p class=muted>Advice is about miles. Cards that pay cashback are left out of the "
        "rankings, the better-card tips and the Advisor unless this is switched on; then cashback counts as miles at 1.5¢ a "
        "mile. A cashback card you hold is always counted.</p><form method=post action=/settings/cashback data-auto>"
        f"<input type=hidden name=sent value=1><label class=chip><input type=checkbox name=on value=1 "
        f"{'checked' if person.get('cashback_too') else ''}> suggest cashback cards too</label> "
        "<noscript><button>Save</button></noscript></form>" +
        "<h2>Daily summary</h2><p class=muted>What the evening message says, and the alerts waiting to go. <a href=/summary>Preview it →</a></p>"
        "<h2>Banks to leave out</h2><p class=muted>Cards from these banks are left out of the catalogue, the sign-up list, the "
        f"rankings and the Advisor.</p><form method=post action=/settings><p>{boxes}</p><button>Save</button></form>"
        + setupweb.awardwallet_section(conn, person, aw_note)
        + setupweb.tokens_section(conn, person, token_made) + setupweb.admin_link(conn, person))


def shrink(data):
    """Phone photos: honour the EXIF rotation, cap at 900 px wide, store as WebP. Falls back to the original bytes."""
    try:
        import io
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        im.thumbnail((900, 900))
        out = io.BytesIO()
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "WEBP", quality=88)
        return "image/webp", out.getvalue()
    except Exception:
        return None, data


# --- server ----------------------------------------------------------------------------------------------------------

def advisor(conn, q, person):
    from . import advisorweb
    return advisorweb.advisor_page(conn, q, person)


def advisor_card(conn, q, person):
    from . import advisorweb
    return advisorweb.card_pick_page(conn, q, person)


ROUTES = {"/ask": lambda conn, q, p: __import__("cards.ask", fromlist=["page"]).page(conn, q, p), "/": lookup, "/wallet": wallet_page, "/fees": fees_page, "/advisor": advisor, "/advisor/card": advisor_card, "/summary": summary_page,
          "/catalogue": catalogue, "/offers": offers, "/changes": changes, "/settings": settings, "/points": points.page,
          "/goals": goals.page, "/flights": goals.flights_page, "/admin": setupweb.admin_page, "/statements": spendweb.statements_page, "/spend": spendweb.spend_page,
          "/spend/lines": spendweb.lines_page}
MOVED = {"/overview": "/wallet"}
CSP = ("default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; manifest-src 'self'; "
       "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
YEAR = {"Cache-Control": "public, max-age=31536000, immutable"}


def denied(reason=None):
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Cards</title>"
            "<body style='font:17px -apple-system,system-ui,sans-serif;max-width:32em;margin:18vh auto;padding:0 24px'>"
            f"<h1 style='font-size:28px'>Not on the list</h1><p>{e(reason or 'This app is for the household only.')}</p></body>")


class Handler(BaseHTTPRequestHandler):
    server_version = "cards"
    protocol_version = "HTTP/1.1"   # keep-alive: the proxy in front reuses its connection instead of opening one per request
    timeout = 75

    def log_message(self, fmt, *args):   # path only: no query strings (search terms) in logs
        pass

    def person(self, conn):
        """-> person row or None (self.why says why not). The door decides the rule: see access.py."""
        self.why = None
        if getattr(self.server, "public", False):
            p, self.why = access.public_person(conn, self.headers)
            return p
        return access.internal_person(conn, self.headers)

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        b = body.encode()
        headers = dict(headers or {})
        if len(b) > 1200 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            b = gzip.compress(b, 5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        if "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # same-origin: nothing about our pages goes to other sites, and our own form posts keep a real Origin
        # ("no-referrer" made browsers send "Origin: null", which the cross-site check below then refused).
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def send_bytes(self, code, data, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def refuse_host(self):
        """No login (CARDS_SINGLE_USER) and a Host that is not this machine or in CARDS_HOSTS: see access.host_ok."""
        self.close_connection = True
        return self.send(421, "This app is not served under that name. Open it as http://127.0.0.1:8080, or list the name in "
                              "CARDS_HOSTS.", "text/plain")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        public = getattr(self.server, "public", False)
        if u.path == "/healthz":
            return self.send(200, f"ok {__version__}", "text/plain")
        if not public and not access.host_ok(self.headers):
            return self.refuse_host()
        if u.path == "/app.css":
            return self.send(200, look.CSS, "text/css; charset=utf-8", YEAR)
        if u.path == "/theme.js":
            return self.send(200, look.THEME_JS, "text/javascript; charset=utf-8", YEAR)
        if u.path == "/app.js":
            return self.send(200, look.JS, "text/javascript; charset=utf-8", YEAR)
        if u.path == "/manifest.webmanifest":
            return self.send(200, look.MANIFEST, "application/manifest+json", {"Cache-Control": "public, max-age=86400"})
        if u.path in ("/apple-touch-icon.png", "/icon-192.png", "/icon-512.png"):
            size = {"/apple-touch-icon.png": 180, "/icon-192.png": 192, "/icon-512.png": 512}[u.path]
            return self.send_bytes(200, look.icon(size), "image/png", {"Cache-Control": "public, max-age=604800"})
        if u.path in API:
            if public:   # the bot API is on the internal door only
                return self.send(404, "not found", "text/plain")
            who = bot_person(self.headers)
            if not who:
                return self.send(401, json.dumps({"error": "bot token required"}), "application/json")
            qa = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                with db.connect() as conn:
                    person = conn.execute("SELECT * FROM people WHERE id = %s", (who,)).fetchone()
                    return self.send(200, json.dumps(API[u.path](conn, qa, person), default=str), "application/json")
            except Exception as ex:
                print(f"api error {u.path}: {type(ex).__name__}", flush=True)
                return self.send(500, json.dumps({"error": "server error"}), "application/json")
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            with db.connect() as conn, cache.request():
                p = self.person(conn)
                if not p:
                    return self.send(403, denied(self.why))
                if u.path.startswith("/img/"):
                    img = cache.image(conn, urllib.parse.unquote(u.path[5:]), q.get("s") == "1")
                    if not img:
                        return self.send(404, "", "text/plain")
                    return self.send_bytes(200, img[1], img[0], {"Cache-Control": "private, max-age=31536000, immutable"})
                if u.path in MOVED:
                    return self.send(302, "", headers={"Location": MOVED[u.path]})
                if u.path == "/settings/mail-filters.xml":
                    try:
                        made = setupweb.filter_file(conn, p)
                    except crypto.Unavailable:
                        made = None
                    if not made:
                        return self.send(404, page("Not found", "<h1>No filter file</h1><p>Pick the cards held first. "
                                                   "<a href=/settings>Settings →</a></p>", "/settings", p))
                    return self.send(200, made[1], "application/atom+xml; charset=utf-8",
                                     {"Content-Disposition": f'attachment; filename="{made[0]}"'})
                if u.path == "/advisor/take":
                    from . import advisorweb
                    return self.send(200, json.dumps(advisorweb.take_json(conn, q, p)), "application/json")
                if u.path.startswith("/card/"):
                    r = card_page(conn, urllib.parse.unquote(u.path[6:]), p)
                elif u.path == "/mcc" or u.path.startswith("/mcc/"):
                    r = mcc_page(conn, q.get("code") or urllib.parse.unquote(u.path[5:]), p)
                elif u.path.startswith("/statements/"):
                    r = spendweb.statement_page(conn, u.path.split("/")[2], p)
                    if r and q.get("already"):
                        r = (r[0], "<div class=hint>You'd already uploaded this exact PDF, so it wasn't read again. Here it is.</div>" + r[1])
                elif u.path == "/compare":
                    r = compare(conn, q, p, urllib.parse.parse_qs(u.query).get("c", []))
                elif u.path == "/simulate":
                    r = spendweb.simulate_page(conn, q, p, urllib.parse.parse_qs(u.query).get("card", []))
                elif u.path == "/merchant":
                    v = {"name": q.get("name", "")[:80]}
                    if q.get("slug"):
                        m = conn.execute("SELECT * FROM merchants WHERE slug = %s", (q["slug"],)).fetchone()
                        if m:
                            v = {"slug": m["slug"], "name": m["name"], "mcc": (m["mccs"] or [""])[0], "channel": m["channel"]}
                    r = merchant_form(conn, p, v)
                elif u.path in ROUTES:
                    r = ROUTES[u.path](conn, q, p)
                else:
                    r = None
                if r is None:
                    return self.send(404, page("Not found", "<h1>Not found</h1><p><a href=/>Back to Pay →</a></p>", u.path, p))
                self.send(200, page(r[0], r[1], u.path, p))
        except Exception as ex:   # no details to the browser
            self.close_connection = True
            self.send(500, "Something went wrong.", "text/plain")
            print(f"error on {u.path}: {type(ex).__name__}: {ex}", flush=True)

    def multipart(self, limit):
        size = int(self.headers.get("Content-Length") or 0)
        if size > limit:
            self.close_connection = True   # the body stays unread
            return None
        raw = self.rfile.read(size)
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + (self.headers.get("Content-Type") or "").encode() + b"\r\n\r\n" + raw)
        return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) or b""
                for part in (msg.iter_parts() if msg.is_multipart() else [])}

    def upload_statement(self):
        f = self.multipart(20_000_000)
        if f is None:
            return self.send(413, "That PDF is over 20 MB.", "text/plain")
        data = f.get("pdf", b"")
        if not data.startswith(b"%PDF"):
            return self.send(400, "That isn't a PDF.", "text/plain")
        with db.connect() as conn:
            p = self.person(conn)
            if not p:
                return self.send(403, denied(self.why))
            hint = f.get("card", b"").decode(errors="replace")
            hint = hint if hint in p["held_cards"] else None
            try:
                sid, already = statements.start(conn, p["id"], data, f.get("password", b"").decode(errors="replace"), hint)
            except crypto.Unavailable:
                return self.send(503, page("Unavailable", spendweb.unavailable(), "/statements", p))
        return self.send(303, "", headers={"Location": f"/statements/{sid}" + ("?already=1" if already else "")})

    def upload_image(self):
        """multipart/form-data: card, image (png/jpeg/webp <= 2 MB), remove. Checked by magic bytes, not the header."""
        size = int(self.headers.get("Content-Length") or 0)
        if size > 12_500_000:
            self.close_connection = True
            return self.send(413, "That picture is over 12 MB. Try a screenshot of it instead.", "text/plain")
        raw = self.rfile.read(size)
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + (self.headers.get("Content-Type") or "").encode() + b"\r\n\r\n" + raw)
        fields = {}
        for part in msg.iter_parts() if msg.is_multipart() else []:
            fields[part.get_param("name", header="content-disposition")] = part.get_payload(decode=True) or b""
        cid = fields.get("card", b"").decode(errors="replace")[:120]
        data = fields.get("image", b"")
        mime = ("image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg" if data[:3] == b"\xff\xd8\xff" else
                "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
        with db.connect() as conn:
            p = self.person(conn)
            if not p:
                return self.send(403, denied(self.why))
            if not conn.execute("SELECT 1 FROM cards WHERE id = %s", (cid,)).fetchone():
                return self.send(404, "No such card.", "text/plain")
            if fields.get("remove"):
                conn.execute("DELETE FROM card_images WHERE card_id = %s", (cid,))
                conn.execute("UPDATE cards SET image_blocked = true WHERE id = %s", (cid,))   # don't search it again
            elif mime:
                shrunk, data = shrink(data)
                mime = shrunk or mime
                conn.execute("INSERT INTO card_images (card_id, mime, data, added_by, source, source_url) VALUES "
                             "(%s,%s,%s,%s,'household',NULL) ON CONFLICT (card_id) DO UPDATE SET mime = EXCLUDED.mime, "
                             "data = EXCLUDED.data, added_by = EXCLUDED.added_by, source = 'household', source_url = NULL, "
                             "updated_at = now()", (cid, mime, data, p["id"]))
                conn.execute("UPDATE cards SET image_blocked = false WHERE id = %s", (cid,))
            else:
                return self.send(400, "That isn't a PNG, JPEG or WebP image.", "text/plain")
            conn.commit()
        return self.send(303, "", headers={"Location": "/card/" + urllib.parse.quote(cid)})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        if not getattr(self.server, "public", False) and not access.host_ok(self.headers):
            return self.refuse_host()
        origin = self.headers.get("Origin")
        if origin == "null" and self.headers.get("Sec-Fetch-Site") == "same-origin":
            origin = None   # a page served before the Referrer-Policy fix: the browser itself vouches it is ours
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self.close_connection = True
            return self.send(403, "cross-site request refused", "text/plain")
        if u.path == "/api/feed":
            who = None if getattr(self.server, "public", False) else feed_person(self.headers)
            if not who:
                self.close_connection = True
                return self.send(401, json.dumps({"error": "feed token required"}), "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5_000_000:
                self.close_connection = True
                return self.send(413, json.dumps({"error": "too big"}), "application/json")
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                with db.connect() as conn:
                    res = statements.feed(conn, who, body)
                return self.send(200, json.dumps(res), "application/json")
            except crypto.Unavailable:
                return self.send(503, json.dumps({"error": "encryption unavailable"}), "application/json")
            except Exception as ex:
                print(f"feed error: {type(ex).__name__}", flush=True)
                return self.send(500, json.dumps({"error": "server error"}), "application/json")
        if u.path == "/card/image":
            return self.upload_image()
        if u.path == "/statements/upload":
            return self.upload_statement()
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            self.close_connection = True
            return self.send(413, "too big", "text/plain")
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        with db.connect() as conn:
            p = self.person(conn)
            if not p:
                return self.send(403, denied(self.why))
            if u.path == "/goals" or u.path.startswith("/goals/"):
                loc = goals.post(conn, u.path, form, p)
                return self.send(303, "", headers={"Location": loc or "/goals"})
            if u.path.startswith("/statements/") and u.path.endswith(("/map", "/delete")):
                sid = u.path.split("/")[2]
                st = conn.execute("SELECT id FROM statements WHERE id = %s AND person = %s", (sid, p["id"])).fetchone()
                if not st:
                    return self.send(404, "not found", "text/plain")
                if u.path.endswith("/delete"):
                    conn.execute("DELETE FROM statements WHERE id = %s", (sid,))
                    conn.commit()
                    return self.send(303, "", headers={"Location": "/statements"})
                valid = {r["id"] for r in conn.execute("SELECT id FROM cards").fetchall()}
                for k, v in form.items():
                    if k.startswith("s_") and v and v[0] in valid:
                        conn.execute("UPDATE stmt_lines SET card_id = %s WHERE statement_id = %s AND section = %s", (v[0], sid, k[2:]))
                # the statement's own closing date now tells these cards' closing day (kept unless typed in Settings)
                pto = conn.execute("SELECT period_to FROM statements WHERE id = %s", (sid,)).fetchone()["period_to"]
                statements.learn_close_day(conn, p["id"], {v[0] for k, v in form.items() if k.startswith("s_") and v and v[0] in valid}, pto)
                left = conn.execute("SELECT count(*) n FROM stmt_lines WHERE statement_id = %s AND card_id IS NULL", (sid,)).fetchone()["n"]
                conn.execute("UPDATE statements SET status = %s WHERE id = %s", ("needs_cards" if left else "ready", sid))
                conn.commit()
                return self.send(303, "", headers={"Location": f"/statements/{sid}"})
            if u.path == "/boost/amount":
                bid = (form.get("boost") or [""])[0]
                cid = (form.get("card") or [""])[0]
                raw = (form.get("amount") or [""])[0].replace(",", "").replace("S$", "").replace("$", "").strip()
                amounts = dict(p["boost_amounts"])
                if conn.execute("SELECT 1 FROM card_boosts WHERE id::text = %s", (bid,)).fetchone():
                    if raw == "":
                        amounts.pop(bid, None)
                    elif re.fullmatch(r"\d{1,9}(\.\d{1,2})?", raw):
                        amounts[bid] = float(raw)
                    conn.execute("UPDATE people SET boost_amounts = %s WHERE id = %s", (Jsonb(amounts), p["id"]))
                    conn.commit()
                return self.send(303, "", headers={"Location": "/card/" + urllib.parse.quote(cid) + "#boosts"})
            if u.path == "/boost/new":
                f = {k: (form.get(k) or [""])[0].strip() for k in ("card", "label", "requirement", "tiers", "mode", "cap", "applies_to")}
                tiers = []
                for part in re.split(r"[,;]", f["tiers"]):
                    m = re.fullmatch(r"\s*S?\$?\s*([\d,]+(?:\.\d+)?)\s*=\s*\+?(\d+(?:\.\d+)?)\s*(?:mpd)?\s*", part)
                    if m:
                        tiers.append([float(m.group(1).replace(",", "")), float(m.group(2))])
                card = conn.execute("SELECT id FROM cards WHERE id = %s", (f["card"],)).fetchone()
                if card and f["label"] and tiers and all(0 < t[1] <= 20 for t in tiers):
                    buckets = {r["bucket"]: (r["label"] or r["bucket"]).lower() for r in conn.execute(
                        "SELECT bucket, label FROM card_rules WHERE card_id = %s AND valid_to IS NULL", (f["card"],)).fetchall()}
                    want = [w.strip().lower() for w in f["applies_to"].split(",") if w.strip()]
                    applies = [b for b, lab in buckets.items() if any(w in (b.lower(), lab) for w in want)]
                    cap = float(f["cap"].replace(",", "")) if re.fullmatch(r"[\d,]+(\.\d+)?", f["cap"]) else None
                    conn.execute("INSERT INTO card_boosts (card_id, label, requirement, tiers, mode, cap_override, applies_to, "
                                 "depth, added_by) VALUES (%s,%s,%s,%s,%s,%s,%s,'headline',%s) ON CONFLICT (card_id, label) DO "
                                 "UPDATE SET tiers = EXCLUDED.tiers, mode = EXCLUDED.mode, cap_override = EXCLUDED.cap_override, "
                                 "applies_to = EXCLUDED.applies_to, requirement = EXCLUDED.requirement",
                                 (f["card"], f["label"][:80], f["requirement"][:80] or None, Jsonb(sorted(tiers)),
                                  "set" if f["mode"] == "set" else "add", cap, applies, p["id"]))
                    store.log(conn, f["card"], "boost added", None, f"{f['label'][:80]}: {f['tiers'][:80]} by {p['display']}")
                    conn.commit()
                return self.send(303, "", headers={"Location": "/card/" + urllib.parse.quote(f["card"]) + "#boosts"})
            if u.path == "/merchant":
                v = {k: (form.get(k) or [""])[0].strip() for k in ("slug", "name", "mcc", "channel", "info", "action")}
                v["name"] = v["name"][:80]
                if v["channel"] not in dict(CHANNELS):
                    v["channel"] = "in_person"
                if not v["name"]:
                    return self.send(200, page("Add a shop", merchant_form(conn, p, v, "<p class=warn>Give the shop a name.</p>")[1], "/merchant", p))
                if v["action"] == "suggest":
                    sug = llm.suggest_mcc(v["name"], v["info"], dict(CHANNELS)[v["channel"]])
                    if sug:
                        v["mcc"] = sug["mcc"]
                        n = conn.execute("SELECT name FROM mcc_codes WHERE mcc = %s", (sug["mcc"],)).fetchone()
                        msg = (f"<p class=ok>AI suggests <b>{e(sug['mcc'])}</b> {e(n['name'] if n else '')} ({e(sug['confidence'])}"
                               f" confidence): {e(sug['why'])}. Check it, then Save.</p>")
                    else:
                        msg = "<p class=warn>The AI model didn't answer. Enter the MCC yourself.</p>"
                    t, b = merchant_form(conn, p, v, msg)
                    return self.send(200, page(t, b, "/merchant", p))
                if not re.fullmatch(r"\d{4}", v["mcc"]):
                    return self.send(200, page("Add a shop", merchant_form(conn, p, v, "<p class=warn>The MCC must be 4 digits.</p>")[1], "/merchant", p))
                slug = v["slug"] if v["slug"] and conn.execute("SELECT 1 FROM merchants WHERE slug = %s", (v["slug"],)).fetchone() else slugify(v["name"])
                old = conn.execute("SELECT mccs FROM merchants WHERE slug = %s", (slug,)).fetchone()
                conn.execute("INSERT INTO merchants (slug, name, mccs, source, channel, added_by, updated_at) VALUES "
                             "(%s,%s,%s,'household',%s,%s,now()) ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, mccs = "
                             "EXCLUDED.mccs, source = 'household', channel = EXCLUDED.channel, added_by = EXCLUDED.added_by, "
                             "updated_at = now()", (slug, v["name"], [v["mcc"]], v["channel"], p["id"]))
                store.log(conn, None, "shop " + ("fixed" if old else "added"), ", ".join(old["mccs"]) if old else None,
                          f"{v['name']}: {v['mcc']} ({v['channel']}) by {p['display']}")
                conn.commit()
                pay = "online" if v["channel"] == "online" else pay_choices(p)[1]
                return self.send(303, "", headers={"Location": "/?" + urllib.parse.urlencode({"q": v["name"], "m": slug, "pay": pay})})
            if u.path == "/settings/ingest":
                try:
                    st = ingest.settings(conn, p["id"])
                    held = set(p["held_cards"])
                    l4 = {}
                    for k, v in form.items():
                        if k.startswith("l4_") and k[3:] in held:
                            for d in re.findall(r"\b\d{4}\b", v[0]):
                                l4[d] = k[3:]
                    if set(l4.items()) - set(st["last4"].items()):
                        ingest.retry_alerts_without_card(conn, p["id"])   # a new card's earlier alerts are read again
                    st["last4"] = l4
                    if form.get("clear_passwords"):
                        st["pdf_passwords"] = []
                    pw = (form.get("pdf_password") or [""])[0].strip()
                    if pw and pw not in st["pdf_passwords"]:
                        st["pdf_passwords"] = (st["pdf_passwords"] + [pw])[-10:]
                    ingest.save_settings(conn, p["id"], st)
                    conn.commit()
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/settings", p))
                return self.send(303, "", headers={"Location": "/settings?saved=ingest"})
            if u.path == "/settings/mailbox":
                try:
                    note = setupweb.post_mailbox(conn, form, p)
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/settings", p))
                if note is None:
                    return self.send(303, "", headers={"Location": "/settings?saved=mail#mail"})
                p = conn.execute("SELECT * FROM people WHERE id = %s", (p["id"],)).fetchone()
                r = settings(conn, {}, p, mail_note=note)
                return self.send(200, page(r[0], r[1], "/settings", p))
            if u.path == "/settings/wealthfolio":
                try:
                    note = setupweb.post_wealthfolio(conn, form, p)
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/settings", p))
                if note is None:
                    return self.send(303, "", headers={"Location": "/settings?saved=wealthfolio#wealthfolio"})
                p = conn.execute("SELECT * FROM people WHERE id = %s", (p["id"],)).fetchone()
                r = settings(conn, {}, p, wf_note=note)
                return self.send(200, page(r[0], r[1], "/settings", p))
            if u.path == "/settings/sources":
                try:
                    note = setupweb.post_sources(conn, form, p)
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/settings", p))
                if note is None:
                    return self.send(303, "", headers={"Location": "/settings?saved=sources#sources"})
                p = conn.execute("SELECT * FROM people WHERE id = %s", (p["id"],)).fetchone()
                r = settings(conn, {}, p, src_note=note)
                return self.send(200, page(r[0], r[1], "/settings", p))
            if u.path == "/settings/closing":
                setupweb.post_closing(conn, form, p)
                return self.send(303, "", headers={"Location": "/settings?saved=closing#closing"})
            if u.path == "/spend/fix":
                try:
                    back = spendweb.post_fix(conn, form, p)
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/settings", p))
                return self.send(303, "", headers={"Location": back})
            if u.path == "/settings/fixes":
                try:
                    setupweb.post_fixes(conn, form, p)
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/settings", p))
                return self.send(303, "", headers={"Location": "/settings?saved=fixes#fixes"})
            if u.path in ("/settings/paid", "/spend/paid"):
                try:
                    back = setupweb.post_paid(conn, form, p) if u.path == "/settings/paid" else spendweb.post_paid(conn, form, p)
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/settings", p))
                return self.send(303, "", headers={"Location": back or "/settings?saved=paid#paid"})
            if u.path == "/settings/awardwallet":
                try:
                    note = setupweb.post_awardwallet(conn, form, p)
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/settings", p))
                if note is None:
                    return self.send(303, "", headers={"Location": "/settings?saved=awardwallet#awardwallet"})
                r = settings(conn, {}, p, aw_note=note)
                return self.send(200, page(r[0], r[1], "/settings", p))
            if u.path == "/points":
                try:
                    said = points.post(conn, form, p)
                except crypto.Unavailable:
                    return self.send(503, page("Unavailable", spendweb.unavailable(), "/points", p))
                return self.send(303, "", headers={"Location": "/points" + (f"?note={urllib.parse.quote(said)}" if said else "")})
            if u.path == "/settings/token":
                made = setupweb.post_token(conn, form, p)
                if not made:
                    return self.send(303, "", headers={"Location": "/settings?saved=token#tokens"})
                r = settings(conn, {}, p, token_made=made)
                return self.send(200, page(r[0], r[1], "/settings", p))
            if u.path.startswith("/admin/"):
                note = setupweb.post_admin(conn, u.path, form, p)
                if note is None:
                    return self.send(303, "", headers={"Location": "/admin?saved=1"})
                p = conn.execute("SELECT * FROM people WHERE id = %s", (p["id"],)).fetchone()
                r = setupweb.admin_page(conn, {}, p, note)
                return self.send(200, page(r[0], r[1], "/admin", p))
            if u.path == "/settings/cashback":
                conn.execute("UPDATE people SET cashback_too = %s WHERE id = %s", (bool(form.get("on")), p["id"]))
                conn.commit()
                return self.send(303, "", headers={"Location": "/settings?saved=cashback#cashback"})
            if u.path == "/settings/pay":
                m = (form.get("method") or [""])[0]
                if m in dict(METHODS):
                    conn.execute("UPDATE people SET pay_method = %s WHERE id = %s", (m, p["id"]))
                    conn.commit()
                return self.send(303, "", headers={"Location": "/settings?saved=pay#pay"})
            if u.path == "/fees/mark":   # Fees page buttons: asked / waived / paid / reset, or the fee month
                cid = (form.get("card") or [""])[0]
                act = (form.get("action") or [""])[0]
                if cid in set(p["held_cards"]):
                    try:
                        if act == "month":
                            m = (form.get("month") or [""])[0]
                            dates = dict(p.get("fee_dates") or {})
                            cur = dict(dates.get(cid) or {})
                            if m.isdigit() and 1 <= int(m) <= 12:
                                cur["month"] = int(m)
                            else:
                                cur.pop("month", None)
                            dates[cid] = cur
                        elif act in ("asked", "waived", "paid", "reset"):
                            dates = fees.mark(p, cid, act, fee_rows(conn, p)[1])
                        else:
                            dates = None
                        if dates is not None:
                            conn.execute("UPDATE people SET fee_dates = %s WHERE id = %s", (Jsonb(dates), p["id"]))
                            conn.commit()
                    except crypto.Unavailable:
                        return self.send(503, page("Unavailable", spendweb.unavailable(), "/fees", p))
                back = (form.get("back") or ["/fees"])[0]
                return self.send(303, "", headers={"Location": back if back in ("/", "/wallet", "/fees") else "/fees"})
            if u.path == "/settings/cards":
                valid = {r["id"] for r in conn.execute("SELECT id FROM cards").fetchall()}
                conn.execute("UPDATE people SET held_cards = %s WHERE id = %s",
                             (sorted(set(form.get("card", [])) & valid), p["id"]))
                conn.commit()
                return self.send(303, "", headers={"Location": "/settings?saved=cards"})
            if u.path == "/ask/api":   # the chat window's question: JSON back, nothing stored
                from . import ask
                return self.send(200, json.dumps(ask.api(conn, p, form)), "application/json")
            if u.path == "/ask":   # a question for the model: answered in this request, never stored
                from . import ask
                r = ask.page(conn, {}, p, asked=(form.get("q") or [""])[0])
                return self.send(200, page(r[0], r[1], "/ask", p))
            if u.path == "/hold":   # card page toggle
                cid = (form.get("card") or [""])[0]
                if conn.execute("SELECT 1 FROM cards WHERE id = %s", (cid,)).fetchone():
                    held = set(p["held_cards"])
                    held.symmetric_difference_update({cid})
                    conn.execute("UPDATE people SET held_cards = %s WHERE id = %s", (sorted(held), p["id"]))
                    conn.commit()
                return self.send(303, "", headers={"Location": "/card/" + urllib.parse.quote(cid)})
            if u.path == "/settings":
                valid = {r["id"] for r in conn.execute("SELECT id FROM banks").fetchall()}
                ex = sorted(set(form.get("exclude", [])) & valid)
                conn.execute("UPDATE people SET excluded_banks = %s WHERE id = %s", (ex, p["id"]))
                conn.commit()
                return self.send(303, "", headers={"Location": "/settings?saved=1"})
        self.send(404, "not found", "text/plain")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    public = False


def serve(port=8080):
    """Internal door on `port` (behind a forward-auth proxy); public door on CARDS_PUBLIC_PORT (behind a Cloudflare
    tunnel with Access), default port + 1."""
    import threading
    pub = Server(("0.0.0.0", int(os.environ.get("CARDS_PUBLIC_PORT", port + 1))), Handler)
    pub.public = True
    threading.Thread(target=pub.serve_forever, daemon=True).start()
    try:   # warm the catalogue cache so the first page isn't the slow one
        with db.connect() as conn:
            cache.catalogue(conn)
    except Exception as ex:
        print(f"warm-up skipped: {type(ex).__name__}", flush=True)
    print(f"cards {__version__} web on :{port} (internal) and :{pub.server_address[1]} (public, {'on' if access.AUD else 'off: no CF_ACCESS_AUD'})", flush=True)
    Server(("0.0.0.0", port), Handler).serve_forever()
