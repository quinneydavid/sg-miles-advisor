"""T&C checker: pure tests (no network, no database). Fixtures are cut-down wordings of real bank documents."""
from cards import tc

UOB = "https://www.uob.com.sg/assets/pdfs/rewardsplus_tnc.pdf"


def doc(body, url=UOB, kind="tc"):
    return tc.Doc(url, tc.tidy(body), kind)


def by_subject(checks):
    return {c["subject"]: c for c in checks}


def card(name="UOB Visa Signature", kind="miles", **kw):
    return {"id": "x", "name": name, "bank_id": "uob", "kind": kind, "base_sgd": None, "base_fcy": None, "annual_fee": None, **kw}


def rule(bucket="local", rate=4, match=None, cap=None, cap_period="calendar", min_spend=None, label=None, rid=1):
    return {"id": rid, "bucket": bucket, "label": label, "rate": rate, "match": match or {}, "cap": cap,
            "cap_period": cap_period, "min_spend": min_spend}


# --- A. only the bank's own site -------------------------------------------------------------------------------------

def test_allowed_is_exact_host_suffix_and_https():
    assert tc.allowed("uob", "https://www.uob.com.sg/assets/pdfs/x.pdf")
    assert tc.allowed("ocbc", "https://www.frankbyocbc.com/pdf/t.pdf")
    assert tc.allowed("sc", "https://av.sc.com/sg/content/docs/x.pdf")
    assert not tc.allowed("uob", "http://www.uob.com.sg/x.pdf")                  # https only
    assert not tc.allowed("uob", "https://www.uob.com.sg.evil.example/x.pdf")    # suffix must be the whole host tail
    assert not tc.allowed("uob", "https://notuob.com.sg/x.pdf")
    assert not tc.allowed("sc", "https://abs.org.sg/docs/library/abs_creditcards_english.pdf")
    assert not tc.allowed("dbs", "https://milelion.com/dbs.com.sg/x.pdf")
    assert not tc.allowed("dbs", "https://www.uob.com.sg/x.pdf")                 # another bank's site is not this bank's
    assert not tc.allowed("nobank", "https://www.uob.com.sg/x.pdf")


def test_clean_url_one_spelling_per_document():
    assert tc.clean_url("https://www.uob.com.sg/assets/pdfs/kf.pdf?i_cid=pfs:sg&amp;vid=none") == "https://www.uob.com.sg/assets/pdfs/kf.pdf"
    assert tc.clean_url("http://WWW.DBS.com.sg/a b.pdf#page=2") == "https://www.dbs.com.sg/a%20b.pdf"
    assert tc.clean_url("https://www.hsbc.com.sg/terms/?utm_source=x&id=7") == "https://www.hsbc.com.sg/terms/?id=7"
    assert tc.clean_url("javascript:void(0)") is None


def test_guess_kind():
    assert tc.guess_kind("https://www.citibank.com.sg/credit-cards/pdf/rewards-exclusion-list.pdf") == "exclusions"
    assert tc.guess_kind("https://www.uob.com.sg/assets/pdfs/uob-one-credit-card-faq.pdf") == "faq"
    assert tc.guess_kind("https://www.ocbc.com/pdf/cards/cards-fee-charge.pdf") == "fees"
    assert tc.guess_kind("https://www.uob.com.sg/x/bonus-welcome-gift-cmpg-tnc.pdf") == "promo"
    assert tc.guess_kind("https://www.dbs.com.sg/iwov-resources/pdf/cards/rewards_programme_tnc.pdf") == "tc"


def test_page_links_keeps_terms_pdfs_only():
    page = """<a href="/assets/pdfs/card-tnc.pdf?i_cid=abc">Terms and Conditions</a>
              <a href="/assets/pdfs/brochure.pdf">Download brochure</a> <a href="/personal/cards/apply.page">Apply now</a>
              <a href='https://www.uob.com.sg/x/list.pdf'><span>Rewards exclusion</span> list</a>
              <a href="https://blog.example/uob-tnc.pdf">Terms and conditions explained</a>"""
    links = dict(tc.page_links(page, "https://www.uob.com.sg/personal/cards/one.page"))
    assert links == {"https://www.uob.com.sg/assets/pdfs/card-tnc.pdf": "Terms and Conditions",
                     "https://www.uob.com.sg/x/list.pdf": "Rewards exclusion list",
                     "https://blog.example/uob-tnc.pdf": "Terms and conditions explained"}
    assert [u for u in links if tc.allowed("uob", u)] == list(links)[:2]   # the blog link never becomes a document


def test_extract_html():
    text, pages, title = tc.extract(b"<html><head><title>Card T&amp;Cs</title><style>p{}</style></head><body><p>Earn 4 miles"
                                    b"</p><script>x()</script><p>per S$1</p></body></html>", "text/html")
    assert (text, pages, title) == ("Earn 4 miles\nper S$1", None, "Card T&Cs")


# --- C. MCC lists ----------------------------------------------------------------------------------------------------

EXCLUSION_TABLE = """
1.2 UNI$ will not be awarded for NETS transactions, cash advances and fees, and shall further exclude the
transactions stated below (collectively the "Excluded Transactions"):

a) all transactions in relation to bill payment and top-ups of any pre-paid card;

b) all transactions or payments made to SPC Service Stations;

c) all transactions under the following Merchant Category Codes (MCC):

MCC Description
4829 Wire Transfer/Remittance
4900 Utilities (with effect from 1 August 2022)
5199 Nondurable Goods (Not Elsewhere Classified)
5960 Direct Marketing - Insurance Services
6540 Stored Value Card Purchase/Load
7995 Betting, including Lottery Tickets and Wagers at Race Tracks
8211 to 8220 Schools
Call us at 1800 222 2121 or 6222 2121. Updated 1 August 2025. A fee of S$5000 applies.
"""


def test_exclusion_table_under_an_enumeration():
    lists = doc(EXCLUSION_TABLE).lists
    assert len(lists) == 1 and lists[0]["kind"] == "exclude"
    # codes and the range, but no year (2022, 2025), phone number or dollar amount; table descriptions do not split it
    assert lists[0]["codes"] == {"4829", "4900", "5199", "5960", "6540", "7995"} | {f"{i}" for i in range(8211, 8221)}


CATEGORY_TABLE = """
"Preferred Rewards Category" means a category you selected, to qualify for Bonus UNI$, set out in the table below.
Categories Merchant Category Codes
(MCC)
Description
Dining 5811, 5812, 5814, 5499 Caterers, Eating places and Restaurants
Fashion 5311, 5611, 5621, 5631,
5651, 5655
Department Stores, Clothing Stores
Transport 4111, 4121, 4789, 5541,
5542
Local Commuter Transport, Taxi

"Dining Merchants" refer to all merchants tagged with the following MCCs, excluding hotel dining.
MCC Description
1 5811 Caterers
2 5812 Eating Places and Restaurants
"""


def test_category_table_rows_and_asides():
    lists = doc(CATEGORY_TABLE).lists
    assert [l["kind"] for l in lists] == ["include", "include"]     # 'excluding hotel dining' is not about the list
    rows = {r[0]: r[1] for r in lists[0]["rows"]}
    assert rows == {"Dining": {"5811", "5812", "5814", "5499"}, "Fashion": {"5311", "5611", "5621", "5631", "5651", "5655"},
                    "Transport": {"4111", "4121", "4789", "5541", "5542"}}
    assert lists[1]["codes"] == {"5811", "5812"}


def test_cue_kind():
    assert tc.cue_kind("KrisFlyer miles will not be awarded for transactions bearing the following MCC: ") == "exclude"
    assert tc.cue_kind("Eligible Transactions fall within any one of the following Merchant Category Codes (MCC): ") == "include"
    assert tc.cue_kind("(excluding hotel dining and supermarkets) which fall under any one of the following MCC: ") is None
    assert tc.cue_kind("Online spend refers to retail transactions, excluding the following transactions: ") == "exclude"
    assert tc.cue_kind("all transactions classified under the following Merchant Category Codes: ") is None   # neutral


def test_fmt_and_compare_codes():
    assert tc.fmt_codes({"5811", "5812", "5813", "5814", "6300", "0742"}) == "0742, 5811-5814, 6300"
    assert tc.compare_codes({"5811", "5812"}, {"5812", "5811"}) == ("match", "2 MCCs", "2 MCCs")
    status, ours, theirs = tc.compare_codes({"4829", "5960"}, {"5960", "6540", "6300"})
    assert (status, ours) == ("differs", "2 MCCs")
    assert theirs == "3 MCCs; in the document but not ours: 6300, 6540; ours but not in the document: 4829"


def test_rule_mccs_and_exclusions_are_compared_code_by_code():
    d = doc(EXCLUSION_TABLE + CATEGORY_TABLE)
    r = rule("dining", match={"mcc": ["5811", "5812", "5814", "5499"]})
    c = by_subject(tc.pattern_checks(card("UOB Lady's Solitaire"), [r], {}, {"4829", "4900"}, [d]))
    assert c["rule:dining:mccs"]["status"] == "match" and "Dining 5811, 5812, 5814, 5499" in c["rule:dining:mccs"]["quote"]
    ex = c["exclusions"]
    assert ex["status"] == "differs" and ex["ours"] == "2 MCCs" and "in the document but not ours: 5199, 5960, 6540" in ex["theirs"]
    assert ex["quote"].startswith("1.2 UNI$ will not be awarded") and ex["url"] == UOB
    # nothing recorded on our side is a real finding, worded plainly
    ex = by_subject(tc.pattern_checks(card(), [], {}, set(), [d]))["exclusions"]
    assert (ex["status"], ex["ours"]) == ("differs", "none recorded")
    # every code of the document is excluded here too: no red flag for holding more
    ours = set(d.lists[0]["codes"]) | {"6300"}
    ex = by_subject(tc.pattern_checks(card(), [], {}, ours, [d]))["exclusions"]
    assert ex["status"] == "match" and "ours holds 1 more" in ex["theirs"]
    # ranges in match.mcc and include rows from card_rule_mccs
    assert tc.rule_mccs({"match": {"any_of": [{"mcc": ["5811-5813"]}, {"fcy": True}]}}, [{"mcc": "4121", "effect": "include"},
                                                                                         {"mcc": "6300", "effect": "exclude"}]) == {"5811", "5812", "5813", "4121"}


# --- C. numbers ------------------------------------------------------------------------------------------------------

SHARED = """
8.2 The following cards earn UNI$ as set out below.

• UOB Preferred Visa Cardmembers earn up to UNI$10 per S$5 spend on Mobile Contactless Transactions and Selected
Online Transactions only.

• UOB Visa Signature Cardmembers earn up to:
(i) UNI$10 for every S$5 spent on transactions in foreign currencies, subject to a minimum foreign currencies spend of
S$1,000 per statement period.
(ii) UNI$10 for every S$5 spent on petrol and contactless transactions, subject to a minimum local spend of S$1,000
per statement period. The awarding of UNI$10 per S$5 spend on Petrol Spend and/or Contactless Transactions is subject
to a cap of UNI$2,400, per statement period.

• UOB Lady's Classic Card Cardmembers: the Bonus UNI$ you can earn on dining in a calendar month is capped at 1,800 UNI$
which is equivalent to S$1,000 spent.
"""
CONTACTLESS = {"any_of": [{"channel": "contactless"}, {"mcc": ["5541", "5542"]}]}


def test_points_rate_needs_the_programme_conversion():
    r = rule("local", 4, CONTACTLESS)
    assert tc.rates_in("UNI$10 for every S$5 spent", False, 0.5)[0][0] == 4          # 2 UNI$ per S$1 at 0.5 UNI$ per mile
    assert tc.rates_in("UNI$10 for every S$5 spent", False, None) == []              # no conversion known: no claim
    assert tc.rates_in("earn 1.2 KrisFlyer miles per S$1 and 3 miles for every S$1", False)[1][0] == 3
    assert tc.rates_in("8% cashback on dining", True)[0][0] == 8
    c = by_subject(tc.pattern_checks(card(), [r], {}, set(), [doc(SHARED)], ppm=0.5))["rule:local:rate"]
    assert c["status"] == "match" and c["theirs"].startswith("4 mpd (UNI$10 for every S$5")
    assert by_subject(tc.pattern_checks(card(), [r], {}, set(), [doc(SHARED)]))["rule:local:rate"]["status"] == "not_found"


def test_points_cap_is_never_read_as_dollars():
    """Coordinator case 1: 'cap of UNI$2,400' is points. Our cap is dollars of spend: no comparison, no 'S$2,400'."""
    r = rule("local", 4, CONTACTLESS, cap=1200, cap_period="statement")
    c = by_subject(tc.pattern_checks(card(), [r], {}, set(), [doc(SHARED)], ppm=0.5))["rule:local:cap"]
    assert (c["status"], c["theirs"], c["quote"]) == ("not_found", None, None)
    assert [m.group(0) for m in tc.MONEY.finditer("a cap of UNI$2,400 or US$50, min S$1,000")] == ["S$1,000"]


def test_cashback_cap_is_not_a_spend_cap():
    """Coordinator case 1: 'capped at S$120' of cashback against our S$2,000 of spend is shown, never 'differs'."""
    body = """UOB One Card. All Selected Merchant Transactions on dining charged in each statement month will qualify for
    additional cashback that will be capped at S$120 per statement month. Separately, you earn 6% cashback on dining
    transactions, capped at S$60 per statement month."""
    r = rule("w1", 10, {"mcc": ["5812"]}, cap=2000)
    c = by_subject(tc.pattern_checks(card("UOB One", "cashback"), [r], {}, set(), [doc(body)]))["rule:w1:cap"]
    assert c["status"] == "not_found"            # two different cashback caps, none convertible to ours: nothing claimed
    r = rule("w1", 6, {"mcc": ["5812"]}, cap=1000, cap_period="statement")     # S$60 at 6% = S$1,000 of spend: exact
    c = by_subject(tc.pattern_checks(card("UOB One", "cashback"), [r], {}, set(), [doc(body)]))["rule:w1:cap"]
    assert c["status"] == "match" and c["theirs"] == "S$60 cashback per statement month (= S$1,000 of spend at 6%)"
    one = "UOB One Card dining transactions earn additional cashback that will be capped at S$120 per statement month."
    r = rule("w1", 10, {"mcc": ["5812"]}, cap=2000)
    c = by_subject(tc.pattern_checks(card("UOB One", "cashback"), [r], {}, set(), [doc(one)]))["rule:w1:cap"]
    assert (c["status"], c["theirs"]) == ("not_found", "S$120 cashback per statement month")   # the bank's figure, no verdict
    assert c["quote"] in " ".join(one.split())


def test_spend_cap_and_minimum_spend():
    body = """DBS Woman's World Card. Bonus DBS Points are accumulated for the calendar month, subject to a cap on the first
    S$1,000 of online spend per calendar month. To earn 10 DBS Points for every S$5 on online spend you must meet a minimum
    spend of S$500 on online transactions in the month."""
    r = rule("online", 4, {"channel": "online"}, cap=1000, min_spend=800)
    c = by_subject(tc.pattern_checks(card("DBS Woman's World"), [r], {}, set(), [doc(body)], ppm=0.5))
    assert c["rule:online:cap"]["status"] == "match" and c["rule:online:cap"]["theirs"] == "S$1,000 of spend per calendar month"
    # another minimum spend, in the sentence that also states this rule's rate: a difference worth a flag
    assert (c["rule:online:min_spend"]["status"], c["rule:online:min_spend"]["theirs"]) == ("differs", "S$500")
    r = rule("online", 4, {"channel": "online"}, cap=1500)
    cap = by_subject(tc.pattern_checks(card("DBS Woman's World"), [r], {}, set(), [doc(body)], ppm=0.5))["rule:online:cap"]
    assert (cap["status"], cap["theirs"]) == ("not_found", "S$1,000 of spend per calendar month")   # no rate in that sentence


def test_other_cards_sentences_are_not_used():
    """Coordinator case 2: in a programme document, a sentence that names another card proves nothing about this one."""
    d = doc(SHARED)
    mobile = rule("mobile", 4, {"channel": "mobile"})
    # 'UOB Preferred Visa Cardmembers earn up to UNI$10 per S$5 ... Mobile Contactless' is not about Visa Signature ...
    lone = doc(SHARED.split("• UOB Visa Signature")[0])
    assert by_subject(tc.pattern_checks(card(), [mobile], {}, set(), [lone], ppm=0.5))["rule:mobile:rate"]["status"] == "not_found"
    # ... but it is about the Preferred Visa
    c = by_subject(tc.pattern_checks(card("UOB Preferred Visa"), [mobile], {}, set(), [d], ppm=0.5))["rule:mobile:rate"]
    assert c["status"] == "match" and c["quote"].startswith("• UOB Preferred Visa Cardmembers earn up to UNI$10 per S$5")
    # Visa Signature's own section is found under its heading, and the quote is not the Preferred Visa sentence
    c = by_subject(tc.pattern_checks(card(), [rule("local", 4, CONTACTLESS, min_spend=1000)], {}, set(), [d], ppm=0.5))
    assert c["rule:local:rate"]["status"] == "match" and "Preferred" not in c["rule:local:rate"]["quote"]
    assert c["rule:local:min_spend"]["status"] == "match"
    # a sibling with a longer name: 'Lady's Classic' is not Lady's Solitaire; the plain Lady's card is covered by it
    dining = rule("dining", 4, {"mcc": ["5812"]}, cap=1000)
    sol = by_subject(tc.pattern_checks(card("UOB Lady's Solitaire"), [dining], {}, set(), [d], ppm=0.5, siblings=["UOB Lady's"]))
    assert sol["rule:dining:cap"]["status"] == "not_found" and sol["rule:dining:cap"]["quote"] is None
    lady = by_subject(tc.pattern_checks(card("UOB Lady's"), [dining], {}, set(), [d], ppm=0.5, siblings=["UOB Lady's Solitaire"]))
    assert lady["rule:dining:cap"]["status"] == "match" and lady["rule:dining:cap"]["theirs"] == "S$1,000 of spend per calendar month"


def test_unnamed_sentence_in_a_document_about_several_cards_is_unknown():
    body = """General terms. Cardmembers will earn UNI$1 for every S$5 on all other transactions in local currency.
    """ + "x " * 900 + """UOB Empire World Business Cardmembers earn UNI$5 for every S$10 charged on retail purchase."""
    c = card(base_sgd=0.4)
    assert by_subject(tc.pattern_checks(c, [], {}, set(), [doc(body)], ppm=0.5))["base_sgd"]["status"] == "not_found"
    own = "UOB Visa Signature Card terms. You will earn UNI$1 for every S$5 on all other transactions in local currency."
    assert by_subject(tc.pattern_checks(c, [], {}, set(), [doc(own)], ppm=0.5))["base_sgd"]["status"] == "match"


def test_rates_are_never_called_different_by_a_pattern():
    body = "CIMB Visa Signature Card awards an extra 2% cashback on online shopping, capped at S$25 with a minimum spend of S$800."
    r = rule("w1", 8, {"channel": "online"}, cap=25, min_spend=800)
    c = by_subject(tc.pattern_checks(card("CIMB Visa Signature", "cashback"), [r], {}, set(), [doc(body)]))
    assert c["rule:w1:rate"]["status"] == "not_found" and c["rule:w1:min_spend"]["status"] == "match"


def test_every_claim_carries_a_verbatim_quote():
    d = doc(SHARED + EXCLUSION_TABLE + CATEGORY_TABLE)
    rules = [rule("local", 4, CONTACTLESS, cap=1200, min_spend=1000), rule("dining", 4, {"mcc": ["5811", "5812", "5814", "5499"]}, rid=2)]
    checks = tc.pattern_checks(card(), rules, {}, {"4829"}, [d], ppm=0.5)
    assert {c["status"] for c in checks} >= {"match", "differs", "not_found"}
    for c in checks:
        if c["status"] in ("match", "differs"):
            assert c["url"] == UOB and c["method"] == "pattern"
            assert c["quote"] in d.norm and "\n" not in c["quote"] and len(c["quote"]) <= 320
            assert not c["quote"].endswith(("UN", "S$1,0"))
        elif c["quote"] is None:
            assert c["theirs"] is None and c["url"] is None
    # a quote that is not in the document can never get through, whatever claims it
    fake = tc.check("base_sgd", "match", "4 mpd", "4 mpd", "Cardmembers earn 4 miles per S$1 on everything.", UOB)
    assert tc.verified(fake, [d])["status"] == "not_found"


def test_long_quotes_stop_at_a_word():
    body = "The following Merchant Category Codes are excluded " + "and also this very long clause continues " * 12 + "end."
    d = doc(body)
    q = d.quote(0, 400)
    assert len(q) <= 300 and q in d.norm and body.startswith(q) and body[len(q)] == " "


# --- C. model path (stubbed) -----------------------------------------------------------------------------------------

def test_model_answer_needs_a_verbatim_quote(monkeypatch):
    d = doc(SHARED)
    asks = {"rule:fcy:min_spend": "S$1,000", "rule:fcy:rate": "4 mpd", "base_sgd": "0.4 mpd", "annual_fee": "S$218"}
    reply = """```json
    {"rule:fcy:min_spend": {"status": "match", "theirs": "minimum foreign currency spend S$1,000",
        "quote": "(i) UNI$10 for every S$5 spent on transactions in foreign currencies, subject to a minimum foreign  currencies spend of\\nS$1,000 per statement period."},
     "rule:fcy:rate": {"status": "differs", "theirs": "5 mpd", "quote": "UOB Visa Signature Cardmembers earn 5 miles per S$1 overseas."},
     "base_sgd": {"status": "match", "theirs": "UNI$10 per S$5", "quote": "UOB Preferred Visa Cardmembers earn up to UNI$10 per S$5 spend on Mobile Contactless Transactions and Selected"},
     "annual_fee": {"status": "not_found", "theirs": "", "quote": ""},
     "rule:made:up": {"status": "match", "theirs": "x", "quote": "UOB Visa Signature Cardmembers earn up to:"}}```"""
    seen = {}
    monkeypatch.setattr(tc.llm, "chat", lambda prompt, **kw: seen.setdefault("prompt", prompt) and reply)
    out = tc.model_answers(card(), asks, d, ppm=0.5)
    assert set(out) == {"rule:fcy:min_spend"}       # invented quote, another card's sentence, unasked subject: all dropped
    c = out["rule:fcy:min_spend"]
    assert c["method"] == "model" and c["quote"] in d.norm and c["url"] == UOB
    assert "0.5 point(s) = 1 mile" in seen["prompt"] and "rule:fcy:rate: 4 mpd" in seen["prompt"]


def test_model_never_overrides_a_quoted_pattern_result(monkeypatch):
    pattern = tc.check("rule:local:rate", "match", "4 mpd", "4 mpd", "UNI$10 for every S$5 spent on petrol", UOB)
    model = tc.check("rule:local:rate", "differs", "4 mpd", "2 mpd", "something else in the document", UOB, "model")
    assert tc.merge(pattern, model) is pattern
    assert tc.merge(pattern, dict(model, status="match")) is pattern          # they agree: nothing changes
    nothing = tc.check("rule:local:cap", "not_found", "S$1,200")
    assert tc.merge(nothing, model) is model and tc.merge(nothing, None) is nothing
    assert tc.merge(nothing, dict(model, quote=None)) is nothing             # a model answer without a quote is no answer

    d = doc(SHARED)
    checks = [pattern, nothing, tc.check("exclusions", "not_found", "3 MCCs")]
    asked = []

    def chat(prompt, **kw):
        asked.append(prompt)
        return '{"rule:local:cap": {"status": "match", "theirs": "S$1,200", "quote": "There is a cap of S$1,200 on petrol spend."}}'
    monkeypatch.setattr(tc.llm, "chat", chat)
    out = by_subject(tc.model_checks(card(), checks, [d], {}, set(), ppm=0.5))
    assert len(asked) == 1 and "rule:local:cap" in asked[0] and "- exclusions" not in asked[0] and "- rule:local:rate" not in asked[0]
    assert out["rule:local:rate"]["method"] == "pattern" and out["rule:local:rate"]["status"] == "match"
    assert (out["rule:local:cap"]["status"], out["rule:local:cap"]["method"]) == ("not_found", "model")   # quote was invented
    # next run: same facts, unchanged documents -> the remembered answer is reused, the model is not asked again
    old = {"rule:local:cap": dict(out["rule:local:cap"])}
    tc.model_checks(card(), [tc.check("rule:local:cap", "not_found", "S$1,200")], [d], old, set(), ppm=0.5)
    assert len(asked) == 1
    tc.model_checks(card(), [tc.check("rule:local:cap", "not_found", "S$1,200")], [d], old, {UOB}, ppm=0.5)   # document changed
    assert len(asked) == 2


# --- writing + read API ----------------------------------------------------------------------------------------------

class FakeConn:
    """Just enough of a connection for write_checks: remembers the check rows and the change-log lines."""

    def __init__(self):
        self.rows, self.log = {}, []

    def execute(self, sql, params=()):
        if sql.startswith("SELECT"):
            return type("R", (), {"fetchall": lambda _: list(self.rows.values())})()
        if "catalogue_changes" in sql:
            self.log.append(params)
        elif sql.startswith("INSERT INTO card_tc_checks"):
            self.rows[params[1]] = dict(zip(("card_id", "subject", "status", "ours", "theirs", "quote", "url", "method"), params))
        elif sql.startswith("DELETE"):
            self.rows.pop(params[1], None)


def test_a_difference_is_logged_once():
    from collections import Counter
    conn, stats = FakeConn(), Counter()
    differs = tc.check("exclusions", "differs", "2 MCCs", "3 MCCs; in the document but not ours: 6300", "q", UOB)
    gone = tc.check("rule:old:rate", "not_found", "4 mpd")
    tc.write_checks(conn, stats, "c1", [differs, gone])
    tc.write_checks(conn, stats, "c1", [differs])                       # same finding next week: no new line
    assert conn.log == [("c1", "T&C differs: exclusions", "2 MCCs", "3 MCCs; in the document but not ours: 6300", UOB)]
    assert set(conn.rows) == {"exclusions"}                             # the closed rule's row is gone
    tc.write_checks(conn, stats, "c1", [dict(differs, theirs="4 MCCs; in the document but not ours: 6300, 6540")])
    assert len(conn.log) == 2                                           # the bank's list changed: said again
    assert stats["checks differs"] == 3 and stats["checks not_found"] == 1


def test_badge():
    assert tc.badge({"match": 3, "differs": 0, "not_found": 5}) == "verified"
    assert tc.badge({"match": 3, "differs": 1, "not_found": 0}) == "differs"
    assert tc.badge({"match": 0, "differs": 0, "not_found": 4}) == "unverified"
    assert tc.badge({}) == "unverified"


# --- annual fee and waiver -------------------------------------------------------------------------------------------

FEE_PAGE = """
DBS Credit Card Annual Fee
Annual Fee amounts shown are inclusive of GST.
Card Name
Principal Card's Annual Fee
Supplementary Card's Annual Fee
Fee Waiver
DBS Woman’s MasterCard® Card
S$163.50
S$81.75
1 Year Annual Fee Waiver
*Charge a minimum of S$15,000 to your DBS Woman’s MasterCard® Card each year to receive the annual fee waiver for the following year.
DBS Woman’s World MasterCard® Card
S$196.20
S$98.10
1 Year Annual Fee Waiver
*Charge a minimum of S$25,000 to your DBS Woman’s World MasterCard® Card each year to receive the annual fee waiver for the following year.
With effect from 1 August 2026 , the Annual Fee waiver eligibility upon meeting the retail spend of S$25,000 in the previous card year will be ceased.
DBS Vantage Card
S$599.50
N.A.
Annual fee waiver is not applicable for this card.
PRVI Miles American Express ®
S$261.60
S$130.80
Spend p.a. S$50,000
PRVI Miles World Mastercard/Visa
S$261.60
Free #
UNI$6500
SAFRA DBS Card
No annual fee, so long as you remain a SAFRA member.
Quarterly Service Fee
DBS Platinum Visa Card
S$21.80
Card Replacement Fee
S$30
"""
FEES = "https://www.dbs.com.sg/personal/support/card-charges-and-fees-annual-fee.html"


def fees(name, today, annual_fee=None, body=FEE_PAGE, siblings=(), kind="fees"):
    import datetime
    c = dict(card(name), bank_id="dbs", annual_fee=annual_fee)
    out = by_subject(tc.pattern_checks(c, [], {}, set(), [tc.Doc(FEES, tc.tidy(body), kind)], siblings=siblings,
                                       today=datetime.date(*today)))
    return out["annual_fee"], out["fee_waiver"]


def test_fee_table_row_is_matched_by_card_name():
    fee, waiver = fees("DBS Woman's", (2026, 11, 10), siblings=["DBS Woman's World"])
    assert (fee["status"], fee["theirs"], fee["ours"]) == ("info", "S$163.50 a year (first year waived)", None)   # nothing on file: 'bank says'
    assert fee["data"] == {"fee": 163.5, "first_year_free": True, "supp_fee": 81.75}
    assert fee["quote"] == "DBS Woman’s MasterCard® Card S$163.50" and fee["url"] == FEES
    assert waiver["data"] == {"kind": "spend", "amount": 15000.0, "period": "membership_year", "years_free": 1}
    # the sibling's row, not the shorter name's; compared with the figure we hold (within S$1)
    fee, _ = fees("DBS Woman's World", (2026, 11, 10), annual_fee=196.2, siblings=["DBS Woman's"])
    assert (fee["status"], fee["data"]["fee"], fee["ours"]) == ("match", 196.2, "S$196.20")
    assert fees("DBS Woman's World", (2026, 11, 10), annual_fee=150)[0]["status"] == "differs"
    # a card the table does not list, and rows outside the annual-fee table, are never guessed at
    fee, waiver = fees("DBS Altitude", (2026, 11, 10))
    assert (fee["status"], fee["quote"], waiver["status"], waiver["data"]["kind"]) == ("not_found", None, "not_found", "none_stated")
    assert fees("DBS Platinum Visa", (2026, 11, 10))[0]["status"] == "not_found"          # that S$21.80 is a quarterly fee
    # same family, another scheme: the Visa is not the American Express row
    fee, waiver = fees("UOB PRVI Miles Visa", (2026, 11, 10))
    assert fee["quote"].startswith("PRVI Miles World Mastercard/Visa") and fee["data"]["supp_fee"] == 0
    assert waiver["data"]["kind"] == "points" and waiver["quote"] == "UNI$6500"            # 'Free #' is the supplementary card
    fee, waiver = fees("UOB PRVI Miles American Express", (2026, 11, 10))
    assert (waiver["data"]["kind"], waiver["data"]["amount"], waiver["quote"]) == ("spend", 50000.0, "Spend p.a. S$50,000")
    fee, waiver = fees("SAFRA DBS", (2026, 11, 10))
    assert (fee["theirs"], fee["data"]["fee"], waiver["data"]["kind"]) == ("no annual fee", 0.0, "automatic")


def test_spend_waiver_and_its_end_date():
    _, waiver = fees("DBS Woman's World", (2026, 7, 1))                 # before the bank's end date: still a spend waiver
    assert waiver["status"] == "info" and waiver["data"] == {"kind": "spend", "amount": 25000.0, "period": "membership_year", "years_free": 1}
    assert "ends on 1 August 2026" in waiver["theirs"] and waiver["quote"].startswith("*Charge a minimum of S$25,000")
    _, waiver = fees("DBS Woman's World", (2026, 11, 10))               # after it: only what still stands is reported
    assert waiver["data"] == {"kind": "first_year", "amount": None, "period": None, "years_free": 1}
    assert waiver["theirs"] == "First year waived; the S$25,000 spend waiver ended on 1 August 2026"
    assert waiver["quote"].startswith("With effect from 1 August 2026")
    # a sentence in the card's own terms
    body = """Terms and Conditions Governing UOB PRVI Miles American Express Card. Your annual fee will be waived with a minimum
    spend of S$25,000 in a membership year. The annual fee for supplementary cards is S$130.80."""
    fee, waiver = fees("UOB PRVI Miles American Express", (2026, 11, 10), body=body, kind="tc")
    assert waiver["data"] == {"kind": "spend", "amount": 25000.0, "period": "membership_year", "years_free": None}
    assert waiver["theirs"] == "Waived with S$25,000 of spend in a membership year" and fee["status"] == "not_found"
    # ... which says nothing to the Visa of the same family
    assert fees("UOB PRVI Miles Visa", (2026, 11, 10), body=body, kind="tc")[1]["status"] == "not_found"


def test_waiver_not_applicable_and_gst():
    fee, waiver = fees("DBS Vantage", (2026, 11, 10), annual_fee=599.5)
    assert (fee["status"], fee["data"]) == ("match", {"fee": 599.5, "first_year_free": None, "supp_fee": None})
    assert (waiver["status"], waiver["theirs"], waiver["data"]["kind"]) == ("info", "Not waivable", "not_waivable")
    assert waiver["quote"] == "Annual fee waiver is not applicable for this card."
    body = "Standard Chartered Smart Card. The annual fee for the Smart Card is waived off for the first year. Thereafter, " \
           "an annual fee of S$91 (excluding prevailing GST) will be charged."
    fee, waiver = fees("Standard Chartered Smart", (2026, 11, 10), annual_fee=99.19, body=body, kind="tc")
    assert fee["status"] == "match" and fee["theirs"] == "S$91 a year before GST (first year waived); the same fee as ours once 9% GST is added"
    assert (waiver["data"]["kind"], waiver["data"]["years_free"]) == ("first_year", 1)
    assert tc.read_waiver([(0, 5, "There is no annual fee charged for supplementary Journey Cards.")]) is None
    assert tc.read_waiver([(0, 5, "Citi Prestige: perpetual annual fee waiver for the basic card")])[0]["kind"] == "automatic"
    assert tc.read_waiver([(0, 5, "The annual fee is S$196.20 and is charged yearly.")]) is None       # nothing written, nothing inferred
