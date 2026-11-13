#!/usr/bin/env python3
"""Fill a throwaway sg-card-advisor with an invented household, for the README screenshots.

Everything goes in through the app's own pages and its feeder API, the way a person and a feeder would do it: nothing
is written to the database directly. All of it is made up: "Alex" and "Sam", card numbers like 1234, shops any
Singapore card statement would have, round-ish amounts. The cards themselves come from the app's public catalogue.

It refuses to run unless all of these hold, so demo rows cannot land in a real installation by accident:
  - --throwaway is given;
  - the address is this machine (127.0.0.1, localhost or [::1]);
  - the app runs with CARDS_SINGLE_USER (no login) and that person holds no cards yet.

    python tools/demo/seed.py --url http://127.0.0.1:18080 --throwaway

Standard library only. Not part of the app and not in the Docker image."""
import argparse
import calendar
import datetime
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# card id -> invented last 4 digits
CARDS = {"dbs_womens_world": "1234", "uob_lady_solitaire": "5678", "uob_preferred_platinum": "4321",
         "uob_visa_signature": "2468", "citibank_premiermiles": "1357", "uob_krisflyer_cc": "8642"}
WWMC, LADY, PPV, VS, PM, KF = CARDS

# shop -> (MCC, how it is sold). Also added to the app's shop list, so the Pay page knows them.
SHOPS = {
    "FairPrice": ("5411", "in_person"), "Cold Storage": ("5411", "in_person"), "Sheng Siong": ("5411", "in_person"),
    "Guardian": ("5912", "in_person"), "Watsons": ("5912", "in_person"), "Uniqlo": ("5651", "in_person"),
    "Decathlon": ("5941", "in_person"), "IKEA": ("5712", "in_person"), "Bus/MRT": ("4111", "in_person"),
    "Din Tai Fung": ("5812", "in_person"), "Crystal Jade": ("5812", "in_person"), "Jumbo Seafood": ("5812", "in_person"),
    "Ya Kun Kaya Toast": ("5814", "in_person"), "Toast Box": ("5814", "in_person"), "Starbucks": ("5814", "in_person"),
    "Grab": ("4121", "online"), "ComfortDelGro Taxi": ("4121", "in_person"), "Esso": ("5541", "in_person"),
    "Shopee": ("5311", "online"), "Lazada": ("5311", "online"), "Amazon SG": ("5311", "online"),
    "Zalora": ("5651", "online"), "Foodpanda": ("5814", "online"), "Klook": ("4722", "online"),
    "Netflix": ("4899", "online"), "Singapore Airlines": ("3075", "online"), "Scoot": ("3075", "online"),
    "Smile Dental Clinic": ("8021", "in_person"), "Vet Clinic": ("0742", "in_person"), "Singtel": ("4814", "online"),
    "SP Services": ("4900", "online"), "Town Council": ("9399", "online"),
    "Hotel Shinjuku Tokyo": ("7011", "in_person"), "Ramen Shibuya Tokyo": ("5812", "in_person"),
    "Don Quijote Tokyo": ("5311", "in_person"), "Bangkok Night Market": ("5812", "in_person"),
}

# (how far into the month 0..1, shop, S$, card[, currency]). The open month: one online cap nearly used, a dining cap
# most of the way, a minimum spend not met yet, and bills that earn nothing.
THIS_MONTH = [
    (0.02, "Shopee", 268.40, WWMC), (0.10, "Lazada", 189.90, WWMC), (0.22, "Amazon SG", 142.35, WWMC),
    (0.35, "Zalora", 154.80, WWMC), (0.48, "Klook", 96.00, WWMC), (0.62, "Foodpanda", 38.60, WWMC),
    (0.80, "Netflix", 19.98, WWMC),
    (0.05, "Din Tai Fung", 86.40, LADY), (0.15, "Ya Kun Kaya Toast", 12.80, LADY), (0.28, "Jumbo Seafood", 248.60, LADY),
    (0.40, "Toast Box", 9.60, LADY), (0.55, "Crystal Jade", 132.50, LADY), (0.70, "Starbucks", 15.30, LADY),
    (0.90, "Din Tai Fung", 104.20, LADY),
    (0.08, "Grab", 18.40, LADY), (0.33, "Grab", 23.10, LADY), (0.58, "ComfortDelGro Taxi", 27.30, LADY),
    (0.85, "Grab", 31.60, LADY),
    (0.04, "FairPrice", 86.35, PPV), (0.18, "Cold Storage", 64.20, PPV), (0.30, "Guardian", 28.90, PPV),
    (0.45, "Uniqlo", 119.80, PPV), (0.52, "Bus/MRT", 14.36, PPV), (0.66, "FairPrice", 73.15, PPV),
    (0.78, "Watsons", 23.40, PPV), (0.95, "Sheng Siong", 41.70, PPV),
    (0.25, "Hotel Shinjuku Tokyo", 412.60, VS, "JPY"), (0.27, "Ramen Shibuya Tokyo", 36.80, VS, "JPY"),
    (0.29, "Don Quijote Tokyo", 148.30, VS, "JPY"), (0.75, "Esso", 72.40, VS),
    (0.12, "Smile Dental Clinic", 185.00, PM), (0.38, "IKEA", 236.90, PM), (0.50, "Singtel", 68.40, PM),
    (0.60, "SP Services", 142.75, PM), (0.88, "Town Council", 76.50, PM),
    (0.20, "Singapore Airlines", 684.30, KF), (0.72, "Scoot", 212.00, KF),
]
# Earlier months, repeated with slightly different amounts. A few purchases are on the wrong card on purpose, so the
# Spend page has something to say.
EARLIER = [
    (0.03, "Shopee", 312.50, WWMC), (0.15, "Lazada", 226.00, WWMC), (0.30, "Amazon SG", 178.45, WWMC),
    (0.50, "Zalora", 139.00, WWMC), (0.68, "Foodpanda", 42.30, WWMC), (0.80, "Netflix", 19.98, WWMC),
    (0.92, "Shopee", 164.90, PM),
    (0.06, "Din Tai Fung", 92.70, LADY), (0.20, "Jumbo Seafood", 216.40, LADY), (0.36, "Toast Box", 11.20, LADY),
    (0.52, "Crystal Jade", 148.60, LADY), (0.74, "Starbucks", 14.10, LADY), (0.88, "Ya Kun Kaya Toast", 13.40, KF),
    (0.10, "Grab", 21.50, LADY), (0.42, "Grab", 17.80, LADY), (0.66, "ComfortDelGro Taxi", 24.60, LADY),
    (0.05, "FairPrice", 94.20, PPV), (0.22, "Cold Storage", 71.85, PM), (0.34, "Guardian", 31.60, PPV),
    (0.48, "Decathlon", 88.00, PPV), (0.56, "Bus/MRT", 46.12, PPV), (0.70, "FairPrice", 68.90, PPV),
    (0.84, "Watsons", 19.90, PPV), (0.96, "Sheng Siong", 52.35, PPV),
    (0.40, "Bangkok Night Market", 64.50, VS, "THB"), (0.44, "Hotel Shinjuku Tokyo", 688.00, VS, "JPY"),
    (0.46, "Don Quijote Tokyo", 205.40, VS, "JPY"), (0.60, "Esso", 78.20, VS), (0.90, "Esso", 69.50, VS),
    (0.14, "Vet Clinic", 96.00, PM), (0.38, "IKEA", 129.00, PM), (0.50, "Singtel", 68.40, PM),
    (0.62, "SP Services", 156.30, PM), (0.86, "Town Council", 76.50, PM),
    (0.26, "Singapore Airlines", 1248.60, KF), (0.78, "Scoot", 186.00, KF),
]
MONTHS_BACK = 5
VARY = [1.0, 0.93, 1.08, 0.97, 1.04, 0.9]

POINTS = [("KF", 48200), ("UNI", 61350), ("DBS_POINTS", 14800), ("CITI_MILES", 22640)]
GOALS = [("HND", "J", "return", 2), ("DPS", "Y", "return", 2), ("LHR", "J", "oneway", 2)]


class App:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=30) as r:
            return r.read().decode()

    def post(self, path, fields=None, body=None, token=None):
        """A form post (fields: [(name, value)]) or a JSON post (body). Redirects are followed."""
        if body is not None:
            data, ctype = json.dumps(body).encode(), "application/json"
        else:
            data, ctype = urllib.parse.urlencode(fields or []).encode(), "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.url + path, data=data, method="POST", headers={"Content-Type": ctype})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode()


def month_start(today, back):
    y, m = today.year, today.month - back
    while m < 1:
        y, m = y - 1, m + 12
    return datetime.date(y, m, 1)


def lines(today):
    out = []
    for back in range(MONTHS_BACK + 1):
        first = month_start(today, back)
        last = today.day if back == 0 else calendar.monthrange(first.year, first.month)[1]
        for i, row in enumerate(THIS_MONTH if back == 0 else EARLIER):
            frac, shop, amount, card = row[:4]
            mcc, sold = SHOPS[shop]
            cur = row[4] if len(row) > 4 else "SGD"
            line = {"id": f"demo-{first:%Y%m}-{i}", "date": first.replace(day=1 + round(frac * (last - 1))).isoformat(),
                    "merchant": shop, "amount": round(amount * (1 if back == 0 else VARY[back % len(VARY)]), 2),
                    "card": card, "mcc": mcc, "currency": cur, "fcy": cur != "SGD"}
            if sold == "online":
                line["channel"] = "online"
            elif cur != "SGD":
                line["channel"] = "insert"   # a chip payment abroad
            out.append(line)
    return out


def fees(today):
    """One fee the bank took back last year and one charged a few weeks ago that nobody has asked about yet."""
    ago = lambda days: (today - datetime.timedelta(days=days)).isoformat()
    return [{"card": LADY, "date": ago(215), "amount": 414.20, "text": "ANNUAL FEE"},
            {"card": LADY, "date": ago(196), "amount": -414.20, "text": "ANNUAL FEE REVERSAL"},
            {"card": PM, "date": ago(24), "amount": 196.20, "text": "ANNUAL FEE"}]


def check(app, args):
    host = urllib.parse.urlsplit(app.url).hostname or ""
    if not args.throwaway:
        sys.exit("Refused: this writes invented people, cards and transactions. Point it at a throwaway installation "
                 "and say so with --throwaway.")
    if host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit(f"Refused: {host} is not this machine. The demo data only goes into an app on 127.0.0.1.")
    try:
        page = app.get("/settings")
    except urllib.error.HTTPError as ex:
        sys.exit(f"Refused: the app answered {ex.code}. It must run with CARDS_SINGLE_USER (one person, no login).")
    except OSError as ex:
        sys.exit(f"No app at {app.url} ({type(ex).__name__}).")
    held = re.search(r"My cards \((\d+)\)", page)
    if not held or held.group(1) != "0":
        sys.exit("Refused: this person already holds cards, so this is not a fresh throwaway installation.")


def main():
    ap = argparse.ArgumentParser(description="Load invented demo data into a throwaway sg-card-advisor.")
    ap.add_argument("--url", default="http://127.0.0.1:18080")
    ap.add_argument("--throwaway", action="store_true", help="yes, this installation holds nothing worth keeping")
    args = ap.parse_args()
    app = App(args.url)
    check(app, args)
    today = datetime.date.today()

    app.post("/admin/person", [("action", "add"), ("id", "sam"), ("display", "Sam")])
    app.post("/settings/cards", [("card", c) for c in CARDS])
    app.post("/settings/pay", [("method", "mobile")])
    app.post("/settings/ingest", [(f"l4_{c}", l4) for c, l4 in CARDS.items()])
    for name, (mcc, sold) in SHOPS.items():
        app.post("/merchant", [("name", name), ("mcc", mcc), ("channel", sold), ("action", "save")])

    made = re.search(r"shown this once:<br><code[^>]*>([^<]+)</code>", app.post("/settings/token", [("kind", "feed"), ("action", "new")]))
    if not made:
        sys.exit("The app did not hand out a feeder token.")
    sent = lines(today)
    got = json.loads(app.post("/api/feed", body={"lines": sent, "fees": fees(today)}, token=html.unescape(made.group(1))))

    for programme, balance in POINTS:
        app.post("/points", [("programme", programme), ("balance", str(balance)), ("date", today.replace(day=1).isoformat())])
    for dest, cabin, trip, pax in GOALS:
        app.post("/goals", [("dest", dest), ("cabin", cabin), ("trip", trip), ("pax", str(pax))])
    print(f"demo data loaded: {len(CARDS)} cards, {got['added']} of {len(sent)} purchases, {len(SHOPS)} shops, "
          f"{len(POINTS)} balances, {len(GOALS)} flights")


if __name__ == "__main__":
    main()
