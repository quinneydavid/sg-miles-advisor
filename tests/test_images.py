import pytest

from cards import images

LADYS = {"id": "uob_lady_s_card", "name": "UOB Lady's", "bank_id": "uob"}
SOLITAIRE = {"id": "uob_lady_solitaire", "name": "UOB Lady's Solitaire", "bank_id": "uob"}
SPREE = {"id": "sc_spree", "name": "Standard Chartered Spree", "bank_id": "sc"}
M1 = {"id": "citi_m1_card", "name": "Citi M1", "bank_id": "citi"}
SIBS = [LADYS, SOLITAIRE]


def test_sibling_superset_rejected():
    assert not images.names_match(LADYS, "uob.com.sg/cards/ladys-solitaire-metal-card.png", SIBS)
    assert images.names_match(SOLITAIRE, "uob.com.sg/cards/ladys-solitaire-metal-card.png", SIBS)
    assert images.names_match(LADYS, "uob.com.sg/cards/ladys-card/index/card-face.png", SIBS)


def test_key_words_required():
    assert not images.names_match(SPREE, "sc.com/sg/credit-cards/beyond-card-face.png", [SPREE])
    assert images.names_match(SPREE, "sc.com/sg/credit-cards/spree-card.png", [SPREE])
    assert not images.names_match(M1, "citibank.com.sg/premiermiles-card.png", [M1])


def test_only_web_addresses_are_fetched():
    for url in ("file:///etc/passwd", "ftp://example.com/card.png", "data:image/png;base64,AAAA", "/etc/passwd", ""):
        with pytest.raises(ValueError):
            images.fetch_image(url)
