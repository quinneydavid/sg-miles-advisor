"""Advisor pages: the wallet review (keep / cancel / get), the verdict on any one card, and the T&C check shown on a
card page. The sums come from advise.py; this file only lays them out."""
import datetime
import urllib.parse

from . import advise, cache, crypto, look, replay


def h():
    from . import web
    return web


def balances(conn, person):
    """{programme id: points} when the person's statements printed them (to warn before a cancel strands points)."""
    try:
        row = conn.execute("SELECT points_enc FROM people WHERE id = %s", (person["id"],)).fetchone()
        pts = crypto.dec_json(conn, row["points_enc"], f"people.points_enc:{person['id']}") if row and row["points_enc"] else {}
        return {k: float(v.get("balance") or 0) for k, v in pts.items() if v.get("balance")}
    except Exception:
        return {}


def setup(conn, person):
    """(base, cat, lines, months, allowed) for this person: the last complete months of their real lines."""
    cat = cache.catalogue(conn)
    base, allowed = replay.load(conn, person["excluded_banks"], person["boost_amounts"], person["statement_days"], replay.in_store(person),
                                  person["held_cards"], person.get("cashback_too"))
    lines, months = advise.recent(cache.lines(conn, person["id"]), datetime.date.today())
    return base, cat, lines, months, allowed


def analysis(conn, person):
    base, cat, lines, months, allowed = setup(conn, person)
    if not lines:
        return base, cat, lines, months, allowed, None
    def work():
        # what each card really costs: the person's own figure if they ever gave one, then what the statements settle
        # (fee reversed = 0, fee paid = the charge); otherwise the bank's document or the list price (advise.fee_of)
        from . import fees
        rows = fees.tracker(conn, person, cat, cache.lines(conn, person["id"]), cache.fee_lines(conn, person["id"]))
        real = {**fees.effective(rows), **(person.get("card_fees") or {})}
        return advise.analyse(base, cat, lines, list(person["held_cards"]), allowed, months, balances(conn, person), real)

    an = cache.memo(conn, person["id"], ("analyse", cache.person_key(person), datetime.date.today()), work)
    return base, cat, lines, months, allowed, an


def card_verdict(conn, person, cid):
    """(verdict dict, pros, cons) for one card, memoised with the person's lines."""
    w = h()
    base, cat, lines, months, allowed, an = analysis(conn, person)
    held = list(person["held_cards"])
    tc = w.tc_counts(conn, cid).get(cid)
    pros, cons = advise.points(cat, cid, held, tc)
    v = cache.memo(conn, person["id"], ("verdict", cid, cache.person_key(person), datetime.date.today()),
                   lambda: advise.verdict(base, cat, lines, held, allowed, months, cid, an))
    if v["verdict"] != "nodata" and (months < 3 or len(lines) < 40):
        v = dict(v, thin=f"Worked out from only {len(lines)} transactions over {months} complete month(s): provisional until about "
                         "three months of spend are on file.")
        if v["verdict"] in ("cancel", "spare", "waiver"):   # never tell someone to close a card on a few weeks of spend
            v = dict(v, label="Too early to say", tone="", headline="Too early to say. On the little spend on file so far: " + v["headline"])
    return v, pros, cons, cat


def verdict_box(conn, person, cid, title=True):
    """The verdict box + what's good / what's not for one card, as HTML."""
    w = h()
    e = w.e
    try:
        v, pros, cons, cat = card_verdict(conn, person, cid)
    except crypto.Unavailable:
        cat = cache.catalogue(conn)
        pros, cons = advise.points(cat, cid, list(person["held_cards"]), w.tc_counts(conn, cid).get(cid))
        v = None
    out = ""
    if v:
        mine = cid in person["held_cards"]
        sums = "".join(f"<div><span>{e(k)}</span><span>{e(val)}</span></div>" for k, val in v["sums"])
        pts = "".join(f"<li>{e(x)}</li>" for x in v["why"]) + "".join(f"<li class=c>{e(x)}</li>" for x in v["catch"])
        take = ""
        if v["verdict"] != "nodata":
            try:
                t = advise.take(conn, person["id"], f"card:{cid}", advise.facts_of(cat, cid, v, pros, cons))
            except Exception:
                t = {"status": "off"}
            if t["status"] == "ready":
                take = f"<p class=take>{e(t['text'])}</p>"
            elif t["status"] == "writing":
                take = f"<p class=take hidden data-take='/advisor/take?card={urllib.parse.quote(cid)}'></p>"
        out += (f"<div class='verdict {'v-' + v['tone'] if v['tone'] else ''}'><div class=tag>{'Keep or cancel?' if mine else 'Should you get it?'} · {e(v['label'])}</div>"
                f"<p class=say>{e(v['headline'])}</p>{'<ul>' + pts + '</ul>' if pts else ''}"
                f"{'<p class=muted><small>⚠️ ' + e(v['thin']) + '</small></p>' if v.get('thin') else ''}"
                f"{'<details><summary>Show the sums</summary><div class=sums>' + sums + '</div></details>' if sums else ''}{take}</div>")
    if pros or cons:
        out += ("<div class=pc><div class='panel good'><h3>What's good</h3><ul>" + ("".join(f"<li>{e(x)}</li>" for x in pros) or "<li>Nothing stands out.</li>") +
                "</ul></div><div class='panel bad'><h3>What's not</h3><ul>" + ("".join(f"<li>{e(x)}</li>" for x in cons) or "<li>Nothing we know of.</li>") +
                "</ul></div></div>")
    return out


def take_json(conn, q, person):
    """Polled by app.js while the model writes the paragraph."""
    cid = q.get("card", "")
    cat = cache.catalogue(conn)
    if cid not in cat["cards"]:
        return {"status": "off"}
    try:
        v, pros, cons, cat = card_verdict(conn, person, cid)
        return advise.take(conn, person["id"], f"card:{cid}", advise.facts_of(cat, cid, v, pros, cons))
    except Exception:
        return {"status": "off"}


CHIP = {"keep": "ok", "get": "ok", "bonus": "ok", "waiver": "warn", "cancel": "warn", "skip": "warn"}


def advisor_page(conn, q, person):
    w = h()
    e, num, money = w.e, w.num, w.money
    held = [c for c in person["held_cards"]]
    if not held:
        return "Advisor", ("<h1>Advisor</h1><div class=panel><p>Pick the cards you hold first.</p>"
                           "<a class=btn href=/settings>Choose your cards</a></div>")
    try:
        base, cat, lines, months, allowed, an = analysis(conn, person)
    except crypto.Unavailable:
        from .spendweb import unavailable
        return "Advisor", "<h1>Advisor</h1>" + unavailable()
    if not an:
        return "Advisor", ("<h1>Advisor</h1><div class=panel><p><b>No spend on file yet.</b></p><p class=muted>The advice is worked "
                           "out from your own card spend. Upload a statement, or wait for your card lines to arrive.</p>"
                           "<a class=btn href=/statements>Statements</a></div>"
                           "<p style='margin-top:18px'><a href=/advisor/card>Look at any card in the meantime →</a></p>")
    ph = cat["photos"]
    card = lambda cid: cat["cards"][cid]
    fees = sum(x["fee"] or 0 for x in an["cards"])
    unknown = sum(1 for x in an["cards"] if x["fee"] is None)
    cut = [x for x in an["cards"] if x["verdict"] == "cancel"]
    ask = [x for x in an["cards"] if x["verdict"] == "waiver"]
    get = [x for x in an["add"] if x["net"] >= 150 or (x["offer"] and x["net"] + x["bonus_value"] >= 250)][:3]
    say = []
    if cut:
        say.append(f"cancel {len(cut)}")
    if ask:
        say.append(f"get the fee waived on {len(ask)}")
    if get:
        say.append(f"add {len(get)}")
    thin = months < 3 or len(lines) < 40   # too little spend on file to tell anyone to close a card
    head = ("Your wallet is in good shape: nothing to cancel and nothing worth adding right now." if not say else
            "A first read only: there isn't enough spend on file yet to say what to cancel." if thin else
            "The short version: " + ", ".join(say) + ".")
    body = (f"<h1>Advisor</h1><p class=lede>{e(head)}</p>"
            f"<div class=grid><div class=tile><b>{num(an['miles_year'], 0)}</b><span>miles a year, best use of your {len(an['cards'])} cards</span></div>"
            f"<a class=tile href=/spend><b>{money(an['spend_year'] / 12)}</b><span>a month on cards<small class=go>see your spend ›</small></span></a>"
            f"<a class=tile href=/fees><b>{money(fees)}</b><span>annual fees you'd really pay{f' ({unknown} not known)' if unknown else ''}"
            "<small class=go>a waived fee counts as S$0 · see each fee ›</small></span></a>"
            f"<div class=tile><b>{num(an['miles_year'] / an['spend_year'], 2) if an['spend_year'] else '0'}</b><span>miles per S$, overall</span></div></div>")

    if thin:
        body += (f"<div class=hint>⚠️ This rests on only {len(lines)} transactions over {months} complete month(s). A card that looks idle here "
                 "may simply not have been used yet: don't cancel anything on this until about three months of spend are on file.</div>")

    def held_row(x):
        c = card(x["card"])
        v = x["verdict"]
        line = {"keep": f"+{num(x['adds'], 0)} miles a year over your other cards",
                "waiver": f"+{num(x['adds'], 0)} miles a year, worth {money(x['value'])}, against a {money(x['fee'])} fee",
                "cancel": f"adds {num(x['adds'], 0)} miles a year and costs {money(x['fee'])}",
                "spare": "adds almost nothing, but it's free" if x["fee"] is not None else "adds almost nothing; keep only if it's free"}[v]
        return (f"<a class='row wrapt' href='/card/{e(c['id'])}'>{look.face(c, 'sm', ph.get(c['id']))}<span class=grow><b>{e(c['name'])}</b> "
                f"<span class='chip {CHIP.get(v, '')}'>{e(advise.LABEL[v])}</span><small>{e(line)}</small>"
                f"<small>{money(x['spend_month'])} a month belongs on it · fee {money(x['fee']) if x['fee'] is not None else 'not known'}</small></span>"
                f"<span class=r>{'−' if x['net'] < 0 else ''}{money(abs(x['net']))}<small>a year</small></span></a>")

    body += ("<h2>The cards you hold</h2><p class=muted>What each one earns that the rest of your wallet couldn't, at 1.5¢ a mile, "
             "minus its fee.</p><div class=list>" + "".join(held_row(x) for x in an["cards"]) + "</div>")
    if an["add"]:
        def add_row(x):
            c = card(x["card"])
            v = "get" if x["net"] >= 150 else "bonus" if (x["offer"] and x["net"] + x["bonus_value"] >= 250) else "maybe" if x["net"] > 0 else "skip"
            where = x["where"][0]["what"] if x["where"] else ""
            bonus = ((f" · sign-up bonus for you {num(x['bonus'], 0)} {e(x['offer']['bonus_unit'])}" if x["bonus"] else
                      " · sign-up bonus not for existing customers") if x["offer"] else "")
            return (f"<a class='row wrapt' href='/advisor/card?card={urllib.parse.quote(c['id'])}'>{look.face(c, 'sm', ph.get(c['id']))}<span class=grow>"
                    f"<b>{e(c['name'])}</b> <span class='chip {CHIP.get(v, '')}'>{e(advise.LABEL[v])}</span>"
                    f"<small>+{num(x['adds'], 0)} miles a year{', mostly ' + e(where.lower()) if where else ''}</small>"
                    f"<small>fee {money(x['fee']) if x['fee'] is not None else 'not known'}{bonus}{'' if x['checked'] else ' · rates unchecked'}</small></span>"
                    f"<span class=r>{'+' if x['net'] >= 0 else '−'}{money(abs(x['net']))}<small>a year</small></span></a>")
        body += ("<h2>Cards worth a look</h2><p class=muted>What each would add on top of your wallet, on your real spend, after its fee. "
                 "Tap one for the full verdict.</p><div class=list>" + "".join(add_row(x) for x in an["add"][:6]) + "</div>")
    if unknown:
        body += (f"<div class=hint>{unknown} of your cards have no annual fee in the bank documents read so far, so none was counted "
                 "for them. <a href=/fees>Fees</a> shows where each figure comes from.</div>")
    slim = an["slim"]
    if slim and len(an["cards"]) > 2:
        steps = [st for st in slim if st["share"] >= 0.5] or slim
        pick = next((st for st in steps if st["share"] >= 0.95), steps[-1])
        names = ", ".join(e(card(c)["name"]) for c in pick["cards"])
        body += (f"<h2>The lean wallet</h2><div class=panel><p><b>{len(pick['cards'])} of your {len(an['cards'])} cards earn "
                 f"{pick['share'] * 100:.0f}% of the miles.</b></p><p class=muted>{names}.</p><div class=sums>" + "".join(
                     f"<div><span>{len(st['cards'])} card{'s' if len(st['cards']) > 1 else ''}: + {e(card(st['cards'][-1])['name'])}</span>"
                     f"<span>{num(st['miles_year'], 0)} · {st['share'] * 100:.0f}%</span></div>" for st in slim) +
                 "</div><p class=muted style='margin-top:12px'><small>Built one card at a time, each step adding the card that lifts "
                 "miles (less fees) the most.</small></p></div>")
    body += (f"<p class=muted style='margin-top:26px'><small>From {len(lines)} transactions over {months} complete month(s), replayed with "
             "every cap and minimum spend, always on the best card. Miles valued at 1.5¢; cashback converted at the same rate. "
             "Fees are counted in full: a waived fee only makes a card look better than shown.</small></p>")
    return "Advisor", body


def card_pick_page(conn, q, person):
    """Pick any card -> the verdict for you."""
    w = h()
    e = w.e
    cat = cache.catalogue(conn)
    cid = q.get("card", "")
    held = set(person["held_cards"])
    ex = set(person["excluded_banks"])
    groups, cur = [], None
    for c in sorted(cat["active"].values(), key=lambda c: (c["bank"], c["name"])):
        if c["bank_id"] in ex and c["id"] != cid:
            continue
        if c["bank"] != cur:
            groups.append(("</optgroup>" if cur else "") + f"<optgroup label='{e(c['bank'])}'>")
            cur = c["bank"]
        groups.append(f"<option value='{e(c['id'])}'{' selected' if c['id'] == cid else ''}>{e(c['name'])}{' (yours)' if c['id'] in held else ''}</option>")
    body = ("<h1>Should I get it?</h1><form class=stack method=get action=/advisor/card data-auto><select name=card>"
            f"<option value=''>Pick a card…</option>{''.join(groups)}{'</optgroup>' if cur else ''}</select></form>")
    if cid not in cat["cards"]:
        return "Should I get it?", body + ("<p class=muted>Any card in Singapore, held or not. The answer is worked out on your own "
                                           "spend: what it would add to (or already adds to) your wallet, after its fee.</p>")
    c = cat["cards"][cid]
    body += (f"<a class=row href='/card/{e(cid)}' style='padding-left:0;padding-right:0'>{look.face(c, 'sm', cat['photos'].get(cid))}"
             f"<span class=grow><b>{e(c['name'])}</b><small>{e(c['bank'])} · rules, MCCs and T&amp;C check →</small></span></a>")
    body += verdict_box(conn, person, cid)
    return c["name"], body


TC_WORD = {"match": ("ok", "matches"), "differs": ("warn", "differs"), "not_found": ("muted", "not found in the documents")}


def subject_name(subject, rule_labels):
    parts = subject.split(":")
    if parts[0] == "rule" and len(parts) >= 3:
        what = {"rate": "rate", "cap": "cap", "min_spend": "minimum spend", "mccs": "MCC list"}.get(parts[2], parts[2])
        return f"{rule_labels.get(parts[1], parts[1])}: {what}"
    return {"base_sgd": "Base rate (local)", "base_fcy": "Base rate (foreign currency)", "annual_fee": "Annual fee",
            "exclusions": "MCCs that earn nothing"}.get(subject, subject)


def tc_section(conn, cid, rule_labels):
    """Bank T&C check for a card: each thing we claim, what the bank's document says, and the sentence it comes from."""
    w = h()
    e = w.e
    try:
        from . import tc
        sm = tc.summary(conn, cid)
    except Exception:
        conn.rollback()
        return ""
    docs, checks = sm.get("docs") or [], sm.get("checks") or []
    if not docs and not checks:
        return ("<h2 id=tc>T&amp;C check</h2><p class=muted>No document from the bank's own website is on file for this card yet, so "
                "its figures are unverified.</p>")
    n = sm.get("counts") or {}
    order = {"differs": 0, "match": 1, "not_found": 2}
    rows = []
    for c in sorted(checks, key=lambda c: (order.get(c["status"], 3), c["subject"])):
        cls, word = TC_WORD.get(c["status"], ("muted", c["status"]))
        both = (f"<small class=w>Ours: {e(c['ours'] or '-')}" + (f" · Bank: {e(c['theirs'])}" if c.get("theirs") else "") + "</small>")
        quote = (f"<div class=q>“{e(c['quote'])}”" + (f" <a href='{e(c['url'])}' rel=noreferrer>source</a>" if c.get("url") else "") + "</div>") if c.get("quote") else ""
        rows.append(f"<div class='row wrapt'><span class=grow><b>{e(subject_name(c['subject'], rule_labels))}</b> "
                    f"<span class='chip {cls if cls != 'muted' else ''}'>{word}</span>{both}{quote}</span></div>")
    ok_docs = [d for d in docs if d["status"] == "ok"]
    doc_rows = "".join(
        f"<div class='row wrapt'><span class=grow><a href='{e(d['url'])}' rel=noreferrer>{e(d.get('title') or d['url'].rsplit('/', 1)[-1])}</a>"
        f"<small class=w>{e(urllib.parse.urlsplit(d['url']).netloc)} · {e(d.get('kind') or 'T&C')} · "
        f"{'read ' + d['fetched_at'].strftime('%d %b %Y') if d.get('fetched_at') and d['status'] == 'ok' else e(d['status'])}"
        f"{' · changed ' + d['changed_at'].strftime('%d %b %Y') if d.get('changed_at') else ''}</small></span></div>" for d in docs)
    head = (f"{n.get('match', 0)} match" + (f" · <span class=warn>{n['differs']} differ</span>" if n.get("differs") else "") +
            (f" · {n['not_found']} not found" if n.get("not_found") else ""))
    out = (f"<h2 id=tc>T&amp;C check</h2><p class=muted>Our figures against {len(ok_docs)} document(s) from the bank's own website. "
           f"Every result quotes the sentence it rests on. {head}.</p>")
    if rows:
        shown, rest = rows[:6], rows[6:]
        out += "<div class=list>" + "".join(shown) + "</div>"
        if rest:
            out += f"<details><summary>{len(rest)} more checks</summary><div class=list>{''.join(rest)}</div></details>"
    out += f"<details><summary>The bank documents ({len(docs)})</summary><div class=list>{doc_rows}</div></details>"
    return out
