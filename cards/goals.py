"""Flight goals: miles you have, miles each flight costs, and when you get there at your real earn rate, at the
best-card rate, and with the best card you don't have yet. Goals are taken in order (each one spends the miles before
the next)."""
import datetime
import time
import uuid

from . import crypto, look, replay, statements
from . import awards

CABINS = {"Y": "Economy", "W": "Premium Economy", "J": "Business", "F": "First / Suites"}


def h():
    from . import web
    return web


def wallet(conn, person):
    """The person's miles wallet: a live feeder's, else the app's own (points.wallet)."""
    from . import points
    return points.wallet(conn, person)


def rates(conn, person):
    """Miles a month from the last 3 complete months of real spend: earned, best of your cards, + the best card you
    don't hold (and which one)."""
    today = datetime.date.today()
    first = today.replace(day=1)
    months = []
    d = first
    for _ in range(3):
        d = (d - datetime.timedelta(days=1)).replace(day=1)
        months.append(d.strftime("%Y-%m"))
    from . import cache
    lines = cache.lines(conn, person["id"], months)
    n = len({ln["date"].strftime("%Y-%m") for ln in lines})
    if not lines or not n:
        return None

    def work():
        base, allowed = replay.load(conn, person["excluded_banks"], person["boost_amounts"], person["statement_days"], replay.in_store(person),
                                  person["held_cards"], person.get("cashback_too"))
        held = list(person["held_cards"])
        have = replay.assign(base, lines, held)
        actual = replay.run(base, lines, held, held)[1]["actual"]
        wg = replay.worth_getting(base, lines, held, allowed, n, limit=1)
        return {"months": n, "actual": actual / n, "best": have["miles"] / n,
                "plus": (have["miles"] / n + wg[0]["extra_month"]) if wg else None,
                "plus_card": base.cards[wg[0]["card"]] if wg else None}

    return cache.memo(conn, person["id"], ("rates", cache.person_key(person), today), work)


def when(need, per_month, today):
    if need <= 0:
        return "now"
    if not per_month or per_month <= 0:
        return "never at this rate"
    days = need / per_month * 30.44
    d = today + datetime.timedelta(days=days)
    return f"{d:%b %Y}"


def chart_table(w):
    """SIA's whole one-way chart from Singapore, folded away: every zone, every cabin, Saver and Advantage."""
    c = awards.chart()
    num = w.num
    k = lambda v: "–" if v is None else num(v / 1000, 1).rstrip("0").rstrip(".") + "k"
    rows = ""
    for z, name in c["zones"].items():
        for cab, label in CABINS.items():
            sv, ad = c["from_sin"]["saver"].get(cab, {}).get(z), c["from_sin"]["advantage"].get(cab, {}).get(z)
            first = f"<td rowspan=4><b>Zone {z}</b><small style='display:block;white-space:normal'>{w.e(name)}</small></td>" if cab == "Y" else ""
            rows += f"<tr>{first}<td>{label.split(' /')[0]}</td><td class=n>{k(sv)}</td><td class=n>{k(ad)}</td></tr>"
    return ("<details><summary>All KrisFlyer award prices from Singapore</summary><div class=wrap><table class=earn><tr><th>To</th><th>Cabin</th>"
            f"<th class=n>Saver</th><th class=n>Advantage</th></tr>{rows}</table></div><p class=muted><small>One-way, in thousands of miles; "
            "double for a return. – = Singapore Airlines gives no fixed price (it points to its mileage calculator), or the cabin has no "
            "Advantage award (Premium Economy). Access is priced by demand.</small></p></details>")


def page(conn, q, person):
    w = h()
    e, num = w.e, w.num
    today = datetime.date.today()
    try:
        wal, wal_at = wallet(conn, person)
        rt = rates(conn, person)
    except crypto.Unavailable:
        from .spendweb import unavailable
        return "Goals", unavailable()
    have = float((wal or {}).get("total") or 0)
    body = "<h1>My flights</h1>"
    body += (f"<div class=grid><div class=tile><b>{num(have, 0)}</b><span>KrisFlyer miles you could have now</span></div>")
    if rt:
        body += (f"<a class=tile href=/spend><b>{num(rt['actual'], 0)}</b><span>miles a month (last {rt['months']} months)"
                 "<small class=go>see your spend ›</small></span></a>"
                 f"<a class=tile href='/spend/lines?show=missed'><b>{num(rt['best'], 0)}</b><span>a month with your best card every time"
                 "<small class=go>where you missed out ›</small></span></a>")
        if rt["plus"]:
            why = f"/advisor/card?card={e(rt['plus_card']['id'])}"
            body += (f"<a class=tile href='{why}'><b>{num(rt['plus'], 0)}</b><span>a month adding {e(rt['plus_card']['name'])}"
                     "<small class=go>why this card ›</small></span></a>")
    body += "</div>"
    if wal:
        pools = wal.get("pools") or []
        parts = [f"KrisFlyer account {num(wal.get('kf_account') or 0, 0)}"] + [
            f"{e(p['name'])} {num(p['balance'], 0)} → {num(p['kf'], 0)} KF" for p in pools if p.get("kf")]
        body += (f"<p class=muted><small>{' · '.join(parts)} · as of {wal_at:%d %b %H:%M} ("
                 f"{'see <a href=/points>Points</a>' if wal.get('own') else 'from your feeder'}; points convert in "
                 "whole transfer blocks)</small></p>")
    else:
        body += "<p class=muted>No miles balance yet: <a href=/points>type one or upload a statement</a>.</p>"
    goals = conn.execute("SELECT g.*, a.saver, a.advantage, a.city, a.next_date, a.seen_dates FROM goals g LEFT JOIN award_prices a "
                         "ON a.dest = g.dest AND a.cabin = g.cabin WHERE g.person = %s ORDER BY g.position, g.created_at",
                         (person["id"],)).fetchall()
    if goals:
        rows, spent = [], 0.0
        for i, g in enumerate(goals):
            cp = awards.prices(g["dest"], g["cabin"])            # Singapore Airlines' own chart
            per = (cp or {}).get("saver") or g["saver"]          # else the lowest the seat scan saw
            mult = (2 if g["trip"] == "return" else 1) * g["pax"]
            cost = g["miles"] or ((per or 0) * mult)
            avail = max(0.0, have - spent)
            need = max(0.0, cost - avail)
            pct = min(100, round(100 * avail / cost)) if cost else 0
            spent += cost
            need_all = max(0.0, spent - have)   # stacked: everything up to and incl. this goal
            etas = []
            if rt:
                etas.append(f"<b>{when(need_all, rt['actual'], today)}</b> at your rate")
                etas.append(f"{when(need_all, rt['best'], today)} with best-card use")
                if rt["plus"]:
                    etas.append(f"{when(need_all, rt['plus'], today)} adding <a href='/advisor/card?card={e(rt['plus_card']['id'])}'>"
                                f"{e(rt['plus_card']['name'])} ›</a>")
            seats = (f" · Saver seats next {g['next_date']:%d %b}" if g["next_date"] else
                     (" · no Saver seats seen lately" if g["saver"] and not g["seen_dates"] else ""))
            label = f"{e(g['city'] or g['dest'])} · {CABINS[g['cabin']]} · {'return' if g['trip'] == 'return' else 'one-way'}" + \
                    (f" · {g['pax']} people" if g["pax"] > 1 else "")
            price = (f"{num(cost, 0)} miles" + (" (your price)" if g["miles"] else f" (Saver {num(per, 0)} one-way)" if per else " (price unknown: set one)"))
            levels = ""
            if cp:
                avail_now = max(0.0, have - (spent - cost))
                cells = []
                for key, name in awards.LEVELS:
                    one = cp[key]
                    if one is None:
                        why = "no fixed price: use SIA's calculator" if key == "saver" else ("not sold in this cabin" if g["cabin"] == "W" else "no fixed price")
                        cells.append(f"<div class=lvl><span>{name}</span><span><b>–</b><small>{why}</small></span></div>")
                        continue
                    tot = one * mult
                    short = tot - avail_now
                    each = f"{num(one, 0)} each way, per person · " if mult > 1 else ""
                    cells.append(f"<div class=lvl><span>{name}</span><span><b>{num(tot, 0)} miles</b><small>{each}"
                                 f"{'<span class=ok>you have enough</span>' if short <= 0 else num(short, 0) + ' short today'}</small></span></div>")
                seen = awards.access_seen(cp, g["advantage"])
                cells.append("<div class=lvl><span>Access</span><span>" + (f"<b>up to {num(seen * mult, 0)} miles</b><small>the highest the weekly seat scan saw; "
                             "priced by demand when you book</small>" if seen else "<b>varies</b><small>no fixed price: set by demand when you book</small>")
                             + "</span></div>")
                levels = (f"<details class=lv{' open' if i == 0 else ''}><summary>Saver, Advantage and Access prices</summary>{''.join(cells)}"
                          f"<small style='white-space:normal;display:block;margin-top:6px'>Zone {cp['zone']}: {e(cp['zone_name'])}. "
                          "Singapore Airlines' chart; taxes and fees are extra.</small></details>")
            bar = (f"<div style='height:8px;border-radius:4px;background:var(--seg);margin:8px 0 4px'><div style='height:8px;border-radius:4px;"
                   f"width:{pct}%;background:{'var(--ok)' if pct >= 100 else 'var(--accent)'}'></div></div>")
            ctl = (f"<form method=post action=/goals/{g['id']}/up style='display:inline'><button class='quiet sm'>↑</button></form> "
                   f"<form method=post action=/goals/{g['id']}/delete style='display:inline'><button class='quiet sm'>Remove</button></form>")
            rows.append(f"<div class='row goal'><span class=grow><b>{i + 1}. {label}</b><small style='white-space:normal'>{price}{seats}</small>{bar}"
                        f"<small style='white-space:normal'>{'✓ you have the miles' if need <= 0 else num(need, 0) + ' more miles'}"
                        f"{' · ' + ' · '.join(etas) if etas and need_all > 0 else ''}</small></span><span>{ctl}</span>{levels}</div>")
        body += ("<h2>Your goals</h2><p class=muted>In order: each goal uses the miles before the next, so the dates stack.</p>"
                 "<div class=list>" + "".join(rows) + "</div>")
    dests = conn.execute("SELECT DISTINCT dest, city, region FROM award_prices ORDER BY region, city").fetchall()
    if dests:
        opts = "".join(f"<option value='{e(d['dest'])}'>{e(d['city'] or d['dest'])} ({e(d['dest'])})</option>" for d in dests)
        body += ("<h2>Add a flight</h2><div class=panel><form method=post action=/goals class=stack>"
                 f"<select name=dest required><option value=''>Where to (from Singapore)?</option>{opts}</select>"
                 "<div class=seg>" + "".join(f"<label><input type=radio name=cabin value={k} {'checked' if k == 'J' else ''}><span>{v.split(' /')[0]}</span></label>"
                                              for k, v in CABINS.items()) + "</div>"
                 "<div class=seg><label><input type=radio name=trip value=oneway checked><span>One-way</span></label>"
                 "<label><input type=radio name=trip value=return><span>Return</span></label></div>"
                 "<select name=pax>" + "".join(f"<option value={n}>{n} {'person' if n == 1 else 'people'}</option>" for n in range(1, 5)) + "</select>"
                 "<input name=miles inputmode=numeric placeholder='Miles price (optional, else the Saver price seen)'>"
                 "<button>Add goal</button></form>"
                 "<p class=muted><small>Saver and Advantage prices are Singapore Airlines' own one-way chart from Singapore "
                 f"(<a href='{e(awards.chart()['source'])}' target=_blank rel='noopener noreferrer'>effective 1 Nov 2025 ↗</a>). A goal is "
                 "counted at the Saver price unless you type your own. Access has no chart: SIA prices it by demand when you book. "
                 "Seat dates come from the weekly seats.aero scan. Always confirm on singaporeair.com.</small></p></div>")
        body += chart_table(w)
    else:
        body += "<p class=muted>Award prices arrive with the next feed run.</p>"
    return "Goals", body


def flights_page(conn, q, person):
    """Where can I go: every destination Singapore Airlines prices from Singapore, for one cabin, against the miles
    the person could have today, with the month the rest would be earned and the next date with Saver seats."""
    w = h()
    e, num = w.e, w.num
    today = datetime.date.today()
    try:
        wal, wal_at = wallet(conn, person)
        rt = rates(conn, person)
    except crypto.Unavailable:
        from .spendweb import unavailable
        return "Flights", unavailable()
    have = float((wal or {}).get("total") or 0)
    cabin = q.get("cabin") if q.get("cabin") in CABINS else "J"
    trip = "return" if q.get("trip") == "return" else "oneway"
    try:
        pax = min(4, max(1, int(q.get("pax") or 1)))
    except ValueError:
        pax = 1
    mult = (2 if trip == "return" else 1) * pax
    body = "<h1>Flights</h1><div class=grid>"
    body += f"<a class=tile href=/points><b>{num(have, 0)}</b><span>KrisFlyer miles you could have now<small class=go>where they are ›</small></span></a>"
    if rt:
        body += (f"<a class=tile href=/spend><b>{num(rt['actual'], 0)}</b><span>miles a month (last {rt['months']} months)"
                 "<small class=go>see your spend ›</small></span></a>")
    n_goals = conn.execute("SELECT count(*) n FROM goals WHERE person = %s", (person["id"],)).fetchone()["n"]
    body += (f"<a class=tile href=/goals><b>{n_goals}</b><span>flight{'' if n_goals == 1 else 's'} you are saving for"
             "<small class=go>my flights ›</small></span></a></div>")
    if not wal:
        body += "<p class=muted>No miles balance yet: <a href=/points>type one or upload a statement</a>.</p>"
    link = lambda **kw: "/flights?" + "&".join(f"{k}={v}" for k, v in {"cabin": cabin, "trip": trip, "pax": pax, **kw}.items())
    seg = lambda items, key, cur: "<div class=seg>" + "".join(
        f"<label><a href='{link(**{key: k})}' style='display:block;text-decoration:none;color:inherit'><input type=radio disabled "
        f"{'checked' if str(k) == str(cur) else ''}><span>{e(t)}</span></a></label>" for k, t in items) + "</div>"
    body += ("<h2>Where can I go</h2>"
             + seg([(k, v.split(" /")[0]) for k, v in CABINS.items()], "cabin", cabin)
             + seg([("oneway", "One-way"), ("return", "Return")], "trip", trip)
             + seg([(n, f"{n} {'person' if n == 1 else 'people'}") for n in range(1, 5)], "pax", pax))
    rows = conn.execute("SELECT dest, city, region, saver, advantage, next_date FROM award_prices WHERE cabin = %s AND saver IS NOT NULL "
                        "ORDER BY region, saver, city", (cabin,)).fetchall()
    if not rows:
        return "Flights", body + "<p class=muted>No award prices for this cabin yet. They load with the weekly catalogue run.</p>"
    mine = {(g["dest"], g["cabin"]) for g in conn.execute("SELECT dest, cabin FROM goals WHERE person = %s", (person["id"],)).fetchall()}
    can = sum(1 for r in rows if r["saver"] * mult <= have)
    body += (f"<p class=muted>{can} of {len(rows)} places are within reach today at the Saver price"
             + (f", {CABINS[cabin].split(' /')[0]} {'return' if trip == 'return' else 'one-way'}" + (f" for {pax}" if pax > 1 else "")) + ".</p>")
    region, out = None, []
    for r in rows:
        if r["region"] != region:
            if region is not None:
                out.append("</div>")
            region = r["region"]
            out.append(f"<h3 style='margin:18px 0 6px'>{e(region or 'Other')}</h3><div class=list>")
        cost = float(r["saver"]) * mult
        short = cost - have
        if short <= 0:
            state = "<span class='chip ok'>you have enough</span>"
        else:
            eta = when(short, rt["actual"], today) if rt else None
            state = f"<span class=chip>{num(short, 0)} short{' · ' + eta if eta else ''}</span>"
        seats = f" · Saver seats next {r['next_date']:%d %b}" if r["next_date"] else ""
        adv = f" · Advantage {num(float(r['advantage']) * mult, 0)}" if r["advantage"] else ""
        if (r["dest"], cabin) in mine:
            act = "<a class=muted href=/goals style='font-size:13px'>in my flights</a>"
        else:
            act = ("<form method=post action=/goals style='display:inline'>"
                   f"<input type=hidden name=dest value='{e(r['dest'])}'><input type=hidden name=cabin value={cabin}>"
                   f"<input type=hidden name=trip value={trip}><input type=hidden name=pax value={pax}>"
                   "<button class='quiet sm'>Save for it</button></form>")
        out.append(f"<div class=row><span class=grow><b>{e(r['city'] or r['dest'])}</b> <span class=muted>{e(r['dest'])}</span> {state}"
                   f"<small style='white-space:normal'>Saver {num(cost, 0)} miles{adv}{seats}</small></span><span>{act}</span></div>")
    out.append("</div>")
    body += "".join(out)
    body += ("<p class=muted style='margin-top:14px'><small>Prices are Singapore Airlines' own one-way chart from Singapore; taxes and fees "
             "are extra. Dates with seats come from the weekly seat scan when one is set up. The month shown is when the missing "
             "miles would be earned at the current monthly rate. Always confirm on singaporeair.com.</small></p>")
    return "Flights", body


def post(conn, path, form, person):
    """Returns a redirect location, or None."""
    if path == "/goals":
        dest = (form.get("dest") or [""])[0][:4].upper()
        cabin = (form.get("cabin") or ["J"])[0]
        trip = (form.get("trip") or ["oneway"])[0]
        try:
            pax = max(1, min(6, int((form.get("pax") or ["1"])[0])))
        except ValueError:
            pax = 1
        raw = (form.get("miles") or [""])[0].replace(",", "").strip()
        miles = int(raw) if raw.isdigit() and 0 < int(raw) < 5_000_000 else None
        ok = conn.execute("SELECT 1 FROM award_prices WHERE dest = %s", (dest,)).fetchone()
        if ok and cabin in CABINS and trip in ("oneway", "return"):
            pos = conn.execute("SELECT coalesce(max(position), 0) + 1 p FROM goals WHERE person = %s", (person["id"],)).fetchone()["p"]
            conn.execute("INSERT INTO goals (id, person, dest, cabin, trip, pax, miles, position) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (uuid.uuid4().hex, person["id"], dest, cabin, trip, pax, miles, pos))
            conn.commit()
        return "/goals"
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "goals":
        g = conn.execute("SELECT * FROM goals WHERE id = %s AND person = %s", (parts[1], person["id"])).fetchone()
        if g and parts[2] == "delete":
            conn.execute("DELETE FROM goals WHERE id = %s", (g["id"],))
        elif g and parts[2] == "up":
            prev = conn.execute("SELECT id, position FROM goals WHERE person = %s AND (position, created_at) < (%s, %s) ORDER BY "
                                "position DESC, created_at DESC LIMIT 1", (person["id"], g["position"], g["created_at"])).fetchone()
            if prev:
                conn.execute("UPDATE goals SET position = %s WHERE id = %s", (prev["position"], g["id"]))
                conn.execute("UPDATE goals SET position = %s WHERE id = %s", (g["position"] if g["position"] != prev["position"] else g["position"] + 1, prev["id"]))
        conn.commit()
        return "/goals"
    return None
