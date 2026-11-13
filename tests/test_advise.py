import base64
import datetime
import json
import time

from cards import access, advise, replay

D = lambda m, d: datetime.date(2026, m, d)
CARDS = {
    "gen": {"id": "gen", "name": "General", "bank": "A", "kind": "miles", "base_sgd": 1.4, "base_fcy": 2.2, "annual_fee": 0, "bank_id": "a",
            "depth": "full", "programme_id": "P1", "fee_waiver": None, "rounding": None},
    "onl": {"id": "onl", "name": "Online", "bank": "B", "kind": "miles", "base_sgd": 0.4, "base_fcy": 0.4, "annual_fee": 200, "bank_id": "b",
            "depth": "full", "programme_id": "P2", "fee_waiver": None, "rounding": None},
    "dup": {"id": "dup", "name": "Dupe", "bank": "C", "kind": "miles", "base_sgd": 1.2, "base_fcy": 1.2, "annual_fee": 300, "bank_id": "c",
            "depth": "full", "programme_id": "P3", "fee_waiver": None, "rounding": None},
    "din": {"id": "din", "name": "Diner", "bank": "D", "kind": "miles", "base_sgd": 0.4, "base_fcy": 0.4, "annual_fee": 0, "bank_id": "d",
            "depth": "headline", "programme_id": "P4", "fee_waiver": None, "rounding": None},
}
RULES = {
    "onl": [{"bucket": "online", "label": "Online", "rate": 4, "match": {"channel": "online"}, "cap": 1000, "cap_period": "calendar",
             "min_spend": None, "promo_until": None}],
    "din": [{"bucket": "dining", "label": "Dining", "rate": 4, "match": {"mcc": ["5812"]}, "cap": 1000, "cap_period": "calendar",
             "min_spend": None, "promo_until": None}],
}
CAT = {"cards": CARDS, "active": CARDS, "rules": RULES, "excl": {}, "boosts": {}, "offers": {}, "partners": {},
       "mcc_names": {"5812": "Restaurants", "5999": "Misc retail"}}


def base():
    return replay.Wallet(CARDS, RULES, {}, {}, {})


def lines():
    out = []
    for m in (8, 9, 10):
        out.append({"date": D(m, 3), "amount": 800.0, "card_id": "gen", "online": True, "mcc": "5999", "merchant": "SHOP", "fcy": False})
        out.append({"date": D(m, 9), "amount": 500.0, "card_id": "gen", "online": False, "mcc": "5812", "merchant": "CAFE", "fcy": False})
        out.append({"date": D(m, 20), "amount": 700.0, "card_id": "gen", "online": False, "mcc": "5411", "merchant": "MART", "fcy": False})
    return out


def test_assign_matches_the_full_replay():
    held = ["gen", "onl", "dup"]
    assert round(replay.assign(base(), lines(), held)["miles"], 6) == round(replay.run(base(), lines(), held, held)[1]["wallet"], 6)


def test_keep_cancel_and_fee():
    an = advise.analyse(base(), CAT, lines(), ["gen", "onl", "dup"], list(CARDS), 3)
    v = {x["card"]: x for x in an["cards"]}
    assert v["gen"]["verdict"] == "keep"
    # Online: 800 x (4 - 1.4) x 12 = 24,960 miles = S$374 > S$200 fee
    assert round(v["onl"]["adds"]) == 24960 and v["onl"]["verdict"] == "keep"
    assert v["dup"]["verdict"] == "cancel" and v["dup"]["adds"] == 0      # never the best card, S$300 a year
    # the person's own fee wins over the list price: at S$500 the online card no longer pays for itself
    an = advise.analyse(base(), CAT, lines(), ["gen", "onl", "dup"], list(CARDS), 3, fees={"onl": 500, "dup": 0})
    v = {x["card"]: x for x in an["cards"]}
    assert v["onl"]["verdict"] == "waiver" and v["dup"]["verdict"] == "spare"


def test_candidate_and_verdict():
    held = ["gen", "onl"]
    an = advise.analyse(base(), CAT, lines(), held, list(CARDS), 3)
    assert [x["card"] for x in an["add"]] == ["din"]           # dining card adds 500 x 2.6 x 12; Dupe adds nothing
    v = advise.verdict(base(), CAT, lines(), held, list(CARDS), 3, "din", an)
    assert v["verdict"] == "get" and "15,600" in v["headline"]
    assert any("Restaurants" in w for w in v["why"])
    assert any("public reports" in c for c in v["catch"])      # headline depth = unchecked
    assert advise.verdict(base(), CAT, lines(), held, list(CARDS), 3, "dup", an)["verdict"] == "skip"
    assert advise.verdict(base(), CAT, [], held, list(CARDS), 1, "din")["verdict"] == "nodata"


def test_lean_wallet_shares_rise():
    an = advise.analyse(base(), CAT, lines(), ["gen", "onl", "dup"], list(CARDS), 3)
    shares = [s["share"] for s in an["slim"]]
    assert shares == sorted(shares) and shares[-1] > 0.99 and an["slim"][0]["cards"] == ["gen"]


def test_points_plain_words():
    pros, cons = advise.points(CAT, "onl", held=["gen"])
    assert any(p.startswith("4 mpd on online") for p in pros)
    assert any("0.4 mpd" in c for c in cons) and any("S$1,000" in c for c in cons)
    assert advise.label({"label": "This is an eligible merchant; Must complete transaction online to be eligible fo"}) == "Eligible merchants, online only"


def test_written_take_must_use_our_numbers():
    facts = {"sums": ["Extra miles: 15,600 a year", "Fee: S$0"], "card": "Diner"}
    assert advise.grounded("It adds 15,600 miles a year for S$0.", facts)
    assert not advise.grounded("It adds 18,000 miles a year.", facts)


def _token(claims):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'RS256'})}.{b(claims)}.sig"


class _Conn:
    def __init__(self, row):
        self.row, self.asked = row, None

    def execute(self, sql, args=()):
        self.asked = (sql, args)
        return self

    def fetchone(self):
        return self.row


def test_public_door_needs_the_right_audience(monkeypatch):
    monkeypatch.setattr(access, "AUD", "aud-cards")
    monkeypatch.setattr(access, "email_hash", lambda conn, email: "h:" + email.lower())
    ok = {"aud": ["aud-cards"], "exp": time.time() + 60, "email": "Someone@Example.com"}
    conn = _Conn({"id": "bob"})
    # Remote-User is ignored on the public door: only the token's email counts
    p, why = access.public_person(conn, {"Cf-Access-Jwt-Assertion": _token(ok), "Remote-User": "alice"})
    assert p == {"id": "bob"} and conn.asked[1] == ("h:someone@example.com",)
    assert access.public_person(conn, {"Remote-User": "alice"})[0] is None
    assert access.public_person(conn, {"Cf-Access-Jwt-Assertion": _token({**ok, "aud": ["other-app"]})})[0] is None
    assert access.public_person(conn, {"Cf-Access-Jwt-Assertion": _token({**ok, "exp": time.time() - 5})})[0] is None
    assert access.public_person(_Conn(None), {"Cf-Access-Jwt-Assertion": _token(ok)})[0] is None
    monkeypatch.setattr(access, "AUD", "")
    assert access.public_person(conn, {"Cf-Access-Jwt-Assertion": _token(ok)})[0] is None


def test_signup_bonus_only_when_you_qualify():
    o = lambda text, bonus=30000: {"bonus": bonus, "bonus_unit": "miles", "eligibility": text}
    f = advise.offer_for
    ntb = "Get 22,000 bonus miles with a minimum spend of S$800 in the first 60 days of approval, as a new-to-bank customer."
    assert f(o(ntb), "ocbc", "OCBC", {"dbs"})["status"] == "full"                 # no OCBC card: new to the bank
    assert f(o(ntb), "ocbc", "OCBC", {"ocbc"})["bonus"] == 0                      # holds an OCBC card: no bonus
    both = "minimum spend of S$2,000, available to both new and existing Standard Chartered customers."
    assert f(o(both), "sc", "SC", {"sc"})["status"] == "full"
    tier = "Receive a 30,000 (new-to-bank) or 20,000 (existing customer) bonus miles when you pay the annual fee"
    r = f(o(tier), "hsbc", "HSBC", {"hsbc"})
    assert r["status"] == "reduced" and r["bonus"] == 20000
    amex = ("New-to-AMEX customers: Spend S$15,000 and receive 100,000 bonus MR points.\\n"
            "Existing AMEX customers: Spend S$15,000 and receive 60,000 bonus MR points.")
    assert f(o(amex, 109375), "amex", "AMEX", {"amex"})["bonus"] == round(109375 * 0.6)
    silent = "Spend S$800 in the first 2 months and get 40,000 bonus ThankYou points (16,000 miles)."
    r = f(o(silent), "citi", "Citi", {"citi"})
    assert r["status"] == "unclear" and r["bonus"] == 0 and "isn't counted" in r["note"]
    assert f(o(silent), "citi", "Citi", set())["bonus"] == 30000


def test_bank_terms_beat_the_blog_text():
    o = {"bonus": 30000, "bonus_unit": "miles", "eligibility": "Spend S$800 and get 30,000 miles.",
         "terms": {"status": "verified", "who": "new_only", "bank_id": "citi", "lookback_months": 12,
                   "new_means": "no principal Citi credit card now or in the last 12 months", "quote": "q", "url": "https://bank/x.pdf"}}
    r = advise.offer_for(o, "citi", "Citi", {"citi"})
    assert r["status"] == "no" and r["bonus"] == 0 and r["verified"] and r["url"].endswith("x.pdf")
    r = advise.offer_for(o, "citi", "Citi", set())
    assert r["status"] == "full" and "last 12 months" in r["note"]
    o["terms"].update(who="tiered", existing_share=0.5, existing_bonus="15,000 miles", new_bonus="30,000 miles")
    assert advise.offer_for(o, "citi", "Citi", {"citi"})["bonus"] == 15000
    o["terms"].update(who="existing_too")
    assert advise.offer_for(o, "citi", "Citi", {"citi"})["status"] == "full"
    o["terms"]["status"] = "unverified"      # falls back to reading the offer text: silent on existing customers
    assert advise.offer_for(o, "citi", "Citi", {"citi"})["status"] == "unclear"
