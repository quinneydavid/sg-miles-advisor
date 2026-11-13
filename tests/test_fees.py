import datetime

from cards import fees

D = datetime.date
T = D(2026, 11, 10)


def lines(card, per_month, months):
    return [{"card_id": card, "date": D(2026, m, 15), "amount": per_month} for m in months]


def test_fee_lines_are_recognised():
    assert fees.is_fee("ANNUAL FEE") and fees.is_fee("Annual Membership Fee") and fees.is_fee("ANNUAL CARD FEE REVERSAL")
    assert not fees.is_fee("FEE BROS COFFEE") and not fees.is_fee("LATE PAYMENT FEE") and not fees.is_fee(None)


def test_spend_waiver_progress():
    w = {"data": {"kind": "spend", "amount": 12000}}
    last = D(2026, 3, 14)
    p = fees.waiver_progress(w, lines("a", 1500, range(4, 11)), last, fees.add_year(last), T)
    assert p["spent"] == 10500 and p["status"] == "on_pace" and p["by"] == D(2027, 3, 14)   # 10,500 in 241 days
    p = fees.waiver_progress(w, lines("a", 400, range(4, 11)), last, fees.add_year(last), T)
    assert p["status"] == "short" and p["left"] == 12000 - 2800
    assert fees.waiver_progress(w, lines("a", 4000, range(4, 11)), last, None, T)["status"] == "met"
    assert fees.waiver_progress(w, [], None, None, T)["status"] == "unknown"
    assert fees.waiver_progress({"data": {"kind": "on_request"}}, [], None, None, T) is None


def test_due_dates():
    assert fees.add_year(D(2024, 2, 29)) == D(2025, 2, 28)
    assert fees.next_month(3, T) == D(2027, 3, 1) and fees.next_month(12, T) == D(2026, 12, 1)
    assert fees.next_month(11, T) == D(2026, 11, 1)   # this month still counts


def test_advice_never_invents_a_rule():
    base = {"fee": 196.2, "progress": None, "waiver": None, "waived_last": False}
    assert "No waiver rule" in fees.advice(base)
    assert "was waived" in fees.advice({**base, "waived_last": True})
    assert fees.advice({**base, "fee": 0}) == "Nothing to pay."
    assert "on request" in fees.advice({**base, "waiver": {"data": {"kind": "on_request"}}})


def test_waiver_chase_is_tied_to_the_charge():
    last = {"date": D(2026, 10, 20), "amount": 196.2, "text": "ANNUAL FEE"}
    rows = [{"card": "a", "last": last}]
    p = {"fee_dates": {"a": {"month": 10}}}
    d = fees.mark(p, "a", "asked", rows, T)
    assert d["a"] == {"month": 10, "for": "2026-10-20", "asked": "2026-11-10"}
    d = fees.mark({"fee_dates": d}, "a", "waived", rows, T)
    assert d["a"]["outcome"] == "waived"
    # next year's charge starts clean: the old answer was about the old charge
    nxt = [{"card": "a", "last": {"date": D(2027, 10, 20), "amount": 196.2, "text": "ANNUAL FEE"}}]
    d2 = fees.mark({"fee_dates": d}, "a", "asked", nxt, D(2027, 10, 25))
    assert d2["a"] == {"month": 10, "for": "2027-10-20", "asked": "2027-10-25"}
    assert fees.mark(p, "zzz", "asked", rows, T) == p["fee_dates"]          # not a card with a charge: nothing changes
    assert fees.effective([{"card": "a", "chase": "waived", "last": last}, {"card": "b", "chase": "paid", "last": last},
                           {"card": "c", "chase": "ask", "last": last}]) == {"a": 0.0, "b": 196.2}
    ask = {"fee": 196.2, "progress": None, "waiver": None, "waived_last": False, "chase": "ask", "last": last}
    assert "Ask the bank for a waiver now" in fees.advice(ask)
    assert "can't be waived" in fees.advice({**ask, "waiver": {"data": {"kind": "not_waivable"}}})


def test_how_to_ask_is_per_bank():
    from cards import fees
    assert "Request fee waiver" in fees.how_to_ask("ocbc_titanium") and fees.how_to_ask("hsbc_x") == ""


def test_fee_prompts_order_and_kinds():
    import datetime
    from cards import fees
    t = datetime.date.today()
    rows = [{"card": "a", "chase": None, "fee": 100, "days": 12, "asked": None},
            {"card": "b", "chase": "asked", "fee": 100, "days": 300, "asked": (t - datetime.timedelta(days=50)).isoformat()},
            {"card": "c", "chase": "ask", "fee": 100, "days": 340, "asked": None},
            {"card": "d", "chase": "asked", "fee": 100, "days": 300, "asked": (t - datetime.timedelta(days=5)).isoformat()},
            {"card": "e", "chase": "waived", "fee": 100, "days": 200, "asked": None},
            {"card": "f", "chase": None, "fee": 0, "days": 5, "asked": None}]
    assert [(x["row"]["card"], x["kind"]) for x in fees.prompts(rows)] == [("c", "ask"), ("b", "chase"), ("a", "soon")]
    assert fees.how("uob_lady_solitaire")["steps"].startswith("In the UOB TMRW app") and fees.how("hsbc_x") is None
    assert "10,000" in fees.how("citibank_premiermiles")["before"]
