"""Ingest card mail and statement files per person (for anyone without a feeder).

  mail   read-only IMAP (BODY.PEEK: nothing in the mailbox changes) of ONE folder of the person's mailbox, last 45
         days: a Gmail label (e.g. "Cards", every mail in it counts) or INBOX (then only bank senders). Transaction ALERT mails -> the model extracts date / merchant / amount / currency /
         last4 -> a live line on the card with that last4 (Settings). Statement PDF attachments -> the upload pipeline
         (with the person's stored PDF passwords, duplicate guard included).
  files  a drop folder per person (<CARDS_INBOX_DIR>/<person>/): new PDFs go through the same pipeline; screenshots
         of the bank's app (PNG, JPEG, WebP) are read by the model and only their new rows are added (screenshots.py).
         Each file is moved to done/ or failed/, with a .txt saying why or what became of each row.

The mailbox is set by each person on the Settings page (config.py; the password is stored encrypted), with a
"Test connection" button (test_mailbox). A person who connected Wealthfolio and switched its import on gets that
import in the same run (wealthfolio.py). Nothing is printed except counts; the counts of each person's last run are
kept for their Settings page. What was handled is remembered as keyed HMACs (ingest_seen), so a mail or file is never read twice."""
import datetime
import email
import email.policy
import hashlib
import html
import imaplib
import os
import re
import shutil

from . import config, crypto, db, llm, pipeline, screenshots, statements

# bank id -> the mail domains its alerts and e-statements come from (subdomains count). One list for reading mail
# (BANK_SENDERS) and for the mailbox rules the Settings page hands out (mailhelp.py).
SENDER_DOMAINS = {
    "dbs": ["dbs.com", "dbs.com.sg", "posb.com.sg"], "uob": ["uob.com.sg", "uobgroup.com"], "ocbc": ["ocbc.com"],
    "citi": ["citi.com", "citibank.com.sg"], "hsbc": ["hsbc.com.sg", "hsbc.com"], "sc": ["sc.com"],
    "maybank": ["maybank.com.sg", "maybank2u.com.sg"], "amex": ["americanexpress.com", "aexp.com"],
    "trust": ["trustbank.sg"], "cimb": ["cimb.com.sg"], "boc": ["bankofchina.com"], "dcs": ["dcscard.com"],
    "icbc": ["icbc.com.sg"],
}
BANK_SENDERS = re.compile(r"@([a-z0-9-]+\.)*(" + "|".join(re.escape(d) for ds in SENDER_DOMAINS.values() for d in ds) + r")>?$", re.I)
INBOX = os.environ.get("CARDS_INBOX_DIR", "/inbox")


def settings(conn, person):
    row = conn.execute("SELECT settings_enc FROM people WHERE id = %s", (person,)).fetchone()
    if not row or not row["settings_enc"]:
        return {"last4": {}, "pdf_passwords": []}
    d = crypto.dec_json(conn, row["settings_enc"], f"people.settings_enc:{person}")
    d.setdefault("last4", {})
    d.setdefault("pdf_passwords", [])
    return d


def save_settings(conn, person, d):
    conn.execute("UPDATE people SET settings_enc = %s WHERE id = %s",
                 (crypto.enc_json(conn, d, f"people.settings_enc:{person}"), person))


def seen(conn, person, kind, key):
    h = crypto.hmac_hex(conn, f"ingest-{kind}", key.encode())
    return h, conn.execute("SELECT 1 FROM ingest_seen WHERE person = %s AND kind = %s AND hmac = %s", (person, kind, h)).fetchone() is not None


def mark(conn, person, kind, h, outcome):
    conn.execute("INSERT INTO ingest_seen (person, kind, hmac, outcome) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (person, kind, h, outcome))
    conn.commit()


def retry_alerts_without_card(conn, person):
    """An alert for a card the person had not added yet (or whose last 4 digits were not entered) was set aside; once
    new last 4 digits are saved, those mails are read again on the next run (the mailbox is read 45 days back), so a
    payment made before the card was added still counts. "failed" is how such mails were marked before 0.10.17."""
    return conn.execute("DELETE FROM ingest_seen WHERE person = %s AND kind = 'mail' AND outcome IN ('no card', 'failed')",
                        (person,)).rowcount


def text_of(msg):
    parts = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html") and not part.get_filename():
            try:
                t = part.get_content()
            except Exception:
                continue
            if ct == "text/html":
                t = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[\s\S]*?</\1>", "", t)))
            parts.append(t)
    return re.sub(r"\s+", " ", " ".join(parts))[:6000]


ALERT = """This is an email from a Singapore bank. If it is a card TRANSACTION alert (a purchase or refund on a credit card),
reply JSON only: {{"alert": true, "date": "YYYY-MM-DD", "merchant": "...", "amount": 12.34, "currency": "SGD",
"sgd_amount": null or number if a SGD equivalent is shown, "last4": "1234", "refund": false, "online": true/false/null}}
Otherwise reply {{"alert": false}}. Subject: {subject}
Received: {date}
Body: {body}"""


def alert_line(conn, person, st, msg):
    subject = str(msg.get("Subject") or "")[:200]
    body = text_of(msg)
    try:
        d = llm.first_json(llm.chat(ALERT.format(subject=subject, date=str(msg.get("Date") or ""), body=body), max_tokens=300))
    except Exception:
        return None
    if not d.get("alert"):
        return None
    card = st["last4"].get(str(d.get("last4") or "")[-4:])
    dt = statements.to_date(d.get("date"))
    try:
        amt = float(d.get("sgd_amount") or d.get("amount"))
    except (TypeError, ValueError):
        return None
    cur = str(d.get("currency") or "SGD").upper()[:3]
    fcy = cur != "SGD"
    if fcy and not d.get("sgd_amount"):
        amt = round(amt, 2)   # foreign amount: kept as an estimate until the statement line replaces it
    if not dt or not card:
        return {"skip": "no card with that last4" if not card else "no date"}
    return {"date": dt, "card": card, "merchant": str(d.get("merchant") or "")[:120], "amount": -abs(amt) if d.get("refund") else abs(amt),
            "currency": cur, "fcy": fcy, "online": d.get("online"), "est": fcy and not d.get("sgd_amount")}


def store_alerts(conn, person, lines, section="alert"):
    """Alert lines go under statement alerts-<person> (a statement for the same card + month hides them: load_lines).
    Screenshot rows go there too, with section "screenshot" and their own id ("lid")."""
    if not lines:
        return 0
    sid = f"alerts-{person}"
    conn.execute("INSERT INTO statements (id, person, status) VALUES (%s,%s,'ready') ON CONFLICT (id) DO NOTHING", (sid, person))
    # the same clean-up every source goes through (pipeline.py): the alert states a currency and sometimes "online";
    # the rest is read from the merchant text
    raws = [{**ln, "text": ln["merchant"], "currency": ln["currency"] if ln.get("fcy") else "", "fcy": None} for ln in lines]
    cleaned, _ = pipeline.run(conn, raws, model=True)
    n = 0
    for ln in cleaned:
        lid = ln.get("lid") or crypto.hmac_hex(conn, "alert", f"{person}|{ln['card']}|{ln['date']}|{ln['amount']:.2f}|{ln['text'].upper()}|{ln['mid']}".encode())[:32]
        data = {k: ln.get(k) for k in pipeline.FIELDS}
        conn.execute("INSERT INTO stmt_lines (id, statement_id, person, card_id, section, txn_date, data_enc) VALUES "
                     "(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                     (lid, sid, person, ln["card"], section, ln["date"], crypto.enc_json(conn, data, f"stmt_lines.data_enc:{lid}")))
        n += 1
    c = conn.execute("SELECT count(*) n, min(txn_date) a, max(txn_date) b FROM stmt_lines WHERE statement_id = %s", (sid,)).fetchone()
    conn.execute("UPDATE statements SET n_lines = %s, period_from = %s, period_to = %s WHERE id = %s", (c["n"], c["a"], c["b"], sid))
    conn.commit()
    return n


def run_mail(conn, person, stats):
    cfg = config.load(conn, person)
    if not (cfg["imap_host"] and cfg["imap_user"] and cfg["imap_password"]):
        return
    st = settings(conn, person)
    box = imaplib.IMAP4_SSL(cfg["imap_host"], _port(cfg), timeout=60)
    try:
        box.login(cfg["imap_user"], cfg["imap_password"])
        folder = cfg["imap_folder"] or "INBOX"
        # a Gmail label (IMAP folder) the person fills on purpose: every mail in it counts (incl. statements they forward
        # from their own address); a plain INBOX is filtered to bank senders
        trust = folder.upper() != "INBOX"
        typ, _ = box.select(_quoted(folder), readonly=True)
        if typ != "OK":
            stats["mail folder not found"] += 1
            return
        since = (datetime.date.today() - datetime.timedelta(days=45)).strftime("%d-%b-%Y")
        typ, data = box.search(None, "SINCE", since)
        alerts = []
        for num in (data[0] or b"").split()[-400:]:
            typ, hdr = box.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)])")
            head = email.message_from_bytes(hdr[0][1], policy=email.policy.default)
            mid = str(head.get("Message-ID") or num.decode())
            h, done = seen(conn, person, "mail", mid)
            if done:
                continue
            if not trust and not BANK_SENDERS.search(str(head.get("From") or "").strip()):
                mark(conn, person, "mail", h, "skipped")
                stats["mail skipped"] += 1
                continue
            typ, raw = box.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
            pdfs = [p for p in msg.iter_attachments() if (p.get_filename() or "").lower().endswith(".pdf")
                    or p.get_content_type() == "application/pdf"]
            if pdfs:
                outcome = "statement"
                for p in pdfs:
                    data_b = p.get_payload(decode=True) or b""
                    stats[statement_file(conn, person, st, data_b)] += 1
                mark(conn, person, "mail", h, outcome)
                continue
            ln = alert_line(conn, person, st, msg)
            if ln and "skip" not in ln:
                ln["mid"] = h[:12]
                alerts.append(ln)
                mark(conn, person, "mail", h, "alert")
            else:
                stats["mail not an alert" if not ln else "alert without a known card"] += 1
                mark(conn, person, "mail", h, "skipped" if not ln else "no card")
        alerts, known = screenshots.drop_seen_alerts(conn, person, alerts)
        if known:
            stats["alerts a screenshot already added"] += known
        stats["alerts"] += store_alerts(conn, person, alerts)
    finally:
        try:
            box.logout()
        except Exception:
            pass


def _port(cfg):
    try:
        return int(cfg.get("imap_port") or 993)
    except ValueError:
        return 993


def _quoted(folder):
    return f'"{folder}"' if " " in folder else folder


def test_mailbox(cfg):
    """Try the mailbox the way ingest would (TLS, sign in, open the folder read-only) and say plainly what happened.
    -> (ok, sentence). Nothing in the mailbox is read or changed."""
    host, user, password = cfg.get("imap_host"), cfg.get("imap_user"), cfg.get("imap_password")
    folder = cfg.get("imap_folder") or "INBOX"
    if not (host and user and password):
        missing = [n for n, v in (("the server", host), ("the user name", user), ("the app password", password)) if not v]
        return False, "Not tried: " + ", ".join(missing) + " missing."
    try:
        box = imaplib.IMAP4_SSL(host, _port(cfg), timeout=20)
    except Exception as ex:
        return False, (f"Could not reach {host} on port {_port(cfg)} ({type(ex).__name__}). Check the server name and that "
                       "this machine can reach it.")
    try:
        try:
            box.login(user, password)
        except imaplib.IMAP4.error as ex:
            said = str(ex)
            low = said.lower()
            if "application-specific password" in low or "app password" in low:
                why = "the provider wants an app password here, not the account's normal password"
            elif "imap" in low and ("disabled" in low or "not enabled" in low):
                why = "IMAP access is switched off for this mailbox; switch it on in the provider's settings"
            elif "basicauthblocked" in low or "basic auth" in low:
                why = "this provider no longer accepts passwords for IMAP"
            else:
                why = "the user name or the app password is wrong, or the app password was revoked"
            return False, f"Reached {host}, but the sign-in was refused: {why}. The server said: {said[:160]}"
        typ, _ = box.select(_quoted(folder), readonly=True)
        if typ != "OK":
            names = []
            try:
                for line in box.list()[1] or []:
                    m = re.search(rb'"?([^"]+)"?$', line.strip())
                    if m:
                        names.append(m.group(1).decode("utf-8", "replace"))
            except Exception:
                pass
            hint = f" Folders in this mailbox: {', '.join(sorted(names)[:30])}." if names else ""
            return False, f"Signed in, but there is no folder or label named \"{folder}\".{hint}"
        typ, data = box.search(None, "ALL")
        n = len((data[0] or b"").split()) if typ == "OK" else 0
        return True, f"Connected. \"{folder}\" holds {n} message{'' if n == 1 else 's'}."
    except Exception as ex:
        return False, f"Signed in, then the mailbox stopped answering ({type(ex).__name__})."
    finally:
        try:
            box.logout()
        except Exception:
            pass


def statement_file(conn, person, st, data):
    """One PDF through the pipeline, trying each stored password. Returns a stats key."""
    if not data.startswith(b"%PDF"):
        return "not a pdf"
    try:
        from pypdf import PdfReader
        import io
        r = PdfReader(io.BytesIO(data))
        pw = ""
        if r.is_encrypted:
            pw = next((p for p in st["pdf_passwords"] if r.decrypt(p)), None)
            if pw is None:
                return "pdf password unknown"
        sid, already = statements.start(conn, person, data, pw, None, wait=True)
        return "duplicate file" if already else "statement read"
    except crypto.Unavailable:
        raise
    except Exception as ex:
        print(f"ingest pdf: {type(ex).__name__}", flush=True)
        return "pdf failed"


def run_files(conn, person, stats):
    root = os.path.join(INBOX, person)
    if not os.path.isdir(root):
        return
    st = settings(conn, person)
    for sub in ("done", "failed"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        low = name.lower()
        if not os.path.isfile(path) or name.startswith(".") or not low.endswith((".pdf",) + screenshots.IMAGES + screenshots.HEIC):
            continue
        if low.endswith(".pdf"):
            with open(path, "rb") as f:
                data = f.read()
            res = statement_file(conn, person, st, data)
            stats[res] += 1
            ok = res in ("statement read", "duplicate file")
            why = None if ok else {"pdf password unknown": "The PDF is password-protected and none of your saved passwords opened it. Add it in "
                                                           "Settings in the app, then move the file back here.",
                                   "not a pdf": "This isn't a PDF file."}.get(res, "It couldn't be read. Try uploading it on the Statements page.")
        else:
            ok, why = screenshot_file(conn, person, st, path, stats)
        dest = os.path.join(root, "done" if ok else "failed", name)
        shutil.move(path, dest)
        if why:
            with open(dest + ".txt", "w") as f:
                f.write(why + "\n")


def screenshot_file(conn, person, st, path, stats):
    """One screenshot from the drop folder (screenshots.py). -> (read, the text of the .txt beside it)."""
    if path.lower().endswith(screenshots.HEIC):
        stats["screenshot failed"] += 1
        return False, ("This is a HEIC picture, which can't be read here. On the phone, save or share the screenshot as "
                       "PNG or JPEG (on an iPhone: Settings, Camera, Formats, Most Compatible) and drop it here again.")
    # the day it was taken (the file's time here) settles "Today", "Yesterday" and dates without a year
    day = datetime.datetime.fromtimestamp(os.path.getmtime(path)).date()
    with open(path, "rb") as f:
        data = f.read()
    try:
        counts, notes = screenshots.take(conn, person, st, data, os.path.basename(path), day)
    except screenshots.Refused as ex:
        conn.rollback()
        stats["screenshot failed"] += 1
        return False, str(ex)
    stats["screenshot read"] += 1
    stats.update({k: v for k, v in counts.items() if v})
    head = (f"Read on {datetime.date.today().isoformat()}: {counts['screenshot rows added']} added, "
            f"{counts['screenshot rows already there']} already there, {counts['screenshot rows skipped']} skipped.")
    return True, "\n".join([head, ""] + notes)


def note_run(conn, person, counts):
    """Remember when ingest last ran for the person and what it counted (counts only), for their Settings page."""
    import json
    at = datetime.datetime.now().astimezone().isoformat(timespec="minutes")
    config.put(conn, "ingest_last", json.dumps({"at": at, "counts": dict(sorted(counts.items()))}), person)
    conn.commit()


def run_wealthfolio(conn, person):
    """The Wealthfolio import for a person who switched it on (wealthfolio.py). Its own counts go to its own note on
    the Settings page; here only whether it ran. Never raises, except when encryption is unavailable."""
    from . import wealthfolio
    try:
        if config.load(conn, person).get("wf_on") != "1":
            return {}
        row = conn.execute("SELECT * FROM people WHERE id = %s", (person,)).fetchone()
        counts = wealthfolio.run_person(conn, row)
        said = {"wealthfolio lines new": counts["new"], "wealthfolio lines updated": counts["updated"],
                "wealthfolio lines removed": counts["removed"]}
        return {k: v for k, v in said.items() if v}
    except crypto.Unavailable:
        raise
    except wealthfolio.Problem:
        return {"wealthfolio import failed": 1}
    except Exception as ex:
        conn.rollback()
        print(f"ingest {person} wealthfolio: {type(ex).__name__}", flush=True)
        return {"wealthfolio import failed": 1}


def run():
    from collections import Counter
    stats = Counter()
    with db.connect() as conn:
        crypto.ready(conn)
        people = [r["id"] for r in conn.execute("SELECT id FROM people ORDER BY id").fetchall()]
        for p in people:
            mine = Counter()
            for fn in (run_mail, run_files):
                try:
                    fn(conn, p, mine)
                except crypto.Unavailable:
                    raise
                except Exception as ex:
                    mine[f"{'mailbox' if fn is run_mail else 'drop folder'} failed ({type(ex).__name__})"] += 1
                    print(f"ingest {p} {fn.__name__}: {type(ex).__name__}", flush=True)
            note_run(conn, p, mine)
            stats.update(mine)
            stats.update(run_wealthfolio(conn, p))
        from . import outside
        stats.update(outside.refresh(conn))   # match results, AwardWallet: only what has a key, a few times a day
    print("ingest: " + (", ".join(f"{k} {v}" for k, v in sorted(stats.items())) or "nothing new"), flush=True)
    return 0
