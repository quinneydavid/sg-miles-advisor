"""Who is asking? Two doors, two rules, never mixed.

  internal (port 8080)  behind a reverse proxy that signs people in (forward auth: Authelia, Authentik, Pocket ID with
                        Traefik, ...). The proxy sets Remote-User (the person id) and, if it can, Remote-Email. Only
                        the proxy may reach this port: a browser that reaches it directly could send the header itself.
                        CARDS_SINGLE_USER replaces the proxy on a private machine (people.py). With no login the
                        Host header is checked instead (host_ok): a hostile web page that points its own name at
                        127.0.0.1 (DNS rebinding) would otherwise be read as this person.
                        While the app has no people at all, the first person the proxy names is added and becomes
                        the admin; everyone after that is added by an admin on the App page.
  public   (port 8081)  behind a Cloudflare tunnel with Cloudflare Access. cloudflared must be set to refuse requests
                        without a valid Access token for this app (signature + audience checked there), and only
                        cloudflared may reach this port. The person is the token's email. Remote-User is IGNORED
                        here: a browser could send it. Off unless CF_ACCESS_AUD is set.

Emails are not kept in the clear: at an internal login the app stores a keyed hash of the login email on the person's
row, and the public door looks people up by that hash. So each person signs in once on the internal door before the
public door knows them.

The token is not re-verified by signature here (that would need internet egress from a process that holds personal
data, to fetch Cloudflare's keys); audience and expiry are still checked, so a token for another app or an old one is
refused even if the tunnel rule were ever wrong."""
import base64
import json
import os
import re
import time

from . import crypto, people

AUD = os.environ.get("CF_ACCESS_AUD", "")
_learned = {}   # person id -> email hash already stored (per process)


LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_HOST = re.compile(r"(\[[0-9a-f:.]+\]|[^:\[\]/@\s]+)(:\d+)?")


def host_ok(headers, env=None):
    """Internal door, CARDS_SINGLE_USER only: the request must name this machine (127.0.0.1, localhost, [::1], any
    port) or a name listed in CARDS_HOSTS (comma-separated, e.g. the name a proxy in front serves the app under).
    With a signing-in proxy (no CARDS_SINGLE_USER) the proxy decides who gets in and every Host is accepted."""
    env = os.environ if env is None else env
    if not people.single_user(env):
        return True
    m = _HOST.fullmatch((headers.get("Host") or "").strip().lower())
    if not m:
        return False
    names = lambda text: {n.group(1) for n in map(_HOST.fullmatch, (p.strip().lower() for p in text.split(","))) if n}
    return m.group(1) in LOCAL_HOSTS | names(env.get("CARDS_HOSTS") or "")


def _claims(token):
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return None


def email_hash(conn, email):
    return crypto.hmac_hex(conn, "login-email", email.strip().lower().encode())


def public_person(conn, headers):
    """Person row for a request on the public door, or (None, reason)."""
    if not AUD:
        return None, "The public address isn't switched on."
    c = _claims(headers.get("Cf-Access-Jwt-Assertion") or "")
    if not c:
        return None, "No Cloudflare Access sign-in on this request."
    aud = c.get("aud")
    if AUD not in (aud if isinstance(aud, list) else [aud]) or float(c.get("exp") or 0) < time.time():
        return None, "That sign-in is not for this app, or it has expired."
    email = c.get("email")
    if not email:
        return None, "The sign-in carries no email."
    try:
        h = email_hash(conn, email)
    except crypto.Unavailable:
        return None, "The app can't check who you are right now (encryption unavailable). Try again shortly."
    row = conn.execute("SELECT * FROM people WHERE email_hmac = %s", (h,)).fetchone()
    if not row:
        return None, ("This address doesn't know you yet. Sign in once on the internal address; after that this "
                      "one works too.")
    return row, None


def _first_person(conn, user):
    """An empty app takes the first signed-in person as its first person and admin. One statement, so two first
    requests cannot both win."""
    row = conn.execute("INSERT INTO people (id, display, admin) SELECT %s, %s, true WHERE NOT EXISTS (SELECT 1 FROM people) "
                       "RETURNING *", (user, user.capitalize())).fetchone()
    if row:
        conn.commit()
    return row


def internal_person(conn, headers):
    user = people.single_user() or headers.get("Remote-User")
    if not user:
        return None
    user = user.strip().lower()
    row = conn.execute("SELECT * FROM people WHERE id = %s", (user,)).fetchone()
    if not row and people.ID.fullmatch(user):
        row = _first_person(conn, user)
    email = headers.get("Remote-Email")
    if row and email and _learned.get(row["id"]) != email:
        try:   # remember the login email (hashed) for the public door
            h = email_hash(conn, email)
            if row.get("email_hmac") != h:
                conn.execute("UPDATE people SET email_hmac = %s WHERE id = %s", (h, row["id"]))
                conn.commit()
            _learned[row["id"]] = email
        except crypto.Unavailable:
            pass
    return row
