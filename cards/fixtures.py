"""Match results, for card rules that pay more on the day a team wins or draws.

A card in data/cards.json may name its fixtures: "fixtures": "football-data/<competition>/<team id>". Its rules then
match on {"match_day": "win" | "draw"} and may cap per match day (cap period "match_day") and per month
("max_per_month" on the bucket). The bonus day is the match's start date in the United Kingdom.

Results come from football-data.org (v4, free key: App settings). They are kept in the table match_results and
fetched by the scheduled runs: at most every 3 hours, and every 10 minutes while a match day's result is pending. No key = no results: such a card then earns its base rate, as
before. A match that is scheduled or being played is stored as "pending": a purchase that day is rated at the base
rate until the result is in.
  result: win | draw | loss | pending | other"""
import datetime
import functools
import json
import pathlib
import urllib.parse
import urllib.request
import zoneinfo

from . import config

API = "https://api.football-data.org/v4"
EVERY = 3 * 3600
LIVE_EVERY = 600     # while a match day's result is pending
try:
    UK = zoneinfo.ZoneInfo("Europe/London")
except Exception:   # no tzdata: the UTC date is the UK date for any real kick-off time
    UK = datetime.timezone.utc
SAY = {"win": "a win today: the win-day rate applies", "draw": "a draw today: the draw-day rate applies",
       "loss": "a loss today: base rate", "pending": "match today, result not in yet: base rate until it is",
       "other": "today's match did not finish: base rate", None: "no match today: base rate"}


@functools.lru_cache(maxsize=1)
def declared():
    """({card id: fixtures key}, {(card id, bucket): max bonus days a month}) from the card data file."""
    cards = json.loads((pathlib.Path(__file__).parent / "data" / "cards.json").read_text()).get("cards", {})
    keys, per_month = {}, {}
    for cid, c in cards.items():
        if c.get("fixtures"):
            keys[cid] = c["fixtures"]
            for b in c.get("buckets", []):
                if b.get("max_per_month"):
                    per_month[(cid, b["id"])] = int(b["max_per_month"])
    return keys, per_month


def load(conn):
    """{fixtures key: {date: result}} as stored."""
    out = {}
    try:
        for r in conn.execute("SELECT fixtures, day, result FROM match_results").fetchall():
            out.setdefault(r["fixtures"], {})[r["day"]] = r["result"]
    except Exception:
        conn.rollback()
    return out


def parse(matches, team, competition):
    """football-data matches -> {date: result} for one team."""
    out = {}
    for m in matches or []:
        if (m.get("competition") or {}).get("code") != competition:
            continue
        try:
            day = datetime.datetime.fromisoformat(str(m["utcDate"]).replace("Z", "+00:00")).astimezone(UK).date()
        except (KeyError, ValueError):
            continue
        if m.get("status") != "FINISHED":
            out[day] = "pending" if m.get("status") in ("IN_PLAY", "PAUSED", "TIMED", "SCHEDULED") else "other"
            continue
        home = str((m.get("homeTeam") or {}).get("id")) == str(team)
        ft = (m.get("score") or {}).get("fullTime") or {}
        us, them = (ft.get("home"), ft.get("away")) if home else (ft.get("away"), ft.get("home"))
        if us is None or them is None:
            continue
        out[day] = "win" if us > them else "draw" if us == them else "loss"
    return out


def fetch(key, token, start, end, api=None):
    """One team's matches between two dates -> {date: result}. key = 'football-data/<competition>/<team id>'."""
    provider, competition, team = (key.split("/") + ["", ""])[:3]
    if provider != "football-data":
        raise ValueError(f"unknown fixtures source {provider}")
    q = urllib.parse.urlencode({"competitions": competition, "dateFrom": start.isoformat(), "dateTo": end.isoformat()})
    req = urllib.request.Request(f"{(api or API).rstrip('/')}/teams/{urllib.parse.quote(team)}/matches?{q}",
                                 headers={"X-Auth-Token": token, "User-Agent": config.user_agent()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return parse(json.load(r).get("matches", []), team, competition)


def test(token, api=None):
    """-> (ok, sentence) for the App settings page."""
    keys = sorted(set(declared()[0].values()))
    if not keys:
        return True, "No card in the catalogue file uses match results."
    today = datetime.date.today()
    try:
        got = fetch(keys[0], token, today - datetime.timedelta(days=30), today + datetime.timedelta(days=7), api)
    except urllib.error.HTTPError as ex:
        return False, ("The key was refused." if ex.code in (400, 401, 403) else f"The service answered {ex.code}.")
    except Exception as ex:
        return False, f"The service could not be reached ({type(ex).__name__})."
    return True, f"Connected: {len(got)} match days in the last month and the coming week."


def refresh(conn, today=None, force=False, api=None, back_days=None):
    """Fetch results for every declared fixtures key and store them. At most every 3 hours unless forced.
    The first fetch goes back 400 days (for the history of purchases), later ones 70. -> days stored, or None when
    there was nothing to do (no key, no card, or done recently)."""
    token = config.app("football_key")
    keys = sorted(set(declared()[0].values()))
    if not token or not keys:
        return None
    today = today or datetime.date.today()
    now = datetime.datetime.now(datetime.timezone.utc)
    last = config.app("fixtures_at")
    # a match is on (or was last night and its result is not in): ask every 10 minutes, and only for these days
    live = bool(conn.execute("SELECT 1 FROM match_results WHERE result = 'pending' AND day BETWEEN %s AND %s LIMIT 1",
                             (today - datetime.timedelta(days=1), today + datetime.timedelta(days=1))).fetchone())
    if last and not force:
        try:
            if (now - datetime.datetime.fromisoformat(last)).total_seconds() < (LIVE_EVERY if live else EVERY):
                return None
        except ValueError:
            pass
    first = not conn.execute("SELECT 1 FROM match_results LIMIT 1").fetchone()
    back = back_days if back_days is not None else (400 if first else 3 if live else 70)
    n = 0
    for key in keys:
        end = today + datetime.timedelta(days=8)
        start = today - datetime.timedelta(days=back)
        while start <= end:   # the service limits a query's date span: 60 days at a time
            stop = min(end, start + datetime.timedelta(days=59))
            for day, res in fetch(key, token, start, stop, api).items():
                conn.execute("INSERT INTO match_results (fixtures, day, result) VALUES (%s,%s,%s) ON CONFLICT (fixtures, day) "
                             "DO UPDATE SET result = EXCLUDED.result, updated_at = now() WHERE match_results.result <> EXCLUDED.result",
                             (key, day, res))
                n += 1
            start = stop + datetime.timedelta(days=1)
    config.put(conn, "fixtures_at", now.isoformat(timespec="seconds"))
    conn.commit()
    return n
