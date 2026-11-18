"""In-memory caches, so a page costs one small query instead of loading the catalogue and decrypting every line again.

  catalogue(conn)        cards, rules, exclusions, boosts, picture versions: reloaded only when the catalogue stamp
                         (newest change id + row counts) moves. Checked at most every 5 s.
  lines(conn, person)    the person's decrypted lines, reloaded when their line stamp moves
  memo(conn, person, k)  results worked out from those lines (replays, advice): dropped with the stamp

Decrypted lines live in this process's memory only (like the data keys), never on disk, and only for the signed-in
person's own requests."""
import json
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

_lock = threading.Lock()
_cat = {"stamp": None, "at": 0.0, "data": None}
_lines = {}     # person -> (stamp, spend lines, annual-fee lines)
_memo = {}      # person -> (stamp, {key: value})
_img = {}       # (card id, size) -> (version, mime, bytes)
_req = threading.local()   # what one page view has read already: see request()


def _stamp(conn):
    r = conn.execute(
        "SELECT (SELECT coalesce(max(id), 0) FROM catalogue_changes) a, (SELECT count(*) FROM card_rules WHERE valid_to IS NULL) b,"
        " (SELECT count(*) FROM cards) c, (SELECT count(*) FROM card_boosts) d, (SELECT count(*) FROM card_mcc_exclusions "
        "WHERE valid_to IS NULL) f, (SELECT coalesce(extract(epoch FROM max(updated_at)), 0) FROM card_images) g, "
        "(SELECT coalesce(extract(epoch FROM max(checked_at)), 0) FROM cards) h, (SELECT count(*) FROM card_tc_checks) i, "
        "(SELECT coalesce(extract(epoch FROM max(checked_at)), 0) FROM card_tc_checks) j, "
        "(SELECT count(*) || ':' || coalesce(extract(epoch FROM max(updated_at)), 0) FROM match_results) k").fetchone()
    return tuple(r.values())


def usual_exclusions():
    """The MCCs Singapore banks commonly give nothing on (cards.json exclusion_sets.common), expanded."""
    import pathlib
    from . import store
    sets = json.loads((pathlib.Path(__file__).parent / "data" / "cards.json").read_text()).get("exclusion_sets", {})
    return {m for spec in sets.get("common", {}).get("mcc", []) for m in store.expand_mcc(spec)}


def catalogue(conn):
    now = time.monotonic()
    with _lock:
        if _cat["data"] is not None and now - _cat["at"] < 5:
            return _cat["data"]
    stamp = _stamp(conn)
    with _lock:
        if _cat["data"] is not None and _cat["stamp"] == stamp:
            _cat["at"] = now
            return _cat["data"]
    cards = {c["id"]: c for c in conn.execute(
        "SELECT c.*, b.name bank, p.name programme, p.points_per_mile FROM cards c JOIN banks b ON b.id = c.bank_id "
        "LEFT JOIN programmes p ON p.id = c.programme_id").fetchall()}
    for r in conn.execute("SELECT card_id, data FROM card_tc_checks WHERE subject = 'annual_fee' AND status <> 'not_found' AND "
                          "data ? 'fee'").fetchall():
        if r["card_id"] in cards:   # the bank document's fee, used when neither the person nor the catalogue has one
            try:
                cards[r["card_id"]]["bank_fee"] = float(r["data"]["fee"])
            except (TypeError, ValueError):
                pass
    rules = defaultdict(list)
    for r in conn.execute("SELECT * FROM card_rules WHERE valid_to IS NULL ORDER BY rate DESC").fetchall():
        rules[r["card_id"]].append(r)
    # Rules as they were: a change closes a rule's row (valid_to) and opens a new one, so a purchase made before the
    # change can be valued with the rule of its time. hist: card -> (date of its last recorded change, every version
    # with "_a" <= date < "_b"; None = open-ended. The first version of a bucket also covers everything before it).
    past = defaultdict(list)
    for r in conn.execute("SELECT * FROM card_rules WHERE valid_to IS NOT NULL ORDER BY rate DESC").fetchall():
        past[r["card_id"]].append(r)
    hist = {}
    for cid, old in past.items():
        first = {}
        for r in old + rules.get(cid, []):
            if r["bucket"] not in first or r["valid_from"] < first[r["bucket"]]:
                first[r["bucket"]] = r["valid_from"]
        every = [dict(r, _a=None if r["valid_from"] == first[r["bucket"]] else r["valid_from"], _b=r["valid_to"])
                 for r in old + rules.get(cid, [])]
        every.sort(key=lambda r: -float(r["rate"] or 0))
        hist[cid] = (max(r["valid_to"] for r in old), every)
    excl = defaultdict(set)
    for r in conn.execute("SELECT card_id, mcc FROM card_mcc_exclusions WHERE valid_to IS NULL").fetchall():
        excl[r["card_id"]].add(r["mcc"])
    # A card nobody has checked against its bank's terms carries no exclusion list of its own, and would otherwise
    # "earn" on everything (insurance, utilities, e-wallet top-ups...). Assume the list banks share until it is checked.
    usual = usual_exclusions()
    assumed = set()
    for r in conn.execute("SELECT id FROM cards WHERE status = 'active' AND depth <> 'full'").fetchall():
        excl[r["id"]] |= usual
        assumed.add(r["id"])
    boosts = defaultdict(list)
    for b in conn.execute("SELECT * FROM card_boosts ORDER BY id").fetchall():
        boosts[b["card_id"]].append(b)
    photos = {r["card_id"]: str(int(r["updated_at"].timestamp())) for r in
              conn.execute("SELECT card_id, updated_at FROM card_images").fetchall()}
    offers = defaultdict(list)
    for o in conn.execute("SELECT * FROM signup_offers WHERE active ORDER BY bonus DESC").fetchall():
        if o["card_id"]:
            offers[o["card_id"]].append(o)
    partners = defaultdict(list)
    for t in conn.execute("SELECT * FROM transfer_partners").fetchall():
        partners[t["programme_id"]].append(t)
    mcc_names = {r["mcc"]: r["name"] for r in conn.execute("SELECT mcc, name FROM mcc_codes").fetchall()}
    from . import fixtures
    fx, per_month = fixtures.declared()
    data = {"fixtures": fx, "results": fixtures.load(conn), "per_month": per_month, "stamp": stamp, "mcc_names": mcc_names, "cards": cards, "active": {k: c for k, c in cards.items() if c["status"] == "active"},
            "rules": rules, "excl": excl, "assumed_excl": assumed, "boosts": boosts, "photos": photos, "offers": offers, "partners": partners, "app": {}, "hist": hist}
    with _lock:
        _cat.update(stamp=stamp, at=now, data=data)
        _img.clear()
    return data


@contextmanager
def request():
    """While a page is drawn, a few small reads are made once instead of by every helper that needs them: the person's
    line stamp (a page made up to six, each a pass over all the person's stored lines) and the saved settings. Only for
    reading pages: a form post writes."""
    _req.held = {}
    try:
        yield
    finally:
        _req.held = None


def once(key, fn):
    """fn() once per page view (see request()); outside one, every time."""
    held = getattr(_req, "held", None)
    if held is None:
        return fn()
    if key not in held:
        held[key] = fn()
    return held[key]


def drop(key):
    """Forget a value once() holds, after writing what it was read from."""
    held = getattr(_req, "held", None)
    if held:
        held.pop(key, None)


def _line_stamp(conn, person):
    return once(("lines", person), lambda: _read_line_stamp(conn, person))


def _read_line_stamp(conn, person):
    # the stored bytes are part of the stamp: a line corrected in place (a feeder or Wealthfolio re-sent it) is seen
    r = conn.execute("SELECT count(*) n, md5(coalesce(string_agg(id || coalesce(card_id, '') || md5(data_enc), '' ORDER BY id), '')) h "
                     "FROM stmt_lines WHERE person = %s", (person,)).fetchone()
    # ... and what decides how the lines are read: the card's source and the saved "how I paid" answers
    p = conn.execute("SELECT md5(coalesce(settings_enc, ''::bytea)) s, card_sources::text c FROM people WHERE id = %s", (person,)).fetchone() or {}
    return (r["n"], r["h"], p.get("s"), p.get("c"))


def lines(conn, person, months=None):
    """Decrypted lines with a card, optionally only some 'YYYY-MM' months. Raises crypto.Unavailable like load_lines."""
    from . import statements
    stamp = _line_stamp(conn, person)
    with _lock:
        hit = _lines.get(person)
    if not hit or hit[0] != stamp:
        from . import fees
        every = [ln for ln in statements.load_lines(conn, person) if ln["card_id"]]
        # an annual fee printed on a statement is not spend: it earns nothing and belongs to the fee tracker
        hit = (stamp, [ln for ln in every if not fees.is_fee(ln.get("merchant"))], [ln for ln in every if fees.is_fee(ln.get("merchant"))])
        with _lock:
            _lines[person] = hit
            _memo.pop(person, None)
    if months is None:
        return hit[1]
    want = set(months)
    return [ln for ln in hit[1] if ln["date"].strftime("%Y-%m") in want]


def fee_lines(conn, person):
    """Annual-fee lines found on the person's statements (kept out of lines())."""
    lines(conn, person)
    with _lock:
        return _lines[person][2]


def memo(conn, person, key, fn):
    """fn() once per (person's lines, catalogue, the person's own settings in `key`)."""
    stamp = (_line_stamp(conn, person), catalogue(conn)["stamp"])
    with _lock:
        hit = _memo.get(person)
        if hit and hit[0] == stamp and key in hit[1]:
            return hit[1][key]
    val = fn()
    with _lock:
        hit = _memo.get(person)
        if not hit or hit[0] != stamp:
            hit = (stamp, {})
            _memo[person] = hit
        if len(hit[1]) > 200:
            hit[1].clear()
        hit[1][key] = val
    return val


def person_key(person):
    """The settings a replay depends on: a change to any of them is a different result."""
    import json
    return (tuple(person["held_cards"]), tuple(person["excluded_banks"]), json.dumps(person["boost_amounts"], sort_keys=True),
            json.dumps(person.get("statement_days") or {}, sort_keys=True), json.dumps(person.get("card_fees") or {}, sort_keys=True),
            json.dumps(person.get("fee_dates") or {}, sort_keys=True), person.get("pay_method"))


def image(conn, cid, small):
    """(mime, bytes) of a card picture; small = a 192 px wide copy for list rows (the full picture is 900 px)."""
    ver = catalogue(conn)["photos"].get(cid)
    if ver is None:
        return None
    with _lock:
        hit = _img.get((cid, small))
    if hit and hit[0] == ver:
        return hit[1], hit[2]
    row = conn.execute("SELECT mime, data FROM card_images WHERE card_id = %s", (cid,)).fetchone()
    if not row:
        return None
    mime, data = row["mime"], bytes(row["data"])
    if small:
        try:
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            im.thumbnail((192, 192))
            out = io.BytesIO()
            im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "WEBP", quality=82)
            mime, data = "image/webp", out.getvalue()
        except Exception:
            pass
    with _lock:
        if len(_img) > 600:
            _img.clear()
        _img[(cid, small)] = (ver, mime, data)
    return mime, data
