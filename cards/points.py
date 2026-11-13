"""Points per person: what you hold per programme and what it converts to (KrisFlyer first, other partners too).

The wallet (own_wallet) is worked out in the app:
  a programme's balance   the newer of the balance printed on its latest statement and a balance the person typed on
                          the Points page, + the points its cards earned since that date (from the person's lines)
  the KrisFlyer account   the newest of AwardWallet (optional, the person's own Business API key: Settings), a typed
                          balance and a statement, + the miles cards that pay straight into KrisFlyer earned since
  what it converts to     whole transfer blocks only, the leftover shown
A feeder may still send a wallet of its own (POST /api/feed, "wallet"); it is shown while the feeder is alive (it
sent something in the last 3 days) and the person has not stopped listening to it."""
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

from . import crypto, look, replay, statements


def h():
    from . import web
    return web


def convert(conn, programme, points):
    """[(partner, miles, rule)] for a points balance: whole transfer steps only."""
    out = []
    for t in conn.execute("SELECT partner, ratio_from, ratio_to, block, fee FROM transfer_partners WHERE programme_id = %s",
                          (programme,)).fetchall():
        rf, rt = float(t["ratio_from"]), float(t["ratio_to"])
        step = float(t["block"] or rf)
        usable = (points // step) * step if points >= rf else 0
        miles = int(usable * rt / rf)
        out.append((t["partner"], miles, f"{rf:,.0f} → {rt:,.0f}" + (f" · fee {t['fee']}" if t["fee"] else "")))
    return sorted(out, key=lambda x: (not x[0].startswith("KrisFlyer"), -x[1]))


KF = "KF"                      # the programme whose points ARE KrisFlyer miles (cards that credit the account directly)
AW_API = "https://business.awardwallet.com/api/export/v1"
AW_EVERY = 6 * 3600            # ask AwardWallet at most this often
FEED_DAYS = 3                  # a feeder silent for longer no longer speaks for the wallet


def _settings(conn, pid):
    from . import ingest
    return ingest.settings(conn, pid)


def typed(conn, pid):
    """({programme: {"balance", "date"}}, the typed KrisFlyer account balance or None, the last AwardWallet answer or None)"""
    st = _settings(conn, pid)
    return st.get("points_typed") or {}, st.get("kf_typed"), st.get("kf_aw")


def set_typed(conn, pid, programme, balance, date):
    """Save (balance not None) or remove a typed balance. programme 'KF' = the KrisFlyer account itself."""
    from . import ingest
    st = ingest.settings(conn, pid)
    if programme == KF:
        if balance is None:
            st.pop("kf_typed", None)
        else:
            st["kf_typed"] = {"balance": float(balance), "date": date.isoformat()}
    else:
        t = st.setdefault("points_typed", {})
        if balance is None:
            t.pop(programme, None)
        else:
            t[programme] = {"balance": float(balance), "date": date.isoformat()}
    ingest.save_settings(conn, pid, st)


# ---------------------------------------------------------------- AwardWallet ----------------------------------------------------------------
def aw_fetch(key, user, api=None):
    """The KrisFlyer account as AwardWallet has it -> {"balance", "date", "retrieved", "expiring", "source"}.
    Raises ValueError with a sentence for the person. Only the key and the connected user's id go out."""
    req = urllib.request.Request(f"{(api or AW_API).rstrip('/')}/connectedUser/{urllib.parse.quote(str(user))}",
                                 headers={"X-Authentication": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            accts = json.load(r).get("accounts") or []
    except urllib.error.HTTPError as ex:
        raise ValueError("AwardWallet refused the key." if ex.code in (401, 403) else
                         "AwardWallet does not know that connected user id." if ex.code == 404 else
                         f"AwardWallet answered {ex.code}.") from None
    except Exception as ex:
        raise ValueError(f"AwardWallet could not be reached ({type(ex).__name__}).") from None
    kf = next((a for a in accts if a.get("code") == "singaporeair" or "krisflyer" in str(a.get("displayName") or "").lower()), None)
    if not kf:
        raise ValueError("Connected, but that AwardWallet user has no KrisFlyer account.")
    prop = lambda kind: next((p.get("value") for p in kf.get("properties") or [] if p.get("kind") == kind), None)
    try:
        bal = int(float(kf["balanceRaw"]))
    except (KeyError, TypeError, ValueError):
        raise ValueError("AwardWallet's answer has no KrisFlyer balance.") from None
    return {"balance": bal, "date": datetime.date.today().isoformat(), "retrieved": kf.get("lastRetrieveDate") or kf.get("lastChangeDate"),
            "expiring": prop(6), "source": "AwardWallet"}


def aw_refresh(conn, pid, force=False, api=None):
    """Ask AwardWallet for this person's KrisFlyer balance when they connected it, at most every 6 hours. The last
    good answer is kept, so an outage shows the earlier balance with its date. -> the answer, or None (not connected,
    asked recently, or it failed: the reason is on their Settings page)."""
    from . import config, ingest
    cfg = config.load(conn, pid)
    if not (cfg.get("aw_key") and cfg.get("aw_user")):
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        last = json.loads(cfg.get("aw_last") or "{}")
    except ValueError:
        last = {}
    if not force and last.get("at"):
        try:
            if (now - datetime.datetime.fromisoformat(last["at"])).total_seconds() < AW_EVERY:
                return None
        except ValueError:
            pass
    try:
        got = aw_fetch(cfg["aw_key"], cfg["aw_user"], api)
    except ValueError as ex:
        config.put(conn, "aw_last", json.dumps({"at": now.isoformat(timespec="seconds"), "error": str(ex)}), pid)
        conn.commit()
        return None
    st = ingest.settings(conn, pid)
    st["kf_aw"] = got
    ingest.save_settings(conn, pid, st)
    config.put(conn, "aw_last", json.dumps({"at": now.isoformat(timespec="seconds"), "error": None}), pid)
    conn.commit()
    return got


def aw_refresh_all(conn):
    n = 0
    for p in conn.execute("SELECT id FROM people ORDER BY id").fetchall():
        try:
            n += bool(aw_refresh(conn, p["id"]))
        except Exception:
            conn.rollback()
    return n


# ---------------------------------------------------------------- the wallet ----------------------------------------------------------------
def _newest(cands):
    """The candidate with the newest date; on the same date the later one in the list wins."""
    best = None
    for c in cands:
        if c and c.get("balance") is not None and c.get("date") and (best is None or c["date"] >= best["date"]):
            best = c
    return best


def own_wallet(conn, person, today=None):
    """The wallet from what the app holds itself (see the module text). Same shape a feeder sends:
    {"pools": [{programme, name, unit, balance, kf, leftover, rule, as_of, earned_since, source}], "from_banks",
     "kf_account", "kf_expiring", "kf_account_as_of", "kf_account_source", "total", "own": True}, or None when the
    person has no balance of any kind yet."""
    from . import cache, replay
    pid = person["id"]
    row = conn.execute("SELECT points_enc FROM people WHERE id = %s", (pid,)).fetchone()
    stmt = crypto.dec_json(conn, row["points_enc"], f"people.points_enc:{pid}") if row and row["points_enc"] else {}
    mine, kf_typed, kf_aw = typed(conn, pid)
    progs = {r["id"]: r for r in conn.execute("SELECT id, name FROM programmes").fetchall()}
    anchors = {}
    for prog in set(stmt) | set(mine):
        if prog == KF:
            continue
        a = _newest([dict(stmt[prog], source=f"{stmt[prog].get('bank') or 'the'} statement") if prog in stmt else None,
                     dict(mine[prog], source="typed") if prog in mine else None])
        if a:
            anchors[prog] = a
    account = _newest([dict(stmt[KF], source="statement") if KF in stmt else None,
                       dict(kf_typed, source="typed") if kf_typed else None,
                       dict(kf_aw, source=kf_aw.get("source") or "AwardWallet") if kf_aw else None])
    if not anchors and not account:
        return None
    base, _ = replay.load(conn, person["excluded_banks"], person["boost_amounts"], person["statement_days"], replay.in_store(person),
                          person["held_cards"], person.get("cashback_too"))
    try:
        lines = cache.lines(conn, pid)
    except crypto.Unavailable:
        lines = []

    def earned(prog, since):
        cards = [cid for cid, c in base.cards.items() if c.get("programme_id") == prog]
        later = [ln for ln in lines if ln["date"] > since and ln["card_id"] in cards and ln["amount"] > 0]
        if not later:
            return 0.0
        res, _, _ = replay.run(base, later, cards, cards)
        ppm = {cid: float(base.cards[cid].get("points_per_mile") or 1) for cid in cards}
        return sum(x["actual"] * ppm.get(x["card_id"], 1) for x in res)

    pools, from_banks = [], 0
    for prog, a in anchors.items():
        since = datetime.date.fromisoformat(a["date"])
        got = earned(prog, since)
        total = a["balance"] + got
        t = conn.execute("SELECT ratio_from, ratio_to, block FROM transfer_partners WHERE programme_id = %s AND partner LIKE 'KrisFlyer%%' "
                         "ORDER BY partner LIMIT 1", (prog,)).fetchone()
        if t:
            rf, rt = float(t["ratio_from"]), float(t["ratio_to"])
            step = float(t["block"] or rf)
            usable = (total // step) * step if total >= rf else 0
            kf, left, rule = int(usable * rt / rf), total - usable, f"{rf:,.0f} → {rt:,.0f} KF"
        else:
            kf, left, rule = 0, total, "no KrisFlyer transfer known"
        pools.append({"programme": prog, "name": a.get("name") or (progs.get(prog) or {}).get("name") or prog, "unit": "points",
                      "balance": round(total), "kf": kf, "leftover": round(left), "rule": rule, "as_of": a["date"],
                      "earned_since": round(got), "source": a["source"], "expiry": a.get("expiry")})
        from_banks += kf
    kf_total = None
    if account:
        kf_total = round(account["balance"] + earned(KF, datetime.date.fromisoformat(account["date"])))
    return {"pools": sorted(pools, key=lambda x: -x["kf"]), "from_banks": from_banks, "kf_account": kf_total,
            "kf_expiring": account and account.get("expiring"), "kf_account_as_of": account and account["date"],
            "kf_account_source": account and account["source"], "total": from_banks + (kf_total or 0), "own": True}


def feed_alive(conn, pid):
    """A feeder speaks for this person: it sent something in the last 3 days."""
    row = conn.execute("SELECT feed_at FROM people WHERE id = %s", (pid,)).fetchone()
    return bool(row and row["feed_at"] and
                (datetime.datetime.now(datetime.timezone.utc) - row["feed_at"]).total_seconds() < FEED_DAYS * 86400)


def wallet(conn, person):
    """(the wallet, when it is from): the feeder's while it is alive and has one, else the app's own. (None, None)
    when there is no balance of any kind."""
    from . import cache
    pid = person["id"]
    row = conn.execute("SELECT wallet_enc, wallet_at, md5(coalesce(points_enc, ''::bytea)) h FROM people WHERE id = %s", (pid,)).fetchone()
    if row and row["wallet_enc"] and feed_alive(conn, pid):
        return crypto.dec_json(conn, row["wallet_enc"], f"people.wallet_enc:{pid}"), row["wallet_at"]
    today = datetime.date.today()
    own = cache.memo(conn, pid, ("own_wallet", cache.person_key(person), today, row and row["h"]),
                     lambda: own_wallet(conn, person, today))
    return (own, datetime.datetime.now().astimezone()) if own else (None, None)


def post(conn, form, person):
    """The Points page's forms: a typed balance per programme, or the KrisFlyer account. -> a sentence or None."""
    get = lambda k: (form.get(k) or [""])[0].strip()
    prog = get("programme")
    ok = prog == KF or conn.execute("SELECT 1 FROM programmes WHERE id = %s", (prog,)).fetchone()
    if not ok:
        return "Pick a programme."
    if get("action") == "remove":
        set_typed(conn, person["id"], prog, None, None)
        conn.commit()
        return None
    try:
        bal = float(get("balance").replace(",", ""))
        day = datetime.date.fromisoformat(get("date")) if get("date") else datetime.date.today()
    except ValueError:
        return "That balance or date could not be read."
    if bal < 0 or bal > 100_000_000 or day > datetime.date.today():
        return "That balance or date does not look right."
    set_typed(conn, person["id"], prog, bal, day)
    conn.commit()
    return None


def page(conn, q, person):
    w = h()
    e, num = w.e, w.num
    try:
        wal, wal_at = wallet(conn, person)
        mine, kf_typed, kf_aw = typed(conn, person["id"])
    except crypto.Unavailable:
        from .spendweb import unavailable
        return "Points", unavailable()
    body = "<h1>Points</h1>" + (f"<p class=warn>{e(q.get('note'))}</p>" if q.get("note") else "")
    if wal:
        pools = wal.get("pools") or []
        acct = wal.get("kf_account")
        body += (f"<div class=grid><div class=tile><b>{num(wal.get('total') or 0, 0)}</b><span>KrisFlyer miles you could have now</span></div>"
                 f"<div class=tile><b>{num(acct or 0, 0)}</b><span>in your KrisFlyer account"
                 + (f"<small>{e(wal.get('kf_account_source') or '')} · {e(wal.get('kf_account_as_of') or '')}"
                    f"{' · expiring: ' + e(str(wal['kf_expiring'])) if wal.get('kf_expiring') else ''}</small>" if acct is not None else "")
                 + "</span></div></div><div class=list>" + "".join(
                     f"<div class=row><span class=grow><b>{e(p['name'])}</b><small>{num(p['balance'], 0)} {e(p.get('unit') or 'points')} · "
                     f"{e(p.get('rule') or '')}{' · leftover ' + num(p['leftover'], 0) if p.get('leftover') else ''}"
                     f"{' · as of ' + e(p['as_of']) if p.get('as_of') else ''}"
                     f"{' (' + e(p['source']) + ')' if p.get('source') else ''}"
                     f"{' + ~' + num(p['earned_since'], 0) + ' earned since' if p.get('earned_since') else ''}</small></span>"
                     f"<span class=r>{num(p['kf'], 0)} KF</span></div>"
                     for p in pools) + "</div>")
        src = ("Worked out here: the balance on your latest statement or the one you typed below, plus points your cards earned "
               "since; the KrisFlyer account from AwardWallet, a statement or what you typed." if wal.get("own") else
               f"From your feeder ({wal_at:%d %b %H:%M}).")
        body += f"<p class=muted><small>{src} Whole transfer blocks only.</small></p>"
    else:
        body += ("<div class=panel><p>No points balances yet.</p><p class=muted>They are read from the rewards balance printed on "
                 "card statements (<a href=/statements>upload one</a>), or type what the bank's app shows below.</p></div>")
    progs = conn.execute("SELECT DISTINCT p.id, p.name FROM programmes p JOIN cards c ON c.programme_id = p.id WHERE c.id = ANY(%s) "
                         "ORDER BY p.name", (list(person["held_cards"]) or [""],)).fetchall()
    today = datetime.date.today().isoformat()
    rows = []
    for p in list(progs) + ([{"id": KF, "name": "KrisFlyer account"}] if not any(x["id"] == KF for x in progs) else []):
        have = kf_typed if p["id"] == KF else mine.get(p["id"])
        label = "KrisFlyer account (miles already there)" if p["id"] == KF else p["name"]
        rows.append(
            "<form method=post action=/points class='row wrapt f'>"
            f"<input type=hidden name=programme value='{e(p['id'])}'><span class=grow><b>{e(label)}</b>"
            f"<small>{'typed: ' + num(have['balance'], 0) + ' as of ' + e(have['date']) if have else 'nothing typed'}</small></span>"
            f"<input name=balance inputmode=numeric size=9 placeholder='balance' value='{num(have['balance'], 0).replace(',', '') if have else ''}'> "
            f"<input type=date name=date max='{today}' value='{e(have['date']) if have else today}'> "
            "<button class='quiet sm'>Save</button>"
            + (" <button class='quiet sm' name=action value=remove>Remove</button>" if have else "") + "</form>")
    body += ("<h2>Type a balance</h2><p class=muted>What the bank's app or the KrisFlyer site shows today. The newer of this and a "
             "statement is used, and points earned on that programme's cards after its date are added. "
             + ("AwardWallet is connected: it keeps the KrisFlyer account current. " if kf_aw else
                "The KrisFlyer account can also be kept current by AwardWallet (<a href='/settings#awardwallet'>Settings</a>). ")
             + f"</p><div class=list>{''.join(rows)}</div>")
    return "Points", body
