"""Online orders on cards that pay more online than for a tap: a line whose text does not say how it was paid must not
fall back to the person's usual way (a tap) when the shop only sells online."""
from cards import llm, pipeline


class NoShops:
    def find(self, merchant):
        return None


def line(text, amount=10.0):
    return pipeline.clean({"merchant": text, "amount": amount})


def test_grab_and_atome_lines_read_as_online_from_their_text():
    for text in ("GRB*Kyuukei Coffee Gil", "Grab* A-AXFGG6CG2BEEAV", "ATOME* LIBERNOVO"):
        ln = line(text)
        assert ln["online"] is True and ln["channel"] == "online" and ln["channel_src"] == "text", text
    for text in ("DON DON DONKI", "BEST DENKI - NGEE ANN", "GRABBER SHOP"):
        assert line(text)["online"] is None, text


def test_the_model_marks_online_only_shops_and_leaves_the_rest(monkeypatch):
    asked = []
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {n: "5411" for n in names})
    monkeypatch.setattr(llm, "guess_online", lambda names: asked.extend(names) or {"Butcher Box": "online", "DON DON DONKI": "store"})
    lines = [line("Butcher Box"), line("DON DON DONKI"), line("Lazada SG SINGAPORE"), line("Refund", -5.0)]
    pipeline.place(None, lines, NoShops(), model=True)
    assert asked == ["Butcher Box", "DON DON DONKI"]           # the text already said Lazada; refunds are not asked
    box, donki = lines[0], lines[1]
    assert (box["online"], box["channel"], box["channel_src"]) == (True, "online", "ai")
    assert donki["online"] is None and donki["channel"] is None and donki["web"] == "store"
    assert box["web"] == "online" and lines[2]["web"] == "online"
    assert pipeline.paid(box, {}) == ("online", "ai")
    assert pipeline.paid(donki, {}) == (None, "setting")


def test_no_model_no_question(monkeypatch):
    monkeypatch.setattr(llm, "guess_online", lambda names: (_ for _ in ()).throw(AssertionError("asked")))
    lines = [line("Butcher Box")]
    pipeline.place(None, lines, NoShops(), model=False)
    assert lines[0]["online"] is None


def test_a_model_answer_outside_the_list_or_the_three_words_is_ignored(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda content, **kw: '{"Butcher Box": "online", "DON DON DONKI": "yes", "KOPI": "store", "Other": "online"}')
    assert llm.guess_online(["Butcher Box", "DON DON DONKI", "KOPI"]) == {"Butcher Box": "online", "KOPI": "store"}


def test_fares_and_petrol_are_paid_on_the_spot(monkeypatch):
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {"BUS/MRT 123": "4111", "ESSO WOODLANDS": "5541"})
    monkeypatch.setattr(llm, "guess_online", lambda names: {})
    lines = [line("BUS/MRT 123"), line("ESSO WOODLANDS")]
    pipeline.place(None, lines, NoShops(), model=True)
    assert [x["web"] for x in lines] == ["store", "store"] and all(x["online"] is None for x in lines)


def test_an_atome_line_gets_atomes_own_shop_type(monkeypatch):
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {n: "6011" for n in names})
    monkeypatch.setattr(llm, "guess_online", lambda names: {})
    lines = [line("ATOME* LIBERNOVO")]
    pipeline.place(None, lines, NoShops(), model=True)
    assert (lines[0]["mcc"], lines[0]["mcc_src"], lines[0]["online"]) == ("5999", "text", True)


def test_guessed_lines_get_a_check_badge_and_one_tap_answers(pg, monkeypatch):
    import datetime
    from cards import cache, spendweb, statements
    conn, ids = pg
    me = conn.execute("SELECT * FROM people WHERE id = 'alice'").fetchone()
    held = list(me["held_cards"])
    paymatters = [r["card_id"] for r in conn.execute(
        "SELECT DISTINCT card_id FROM card_rules WHERE card_id = ANY(%s) AND match::text LIKE '%%\"channel\"%%'", (held,)).fetchall()]
    flat = [c for c in held if c not in paymatters]
    assert paymatters, "a held card whose rules depend on how it is paid"
    monkeypatch.setattr(llm, "guess_mccs", lambda names, cands=None: {})
    day = datetime.date.today().isoformat()
    lines = [{"id": "1", "date": day, "card": paymatters[0], "merchant": "BUTCHER BOX", "amount": 159.54},
             {"id": "2", "date": day, "card": paymatters[0], "merchant": "KOPITIAM", "amount": 5, "channel": "mobile"},
             {"id": "3", "date": day, "card": paymatters[0], "merchant": "LAZADA SG", "amount": 20},
             {"id": "5", "date": day, "card": paymatters[0], "merchant": "NTUC FAIRPRICE VIVO", "amount": 42}]
    conn.execute("INSERT INTO merchants (slug, name, mccs, source, channel) VALUES ('ntuc', 'NTUC FairPrice', '{5411}', 'wiki', 'in_person')")
    if flat:
        lines.append({"id": "4", "date": day, "card": flat[0], "merchant": "PLAIN SHOP", "amount": 30})
    statements.feed(conn, "alice", {"lines": lines})
    conn.commit()
    _, body = spendweb.lines_page(conn, {}, me)
    # only the guessed line on the card where it matters: not the bank-stated one, not the online-by-name one, not a
    # shop the shop list knows has stores
    assert body.count(">Check</span>") == 1 and "To check (1)" in body
    assert "name=ch value=online" in body and "name=ch value=mobile" in body
    _, only = spendweb.lines_page(conn, {"show": "check"}, me)
    assert only.count("<details class=tx>") == 1 and "BUTCHER BOX" in only
    # one tap: remembered for the shop, the badge goes
    lid = next(x["id"] for x in cache.lines(conn, "alice") if x["merchant"].startswith("BUTCHER"))
    back = spendweb.post_paid(conn, {"line": [lid], "ch": ["online"], "back": ["/spend/lines?show=check"]}, me)
    assert back == "/spend/lines?show=check"
    _, body = spendweb.lines_page(conn, {}, me)
    assert ">Check</span>" not in body and "paid: Online (your answer for this shop)" in body
