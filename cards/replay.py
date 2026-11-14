"""Replay real spend against cards with real caps.

For each line in date order:
  actual  - the card it was charged to (its caps fill as you spent)
  wallet  - the best card you hold at that moment (a second, simulated set of caps: if you had always used the best
            card, its caps would fill too)
  any     - the best card in the catalogue (excluded banks left out), same simulated caps idea
Plus: cap overflows on the actual cards (date the bonus ran out, spend after it, miles lost), tips by merchant, and
"worth getting" (extra miles a year if a card were added to your wallet, minus its fee).

Assumptions shown on the page: in-store spend paid by Apple/Google Pay (taps count as contactless), online when the
statement or shop list says so; statement-period caps follow the card's statement closing day when the feed knows
it (else calendar month); cashback counts as miles at 1.5 c/mile."""
import datetime
import functools
import heapq
from collections import defaultdict

from . import engine

CENTS_PER_MILE = 1.5


@functools.lru_cache(maxsize=4096)
def ym(d):
    return d.strftime("%Y-%m")


@functools.lru_cache(maxsize=8192)
def statement_end(d, close):
    """The closing date of the statement period that contains d (close = day of month, clamped to month length)."""
    import calendar
    last = calendar.monthrange(d.year, d.month)[1]
    end = datetime.date(d.year, d.month, min(close, last))
    if d <= end:
        return end
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return datetime.date(y, m, min(close, calendar.monthrange(y, m)[1]))


def period_key(rule, d, close=None):
    p = rule.get("cap_period")
    if p == "match_day":
        return "d" + d.isoformat()   # a cap per match day
    if p == "statement" and close:
        return "s" + statement_end(d, close).isoformat()
    if p in ("calendar", "statement"):
        return ym(d)   # statement period unknown: calendar month
    if p == "membership_year":
        return str(d.year)
    return None


def period_bounds(rule, d, close=None):
    """(first day, last day) of the period containing d, for the progress panel."""
    import calendar
    p = rule.get("cap_period")
    if p == "statement" and close:
        end = statement_end(d, close)
        y, m = (end.year - 1, 12) if end.month == 1 else (end.year, end.month - 1)
        prev = datetime.date(y, m, min(close, calendar.monthrange(y, m)[1]))   # the previous closing date
        return prev + datetime.timedelta(days=1), end
    if p == "match_day":
        return d, d
    if p == "membership_year":
        return datetime.date(d.year, 1, 1), datetime.date(d.year, 12, 31)
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


class Wallet:
    """Cards + rules + exclusions + boosts, with one cap ledger."""

    def __init__(self, cards, rules, excl, boosts, amounts, closes=None, app=None, store="mobile", hist=None):
        self.cards, self.rules, self.excl, self.boosts, self.amounts = cards, rules, excl, boosts, amounts
        self.closes = closes or {}   # card id -> statement closing day (Settings, a statement file, a feeder)
        self.hist = hist or {}       # card id -> (date of its last recorded rule change, every rule version): cache.catalogue
        # (card, mcc, online, fcy, merchant) -> the rules that apply. Whether a rule applies never depends on caps or
        # dates, and testing it (merchant regexes) was most of a replay's time: every replay shares this table.
        self.app = app if app is not None else {}
        self.store = store   # how this person pays in a shop: 'mobile' (phone / watch) or 'tap' (the physical card)
        self.used = defaultdict(float)
        self.off = set()   # (card, bucket, period) whose minimum spend wasn't met: bonus not paid
        # match-day rates (fixtures.py): card -> its fixtures, their results by date, bonus days allowed a month
        self.fixtures, self.results, self.per_month = {}, {}, {}
        self.mdays = defaultdict(set)   # (card, bucket, month) -> the match days that already took the bonus
        self.pending = False            # the last earn() met a match day whose result is not in yet

    def counts_for(self, rule, line):
        """Does this line match the rule (its own kind of spend), for minimums counted per bucket?"""
        m = {k: v for k, v in rule["match"].items() if k not in ("min_own", "shares_cap", "link_only", "sets_base")}
        pay = line.get("pay") or ("online" if line.get("online") else self.store)
        fcy = bool(line.get("fcy"))
        p = {"mcc": line.get("mcc"), "pay": pay, "fcy": fcy, "currency": line.get("currency") if fcy else None,
             "merchant": line.get("merchant"), "match_any": True}
        return engine.applies(m, p)[0]

    def fork(self, off=None):
        w = Wallet(self.cards, self.rules, self.excl, self.boosts, self.amounts, self.closes, self.app, self.store, self.hist)
        w.off = set(off or ())
        w.fixtures, w.results, w.per_month = self.fixtures, self.results, self.per_month
        return w

    def key(self, cid, rule, d):
        return period_key(rule, d, self.closes.get(cid))

    def earn(self, cid, line, consume=False):
        """Miles(-equivalent) this card gives the line, honouring cap room. Returns (miles, rule, overflow).
        Most (card, purchase) pairs have no bonus rule at all: those return on the short path, base rate only."""
        mcc, fcy, d = line.get("mcc"), bool(line.get("fcy")), line["date"]
        # how this line was paid: what its source or the person says (pipeline.paid), else the person's usual way
        pay = line.get("pay") or ("online" if line.get("online") else self.store)
        cur = line.get("currency") if fcy else None
        h = self.hist.get(cid)
        dated = h is not None and d < h[0]   # bought before this card's last recorded rule change: the rules of the time
        ak = (cid, mcc, pay, fcy, line.get("merchant"), cur, dated)
        hits = self.app.get(ak)
        if hits is None:
            if len(self.app) > 400_000:
                self.app.clear()
            p = {"mcc": mcc, "pay": pay, "fcy": fcy, "currency": cur, "merchant": line.get("merchant"), "today": d,
                 "match_any": True}   # whether it is a win or draw day is decided per purchase date, below
            hits = self.app[ak] = [r for r in (h[1] if dated else self.rules.get(cid, [])) if engine.applies(r["match"], p)[0]]
        nums = self.app.get(cid)
        if nums is None:   # (per-mile factor, local base, foreign base) as floats, once per card
            c = self.cards[cid]
            f = 1 / CENTS_PER_MILE if c["kind"] == "cashback" else 1.0
            nums = self.app[cid] = (f, float(c["base_sgd"] or 0), float(c["base_fcy"] or 0))
        f, base = nums[0], (nums[2] if fcy else nums[1])
        self.pending = False
        if mcc and mcc in self.excl.get(cid, ()):
            return 0.0, None, None
        amt = line["amount"]
        if hits and any(isinstance(r["match"], dict) and (r["match"].get("sets_base") or r["match"].get("link_only")) for r in hits):
            # a rate below the card's normal one for some kinds of spend (Maybank Man Utd: medical, schools, telecoms)
            for r in hits:
                if isinstance(r["match"], dict) and r["match"].get("sets_base"):
                    base = float(r["rate"])
            # bonuses paid only when booked through the bank's own link: a statement line can't show that, so past
            # spend is counted at the normal rate (Which card? still names the bonus and the link)
            hits = [r for r in hits if not (isinstance(r["match"], dict) and (r["match"].get("sets_base") or r["match"].get("link_only")))]
        if not hits:
            return amt * base * f, None, None
        day_is = self.results.get(self.fixtures.get(cid), {}).get(d) if cid in self.fixtures else None
        best = (amt * base * f, None, None, 0.0)
        blocked = None   # a bonus that applies but whose cap was already used up before this purchase
        for r in hits:
            if r.get("promo_until") and r["promo_until"] < d:
                continue
            if dated and ((r["_a"] and d < r["_a"]) or (r["_b"] and d >= r["_b"])):
                continue   # not the version of this rule that was in force that day
            md = r["match"].get("match_day") if isinstance(r["match"], dict) else None
            if md:
                if day_is != md:
                    self.pending = self.pending or day_is == "pending"
                    continue
                lim = self.per_month.get((cid, r["bucket"]))
                days = self.mdays.get((cid, r["bucket"], ym(d)), ())
                if lim and d not in days and len(days) >= lim:
                    continue   # this month's bonus days of this kind are used
            pk = self.key(cid, r, d)
            key = pk or ym(d)
            if (cid, r["bucket"], key) in self.off:
                continue
            rate, cap, _ = engine.boosted(r, float(r["rate"]), self.boosts.get(cid, []), self.amounts)
            if cap and pk:
                room = max(0.0, float(cap) - self.used.get((cid, r["bucket"], key), 0.0))
                bonus = min(amt, room)
            else:
                bonus = amt
            miles = (bonus * rate + (amt - bonus) * base) * f
            if bonus == 0 and rate > base and blocked is None:
                blocked = (r, {"key": key, "cap": cap, "over": amt, "rate": rate, "base": base})
            if miles > best[0]:
                over = (amt - bonus) if bonus < amt else 0.0
                best = (miles, r, {"key": key, "cap": cap, "over": over, "rate": rate, "base": base} if over else None, bonus)
        if best[1] is None and blocked:
            best = (best[0], blocked[0], blocked[1], 0.0)
        miles, rule, over, bonus = best
        if consume and rule is not None:
            pk = self.key(cid, rule, d)
            if pk:
                self.used[(cid, rule["bucket"], pk)] += bonus
                # buckets that draw on one bonus-points limit (OCBC Rewards: the 6 mpd promotion may count toward the
                # 10,000 OCBC$ limit of the 4 mpd bonus): spend here uses up the others' room in their own S$
                sc = rule["match"].get("shares_cap") if isinstance(rule["match"], dict) else None
                if sc:
                    for other, ratio in sc.items():
                        self.used[(cid, other, pk)] += bonus * float(ratio)
            if isinstance(rule["match"], dict) and rule["match"].get("match_day"):
                self.mdays[(cid, rule["bucket"], ym(d))].add(d)
        return miles, rule, over

    def ranked(self, line, ids, n=3):
        """Top n cards for the line at this moment (cap room as it stands), best first: [(miles, card id)]."""
        return sorted(((self.earn(cid, line)[0], cid) for cid in ids if cid in self.cards), reverse=True)[:n]

    def best(self, line, ids, consume=True):
        scored = [(self.earn(cid, line)[0], cid) for cid in ids if cid in self.cards]
        if not scored:
            return 0.0, None
        miles, cid = max(scored)
        if consume:
            self.earn(cid, line, consume=True)
        return miles, cid


def in_store(person):
    """The engine's pay method for this person's shop purchases."""
    return "tap" if (person.get("pay_method") == "tap") else "mobile"


def load(conn, excluded_banks=(), amounts=None, closes=None, store="mobile", held=(), cashback=False):
    """The catalogue (cached, shared and read-only) as a Wallet with this person's boosts and statement days.
    allowed = the cards advice may name. A cashback card is never suggested as a way to earn miles: it is left out
    unless the person holds it or asked for cashback cards too (Settings)."""
    from . import cache
    cat = cache.catalogue(conn)
    ex, mine = set(excluded_banks), set(held or ())
    allowed = [cid for cid, c in cat["active"].items() if c["bank_id"] not in ex
               and (cashback or c["kind"] != "cashback" or cid in mine)]
    w = Wallet(cat["active"], cat["rules"], cat["excl"], cat["boosts"], amounts or {}, closes or {}, cat["app"], store,
               cat.get("hist"))
    w.fixtures, w.results, w.per_month = cat.get("fixtures") or {}, cat.get("results") or {}, cat.get("per_month") or {}
    return w, allowed


def assign(base, lines, ids, enforce_min=True):
    """Best-card replay of the lines on ONE set of cards (no 'actual' track, no alternatives): what the advisor and
    'worth getting' need, at a fraction of run()'s cost. Minimum spends as in run(): bonuses whose minimum the card's
    period total missed are switched off and the replay repeats (max 4 passes).
    -> {"miles", "per": [(line, card id, miles)], "by_card": {card id: {"miles", "spend", "n"}}}"""
    spend = [ln for ln in lines if ln["amount"] > 0]
    ids = [c for c in ids if c in base.cards]
    off, per = set(), []
    for _ in range(4):
        w = base.fork(off)
        per, card_lines = [], defaultdict(list)
        for ln in spend:
            miles, cid = w.best(ln, ids)
            per.append((ln, cid, miles))
            card_lines[cid].append((ln["date"], ln["amount"], ln.get("merchant"), ln))
        new = (_unmet(base, card_lines) - off) if enforce_min else set()
        if not new:
            break
        off |= new
    by = defaultdict(lambda: {"miles": 0.0, "spend": 0.0, "n": 0})
    for ln, cid, miles in per:
        b = by[cid]
        b["miles"] += miles
        b["spend"] += ln["amount"]
        b["n"] += 1
    return {"miles": sum(m for _, _, m in per), "per": per, "by_card": dict(by)}


def shortlist(base, lines, held, allowed, have=None, n=10):
    """Cards worth an exact replay. Replaying every catalogue card was the slow part of My spend. Two cheap scores per
    card, from one look at each line with the card's caps empty:
      direct  - miles it would add over what your wallet got on that line
      floor   - miles it would add over your best UNCAPPED rate (it can also help by freeing another card's cap)
    The top n by each are kept; a card that scores 0 on both cannot add a mile."""
    have = have or assign(base, lines, held)
    probe = base.fork()
    floor = []
    for ln, _, _ in have["per"]:
        flat = dict(ln, amount=ln["amount"])
        best = 0.0
        for h in held:
            if h in base.cards:
                c = base.cards[h]
                rate = float((c["base_fcy"] if ln.get("fcy") else c["base_sgd"]) or 0)
                best = max(best, flat["amount"] * (rate / CENTS_PER_MILE if c["kind"] == "cashback" else rate))
        floor.append(best)
    direct, loose = {}, {}
    for cid in allowed:
        if cid in held or cid not in base.cards:
            continue
        d = l = 0.0
        for (ln, _, got), fl in zip(have["per"], floor):
            m = probe.earn(cid, ln)[0]
            if m > got:
                d += m - got
            if m > fl:
                l += m - fl
        if l > 1:
            direct[cid], loose[cid] = d, l
    top = sorted(direct, key=direct.get, reverse=True)[:n] + sorted(loose, key=loose.get, reverse=True)[:n]
    return list(dict.fromkeys(top))


def _pass(base, spend, held, allowed, off):
    actual, wallet, anyw = base.fork(off["actual"]), base.fork(off["wallet"]), base.fork(off["any"])
    out, overflows = [], {}
    held_ok = [c for c in held if c in base.cards]
    allowed_ok = [c for c in allowed if c in base.cards]
    held_set = set(held)
    card_lines = {"actual": defaultdict(list), "wallet": defaultdict(list), "any": defaultdict(list)}
    for ln in spend:
        cid = ln.get("card_id")
        # the person's cards with every cap as it REALLY stood before this purchase (their own spend so far)
        real_alts = sorted(((actual.earn(c, ln)[0], c) for c in held_ok), reverse=True)[:3]
        a_miles, a_rule, over = actual.earn(cid, ln, consume=True) if cid in base.cards else (0.0, None, None)
        waiting = actual.pending and cid in base.cards   # a match day whose result is not in yet: this may rise
        cap_state = None
        if a_rule is not None and a_rule.get("cap"):
            pk = actual.key(cid, a_rule, ln["date"])
            if pk:
                cap = engine.boosted(a_rule, float(a_rule["rate"]), base.boosts.get(cid, []), base.amounts)[1]
                cap_state = {"label": a_rule["label"] or a_rule["bucket"], "cap": float(cap or a_rule["cap"]),
                             "used": actual.used.get((cid, a_rule["bucket"], pk), 0.0)}
        if over and a_rule is not None:
            k = (cid, a_rule["bucket"], over["key"])
            o = overflows.setdefault(k, {"card": cid, "label": a_rule["label"] or a_rule["bucket"], "cap": over["cap"],
                                         "period": over["key"], "hit": ln["date"], "over_spend": 0.0, "lost": 0.0})
            o["over_spend"] += over["over"]
            o["lost"] += over["over"] * (over["rate"] - over["base"])
        # each card scored once per line, before the best one takes its cap room
        ws = sorted(((wallet.earn(c, ln)[0], c) for c in held_ok), reverse=True)
        w_alts = ws[:3]
        w_miles, w_cid = ws[0] if ws else (0.0, None)
        w_over = None
        if w_cid:
            _, w_rule, wo = wallet.earn(w_cid, ln, consume=True)
            if wo and w_rule is not None and wo["over"] > 0:
                w_over = {"label": w_rule["label"] or w_rule["bucket"], "amount": wo["over"], "cap": wo["cap"],
                          "split": split_rest(wallet, ln, w_cid, wo, held_ok)}
        ys = [(anyw.earn(c, ln)[0], c) for c in allowed_ok]
        y_miles, y_cid = max(ys) if ys else (0.0, None)
        y_alts = heapq.nlargest(3, (y for y in ys if y[1] not in held_set))   # cards to add, same moment
        if y_cid:
            anyw.earn(y_cid, ln, consume=True)
        # spend that counts toward a card's minimum: not what the card excludes (banks leave excluded transactions out)
        for k, c in (("actual", cid), ("wallet", w_cid), ("any", y_cid)):
            if not (ln.get("mcc") and ln["mcc"] in base.excl.get(c, ())):
                card_lines[k][c].append((ln["date"], ln["amount"], ln.get("merchant"), ln))
        out.append({**ln, "match_pending": waiting, "actual": a_miles, "wallet_card": w_cid, "wallet": w_miles, "any_card": y_cid, "any": y_miles,
                    "wallet_alts": w_alts, "add_alts": y_alts, "wallet_over": w_over,
                    "real_alts": real_alts, "cap_state": cap_state,
                    "over": ({"label": a_rule["label"] or a_rule["bucket"], "amount": over["over"]} if over and a_rule else None)})
    return out, overflows, card_lines, actual


def split_rest(wallet, line, cid, over, ids):
    """A purchase that runs over `cid`'s cap can be paid in two parts: the part above the cap on the card that earns
    most for it right now. Returns {card, amount, extra miles over leaving it all on cid} or None when nothing beats it."""
    rest = {**line, "amount": over["over"]}
    alts = [(wallet.earn(c, rest)[0], c) for c in ids if c != cid and c in wallet.cards]
    if not alts:
        return None
    miles, other = max(alts)
    nums = wallet.app.get(cid)
    extra = miles - over["over"] * over["base"] * (nums[0] if nums else 1.0)
    return {"card": other, "amount": over["over"], "extra": extra} if extra >= 1 else None


def card_totals(base, cid, lines):
    """{(bucket, period key): spend on the card} for every rule of the card with a period."""
    tot = defaultdict(float)
    for r in base.rules.get(cid, []):
        own = isinstance(r.get("match"), dict) and r["match"].get("min_own")
        for d, amt, *rest in lines:
            if own and not (len(rest) > 1 and base.counts_for(r, rest[1])):
                continue   # a minimum counted on the bucket's own kind of spend only (UOB Visa Signature)
            k = base.key(cid, r, d) or ym(d)
            tot[(r["bucket"], k)] += amt
    return tot


def shops_rule(r):
    """A minimum that also asks for purchases at N different named shops in the period (DBS yuu: 4 participating
    merchants): (count, {shop: regex}) or None."""
    m = r.get("match") if isinstance(r.get("match"), dict) else {}
    ms = m.get("min_shops")
    return (int(ms["count"]), ms["shops"]) if isinstance(ms, dict) and ms.get("count") else None


def shop_counts(base, cid, lines):
    """{(bucket, period key): set of the named shops bought at} for the card's rules with min_shops."""
    out = defaultdict(set)
    for r in base.rules.get(cid, []):
        sr = shops_rule(r)
        if not sr:
            continue
        for d, _amt, *rest in lines:
            name = rest[0] if rest else None
            for shop, rx in sr[1].items():
                if engine._re_any([rx], name):
                    out[(r["bucket"], base.key(cid, r, d) or ym(d))].add(shop)
    return out


def _unmet(base, card_lines):
    """(card, bucket, period) whose rule has a minimum spend the card's total in that period didn't reach. Periods
    still running count too (the bonus isn't earned yet)."""
    bad = set()
    for cid, lines in card_lines.items():
        if cid not in base.cards:
            continue
        tot = card_totals(base, cid, lines)
        shops = shop_counts(base, cid, lines)
        for r in base.rules.get(cid, []):
            if not r.get("min_spend"):
                continue
            sr = shops_rule(r)
            for (bucket, k), total in tot.items():
                if bucket != r["bucket"]:
                    continue
                if total < float(r["min_spend"]) or (sr and len(shops.get((bucket, k), ())) < sr[0]):
                    bad.add((cid, bucket, k))
                else:
                    # a tier that, once reached, takes the place of a lower one (both draw on one bonus limit)
                    rep = (r.get("match") or {}).get("instead_of") if isinstance(r.get("match"), dict) else None
                    if rep:
                        bad.add((cid, rep, k))
    return bad


def run(base, lines, held, allowed, enforce_min=True):
    """Replay lines. held/allowed = card id lists. Returns per-line results + totals + overflows.
    Minimum spends: a pass finds cards whose month total missed a rule's minimum; those bonuses are switched off and
    the replay runs again (the simulated choices then move to other cards), until nothing changes (max 4 passes)."""
    spend = [ln for ln in lines if ln["amount"] > 0]
    off = {"actual": set(), "wallet": set(), "any": set()}
    for _ in range(4):
        out, overflows, card_lines, actual = _pass(base, spend, held, allowed, off)
        if not enforce_min:
            break   # progress view: count every dollar toward the caps, minimums or not
        changed = False
        for k in off:
            new = _unmet(base, card_lines[k]) - off[k]
            if new:
                off[k] |= new
                changed = True
        if not changed:
            break
    totals = {"spend": sum(ln["amount"] for ln in spend), "actual": sum(x["actual"] for x in out),
              "wallet": sum(x["wallet"] for x in out), "any": sum(x["any"] for x in out)}
    totals["_actual"] = actual           # cap ledger of the real cards (progress panel)
    totals["_card_lines"] = card_lines["actual"]
    totals["_off"] = set(off["actual"])  # bonuses whose minimum spend the real cards missed
    return out, totals, sorted(overflows.values(), key=lambda o: -o["lost"])


def progress(base, lines, held, today):
    """This period on each held card: per capped / minimum-spend bucket -> used, cap, left, min still needed,
    period end. From the real (fed / uploaded) lines."""
    spend = [ln for ln in lines if ln["amount"] > 0]
    _, tot, _ = run(base, spend, held, held, enforce_min=False)
    used, card_lines = tot["_actual"].used, tot["_card_lines"]
    out = []
    for cid in held:
        if cid not in base.cards:
            continue
        sums = card_totals(base, cid, card_lines.get(cid, []))
        shops = shop_counts(base, cid, card_lines.get(cid, []))
        for r in base.rules.get(cid, []):
            if not (r.get("cap") or r.get("min_spend")):
                continue
            k = base.key(cid, r, today)
            if not k:
                continue
            start, end = period_bounds(r, today, base.closes.get(cid))
            rate, cap, _ = engine.boosted(r, float(r["rate"]), base.boosts.get(cid, []), base.amounts)
            u = used.get((cid, r["bucket"], k), 0.0)
            total = sums.get((r["bucket"], k), 0.0)
            out.append({"card": cid, "bucket": r["bucket"], "label": r["label"] or r["bucket"], "rate": float(rate),
                        "cap": float(cap) if cap else None, "used": u, "left": max(0.0, float(cap) - u) if cap else None,
                        "min_spend": float(r["min_spend"]) if r.get("min_spend") else None,
                        "min_left": max(0.0, float(r["min_spend"]) - total) if r.get("min_spend") else None,
                        "shops_need": shops_rule(r)[0] if shops_rule(r) else None,
                        "shops_have": sorted(shops.get((r["bucket"], k), ())) if shops_rule(r) else None,
                        "shops_left": max(0, shops_rule(r)[0] - len(shops.get((r["bucket"], k), ()))) if shops_rule(r) else None,
                        "shops_all": sorted(shops_rule(r)[1]) if shops_rule(r) else None,
                        "card_total": total, "start": start, "end": end, "days_left": (end - today).days,
                        "statement": r.get("cap_period") == "statement" and bool(base.closes.get(cid))})
    return out


def _swap_gain(base, results, name, best, off, held=()):
    """What really changes if every purchase at this merchant had gone on `best` and nothing else moved: the whole
    period is earned again with the person's real spend on every card, so a cap that was really used up (or that the
    switch would take from other purchases) is counted against it.
    Returns (net miles gained, detail) with detail = the S$ of the moved purchases above the card's cap, that cap, how
    much of it the person's real spend had already used when they paid, and the best split of the part over the cap."""
    real, moved = base.fork(off), base.fork(off)
    gain = 0.0
    d = {"over": 0.0, "cap": None, "cap_label": None, "used_before": None, "split": None, "first": None}
    for x in results:
        cid = x.get("card_id")
        hit = _tip_name(x) == name
        if hit and best in base.cards:
            _, rule, _ = real.earn(best, x)   # the bonus that would apply on `best`, and its real cap use right now
            if d["first"] is None:
                d["first"] = x["date"]
                if rule is not None and rule.get("cap"):
                    pk = real.key(best, rule, x["date"])
                    if pk:
                        d["used_before"] = real.used.get((best, rule["bucket"], pk), 0.0)
                        d["cap"], d["cap_label"] = float(rule["cap"]), rule["label"] or rule["bucket"]
        if cid in base.cards:
            gain -= real.earn(cid, x, consume=True)[0]
        to = best if hit else cid
        if to in base.cards:
            if hit:
                _, _, o0 = moved.earn(to, x)
                if o0 and o0["over"] > 0:
                    sp = split_rest(moved, x, to, o0, held)
                    if sp and (d["split"] is None or sp["extra"] > d["split"]["extra"]):
                        d["split"] = {**sp, "on_best": x["amount"] - o0["over"]}
            m, rule, o = moved.earn(to, x, consume=True)
            gain += m
            if hit and o and rule is not None and o["over"] > 0:
                d["over"] += o["over"]
                d["cap"], d["cap_label"] = float(o["cap"]), rule["label"] or rule["bucket"]
    return gain, d


def _tip_name(x):
    return " ".join(x["merchant"].split()[:3])


def tips(results, base, limit=6, off=None, held=()):
    """Repeated merchants where a card you hold would have earned more. The gain is the net change over the whole
    period (see _swap_gain), not the purchase on its own: a switch that only takes cap room from other purchases on
    the same card is no tip."""
    by = defaultdict(lambda: {"gain": 0.0, "n": 0, "spend": 0.0, "used": defaultdict(float), "best": defaultdict(float)})
    for x in results:
        gain = x["wallet"] - x["actual"]
        if gain <= 1 or not x["wallet_card"] or x["wallet_card"] == x.get("card_id"):
            continue
        t = by[_tip_name(x)]
        t["gain"] += gain
        t["n"] += 1
        t["spend"] += x["amount"]
        t["used"][x.get("card_id")] += x["amount"]
        t["best"][x["wallet_card"]] += gain
    out = []
    for name, t in sorted(by.items(), key=lambda kv: -kv[1]["gain"])[:limit * 3]:
        best = max(t["best"], key=t["best"].get)
        used = max(t["used"], key=t["used"].get)
        hits = [x for x in results if _tip_name(x) == name]
        net, d = _swap_gain(base, results, name, best, off, held)
        if net <= 1:
            continue
        out.append({"merchant": name, "gain": net, "n": len(hits), "spend": sum(x["amount"] for x in hits), "best": best,
                    "used": used, **d})
    return sorted(out, key=lambda t: -t["gain"])[:limit]


def worth_getting(base, lines, held, allowed, months, limit=5):
    """Extra miles if one card were added to the wallet (minimum spends counted): per month and a year, valued at
    1.5 c/mile, minus the annual fee (None = fee unknown, not deducted)."""
    if not [ln for ln in lines if ln["amount"] > 0] or not months:
        return []

    def by_month(per):
        m = defaultdict(float)
        for ln, _, miles in per:
            m[ym(ln["date"])] += miles
        return m

    have = assign(base, lines, held)
    have_m = by_month(have["per"])
    out = []
    for cid in shortlist(base, lines, held, allowed, have):
        tot = assign(base, lines, list(held) + [cid])
        extra = (tot["miles"] - have["miles"]) * 12 / months
        if extra <= 1:
            continue
        with_m = by_month(tot["per"])
        per_month = {m: round(with_m[m] - have_m.get(m, 0.0)) for m in sorted(with_m)}
        fee = base.cards[cid]["annual_fee"]
        fee = float(fee) if fee is not None else None
        out.append({"card": cid, "extra_year": extra, "extra_month": extra / 12, "per_month": per_month,
                    "with_month": {m: round(v) for m, v in sorted(with_m.items())},
                    "value": extra * CENTS_PER_MILE / 100 - (fee or 0), "fee": fee,
                    "checked": base.cards[cid]["depth"] == "full", "used": tot["by_card"].get(cid, {"spend": 0.0})["spend"]})
    return sorted(out, key=lambda x: -x["value"])[:limit]
