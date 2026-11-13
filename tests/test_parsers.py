import datetime

from cards import catalogue, store


def test_reward_table_splits_on_card_titles():
    t = ("Card Reward Cap Min spend Conditions UOB Preferred Visa CardHamilton 4 mpd S$600 Must complete transaction online "
         "to be eligible UOB Visa Signature CardHamilton 4 mpd S$1,200 S$1,000 Must complete transaction via contactless")
    rows = catalogue.parse_reward_table(t, {"UOB Preferred Visa Card", "UOB Visa Signature Card"})
    assert [(r["card"], r["rate"], r["cap"], r["min"]) for r in rows] == [
        ("UOB Preferred Visa Card", 4.0, 600.0, None), ("UOB Visa Signature Card", 4.0, 1200.0, 1000.0)]
    assert catalogue.condition_match(rows[0]["cond"]) == {"channel": "online"}
    assert catalogue.condition_match(rows[1]["cond"]) == {"channel": "contactless"}


def test_cashback_rows():
    rows = catalogue.parse_reward_table("Trust Cashback Credit CardX 15% S$250 S$2,000 Must select Dining category",
                                        {"Trust Cashback Credit Card"})
    assert rows[0]["unit"] == "%" and rows[0]["rate"] == 15.0 and rows[0]["min"] == 2000.0


def test_name_matching():
    assert catalogue.name_sim("AMEX KrisFlyer Ascend", "American Express Singapore Airlines KrisFlyer Ascend Credit") >= 0.6
    assert catalogue.name_sim("The Platinum Card", "DCS Flex Visa Platinum") < 0.6
    assert catalogue.name_sim("AMEX Platinum Credit Card", "American Express Platinum Credit") > \
        catalogue.name_sim("AMEX Platinum Credit Card", "American Express Platinum")


def test_offer_cost():
    o = {"total_spend": 1000, "bonus": 20000, "fee_text": "None"}
    assert catalogue.offer_cost(o) == round(1000 * 0.0245 * 100 / 20000, 2)
    o = {"total_spend": 4000, "bonus": 65600, "fee_text": "$654"}
    assert catalogue.offer_cost(o) == round((4000 * 0.0245 + 654) * 100 / 65600, 2)
    assert catalogue.offer_cost({"total_spend": 800, "bonus": 1, "fee_text": "S$196.20 (FYF)"}) == round(800 * 0.0245 * 100, 2)


def test_milelion_block():
    raw = ("<h2></h2>\n<p>HSBC Revolution</p><p>4 mpd</p><p>Offer Expires: Dec 31, 2026</p><p>Total Spend</p><p>S$1,000</p>"
           "<p>Total Miles</p><p>20,800</p><p>Income Req.</p><p>S$30,000</p><p>Annual Fee</p><p>None</p>"
           "<h2>DETAILS</h2><table><tr><td>4 mpd</td><td>Dining, Shopping</td></tr></table><p>Other Benefits</p>"
           "<p>Get 42,000 HSBC Points.</p><p>Apply Now</p>")
    o = catalogue.parse_milelion(raw)[0]
    assert o["card_name"] == "HSBC Revolution" and o["bonus"] == 20800 and o["total_spend"] == 1000
    assert o["ends"] == datetime.date(2026, 12, 31) and "4 mpd Dining, Shopping" in o["earn"]


def test_expand_mcc():
    assert store.expand_mcc("8211-8213") == ["8211", "8212", "8213"] and store.expand_mcc("0742") == ["0742"]


def test_annual_fee():
    assert catalogue.annual_fee("S$196.20 (FYF)") == 196.2
    assert catalogue.annual_fee("None") == 0.0 and catalogue.annual_fee("$654") == 654.0
    assert catalogue.annual_fee("") is None and catalogue.annual_fee("-") is None


def test_card_kind_one_unit_per_card():
    from cards.catalogue import card_kind
    b = lambda n: {"n": n}
    assert card_kind({(4.0, "mpd", None, None, "x"): b(5)}) == "miles"
    assert card_kind({(5.0, "%", None, None, "x"): b(5)}) == "cashback"
    # listed in both tables: a miles card, never read as 4 %
    assert card_kind({(4.0, "mpd", None, None, "x"): b(5), (5.0, "%", None, None, "y"): b(9)}) == "miles"
    # a cashback row seen on fewer than 3 shops does not count either way
    assert card_kind({(5.0, "%", None, None, "x"): b(5), (4.0, "mpd", None, None, "y"): b(2)}) == "cashback"
    assert card_kind({}) == "miles"


def test_seeded_offers_state_the_offer_only():
    import json
    from importlib import resources
    doc = json.loads(resources.files("cards").joinpath("data", "recommended-cards.json").read_text())
    assert set(doc) == {"_doc", "checked", "cards"}
    for o in doc["cards"]:
        assert set(o) == {"name", "bonus", "min_spend", "window", "ends", "fee", "existing", "cardup", "cents_per_mile", "base_mpd", "source"}
        cost = catalogue.offer_cost({"total_spend": o["min_spend"], "bonus": o["bonus"], "fee_text": o["fee"]})
        assert o["cents_per_mile"] == cost, o["name"]
