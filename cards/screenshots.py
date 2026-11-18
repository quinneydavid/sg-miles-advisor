"""Screenshots of a bank's app or website, for a purchase the alert mails missed.

A person drops a screenshot of their card's transaction list into their drop folder (ingest.run_files). The model
reads it twice: once for every row (date, description, amount, currency, pending) and the card, once for the amounts
alone. The two must agree, or nothing is stored and the file goes to failed/ with a note.

Rows that are pending, not in S$, dated after the screenshot or more than 120 days before it are left out. Each row
left is matched against the lines already held for that card (alerts, earlier screenshots, statements): the same
amount on the same side within 3 days, closest date first, each held line taken once, so two equal coffees on one day
stay two and overlapping screenshots add only their new rows. An alert can show the amount before a foreign
transaction fee while the bank's app shows the posted amount (about 1% more), so a held line at the same shop (a
shared word of its name), on the same side, within 5 days and 4% (at least S$2) is the same purchase too; an alert
matched that way takes the posted amount. Only rows with no match are stored. They sit with the
alert lines (section "screenshot"), so a statement for that card and month hides them like it hides alerts, and an
alert mail that arrives later for a row already stored is skipped (drop_seen_alerts).

The card comes from the last four digits on the screenshot, else from four digits in the file name ("UOB 7545.png"),
matched against the person's cards on the Settings page. The file goes to done/ with a .txt beside it saying what
became of each row. Logs hold counts only."""
import base64
import datetime
import io
import os
import re
import uuid
from collections import Counter

from . import crypto, llm, statements

IMAGES = (".png", ".jpg", ".jpeg", ".webp")
HEIC = (".heic", ".heif")
DAYS = 3          # how far apart a screenshot row and a held line may be dated and still be the same purchase
NEAR_DAYS = 5     # ... when the amounts differ a little (a foreign fee posted after the alert) at the same shop
NEAR_SHARE, NEAR_MIN = 0.04, 200   # how much they may differ: 4% of the larger, at least S$2 (in cents)
FILLER = {"SINGAPORE", "SGP", "PTE", "LTD", "LONDON", "THE", "COM", "WWW", "INC", "LLC", "PAYMENT", "PURCHASE"}
OLDEST = 120      # days before the screenshot: older rows are left to the statements
SECTION = "screenshot"

READ = """This is a screenshot of a Singapore bank's app or website, taken on {day} ({weekday}).
If it shows card transactions, reply JSON only:
{{"transactions": true, "bank": "...", "kind": "card transactions", "card_name": "..." or null, "last4": "1234" or null,
"rows": [{{"date": "YYYY-MM-DD", "description": "...", "amount": 12.34, "currency": "SGD", "pending": false}}]}}
One row per transaction, top to bottom, as shown. A date heading applies to the rows below it; rows above the
first date heading visible have an unknown date: give them "date": null. amount: positive for a purchase or a charge, negative for a refund,
a credit or a payment to the card. Dates such as "Today", "Yesterday", "Mon" or "16 Nov" are worked out from the day
the screenshot was taken, when the row or the heading above it shows one; a date without a year is the latest such date not after that day. pending: true for a row
marked pending, authorised or processing. currency: the currency of the amount shown, SGD unless another is shown.
last4: the last four digits of the card number, if the screenshot shows them.
If it does not show card transactions, reply {{"transactions": false}}."""

AMOUNTS = """List the amount of every transaction this screenshot of a bank's app shows, top to bottom, as plain numbers:
positive for a purchase or a charge, negative for a refund, a credit or a payment to the card.
Reply JSON only: {"amounts": [12.34, ...]}"""


class Refused(Exception):
    """The screenshot cannot be used; the text says why, for the note beside the file."""


def _image(data):
    """-> (mime, bytes) the model is sent. Anything Pillow cannot open is refused; a very large picture is re-encoded."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise Refused("This file couldn't be opened as a picture. Save the screenshot as PNG or JPEG and drop it here again.")
    if len(data) <= 4_000_000 and im.format in ("PNG", "JPEG", "WEBP"):
        return f"image/{im.format.lower()}", data
    im.thumbnail((2000, 4000))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=90)
    return "image/jpeg", out.getvalue()


def _ask(mime, data, prompt, max_tokens):
    url = f"data:{mime};base64," + base64.b64encode(data).decode()
    reply = llm.chat([{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": prompt}],
                     max_tokens=max_tokens, timeout=180)
    return llm.first_json(reply) or {}


def _cents(v):
    try:
        return round(float(str(v).replace(",", "").replace("S$", "").replace("$", "")) * 100)
    except (TypeError, ValueError):
        return None


def read(data, day):
    """The model's two reads of one screenshot. -> the first read, with each row's "cents". Raises Refused."""
    if not llm.key():
        raise Refused("No model is set up in the app, so screenshots can't be read. Ask the app's admin to set one up "
                      "on the App page, then move the file back here.")
    mime, data = _image(data)
    try:
        first = _ask(mime, data, READ.format(day=day.isoformat(), weekday=day.strftime("%A")), 4000)
        second = _ask(mime, data, AMOUNTS, 1000)
    except Exception as ex:
        print(f"screenshot read failed: {type(ex).__name__}", flush=True)
        raise Refused("The model didn't answer. The file will be tried again if you move it back here later.")
    if not first.get("transactions"):
        raise Refused("This doesn't look like a list of card transactions. Take the screenshot on the card's "
                      "transaction list in the bank's app.")
    rows = [r for r in first.get("rows") or [] if isinstance(r, dict)]
    for r in rows:
        r["cents"] = _cents(r.get("amount"))
    again = [_cents(a) for a in second.get("amounts") or []]
    if not rows or None in again or Counter(r["cents"] for r in rows) != Counter(again):
        raise Refused("The screenshot was read twice and the two reads didn't agree on the amounts, so nothing was "
                      "added. A sharper or less crowded screenshot usually helps; drop it here again.")
    first["rows"] = rows
    return first


def card_for(st, said, filename):
    """-> card id. The last four digits on the screenshot, else four digits in the file name. Raises Refused."""
    known = st.get("last4") or {}
    said = re.sub(r"\D", "", str(said or ""))[-4:]
    if said in known:
        return known[said]
    stem = os.path.splitext(os.path.basename(filename))[0]
    for d in re.findall(r"(?<!\d)(\d{4})(?!\d)", stem):
        if d in known:
            return known[d]
    if said:
        raise Refused(f"The screenshot shows a card ending {said}, and none of your cards in Settings ends with those "
                      "digits. Add them to the card in Settings, then move the file back here.")
    raise Refused("The screenshot doesn't show which card this is. Rename the file to include the card's last four "
                  "digits (for example \"UOB 1234.png\") and drop it here again.")


def words(text):
    """The words of a shop's name that tell it apart: no company endings, and no place names (two shops in one city
    are not one shop). Pure."""
    from . import pipeline
    return {w for w in re.findall(r"[A-Z]{3,}", str(text or "").upper())
            if w not in FILLER and not pipeline.FOREIGN_PLACE.fullmatch(w)}


def _near(a, b):
    """Two amounts in cents on the same side, within 4% of the larger or S$2, whichever is more."""
    if a is None or b is None or (a < 0) != (b < 0):
        return False
    return abs(a - b) <= max(round(NEAR_SHARE * max(abs(a), abs(b))), NEAR_MIN)


def claim(rows, held, days=DAYS):
    """Pair rows with held lines, each held line at most once, closest date first:
      1. the same amount in cents (so the same side), dated at most `days` apart;
      2. then, for a row and a held line that both carry the shop's words (a third item) and share one: amounts within
         4% (at least S$2) on the same side, dated at most 5 days apart.
    rows, held: lists of (date, cents) or (date, cents, words). -> for each row, the index of its held line or None. Pure."""
    got, used = [None] * len(rows), set()

    def take_pairs(pairs):
        for *_, i, j in sorted(pairs):
            if got[i] is None and j not in used:
                got[i] = j
                used.add(j)

    take_pairs((abs((r[0] - h[0]).days), i, j) for i, r in enumerate(rows) for j, h in enumerate(held)
               if r[1] == h[1] and abs((r[0] - h[0]).days) <= days)
    take_pairs((abs((r[0] - h[0]).days), abs(r[1] - h[1]), i, j) for i, r in enumerate(rows) for j, h in enumerate(held)
               if got[i] is None and j not in used and len(r) > 2 and len(h) > 2 and r[2] & h[2]
               and _near(r[1], h[1]) and abs((r[0] - h[0]).days) <= NEAR_DAYS)
    return got


def held(conn, person, card, lo, hi, section=None):
    """The person's stored lines on a card between two dates, every source (or one section):
    dicts with id, section, date, cents, words, data."""
    sql = ("SELECT id, section, txn_date, data_enc FROM stmt_lines WHERE person = %s AND card_id = %s AND txn_date "
           "BETWEEN %s AND %s" + (" AND section = %s" if section else "") + " ORDER BY txn_date, id")
    args = [person, card, lo, hi] + ([section] if section else [])
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = crypto.dec_json(conn, r["data_enc"], f"stmt_lines.data_enc:{r['id']}")
        c = _cents(d.get("amount"))
        if c is not None:
            out.append({"id": r["id"], "section": r["section"], "date": r["txn_date"], "cents": c,
                        "words": words(d.get("merchant")), "data": d})
    return out


def _as_pairs(lines):
    return [(x["date"], x["cents"], x["words"]) for x in lines]


def take(conn, person, st, data, filename, day):
    """One screenshot: read, check, match, store the new rows. -> (counts, receipt lines). Raises Refused."""
    from . import ingest
    got = read(data, day)
    card = card_for(st, got.get("last4"), filename)
    keep, notes = [], []
    for r in got["rows"]:
        dt = statements.to_date(r.get("date"))
        cur = str(r.get("currency") or "SGD").upper()[:3]
        what = f"{dt.isoformat() if dt else '?'}  {str(r.get('description') or '')[:60]}  {r['cents'] / 100:.2f}"
        why = ("pending, not charged yet" if r.get("pending") else f"in {cur}, not S$" if cur != "SGD" else
               "no date" if not dt else "dated after the screenshot" if dt > day else
               f"more than {OLDEST} days old" if dt < day - datetime.timedelta(days=OLDEST) else "")
        if why:
            notes.append(f"skipped        {what}  ({why})")
        else:
            keep.append((dt, r, what))
    counts = Counter()
    new = []
    if keep:
        lo, hi = min(k[0] for k in keep), max(k[0] for k in keep)
        have = held(conn, person, card, lo - datetime.timedelta(days=NEAR_DAYS), hi + datetime.timedelta(days=NEAR_DAYS))
        mine = [(k[0], k[1]["cents"], words(k[1].get("description"))) for k in keep]
        for (dt, r, what), m in zip(keep, claim(mine, _as_pairs(have))):
            if m is None:
                new.append({"date": dt, "card": card, "merchant": str(r.get("description") or "")[:120],
                            "amount": r["cents"] / 100, "currency": "SGD", "fcy": False, "online": None, "est": False,
                            "lid": uuid.uuid4().hex})
                notes.append(f"added          {what}")
            else:
                h = have[m]
                if h["cents"] != r["cents"] and h["section"] == "alert":   # the alert's amount before the fee
                    d = dict(h["data"], amount=r["cents"] / 100, est=False)
                    conn.execute("UPDATE stmt_lines SET data_enc = %s WHERE id = %s",
                                 (crypto.enc_json(conn, d, f"stmt_lines.data_enc:{h['id']}"), h["id"]))
                    notes.append(f"already there  {what}  (the alert said {h['cents'] / 100:.2f}; now {r['cents'] / 100:.2f})")
                    counts["screenshot amounts corrected"] += 1
                else:
                    notes.append(f"already there  {what}")
                counts["screenshot rows already there"] += 1
    counts["screenshot rows added"] += ingest.store_alerts(conn, person, new, section=SECTION)
    counts["screenshot rows skipped"] += len(got["rows"]) - len(keep)
    return counts, notes


def drop_seen_alerts(conn, person, alerts):
    """Alert lines a screenshot already brought in (same card and side, the same amount within 3 days, or at the same shop
    within 4% and 5 days: claim) are left out, so it does not matter whether the screenshot or the mail came first. The
    screenshot's amount is the posted one, so it stays. -> (alerts to store, how many were left out)."""
    keep, dropped = [], 0
    by_card = {}
    for a in alerts:
        by_card.setdefault(a["card"], []).append(a)
    for card, mine in by_card.items():
        lo = min(a["date"] for a in mine) - datetime.timedelta(days=NEAR_DAYS)
        hi = max(a["date"] for a in mine) + datetime.timedelta(days=NEAR_DAYS)
        shots = held(conn, person, card, lo, hi, section=SECTION)
        alerts_ = [(a["date"], _cents(a["amount"]), words(a.get("merchant"))) for a in mine]
        for a, m in zip(mine, claim(alerts_, _as_pairs(shots))):
            if m is None:
                keep.append(a)
            else:
                dropped += 1
    return keep, dropped
