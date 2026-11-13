"""Pages for statements, spend results (what was earned, cap overflows, tips), the transaction list and the wallet
simulator. Personal data is decrypted for the signed-in person only; decrypted lines and results are held in this
process's memory (cache.py) until that person's lines change, never on disk and never logged."""
import datetime
import re
import urllib.parse
from collections import Counter, defaultdict

from . import cache, crypto, ingest, look, pipeline, replay, statements


def h():
    from . import web
    return web


ASSUME = ("Statement-period caps follow each card's statement date when it's known (else the calendar month), a bonus "
          "with a minimum spend counts only in periods that reached it, and cashback is counted at 1.5¢ a mile.")


def assume(person):
    """How this person's lines were read, said out loud: the payment method is theirs (Settings), not a guess."""
    how = {"mobile": "Apple Pay on your phone or watch, never the physical card", "tap": "a tap of the physical card"}.get(
        person.get("pay_method"), "Apple Pay on your phone or watch")
    return (f"Spend in shops is counted as {how} (<a href='/settings#pay'>change</a>), unless the bank's line or your own answer "
            f"for that shop says how it was paid; a line is online when its source or the shop list says so. {ASSUME}")


def rules_note(conn):
    """Whether past months are valued with the rules of their time (only where the catalogue recorded a change)."""
    n = len(cache.catalogue(conn).get("hist") or {})
    if not n:
        return ("Past months are valued with today's rules: the catalogue has no record of an earlier rate or cap for any "
                "card, so a rule a bank changed before the card was added here is not known.")
    return (f"A purchase made before a rule changed is valued with the rule of its time, for the {n} card(s) whose changes the "
            "catalogue recorded. Everything else in past months is valued with today's rules.")


def post_paid(conn, form, person):
    """The answer to "How was this paid?" on one purchase: remembered for that shop (pipeline.remember).
    -> where to go back to."""
    lid, ch = (form.get("line") or [""])[0], (form.get("ch") or [""])[0]
    back = (form.get("back") or [""])[0]
    back = back if re.fullmatch(r"/spend/lines(\?[A-Za-z0-9=&_%.-]*)?", back or "") else "/spend/lines"
    ln = next((x for x in cache.lines(conn, person["id"]) if x["id"] == lid), None)
    if ln:
        st = ingest.settings(conn, person["id"])
        mem = dict(st.get("paid") or {})
        if pipeline.remember(mem, ln.get("merchant"), ch, ln["date"]):
            st["paid"] = mem
            ingest.save_settings(conn, person["id"], st)
            conn.commit()
    return back


def unavailable(msg="Encryption is unavailable right now, so personal data can't be shown. Try again shortly."):
    return f"<div class=panel><p><b>Can't open your data.</b></p><p class=muted>{h().e(msg)}</p></div>"


def statements_page(conn, q, person):
    w = h()
    e = w.e
    held = conn.execute("SELECT id, name FROM cards WHERE id = ANY(%s) ORDER BY name", (list(person["held_cards"]) or [""],)).fetchall()
    opts = "<option value=''>Work it out from the statement</option>" + "".join(
        f"<option value='{e(c['id'])}'>{e(c['name'])}</option>" for c in held)
    body = ("<h1>Statements</h1><p class=lede>Upload a card statement and see what each spend earned, what it could have "
            "earned, and what to use next time.</p>"
            "<div class=panel><form method=post action=/statements/upload enctype=multipart/form-data class=stack>"
            "<input type=file name=pdf accept='application/pdf,.pdf' required>"
            "<input type=password name=password placeholder='PDF password, if it has one' autocomplete=off>"
            f"<select name=card>{opts}</select><button>Upload and read</button></form>"
            "<p class=muted><small>The PDF is read by the AI model this app is set up with, then deleted. Only the "
            "transactions are kept, encrypted, visible to you alone. The password is used once and never stored.</small></p></div>")
    fd = conn.execute("SELECT n_lines, period_from, period_to FROM statements WHERE id = %s", (statements.feed_id(person["id"]),)).fetchone()
    if fd and fd["n_lines"]:
        body += (f"<div class=hint style='margin-top:16px'>🔄 Card spend also arrives automatically from a feeder "
                 f"({fd['n_lines']} lines, {fd['period_from']:%d %b} – {fd['period_to']:%d %b}), every 30 minutes. Uploads "
                 "are only needed for cards or months it doesn't cover.</div>")
    wf = conn.execute("SELECT n_lines, period_from, period_to FROM statements WHERE id = %s", (statements.wf_id(person["id"]),)).fetchone()
    if wf and wf["n_lines"]:
        body += (f"<div class=hint style='margin-top:16px'>Card spend also arrives from Wealthfolio "
                 f"({wf['n_lines']} lines, {wf['period_from']:%d %b} – {wf['period_to']:%d %b}). Uploads are only needed for "
                 "cards or months it does not cover. <a href=/settings#wealthfolio>Settings</a></div>")
    rows = conn.execute("SELECT id, uploaded_at, status, period_from, period_to, n_lines, checked FROM statements WHERE "
                        "person = %s AND id NOT LIKE 'feed-%%' AND id NOT LIKE 'wf-%%' ORDER BY uploaded_at DESC LIMIT 50", (person["id"],)).fetchall()
    if rows:
        label = {"reading": "Reading…", "needs_cards": "Pick the card", "ready": "Ready", "failed": "Couldn't read"}
        body += "<h2>Your uploads</h2><div class=list>" + "".join(
            f"<a class=row href='/statements/{r['id']}'><span class=grow><b>"
            f"{(r['period_from'].strftime('%d %b') + ' – ' + r['period_to'].strftime('%d %b %Y')) if r['period_from'] else r['uploaded_at'].strftime('%d %b %Y')}</b>"
            f"<small>{r['n_lines']} lines{' · adds up to the printed total ✓' if r['checked'] else ''}</small></span>"
            f"<span class='chip{' ok' if r['status'] == 'ready' else ' warn' if r['status'] in ('failed', 'needs_cards') else ''}'>"
            f"{label[r['status']]}</span></a>" for r in rows) + "</div>"
        body += "<p style='margin-top:16px'><a href=/spend>See what your spend earned →</a></p>"
    return "Statements", body


def statement_page(conn, sid, person):
    w = h()
    e = w.e
    s = conn.execute("SELECT * FROM statements WHERE id = %s AND person = %s", (sid, person["id"])).fetchone()
    if not s:
        return None
    if s["status"] == "reading":
        return "Reading…", ("<meta http-equiv=refresh content=4><h1>Reading your statement…</h1><div class=panel><p>The AI model "
                            "is reading the transactions. This usually takes under a minute; this page refreshes by itself.</p></div>")
    try:
        meta = crypto.dec_json(conn, s["meta_enc"], f"statements.meta_enc:{sid}") if s["meta_enc"] else {}
    except crypto.Unavailable:
        return "Statement", unavailable()
    delete = (f"<form method=post action=/statements/{sid}/delete style='margin-top:22px'><button class=quiet>Delete this statement"
              "</button></form>")
    if s["status"] == "failed":
        return "Couldn't read", (f"<h1>Couldn't read that one</h1><div class=panel><p>{e(meta.get('error') or 'Something went wrong.')}</p>"
                                 "<a href=/statements>Try another upload →</a></div>" + delete)
    secs = meta.get("sections") or []
    mapped = {r["section"]: r["card"] for r in conn.execute(
        "SELECT section, max(card_id) card FROM stmt_lines WHERE statement_id = %s GROUP BY section", (sid,)).fetchall()}
    allc = conn.execute("SELECT c.id, c.name, b.name bank FROM cards c JOIN banks b ON b.id = c.bank_id WHERE c.status = 'active' "
                        "ORDER BY c.id = ANY(%s) DESC, b.name, c.name", (list(person["held_cards"]) or [""],)).fetchall()
    form = ""
    for sec in secs:
        if not sec["n"]:
            continue
        cur = mapped.get(sec["key"])
        opts = "<option value=''>Choose…</option>" + "".join(
            f"<option value='{e(c['id'])}' {'selected' if c['id'] == cur else ''}>{e(c['name'])} ({e(c['bank'])})</option>" for c in allc)
        form += (f"<div class=row><span class=grow><b>{e(sec['name'] or 'Card')}</b>{' ··' + e(sec['last4']) if sec['last4'] else ''}"
                 f"<small>{sec['n']} lines</small></span><select name='s_{e(sec['key'])}'>{opts}</select></div>")
    head = (f"<h1>{e(meta.get('bank') or 'Statement')}</h1><p class=lede>{s['period_from']:%d %b} – {s['period_to']:%d %b %Y} · "
            f"{s['n_lines']} lines</p>")
    check = (f"<p class=ok>✓ The lines add up to the printed total (S${meta.get('charges', 0):,.2f}).</p>" if s["checked"] else
             f"<p class=warn>The lines add up to S${meta.get('charges', 0):,.2f}"
             f"{', the statement prints S$' + format(meta['printed_total'], ',.2f') if meta.get('printed_total') else ''}. "
             "A line may be missing or extra: the results are still shown, but check them.</p>")
    body = head + check
    body += (("<div class=panel><p><b>Which card is each section?</b></p>" if s["status"] == "needs_cards" else "<h2>Cards</h2><div class=panel>")
             + f"<form method=post action=/statements/{sid}/map><div class=list>{form}</div><p style='margin-top:12px'>"
             "<button>Save</button></p></form></div>")
    if s["status"] == "ready":
        months = sorted({d.strftime("%Y-%m") for d in (s["period_from"], s["period_to"]) if d})
        body += f"<p style='margin-top:18px'><a href='/spend?month={months[-1]}'>See what this spend earned →</a></p>"
    return "Statement", body + delete


def month_options(conn, person, cur):
    ms = [r["m"] for r in conn.execute("SELECT DISTINCT to_char(txn_date, 'YYYY-MM') m FROM stmt_lines WHERE person = %s "
                                       "AND card_id IS NOT NULL ORDER BY m DESC", (person["id"],)).fetchall()]
    return ms, "".join(f"<option value={m} {'selected' if m == cur else ''}>{datetime.date.fromisoformat(m + '-01'):%B %Y}</option>"
                       for m in ms) + f"<option value=all {'selected' if cur == 'all' else ''}>All months</option>"


def replayed(conn, person, cur):
    """(base, lines, results, totals, overflows) for a month ('YYYY-MM' or 'all'), worked out once per change."""
    lines = cache.lines(conn, person["id"], None if cur == "all" else [cur])
    base, allowed = replay.load(conn, person["excluded_banks"], person["boost_amounts"], person["statement_days"], replay.in_store(person),
                                  person["held_cards"], person.get("cashback_too"))
    res, tot, over = cache.memo(conn, person["id"], ("run", cur, cache.person_key(person)),
                                lambda: replay.run(base, lines, list(person["held_cards"]), allowed))
    return base, lines, res, tot, over


def _month(conn, q, person, title, action):
    """Shared top of the two spend pages: (cur, month form) or (None, empty-state page)."""
    ms, _ = month_options(conn, person, None)
    if not ms:
        return None, (title, f"<h1>{title}</h1><div class=panel><p><b>No spend on file yet.</b></p><p class=muted>Upload a card "
                             "statement and this page shows what each purchase earned and what it could have.</p>"
                             "<a class=btn href=/statements>Upload a statement</a></div>")
    cur = q.get("month") if q.get("month") in ms + ["all"] else ms[0]
    _, opts = month_options(conn, person, cur)
    return cur, f"<form class=f data-auto method=get action='{action}'><select name=month aria-label=Month>{opts}</select></form>"


def spend_page(conn, q, person):
    w = h()
    e, num, money = w.e, w.num, w.money
    cur, form = _month(conn, q, person, "Spend", "/spend")
    if cur is None:
        return form
    try:
        base, lines, res, tot, overflows = replayed(conn, person, cur)
    except crypto.Unavailable:
        return "Spend", unavailable()
    ph = w.photos(conn)
    card = lambda cid: base.cards.get(cid, {"id": cid, "name": cid or "?", "bank_id": ""})
    eff = round(100 * tot["actual"] / tot["wallet"]) if tot["wallet"] else 100
    per_all = f"{tot['actual'] / tot['spend']:.2f}".rstrip("0").rstrip(".") if tot["spend"] else "0"
    body = (f"<h1>Spend</h1>{form}"
            f"<div class=grid><a class=tile href='/spend/lines?month={cur}'><b>{money(tot['spend'])}</b><span>on cards<small class=go>every purchase ›</small></span></a>"
            f"<a class=tile href='/spend/lines?month={cur}'><b>{num(tot['actual'], 0)}</b><span>miles earned · {per_all} per S$ overall<small class=go>what each purchase earned ›</small></span></a>"
            f"<a class=tile href='/spend/lines?month={cur}&show=missed'><b>{num(tot['wallet'], 0)}</b><span>miles with your best card every time<small class=go>where you missed out ›</small></span></a>"
            f"<div class=tile><b>{eff}%</b><span>of what your wallet could earn</span></div></div>")
    missed = tot["wallet"] - tot["actual"]
    if missed > 50:
        body += (f"<div class=hint>You left about <b>{num(missed, 0)} miles</b> on the table with cards already in your wallet "
                 f"(≈ S${missed * replay.CENTS_PER_MILE / 100:,.0f}).</div>")
    tp = replay.tips(res, base, off=tot.get("_off"), held=[c for c in person["held_cards"] if c in base.cards])

    def capnote(t):
        out = ""
        if t["used_before"] is not None:
            out += (f"<small>When you paid ({t['first']:%d %b}) your real spend had used {money(t['used_before'])} of its "
                    f"{money(t['cap'])} {e(t['cap_label'])} cap.</small>")
        if t["over"]:
            out += (f"<small class=warn>{money(t['over'])} of this is above that cap and earns only the base rate there. "
                    "The miles shown already count that.</small>")
        sp = t["split"]
        if sp:
            out += (f"<small class=ok>Better still, pay in two parts if the shop lets you: {money(sp['on_best'])} on "
                    f"{e(card(t['best'])['name'])} and {money(sp['amount'])} on {e(card(sp['card'])['name'])} "
                    f"(+{num(sp['extra'], 0)} more miles).</small>")
        m = f"{t['first']:%Y-%m}"
        out += (f"<small><a href='/spend/lines?month={m}&card={e(t['best'])}'>What was on {e(card(t['best'])['name'])} that month ›</a> · "
                f"<a href='/card/{e(t['best'])}'>what this card earns ›</a></small>")
        return out

    if tp:
        body += "<h2>Next time</h2><div class=list>" + "".join(
            f"<div class='row wrapt'>{look.face(card(t['best']), 'sm', ph.get(t['best']))}<span class=grow><b>{e(t['merchant'].title())}: "
            f"use {e(card(t['best'])['name'])}</b><small>not {e(card(t['used'])['name'])} · {t['n']}× · {money(t['spend'])}</small>"
            f"{capnote(t)}</span>"
            f"<span class=r>+{num(t['gain'], 0)}<small>miles</small></span></div>" for t in tp) + "</div>"
    if overflows:
        body += "<h2>Where you went over a cap</h2><div class=list>" + "".join(
            f"<div class='row wrapt'>{look.face(card(o['card']), 'sm', ph.get(o['card']))}<span class=grow><b>{e(card(o['card'])['name'])}</b> "
            f"<small>{e(o['label'])} cap {money(o['cap'])} used up on {o['hit']:%d %b}; {money(o['over_spend'])} after that earned the base rate</small>"
            f"</span><span class=r>−{num(o['lost'], 0)}<small>miles</small></span></div>" for o in overflows) + "</div>"
    extra = tot["any"] - tot["wallet"]
    body += (f"<div class=list style='margin-top:26px'><a class=row href='/spend/lines?month={cur}'><span class=grow><b>Every transaction</b>"
             f"<small>{len(res)} purchases, each with the card you used and the better ones</small></span><span class=muted>›</span></a>"
             f"<a class=row href=/advisor><span class=grow><b>Cards you don't have</b><small>"
             f"{'Up to ' + num(extra, 0) + ' more miles here with the right additions' if extra > 50 else 'Nothing in the catalogue would add much here'}"
             "</small></span><span class=muted>›</span></a></div>"
             f"<p class=muted style='margin-top:18px'><small>{assume(person)} {rules_note(conn)}</small></p>")
    return "Spend", body


def lines_page(conn, q, person):
    """Every transaction as a row you can open: the card used, then your better cards and the cards to add."""
    w = h()
    e, num, money = w.e, w.num, w.money
    cur, form = _month(conn, q, person, "Transactions", "/spend/lines")
    if cur is None:
        return form
    try:
        base, lines, res, tot, _ = replayed(conn, person, cur)
    except crypto.Unavailable:
        return "Transactions", unavailable()
    card = lambda cid: base.cards.get(cid, {"id": cid, "name": cid or "?", "bank_id": ""})
    ph = w.photos(conn)
    only = q.get("show") == "missed"
    one = q.get("card") if q.get("card") in base.cards else None   # one card's month, oldest first, with its cap filling up
    span = []
    for k in ("since", "until"):   # with a card: only its purchases in one cap period (Wallet links here)
        try:
            span.append(datetime.date.fromisoformat(q.get(k, "")) if one else None)
        except ValueError:
            span.append(None)
    since, until = span

    def per(m, amt):
        """Miles per S$, short: 4, 1.4, 0.4."""
        return f"{m / amt:.1f}".rstrip("0").rstrip(".") if amt else "0"

    def alts(pairs, ref, amt=0):
        out = []
        for m, cid in pairs:
            gain = m - ref
            out.append(f"{e(card(cid)['name'])}: {num(m, 0)} miles" + (f" ({per(m, amt)} per S$)" if amt else "")
                       + (f" <b class=ok>+{num(gain, 0)}</b>" if gain >= 1 else ""))
        return "<br>".join(out)

    rows = []
    for x in sorted(res, key=lambda x: x["date"], reverse=not one):
        gain = x["wallet"] - x["actual"]
        if (only and gain < 1) or (one and x.get("card_id") != one):
            continue
        if (since and x["date"] < since) or (until and x["date"] > until):
            continue
        amount = money(x["amount"]) if x["amount"] >= 100 else "$%.2f" % x["amount"]
        better = f"<small class=ok>+{num(gain, 0)} with {e(card(x['wallet_card'])['name'])}</small>" if gain >= 1 and x["wallet_card"] else ""
        wo = x.get("wallet_over")
        if better and wo:
            better += f"<small class=warn>{money(wo['amount'])} of it above the {money(wo['cap'])} {e(wo['label'])} cap</small>"
        cs = x.get("cap_state")
        capline = (f"<small>{e(cs['label'])} cap: {money(cs['used'])} of {money(cs['cap'])} used after this</small>" if cs and one else "")
        ra = x.get("real_alts") or []
        if ra and ra[0][1] == x.get("card_id") or (ra and ra[0][0] - x["actual"] < 1):
            real = ("<p class=ok>✓ Right card on the day: with your caps as they really stood, nothing in your wallet earned more."
                    + (" The higher figures below assume every earlier purchase had also gone on its best card." if gain >= 1 else "") + "</p>")
        elif ra:
            real = (f"<p><b>Better on the day, with your bonus limits as they really stood:</b><br>{alts(ra, x['actual'], x['amount'])}</p>")
        else:
            real = ""
        split = ""
        if wo and wo.get("split") and gain >= 1:
            sp = wo["split"]
            split = (f"<p class=ok>Or pay in two parts: {money(x['amount'] - wo['amount'])} on {e(card(x['wallet_card'])['name'])}, "
                     f"{money(sp['amount'])} on {e(card(sp['card'])['name'])} (+{num(sp['extra'], 0)} more miles).</p>")
        mon = f"{x['date']:%Y-%m}"
        seemore = " · ".join(f"<a href='/spend/lines?month={mon}&card={e(c)}'>{e(card(c)['name'])} that month ›</a>"
                             for c in dict.fromkeys([x.get("card_id"), x.get("wallet_card")]) if c in base.cards and c != one)
        over = (f"<p class=warn>{e(x['over']['label'])} cap was used up: {money(x['over']['amount'])} of this earned the base rate.</p>"
                if x.get("over") else "")
        add = [(m, c) for m, c in (x.get("add_alts") or []) if m - max(x["wallet"], x["actual"]) >= 1]
        meta = (f"Shop type {e(x.get('mcc') or 'not known')}" + (f" (from the {e(x.get('mcc_src'))})" if x.get("mcc_src") else "") +
                (" · online" if x.get("online") else "") + (f" · {e(x.get('currency'))}" if x.get("fcy") else "") +
                (f" {num(x['fcy_amount'], 2)}" if x.get("fcy") and x.get("fcy_amount") else "") +
                (" · estimated, statement pending" if x.get("est") else "") +
                (" · match day, result not in yet: this may rise" if x.get("match_pending") else ""))
        how = x.get("pay") or ("online" if x.get("online") else None)
        fixed = x.get("paid_src") == "bank" and x.get("pay")   # the bank's line says it: not the person's to change
        paid = ("" if fixed or not pipeline.shop_key(x.get("merchant")) else
                "<form method=post action=/spend/paid class=f style='margin:6px 0'>"
                f"<input type=hidden name=line value='{e(x.get('id'))}'><input type=hidden name=back value='/spend/lines?month={cur}'>"
                "<label>How was this paid? <select name=ch><option value=''>my usual way</option>"
                + "".join(f"<option value={k} {'selected' if how == k and x.get('paid_src') == 'you' else ''}>{t}</option>" for k, t in pipeline.CHANNELS)
                + "</select></label> <button class='quiet sm'>Remember for this shop</button></form>")
        meta += f" · paid: {e(dict(pipeline.CHANNELS).get(how, 'your usual way'))} ({e(pipeline.PAID_SRC.get(x.get('paid_src'), 'your usual way'))})"
        wa = x.get("wallet_alts") or []
        # the "perfect month" list only when it says something the on-the-day list doesn't
        same = bool(ra) and bool(wa) and ra[0][1] == wa[0][1] and abs(ra[0][0] - wa[0][0]) < 1
        ideal = "" if same or not wa else (f"<p><b>If every earlier purchase had also gone on its best card:</b><br>"
                                             f"{alts(wa, x['actual'], x['amount'])}</p>")
        rate = x["actual"] / x["amount"] if x["amount"] else 0.0
        tone = "ok" if rate >= 3 else ("warn" if rate < 1 else "")
        sums = (f"<p><b>{amount} × {per(x['actual'], x['amount'])} miles per S$ = {num(x['actual'], 0)} miles</b> on "
                f"{e(card(x['card_id'])['name'])}.</p>")
        rows.append(f"<details class=tx><summary>{look.face(card(x['card_id']), 'sm', ph.get(x['card_id']))}"
                    f"<span class=grow><b>{e(x['merchant'][:40])}</b>"
                    f"<small>{x['date']:%d %b} · {e(card(x['card_id'])['name'])}{' · estimated' if x.get('est') else ''}</small>{better}{capline}</span>"
                    f"<span class='c amt'>{amount}</span><span class='c mi {tone}'>{num(x['actual'], 0)}</span>"
                    f"<span class='c rt {tone}'>{per(x['actual'], x['amount'])}</span></summary>"
                    f"<div class=more>{sums}<p class=muted>{meta}</p>{paid}{over}{real}"
                    f"{ideal}{split}"
                    f"<p><b>Cards to add:</b><br>{alts(add, max(x['wallet'], x['actual']), x['amount']) if add else 'none better than yours'}</p>"
                    f"{'<p><small>' + seemore + '</small></p>' if seemore else ''}</div></details>")
    n_missed = sum(1 for x in res if x["wallet"] - x["actual"] >= 1)
    filt = (f"<nav class=sub style='margin-top:-6px'><a href='/spend/lines?month={cur}'{'' if only else ' class=on'}>All {len(res)}</a>"
            f"<a href='/spend/lines?month={cur}&show=missed'{' class=on' if only else ''}>Could have earned more ({n_missed})</a></nav>")
    in_span = lambda x: not (since and x["date"] < since) and not (until and x["date"] > until)
    by = {}
    for x in res:
        if in_span(x):
            t = by.setdefault(x.get("card_id"), {"n": 0, "spend": 0.0, "miles": 0.0})
            t["n"] += 1
            t["spend"] += x["amount"]
            t["miles"] += x["actual"]
    span_q = (f"&since={since:%Y-%m-%d}&until={until:%Y-%m-%d}" if since and until else "")
    chips = "".join(
        f"<a class='cardchip{' on' if cid == one else ''}' href='/spend/lines?month={cur}&card={e(cid)}{span_q}'>"
        f"{look.face(card(cid), 'sm', ph.get(cid))}<span><b>{num(t['miles'], 0)} miles</b><small>{e(card(cid)['name'][:22])} · {t['n']}×</small></span></a>"
        for cid, t in sorted(by.items(), key=lambda kv: -kv[1]["miles"]) if cid in base.cards)
    cardbar = (f"<div class=cardbar><a class='cardchip all{'' if one else ' on'}' href='/spend/lines?month={cur}'><span><b>All cards</b>"
               f"<small>{num(sum(t['miles'] for t in by.values()), 0)} miles</small></span></a>{chips}</div>") if len(by) > 1 or one else ""
    if one:
        t = by.get(one, {"n": 0, "spend": 0.0, "miles": 0.0})
        c1 = card(one)
        ppm = float(c1["points_per_mile"]) if c1.get("points_per_mile") and c1.get("kind") != "cashback" and float(c1["points_per_mile"]) != 1 else None
        when = (f"{since:%d %b} to {until:%d %b}" if since and until else "this period")
        pts = (f"<div class=tile><b>{num(t['miles'] * ppm, 0)}</b><span>{e(c1.get('programme') or 'points')} "
               f"({num(ppm)} make 1 mile)</span></div>") if ppm else ""
        rate = f"{t['miles'] / t['spend']:.2f}".rstrip("0").rstrip(".") if t["spend"] else "0"
        filt = (f"<div class=grid><div class=tile><b>{num(t['miles'], 0)}</b><span>miles from {e(c1['name'])}, {when}</span></div>{pts}"
                f"<div class=tile><b>{money(t['spend'])}</b><span>spent on it · {t['n']} purchase{'' if t['n'] == 1 else 's'}</span></div>"
                f"<div class=tile><b>{rate}</b><span>miles per S$ on this card</span></div></div>"
                f"<p class=muted><small>Oldest first, with its bonus limit filling up as you paid. <a href='/card/{e(one)}'>What this card earns ›</a></small></p>")
        if since:
            form = ""
    filt = cardbar + filt
    head = ("<div class=txhead><span></span><span>Purchase</span><span class=c>Price</span><span class=c>Miles</span>"
            "<span class=c>Per S$</span></div>") if rows else ""
    body = (f"<h1>Transactions</h1>{form}{filt}<div class='list txlist'>{head}{''.join(rows) or '<div class=row>Nothing here.</div>'}</div>"
            "<p class=muted style='margin-top:14px'><small>Tap a line for the detail. Each ranking is worked out at the moment of the "
            "purchase, with every cap as it stood then. +N = miles more than you earned. Wrong MCC? "
            "<a href=/merchant>Add or fix the shop</a> and it's used next time. " + rules_note(conn) + "</small></p>")
    return "Transactions", body


def simulate_page(conn, q, person, picked):
    w = h()
    e, num, money = w.e, w.num, w.money
    try:
        lines = cache.lines(conn, person["id"])
    except crypto.Unavailable:
        return "Try a wallet", unavailable()
    if not lines:
        return "Try a wallet", "<h1>Try a wallet</h1><div class=panel><p>Upload a statement first.</p><a class=btn href=/statements>Statements</a></div>"
    base, allowed = replay.load(conn, person["excluded_banks"], person["boost_amounts"], person["statement_days"], replay.in_store(person),
                                  person["held_cards"], person.get("cashback_too"))
    held = list(person["held_cards"])
    chosen = [c for c in picked if c in base.cards] or held
    pk = cache.person_key(person)
    res, tot, _ = cache.memo(conn, person["id"], ("sim", tuple(sorted(chosen)), pk), lambda: replay.run(base, lines, chosen, allowed))
    _, now, _ = cache.memo(conn, person["id"], ("run", "all", pk), lambda: replay.run(base, lines, held, allowed))
    months = len({ln["date"].strftime("%Y-%m") for ln in lines}) or 1
    use = Counter()
    for x in res:
        if x["wallet_card"]:
            use[x["wallet_card"]] += x["amount"]
    ph = w.photos(conn)
    box = lambda cid: (f"<label class=chip><input type=checkbox name=card value='{e(cid)}' {'checked' if cid in chosen else ''}> "
                       f"{e(base.cards[cid]['name'])}</label> ")
    by_bank = defaultdict(list)
    for cid in sorted(allowed, key=lambda c: base.cards[c]["name"]):
        if cid not in held:
            by_bank[base.cards[cid]["bank_id"]].append(cid)
    extra_on = any(c not in held for c in chosen)
    pick = ("<h3>Your cards</h3><p>" + "".join(box(c) for c in held if c in base.cards) + "</p>"
            f"<details{' open' if extra_on else ''}><summary>Add other cards</summary>" +
            "".join(f"<h3>{e(bank.upper())}</h3><p>{''.join(box(c) for c in ids)}</p>" for bank, ids in sorted(by_bank.items())) +
            "</details>")
    diff = tot["wallet"] - now["wallet"]
    body = ("<h1>Try a wallet</h1><p class=lede>Your real spend, replayed on any set of cards, always using the best one.</p>"
            f"<div class=grid><div class=tile><b>{num(tot['actual'], 0)}</b><span>miles you actually earned</span></div>"
            f"<div class=tile><b>{num(now['wallet'], 0)}</b><span>best use of your current cards</span></div>"
            f"<div class=tile><b>{num(tot['wallet'], 0)}</b><span>with the cards picked below</span></div>"
            f"<div class=tile><b>{'+' if diff >= 0 else '−'}{num(abs(diff) * 12 / months, 0)}</b><span>miles a year vs your current cards</span></div></div>")
    if use:
        body += "<h2>How the picked cards would be used</h2><div class=list>" + "".join(
            f"<div class=row>{look.face(base.cards[cid], 'sm', ph.get(cid))}<span class=grow><b>{e(base.cards[cid]['name'])}</b></span>"
            f"<span class=r>{money(amt)}</span></div>" for cid, amt in use.most_common()) + "</div>"
    body += (f"<h2>Pick the cards</h2><form method=get action=/simulate data-auto><p>{pick}</p></form>"
             f"<p class=muted><small>{len(lines)} transactions over {months} month(s). {assume(person)}</small></p>")
    return "Try a wallet", body
