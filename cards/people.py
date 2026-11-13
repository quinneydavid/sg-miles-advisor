"""Who uses the app. People live in the database and are managed by an admin on the App page (setupweb.py); the
first person is whoever signs in first (access.py). What the environment still does:

  CARDS_SINGLE_USER        one person id that every request on the internal door is treated as, with no login at all.
                           For a private machine only: never set it on a deployment other people can reach.
  CARDS_HOSTS              with CARDS_SINGLE_USER: further names the app may be opened under, comma-separated
                           (access.host_ok). 127.0.0.1, localhost and [::1] always work.
  CARDS_PEOPLE             optional: "alice:Alice,bob:Bob" (person id = the name a proxy sends as Remote-User, lower
                           case letters, digits, _). `seed` creates these rows if they are missing.
  CARDS_FEED_TOKEN_<ID>    optional: a feeder's Bearer token for that person, for deployments that keep tokens in a
  CARDS_BOT_TOKEN_<ID>     secret store. Tokens are normally made on the Settings page (config.py).
"""
import hmac
import os
import re

ID = re.compile(r"[a-z0-9_]{1,32}")


def configured(env=None):
    """[(id, display)] from CARDS_PEOPLE; a lone CARDS_SINGLE_USER counts as a one-person list."""
    env = os.environ if env is None else env
    out = []
    for part in (env.get("CARDS_PEOPLE") or "").split(","):
        pid, _, name = part.strip().partition(":")
        pid = pid.strip().lower()
        if ID.fullmatch(pid) and pid not in [p for p, _ in out]:
            out.append((pid, name.strip() or pid.capitalize()))
    single = single_user(env)
    if single and single not in [p for p, _ in out]:
        out.append((single, single.capitalize()))
    return out


def single_user(env=None):
    env = os.environ if env is None else env
    pid = (env.get("CARDS_SINGLE_USER") or "").strip().lower()
    return pid if ID.fullmatch(pid) else None


def token_person(headers, prefix, env=None):
    """Bearer token -> person id, by the variable <prefix><ID> that holds the same token. None if no match."""
    env = os.environ if env is None else env
    auth = headers.get("Authorization") or ""
    tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
    found = None
    for var, want in env.items():
        pid = var[len(prefix):].lower() if var.startswith(prefix) else ""
        if tok and want and ID.fullmatch(pid) and hmac.compare_digest(tok.encode(), want.encode()):
            found = pid
    return found
