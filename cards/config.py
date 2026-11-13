"""Settings kept in the app (table app_settings), so a deployment needs no environment beyond what must exist before
the database can be opened and read: the database connection, the key source, the ports and the sign-in door.

Two scopes: '' = the whole app (set by an admin on the App page), a person id = that person's own (Settings page).
Secrets are encrypted with the data keys (crypto.py) and never leave the server again: pages show "set" or "not set".

Each setting may also name an environment variable. It is only a starting value: used while nothing is stored in the
app, ignored once something is. That keeps an older deployment working until its values are saved in the app
(`python -m cards import-env` copies them in one go)."""
import hashlib
import hmac
import os
import secrets
import threading
import time

from . import crypto

# key: (environment variable, default, secret)
APP = {
    "llm_url": ("LLM_URL", "", False),             # full URL of an OpenAI-compatible chat completions endpoint
    "llm_model": ("LLM_MODEL", "", False),
    "llm_key": ("LLM_API_KEY", "", True),
    "llm_min_gap": ("LLM_MIN_GAP", "2.2", False),  # seconds between calls
    "llm_direct": ("LLM_DIRECT", "0", False),      # 1 = vLLM without a gateway: thinking switched off in the request
    "telegram_token": ("TELEGRAM_BOT_TOKEN", "", True),
    "telegram_chat": ("TELEGRAM_CHAT_ID", "", False),
    "notify": ("NOTIFY", "0", False),              # 1 = send the summary and alerts; 0 = preview only
    "notify_person": ("NOTIFY_PERSON", "", False),
    "searx_url": ("SEARX_URL", "", False),         # SearXNG /search endpoint for card pictures
    "catalogue_ua": ("CATALOGUE_UA", "", False),
    "cardup_fee": ("CARDUP_FEE", "0.0245", False),
    "tc_pause": ("TC_PAUSE", "2.0", False),        # seconds between requests to bank sites
    "tc_limit": ("TC_LIMIT", "400", False),        # bank documents fetched per run
    "tc_model": ("TC_MODEL", "0", False),          # 1 = also ask the model about bank documents (experimental)
    # optional outside services (App settings); without a key the feature is simply off
    "football_key": ("FOOTBALL_DATA_API_KEY", "", True),   # football-data.org: match results for match-day card rates
    "fixtures_at": (None, "", False),              # when match results were last fetched
    "seats_key": ("SEATSAERO_API_KEY", "", True),  # seats.aero partner key: dates with award seats from Singapore
    "seats_at": (None, "", False),                 # when the seat scan last ran, and what it found (JSON)
}
# per person; the environment form (mailbox only) is CARDS_IMAP_<ID>_<suffix>
PERSON = {
    "imap_provider": (None, "", False),
    "imap_host": ("HOST", "", False),
    "imap_port": ("PORT", "993", False),
    "imap_user": ("USER", "", False),
    "imap_password": ("PASSWORD", "", True),
    "imap_folder": ("FOLDER", "", False),
    "imap_skip_inbox": (None, "0", False),         # only shapes the downloaded filter file
    "ingest_last": (None, "", False),              # JSON: when the last ingest ran and its counts
    # Wealthfolio (wealthfolio.py): the person's own instance, read-only
    "wf_url": (None, "", False),                   # the address Wealthfolio is opened at
    "wf_token": (None, "", True),                  # personal access token (accounts:read, activities:read)
    "wf_on": (None, "0", False),                   # 1 = the scheduled import runs for this person
    "wf_days": (None, "62", False),                # the rolling window that is re-read on each run
    "wf_map": (None, "", False),                   # JSON {Wealthfolio account id: card id}
    "wf_accounts": (None, "", False),              # JSON [{id, name, type}] as last listed (names only)
    "wf_last": (None, "", False),                  # JSON: when the last import ran, its counts or what went wrong
    "wf_boost": (None, "", False),                 # JSON {"account": Wealthfolio account id, "boost": card boost id}
    "wf_hist": (None, "", False),                  # JSON: the history import's progress and result (counts, months)
    # AwardWallet (points.py): the KrisFlyer account balance, read with the person's own Business API key
    "aw_key": (None, "", True),
    "aw_user": (None, "", False),                  # the connected user id the key may read
    "aw_last": (None, "", False),                  # JSON: when it was last asked and what went wrong, if anything
}
TOKENS = ("feed", "bot")                           # per person API tokens, stored as SHA-256 only


def _spec(scope, key):
    return (PERSON if scope else APP)[key]


def _env(scope, key):
    var = _spec(scope, key)[0]
    if not var:
        return ""
    return os.environ.get(f"CARDS_IMAP_{scope.upper()}_{var}" if scope else var) or ""


def _aad(scope, key):
    return f"app_settings.secret:{scope}:{key}"


def stored(conn, scope=""):
    """{key: {"value": text or None, "secret": bytes or None}} as saved in the app."""
    rows = conn.execute("SELECT key, value, secret FROM app_settings WHERE scope = %s", (scope,)).fetchall()
    return {r["key"]: r for r in rows}


def load(conn, scope=""):
    """Every setting of the scope as text: what is stored, else the environment's starting value, else the default.
    Secrets are decrypted; with encryption unavailable a stored secret reads as empty."""
    have, out = stored(conn, scope), {}
    for key, (_, default, secret) in (PERSON if scope else APP).items():
        row = have.get(key)
        if secret:
            val = ""
            if row and row["secret"]:
                try:
                    val = crypto.decrypt(conn, row["secret"], _aad(scope, key)).decode()
                except crypto.Unavailable:
                    val = ""
            out[key] = val or (_env(scope, key) if not row else "")
        else:
            out[key] = ((row["value"] or default) if row else (_env(scope, key) or default))
    return out


def where(conn, scope=""):
    """{secret key: "app" | "env" | ""}: where each secret comes from, for pages that must not show it."""
    have = stored(conn, scope)
    return {k: ("app" if k in have and have[k]["secret"] else "env" if k not in have and _env(scope, k) else "")
            for k, spec in (PERSON if scope else APP).items() if spec[2]}


def put(conn, key, value, scope=""):
    """Save one setting. Secrets are encrypted (raises crypto.Unavailable when that cannot be done)."""
    value = "" if value is None else str(value).strip()
    if _spec(scope, key)[2]:
        blob = crypto.encrypt(conn, value.encode(), _aad(scope, key)) if value else None
        args = (scope, key, None, blob)
    else:
        args = (scope, key, value, None)
    conn.execute("INSERT INTO app_settings (scope, key, value, secret) VALUES (%s,%s,%s,%s) ON CONFLICT (scope, key) "
                 "DO UPDATE SET value = EXCLUDED.value, secret = EXCLUDED.secret, updated_at = now()", args)
    invalidate()


def forget(conn, key, scope=""):
    """Back to the starting value (environment or default)."""
    conn.execute("DELETE FROM app_settings WHERE scope = %s AND key = %s", (scope, key))
    invalidate()


def save_form(conn, form, keys, scope=""):
    """Save the named settings from a posted form. A blank secret field keeps what is stored; the tick box
    forget_<key> removes it. Unticked switches arrive as missing fields and are saved as "0"."""
    for key in keys:
        secret = _spec(scope, key)[2]
        val = (form.get(key) or [""])[0].strip()
        if secret:
            if form.get("forget_" + key):
                put(conn, key, "", scope)
            elif val:
                put(conn, key, val, scope)
        else:
            put(conn, key, val if key in form else ("0" if _spec(scope, key)[1] in ("0", "1") else ""), scope)


# ---- app-wide settings for code that has no connection at hand (the model, the catalogue run, messages) ----
_lock = threading.Lock()
_cache = {"at": 0.0, "values": None}
TTL = 30.0


def invalidate():
    _cache["at"] = 0.0


def _fresh():
    with _lock:
        if _cache["values"] is not None and time.monotonic() - _cache["at"] < TTL:
            return _cache["values"]
        try:
            from . import db
            with db.connect() as conn:
                values = load(conn, "")
        except Exception:   # no database yet (tests, a first start): the environment and the defaults still answer
            values = {k: ((os.environ.get(env) if env else None) or default) for k, (env, default, _) in APP.items()}
        _cache.update(at=time.monotonic(), values=values)
        return values


def app(key):
    """One app-wide setting as text, read at most every 30 seconds."""
    return _fresh()[key]


def user_agent():
    """How the app names itself to outside services (App settings can replace it)."""
    from . import __version__
    return app("catalogue_ua") or f"sg-card-advisor/{__version__} (+https://github.com/quinneydavid/sg-card-advisor)"


def flag(key):
    return app(key) == "1"


def number(key):
    try:
        return float(app(key))
    except ValueError:
        return float(APP[key][1])


# ---- per person API tokens: shown once when made, kept as a hash ----
def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(conn, person, kind):
    """Make (or replace) the person's feed or bot token. Returns it; only its SHA-256 is stored."""
    assert kind in TOKENS
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO app_settings (scope, key, value) VALUES (%s,%s,%s) ON CONFLICT (scope, key) DO UPDATE SET "
                 "value = EXCLUDED.value, secret = NULL, updated_at = now()", (person, f"{kind}_token_sha256", _digest(token)))
    return token


def drop_token(conn, person, kind):
    assert kind in TOKENS
    conn.execute("DELETE FROM app_settings WHERE scope = %s AND key = %s", (person, f"{kind}_token_sha256"))


def has_token(conn, person, kind):
    return conn.execute("SELECT 1 FROM app_settings WHERE scope = %s AND key = %s", (person, f"{kind}_token_sha256")).fetchone() is not None


def token_person(conn, headers, kind):
    """Bearer token -> person id, from the tokens made in the app; else from CARDS_<KIND>_TOKEN_<ID> in the
    environment (people.py). None if it matches nobody."""
    auth = headers.get("Authorization") or ""
    tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not tok:
        return None
    want = _digest(tok)
    for r in conn.execute("SELECT scope, value FROM app_settings WHERE key = %s", (f"{kind}_token_sha256",)).fetchall():
        if r["scope"] and hmac.compare_digest(r["value"] or "", want):
            return r["scope"]
    from . import people
    return people.token_person(headers, f"CARDS_{kind.upper()}_TOKEN_")


# ---- admins ----
def is_admin(conn, person):
    """True for an admin. While nobody is admin, the person asking becomes the first one."""
    if person.get("admin"):
        return True
    made = conn.execute("UPDATE people SET admin = true WHERE id = %s AND NOT EXISTS (SELECT 1 FROM people WHERE admin) "
                        "RETURNING id", (person["id"],)).fetchone()
    if made:
        conn.commit()
    return bool(made)


# ---- one-time move of an older deployment's environment into the app ----
def import_env(conn):
    """Store every setting the environment names and the app does not hold yet. Secrets are encrypted on the way in;
    tokens named CARDS_FEED_TOKEN_<ID> / CARDS_BOT_TOKEN_<ID> keep working and are stored as fingerprints. Returns
    the lines to print: names only, never values."""
    done = []
    people = [r["id"] for r in conn.execute("SELECT id FROM people ORDER BY id").fetchall()]
    for scope in [""] + people:
        have = stored(conn, scope)
        for key in (PERSON if scope else APP):
            if key not in have and _env(scope, key):
                put(conn, key, _env(scope, key), scope)
                done.append(f"{scope or 'app'}: {key} stored")
        if scope and "imap_provider" not in have and "imap_host" not in have and _env(scope, "imap_host"):
            put(conn, "imap_provider", "gmail" if "gmail" in _env(scope, "imap_host") else "other", scope)
    for pid in people:
        for kind in TOKENS:
            tok = os.environ.get(f"CARDS_{kind.upper()}_TOKEN_{pid.upper()}")
            if tok and not has_token(conn, pid, kind):
                conn.execute("INSERT INTO app_settings (scope, key, value) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                             (pid, f"{kind}_token_sha256", _digest(tok)))
                done.append(f"{pid}: {kind} token fingerprint stored")
    conn.commit()
    return done or ["nothing to import: the app already holds every setting the environment names"]
