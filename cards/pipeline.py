"""One clean-up for every transaction line, whatever brought it in.

Every source hands its lines to the same two steps, so the same text gives the same answer through each of them:

  clean(raw)          the merchant text tidied, the bank's own charges dropped, a purchase abroad and its currency
                      worked out, an online shop recognised. What the source states (a currency, "online", a way of
                      paying) is kept; what it does not state is read from the text.
  place(conn, lines)  the merchant category: the shop list first, the model for the rest when one is set up. A shop
                      the list knows as online-only, and tickets (airlines, travel agents), count as online.

Sources: the Wealthfolio import (wealthfolio.py), statement files and alert mails (statements.py, ingest.py), and a
feeder (statements.feed).

Also here, because every source shares them:

  how a line was paid   paid(): what the source states > the person's saved answer for that shop > an online shop
                        recognised from the text > the person's usual way (Settings) > unknown. Applied when lines
                        are read, so a saved answer needs no rewriting of stored lines.
  one source per card   owner(): where a card's lines come from for this person (Wealthfolio, mail and statements,
                        or a feeder). Unset = worked out from what is there.
  statement closing day set_close_day(): typed by the person > read from a statement file > sent by a feeder >
                        estimated from notes in Wealthfolio."""
import datetime
import re

from psycopg.types.json import Jsonb

from . import llm

# text that only an online purchase carries
ONLINE = re.compile(r"\.COM\b|\bWWW\.|\bHTTPS?\b|\bPAYPAL\s*\*|\bPP\s*\*|\bGOOGLE\s*\*|\bAPPLE\.COM|\bAMZN\b|"
                    r"\bAMAZON\s*(MKTP|MARKETPLACE|PRIME|SG|WEB\s*SERVICES)\b|\bAWS\b|\bOPENAI\b|\bANTHROPIC\b|\bGITHUB\b|\bCLOUDFLARE\b|\bSHOPEE\b|\bLAZADA\b|\bTAOBAO\b|\bALIEXPRESS\b|\bTEMU\b|"
                    r"\bSTEAM(GAMES|POWERED)?\b|\bNETFLIX\b|\bSPOTIFY\b|\bDISNEY\s*PLUS\b|\bAIRALO\b|\bKLOOK\b|"
                    r"\bGRA?B\s*\*|\bATOME\s*\*", re.I)   # GRB*/GRAB* = paid in the Grab app; ATOME* = Atome pay-later
# shop types paid where you are: transit fares, taxis, petrol, hotels, restaurants and bars
ON_THE_SPOT = {"4111", "4121", "5541", "5542", "7011", "5812", "5813"}
ATOME = re.compile(r"^\s*ATOME\s*\*", re.I)
# CardUp charges the card under its own code for every payment, whatever is paid through it (rent, tax, bills)
CARDUP = re.compile(r"\bCARD\s*UP\b", re.I)
# a merchant abroad, from the line's text: a foreign city or a trailing country code ("... HOTEL BANGKOK TH")
FOREIGN_PLACE = re.compile(
    r"\b(BANGKOK|THAILAND|HUA\s?HIN|PATTAYA|PHUKET|CHIANG\s?MAI|KRABI|SAMUI|KUALA\s?LUMPUR|JOHOR|"
    r"PENANG|LANGKAWI|BALI|DENPASAR|JAKARTA|HANOI|HO\s?CHI\s?MINH|DA\s?NANG|MANILA|CEBU|TOKYO|OSAKA|KYOTO|SAPPORO|SEOUL|"
    r"BUSAN|HONG\s?KONG|MACAU|TAIPEI|SHANGHAI|BEIJING|SYDNEY|MELBOURNE|PERTH|BRISBANE|AUCKLAND|QUEENSTOWN|"
    r"CHRISTCHURCH|WELLINGTON|LONDON|PARIS|DOHA|DUBAI|ISTANBUL|ZURICH|AMSTERDAM|DUBLIN|SAN\s?FRANCISCO|NEW\s?YORK|"
    r"LOS\s?ANGELES|SEATTLE)\b"
    r"|\s(TH|MY|ID|JP|KR|HK|MO|TW|CN|AU|NZ|GB|UK|US|USA|FR|DE|IT|ES|NL|CH|IE|QA|AE|VN|PH|IN|TR)$", re.I)
# the currency of a charge abroad when the line does not print it (some cards pay more in certain currencies)
PLACE_CUR = [("THB", re.compile(r"\b(BANGKOK|THAILAND|HUA\s?HIN|PATTAYA|PHUKET|CHIANG\s?MAI|KRABI|SAMUI)\b|\sTH$", re.I)),
             ("MYR", re.compile(r"\b(KUALA\s?LUMPUR|JOHOR|PENANG|LANGKAWI|MALAYSIA)\b|\sMY$", re.I)),
             ("IDR", re.compile(r"\b(BALI|DENPASAR|JAKARTA|INDONESIA)\b|\sID$", re.I)),
             ("VND", re.compile(r"\b(HANOI|HO\s?CHI\s?MINH|DA\s?NANG|VIETNAM)\b|\sVN$", re.I))]
SG_TEXT = re.compile(r"SINGAPOR|\bSG\b|\bSGP\b|\bSGD\b", re.I)
FCY = re.compile(r"\b(USD|EUR|GBP|JPY|AUD|NZD|MYR|THB|HKD|CNY|RMB|IDR|KRW|TWD|VND|PHP|INR|CHF|CAD|AED)\s?[\d,]+\.\d{2}\b")
# the bank's own charges: not spend, no miles
BANK_CHARGE = re.compile(r"\bFINANCE\s*CHARGE|\bLATE\s*(PAYMENT\s*)?(CHARGE|FEE)|\bINTEREST\b|\bOVERLIMIT\s*FEE|"
                         r"\bCONVERSION\s*FEE\b|\bFX\s*FEE|^\s*GST\b|\bFOREIGN\s*(CURRENCY\s*)?TRANSACTION\s*FEE|"
                         r"\bGST\s*(ON|@)|\bADMIN(ISTRATIVE)?\s*FEE|\bCASH\s*ADVANCE\s*FEE", re.I)
# a payment to the card, when a source lists it among the purchases
PAYMENT = re.compile(r"^\s*(PAYMENT\b.*(THANK|RECEIVED)|PYMT\b|GIRO\s*PAYMENT|PAYMT\s*THRU|BILL\s*PAYMENT\b|FAST\s*PAYMENT\b|"
                     r"INTERNET\s*BANKING\s*PAYMENT|AUTO-?PAY\b)", re.I)

FIELDS = ("merchant", "amount", "currency", "fcy", "online", "channel", "channel_src", "mcc", "mcc_src", "est", "web")
# "web": what is known about the shop itself when the line does not say how it was paid: "online" (sells only online),
# "store" (has shops you walk into), "unsure", or None (not looked at). Lines not known to be "store" are worth a check.

# how a purchase was paid. The stored word -> the engine's pay method. 'contactless' = a tap whose device the source does
# not say: the person's usual way decides between phone and card.
CHANNELS = [("online", "Online"), ("mobile", "Phone or watch"), ("tap", "Contactless card"), ("insert", "Chip or swipe")]
PAY_OF = {"online": "online", "mobile": "mobile", "tap": "tap", "insert": "insert", "chip": "insert"}
STATED = set(PAY_OF) | {"contactless"}
PAID_SRC = {"bank": "the bank's line says so", "you": "your answer for this shop", "text": "an online shop, from its name",
            "ai": "an online-only shop (AI guess)",
            "setting": "your usual way of paying (Settings)"}

SOURCES = [("wealthfolio", "Wealthfolio"), ("mail", "Mail and statement files"), ("feed", "Feeder")]
CLOSE_RANK = {"note": 1, "feed": 2, "statement": 3, "you": 4}
CLOSE_SRC = {"note": "estimated from Wealthfolio notes", "feed": "from the feeder", "statement": "read from a statement file",
             "you": "typed here"}

NOISE = {"SINGAPORE", "SINGAPOR", "SINGAPO", "SG", "SGP", "SGD", "PTE", "LTD", "PL", "THE", "CO", "INC", "LLC", "WWW",
         "COM", "HTTPS", "HTTP", "SINGAPOREA", "SIN", "SGPSG"}


def words(s):
    return [w for w in re.findall(r"[A-Z0-9&']+", (s or "").upper()) if w not in NOISE and not w.isdigit()]


def squash(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def place_of(text):
    """The merchant part of a source's text (some sources append notes after ' | ')."""
    return (text or "").split(" | ")[0].strip()


def shop_key(merchant):
    """The shop a line belongs to, for the person's saved answers: its first two words, without reference numbers
    and place noise ('GRAB*A-8812345 SINGAPORE SG' and 'Grab* A-771' are the same shop)."""
    return " ".join([w for w in words(merchant) if len(w) > 1 and not any(ch.isdigit() for ch in w)][:2])


class ShopIndex:
    """Public + household merchant names -> MCCs; household entries win. Matching: all words of a shop name appear in
    the line's text, longest name wins."""

    def __init__(self, conn):
        self.items = []
        for r in conn.execute("SELECT name, mccs, source, channel FROM merchants WHERE mccs <> '{}' ORDER BY source = 'household'").fetchall():
            w = words(r["name"])
            if w:
                self.items.append((set(w), len(" ".join(w)), r))

    def find(self, text):
        tw = words(text)
        t = set(tw)
        lead = set(tw[:2])   # the brand is at the start of a statement line ("AMAZON SG MARKETPLACE")
        best = None
        for ws, n, r in self.items:
            if ws <= t and ws & lead and (best is None or n > best[0] or (n == best[0] and r["source"] == "household")):
                best = (n, r)
        return best[1] if best else None


# ---------------------------------------------------------------- the two steps ----------------------------------------------------------------
def clean(raw):
    """raw: {"merchant": the source's text, "amount", and what the source states, if anything: "currency", "fcy" (a
    bool), "online" (a bool), "channel", "est", "mcc", "mcc_src"}.
    -> the line's fields, or None when the text is the bank's own charge or a payment to the card. Pure."""
    text = squash(raw.get("merchant"))
    if BANK_CHARGE.search(text) or PAYMENT.search(text):
        return None
    place = place_of(text)
    cur = str(raw.get("currency") or "").upper()[:3]
    printed = FCY.search(text)
    if isinstance(raw.get("fcy"), bool):
        fcy = raw["fcy"]                                  # the source knows
    elif cur and cur != "SGD":
        fcy = True
    else:
        fcy = bool(printed) or (bool(FOREIGN_PLACE.search(place)) and not SG_TEXT.search(place))
    if not fcy:
        cur = "SGD"
    elif not cur or cur == "SGD":
        cur = ((printed.group(1) if printed else "") or next((c for c, rx in PLACE_CUR if rx.search(place)), "") or "FCY").upper()[:3]
    ch = str(raw.get("channel") or "").strip().lower()
    ch = ch if ch in STATED else None
    online = raw.get("online") if isinstance(raw.get("online"), bool) else None
    src = None
    if ch:
        online, src = ch == "online", "source"
    elif online:
        ch, src = "online", "source"
    elif online is None and ONLINE.search(text):
        online, ch, src = True, "online", "text"
    mcc = str(raw["mcc"])[:4] if raw.get("mcc") else None
    return {"merchant": place[:120], "amount": raw.get("amount"), "currency": cur, "fcy": fcy, "online": online,
            "channel": ch, "channel_src": src, "mcc": mcc, "mcc_src": (raw.get("mcc_src") or "source") if mcc else None,
            "est": bool(raw.get("est")), "web": "online" if online else None}


def place(conn, lines, shops=None, model=None):
    """Give each purchase its merchant category: what the source sent, else the shop list, else the model when one is
    set up (model=True asks it regardless: the statement reader already needs one). Also marks a shop the list knows
    as online-only, and tickets (airlines, travel agents), as bought online.
    -> how many purchases stayed without a category."""
    shops = shops or ShopIndex(conn)
    unknown, cands = [], {}
    for ln in lines:
        if ln["amount"] <= 0:
            ln.setdefault("mcc", None)
            ln.setdefault("mcc_src", None)
            continue
        m = shops.find(ln["merchant"])
        if not ln.get("mcc") and ATOME.search(ln["merchant"]):
            ln["mcc"], ln["mcc_src"] = "5999", "text"   # Atome charges the card as its own online shop type
        if CARDUP.search(ln["merchant"]) and ln.get("mcc_src") not in ("source", "your correction"):
            ln["mcc"], ln["mcc_src"] = "7399", "text"   # never read as rent (6513) or tax from what was paid
        if not ln.get("mcc"):
            if m and len(m["mccs"]) == 1:
                ln["mcc"], ln["mcc_src"] = m["mccs"][0], ("you" if m["source"] == "household" else "shop list")
            else:
                ln["mcc"], ln["mcc_src"] = None, None
                unknown.append(ln["merchant"])
                if m:
                    cands[ln["merchant"]] = list(m["mccs"])   # shop seen under several MCCs: the model picks one
        if m and ln.get("online") is None and m["channel"] == "online":
            ln["online"], ln["channel"], ln["channel_src"] = True, "online", "text"
        if m and m["channel"] == "in_person" and not ln.get("web"):
            ln["web"] = "store"
    ask = bool(llm.key()) if model is None else model
    guesses = llm.guess_mccs(unknown, cands) if unknown and ask else {}
    left = 0
    for ln in lines:
        if ln["amount"] > 0 and not ln.get("mcc"):
            g = guesses.get(ln["merchant"])
            src = "shop list + ai" if g and g in cands.get(ln["merchant"], ()) else "ai guess"
            ln["mcc"], ln["mcc_src"] = (g, src) if g else (None, "unknown")
            left += not g
        mcc = ln.get("mcc") or ""
        if ln["amount"] > 0 and ln.get("online") is None and (mcc in ("4511", "4722") or "3000" <= mcc <= "3299"):
            ln["online"], ln["channel"], ln["channel_src"] = True, "online", "text"
    # a shop whose name does not say: the model knows online-only shops (delivery, e-commerce); otherwise the person's
    # usual way decides, which would count an online order as a tap
    is_open = lambda ln: ln["amount"] > 0 and ln.get("online") is None and not ln.get("channel") and not ln.get("web")
    web = llm.guess_online([ln["merchant"] for ln in lines if is_open(ln)]) if ask and any(map(is_open, lines)) else {}
    for ln in lines:
        if ln["amount"] > 0 and ln.get("online") is None and not ln.get("channel"):
            if (ln["mcc"] or "") in ON_THE_SPOT and not ln.get("web"):
                ln["web"] = "store"
            v = web.get(ln["merchant"])
            if v and not ln.get("web"):
                ln["web"] = v
            if ln.get("web") == "online":
                ln["online"], ln["channel"], ln["channel_src"] = True, "online", "ai"
    return left


def run(conn, raws, shops=None, model=None):
    """clean + place for a batch. raws: dicts as for clean(), with any extra keys (id, date, card ...) carried over.
    -> (lines, {"bank charges": n, "could not place": n})"""
    out, dropped = [], 0
    for raw in raws:
        c = clean(raw)
        if c is None:
            dropped += 1
            continue
        out.append({**{k: v for k, v in raw.items() if k not in c}, **c})
    left = place(conn, out, shops, model)
    return out, {"bank charges": dropped, "could not place": left}


# ---------------------------------------------------------------- how a line was paid ----------------------------------------------------------------
def paid(line, memory):
    """(the engine's pay method or None = the person's usual way, where the answer came from)."""
    ch, src = line.get("channel"), line.get("channel_src")
    if ch and src is None:   # lines stored before the source was recorded: only 'online' could come from the text
        src = "text" if ch == "online" else "source"
    stated = ch if src == "source" and ch in STATED else None
    if stated and stated != "contactless":
        return PAY_OF[stated], "bank"
    mem = memory.get(shop_key(line.get("merchant"))) if memory else None
    if mem and str(line.get("date") or "9999") >= str(mem.get("since") or "") and mem.get("ch") in PAY_OF:
        if not (stated == "contactless" and mem["ch"] in ("online", "insert")):   # a tap is a tap
            return PAY_OF[mem["ch"]], "you"
    if stated == "contactless":
        return None, "bank"
    if line.get("online") or ch == "online":
        return "online", ("ai" if src == "ai" else "text")
    return None, "setting"


def remember(memory, merchant, ch, line_date=None, today=None):
    """Save (or with ch empty, forget) the person's answer for the shop of this line. It counts from the start of the
    open month, or of the line's month when that is earlier. -> the shop key, or None when the text names no shop."""
    key = shop_key(merchant)
    if not key:
        return None
    if ch not in PAY_OF:
        memory.pop(key, None)
        return key
    start = (today or datetime.date.today()).replace(day=1)
    if line_date and line_date < start:
        start = line_date.replace(day=1)
    old = memory.get(key) or {}
    since = min(start.isoformat(), old["since"]) if old.get("since") else start.isoformat()
    memory[key] = {"ch": ch, "since": since, "name": squash(merchant)[:40]}
    return key


# ---------------------------------------------------------------- the person's corrections ----------------------------------------------------------------
# what a purchase was charged in. Some cards pay more in certain currencies (UOB PRVI Miles: THB, VND, IDR, MYR).
CURRENCIES = ["SGD", "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "MYR", "THB", "IDR", "VND", "PHP", "HKD", "CNY", "TWD", "KRW",
              "INR", "CHF", "CAD", "AED"]


def fix(line, fixes):
    """Apply the person's saved correction for the line's shop (fixes: {shop key: {"currency", "mcc", ...}}), in place.
    A correction counts for every purchase at that shop, earlier ones too: what currency a shop charges in and what
    kind of shop it is do not change with the date. -> the line."""
    f = fixes.get(shop_key(line.get("merchant"))) if fixes else None
    if not f:
        return line
    cur = f.get("currency")
    if cur == "SGD":
        line.update(currency="SGD", fcy=False, est=False, fcy_amount=None)
    elif cur in CURRENCIES:
        line.update(currency=cur, fcy=True)
    if f.get("mcc") and re.fullmatch(r"\d{4}", f["mcc"]):
        line.update(mcc=f["mcc"], mcc_src="your correction")
    line["fixed"] = True
    return line


def remember_fix(fixes, merchant, currency=None, mcc=None):
    """Save the person's correction for the shop of this line: the currency (one of CURRENCIES) and the shop type (4
    digits). An empty value leaves that part to the bank's line and the shop list. -> the shop key, or None."""
    key = shop_key(merchant)
    if not key:
        return None
    entry = {}
    if currency in CURRENCIES:
        entry["currency"] = currency
    if mcc and re.fullmatch(r"\d{4}", mcc):
        entry["mcc"] = mcc
    if entry:
        fixes[key] = {**entry, "name": squash(merchant)[:40]}
    else:
        fixes.pop(key, None)
    return key


# ---------------------------------------------------------------- one source per card ----------------------------------------------------------------
def kind_of(statement_id, person):
    if statement_id == f"feed-{person}":
        return "feed"
    if statement_id == f"wf-{person}":
        return "wealthfolio"
    return "mail"


def chosen(person_row):
    """{card id: source} the person set themselves."""
    d = (person_row or {}).get("card_sources") or {}
    names = dict(SOURCES)
    return {c: s for c, s in d.items() if s in names}


def effective(conn, person_row, wf_cards=()):
    """{card id: (source, chosen?)} for every held card: the person's choice, else what is there: a feeder that sent
    the card in the last 62 days, else Wealthfolio when the card is matched to an account there and its import is on,
    else mail and statement files."""
    pid = person_row["id"]
    mine = chosen(person_row)
    since = datetime.date.today() - datetime.timedelta(days=62)
    fed = {r["card_id"] for r in conn.execute("SELECT DISTINCT card_id FROM stmt_lines WHERE statement_id = %s AND txn_date >= %s",
                                              (f"feed-{pid}", since)).fetchall()}
    out = {}
    for c in person_row["held_cards"] or []:
        if c in mine:
            out[c] = (mine[c], True)
        else:
            out[c] = ("feed" if c in fed else "wealthfolio" if c in wf_cards else "mail", False)
    return out


def set_source(conn, person_row, card, source):
    """Move one card to a source ('' = back to automatic). The automatic sources' lines of that card that no longer
    apply are removed, so nothing is counted twice: a feeder's when the card leaves the feeder, Wealthfolio's when it
    leaves Wealthfolio. Statement and alert lines are never removed. -> lines removed."""
    pid = person_row["id"]
    mine = dict(person_row.get("card_sources") or {})
    if source in dict(SOURCES):
        mine[card] = source
    else:
        mine.pop(card, None)
    conn.execute("UPDATE people SET card_sources = %s WHERE id = %s", (Jsonb(mine), pid))
    gone = 0
    drop = {"wealthfolio": [f"feed-{pid}"], "feed": [f"wf-{pid}"], "mail": [f"feed-{pid}", f"wf-{pid}"]}.get(source, [])
    for sid in drop:
        ids = [r["id"] for r in conn.execute("SELECT id FROM stmt_lines WHERE statement_id = %s AND card_id = %s", (sid, card)).fetchall()]
        if ids:
            conn.execute("DELETE FROM stmt_lines WHERE id = ANY(%s)", (ids,))
            n = conn.execute("SELECT count(*) n, min(txn_date) a, max(txn_date) b FROM stmt_lines WHERE statement_id = %s", (sid,)).fetchone()
            conn.execute("UPDATE statements SET n_lines = %s, period_from = %s, period_to = %s WHERE id = %s", (n["n"], n["a"], n["b"], sid))
        gone += len(ids)
    return gone


# ---------------------------------------------------------------- statement closing day ----------------------------------------------------------------
def set_close_day(conn, person, card, day, src):
    """Record the day of the month a card's statement closes. A typed value wins over one read from a statement file,
    which wins over a feeder's, which wins over an estimate from Wealthfolio notes; day None with src 'you' = forget it (the calendar month, until a source says).
    -> True when stored."""
    row = conn.execute("SELECT statement_days, statement_days_src FROM people WHERE id = %s", (person,)).fetchone()
    days, srcs = dict(row["statement_days"] or {}), dict(row["statement_days_src"] or {})
    if day is None:
        if src != "you":
            return False
        days.pop(card, None)
        srcs.pop(card, None)
    else:
        have = srcs.get(card) or ("feed" if card in days else None)
        if have and CLOSE_RANK[src] < CLOSE_RANK.get(have, 0):
            return False
        if days.get(card) == int(day) and srcs.get(card) == src:
            return False
        days[card], srcs[card] = int(day), src
    conn.execute("UPDATE people SET statement_days = %s, statement_days_src = %s WHERE id = %s", (Jsonb(days), Jsonb(srcs), person))
    return True
