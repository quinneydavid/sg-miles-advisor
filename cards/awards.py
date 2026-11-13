"""KrisFlyer award prices from Singapore, from Singapore Airlines' own one-way chart (data/krisflyer-chart.json).

Saver and Advantage are fixed per zone and cabin. Access has no chart: SIA prices it by demand when you book, so the app
only shows the highest price a seat scan has seen, when that is above Advantage.

fill() puts the chart's prices in the table the flight goals read, for every airport the chart places in a zone. The
weekly catalogue run and the first start call it, so goals work with no feeder. The chart is a file shipped with
the app (read from SIA's PDF by hand: the date is in the file); the catalogue run only checks that SIA still serves
that PDF and says so when it does not, which is the sign that a new chart is out.

Dates with seats are optional (seats_run): with a seats.aero partner key (App settings) the weekly catalogue run asks
seats.aero which dates from Singapore have award seats in the next year and keeps, per destination and cabin, the
lowest and highest price seen, how many dates had seats and the next such date. Only the key and dates go out. seats.aero
re-checks far dates rarely, so the dates are a hint; the prices in the chart are the reliable part. A feeder may send
the same thing (POST /api/feed, "awards"). A row a scan filled in the last 30 days is not overwritten by the chart."""
import datetime
import functools
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

SEATS_API = "https://seats.aero/partnerapi"
SEATS_PAGES = 40            # 1,000 rows a page
SEATS_PAUSE = 1.0           # seconds between pages
SEATS_FRESH_DAYS = 30

LEVELS = (("saver", "Saver"), ("advantage", "Advantage"))


@functools.lru_cache(maxsize=1)
def chart():
    return json.loads((pathlib.Path(__file__).parent / "data" / "krisflyer-chart.json").read_text())


def prices(dest, cabin):
    """{zone, zone_name, saver, advantage} one-way from Singapore (miles or None), or None for an airport we can't
    place in SIA's zones."""
    c = chart()
    z = c["airport_zone"].get(dest)
    if not z:
        return None
    z = str(z)
    return {"zone": int(z), "zone_name": c["zones"][z],
            "saver": c["from_sin"]["saver"].get(cabin, {}).get(z), "advantage": c["from_sin"]["advantage"].get(cabin, {}).get(z)}


def access_seen(chart_price, highest_seen):
    """The highest one-way price the seat scan saw, when it is above the chart's Advantage (so it was an Access fare)."""
    top = (chart_price or {}).get("advantage") or (chart_price or {}).get("saver")
    return highest_seen if highest_seen and top and highest_seen > top else None


@functools.lru_cache(maxsize=1)
def cities():
    return json.loads((pathlib.Path(__file__).parent / "data" / "airports.json").read_text())


def fill(conn):
    """Chart prices -> award_prices, for every airport and cabin the chart prices. A row a feeder refreshed in the last
    30 days (it carries dates with seats) is not touched. -> rows written."""
    c, n = chart(), 0
    for dest, zone in c["airport_zone"].items():
        for cabin in ("Y", "W", "J", "F"):
            p = prices(dest, cabin)
            if not p or not p["saver"]:
                continue
            cur = conn.execute(
                "INSERT INTO award_prices (dest, cabin, city, region, saver, advantage, updated_at) VALUES (%s,%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT (dest, cabin) DO UPDATE SET city = coalesce(award_prices.city, EXCLUDED.city), region = "
                "coalesce(award_prices.region, EXCLUDED.region), saver = EXCLUDED.saver, advantage = EXCLUDED.advantage "
                "WHERE award_prices.seen_dates IS NULL OR award_prices.updated_at < now() - interval '30 days'",
                (dest, cabin, cities().get(dest) or dest, p["zone_name"], p["saver"], p["advantage"]))
            n += cur.rowcount or 0
    return n


def run_chart(conn, stats, today):
    """The catalogue run's step: fill the prices, and check that SIA still serves the chart the file was read from."""
    from . import catalogue
    stats["award prices"] = fill(conn)
    conn.commit()
    try:
        status, _ = catalogue.fetch(chart()["source"], timeout=60)
    except Exception as ex:
        status = getattr(ex, "code", 0)
    if status == 404:
        stats["award chart no longer at its address (a new chart may be out)"] = 1


# ---------------------------------------------------------------- dates with seats (optional) ----------------------------------------------------------------
def store_scan(conn, destinations):
    """A seat scan's result (seats_summarise, or a feeder's "awards.destinations") -> award_prices. -> rows written."""
    n = 0
    for d in destinations or []:
        for cab, c in (d.get("cabins") or {}).items():
            if cab not in ("Y", "W", "J", "F") or not c.get("saver"):
                continue
            nxt = c.get("next")
            try:
                nxt = datetime.date.fromisoformat(str(nxt)[:10]) if nxt else None
            except ValueError:
                nxt = None
            conn.execute("INSERT INTO award_prices (dest, cabin, city, region, saver, advantage, seen_dates, next_date, updated_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (dest, cabin) DO UPDATE SET city = EXCLUDED.city, "
                         "region = EXCLUDED.region, saver = EXCLUDED.saver, advantage = EXCLUDED.advantage, seen_dates = "
                         "EXCLUDED.seen_dates, next_date = EXCLUDED.next_date, updated_at = now()",
                         (str(d.get("airport"))[:4], cab, d.get("city") or cities().get(str(d.get("airport"))), d.get("region"),
                          int(c["saver"]), int(c["advantage"]) if c.get("advantage") else None, c.get("seen_dates"), nxt))
            n += 1
    return n


def seats_fetch(key, api=None, pages=SEATS_PAGES, pause=SEATS_PAUSE, today=None):
    """seats.aero bulk availability for Singapore Airlines from Asia, the next ~360 days: the rows that leave SIN.
    -> (rows, pages read). Stops early when the key's daily allowance runs low."""
    from . import config
    today = today or datetime.date.today()
    q = {"source": "singapore", "origin_region": "Asia", "start_date": (today + datetime.timedelta(days=1)).isoformat(),
         "end_date": (today + datetime.timedelta(days=360)).isoformat(), "take": 1000}
    rows, cursor, n = [], None, 0
    while n < pages:
        url = f"{(api or SEATS_API).rstrip('/')}/availability?" + urllib.parse.urlencode(dict(q, **({"cursor": cursor} if cursor else {})))
        req = urllib.request.Request(url, headers={"Partner-Authorization": key, "Accept": "application/json",
                                                   "User-Agent": config.user_agent()})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
            left = r.headers.get("X-RateLimit-Remaining")
        n += 1
        rows += [x for x in d.get("data") or [] if (x.get("Route") or {}).get("OriginAirport") == "SIN"]
        cursor = d.get("cursor")
        if not d.get("hasMore") or not cursor or (left and left.isdigit() and int(left) < 200):
            break
        time.sleep(pause)
    return rows, n


def seats_summarise(rows, now=None):
    """Rows -> per destination and cabin: lowest and highest one-way price, dates with seats seen lately, the next one."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    fresh_from = (now - datetime.timedelta(days=SEATS_FRESH_DAYS)).isoformat()
    dest = {}
    for x in rows:
        rt = x.get("Route") or {}
        ap = rt.get("DestinationAirport")
        if not ap:
            continue
        d = dest.setdefault(ap, {"airport": ap, "region": rt.get("DestinationRegion"), "city": cities().get(ap, ""), "cabins": {}})
        recent = (x.get("UpdatedAt") or "") >= fresh_from
        for c in ("Y", "W", "J", "F"):
            cost = int(x.get(f"{c}MileageCost") or 0)
            if cost <= 0:
                continue
            e = d["cabins"].setdefault(c, {"saver": cost, "advantage": cost, "seen_dates": 0, "next": None})
            e["saver"], e["advantage"] = min(e["saver"], cost), max(e["advantage"], cost)
            if x.get(f"{c}Available") and recent and x.get("Date"):
                e["seen_dates"] += 1
                e["next"] = min(filter(None, [e["next"], x["Date"]]))
    for d in dest.values():   # one price seen only: a single tier, not two
        for e in d["cabins"].values():
            if e["advantage"] == e["saver"]:
                e["advantage"] = None
    return list(dest.values())


def seats_test(key, api=None):
    """-> (ok, sentence) for the App settings page: one page only."""
    try:
        rows, _ = seats_fetch(key, api, pages=1, pause=0)
    except urllib.error.HTTPError as ex:
        return False, ("seats.aero refused the key." if ex.code in (401, 403) else f"seats.aero answered {ex.code}.")
    except Exception as ex:
        return False, f"seats.aero could not be reached ({type(ex).__name__})."
    return True, f"Connected: the first page has {len(rows)} dates from Singapore."


def seats_run(conn, api=None, pause=SEATS_PAUSE):
    """The scan, when a key is set. -> {"destinations", "rows", "pages"} or None without a key."""
    from . import config
    key = config.app("seats_key")
    if not key:
        return None
    rows, pages = seats_fetch(key, api, pause=pause)
    dests = seats_summarise(rows)
    store_scan(conn, dests)
    out = {"destinations": len(dests), "rows": len(rows), "pages": pages}
    config.put(conn, "seats_at", json.dumps(dict(out, at=datetime.datetime.now().astimezone().isoformat(timespec="minutes"))))
    conn.commit()
    return out


def run_seats(conn, stats, today):
    """The catalogue run's step (after the chart): dates with seats, when a key is set."""
    got = seats_run(conn)
    if got:
        stats["seat scan destinations"], stats["seat scan dates"] = got["destinations"], got["rows"]
