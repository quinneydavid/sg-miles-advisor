"""First data: the hand-checked cards (data/cards.json -> depth 'full'), a sample of sign-up offers
(data/recommended-cards.json), the MCC names and, if CARDS_PEOPLE names any, those people. Safe to re-run: everything goes
through store.py, and a person's own choices (held cards, how they pay) are never touched."""
import datetime
import json
from importlib import resources

from psycopg.types.json import Jsonb

from . import people, store

SOURCE = "cards.json (hand-checked against the bank's own documents)"
BOOST_LABELS = {"uob_lady_savings": "UOB Lady's Savings balance"}

BANKS = {
    "dbs": ("DBS", "https://www.dbs.com.sg/personal/cards/credit-cards/default.page"),
    "uob": ("UOB", "https://www.uob.com.sg/personal/cards/index.page"),
    "ocbc": ("OCBC", "https://www.ocbc.com/personal-banking/cards/credit-cards"),
    "citi": ("Citibank", "https://www.citibank.com.sg/credit-cards/"),
    "hsbc": ("HSBC", "https://www.hsbc.com.sg/credit-cards/"),
    "sc": ("Standard Chartered", "https://www.sc.com/sg/credit-cards/"),
    "maybank": ("Maybank", "https://www.maybank2u.com.sg/en/personal/cards/credit/index.page"),
    "amex": ("American Express", "https://www.americanexpress.com/en-sg/credit-cards/"),
    "cimb": ("CIMB", "https://www.cimb.com.sg/"),
    "boc": ("Bank of China", "https://www.bankofchina.com/sg/bcservice/bc1/"),
    "trust": ("Trust Bank", "https://trustbank.sg/"),
}
PROGRAMMES = {"DBS_POINTS": ("DBS Points", "dbs"), "UNI": ("UOB UNI$", "uob"), "KF": ("KrisFlyer miles", None),
              "TREATS": ("Maybank TreatsPoints", "maybank"), "CITI_MILES": ("Citi Miles", "citi"),
              "OCBC": ("OCBC$", "ocbc"), "YUU": ("yuu Points", None)}
PREFIX = {"dbs": "dbs", "uob": "uob", "maybank": "maybank", "citibank": "citi", "ocbc": "ocbc"}


def data(name):
    return json.loads(resources.files("cards").joinpath("data", name).read_text())


def walk_mccs(match, key):
    out = []
    if isinstance(match, dict):
        for k, v in match.items():
            if k == key and isinstance(v, list):
                out += v
            elif isinstance(v, (dict, list)):
                out += walk_mccs(v, key)
    elif isinstance(match, list):
        for v in match:
            out += walk_mccs(v, key)
    return out


def seed(conn):
    for bid, (name, url) in BANKS.items():
        store.upsert_bank(conn, bid, name, url)
    cj = data("cards.json")
    sets = cj.get("exclusion_sets", {})
    for cid, c in cj["cards"].items():
        bank = PREFIX[cid.split("_")[0]]
        pname, pbank = PROGRAMMES.get(c["programme"], (c["programme"], bank))
        store.upsert_programme(conn, c["programme"], pname, pbank, c.get("points_per_mile"))
        base = c.get("base_mpd", {})
        store.upsert_card(conn, cid, {
            "bank_id": bank, "name": c["name"], "kind": "miles", "programme_id": c["programme"],
            "base_sgd": base.get("sgd"), "base_fcy": base.get("fcy"), "rounding": c.get("rounding"),
            "depth": "full", "notes": c.get("notes")}, protect_full=False)
        for b in c["buckets"]:
            rate = b.get("mpd")
            cond = [b[k] for k in ("provisional", "tier_basis") if b.get(k)]
            if rate is None and b.get("mpd_tiers"):
                rate = b["mpd_tiers"][0][1]
                cond.append("tiers: " + ", ".join(f"S${t[0]:,}+ -> {t[1]} mpd" for t in b["mpd_tiers"]))
                # the first tier is a minimum spend: below it the bonus doesn't pay at all
                b = {**b, "min_spend": b.get("min_spend") or b["mpd_tiers"][0][0]}
            inc = [m for spec in walk_mccs(b.get("match"), "mcc") for m in store.expand_mcc(spec)]
            exc = [m for spec in walk_mccs(b.get("match"), "not_mcc") for m in store.expand_mcc(spec)]
            mccs = [(m, "include", None) for m in dict.fromkeys(inc)] + [(m, "exclude", None) for m in dict.fromkeys(exc)]
            store.upsert_rule(conn, cid, b["id"], {
                "label": b.get("label"), "rate": rate, "match": b.get("match", {}), "cap": b.get("cap"),
                "cap_period": b.get("period") or "none", "min_spend": b.get("min_spend"), "rounding": b.get("rounding"),
                "mcc_list_known": bool(inc or exc), "conditions": "; ".join(cond) or None,
                "promo_until": b.get("until"), "depth": "full", "source_url": SOURCE},
                mccs)
        if c.get("boost"):
            b = c["boost"]
            conn.execute("INSERT INTO card_boosts (card_id, label, requirement, tiers, mode, applies_to, depth, source_url) VALUES "
                         "(%s,%s,%s,%s,'add',%s,'full',%s) ON CONFLICT (card_id, label) DO UPDATE SET tiers = EXCLUDED.tiers, "
                         "applies_to = EXCLUDED.applies_to, requirement = EXCLUDED.requirement",
                         (cid, BOOST_LABELS.get(b.get("account"), b.get("account", "boost")), b.get("rule"),
                          Jsonb(b["tiers"]), b.get("applies_to", []), SOURCE))
        # a card that was in the catalogue before it was hand-checked: its wiki headline rules end here, the
        # hand-checked buckets and base rates replace them
        store.close_missing_rules(conn, cid, {b["id"] for b in c["buckets"]}, "cards.json")
        excl = [m for spec in c.get("exclude_mcc", []) for m in store.expand_mcc(spec)]
        for s in c.get("exclude", []):
            excl += [m for spec in sets.get(s, {}).get("mcc", []) for m in store.expand_mcc(spec)]
        store.set_exclusions(conn, cid, list(dict.fromkeys(excl)), "cards.json")

    for mcc, name in data("mcc-names.json")["names"].items():
        conn.execute("INSERT INTO mcc_codes (mcc, name) VALUES (%s,%s) ON CONFLICT (mcc) DO UPDATE SET name = EXCLUDED.name",
                     (mcc, name))

    for o in data("recommended-cards.json")["cards"]:
        ends = o.get("ends")
        store.upsert_offer(conn, {
            "card_name": o["name"], "bonus": o["bonus"], "ends": datetime.date.fromisoformat(ends) if ends else None,
            "min_spend": o.get("min_spend"), "window_text": o.get("window"), "fee_text": o.get("fee"),
            "eligibility": o.get("existing"), "cardup": o.get("cardup"), "cents_per_mile": o.get("cents_per_mile"),
            "source_url": o.get("source")})

    for pid, name in people.configured():   # held cards and how each person pays are theirs to set (Settings)
        conn.execute("INSERT INTO people (id, display) VALUES (%s,%s) ON CONFLICT DO NOTHING", (pid, name))
    store.apply_offer_terms(conn)   # the banks' own promotion terms, checked by hand (data/offer-terms.json)
    from . import awards
    awards.fill(conn)               # flight-goal prices from Singapore Airlines' chart: no feeder needed
    conn.commit()
