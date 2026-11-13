"""What a bot and the daily message say about the miles.

  caps(conn, person)       -> the /api/caps payload for a bot: this period per card bucket, the card to use per
                              purchase type right now (cap room counted), boost, wallet, earn rates, goals, promos ending
  summary_text(conn, p)    -> the daily message: today's spend vs best card, caps left, minimums at risk, goals
  alerts(conn, p)          -> new events since the last run: cap almost used, minimum at risk near period end, a goal
                              now within reach, Saver seats seen for a goal (each sent once, remembered in meta)

Messages go out through a Telegram bot, for one person, only while sending is switched on. Bot token, chat, person and
the switch are set by an admin on the App page (config.py). Sending off = preview: the text is logged and shown on
/summary, nothing is sent."""
import datetime
import json
import urllib.request

from . import config, crypto, engine, goals, replay, statements

# "store" = however this person pays in a shop (Settings: phone / watch, or the physical card). Petrol used to be
# worked out as chip-and-PIN, which is wrong for someone who only ever pays by phone.
PURCHASES = [("Dining", "5812", "store", False), ("Groceries", "5411", "store", False),
             ("Online shopping", "5999", "online", False), ("Taxi / Grab", "4121", "online", False),
             ("Petrol", "5541", "store", False), ("Overseas (in person)", "5812", "store", True),
             ("Overseas online", "5999", "online", True), ("Everything else (in a shop)", "7299", "store", False)]


def caps(conn, person):
    from .web import wallet_context, boost_warnings
    today = datetime.date.today()
    ctx = wallet_context(conn, person)
    room = ctx["room"] if ctx else None
    names = {c["id"]: c["name"] for c in conn.execute("SELECT id, name FROM cards").fetchall()}
    out = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "person": person["id"]}
    out["caps"] = [{"card": x["card"], "card_name": names.get(x["card"]), "label": x["label"], "mpd": x["rate"], "cap": x["cap"],
                    "used": round(x["used"], 2), "left": round(x["left"], 2) if x["left"] is not None else None,
                    "period": "statement" if x["statement"] else "calendar", "from": x["start"].isoformat(), "to": x["end"].isoformat(),
                    "resets_in_days": x["days_left"], "min_spend": x["min_spend"],
                    "min_left": round(x["min_left"], 2) if x["min_left"] is not None else None}
                   for x in (ctx["progress"] if ctx else [])]
    routing = []
    store = replay.in_store(person)
    out["pays_in_shops_by"] = {"mobile": "phone or watch (Apple Pay), never the physical card" if person.get("pay_method") == "mobile"
                               else "phone or watch (Apple Pay) mostly", "tap": "the physical card"}[store]
    for label, mcc, pay, fcy in PURCHASES:
        pay = store if pay == "store" else pay
        ranked = engine.rank(conn, {"mcc": mcc, "pay": pay, "fcy": fcy, "merchant": "", "today": today},
                             set(person["held_cards"]), person["excluded_banks"], person["boost_amounts"], room)
        mine = [x for x in ranked if x["held"]][:2]
        if mine:
            r = mine[0]
            note = (r["rule"]["label"] or r["rule"]["bucket"]) if r["rule"] else "base rate"
            if r["extra"].get("left") is not None:
                note += f", S${r['extra']['left']:,.0f} left"
            routing.append({"purchase": label, "card": r["card"]["name"], "mpd": round(r["rate"], 2), "note": note,
                            "fallback": mine[1]["card"]["name"] if len(mine) > 1 else None})
    out["routing"] = routing
    out["boost_warnings"] = boost_warnings(conn, person)
    try:
        wal, _ = goals.wallet(conn, person)
        rt = goals.rates(conn, person)
    except crypto.Unavailable:
        wal, rt = None, None
    out["wallet"] = wal
    out["forecast"] = ({"per_month": round(rt["actual"]), "best_card_per_month": round(rt["best"]),
                        "with_card": rt["plus_card"]["name"] if rt["plus_card"] else None,
                        "with_card_per_month": round(rt["plus"]) if rt["plus"] else None} if rt else None)
    out["goals"] = goal_list(conn, person, wal, rt)
    try:   # annual fees: amount, next date, the bank's waiver rule and where the person's spend stands against it
        from . import cache, fees
        cat = cache.catalogue(conn)
        rows = fees.tracker(conn, person, cat, cache.lines(conn, person["id"]), cache.fee_lines(conn, person["id"]), today)
        out["fees"] = [{"card": r["card"], "card_name": names.get(r["card"]), "fee": r["fee"], "fee_source": r["fee_src"],
                        "due": r["due"].isoformat() if r["due"] else None, "days": r["days"], "due_source": r["due_src"],
                        "last_fee_waived": r["waived_last"], "chase": r["chase"],
                        "charged": r["last"]["date"].isoformat() if r["last"] else None, "waiver_rule": (r["waiver"] or {}).get("theirs"),
                        "waiver_status": (r["progress"] or {}).get("status"), "advice": fees.advice(r)} for r in rows]
    except crypto.Unavailable:
        out["fees"] = []
    soon = today + datetime.timedelta(days=30)
    out["promos_ending"] = [{"card": names.get(r["card_id"]), "label": r["label"], "until": r["promo_until"].isoformat()}
                            for r in conn.execute("SELECT card_id, label, promo_until FROM card_rules WHERE valid_to IS NULL AND "
                                                  "promo_until BETWEEN %s AND %s AND card_id = ANY(%s)",
                                                  (today, soon, list(person["held_cards"]) or [""])).fetchall()]
    return out


def goal_list(conn, person, wal, rt):
    have = float((wal or {}).get("total") or 0)
    today = datetime.date.today()
    rows = conn.execute("SELECT g.*, a.saver, a.city, a.next_date FROM goals g LEFT JOIN award_prices a ON a.dest = g.dest AND "
                        "a.cabin = g.cabin WHERE g.person = %s ORDER BY g.position, g.created_at", (person["id"],)).fetchall()
    out, spent = [], 0.0
    for g in rows:
        cost = g["miles"] or ((g["saver"] or 0) * (2 if g["trip"] == "return" else 1) * g["pax"])
        spent += cost
        need = max(0.0, spent - have)
        out.append({"goal": f"{g['city'] or g['dest']} {goals.CABINS[g['cabin']]} {'return' if g['trip'] == 'return' else 'one-way'}"
                            + (f" x{g['pax']}" if g["pax"] > 1 else ""),
                    "id": g["id"], "dest": g["dest"], "miles": cost, "need": need, "reachable": need <= 0,
                    "eta": goals.when(need, rt["actual"], today) if rt else None,
                    "saver_next": g["next_date"].isoformat() if g["next_date"] else None})
    return out


def summary_text(conn, person):
    today = datetime.date.today()
    c = caps(conn, person)
    lines = [f"✈️ Cards · {today:%a %d %b}"]
    try:
        from . import cache
        ls = cache.lines(conn, person["id"], [today.strftime("%Y-%m")])
    except crypto.Unavailable:
        ls = []
    tl = [ln for ln in ls if ln["date"] == today and ln["amount"] > 0]
    if tl:
        base, allowed = replay.load(conn, person["excluded_banks"], person["boost_amounts"], person["statement_days"], replay.in_store(person),
                                  person["held_cards"], person.get("cashback_too"))
        res, tot, _ = replay.run(base, ls, list(person["held_cards"]), allowed)
        todays = [x for x in res if x["date"] == today]
        earned, best = sum(x["actual"] for x in todays), sum(x["wallet"] for x in todays)
        lines.append(f"Today: S${sum(x['amount'] for x in todays):,.0f} → {earned:,.0f} miles (best of your cards: {best:,.0f})")
        for x in sorted(todays, key=lambda x: x["actual"] - x["wallet"])[:3]:
            if x["wallet"] - x["actual"] >= 50 and x["wallet_card"] != x["card_id"]:
                lines.append(f"• {x['merchant'][:28]}: {base.cards[x['wallet_card']]['name']} would have given +{x['wallet'] - x['actual']:,.0f}")
    else:
        lines.append("No card spend today.")
    low = [x for x in c["caps"] if x["cap"] and x["left"] is not None and x["used"] > 0]
    if low:
        lines.append("")
        lines.append("💳 Bonus left:")
        for x in sorted(low, key=lambda x: x["left"])[:6]:
            lines.append(f"• {x['card_name']} {x['label']}: S${x['left']:,.0f} of S${x['cap']:,.0f} ({x['resets_in_days']} d)")
    mins = [x for x in c["caps"] if x["min_left"]]
    if mins:
        lines.append("")
        lines.append("🎯 Minimum spend:")
        for x in mins[:4]:
            lines.append(f"• {x['card_name']} ({x['label']}): S${x['min_left']:,.0f} more by {datetime.date.fromisoformat(x['to']):%d %b}")
    for w in c["boost_warnings"]:
        lines += ["", f"⚠️ {w}"]
    if c["goals"]:
        lines.append("")
        lines.append("🏁 Goals:")
        for g in c["goals"][:3]:
            lines.append(f"• {g['goal']}: " + ("you have the miles ✓" if g["reachable"] else f"{g['need']:,.0f} to go, ~{g['eta']}"))
    return "\n".join(lines)


def _sent(conn, key):
    return conn.execute("SELECT 1 FROM meta WHERE key = %s", (f"alert:{key}",)).fetchone() is not None


def _mark(conn, key):
    conn.execute("INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                 (f"alert:{key}", datetime.date.today().isoformat()))
    conn.commit()


def alerts(conn, person):
    """New events, each once: (key, text)."""
    c = caps(conn, person)
    out = []
    for x in c["caps"]:
        if x["cap"] and x["left"] is not None and x["used"] >= 0.9 * x["cap"]:
            out.append((f"{person['id']}:cap90:{x['card']}:{x['label']}:{x['from']}",
                        f"💳 {x['card_name']} {x['label']}: only S${x['left']:,.0f} of the S${x['cap']:,.0f} bonus left "
                        f"until {datetime.date.fromisoformat(x['to']):%d %b}. Next best: see Which card?"))
        if x["min_left"] and x["resets_in_days"] <= 5:
            out.append((f"{person['id']}:min:{x['card']}:{x['label']}:{x['from']}",
                        f"🎯 {x['card_name']}: S${x['min_left']:,.0f} more by {datetime.date.fromisoformat(x['to']):%d %b} "
                        f"to unlock {x['mpd']:g} mpd on {x['label']}."))
    for g in c["goals"]:
        if g["reachable"]:
            out.append((f"{person['id']}:goal:{g['id']}", f"🏁 You have the miles for {g['goal']} ({g['miles']:,.0f})."))
        if g["saver_next"]:
            out.append((f"{person['id']}:seats:{g['id']}:{g['saver_next']}",
                        f"🪑 Saver seats seen for {g['goal']}: next {datetime.date.fromisoformat(g['saver_next']):%d %b}. "
                        "Check singaporeair.com."))
    for f in c.get("fees") or []:   # the fee was charged: time to ask for the waiver (once), then a nudge if nothing came back
        if f.get("chase") == "ask":
            out.append((f"{person['id']}:feeask:{f['card']}:{f['charged']}", f"🧾 {f['card_name']}: {f['advice']}"))
        elif f.get("chase") == "asked" and "over a month" in f["advice"]:
            out.append((f"{person['id']}:feechase:{f['card']}:{f['charged']}", f"🧾 {f['card_name']}: {f['advice']}"))
    for f in c.get("fees") or []:   # one reminder a month before each annual fee
        if f["due"] and f["days"] is not None and 0 <= f["days"] <= 30 and f["fee"] != 0:
            out.append((f"{person['id']}:fee:{f['card']}:{f['due']}",
                        f"🧾 {f['card_name']}: annual fee{' of S$%s' % format(f['fee'], ',.0f') if f['fee'] else ''} due around "
                        f"{datetime.date.fromisoformat(f['due']):%d %b}. {f['advice']}"))
    return [(k, t) for k, t in out if not _sent(conn, k)]


def telegram(text, force=False):
    """Send one message. Nothing goes out while sending is switched off, unless force (the Test button)."""
    tok, chat = config.app("telegram_token"), config.app("telegram_chat")
    if not force and not config.flag("notify") or not tok or not chat:
        return False
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",
                                 data=json.dumps({"chat_id": chat, "text": text}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200


def run(mode):
    from . import db
    with db.connect() as conn:
        from . import outside, people
        outside.refresh(conn)   # today's match result and miles balance, when those services are set up
        who = config.app("notify_person") or (people.configured() or [(None, None)])[0][0]
        if not who:
            first = conn.execute("SELECT id FROM people ORDER BY admin DESC, id LIMIT 1").fetchone()
            who = first and first["id"]
        p = conn.execute("SELECT * FROM people WHERE id = %s", (who,)).fetchone()
        if not p:
            print("notify: no such person (choose who the messages are about on the App page)", flush=True)
            return 1
        if mode == "summary":
            text = summary_text(conn, p)
            sent = telegram(text)
            conn.execute("INSERT INTO meta (key, value) VALUES ('summary:last', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         (json.dumps({"at": datetime.datetime.now().astimezone().isoformat(timespec="minutes"), "text": text, "sent": sent}),))
            conn.commit()
            print(f"summary {'sent' if sent else 'preview only (sending is off)'}: {len(text.splitlines())} lines", flush=True)
        else:
            new = alerts(conn, p)
            for k, t in new:
                sent = telegram(t)
                if sent or not config.flag("notify"):
                    _mark(conn, k)   # preview mode: remembered too, so switching on doesn't replay old events
                print(f"alert {'sent' if sent else 'preview'}: {k.split(':')[1]}", flush=True)
            print(f"alerts: {len(new)} new", flush=True)
    return 0
