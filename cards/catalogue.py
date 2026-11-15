"""Weekly catalogue refresh. v1 sources, all parsed deterministically (no model in the loop):

  wiki        SG Miles Public MCC Wiki search index: every SG card + issuer, T&C PDF links and the MCCs they name
              (whitelist / blacklist), merchant -> MCC references, per-merchant card rewards (rate, cap, min spend,
              condition) aggregated into each card's headline bonus rules + the MCCs they were seen on, user-verified
              exceptions. Snapshot date shown on the page (it is not updated daily).
  milelion    Current sign-up bonuses page: offer, expiry, total spend, total miles, income, fee, earn rates.
  mainlymiles Transfer partners (window.MMTP_DATA JSON): points currency -> programme ratios, minimum, step, time.

Cards that hold hand-checked ('full') rules are never overwritten: differences are logged as changes instead.
Maybank's own site blocks automated requests; its cards come from the wiki and the blogs only."""
import datetime
import gzip
import hashlib
import html
import json
import os
import re
import urllib.request
from collections import Counter, defaultdict

from psycopg.types.json import Jsonb

from . import __version__, config, db, store


def ua():
    """What the catalogue run calls itself to the sites it reads: the tool and where it lives, unless an admin set
    another string on the App page."""
    return config.app("catalogue_ua") or f"sg-miles-advisor/{__version__} (+https://github.com/quinneydavid/sg-miles-advisor)"


WIKI = "https://sg-miles-public-wiki.pages.dev/search/search_index.json"
MILELION = "https://milelion.com/credit-cards/current-credit-card-sign-up-bonuses/"
MAINLYMILES = "https://mainlymiles.com/transfer-partners/"


INSTITUTIONS = {"American Express": "amex", "OCBC": "ocbc", "Bank of China": "boc", "CIMB": "cimb", "Citibank": "citi",
                "DBS/POSB": "dbs", "DCS Card Centre": "dcs", "HSBC": "hsbc", "ICBC": "icbc", "Maybank": "maybank",
                "Trust": "trust", "UOB": "uob", "Standard Chartered": "sc"}
BANK_NAMES = {"dcs": "DCS Card Centre", "icbc": "ICBC"}
SKIP = re.compile(r"debit|corporate|business|jumpstart", re.I)
# wiki slug -> the id data/cards.json uses (hand-checked cards keep their ids)
HELD_ALIAS = {
    "dbs-woman-s-world-credit-card": "dbs_womens_world", "dbs-vantage-card": "dbs_vantage",
    "uob-visa-signature-card": "uob_visa_signature", "uob-preferred-visa-card": "uob_preferred_platinum",
    "uob-lady-s-solitaire-card": "uob_lady_solitaire", "krisflyer-uob-credit-card": "uob_krisflyer_cc",
    "maybank-world-mastercard": "maybank_world", "maybank-xl-rewards-card": "maybank_xl",
    "maybank-manchester-united-platinum-visa-credit-card": "maybank_manu", "citi-premiermiles-card": "citibank_premiermiles",
    "ocbc-rewards-card": "ocbc_titanium",
}
# mainlymiles currency id -> programme id used by cards
CURRENCY_ALIAS = {"dbs-points": "DBS_POINTS", "uob-uni": "UNI", "maybank-treats": "TREATS", "citi-miles": "CITI_MILES",
                  "ocbc-dollars": "OCBC"}


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": ua(), "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return r.status, data


def text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def card_id_for(slug):
    return HELD_ALIAS.get(slug) or slug.replace("-", "_")


def note_source(conn, url, kind, status, data):
    conn.execute("INSERT INTO sources (url, kind, last_status, last_hash, fetched_at) VALUES (%s,%s,%s,%s,now()) ON CONFLICT "
                 "(url) DO UPDATE SET last_status = EXCLUDED.last_status, last_hash = EXCLUDED.last_hash, fetched_at = now()",
                 (url, kind, status, hashlib.sha256(data).hexdigest()[:16] if data else None))


# --- wiki ------------------------------------------------------------------------------------------------------------

REWARD = re.compile(r"(\d+(?:\.\d+)?)\s*(mpd|%)")
MONEY = re.compile(r"S\$\s?([\d,]+(?:\.\d+)?)")


def parse_reward_table(t, titles):
    """'Card Reward Cap Min spend Conditions <card><merchant> 4 mpd S$600 S$1,000 Must ... <card>...' -> rows.
    The card name runs straight into the merchant name, so rows are split on known card titles."""
    body = text(t)
    body = re.sub(r"^Card Reward Cap Min spend Conditions\s*", "", body)
    if not body or body.startswith("No reward rows"):
        return []
    pat = re.compile("(" + "|".join(re.escape(x) for x in sorted(titles, key=len, reverse=True)) + ")")
    parts = pat.split(body)
    rows = []
    for i in range(1, len(parts) - 1, 2):
        card, rest = parts[i], parts[i + 1]
        m = REWARD.search(rest)
        if not m:
            continue
        tail = rest[m.end():].strip()
        monies = []
        while True:
            mm = re.match(r"S\$\s?([\d,]+(?:\.\d+)?)\s*", tail)
            if not mm:
                break
            monies.append(float(mm.group(1).replace(",", "")))
            tail = tail[mm.end():]
        rows.append({"card": card, "rate": float(m.group(1)), "unit": m.group(2), "cap": monies[0] if monies else None,
                     "min": monies[1] if len(monies) > 1 else None, "cond": tail.strip()[:300]})
    return rows


def card_kind(buckets):
    """'cashback' only when every rule seen on 3+ shops is a percentage; any miles rule makes the card a miles card.
    buckets: {(rate, unit, cap, min, condition): {"n": shops, ...}}"""
    units = {key[1] for key, b in buckets.items() if b["n"] >= 3}
    return "cashback" if units == {"%"} else "miles"


def condition_match(cond):
    c = cond.lower()
    if "online" in c:
        return {"channel": "online"}
    if "contactless" in c or "mobile" in c:
        return {"channel": "contactless"}
    if "overseas" in c or "foreign currency" in c:
        return {"fcy": True}
    return {}


def run_wiki(conn, stats, today):
    status, data = fetch(WIKI, timeout=180)
    note_source(conn, WIKI, "wiki", status, data)
    docs = json.loads(data)["docs"]
    by_loc = {d["location"]: d for d in docs}

    # 1. cards
    cards = {}
    for d in docs:
        loc = d["location"]
        if not (loc.startswith("cards/") and loc.count("/") == 2 and loc.endswith("/") and loc != "cards/"):
            continue
        slug = loc[6:-1]
        inst = re.search(r"Institution:\s*([^<]+)", d["text"])
        inst = html.unescape(inst.group(1).strip()) if inst else ""
        title = html.unescape(d["title"])
        if SKIP.search(title) or inst not in INSTITUTIONS:
            continue
        cards[slug] = {"title": title, "bank": INSTITUTIONS[inst], "id": card_id_for(slug)}
    for slug, c in cards.items():
        store.upsert_bank(conn, c["bank"], BANK_NAMES.get(c["bank"], c["bank"].upper()))
        tcs = re.findall(r"https?://[^\s<]+\.pdf", by_loc.get(f"cards/{slug}/#stored-tc-sources", {}).get("text", ""))
        res = store.upsert_card(conn, c["id"], {"bank_id": c["bank"], "name": c["title"].replace(" Card", "").strip() if
                                                c["id"] not in HELD_ALIAS.values() else c["title"],
                                                "tc_url": tcs[0] if tcs else None, "depth": "headline"})
        conn.execute("UPDATE cards SET wiki_slug = %s WHERE id = %s", (slug, c["id"]))
        stats[res] += 1
        # T&C MCC mentions
        for mcc, kind, src in re.findall(r"MCC (\d{4}) \((whitelist|blacklist|mention)\)\s*Source:\s*(\S+?)</p>",
                                         by_loc.get(f"cards/{slug}/#tc-mcc-rules", {}).get("text", "")):
            conn.execute("INSERT INTO card_tc_mccs (card_id, mcc, kind, source_url, seen) VALUES (%s,%s,%s,%s,%s) ON CONFLICT "
                         "(card_id, mcc, kind, source_url) DO UPDATE SET seen = EXCLUDED.seen", (c["id"], mcc, kind, src, today))
            stats["tc mccs"] += 1

    # 2. merchants -> MCC, and each card's reward rows across merchants
    titles = {c["title"]: slug for slug, c in cards.items()}
    seen = defaultdict(lambda: defaultdict(lambda: {"n": 0, "mccs": Counter()}))
    for d in docs:
        loc = d["location"]
        if not loc.startswith("merchants/") or "#" in loc or loc == "merchants/":
            continue
        mslug = loc[10:-1]
        name = html.unescape(d["title"])
        recs = re.findall(r"(?:^|\s)(\d{4})\s+(?=[A-Z])", text(by_loc.get(f"{loc}#public-reference-mcc-records", {}).get("text", "")))
        mccs = sorted(set(recs))
        conn.execute("INSERT INTO merchants (slug, name, mccs, source, seen) VALUES (%s,%s,%s,'wiki',%s) ON CONFLICT (slug) DO "
                     "UPDATE SET name = EXCLUDED.name, mccs = EXCLUDED.mccs, seen = EXCLUDED.seen WHERE merchants.source = 'wiki'",
                     (mslug, name, mccs, today))   # a household correction is never overwritten
        stats["merchants"] += 1
        for sec in ("top-miles-cards", "top-cashback-cards"):
            for r in parse_reward_table(by_loc.get(f"{loc}#{sec}", {}).get("text", ""), titles):
                key = (r["rate"], r["unit"], r["cap"], r["min"], json.dumps(condition_match(r["cond"])))
                b = seen[titles[r["card"]]][key]
                b["n"] += 1
                if len(mccs) == 1:   # a shop seen under several MCCs says nothing about which MCC earns
                    b["mccs"].update(mccs)
                b.setdefault("names", []).append(name)
                b.setdefault("conds", Counter())[r["cond"]] += 1
        # user-verified exceptions on this merchant
        for xloc, xd in ((k, v) for k, v in by_loc.items() if k.startswith(loc + "#") and "-exception-" in k):
            m = re.search(r"mcc-([\d-]+)-exception", xloc)
            for c_slug, c in cards.items():
                if c["title"] in html.unescape(xd["text"]):
                    for mcc in (m.group(1).split("-") if m else [None]):
                        conn.execute("INSERT INTO mcc_reports (card_id, merchant, mcc, outcome, source, detail) VALUES "
                                     "(%s,%s,%s,'unknown','wiki',%s) ON CONFLICT (card_id, merchant, mcc, source) DO NOTHING",
                                     (c["id"], name[:120], mcc, text(xd["text"])[:400]))
                        stats["reports"] += 1

    # 3. headline rules per card: one bucket per distinct (rate, cap, min, condition) seen on 3+ merchants
    full = {r["id"] for r in conn.execute("SELECT id FROM cards WHERE depth = 'full'").fetchall()}
    for slug, buckets in seen.items():
        c = cards[slug]
        keep = []
        if c["id"] in full:   # hand-checked card: the wiki never adds rules (old wiki rules on it are closed)
            store.close_missing_rules(conn, c["id"], set(), "wiki")
            continue
        # A card listed in BOTH the miles and the cashback tables (its points convert either way, or a shop runs a
        # rebate promotion on it) is ONE kind here, and its rules must all be in that kind's unit: a rule holds a
        # number only, so "4" on a card marked cashback would be read as 4% where the source said 4 mpd. Miles win
        # when the card has any miles rule (this is a miles catalogue); the other unit's rows are left out.
        kind = card_kind(buckets)
        want = "%" if kind == "cashback" else "mpd"
        for (rate, unit, cap, mn, _), b in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
            cond = b["conds"].most_common(1)[0][0]
            if b["n"] < 3:
                continue
            if unit != want:
                stats["rules left out (other unit)"] = stats.get("rules left out (other unit)", 0) + 1
                continue
            if (unit == "mpd" and not 0.2 <= rate <= 12) or (unit == "%" and not 0.1 <= rate <= 25) or \
                    abs(rate * 100 - round(rate * 100)) > 0.01:
                continue   # points-per-dollar converted (7.8125, 3.47222...) or a one-off promo, not a card rate
            rate = round(rate, 2)
            bucket = f"w{len(keep) + 1}"
            match = dict(condition_match(cond))
            if re.search(r"eligible|promotional|selected merchant|partner|participating", cond, re.I):
                mccs = []   # merchant-specific: match these shop names, never a whole MCC
                names = sorted(set(b["names"]))[:150]
                match["merchant"] = [store.name_pattern(names)]
            else:
                mccs = [m for m, k in b["mccs"].items() if k >= 3]
                if mccs:
                    match["mcc"] = sorted(mccs)
                else:
                    match["merchant"] = [store.name_pattern(sorted(set(b["names"]))[:150])]
            store.upsert_rule(conn, c["id"], bucket, {
                "label": (cond[:80] or "Bonus merchants"), "rate": rate, "match": match, "cap": cap,
                "cap_period": "calendar" if cap else "none", "min_spend": mn, "conditions": cond or None,
                "mcc_list_known": False, "depth": "headline",
                "source_url": f"https://sg-miles-public-wiki.pages.dev/cards/{slug}/ ({b['n']} merchants)"},
                [(m, "include", None) for m in sorted(mccs)])
            keep.append(bucket)
            stats["rules"] += 1
        store.close_missing_rules(conn, c["id"], set(keep), "wiki")
        conn.execute("UPDATE cards SET kind = %s WHERE id = %s AND depth = 'headline'", (kind, c["id"]))
    conn.commit()


# --- milelion sign-up bonuses ----------------------------------------------------------------------------------------

def page_text(raw):
    t = re.sub(r"<(script|style|nav|header|footer|aside)[\s\S]*?</\1>", "", raw)
    t = re.sub(r"<h2[^>]*>", "\n## ", t)
    t = re.sub(r"<(/?)(tr|p|h\d|li|br|div)[^>]*>", "\n", t)
    t = re.sub(r"<t[dh][^>]*>", " | ", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def money_of(s):
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_milelion(raw):
    t = page_text(raw)
    blocks = re.split(r"\n## \n", t)
    offers = []
    for b in blocks[1:]:
        lines = [x.strip() for x in b.split("\n") if x.strip()]
        if not lines:
            continue
        name = lines[0]
        def after(label):
            for i, x in enumerate(lines):
                if x.startswith(label):
                    rest = x[len(label):].strip(" :")
                    return rest or (lines[i + 1] if i + 1 < len(lines) else "")
            return ""
        exp = after("Offer Expires")
        try:
            ends = datetime.datetime.strptime(exp.strip(), "%b %d, %Y").date()
        except ValueError:
            ends = None
        miles = money_of("$" + after("Total Miles").replace("$", ""))
        if not miles:
            continue
        details = b.split("## DETAILS", 1)[1] if "## DETAILS" in b else ""
        earn = [x.strip(" |") for x in re.findall(r"\|\s*([^|\n]+)", details.split("Other Benefits")[0])]
        earn = [x for x in earn if x]
        pairs = [f"{earn[i]} {earn[i + 1]}" for i in range(0, len(earn) - 1, 2)]
        other = details.split("Other Benefits", 1)[1] if "Other Benefits" in details else ""
        other = re.sub(r"\s*Apply Now[\s\S]*$", "", other).strip()
        offers.append({"card_name": name, "bonus": miles, "ends": ends, "total_spend": money_of(after("Total Spend")),
                       "income_req": after("Income Req.") or None, "fee_text": after("Annual Fee") or None,
                       "earn": "; ".join(pairs), "terms": other[:900]})
    return offers


def annual_fee(text):
    """'S$196.20 (FYF)' -> 196.2 (the fee from year 2), 'None' / 'Free' -> 0.0, unreadable -> None."""
    if not text:
        return None
    if re.search(r"^\s*(none|nil|free|no annual fee)", text, re.I):
        return 0.0
    return money_of(text)


def offer_cost(o):
    """¢ per mile if the whole minimum spend goes through CardUp and a payable fee is paid. None when unclear."""
    if not o["total_spend"] or not o["bonus"]:
        return None
    fee = o["fee_text"] or ""
    payable = money_of(fee) if fee and not re.search(r"none|waive|F\d?YF|free", fee, re.I) else 0.0
    if payable is None:
        payable = 0.0
    return round((o["total_spend"] * config.number("cardup_fee") + payable) * 100 / o["bonus"], 2)


def name_words(name):
    n = name.lower().replace("american express", "amex").replace("stanchart", "standard chartered")
    return {w for w in re.findall(r"[a-z0-9°+]+", n) if w not in ("card", "the", "s")}


def name_sim(a, b):
    """Jaccard on words; a short name wholly inside a longer one (2+ words, e.g. 'AMEX KrisFlyer Ascend') counts as 0.9."""
    wa, wb = name_words(a), name_words(b)
    j = len(wa & wb) / max(len(wa | wb), 1)
    small, big = sorted((wa, wb), key=len)
    return max(j, 0.9 if len(small) >= 2 and small <= big else 0.0)


def match_card(conn, name):
    best, score = None, 0.0
    for r in conn.execute("SELECT id, name FROM cards").fetchall():
        s = name_sim(name, r["name"])
        if s > score:
            best, score = r["id"], s
    return best if score >= 0.6 else None


BOOST = re.compile(r"(?:deposit|maintain|keep)[^.]{0,40}?S\$\s?([\d,]+)[^.]{0,80}?\binto (?:an? |the )?([A-Z][\w ]{2,40}?Account)[^.]*?"
                   r"boosted to (\d+(?:\.\d+)?) mpd(?:, capped at S\$\s?([\d,]+))?", re.I)
BASE = re.compile(r"(\d+(?:\.\d+)?) mpd (Local|Overseas) Spend")


def run_milelion(conn, stats, today):
    seen_boosts = set()
    status, data = fetch(MILELION)
    note_source(conn, MILELION, "blog", status, data)
    for o in parse_milelion(data.decode("utf-8", "replace")):
        store.upsert_offer(conn, {
            "card_name": o["card_name"], "bonus": o["bonus"], "ends": o["ends"], "card_id": match_card(conn, o["card_name"]),
            "min_spend": o["total_spend"], "fee_text": o["fee_text"], "eligibility": o["terms"] or None,
            "cents_per_mile": offer_cost(o), "source_url": MILELION, "window_text": o["earn"] or None})
        conn.execute("UPDATE signup_offers SET total_spend = %s, income_req = %s WHERE card_name = %s AND bonus = %s AND ends "
                     "IS NOT DISTINCT FROM %s", (o["total_spend"], o["income_req"], o["card_name"], o["bonus"], o["ends"]))
        stats["offers"] += 1
        cid = match_card(conn, o["card_name"])
        fee = annual_fee(o["fee_text"])
        if cid and fee is not None:   # the offer page prints the fee; the wiki has none
            conn.execute("UPDATE cards SET annual_fee = %s, fee_waiver = coalesce(fee_waiver, %s) WHERE id = %s AND depth = 'headline'",
                         (fee, o["fee_text"][:60] if re.search(r"F\d?YF|waive", o["fee_text"] or "", re.I) else None, cid))
        bm = BOOST.search(o["terms"] or "")
        if cid and bm:
            amt, acct, rate, cap = bm.groups()
            conn.execute("INSERT INTO card_boosts (card_id, label, requirement, tiers, mode, cap_override, depth, source_url) VALUES "
                         "(%s,%s,'deposit',%s,'set',%s,'headline',%s) ON CONFLICT (card_id, label) DO UPDATE SET tiers = "
                         "EXCLUDED.tiers, cap_override = EXCLUDED.cap_override WHERE card_boosts.depth = 'headline'",
                         (cid, re.sub(r"\bHBSC\b", "HSBC", acct.strip()) + " deposit", Jsonb([[float(amt.replace(",", "")), float(rate)]]),
                          float(cap.replace(",", "")) if cap else None, MILELION))
            stats["boosts"] += 1
            seen_boosts.add((cid, re.sub(r"\bHBSC\b", "HSBC", acct.strip()) + " deposit"))
        rates = {k: float(v) for v, k in BASE.findall(o["earn"])}
        if cid and rates:
            conn.execute("UPDATE cards SET base_sgd = coalesce(%s, base_sgd), base_fcy = coalesce(%s, base_fcy) WHERE id = %s "
                         "AND depth = 'headline'", (rates.get("Local"), rates.get("Overseas"), cid))
        for old in conn.execute("SELECT id, card_name FROM signup_offers WHERE active AND source_url IS DISTINCT FROM %s",
                                (MILELION,)).fetchall():
            if name_sim(old["card_name"], o["card_name"]) >= 0.6:
                conn.execute("UPDATE signup_offers SET active = false WHERE id = %s", (old["id"],))
    for b in conn.execute("SELECT id, card_id, label FROM card_boosts WHERE source_url = %s AND depth = 'headline'",
                          (MILELION,)).fetchall():
        if (b["card_id"], b["label"]) not in seen_boosts:   # gone from the page (or renamed): drop it
            conn.execute("DELETE FROM card_boosts WHERE id = %s", (b["id"],))
    conn.commit()


# --- mainlymiles transfer partners -----------------------------------------------------------------------------------

def parse_mmtp(raw):
    i = raw.index("MMTP_DATA=") + len("MMTP_DATA=")
    return json.JSONDecoder().raw_decode(raw[i:])[0]


def run_mainlymiles(conn, stats, today):
    status, data = fetch(MAINLYMILES)
    note_source(conn, MAINLYMILES, "blog", status, data)
    d = parse_mmtp(data.decode("utf-8", "replace"))
    progs = {p["id"]: p for p in d["programmes"]}
    for c in d["currencies"]:
        pid = CURRENCY_ALIAS.get(c["id"], c["id"])
        bank = INSTITUTIONS.get({"amex": "American Express", "sc": "Standard Chartered", "boc": "Bank of China"}.get(
            c["bank"], c["bank"].upper()), c["bank"])
        store.upsert_bank(conn, bank, BANK_NAMES.get(bank, bank.upper()))
        store.upsert_programme(conn, pid, c["name"] + (f" ({c['earned_by']})" if c.get("earned_by") else ""), bank)
    for t in d["transfers"]:
        pid = CURRENCY_ALIAS.get(t["currency"], t["currency"])
        p = progs.get(t["programme"], {})
        partner = f"{p.get('brand', t['programme'])}" + (f" ({p['name']})" if p.get("name") and p.get("brand") != p.get("name") else "")
        cur = next((c for c in d["currencies"] if c["id"] == t["currency"]), {})
        conn.execute("INSERT INTO transfer_partners (programme_id, partner, ratio_from, ratio_to, block, fee, source_url, "
                     "checked_at) VALUES (%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (programme_id, partner) DO UPDATE SET "
                     "ratio_from = EXCLUDED.ratio_from, ratio_to = EXCLUDED.ratio_to, block = EXCLUDED.block, fee = EXCLUDED.fee, "
                     "checked_at = now()", (pid, partner, t["points"], t["miles"], t.get("step_points"),
                                            f"{cur.get('fee', '')}; {t.get('time', '')}".strip("; "), MAINLYMILES))
        stats["transfers"] += 1
    conn.commit()


# --- run -------------------------------------------------------------------------------------------------------------

IMAGE_PICKER = "v2-vision"   # bump to re-pick every searched picture once (household photos stay)


def run_images(conn, stats, today):
    from . import images
    row = conn.execute("SELECT value FROM meta WHERE key = 'image_picker'").fetchone()
    redo = os.environ.get("IMAGES_REDO") == "1" or (row or {}).get("value") != IMAGE_PICKER
    images.run(conn, stats, redo=redo)
    conn.execute("INSERT INTO meta (key, value) VALUES ('image_picker', %s) ON CONFLICT (key) DO UPDATE SET value = "
                 "EXCLUDED.value", (IMAGE_PICKER,))
    conn.commit()


def run_links(conn, stats, today):
    """Bank card pages + MileLion reviews (links.py): before `tc`, which reads a card's bank page for terms links."""
    from . import links
    links.run(conn, stats)


def run_tc(conn, stats, today):
    """Bank T&C check (tc.py): after `wiki`, which supplies the document links."""
    from . import tc
    tc.run_tc(conn, stats, today)


def run_awards(conn, stats, today):
    from . import awards
    awards.run_chart(conn, stats, today)


def run_seats(conn, stats, today):
    from . import awards
    awards.run_seats(conn, stats, today)   # optional: dates with award seats (needs a seats.aero key)


def run_matches(conn, stats, today):
    from . import fixtures
    n = fixtures.refresh(conn, today, force=True)   # optional: results for match-day card rates (needs a key)
    if n is not None:
        stats["match days"] = n


ATOME_SITEMAP = "https://www.atome.sg/paylater-merchants/sitemap.xml"


def run_atome(conn, stats, today):
    """Shops Atome lists in Singapore: the slugs in atome.sg's merchant sitemap (Atome's own site, robots allow /)."""
    _, raw = fetch(ATOME_SITEMAP)
    slugs = sorted(set(re.findall(r"<loc>https://www\.atome\.sg/paylater-merchants/([a-z0-9][a-z0-9-]*)</loc>",
                                  raw.decode("utf-8", "replace"))))
    if len(slugs) < 200:   # a broken or blocked page must not wipe the list
        raise RuntimeError(f"only {len(slugs)} shops in the Atome sitemap")
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO atome_merchants (slug, seen) VALUES (%s, %s) ON CONFLICT (slug) DO UPDATE SET "
                        "seen = EXCLUDED.seen", [(sl, today) for sl in slugs])
    gone = conn.execute("DELETE FROM atome_merchants WHERE seen < %s", (today - datetime.timedelta(days=30),)).rowcount
    note_source(conn, ATOME_SITEMAP, "atome", 200, raw)
    conn.commit()
    stats["shops"], stats["dropped"] = len(slugs), gone


KRIS_SEARCH = "https://mainlymiles.com/wp-json/wp/v2/posts?search=latest%20kris%20plus%20earn%20merchants&per_page=5&_fields=link,date"


def kris_key(name):
    """'8 Korean BBQ (The Central)' -> '8-korean-bbq': how Kris+ shop names and shop names are compared."""
    name = re.sub(r"\(.*?\)", " ", (name or "").lower()).replace("&", " ").replace("'", "").replace("’", "")
    return "-".join(w for w in re.sub(r"[^a-z0-9]+", " ", name).split() if w not in ("singapore", "sg", "pte", "ltd"))


def parse_kris(raw):
    """MainlyMiles' Kris+ post -> [(name, mpd)]: the biggest two-column 'shop | N mpd' table on the page (the full
    list; the summary tables are short)."""
    best = {}
    for tab in re.findall(r"<table.*?</table>", raw.decode("utf-8", "replace"), re.S):
        out = {}
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tab, re.S):
            cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) == 2 and cells[0] and (r := re.fullmatch(r"(\d+(?:\.\d+)?)\s*mpd", cells[1])):
                out[cells[0][:120]] = float(r.group(1))
        if len(out) > len(best):
            best = out
    return sorted(best.items())


def run_kris(conn, stats, today):
    """Kris+ shops and their KrisPay miles per S$: MainlyMiles' newest monthly list."""
    _, raw = fetch(KRIS_SEARCH)
    posts = [x for x in json.loads(raw) if "latest-kris-plus-earn-merchants" in x.get("link", "")]
    if not posts:
        raise RuntimeError("no Kris+ merchant post found")
    url = max(posts, key=lambda x: x["date"])["link"]
    status, raw = fetch(url)
    rows = parse_kris(raw)
    if len(rows) < 200:   # a changed page must not wipe the list
        raise RuntimeError(f"only {len(rows)} Kris+ shops read from {url}")
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO kris_merchants (name, key, mpd, seen) VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO "
                        "UPDATE SET key = EXCLUDED.key, mpd = EXCLUDED.mpd, seen = EXCLUDED.seen",
                        [(n, kris_key(n), r, today) for n, r in rows])
    gone = conn.execute("DELETE FROM kris_merchants WHERE seen < %s", (today - datetime.timedelta(days=60),)).rowcount
    note_source(conn, url, "blog", status, raw)
    conn.commit()
    stats["shops"], stats["dropped"] = len(rows), gone


SOURCES = {"wiki": run_wiki, "milelion": run_milelion, "mainlymiles": run_mainlymiles, "images": run_images, "links": run_links, "tc": run_tc,
           "awards": run_awards, "seats": run_seats, "matches": run_matches,
           "atome": run_atome, "kris": run_kris}


def run(dry_run=False, only=None):
    today = datetime.date.today()
    started = datetime.datetime.now(datetime.timezone.utc)
    failed = []
    with db.connect() as conn:
        for name, fn in SOURCES.items():
            if only and name != only:
                continue
            stats = Counter()
            try:
                fn(conn, stats, today)
                if dry_run:
                    conn.rollback()
                print(f"{name}: " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items())), flush=True)
            except Exception as ex:
                conn.rollback()
                failed.append(name)
                print(f"{name}: FAILED {type(ex).__name__}: {str(ex)[:300]}", flush=True)
        if not dry_run and not only and "milelion" not in failed:
            store.expire_offers(conn, today, MILELION)
            conn.commit()
        if not dry_run:
            store.apply_offer_terms(conn)
            conn.commit()
        n = conn.execute("SELECT count(*) n FROM catalogue_changes WHERE at >= %s", (started,)).fetchone()["n"]
        print(f"changes this run: {n}", flush=True)
    return 1 if failed else 0
