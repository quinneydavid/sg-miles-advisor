"""Writes into the catalogue with history: a changed rule closes the old row and opens a new one, and every change
lands in catalogue_changes (what the weekly diff and the bot's "card changed" lines read)."""
import json
import re

from psycopg.types.json import Jsonb

RULE_FIELDS = ("label", "rate", "match", "cap", "cap_period", "min_spend", "min_period", "rounding", "mcc_list_known",
               "conditions", "promo_until", "depth", "source_url")
COMPARED = ("rate", "match", "cap", "cap_period", "min_spend", "promo_until")   # a change here = a new version
CARD_FIELDS = ("bank_id", "name", "network", "kind", "programme_id", "annual_fee", "fee_waiver", "min_income",
               "base_sgd", "base_fcy", "rounding", "url", "tc_url", "status", "depth", "needs_check", "notes")
CARD_COMPARED = ("annual_fee", "base_sgd", "base_fcy", "status", "programme_id")


def _norm(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True)
    if v is None:
        return None
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def log(conn, card_id, what, old, new, source=None):
    conn.execute("INSERT INTO catalogue_changes (card_id, what, old_value, new_value, source_url) VALUES (%s,%s,%s,%s,%s)",
                 (card_id, what, None if old is None else str(old), None if new is None else str(new), source))


def upsert_bank(conn, bid, name, url=None):
    conn.execute("INSERT INTO banks (id, name, listing_url) VALUES (%s,%s,%s) ON CONFLICT (id) DO UPDATE SET "
                 "name = EXCLUDED.name, listing_url = COALESCE(EXCLUDED.listing_url, banks.listing_url)", (bid, name, url))


def upsert_programme(conn, pid, name, bank_id=None, ppm=None):
    conn.execute("INSERT INTO programmes (id, name, bank_id, points_per_mile) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO "
                 "UPDATE SET name = EXCLUDED.name, bank_id = COALESCE(EXCLUDED.bank_id, programmes.bank_id), "
                 "points_per_mile = COALESCE(EXCLUDED.points_per_mile, programmes.points_per_mile)", (pid, name, bank_id, ppm))


def upsert_card(conn, cid, fields, *, protect_full=True):
    """Insert or update a card. A 'headline' write never downgrades a card that holds 'full' (hand-checked) data."""
    old = conn.execute("SELECT * FROM cards WHERE id = %s", (cid,)).fetchone()
    if old is None:
        cols = ["id"] + [f for f in CARD_FIELDS if f in fields]
        conn.execute(f"INSERT INTO cards ({', '.join(cols)}, checked_at) VALUES ({', '.join(['%s'] * len(cols))}, now())",
                     [cid] + [fields[f] for f in cols[1:]])
        log(conn, cid, "new card", None, fields.get("name"), fields.get("url"))
        return "new"
    if protect_full and old["depth"] == "full" and fields.get("depth", "headline") == "headline":
        conn.execute("UPDATE cards SET checked_at = now() WHERE id = %s", (cid,))
        for f in CARD_COMPARED:   # still report what the public sources now say differently
            if f in fields and _norm(fields[f]) != _norm(old[f]):
                log(conn, cid, f"{f} (source differs from checked rule)", old[f], fields[f], fields.get("url"))
        return "kept"
    for f in CARD_COMPARED:
        if f in fields and _norm(fields[f]) != _norm(old[f]):
            log(conn, cid, f, old[f], fields[f], fields.get("url"))
    sets = [f for f in CARD_FIELDS if f in fields]
    conn.execute(f"UPDATE cards SET {', '.join(f'{f} = %s' for f in sets)}, checked_at = now() WHERE id = %s",
                 [fields[f] for f in sets] + [cid])
    return "updated"


def current_rules(conn, cid):
    return {r["bucket"]: r for r in conn.execute(
        "SELECT * FROM card_rules WHERE card_id = %s AND valid_to IS NULL", (cid,)).fetchall()}


def upsert_rule(conn, cid, bucket, fields, mccs=None, correction=None):
    """fields: RULE_FIELDS subset. mccs: [(mcc, effect, rate)] for this rule (replaces the list on a new version).
    correction: why the change mends the app's own reading of a rule the bank did not change (cards.json "correction"):
    the current row is changed in place, so purchases made before today are valued with the mended rule too, instead
    of a new version from today. Returns the current rule id."""
    cur = current_rules(conn, cid).get(bucket)
    if cur is not None:
        if cur["depth"] == "full" and fields.get("depth", "headline") == "headline":
            for f in COMPARED:
                if f in fields and _norm(fields[f]) != _norm(cur[f]):
                    log(conn, cid, f"{bucket} {f} (source differs from checked rule)", _norm(cur[f]), _norm(fields[f]),
                        fields.get("source_url"))
            conn.execute("UPDATE card_rules SET checked_at = now() WHERE id = %s", (cur["id"],))
            return cur["id"]
        changed = [f for f in COMPARED if f in fields and _norm(fields[f]) != _norm(cur[f])]
        old_mccs = {(r["mcc"], r["effect"]) for r in conn.execute(
            "SELECT mcc, effect FROM card_rule_mccs WHERE rule_id = %s", (cur["id"],)).fetchall()}
        mcc_changed = mccs is not None and old_mccs != {(m, e) for m, e, _ in mccs}
        if not changed and not mcc_changed:
            # same numbers: the wording (label, conditions) follows the source without a new version
            words = [f for f in ("label", "conditions") if f in fields and fields[f] != cur[f]]
            conn.execute(f"UPDATE card_rules SET checked_at = now(){''.join(f', {f} = %s' for f in words)} WHERE id = %s",
                         [fields[f] for f in words] + [cur["id"]])
            return cur["id"]
        if correction:
            for f in changed:
                log(conn, cid, f"{bucket} {f} corrected", _norm(cur[f]), _norm(fields[f]), correction[:300])
            cols = [f for f in RULE_FIELDS if f in fields]
            conn.execute(f"UPDATE card_rules SET {', '.join(f + ' = %s' for f in cols)}, checked_at = now() WHERE id = %s",
                         [Jsonb(fields[f]) if f == "match" else fields[f] for f in cols] + [cur["id"]])
            if mcc_changed:
                conn.execute("DELETE FROM card_rule_mccs WHERE rule_id = %s", (cur["id"],))
                for m, e, r in mccs:
                    conn.execute("INSERT INTO card_rule_mccs (rule_id, mcc, effect, rate) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                 (cur["id"], m, e, r))
            return cur["id"]
        for f in changed:
            log(conn, cid, f"{bucket} {f}", _norm(cur[f]), _norm(fields[f]), fields.get("source_url"))
        if mcc_changed:
            new = {(m, e) for m, e, _ in mccs}
            log(conn, cid, f"{bucket} mcc", ", ".join(sorted(f"{e}:{m}" for m, e in old_mccs - new)) or "-",
                ", ".join(sorted(f"{e}:{m}" for m, e in new - old_mccs)) or "-", fields.get("source_url"))
        conn.execute("UPDATE card_rules SET valid_to = current_date WHERE id = %s", (cur["id"],))
        if mccs is None:   # carry the old list forward
            mccs = [(r["mcc"], r["effect"], r["rate"]) for r in conn.execute(
                "SELECT mcc, effect, rate FROM card_rule_mccs WHERE rule_id = %s", (cur["id"],)).fetchall()]
    else:
        log(conn, cid, f"new rule {bucket}", None, _norm(fields.get("rate")), fields.get("source_url"))
    cols = [f for f in RULE_FIELDS if f in fields]
    vals = [Jsonb(fields[f]) if f == "match" else fields[f] for f in cols]
    rid = conn.execute(f"INSERT INTO card_rules (card_id, bucket, {', '.join(cols)}) VALUES (%s, %s, "
                       f"{', '.join(['%s'] * len(cols))}) RETURNING id", [cid, bucket] + vals).fetchone()["id"]
    for m, e, r in mccs or []:
        conn.execute("INSERT INTO card_rule_mccs (rule_id, mcc, effect, rate) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (rid, m, e, r))
    return rid


def close_missing_rules(conn, cid, keep_buckets, source=None):
    for bucket, r in current_rules(conn, cid).items():
        if bucket not in keep_buckets and r["depth"] != "full":
            conn.execute("UPDATE card_rules SET valid_to = current_date WHERE id = %s", (r["id"],))
            log(conn, cid, f"rule {bucket} removed", _norm(r["rate"]), None, source)


def set_exclusions(conn, cid, mccs, source=None):
    cur = {r["mcc"] for r in conn.execute(
        "SELECT mcc FROM card_mcc_exclusions WHERE card_id = %s AND valid_to IS NULL", (cid,)).fetchall()}
    new = set(mccs)
    for m in cur - new:
        conn.execute("UPDATE card_mcc_exclusions SET valid_to = current_date WHERE card_id = %s AND mcc = %s AND "
                     "valid_to IS NULL", (cid, m))
    for m in new - cur:
        conn.execute("INSERT INTO card_mcc_exclusions (card_id, mcc, source_url) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                     (cid, m, source))
    if cur and cur != new:
        log(conn, cid, "excluded mcc", ", ".join(sorted(cur - new)) or "-", ", ".join(sorted(new - cur)) or "-", source)


def upsert_offer(conn, o):
    """o: card_name, bonus, ends (date or None) + optional fields. Marks it seen today."""
    row = conn.execute("SELECT id FROM signup_offers WHERE card_name = %s AND bonus = %s AND ends IS NOT DISTINCT FROM %s",
                       (o["card_name"], o["bonus"], o.get("ends"))).fetchone()
    keys = ("card_id", "bonus_unit", "min_spend", "window_text", "fee_text", "eligibility", "cardup", "cents_per_mile",
            "source_url")
    if row:
        sets = [k for k in keys if o.get(k) is not None]
        conn.execute(f"UPDATE signup_offers SET {', '.join(f'{k} = %s' for k in sets)}{', ' if sets else ''}"
                     "last_seen = current_date, active = true WHERE id = %s", [o[k] for k in sets] + [row["id"]])
        return row["id"]
    cols = ["card_name", "bonus", "ends"] + [k for k in keys if o.get(k) is not None]
    oid = conn.execute(f"INSERT INTO signup_offers ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                       [o.get(c) for c in cols]).fetchone()["id"]
    log(conn, o.get("card_id"), "offer", None, f"{o['card_name']}: {o['bonus']:,.0f} {o.get('bonus_unit', 'miles')}",
        o.get("source_url"))
    return oid


def expire_offers(conn, seen_before, source):
    """Offers past their end date, and offers from `source` it no longer lists, go inactive."""
    conn.execute("UPDATE signup_offers SET active = false WHERE active AND (ends < current_date OR (source_url = %s AND "
                 "last_seen < %s))", (source, seen_before))


def expand_mcc(spec):
    """'8211-8299' -> every code in the range; '5812' -> ['5812']."""
    if "-" in spec:
        a, b = spec.split("-", 1)
        return [f"{i:04d}" for i in range(int(a), int(b) + 1)]
    return [spec]


def apply_offer_terms(conn):
    """Attach the hand-checked bank promotion terms (data/offer-terms.json) to the matching active offers. Runs at every
    start and after each offer refresh, so a re-listed offer keeps its terms. Returns how many offers have terms."""
    from importlib import resources
    try:
        doc = json.loads(resources.files("cards").joinpath("data/offer-terms.json").read_text())
    except (FileNotFoundError, ValueError):
        return 0
    n = 0
    for t in doc.get("offers") or []:
        if not t.get("card_name"):
            continue
        t = dict(t, checked=doc.get("checked"))
        n += conn.execute("UPDATE signup_offers SET terms = %s WHERE card_name = %s AND active", (Jsonb(t), t["card_name"])).rowcount
    return n


def name_pattern(names):
    """One regex for a list of shop names that finds them inside statement text ("COLD STORAGE - JELITA",
    "GOJEK SINGAPORE"), not only as the whole text. Case is ignored where it is used."""
    return r"(?<![A-Za-z0-9])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9])"


def unanchor_name_rules(conn):
    """Name lists written by catalogue runs before 0.10.6 matched the whole text only (^(...)$). Rewrite them once."""
    n = 0
    for r in conn.execute("SELECT id, match FROM card_rules WHERE depth = 'headline' AND valid_to IS NULL AND match ? 'merchant'").fetchall():
        m = r["match"]
        pats = m.get("merchant") or []
        new = [r"(?<![A-Za-z0-9])(" + p[2:-2] + r")(?![A-Za-z0-9])" if isinstance(p, str) and p.startswith("^(") and p.endswith(")$") else p
               for p in pats]
        if new != pats:
            conn.execute("UPDATE card_rules SET match = %s WHERE id = %s", (Jsonb({**m, "merchant": new}), r["id"]))
            n += 1
    return n

