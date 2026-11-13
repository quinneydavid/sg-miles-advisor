"""`seed` runs at every start: a database seeded by an older version keeps one row per offer, with today's wording."""
from cards import seed


def test_seeding_again_over_an_older_versions_offers(pg):
    conn, _ = pg
    n = conn.execute("SELECT count(*) n FROM signup_offers").fetchone()["n"]
    assert n == len(seed.data("recommended-cards.json")["cards"])
    # the row as 0.10.1 seeded it: the same card, bonus and end date, other wording
    conn.execute("UPDATE signup_offers SET eligibility = 'yes (old wording)', cardup = 'UNCLEAR: old wording', cents_per_mile = 1.61 "
                 "WHERE card_name = 'Citi Prestige'")
    conn.commit()
    seed.seed(conn)
    seed.seed(conn)
    assert conn.execute("SELECT count(*) n FROM signup_offers").fetchone()["n"] == n
    row = conn.execute("SELECT * FROM signup_offers WHERE card_name = 'Citi Prestige'").fetchone()
    assert row["eligibility"] == "new and existing Citi cardholders" and row["cardup"] == "not confirmed" and float(row["cents_per_mile"]) == 2.21
