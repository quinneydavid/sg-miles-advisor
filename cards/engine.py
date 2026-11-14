"""What does each card earn on one purchase? A purchase = MCC(s) + how it's paid + currency + merchant name.
Rule `match` JSON uses the data/cards.json format, so hand-checked rules and wiki rules share one evaluator.

Pay methods: 'mobile' (Apple/Google Pay), 'tap' (physical card contactless), 'online', 'insert' (chip/PIN, no tap).
A rule asking for channel 'contactless' accepts mobile or tap; 'mobile' only accepts mobile; 'online' only online."""
import re

CHANNEL_OK = {"contactless": {"mobile", "tap"}, "mobile": {"mobile"}, "online": {"online"}}


def _mcc_in(mcc, specs):
    for s in specs or []:
        if "-" in s:
            a, b = s.split("-", 1)
            if mcc.isdigit() and int(a) <= int(mcc) <= int(b):
                return True
        elif s == mcc:
            return True
    return False


def _re_any(patterns, text):
    return any(re.search(p, text or "", re.I) for p in patterns or [])


def applies(match, p):
    """p: {"mcc": "5812" or None, "pay": "mobile", "fcy": False, "merchant": "SHOPEE"}. Returns (ok, needs) where needs
    lists what the rule assumed but the purchase doesn't say (e.g. an unknown MCC)."""
    if not isinstance(match, dict) or not match:
        return False, []
    if "any_of" in match:
        for alt in match["any_of"]:
            ok, needs = applies(alt, p)
            if ok:
                rest = {k: v for k, v in match.items() if k != "any_of"}
                if rest:
                    ok2, needs2 = applies(rest, p)
                    if not ok2:
                        continue
                    needs += needs2
                return True, needs
        return False, []
    needs = []
    if "match_day" in match and not p.get("match_any") and p.get("match_day") != match["match_day"]:
        return False, []   # a rate for the day a team wins or draws (fixtures.py): only on such a day
    if "channel" in match and p.get("pay") not in CHANNEL_OK.get(match["channel"], {match["channel"]}):
        return False, []
    if "fcy" in match and bool(match["fcy"]) != bool(p.get("fcy")):
        return False, []
    if "currency" in match and str(p.get("currency") or "").upper() not in match["currency"]:
        return False, []   # a rate for named currencies (UOB PRVI Miles: IDR / MYR / THB / VND); unknown currency = no
    has_mcc_rule = "mcc" in match
    has_merchant_rule = "merchant" in match
    mcc_ok = has_mcc_rule and p.get("mcc") and _mcc_in(p["mcc"], match["mcc"])
    merch_ok = has_merchant_rule and _re_any(match["merchant"], p.get("merchant"))
    if (has_mcc_rule or has_merchant_rule) and not (mcc_ok or merch_ok):
        return False, []
    if "not_mcc" in match and p.get("mcc") and _mcc_in(p["mcc"], match["not_mcc"]):
        return False, []
    if "not_merchant" in match and _re_any(match["not_merchant"], p.get("merchant")):
        return False, []
    return True, needs


def tier_value(tiers, amount):
    """Highest tier the amount reaches: (value, threshold) or (None, None)."""
    best = (None, None)
    for thr, val in sorted(tiers or [], key=lambda t: float(t[0])):
        if amount is not None and float(amount) >= float(thr):
            best = (float(val), float(thr))
    return best


def boosted(rule, rate, boosts, amounts):
    """Apply the person's qualifying boosts to a matched rule. Returns (rate, cap, applied boost labels)."""
    cap, used = (rule or {}).get("cap"), []
    if rule is None:
        return rate, cap, used
    for b in boosts:
        if b["applies_to"] and rule["bucket"] not in b["applies_to"]:
            continue
        val, thr = tier_value(b["tiers"], amounts.get(str(b["id"])))
        if val is None:
            continue
        if b["mode"] == "add":
            rate += val
        else:
            rate = max(rate, val)
            if b["cap_override"]:
                cap = b["cap_override"]
        used.append(f"{b['label']} S${thr:,.0f}+")
    return rate, cap, used


def next_boost(rule, rate, boosts, amounts):
    """The next tier the person could reach on this rule: (label, threshold, new rate) or None."""
    if rule is None:
        return None
    best = None
    for b in boosts:
        if b["applies_to"] and rule["bucket"] not in b["applies_to"]:
            continue
        have = float(amounts.get(str(b["id"])) or 0)
        for thr, val in sorted(b["tiers"], key=lambda t: float(t[0])):
            if float(thr) > have:
                new = (float(rule["rate"]) + float(val)) if b["mode"] == "add" else max(float(rule["rate"]), float(val))
                if new > rate and (best is None or float(thr) < best[1]):
                    best = (b["label"], float(thr), new)
                break
    return best


def rate_card(card, rules, excluded_mccs, p, boosts=(), amounts=None, room=None):
    """Best rate this card gives the purchase: (rate, rule or None, note, extra) with extra = {cap, boosts, next}."""
    amounts = amounts or {}
    mcc = p.get("mcc")
    if mcc and mcc in excluded_mccs:
        return 0.0, None, "no points on this MCC", {}
    base = float((card["base_fcy"] if p.get("fcy") else card["base_sgd"]) or 0)
    low_rule = None
    for r in rules:   # a lower rate for some kinds of spend replaces the normal one (Maybank Man Utd)
        if isinstance(r.get("match"), dict) and r["match"].get("sets_base") and applies(r["match"], p)[0]:
            base, low_rule = float(r["rate"]), r
    rules = [r for r in rules if not (isinstance(r.get("match"), dict) and r["match"].get("sets_base"))]
    best, best_rule, extra = base, None, {}
    mins = (room or {}).get("__min__") if room is not None else None   # {bucket: S$ still to spend for its minimum}
    shops = (room or {}).get("__shops__") or {}   # {bucket: different shops still needed (DBS yuu: 4)}

    def reached(r):
        return mins is not None and mins.get(r.get("bucket"), 1) <= 0 and shops.get(r.get("bucket"), 0) <= 0

    # a tier that, once reached, takes the place of a lower one (one bonus limit for both): the lower one is gone
    # for the period, even after the higher tier's limit is used up
    replaced = {r["match"].get("instead_of") for r in rules if r.get("min_spend") and isinstance(r.get("match"), dict)
                and r["match"].get("instead_of") and reached(r)}
    cands, used_up = [], []
    for r in rules:
        if r.get("bucket") in replaced:
            continue
        if r.get("promo_until") and p.get("today") and r["promo_until"] < p["today"]:
            continue
        ok, _ = applies(r["match"], p)
        if not ok:
            continue
        rate, cap, used = boosted(r, float(r["rate"]), boosts, amounts)
        left = (room or {}).get(r.get("bucket"))
        if left is not None and left <= 0:
            used_up.append(r.get("label") or r.get("bucket"))
            continue   # this period's cap is used up: the bonus doesn't apply now
        locked = bool(r.get("min_spend")) and not reached(r)
        cands.append((rate, r, cap, used, left, locked))
    # Minimum-spend ladders (S$800 -> 2.8, S$4,000 -> 3.2): a tier already reached beats tiers not reached yet, and
    # with none reached the lowest tier is the one to aim for, not the top of the ladder.
    if any(r.get("min_spend") and not locked for _, r, _, _, _, locked in cands):
        cands = [c for c in cands if not c[5]]
    else:
        low = min((float(c[1]["min_spend"]) for c in cands if c[5]), default=None)
        cands = [c for c in cands if not c[5] or float(c[1]["min_spend"]) == low]
    for rate, r, cap, used, left, locked in cands:
        if rate > best:
            best, best_rule = rate, r
            extra = {"cap": cap, "boosts": used, "next": next_boost(r, rate, boosts, amounts), "left": left,
                     "locked": locked, "min_left": (mins or {}).get(r.get("bucket")) if locked else None,
                     "shops_left": shops.get(r.get("bucket")) if locked else None,
                     "min_shops": (r["match"].get("min_shops") or {}).get("count") if isinstance(r.get("match"), dict) else None}
    extra["used_up"] = used_up
    if best_rule is None and not used_up:
        extra = {}
    note = ("lower rate here: " + (low_rule.get("label") or "this kind of shop")) if best_rule is None and low_rule else \
        "base rate" if best_rule is None else ""
    return best, best_rule, note, extra


def rank(conn, p, held=(), excluded_banks=(), amounts=None, room=None):
    """Every active card's rate for purchase p, best first. Held cards and catalogue cards in one list.
    room: {card id: {bucket: S$ left this period}} for the person's cards (from their real spend), or None."""
    from . import cache
    cat = cache.catalogue(conn)
    ex = set(excluded_banks)
    rules, excl, boosts = cat["rules"], cat["excl"], cat["boosts"]
    fx, results = cat.get("fixtures") or {}, cat.get("results") or {}
    plain = p
    out = []
    for c in cat["active"].values():
        if c["bank_id"] in ex:
            continue
        p, today_is = plain, None
        if c["id"] in fx:   # this card's rates depend on today's match
            today_is = results.get(fx[c["id"]], {}).get(plain.get("today"))
            p = dict(plain, match_day=today_is)
        # boosts count only for the person's own cards (their balances); other cards show the plain rate
        mine = c["id"] in held
        rate, rule, note, extra = rate_card(c, rules.get(c["id"], []), excl.get(c["id"], set()), p,
                                            boosts.get(c["id"], []) if mine else (), amounts if mine else None,
                                            (room or {}).get(c["id"]) if mine else None)
        if not mine and rule is not None:
            extra["next"] = next_boost(rule, rate, boosts.get(c["id"], []), {})
        if c["id"] in fx:
            from . import fixtures
            extra = dict(extra, match=fixtures.SAY.get(today_is, fixtures.SAY[None]))
        out.append({"card": c, "rate": rate, "rule": rule, "note": note, "held": mine, **{"extra": extra}})
    for x in out:   # 1% cashback = 1 c per S$ = ~0.67 mpd at the 1.5 c/mile benchmark
        cash = x["card"]["kind"] == "cashback"
        x["value"] = x["rate"] / 1.5 if cash else x["rate"]
        x["unit"] = "%" if cash else " mpd"
    out.sort(key=lambda x: (-x["value"], x["card"]["depth"] != "full", x["card"]["name"]))
    return out
