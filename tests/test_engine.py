from cards import engine

UOB_PREF_ONLINE = {"channel": "online", "mcc": ["5311", "5812"]}
VISA_SIG_LOCAL = {"any_of": [{"channel": "contactless"}, {"mcc": ["5541", "5542"], "not_merchant": ["\\bSPC\\b"]}]}
OCBC_SIX = {"merchant": ["WATSONS|SHOPEE"], "not_mcc": ["5411"], "not_merchant": ["SHOPEE\\s*PAY"]}
MAYBANK_PETROL = {"mcc": ["5541"], "fcy": False}


def ok(match, **p):
    return engine.applies(match, p)[0]


def test_channel_and_mcc():
    assert ok(UOB_PREF_ONLINE, mcc="5812", pay="online")
    assert not ok(UOB_PREF_ONLINE, mcc="5812", pay="mobile")
    assert not ok(UOB_PREF_ONLINE, mcc="5411", pay="online")


def test_contactless_accepts_mobile_and_tap_only():
    assert ok(VISA_SIG_LOCAL, mcc="5812", pay="mobile") and ok(VISA_SIG_LOCAL, mcc="5812", pay="tap")
    assert not ok(VISA_SIG_LOCAL, mcc="5812", pay="online")
    assert ok(VISA_SIG_LOCAL, mcc="5541", pay="insert", merchant="ESSO")
    assert not ok(VISA_SIG_LOCAL, mcc="5541", pay="insert", merchant="SPC BEDOK")


def test_merchant_rules_and_exclusions():
    assert ok(OCBC_SIX, mcc="5912", pay="online", merchant="Watsons")
    assert ok(OCBC_SIX, mcc="5262", pay="online", merchant="SHOPEE SINGAPORE")   # a card payment on Shopee
    assert not ok(OCBC_SIX, mcc="5262", pay="online", merchant="SHOPEEPAY*SOME SHOP")   # Shopee Pay earns no bonus
    assert not ok(OCBC_SIX, mcc="5262", pay="online", merchant="SHOPEE PAY")
    assert not ok(OCBC_SIX, mcc="5912", pay="online", merchant="Guardian")


def test_fcy_and_ranges():
    assert ok(MAYBANK_PETROL, mcc="5541", pay="insert", fcy=False)
    assert not ok(MAYBANK_PETROL, mcc="5541", pay="insert", fcy=True)
    assert ok({"mcc": ["8211-8299"]}, mcc="8241", pay="online")


def test_rate_card_exclusion_and_base():
    card = {"base_sgd": 1.4, "base_fcy": 2.2}
    rules = [{"rate": 4, "match": {"channel": "online"}, "promo_until": None}]
    assert engine.rate_card(card, rules, {"4900"}, {"mcc": "4900", "pay": "online"})[0] == 0.0
    assert engine.rate_card(card, rules, set(), {"mcc": "5812", "pay": "online"})[0] == 4.0
    assert engine.rate_card(card, rules, set(), {"mcc": "5812", "pay": "mobile", "fcy": True})[0] == 2.2


LADY_RULE = {"bucket": "dining", "rate": 4, "match": {"mcc": ["5812"]}, "cap": 750, "promo_until": None}
LADY_BOOST = {"id": 1, "label": "UOB Lady's Savings balance", "tiers": [[10000, 2], [50000, 4], [100000, 6]],
              "mode": "add", "applies_to": ["dining", "transport"], "cap_override": None}
REV_RULE = {"bucket": "w1", "rate": 4, "match": {"mcc": ["5812"]}, "cap": 1000, "promo_until": None}
REV_BOOST = {"id": 2, "label": "Everyday Global Account deposit", "tiers": [[50000, 8]], "mode": "set",
             "applies_to": [], "cap_override": 1200}
CARD = {"base_sgd": 0.4, "base_fcy": 0.4}
P = {"mcc": "5812", "pay": "mobile"}


def test_add_boost_tiers():
    assert engine.rate_card(CARD, [LADY_RULE], set(), P, [LADY_BOOST], {"1": 10006})[0] == 6.0
    assert engine.rate_card(CARD, [LADY_RULE], set(), P, [LADY_BOOST], {"1": 9999})[0] == 4.0
    rate, _, _, extra = engine.rate_card(CARD, [LADY_RULE], set(), P, [LADY_BOOST], {"1": 60000})
    assert rate == 8.0 and extra["next"] == ("UOB Lady's Savings balance", 100000.0, 10.0)


def test_boost_only_on_named_buckets():
    other = dict(LADY_RULE, bucket="shopping")
    assert engine.rate_card(CARD, [other], set(), P, [LADY_BOOST], {"1": 50000})[0] == 4.0


def test_set_boost_with_cap():
    rate, _, _, extra = engine.rate_card(CARD, [REV_RULE], set(), P, [REV_BOOST], {"2": 50000})
    assert rate == 8.0 and extra["cap"] == 1200
    rate, _, _, extra = engine.rate_card(CARD, [REV_RULE], set(), P, [REV_BOOST], {})
    assert rate == 4.0 and extra["next"] == ("Everyday Global Account deposit", 50000.0, 8.0)


def test_currency_rule_needs_that_currency():
    # UOB PRVI Miles: 3 mpd only when charged in IDR / MYR / THB / VND
    m = {"fcy": True, "currency": ["IDR", "MYR", "THB", "VND"]}
    assert engine.applies(m, {"fcy": True, "currency": "thb"})[0]
    assert not engine.applies(m, {"fcy": True, "currency": "JPY"})[0]
    assert not engine.applies(m, {"fcy": True})[0]            # currency not known: the plain foreign rate
    assert not engine.applies(m, {"fcy": False, "currency": "THB"})[0]


def _x(rate, rule, extra, base=0.4, kind="miles"):
    return {"card": {"base_sgd": base, "base_fcy": base, "kind": kind}, "rule": rule, "rate": rate, "extra": extra}


def test_for_amount_splits_at_the_limit_left():
    # 4 mpd with S$200 of its limit left: a S$500 purchase = 200 x 4 + 300 x 0.4 = 920 miles
    a = engine.for_amount(_x(4.0, LADY_RULE, {"cap": 750, "left": 200.0}), 500)
    assert (a["bonus"], a["over"], round(a["earned"])) == (200.0, 300.0, 920) and round(a["eff"], 2) == 1.84
    # no spend known: the whole cap is assumed free
    assert engine.for_amount(_x(4.0, LADY_RULE, {"cap": 750}), 1000)["bonus"] == 750
    # no cap: all of it at the bonus rate; base rate card: all at base
    assert engine.for_amount(_x(4.0, dict(LADY_RULE, cap=None), {}), 1000)["earned"] == 4000
    assert engine.for_amount(_x(1.2, None, {}), 100)["earned"] == 120
    # cashback card: S$ back and % per S$
    a = engine.for_amount(_x(5.0, dict(LADY_RULE, cap=100), {"cap": 100}, base=0.25, kind="cashback"), 200)
    assert round(a["earned"], 2) == 5.25 and round(a["eff"], 3) == 2.625


def test_purchase_that_reaches_the_minimum_unlocks_it():
    rule = {"bucket": "fcy", "rate": 2.8, "match": {"mcc": ["5812"]}, "cap": None, "min_spend": 800, "promo_until": None}
    room = {"__min__": {"fcy": 300.0}}
    rate, _, _, extra = engine.rate_card(CARD, [rule], set(), dict(P, amount=350), room=room)
    assert rate == 2.8 and extra["unlocks"] and not extra["locked"]
    rate, _, _, extra = engine.rate_card(CARD, [rule], set(), dict(P, amount=100), room=room)
    assert extra["locked"]
    # no spend known: only a purchase as big as the whole minimum unlocks it
    assert engine.rate_card(CARD, [rule], set(), dict(P, amount=800))[3]["unlocks"]
