"""Statement upload -> encrypted transaction lines.

PDF (password used once, never stored) -> text in this process (pypdf) -> the model reads sections + lines ->
lines checked against the printed total -> the shared clean-up (pipeline.py: bank charges out, purchases abroad and
online shops recognised, each line's MCC from the shop list, else a model guess) -> every value encrypted
(crypto.py) -> the PDF bytes are dropped (never written anywhere). The statement's own period also tells the card's
closing day (pipeline.set_close_day), unless the person typed one."""
import datetime
import hashlib
import io
import threading
import traceback
import uuid

from pypdf import PdfReader

from psycopg.types.json import Jsonb

from . import catalogue, crypto, db, llm, pipeline

BOOST_ACCOUNTS = {"uob_lady_savings": "UOB Lady's Savings balance"}

NOISE, words, ShopIndex = pipeline.NOISE, pipeline.words, pipeline.ShopIndex   # they live with the shared clean-up


def pdf_text(data, password):
    r = PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        if not password or not r.decrypt(password):
            raise ValueError("The PDF is password-protected: enter its password (usually your NRIC or date of birth).")
    text = "\n".join((p.extract_text() or "") for p in r.pages)
    if len(text.strip()) < 200:
        raise ValueError("No text found in the PDF (a scanned image?). Download the e-statement PDF from the bank instead.")
    return text


PROGRAMME_WORDS = [("uni", "UNI"), ("dbs point", "DBS_POINTS"), ("treats", "TREATS"), ("ocbc$", "OCBC"), ("ocbc dollar", "OCBC"),
                   ("90", "ocbc-90n"), ("voyage", "ocbc-voyage"), ("citi miles", "CITI_MILES"), ("thankyou", "citi-ty"),
                   ("membership reward", "amex-mr"), ("hsbc", "hsbc-points"), ("360", "sc-360"), ("krisflyer", "KF")]


def keep_points(conn, person, parsed, sdate):
    """Rewards balances printed on the statement -> the person's points (encrypted), newest statement per programme."""
    rewards = parsed.get("rewards") or []
    if not rewards or not sdate:
        return
    row = conn.execute("SELECT points_enc FROM people WHERE id = %s", (person,)).fetchone()
    pts = crypto.dec_json(conn, row["points_enc"], f"people.points_enc:{person}") if row and row["points_enc"] else {}
    for r in rewards:
        name = str(r.get("programme") or "").lower()
        prog = next((v for k, v in PROGRAMME_WORDS if k in name), None)
        try:
            bal = float(r.get("balance"))
        except (TypeError, ValueError):
            continue
        if prog and bal >= 0 and sdate.isoformat() >= (pts.get(prog) or {}).get("date", ""):
            pts[prog] = {"balance": bal, "date": sdate.isoformat(), "bank": str(parsed.get("bank") or "")[:40],
                         "expiry": str(r.get("expiry") or "")[:40] or None, "name": str(r.get("programme"))[:40]}
    conn.execute("UPDATE people SET points_enc = %s WHERE id = %s", (crypto.enc_json(conn, pts, f"people.points_enc:{person}"), person))


def section_key(name, last4):
    return hashlib.sha256(f"{name}|{last4}".encode()).hexdigest()[:12]


def to_date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def auto_map(conn, sections, held, hint, last4=None):
    """section key -> card id, when confident: the card's last 4 digits (Settings) first, then a single section + the
    card picked on upload, then the card name."""
    cards = conn.execute("SELECT id, name, bank_id FROM cards WHERE id = ANY(%s)", (list(held) or [""],)).fetchall()
    out = {}
    for s in sections:
        if last4 and s.get("last4") and s["last4"] in last4:
            out[s["key"]] = last4[s["last4"]]
    if hint and len(sections) == 1 and sections[0]["key"] not in out:
        out[sections[0]["key"]] = hint
        return out
    for s in sections:
        if s["key"] in out:
            continue
        best = max(((catalogue.name_sim(s["name"] or "", c["name"]), c["id"]) for c in cards), default=(0, None))
        if best[0] >= 0.6:
            out[s["key"]] = best[1]
    return out


def process(stmt_id, person, data, password, hint):
    with db.connect() as conn:
        try:
            text = pdf_text(data, password)
            del data
            parsed = llm.read_statement(text)
            del text
            held = conn.execute("SELECT held_cards FROM people WHERE id = %s", (person,)).fetchone()["held_cards"]
            shops = ShopIndex(conn)
            sections, lines = [], []
            for sec in parsed.get("sections") or []:
                key = section_key(sec.get("name"), sec.get("last4"))
                n = 0
                for ln in sec.get("lines") or []:
                    d, amt = to_date(ln.get("date")), ln.get("amount")
                    try:
                        amt = round(float(amt), 2)
                    except (TypeError, ValueError):
                        continue
                    if not d or amt == 0:
                        continue
                    desc = str(ln.get("description") or "")[:120]
                    cur = str(ln.get("currency") or "SGD")[:3].upper()
                    lines.append({"key": key, "date": d, "merchant": desc, "text": desc, "amount": amt,
                                  "currency": cur if cur != "SGD" else "", "online": ln.get("online")})
                    n += 1
                sections.append({"key": key, "name": str(sec.get("name") or "")[:60], "last4": str(sec.get("last4") or "")[:4], "n": n})
            if not lines:
                raise ValueError("No transactions found in this statement.")
            charges = round(sum(ln["amount"] for ln in lines if ln["amount"] > 0), 2)   # as printed: before any line is dropped
            lines, _ = pipeline.run(conn, lines, shops, model=True)   # the same clean-up every source goes through
            if not lines:
                raise ValueError("No purchases found in this statement.")
            # same statement already in (a re-downloaded or re-shared copy has different bytes): don't import twice
            l4 = sorted(sec["last4"] or sec["name"] for sec in sections if sec["n"])
            pto = to_date(parsed.get("period_to")) or max(ln["date"] for ln in lines)
            fp = crypto.hmac_hex(conn, "statement", f"{str(parsed.get('bank') or '').lower()}|{','.join(l4)}|{pto}|{charges:.2f}".encode())
            twin = conn.execute("SELECT id, uploaded_at FROM statements WHERE person = %s AND fingerprint = %s AND id <> %s AND "
                                "status IN ('ready', 'needs_cards')", (person, fp, stmt_id)).fetchone()
            if twin:
                raise ValueError(f"This statement is already in (added {twin['uploaded_at']:%d %b %Y}), so nothing was imported twice.")
            printed = parsed.get("printed_total")
            try:
                printed = round(float(printed), 2) if printed is not None else None
            except (TypeError, ValueError):
                printed = None
            checked = printed is not None and abs(charges - printed) <= max(1.0, printed * 0.01)
            from . import ingest
            mapping = auto_map(conn, sections, held, hint, ingest.settings(conn, person).get("last4"))
            keep_points(conn, person, parsed, to_date(parsed.get("period_to")))
            seen = {}
            for ln in lines:
                # stable id per transaction: the same line in two overlapping statements is stored once
                base = f"{person}|{ln['key']}|{ln['date']}|{ln['amount']:.2f}|{ln['text'].upper()}"
                seen[base] = seen.get(base, 0) + 1
                lid = crypto.hmac_hex(conn, "line", f"{base}|{seen[base]}".encode())[:32]
                if conn.execute("SELECT 1 FROM stmt_lines WHERE id = %s", (lid,)).fetchone():
                    continue
                blob = crypto.enc_json(conn, {k: ln.get(k) for k in pipeline.FIELDS}, f"stmt_lines.data_enc:{lid}")
                conn.execute("INSERT INTO stmt_lines (id, statement_id, person, card_id, section, txn_date, data_enc) VALUES "
                             "(%s,%s,%s,%s,%s,%s,%s)", (lid, stmt_id, person, mapping.get(ln["key"]), ln["key"], ln["date"], blob))
            dates = [ln["date"] for ln in lines]
            meta = {"bank": str(parsed.get("bank") or "")[:40], "printed_total": printed, "charges": charges,
                    "sections": sections}
            status = "ready" if all(s["key"] in mapping for s in sections if s["n"]) else "needs_cards"
            learn_close_day(conn, person, set(mapping.values()), to_date(parsed.get("period_to")))
            conn.execute("UPDATE statements SET status = %s, n_lines = %s, checked = %s, period_from = %s, period_to = %s, "
                         "meta_enc = %s, fingerprint = %s WHERE id = %s",
                         (status, len(lines), checked, to_date(parsed.get("period_from")) or min(dates),
                          to_date(parsed.get("period_to")) or max(dates),
                          crypto.enc_json(conn, meta, f"statements.meta_enc:{stmt_id}"), fp, stmt_id))
            conn.commit()
        except Exception as ex:
            conn.rollback()
            msg = str(ex) if isinstance(ex, ValueError) else "Couldn't read this statement."
            if not isinstance(ex, ValueError):
                print(f"statement {stmt_id[:8]} failed: {type(ex).__name__}", flush=True)
                traceback.print_exc(limit=2)
            try:
                conn.execute("UPDATE statements SET status = 'failed', meta_enc = %s WHERE id = %s",
                             (crypto.enc_json(conn, {"error": msg}, f"statements.meta_enc:{stmt_id}"), stmt_id))
            except crypto.Unavailable:
                conn.execute("UPDATE statements SET status = 'failed' WHERE id = %s", (stmt_id,))
            conn.commit()


def learn_close_day(conn, person, cards, period_to):
    """A statement's own closing date gives its cards' closing day (kept unless the person typed one)."""
    if not period_to:
        return
    for card in cards:
        if card:
            pipeline.set_close_day(conn, person, card, period_to.day, "statement")


def start(conn, person, data, password, hint, wait=False):
    """Returns (statement id, already) - already = the same PDF was uploaded before (nothing is read again).
    wait=True reads it now (ingest job) instead of in a background thread (web upload)."""
    crypto.ready(conn)   # refuse up front when encryption is unavailable
    fh = crypto.hmac_hex(conn, "file", data)
    dup = conn.execute("SELECT id FROM statements WHERE person = %s AND file_hmac = %s AND status <> 'failed' ORDER BY "
                       "uploaded_at LIMIT 1", (person, fh)).fetchone()
    if dup:
        return dup["id"], True
    sid = uuid.uuid4().hex
    conn.execute("INSERT INTO statements (id, person, status, file_hmac) VALUES (%s,%s,'reading',%s)", (sid, person, fh))
    conn.commit()
    if wait:
        process(sid, person, data, password, hint)
    else:
        threading.Thread(target=process, args=(sid, person, data, password, hint), daemon=True).start()
    return sid, False


FEED_FIELDS = pipeline.FIELDS


def feed_id(person):
    return f"feed-{person}"


def wf_id(person):
    """The statement that holds the person's Wealthfolio lines (wealthfolio.py)."""
    return f"wf-{person}"


def feed(conn, person, body):
    """A feeder (a script of the person's own that already collects their card transactions) sends its whole recent
    window. Feed lines in [from, to] are replaced by what arrives; every value is encrypted. Returns counts."""
    crypto.ready(conn)
    sid = feed_id(person)
    conn.execute("INSERT INTO statements (id, person, status) VALUES (%s,%s,'ready') ON CONFLICT (id) DO NOTHING", (sid, person))
    f, t = to_date(body.get("from")), to_date(body.get("to"))
    valid = {r["id"] for r in conn.execute("SELECT id FROM cards").fetchall()}
    prow = conn.execute("SELECT * FROM people WHERE id = %s", (person,)).fetchone()
    owner = pipeline.chosen(prow)   # a card the person gave to another source is not taken from the feeder
    keep, added, updated, elsewhere, batch = set(), 0, 0, 0, []
    for ln in body.get("lines") or []:
        d = to_date(ln.get("date"))
        try:
            amt = round(float(ln.get("amount")), 2)
        except (TypeError, ValueError):
            continue
        if not d or not ln.get("id"):
            continue
        card = ln.get("card") if ln.get("card") in valid else None
        if card and owner.get(card, "feed") != "feed":
            elsewhere += 1
            continue
        c = pipeline.clean({"merchant": ln.get("merchant"), "amount": amt, "currency": ln.get("currency"),
                            "fcy": bool(ln.get("fcy")) if "fcy" in ln else None,
                            "online": ln.get("online") if isinstance(ln.get("online"), bool) else None,
                            "channel": ln.get("channel"), "est": ln.get("est"), "mcc": ln.get("mcc"), "mcc_src": ln.get("mcc_src")})
        if c is None:
            continue   # the bank's own charge: not spend
        c.update({"_id": hashlib.sha256(f"{person}|feed|{ln['id']}".encode()).hexdigest()[:32], "_card": card, "_date": d})
        batch.append(c)
    pipeline.place(conn, batch)   # a line the feeder sent without a category gets one the way every other line does
    for c in batch:
        lid, card, d = c["_id"], c["_card"], c["_date"]
        keep.add(lid)
        data = {k: c.get(k) for k in FEED_FIELDS}
        blob = crypto.enc_json(conn, data, f"stmt_lines.data_enc:{lid}")
        cur = conn.execute("INSERT INTO stmt_lines (id, statement_id, person, card_id, section, txn_date, data_enc) VALUES "
                           "(%s,%s,%s,%s,'feed',%s,%s) ON CONFLICT (id) DO UPDATE SET card_id = EXCLUDED.card_id, txn_date = "
                           "EXCLUDED.txn_date, data_enc = EXCLUDED.data_enc RETURNING (xmax = 0) AS inserted",
                           (lid, sid, person, card, d, blob)).fetchone()
        added, updated = added + bool(cur["inserted"]), updated + (not cur["inserted"])
    removed = 0
    if f and t:
        gone = [r["id"] for r in conn.execute("SELECT id FROM stmt_lines WHERE statement_id = %s AND txn_date BETWEEN %s AND %s",
                                              (sid, f, t)).fetchall() if r["id"] not in keep]
        if gone:
            conn.execute("DELETE FROM stmt_lines WHERE id = ANY(%s)", (gone,))
        removed = len(gone)
    # statement closing days (kept unless the person typed one or a statement file gave one) + boost balances the
    # feeder measures
    for k, v in (body.get("statement_days") or {}).items():
        if k in valid and str(v).isdigit() and 1 <= int(v) <= 31:
            pipeline.set_close_day(conn, person, k, int(v), "feed")
    p = conn.execute("SELECT boost_amounts, boost_auto FROM people WHERE id = %s", (person,)).fetchone()
    amounts, auto = dict(p["boost_amounts"]), dict(p["boost_auto"])
    for b in body.get("boosts") or []:
        label = BOOST_ACCOUNTS.get(b.get("account"))
        row = conn.execute("SELECT id FROM card_boosts WHERE card_id = %s AND label = %s", (b.get("card"), label)).fetchone() if label else None
        try:
            amt = round(float(b.get("amount")), 2)
        except (TypeError, ValueError):
            continue
        if row:
            amounts[str(row["id"])] = amt
            auto[str(row["id"])] = {"month": str(b.get("month") or "")[:7], "source": "feed"}
    conn.execute("UPDATE people SET boost_amounts = %s, boost_auto = %s WHERE id = %s", (Jsonb(amounts), Jsonb(auto), person))
    if isinstance(body.get("fees"), list):   # annual-fee charges / reversals the feeder saw (fee tracker)
        from . import fees
        fees.store_feed(conn, person, body["fees"])
    w = body.get("wallet")
    if isinstance(w, dict) and w.get("total") is not None:
        conn.execute("UPDATE people SET wallet_enc = %s, wallet_at = now() WHERE id = %s",
                     (crypto.enc_json(conn, w, f"people.wallet_enc:{person}"), person))
    from . import awards
    awards.store_scan(conn, (body.get("awards") or {}).get("destinations"))
    conn.execute("UPDATE people SET feed_at = now() WHERE id = %s", (person,))   # the feeder is alive (points.feed_alive)
    n = conn.execute("SELECT count(*) n, min(txn_date) a, max(txn_date) b FROM stmt_lines WHERE statement_id = %s", (sid,)).fetchone()
    conn.execute("UPDATE statements SET n_lines = %s, period_from = %s, period_to = %s, checked = true WHERE id = %s",
                 (n["n"], n["a"], n["b"], sid))
    conn.commit()
    return {"added": added, "updated": updated - 0, "removed": removed, "total": n["n"], "left to another source": elsewhere}


def stop_feed(conn, person_row):
    """Stop listening to the feeder: everything it owned is cleared, so the app's own numbers show. Its lines, its
    wallet, its fee list, its boost balances and closing days go; what the person typed and what statements or
    Wealthfolio gave stays. -> lines removed. (The caller also drops the feeder's token.)"""
    pid = person_row["id"]
    sid = feed_id(pid)
    n = conn.execute("DELETE FROM stmt_lines WHERE statement_id = %s", (sid,)).rowcount or 0
    conn.execute("UPDATE statements SET n_lines = 0, period_from = NULL, period_to = NULL WHERE id = %s", (sid,))
    amounts, auto = dict(person_row.get("boost_amounts") or {}), dict(person_row.get("boost_auto") or {})
    for bid, how in list(auto.items()):
        if (how or {}).get("source") == "feed":
            auto.pop(bid, None)
            amounts.pop(bid, None)
    days, srcs = dict(person_row.get("statement_days") or {}), dict(person_row.get("statement_days_src") or {})
    for card in list(days):
        if srcs.get(card, "feed") == "feed":
            days.pop(card, None)
            srcs.pop(card, None)
    conn.execute("UPDATE people SET wallet_enc = NULL, wallet_at = NULL, fees_enc = NULL, feed_at = NULL, boost_amounts = %s, "
                 "boost_auto = %s, statement_days = %s, statement_days_src = %s WHERE id = %s",
                 (Jsonb(amounts), Jsonb(auto), Jsonb(days), Jsonb(srcs), pid))
    return n


def load_lines(conn, person, months=None, statement=None):
    """Decrypted lines for one person (optionally a month list 'YYYY-MM' or one statement), each with how it was paid
    (pipeline.paid: "pay", "paid_src"). One source speaks for a card in a month, so nothing is counted twice:
      - a card the person gave to a source (Settings) takes its automatic lines only from that source;
      - when both a feeder and the Wealthfolio import hold lines of a card in a month, the feeder's count unless the
        person chose Wealthfolio;
      - uploaded statement lines and alerts are left out where an automatic source covers the card and month, and
        alerts where a statement does."""
    from . import ingest
    sql = "SELECT id, statement_id, card_id, txn_date, data_enc FROM stmt_lines WHERE person = %s"
    args = [person]
    if statement:
        sql += " AND statement_id = %s"
        args.append(statement)
    rows = conn.execute(sql + " ORDER BY txn_date, id", args).fetchall()
    prow = conn.execute("SELECT card_sources FROM people WHERE id = %s", (person,)).fetchone()
    owner = pipeline.chosen(prow)
    st = ingest.settings(conn, person)
    memory, fixes = st.get("paid") or {}, st.get("fixes") or {}
    aid = f"alerts-{person}"
    kind = {r["id"]: pipeline.kind_of(r["statement_id"], person) for r in rows}
    cm = lambda r: (r["card_id"], r["txn_date"].strftime("%Y-%m"))
    if not statement:
        rows = [r for r in rows if kind[r["id"]] == "mail" or owner.get(r["card_id"], kind[r["id"]]) == kind[r["id"]]]
        both = {}
        for r in rows:
            if kind[r["id"]] != "mail":
                both.setdefault(cm(r), set()).add(kind[r["id"]])
        loser = {k: ("feed" if owner.get(k[0]) == "wealthfolio" else "wealthfolio") for k, v in both.items() if len(v) > 1}
        rows = [r for r in rows if loser.get(cm(r)) != kind[r["id"]]]
    covered = {cm(r) for r in rows if kind[r["id"]] != "mail"}
    stmt_cov = {cm(r) for r in rows if kind[r["id"]] == "mail" and r["statement_id"] != aid}
    out = []
    for r in rows:
        if months and r["txn_date"].strftime("%Y-%m") not in months:
            continue
        if not statement and kind[r["id"]] == "mail" and cm(r) in covered:
            continue   # a feeder or Wealthfolio covers this card + month
        if not statement and r["statement_id"] == aid and cm(r) in stmt_cov:
            continue   # a statement covers it: alert lines were only the preview
        d = crypto.dec_json(conn, r["data_enc"], f"stmt_lines.data_enc:{r['id']}")
        ln = {"id": r["id"], "statement_id": r["statement_id"], "card_id": r["card_id"], "date": r["txn_date"], **d}
        pipeline.fix(ln, fixes)   # the person's correction for this shop (currency, shop type)
        ln["pay"], ln["paid_src"] = pipeline.paid(ln, memory)
        if ln["pay"]:
            ln["online"] = ln["pay"] == "online"
        out.append(ln)
    return out
