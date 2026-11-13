"""Envelope encryption for personal data.

Data keys (DEKs) are kept WRAPPED in the table encryption_keys and held unwrapped in memory only. Field ciphertext =
b"v<n>:" + nonce(12) + AES-256-GCM(plaintext, AAD = "<table>.<column>:<row key>").

What wraps the data keys is a choice made by environment:

  Vault Transit   VAULT_ADDR set. The transit key (VAULT_TRANSIT_KEY) never leaves Vault. The app logs in with its
                  Kubernetes ServiceAccount (auth mount VAULT_AUTH_MOUNT, role VAULT_ROLE), or uses VAULT_TOKEN.
  local key       CARDS_KEY_FILE (a file holding a base64 32-byte key; made on first start if the database has no
                  keys yet) or CARDS_LOCAL_KEY (the same value in the environment). The key wraps the data keys with
                  AES-256-GCM. Keep it apart from the database backups: either one alone reveals nothing.

No key source -> no personal data: encrypt/decrypt raise Unavailable and the pages say so; there is never a plaintext
fallback."""
import base64
import json
import os
import threading
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SA_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
LOCAL = "local:v1:"
WRAP_AAD = b"cards-data-key"


class Unavailable(Exception):
    pass


_lock = threading.Lock()
_keys = {}        # version -> AESGCM
_raw = {}         # version -> raw key bytes (only for deriving the HMAC key)
_current = None   # newest version


def _vault(path, body, token=None):
    req = urllib.request.Request(f"{os.environ['VAULT_ADDR'].rstrip('/')}/v1/{path}", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         **({"X-Vault-Token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _login():
    if os.environ.get("VAULT_TOKEN"):
        return os.environ["VAULT_TOKEN"]
    with open(SA_TOKEN) as f:
        jwt = f.read().strip()
    mount, role = os.environ.get("VAULT_AUTH_MOUNT", "kubernetes"), os.environ.get("VAULT_ROLE", "cards")
    return _vault(f"auth/{mount}/login", {"role": role, "jwt": jwt})["auth"]["client_token"]


class _Vault:
    """Wrap / unwrap data keys with a Vault Transit key."""

    def __init__(self):
        self.token = _login()
        self.key = os.environ.get("VAULT_TRANSIT_KEY", "cards")

    def new(self):
        return _vault(f"transit/datakey/wrapped/{self.key}", {"bits": 256}, self.token)["data"]["ciphertext"]

    def unwrap(self, wrapped):
        if wrapped.startswith(LOCAL):
            raise Unavailable("the data keys were wrapped with a local key, not Vault")
        return base64.b64decode(_vault(f"transit/decrypt/{self.key}", {"ciphertext": wrapped}, self.token)["data"]["plaintext"])


class _Local:
    """Wrap / unwrap data keys with a 32-byte key from CARDS_LOCAL_KEY or CARDS_KEY_FILE."""

    def __init__(self, have_keys):
        raw = os.environ.get("CARDS_LOCAL_KEY")
        path = os.environ.get("CARDS_KEY_FILE")
        if not raw and path:
            if not os.path.exists(path):
                if have_keys:   # a fresh key could not open the data already stored
                    raise Unavailable("the key file is missing and the database already holds wrapped keys")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(base64.b64encode(os.urandom(32)).decode() + "\n")
            with open(path) as f:
                raw = f.read().strip()
        try:
            kek = base64.b64decode(raw or "", validate=True)
        except Exception:
            kek = b""
        if len(kek) != 32:
            raise Unavailable("the local key must be 32 bytes, base64")
        self.kek = AESGCM(kek)

    def new(self):
        nonce = os.urandom(12)
        return LOCAL + base64.b64encode(nonce + self.kek.encrypt(nonce, os.urandom(32), WRAP_AAD)).decode()

    def unwrap(self, wrapped):
        if not wrapped.startswith(LOCAL):
            raise Unavailable("the data keys were wrapped by Vault, not a local key")
        blob = base64.b64decode(wrapped[len(LOCAL):])
        return self.kek.decrypt(blob[:12], blob[12:], WRAP_AAD)


def _wrapper(have_keys):
    if os.environ.get("VAULT_ADDR"):
        return _Vault()
    if os.environ.get("CARDS_LOCAL_KEY") or os.environ.get("CARDS_KEY_FILE"):
        return _Local(have_keys)
    raise Unavailable("no key source is configured (VAULT_ADDR, CARDS_KEY_FILE or CARDS_LOCAL_KEY)")


def _load(conn):
    global _current
    q = "SELECT version, wrapped FROM encryption_keys ORDER BY version"
    try:
        rows = conn.execute(q).fetchall()
        w = _wrapper(bool(rows))
        if not rows:
            conn.execute("INSERT INTO encryption_keys (version, wrapped) VALUES (1, %s) ON CONFLICT DO NOTHING", (w.new(),))
            conn.commit()
            rows = conn.execute(q).fetchall()
        for r in rows:
            _raw[r["version"]] = w.unwrap(r["wrapped"])
            _keys[r["version"]] = AESGCM(_raw[r["version"]])
        _current = max(_keys)
    except Unavailable:
        _keys.clear()
        _raw.clear()
        raise
    except Exception as ex:
        _keys.clear()
        _raw.clear()
        raise Unavailable(f"{'Vault' if os.environ.get('VAULT_ADDR') else 'local key'}: {type(ex).__name__}") from None


def ready(conn):
    with _lock:
        if not _keys:
            _load(conn)
    return True


def encrypt(conn, plaintext: bytes, aad: str) -> bytes:
    ready(conn)
    nonce = os.urandom(12)
    return f"v{_current}:".encode() + nonce + _keys[_current].encrypt(nonce, plaintext, aad.encode())


def decrypt(conn, blob: bytes, aad: str) -> bytes:
    ready(conn)
    blob = bytes(blob)
    head, rest = blob.split(b":", 1)
    ver = int(head[1:])
    if ver not in _keys:
        raise Unavailable(f"unknown key version {ver}")
    return _keys[ver].decrypt(rest[:12], rest[12:], aad.encode())


def enc_json(conn, obj, aad):
    return encrypt(conn, json.dumps(obj, separators=(",", ":")).encode(), aad)


def dec_json(conn, blob, aad):
    return json.loads(decrypt(conn, blob, aad))


def hmac_hex(conn, purpose: str, data: bytes) -> str:
    """Keyed fingerprint for duplicate detection (key = derived from the current data key; never stored)."""
    import hashlib
    import hmac as _hmac
    ready(conn)
    k = hashlib.sha256(b"cards-hmac|" + purpose.encode() + b"|" + _raw[min(_raw)]).digest()   # oldest key: stable across rotation
    return _hmac.new(k, data, hashlib.sha256).hexdigest()
