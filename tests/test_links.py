from cards import links

CARDS = [{"id": i, "name": n, "bank": b, "bank_id": b.lower()} for i, n, b in (
    ("alt_v", "DBS Altitude Visa Signature", "DBS"), ("alt_a", "DBS Altitude American Express", "DBS"),
    ("ww", "DBS Woman's World", "DBS"), ("w", "DBS Woman's Credit", "DBS"), ("taka", "DBS Takashimaya Visa", "DBS"),
    ("plat", "American Express Platinum", "amex"), ("platc", "American Express Platinum Credit", "amex"),
    ("res", "American Express Platinum Reserve Credit", "amex"), ("sol", "UOB Lady's Solitaire", "UOB"),
    ("lady", "UOB Lady's", "UOB"), ("kf", "KrisFlyer UOB", "UOB"), ("vs", "UOB Visa Signature", "UOB"))]
PAGE = "".join(f'<a href="https://milelion.com/{d}/{slug}/"><b>{t}</b></a>' for d, slug, t in (
    ("2026/05/24", "review-dbs-altitude-card", "DBS Altitude Card"), ("2025/08/09", "review-dbs-womans-world-card", "DBS Woman’s World Card"),
    ("2026/02/24", "review-american-express-platinum-credit-card", "AMEX Platinum Credit Card"),
    ("2026/03/07", "review-american-express-platinum-charge", "AMEX Platinum Charge"),
    ("2025/08/12", "review-uob-ladys-card-ladys-solitaire", "UOB Lady’s Card &amp; UOB Lady’s Solitaire Card"),
    ("2026/04/12", "review-krisflyer-uob-credit-card", "KrisFlyer UOB Credit Card"),
    ("2026/06/09", "review-uob-visa-infinite-metal-card", "UOB Visa Infinite Metal Card"),
    ("2026/01/01", "some-news", "Not a card")))


def test_reviews_go_to_the_right_card_only():
    m = {k: v[0].rsplit("/", 2)[1] for k, v in links.match_reviews(CARDS, links.reviews(PAGE)).items()}
    assert m == {"alt_v": "review-dbs-altitude-card", "alt_a": "review-dbs-altitude-card", "ww": "review-dbs-womans-world-card",
                 "platc": "review-american-express-platinum-credit-card", "sol": "review-uob-ladys-card-ladys-solitaire",
                 "lady": "review-uob-ladys-card-ladys-solitaire", "kf": "review-krisflyer-uob-credit-card"}


def test_bank_page_needs_every_word_and_never_a_debit_card():
    ls = [("https://www.dbs.com.sg/personal/cards/debit-cards/dbs-takashimaya-visa-debit", "Takashimaya"),
          ("https://www.dbs.com.sg/personal/cards/credit-cards/dbs-woman-mastercard-card", "DBS Woman's Card"),
          ("https://www.dbs.com.sg/personal/cards/credit-cards/dbs-altitude-cards", "DBS Altitude")]
    assert links.match_pages([c for c in CARDS if c["bank"] == "DBS"], ls) == {
        "w": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-woman-mastercard-card"}


def test_bank_page_must_be_a_card_page():
    kf = [c for c in CARDS if c["id"] == "kf"]
    assert links.match_pages(kf, [("https://www.uob.com.sg/personal/save/everyday-accounts/krisflyer-uob-account.page", "KrisFlyer UOB")]) == {}
    assert links.match_pages(kf, [("https://www.uob.com.sg/personal/cards/credit-cards/travel/krisflyer-uob-credit-card.page", "x")]) == {
        "kf": "https://www.uob.com.sg/personal/cards/credit-cards/travel/krisflyer-uob-credit-card.page"}
