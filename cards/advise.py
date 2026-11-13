"""The advisor: frank answers about cards, worked out from the person's own spend.

  points(cat, cid, ...)      what's good and what's not about one card (catalogue facts; plain words)
  analyse(...)               the whole wallet: what each card you hold is really worth to you (miles a year it adds over
                             the rest of your wallet, minus its fee), which cards to add, and the smallest wallet that
                             still earns nearly everything
  verdict(...)               one card, held or not: get it / keep it / skip it / cancel it, with the sums shown
  take(...)                  a short written opinion from the model, built only from those sums (optional)

House style: say what to do first, show the sums, name the catch. Miles are valued at 1.5 c each (what it costs to buy
them through a bill-payment service on a good general card): a card has to beat that, after its annual fee, to earn a
place. Nothing here is taken from any review site; it is arithmetic on the catalogue and the person's lines."""
import datetime
import json
import os
import re
import threading
from collections import defaultdict

from . import crypto, llm, replay

CPM = replay.CENTS_PER_MILE
SMALL = 30.0        # S$ a year: below this a card isn't pulling its weight


def money(v):
    return f"S${float(v):,.0f}"


def n0(v):
    return f"{float(v):,.0f}"


def short(text, n=170):
    """A long scraped sentence cut at a sentence or word end."""
    text = " ".join(str(text).split())
    if len(text) <= n:
        return text if text.endswith(".") else text + "."
    cut = text[:n]
    dot = cut.rfind(". ")
    return cut[:dot + 1] if dot > 60 else cut.rsplit(" ", 1)[0] + "…"


def label(r):
    """A rule's label in plain words (the public reports carry clumsy boilerplate)."""
    t = " ".join(str(r.get("label") or r.get("bucket") or "").split())
    online = bool(re.search(r"must complete transaction online", t, re.I))
    t = re.sub(r";?\s*Must complete transaction online.*$", "", t, flags=re.I)
    t = re.sub(r"^This is an eligible merchant$", "Eligible merchants", t, flags=re.I)
    t = re.sub(r"^This is an? ", "", t, flags=re.I)
    if online:
        t = f"{t}, online only" if t else "Online"
    return t or "Bonus spend"


def rate_txt(c, r):
    unit = "%" if c["kind"] == "cashback" else " mpd"
    return f"{float(r):g}{unit}"


def recent(lines, today, max_months=6):
    """Lines of the last complete months (up to 6). Falls back to everything when there is no complete month yet.
    -> (lines, months counted)"""
    first = today.replace(day=1)
    lo = first
    for _ in range(max_months):
        lo = (lo - datetime.timedelta(days=1)).replace(day=1)
    use = [ln for ln in lines if lo <= ln["date"] < first and ln["amount"] > 0]
    if not use:
        use = [ln for ln in lines if ln["amount"] > 0]
    return use, (len({ln["date"].strftime("%Y-%m") for ln in use}) or 1)


# --- sign-up bonuses: is this person even eligible? ------------------------------------------------------------------

NEW_ONLY = re.compile(r"new[- ]to[- ](bank|amex|card)|new (\w+ )?(customer|cardmember|cardholder)s?\b|do(es)? not (currently )?hold", re.I)
BOTH = re.compile(r"(both )?new and existing|existing (and|or|&) new|new (or|&) existing", re.I)
EXISTING = re.compile(r"existing", re.I)
NUM = r"(\d{1,3}(?:,\d{3})+|\d{4,6})"


def _amounts(text):
    """(new-customer amount, existing-customer amount) when the offer prints both, e.g. '30,000 (new-to-bank) or 20,000
    (existing customer)' or 'New-to-AMEX customers: ... 100,000 bonus ... Existing AMEX customers: ... 60,000 bonus'."""
    m = re.search(NUM + r"\s*\(new[^)]*\)\s*or\s*" + NUM + r"\s*\(existing", text, re.I)
    if m:
        return float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
    a = re.search(r"new[- ]to[- ]\w+ customers?:[^\n]*?receive " + NUM, text, re.I)
    b = re.search(r"existing \w+ customers?:[^\n]*?receive " + NUM, text, re.I)
    if a and b:
        return float(a.group(1).replace(",", "")), float(b.group(1).replace(",", ""))
    return None


def offer_for(o, bank_id, bank_name, held_banks):
    """What a sign-up offer is worth to THIS person. A welcome bonus is usually for people new to the bank, so someone
    who already holds one of the bank's cards gets it only when the offer says existing customers qualify.
    -> {"status": 'full' | 'reduced' | 'no' | 'unclear', "bonus": bonus counted (offer's unit), "note": sentence, "tag": chip text}"""
    bonus = float(o["bonus"])
    t = o.get("terms") or {}
    bank_id = bank_id or t.get("bank_id")
    have = bool(bank_id) and bank_id in held_banks
    who = bank_name or (bank_id or "this bank").upper()
    if t.get("status") == "verified" and t.get("who"):
        return _from_bank_terms(t, bonus, have, who)
    text = " ".join(str(o.get("eligibility") or "").split())
    both, new_only, existing = BOTH.search(text), NEW_ONLY.search(text), EXISTING.search(text)
    tier = _amounts(str(o.get("eligibility") or ""))
    if not have:
        note = (f"You hold no {who} card, so you should count as new to the bank. If you closed one in the last 12 months, check: "
                "most banks then treat you as existing.") if (new_only or tier) else ""
        return {"status": "full", "bonus": bonus, "note": note, "tag": ""}
    if both and not tier:
        return {"status": "full", "bonus": bonus, "note": f"The offer says existing {who} customers qualify too.", "tag": "existing customers too"}
    if tier:
        new, old = tier
        cut = bonus * old / new if new else 0.0   # the printed amounts may be points; the same share applies to the miles
        return {"status": "reduced", "bonus": round(cut), "tag": "smaller bonus for you",
                "note": f"You already hold a {who} card, so you'd get the existing-customer bonus: {n0(old)} instead of {n0(new)}."}
    if new_only and not existing:
        return {"status": "no", "bonus": 0.0, "tag": f"not for existing {who} customers",
                "note": f"The bonus is for people new to {who}. You already hold a {who} card, so you wouldn't get it."}
    if existing and not new_only:
        return {"status": "full", "bonus": bonus, "note": f"The offer mentions existing {who} customers.", "tag": "existing customers too"}
    return {"status": "unclear", "bonus": 0.0, "tag": "probably not for you",
            "note": (f"The offer doesn't say whether existing {who} customers qualify, and most welcome bonuses are for new-to-bank "
                     f"customers only. You hold a {who} card, so it isn't counted here: check the bank's promotion terms.")}


def _from_bank_terms(t, bonus, have, who):
    """The same answer, from the bank's own promotion terms (quoted and linked on the page)."""
    src = {"quote": t.get("quote"), "url": t.get("url"), "verified": True}
    stale = " The terms on file are for an earlier promotion period: check the current ones." if t.get("current") is False else ""
    means = f" New means: {t['new_means'].rstrip('.')}." if t.get("new_means") else ""
    if not have:
        back = (f" If you closed a {who} card in the last {t['lookback_months']} months you don't count as new."
                if t.get("lookback_months") and t["who"] in ("new_only", "tiered") else "")
        return {"status": "full", "bonus": bonus, "tag": "you qualify as new" if t["who"] in ("new_only", "tiered") else "",
                "note": (f"You hold no {who} card.{means}{back}{stale}").strip(), **src}
    if t["who"] in ("existing_too", "anyone"):
        return {"status": "full", "bonus": bonus, "tag": "existing customers too",
                "note": f"The bank's terms give existing {who} customers the same bonus.{stale}", **src}
    if t["who"] == "tiered":
        share = t.get("existing_share")
        if share:
            return {"status": "reduced", "bonus": round(bonus * float(share)), "tag": "smaller bonus for you",
                    "note": f"You already hold a {who} card: the bank's terms give existing customers {t.get('existing_bonus') or 'a smaller gift'}"
                            f"{', against ' + t['new_bonus'] + ' for new ones' if t.get('new_bonus') else ''}.{stale}", **src}
        return {"status": "unclear", "bonus": 0.0, "tag": "smaller bonus for you",
                "note": f"You already hold a {who} card: the bank's terms give existing customers a smaller gift "
                        f"({t.get('existing_bonus') or 'amount not stated'}), not counted here.{stale}", **src}
    return {"status": "no", "bonus": 0.0, "tag": f"not for existing {who} customers",
            "note": f"The bank's terms limit this bonus to new customers, and you already hold a {who} card.{means}{stale}", **src}


def held_banks(cat, held):
    return {cat["cards"][h]["bank_id"] for h in held if h in cat["cards"]}


def my_offer(cat, cid, held):
    """(offer row, offer_for result) for a card's best active offer, or (None, None)."""
    offers = cat["offers"].get(cid, [])
    if not offers:
        return None, None
    c = cat["cards"][cid]
    scored = [(offer_for(o, c["bank_id"], c.get("bank"), held_banks(cat, held)), o) for o in offers]
    fit, o = max(scored, key=lambda x: x[0]["bonus"])
    return o, fit


# --- one card: what's good, what's not -------------------------------------------------------------------------------

def points(cat, cid, held=(), tc=None):
    """(pros, cons) in plain words. held = the person's cards (for overlaps and shared points); tc = T&C check counts."""
    c = cat["cards"][cid]
    rules = cat["rules"].get(cid, [])
    cash = c["kind"] == "cashback"
    today = datetime.date.today()
    pros, cons = [], []
    base_sgd = float(c["base_sgd"]) if c["base_sgd"] is not None else None
    base_fcy = float(c["base_fcy"]) if c["base_fcy"] is not None else None
    live = [r for r in rules if not (r.get("promo_until") and r["promo_until"] < today)]
    for r in live[:3]:
        lb = label(r)
        s = f"{rate_txt(c, r['rate'])} on {lb[0].lower() + lb[1:]}"
        if r["cap"]:
            top = float(r["cap"]) * float(r["rate"]) * (0.01 if cash else 1)
            s += f", up to {money(r['cap'])} a {'statement month' if r['cap_period'] == 'statement' else 'month'}"
            s += f" ({money(top)} back)" if cash else f" ({n0(top)} miles)"
        pros.append(s)
    if not cash and base_sgd is not None and base_sgd >= 1.3:
        pros.append(f"{base_sgd:g} mpd on everything else, with no cap to watch")
    if not cash and base_fcy is not None and base_fcy >= 2.2:
        pros.append(f"{base_fcy:g} mpd on foreign-currency spend")
    fee = fee_of(c)[0]
    if fee == 0:
        pros.append("No annual fee")
    elif fee and c.get("fee_waiver") and re.search(r"FYF|first.year|waiv", str(c["fee_waiver"]), re.I):
        pros.append(f"{money(fee)} a year, first year free" if re.search(r"FYF|first.year", str(c["fee_waiver"]), re.I)
                    else f"{money(fee)} a year: {short(c['fee_waiver'], 60)}")
    partners = cat["partners"].get(c.get("programme_id"), [])
    if len(partners) >= 5:
        pros.append(f"Points move to {len(partners)} airline and hotel programmes")
    same = [cat["cards"][h]["name"] for h in held if h != cid and h in cat["cards"]
            and c.get("programme_id") and cat["cards"][h].get("programme_id") == c["programme_id"]]
    if same:
        pros.append(f"Its points pool with your {same[0]}" + (f" and {len(same) - 1} more" if len(same) > 1 else ""))
    o, fit = my_offer(cat, cid, held) if cid not in held else (None, None)
    if o and fit["bonus"] > 0:
        pros.append(f"Sign-up bonus you'd qualify for: {n0(fit['bonus'])} {o['bonus_unit']}" +
                    (f" for {money(o['min_spend'])} of spend" if o["min_spend"] else ""))
    if tc and tc.get("match") and not tc.get("differs"):
        pros.append(f"{tc['match']} of its terms checked against the bank's own T&C")

    if not cash and base_sgd is not None and base_sgd <= 0.4 and live:
        cons.append(f"Outside its bonus categories it earns {base_sgd:g} mpd: never use it as a general card")
    small = [r for r in live if r["cap"] and float(r["cap"]) <= 1000 and float(r["rate"]) >= (5 if cash else 3)]
    if small:
        r = small[0]
        cons.append(f"The {rate_txt(c, r['rate'])} stops at {money(r['cap'])} a month; after that it's the base rate")
    mins = [r for r in live if r.get("min_spend")]
    if mins:
        cons.append(f"Needs {money(max(float(r['min_spend']) for r in mins))} of spend in the month before the bonus pays at all")
    if fee and fee >= 250 and not re.search(r"FYF|first.year|waiv", str(c.get("fee_waiver") or ""), re.I):
        cons.append(f"{money(fee)} a year, and no waiver that we know of")
    elif fee is None:
        cons.append("Annual fee not known: check before you apply")
    if not cash and c.get("programme_id") and len(partners) == 1:
        cons.append(f"Points only go to {partners[0]['partner'].split(' (')[0]}")
    paid = [t for t in partners if t.get("fee") and not str(t["fee"]).lower().startswith("free")]
    if partners and len(paid) == len(partners):
        cons.append(f"Every transfer costs a fee ({paid[0]['fee']})")
    nex = len(cat["excl"].get(cid, ()))
    if nex >= 15:
        cons.append(f"{nex} merchant categories earn nothing at all")
    ending = [r for r in live if r.get("promo_until") and (r["promo_until"] - today).days <= 60]
    if ending:
        r = ending[0]
        cons.append(f"The {rate_txt(c, r['rate'])} rate is a promotion that ends {r['promo_until']:%d %b %Y}")
    if c.get("rounding") and re.search(r"5", str(c["rounding"])):
        cons.append("Points are worked out on S$5 blocks, so small purchases lose out")
    if c["depth"] != "full" and not (tc and tc.get("match")):
        cons.append("Its rates come from public reports and aren't checked against the bank's T&C yet")
    if tc and tc.get("differs"):
        cons.append(f"{tc['differs']} of our figures differ from the bank's T&C: see the T&C check below")
    if o and fit["bonus"] == 0:
        cons.append("Its sign-up bonus " + ("is for new-to-bank customers, and you already bank with them" if fit["status"] == "no"
                                           else "probably doesn't apply to you as an existing customer of the bank"))
    dup = overlaps(cat, cid, held)
    if dup:
        cons.append(dup)
    return pros[:6], cons[:6]


def overlaps(cat, cid, held):
    """One sentence when a card you hold already does this card's main job (same top bonus rule shape)."""
    mine = cat["rules"].get(cid, [])
    if not mine:
        return None
    top = mine[0]
    key = json.dumps(top["match"], sort_keys=True) if top.get("match") else None
    for h in held:
        if h == cid or h not in cat["cards"]:
            continue
        for r in cat["rules"].get(h, []):
            if key and json.dumps(r["match"], sort_keys=True) == key and float(r["rate"]) >= float(top["rate"]):
                return (f"Your {cat['cards'][h]['name']} already earns {float(r['rate']):g} on the same spend, so this one only "
                        "helps once that cap is full")
    return None


# --- the whole wallet ------------------------------------------------------------------------------------------------

def fee_of(c, fees=None):
    """(fee, where it's from): what the person says they pay beats the catalogue's list price. None = not known."""
    mine = (fees or {}).get(c["id"])
    if mine is not None:
        return float(mine), "yours"
    if c["annual_fee"] is not None:
        return float(c["annual_fee"]), "list"
    if c.get("bank_fee") is not None:   # from the bank's own fee document (T&C check)
        return float(c["bank_fee"]), "bank"
    return None, None


def analyse(base, cat, lines, held, allowed, months, balances=None, fees=None):
    """What each card is worth to this person. Everything per year, from `months` of real lines.
    balances: {programme id: points} when known (so 'cancel' can warn about points that would be lost).
    fees: {card id: S$ a year the person really pays} (Settings)."""
    held = [h for h in held if h in base.cards]
    year = 12 / months
    have = replay.assign(base, lines, held)
    spend = sum(ln["amount"] for ln, _, _ in have["per"])
    out = {"months": months, "spend_year": spend * year, "miles_year": have["miles"] * year, "cards": [], "add": [],
           "have": have}
    for h in held:
        c = base.cards[h]
        without = replay.assign(base, lines, [x for x in held if x != h])
        adds = (have["miles"] - without["miles"]) * year
        mine = have["by_card"].get(h, {"spend": 0.0, "miles": 0.0, "n": 0})
        fee, fee_src = fee_of(c, fees)
        value = adds * CPM / 100
        net = value - (fee or 0)
        if value >= SMALL and net >= 0:
            v = "keep"
        elif value >= SMALL:
            v = "waiver"          # earns its place only with the fee waived
        elif fee:
            v = "cancel"
        else:
            v = "spare"           # free and idle: harmless in a drawer
        alone = c.get("programme_id") and not any(
            x != h and base.cards[x].get("programme_id") == c["programme_id"] for x in held)
        pts = (balances or {}).get(c.get("programme_id")) if alone else None
        out["cards"].append({"card": h, "verdict": v, "adds": adds, "value": value, "fee": fee, "fee_src": fee_src, "net": net,
                             "spend_month": mine["spend"] / months, "miles_year": mine["miles"] * year,
                             "share": (mine["spend"] / spend) if spend else 0.0, "orphan_points": pts,
                             "boosted": bool(base.boosts.get(h)) and any(base.amounts.get(str(b["id"])) for b in base.boosts.get(h, []))})
    out["cards"].sort(key=lambda x: -x["net"])
    for cid in replay.shortlist(base, lines, held, allowed, have):
        x = candidate(base, cat, lines, held, cid, months, have)
        if x["adds"] > 1:
            out["add"].append(x)
    out["add"].sort(key=lambda x: -(x["net"] + x["bonus_value"]))
    out["slim"] = slim(base, lines, [h for h in held if have["by_card"].get(h, {}).get("spend")], months, fees, have["miles"])
    return out


def candidate(base, cat, lines, held, cid, months, have=None):
    """A card the person doesn't hold: what it would add, where from, and what it costs."""
    have = have or replay.assign(base, lines, held)
    year = 12 / months
    tot = replay.assign(base, lines, list(held) + [cid])
    c = base.cards[cid]
    adds = (tot["miles"] - have["miles"]) * year
    fee = fee_of(c)[0]
    value = adds * CPM / 100
    moved = defaultdict(lambda: {"spend": 0.0, "gain": 0.0, "old": 0.0, "new": 0.0, "from": defaultdict(float)})
    for (ln, was, got), (_, now, m) in zip(have["per"], tot["per"]):
        if now == cid:
            g = moved[cat["mcc_names"].get(ln.get("mcc") or "", "Other spend")]
            g["spend"] += ln["amount"]
            g["gain"] += m - got
            g["old"] += got
            g["new"] += m
            g["from"][was] += ln["amount"]
    where = [{"what": k, "spend_month": v["spend"] / months, "gain_year": v["gain"] * year,
              "rate_now": v["old"] / v["spend"] if v["spend"] else 0.0, "rate_new": v["new"] / v["spend"] if v["spend"] else 0.0,
              "from": max(v["from"], key=v["from"].get) if v["from"] else None}
             for k, v in sorted(moved.items(), key=lambda kv: -kv[1]["gain"])[:3]]
    o, fit = my_offer(cat, cid, held)   # counted only as far as this person qualifies for it
    mine_bonus = fit["bonus"] if fit else 0.0
    bonus_value = (mine_bonus * CPM / 100 if o["bonus_unit"] == "miles" else mine_bonus) if o else 0.0
    mine = tot["by_card"].get(cid, {"spend": 0.0})
    return {"card": cid, "adds": adds, "value": value, "fee": fee, "net": value - (fee or 0), "where": where,
            "spend_month": mine["spend"] / months, "offer": o, "offer_fit": fit, "bonus": mine_bonus, "bonus_value": bonus_value,
            "checked": c["depth"] == "full", "assumed_excl": cid in cat.get("assumed_excl", ())}


def slim(base, lines, held, months, fees=None, full=None):
    """Greedy smallest wallet: add the card that lifts (miles value - fees) most, one at a time (only cards that
    carry spend in the full wallet can help). -> [{"cards": [...], "miles_year", "share"}] for 1..5 cards; share =
    of what the full wallet earns."""
    year = 12 / months
    full = full or replay.assign(base, lines, held)["miles"] or 1.0
    chosen, steps, left = [], [], list(held)

    def score(ids):
        cost = sum(fee_of(base.cards[i], fees)[0] or 0 for i in ids)
        m = replay.assign(base, lines, ids)["miles"]
        return m * year * CPM / 100 - cost, m

    while left and len(chosen) < 5:
        scored = {cid: score(chosen + [cid]) for cid in left}
        best = max(left, key=lambda cid: scored[cid][0])
        chosen.append(best)
        left.remove(best)
        m = scored[best][1]
        steps.append({"cards": list(chosen), "miles_year": m * year, "share": m / full})
        if m / full > 0.995:
            break
    return steps


# --- one card: the verdict -------------------------------------------------------------------------------------------

LABEL = {"get": "Get it", "bonus": "Worth it for the bonus", "maybe": "Marginal", "skip": "Skip it",
         "keep": "Keep it", "waiver": "Keep it only if the fee is waived", "cancel": "Cancel it", "spare": "Sock drawer",
         "nodata": "Can't say yet"}
TONE = {"get": "ok", "bonus": "ok", "keep": "ok", "maybe": "", "spare": "", "waiver": "warn", "skip": "warn",
        "cancel": "warn", "nodata": ""}


def verdict(base, cat, lines, held, allowed, months, cid, an=None):
    """{"verdict", "label", "headline", "sums": [(text, value)], "why": [sentences], "catch": [sentences]}"""
    c = cat["cards"][cid]
    name = c["name"]
    if not lines:
        return {"verdict": "nodata", "label": LABEL["nodata"], "tone": "",
                "headline": "There's no spend on file yet, so this can't be worked out for you.",
                "sums": [], "why": [], "catch": ["Upload a statement, or wait for your card lines to arrive, and the answer appears here."]}
    if cid in held:
        an = an or analyse(base, cat, lines, held, allowed, months)
        x = next((k for k in an["cards"] if k["card"] == cid), None)
        if x is None:
            return {"verdict": "nodata", "label": LABEL["nodata"], "tone": "", "headline": "This card isn't in the active catalogue.",
                    "sums": [], "why": [], "catch": []}
        v = x["verdict"]
        sums = [("Spend that belongs on it", f"{money(x['spend_month'])} a month"),
                ("Miles it adds over your other cards", f"{n0(x['adds'])} a year"),
                ("Worth, at 1.5¢ a mile", f"{money(x['value'])} a year"),
                ("Annual fee" + (" (what you pay)" if x.get("fee_src") == "yours" else ""),
                 money(x["fee"]) if x["fee"] is not None else "not known")]
        if x["fee"]:
            sums.append(("After the fee", f"{'−' if x['net'] < 0 else ''}{money(abs(x['net']))} a year"))
        why, catch = [], []
        if v == "keep":
            head = (f"{name} earns its place: {n0(x['adds'])} miles a year that no other card of yours would earn"
                    + (f", well past its {money(x['fee'])} fee." if x["fee"] else ", and it costs nothing to hold."))
            why.append(f"Used on the right spend it carries {money(x['spend_month'])} a month, {x['share'] * 100:.0f}% of what goes on your cards.")
        elif v == "waiver":
            head = (f"{name} adds {n0(x['adds'])} miles a year, worth {money(x['value'])}, against a {money(x['fee'])} fee. "
                    "That only works with the fee waived.")
            why.append("Ask for the waiver when the fee posts. If the bank says no, the sums below say let it go.")
        elif v == "cancel":
            head = (f"{name} adds {n0(x['adds'])} miles a year over the rest of your wallet and costs {money(x['fee'])}. "
                    "It isn't paying for itself.")
            why.append("Your other cards already earn the same or better on everything you would put on it.")
        else:
            if x["fee"] is None:
                head = (f"{name} adds next to nothing for you ({n0(x['adds'])} miles a year). Whether to keep it comes down to its fee, "
                        "which isn't on file.")
                why.append("If it's free or waived, it does no harm in a drawer. If you pay for it, cancel it.")
            else:
                head = (f"{name} adds next to nothing for you ({n0(x['adds'])} miles a year), but it's free, so there's no rush to close it.")
                why.append("Keep it in a drawer if you want the credit line or the bank relationship; it earns nothing in your wallet.")
        if x["orphan_points"]:
            catch.append(f"Closing it strands about {n0(x['orphan_points'])} points: no other card of yours shares the programme. "
                         "Move or spend them first.")
        elif v in ("cancel", "waiver") and c.get("programme_id") and not any(
                h != cid and cat["cards"].get(h, {}).get("programme_id") == c["programme_id"] for h in held):
            catch.append("No other card of yours shares its points programme: transfer the points out before you close it.")
        if x["fee"] is None:
            catch.append("Its annual fee isn't in the bank documents read so far, so none was counted.")
        elif x.get("fee_src") != "yours" and x["fee"]:
            catch.append(f"The {money(x['fee'])} fee is the {'figure in the bank document' if x.get('fee_src') == 'bank' else 'list price'}. "
                         "It counts in full until a waiver shows on your statements: the Fees page tracks that, and when to ask.")
        if x["boosted"]:
            catch.append("You hold a balance that boosts this card: closing it makes that balance pointless too.")
        return {"verdict": v, "label": LABEL[v], "tone": TONE[v], "headline": head, "sums": sums, "why": why, "catch": catch, "x": x}
    if cid not in base.cards:
        return {"verdict": "nodata", "label": LABEL["nodata"], "tone": "", "headline": "This card is no longer offered.",
                "sums": [], "why": [], "catch": []}
    x = candidate(base, cat, lines, held, cid, months)
    o = x["offer"]
    if x["net"] >= 150:
        v = "get"
    elif x["net"] + x["bonus_value"] >= 250 and o:
        v = "bonus"
    elif x["net"] > 0:
        v = "maybe"
    else:
        v = "skip"
    sums = [("Spend it would take over", f"{money(x['spend_month'])} a month"),
            ("Extra miles over your wallet today", f"{n0(x['adds'])} a year"),
            ("Worth, at 1.5¢ a mile", f"{money(x['value'])} a year"),
            ("Annual fee", money(x["fee"]) if x["fee"] is not None else "not known (not deducted)"),
            ("After the fee", f"{'−' if x['net'] < 0 else ''}{money(abs(x['net']))} a year")]
    fit = x["offer_fit"]
    if o:
        sums.append(("Sign-up bonus for you", (f"{n0(x['bonus'])} {o['bonus_unit']}" + (f" ≈ {money(x['bonus_value'])}" if o["bonus_unit"] == "miles" else ""))
                     if x["bonus"] else "none counted"))
    why, catch = [], []
    for w in x["where"]:
        if w["gain_year"] >= 1:
            frm = f" on {cat['cards'][w['from']]['name']}" if w["from"] in cat["cards"] else ""
            r = lambda v: f"{v:.1f}".rstrip("0").rstrip(".")
            why.append(f"{w['what']}: your {money(w['spend_month'])} a month earns {r(w['rate_now'])} miles per S$ now{frm}. "
                       f"Here it would earn {r(w['rate_new'])}: +{n0(w['gain_year'])} miles a year.")
    if v == "get":
        fee = f"its {money(x['fee'])} fee" if x["fee"] else "its fee"
        head = (f"Yes. On your spend, {name} would earn about {n0(x['adds'])} more miles a year than your wallet does now, "
                f"worth about {money(x['value'])}. After {fee} you'd be {money(x['net'])} a year ahead.")
    elif v == "bonus":
        head = (f"For the bonus, yes. Day to day {name} adds only {n0(x['adds'])} miles a year, but the "
                f"{n0(x['bonus'])}-{o['bonus_unit'].rstrip('s')} sign-up bonus you'd qualify for is worth about {money(x['bonus_value'])}.")
    elif v == "maybe":
        head = (f"It's marginal. {name} would add {n0(x['adds'])} miles a year, {money(x['net'])} after its fee: real, "
                "but not worth another card to manage unless you want it for another reason.")
    else:
        head = (f"No. On your spend {name} adds {n0(x['adds'])} miles a year" +
                (f", worth {money(x['value'])} against a {money(x['fee'])} fee." if x["fee"] else
                 ": your wallet already covers what it's good at."))
    if not why and v in ("skip", "maybe"):
        why.append("There's no category where it beats the cards you already hold while their caps have room.")
    if o and x["bonus"] == 0:
        o = None   # no bonus for this person: its spend and end date don't matter
    if o:
        win = o["window_text"] if o["window_text"] and re.search(r"month|day|week", o["window_text"], re.I) and len(o["window_text"]) <= 60 else None
        if o["min_spend"]:
            catch.append(f"The bonus needs {money(o['min_spend'])} of spend" + (f" ({win})" if win else "") + ".")
        if fit and fit["note"]:
            catch.append(fit["note"])
        if o["ends"]:
            catch.append(f"The offer ends {o['ends']:%d %b %Y}.")
    if not x["checked"]:
        catch.append("Not checked against the bank's own terms yet: its rates are from public reports, and the app assumes it gives "
                     "nothing on what banks usually exclude (insurance, utilities, e-wallet top-ups, government, schools, charities). "
                     "Treat the figure as an estimate.")
    if x["fee"] is None:
        catch.append("The annual fee isn't known, so nothing was deducted for it.")
    return {"verdict": v, "label": LABEL[v], "tone": TONE[v], "headline": head, "sums": sums, "why": why, "catch": catch, "x": x}


# --- the written take (model, optional) ------------------------------------------------------------------------

PROMPT = """You write the short verdict box for a personal credit-card app in Singapore. Voice: a frank, well-informed
friend who has done the sums. Dry, a little wry, never salesy, no exclamation marks, no emoji. Plain short sentences.
Start with the answer. Then the one or two reasons that matter. End with the catch, if there is one.

Rules:
- 70 to 110 words, one paragraph.
- Use ONLY the facts below. Every number you write must appear in them exactly. If a fact is missing, leave it out.
- Miles are valued at 1.5 cents each. "mpd" = miles per dollar.
- Do not mention any website, blog or reviewer. Do not give general advice about debt or credit scores.

Facts (JSON):
{facts}"""


def facts_of(cat, cid, v, pros, cons):
    c = cat["cards"][cid]
    return {"card": c["name"], "bank": c["bank"], "kind": c["kind"], "verdict": v["label"], "summary": v["headline"],
            "sums": [f"{k}: {val}" for k, val in v["sums"]], "reasons": v["why"], "catches": v["catch"],
            "good": pros[:4], "bad": cons[:4]}


def _numbers(text):
    return {m.replace(",", "") for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def grounded(text, facts):
    """Every number in the model's paragraph must be one of ours (small counts like 'two cards' aside)."""
    ours = _numbers(json.dumps(facts, ensure_ascii=False))
    return all(n in ours or (n.isdigit() and int(n) <= 12) for n in _numbers(text))


def take(conn, person, subject, facts):
    """The stored paragraph for these exact facts, or None (and, when a model is configured, start writing one)."""
    blob = json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str).encode()
    h = crypto.hmac_hex(conn, "advice", blob)
    row = conn.execute("SELECT * FROM advice WHERE person = %s AND subject = %s", (person, subject)).fetchone()
    if row and row["facts_hmac"] == h:
        if row["status"] == "ready" and row["text_enc"]:
            return {"status": "ready", "text": crypto.decrypt(conn, row["text_enc"], f"advice.text_enc:{person}:{subject}").decode()}
        fresh = (datetime.datetime.now(datetime.timezone.utc) - row["at"]).total_seconds() < 300
        if row["status"] == "failed" or fresh:
            return {"status": row["status"] if row["status"] == "failed" else "writing"}
    if not llm.key():
        return {"status": "off"}
    conn.execute("INSERT INTO advice (person, subject, facts_hmac, status, text_enc, at) VALUES (%s,%s,%s,'writing',NULL,now()) "
                 "ON CONFLICT (person, subject) DO UPDATE SET facts_hmac = EXCLUDED.facts_hmac, status = 'writing', "
                 "text_enc = NULL, at = now()", (person, subject, h))
    conn.commit()
    threading.Thread(target=_write, args=(person, subject, h, facts), daemon=True).start()
    return {"status": "writing"}


def _write(person, subject, h, facts):
    from . import db, llm
    text = None
    try:
        raw = llm.chat(PROMPT.format(facts=json.dumps(facts, ensure_ascii=False, default=str)), max_tokens=320, timeout=90)
        raw = re.sub(r"\s+", " ", (raw or "").strip().strip('"'))
        if 200 <= len(raw) <= 1100 and grounded(raw, facts):
            text = raw
    except Exception as ex:
        print(f"advice: {type(ex).__name__}", flush=True)
    try:
        with db.connect() as conn:
            enc = crypto.encrypt(conn, text.encode(), f"advice.text_enc:{person}:{subject}") if text else None
            conn.execute("UPDATE advice SET status = %s, text_enc = %s, at = now() WHERE person = %s AND subject = %s AND "
                         "facts_hmac = %s", ("ready" if text else "failed", enc, person, subject, h))
    except Exception as ex:
        print(f"advice store: {type(ex).__name__}", flush=True)
