"""Setup pages: everything a deployment needs after its first start is set here, not in the environment.

  Settings page (each person)   Wealthfolio connection (address, access token, which account is which card, import);
                                mailbox connection with a test button and setup steps for the person's mail provider
                                and banks, a Gmail filter file to import, API tokens for a feeder and a bot.
  /admin (admins)               people, the model, messages, the catalogue run.

Secrets (app password, model key, bot token) are write-only: the pages say "set" or "not set" and never show them.
An API token is shown once, when it is made; only its SHA-256 is kept."""
import html
import json

import datetime

from . import config, crypto, ingest, mailhelp, people as people_mod, pipeline, wealthfolio

e = lambda v: html.escape("" if v is None else str(v), quote=True)
PERSON_TABLES = ("stmt_lines", "statements", "goals", "advice", "ingest_seen")


def _field(label, name, value="", kind="text", hint="", extra=""):
    return (f"<p><label>{e(label)}<br><input type={kind} name={name} value='{e(value)}' autocomplete=off {extra} "
            f"style='width:100%;max-width:30em'></label>{f'<br><small>{hint}</small>' if hint else ''}</p>")


def _secret(label, name, state, hint=""):
    said = {"app": "set", "env": "set (from the environment)", "": "not set"}[state]
    forget = (f" <label class=chip><input type=checkbox name=forget_{name} value=1> forget it</label>" if state == "app" else "")
    return (f"<p><label>{e(label)} <span class=chip>{said}</span><br><input type=password name={name} value='' "
            f"autocomplete=new-password placeholder='{'leave blank to keep it' if state else ''}' "
            f"style='width:100%;max-width:30em'></label>{forget}{f'<br><small>{hint}</small>' if hint else ''}</p>")


def _switch(label, name, on, hint=""):
    return (f"<p><label class=chip><input type=checkbox name={name} value=1 {'checked' if on else ''}> {e(label)}</label>"
            f"{f'<br><small>{hint}</small>' if hint else ''}</p>")


def _banks(conn, person):
    """[(bank id, bank name)] of the person's held cards."""
    held = list(person["held_cards"]) or [""]
    return [(r["id"], r["name"]) for r in conn.execute(
        "SELECT DISTINCT b.id, b.name FROM cards c JOIN banks b ON b.id = c.bank_id WHERE c.id = ANY(%s) ORDER BY b.name",
        (held,)).fetchall()]


def _last_run(cfg):
    try:
        last = json.loads(cfg.get("ingest_last") or "")
    except ValueError:
        return "Mail has not been read yet."
    counts = ", ".join(f"{k} {v}" for k, v in last.get("counts", {}).items()) or "nothing new"
    return f"Last read {e(last.get('at', '').replace('T', ' '))}: {e(counts)}."


# ---------------------------------------------------------------- each person: mailbox ----------------------------------------------------------------
def mailbox_section(conn, person, note=None):
    pid = person["id"]
    try:
        cfg, where = config.load(conn, pid), config.where(conn, pid)
    except crypto.Unavailable:
        return "<h2 id=mail>Card mail</h2><p class=muted>Unavailable right now (encryption).</p>"
    prov_id = cfg["imap_provider"] or ("other" if cfg["imap_host"] else "gmail")
    prov = mailhelp.PROVIDERS.get(prov_id, mailhelp.PROVIDERS["other"])
    folder = cfg["imap_folder"] or mailhelp.DEFAULT_FOLDER
    banks = _banks(conn, person)
    known, unknown = mailhelp.senders(banks)
    connected = bool(cfg["imap_host"] and cfg["imap_user"] and cfg["imap_password"])
    options = "".join(f"<option value={k} {'selected' if k == prov_id else ''}>{e(v['name'])}</option>" for k, v in mailhelp.PROVIDERS.items())
    out = ["<h2 id=mail>Card mail</h2>",
           "<p class=muted>The app can read bank alert mails and e-statements from one folder of a mailbox. It only reads: "
           "nothing in the mailbox is changed, marked or deleted. Other mail is never opened when the folder holds only "
           "bank mail.</p>"]
    if note:
        out.append(f"<div class=hint><span class={'ok' if note[0] else 'bad'}>{'Works.' if note[0] else 'Not working yet.'}</span> {e(note[1])}</div>")
    out.append(f"<p><small>{'Connected as ' + e(cfg['imap_user']) + ', folder ' + e(folder) + '. ' + _last_run(cfg) if connected else 'No mailbox connected.'}</small></p>")

    # 1. the steps for this provider
    steps = "".join(f"<li>{e(s)}</li>" for s in prov["steps"])
    out.append(f"<details{'' if connected else ' open'}><summary>How to set up {e(prov['name'])}</summary><ol>{steps}</ol>")
    if not banks:
        out.append("<p class=muted>Pick the cards held first (top of this page): the rules below are then written for those banks.</p>")
    else:
        if known:
            rows = "".join(f"<div class=row><span class=grow>{e(n)}</span><span><small class=w>{e(s)}</small></span></div>"
                           for n, s, _ in mailhelp.rules_text(banks, folder))
            out.append(f"<p>Mail to collect in the folder <b>{e(folder)}</b>, one rule per bank (sender contains):</p><div class=list>{rows}</div>")
            if prov["import"] == "gmail":
                out.append("<p style='margin-top:12px'><a class=btn href=/settings/mail-filters.xml>Download the Gmail filter file</a></p>"
                           f"<p><small>It holds exactly the rules above: mail from these banks gets the label {e(folder)}"
                           f"{' and skips the inbox' if cfg['imap_skip_inbox'] == '1' else ''}. Import it in Gmail on a computer: Settings, "
                           "See all settings, Filters and Blocked Addresses, Import filters. Tick \"Apply new filters to existing "
                           "email\" there to label the mail already received. Save the folder name below first if it is not "
                           f"\"{e(mailhelp.DEFAULT_FOLDER)}\".</small></p>")
            else:
                out.append(f"<p><small>{e(prov['name'])} has no filter file to import. Create the rules by hand: {e(prov['rules'])}.</small></p>")
        if unknown:
            out.append(f"<p class=warn><small>No sender address is known for: {e(', '.join(unknown))}. Look at one of its mails and add a rule "
                       "for that sender by hand.</small></p>")
        out.append("<p><small>Each bank sends card mail only when it is switched on: in the bank's app or internet banking, turn on "
                   "transaction alerts by email and e-statements by email for each card. Banks that only send a notice with no "
                   "amount, or a link instead of a PDF, need the statement uploaded on the Statements page.</small></p>")
    out.append("</details>")

    # 2. the connection
    if prov["works"]:
        host_hint = "" if prov_id == "other" else f"Filled in for {e(prov['name'])} when left empty."
        out.append(
            "<form method=post action=/settings/mailbox>"
            f"<p><label>Mail provider<br><select name=imap_provider>{options}</select></label> <button class='quiet sm' name=action value=provider>Show its steps</button></p>"
            + _field("Server", "imap_host", cfg["imap_host"] if prov_id == "other" or cfg["imap_host"] != prov["host"] else "", hint=host_hint,
                     extra=f"placeholder='{e(prov['host'])}'")
            + _field("Port", "imap_port", cfg["imap_port"], kind="number")
            + _field("User name", "imap_user", cfg["imap_user"], hint=e(prov["user"]))
            + _secret("App password", "imap_password", where["imap_password"], "Stored encrypted. Never shown again.")
            + _field("Folder or label", "imap_folder", cfg["imap_folder"], extra=f"placeholder='{e(mailhelp.DEFAULT_FOLDER)}'",
                     hint="One folder that holds only bank mail. INBOX also works: then only mail from known bank senders is opened.")
            + (_switch("Filter file: also skip the inbox", "imap_skip_inbox", cfg["imap_skip_inbox"] == "1",
                       "Bank mail then shows only under the label.") if prov["import"] == "gmail" else "")
            + "<p><button name=action value=test>Save and test the connection</button> <button class=quiet name=action value=save>Save</button>"
            + (" <button class=quiet name=action value=disconnect>Disconnect</button>" if connected else "") + "</p></form>")
    else:
        out.append(f"<form method=post action=/settings/mailbox><p><label>Mail provider<br><select name=imap_provider>{options}</select></label> "
                   "<button class='quiet sm' name=action value=provider>Show its steps</button></p></form>")
    return "".join(out)


def post_mailbox(conn, form, person):
    """-> (ok, sentence) to show on the Settings page, or None for a plain save."""
    pid = person["id"]
    action = (form.get("action") or ["save"])[0]
    prov_id = (form.get("imap_provider") or [""])[0]
    prov_id = prov_id if prov_id in mailhelp.PROVIDERS else "other"
    if action == "provider":
        config.put(conn, "imap_provider", prov_id, pid)
        if config.load(conn, pid)["imap_host"] in {v["host"] for v in mailhelp.PROVIDERS.values()}:
            config.put(conn, "imap_host", mailhelp.PROVIDERS[prov_id]["host"], pid)   # the old provider's server goes with it
        conn.commit()
        return None
    if action == "disconnect":
        for key in ("imap_host", "imap_user", "imap_password", "imap_folder", "imap_port"):
            config.put(conn, key, "", pid)
        conn.commit()
        return True, "Disconnected. The stored app password is gone; revoke it at the mail provider as well."
    prov = mailhelp.PROVIDERS[prov_id]
    form = dict(form)
    if not (form.get("imap_host") or [""])[0].strip():
        form["imap_host"] = [prov["host"]]
    if not (form.get("imap_port") or [""])[0].strip():
        form["imap_port"] = [str(prov["port"])]
    if not (form.get("imap_folder") or [""])[0].strip():
        form["imap_folder"] = [mailhelp.DEFAULT_FOLDER]
    config.save_form(conn, form, ("imap_provider", "imap_host", "imap_port", "imap_user", "imap_password", "imap_folder", "imap_skip_inbox"), pid)
    conn.commit()
    if action != "test":
        return None
    return ingest.test_mailbox(config.load(conn, pid))


def filter_file(conn, person):
    """-> (file name, XML text) of the Gmail filters for the person's banks, or None when no bank sender is known."""
    cfg = config.load(conn, person["id"])
    banks = _banks(conn, person)
    if not mailhelp.senders(banks)[0]:
        return None
    return "mailFilters.xml", mailhelp.gmail_filters(banks, cfg["imap_folder"] or mailhelp.DEFAULT_FOLDER, cfg["imap_skip_inbox"] == "1")


# ---------------------------------------------------------------- each person: Wealthfolio ----------------------------------------------------------------
WF_STEPS = ("In Wealthfolio, open Settings and then Agent access.",
            "Create a personal access token. Tick the scopes accounts:read and activities:read and nothing else: the app only reads.",
            "Copy the token (Wealthfolio shows it once) and paste it below, with the address Wealthfolio is opened at.",
            "Press \"Save and test\". The accounts then appear here: choose the card for each card account.")


def _held(conn, person):
    """[{"id", "name"}] of the person's held cards."""
    return conn.execute("SELECT c.id, c.name FROM cards c WHERE c.id = ANY(%s) ORDER BY c.name", (list(person["held_cards"]) or [""],)).fetchall()


def _wf_counts(counts):
    said = f"{counts.get('new', 0)} new, {counts.get('updated', 0)} updated, {counts.get('removed', 0)} removed"
    if counts.get("could not place"):
        said += f", {counts['could not place']} without a category"
    if counts.get("cards left to the feeder"):
        said += f"; {counts['cards left to the feeder']} card(s) left to the feeder that already sends them"
    if counts.get("cards left to another source"):
        said += f"; {counts['cards left to another source']} card(s) left to the source chosen for them"
    if counts.get("accounts matched to a new card"):
        said += f"; {counts['accounts matched to a new card']} account(s) matched to a card added since (check below)"
    if counts.get("savings balance read"):
        said += "; savings balance read"
    if counts.get("savings balance not read"):
        said += "; the savings balance could not be read"
    return said


def _hist_said(h):
    """The history import's progress or result, in a sentence (counts, months and dates only)."""
    if not h:
        return ""
    c, when = h.get("counts") or {}, e(str(h.get("at", "")).replace("T", " ")[:16])
    nums = (f"{c.get('new', 0)} new and {c.get('updated', 0)} updated lines in {c.get('months read', 0)} month(s)"
            + (f", {c['could not place']} without a category" if c.get("could not place") else ""))
    if h.get("state") == "running":
        return f"<span class=warn>History import running</span> (started or last moved {when}): {nums} so far{', at ' + e(h['month']) if h.get('month') else ''}. Reload to see it move."
    if h.get("state") == "failed":
        return f"History import stopped ({when}): <span class=bad>{e(h.get('error') or 'unknown reason')}</span> {nums} were kept. It can be started again."
    left = ""
    if c.get("months left to another source"):
        left = (f" {c['months left to another source']} card-month(s) were left alone because a statement, an alert or a feeder "
                f"already has lines there: {e(', '.join(h.get('skipped') or []))}.")
    if c.get("cards left to another source"):
        left += f" {c['cards left to another source']} card(s) were skipped: their source is not Wealthfolio."
    return (f"History import finished {when}: {nums}."
            + (f" The oldest purchase found is dated {e(h['oldest'])}." if h.get("oldest") else " Nothing older than the window was found.") + left)


def wealthfolio_section(conn, person, note=None):
    pid = person["id"]
    try:
        cfg, where = config.load(conn, pid), config.where(conn, pid)
    except crypto.Unavailable:
        return "<h2 id=wealthfolio>Wealthfolio</h2><p class=muted>Unavailable right now (encryption).</p>"
    connected = bool(cfg["wf_url"] and cfg["wf_token"])
    accounts = wealthfolio.known_accounts(cfg)
    out = ["<h2 id=wealthfolio>Wealthfolio</h2>",
           "<p class=muted>For people who keep their card accounts in Wealthfolio: the app reads the purchases and refunds of "
           "the accounts chosen below and keeps them in step. It only reads; nothing in Wealthfolio is changed. An "
           "independent project, not affiliated with Wealthfolio.</p>"]
    if note:
        out.append(f"<div class=hint><span class={'ok' if note[0] else 'bad'}>{'Done.' if note[0] else 'Not working yet.'}</span> {e(note[1])}</div>")
    last = wealthfolio.last(cfg)
    if not connected:
        status = "Not connected."
    elif not last:
        status = f"Connected to {e(cfg['wf_url'])}. Nothing imported yet."
    elif last.get("error"):
        status = (f"Connected to {e(cfg['wf_url'])}. The last import ({e(str(last.get('at', '')).replace('T', ' '))}) did not run: "
                  f"<span class=bad>{e(last['error'])}</span>")
    else:
        status = (f"Connected to {e(cfg['wf_url'])}. Last import {e(str(last.get('at', '')).replace('T', ' '))}: "
                  f"{e(_wf_counts(last.get('counts') or {}))}.")
    out.append(f"<p><small>{status}</small></p>")
    steps = "".join(f"<li>{e(s)}</li>" for s in WF_STEPS)
    out.append(f"<details{'' if connected else ' open'}><summary>How to connect</summary><ol>{steps}</ol></details>")
    out.append(
        "<form method=post action=/settings/wealthfolio>"
        + _field("Address", "wf_url", cfg["wf_url"], kind="url", hint="The address Wealthfolio is opened at, for example http://wealthfolio:8088.")
        + _secret("Access token", "wf_token", where["wf_token"], "Stored encrypted. Never shown again.")
        + "<p><button name=action value=test>Save and test</button> <button class=quiet name=action value=save>Save</button>"
        + (" <button class=quiet name=action value=disconnect>Disconnect</button>" if connected else "") + "</p></form>")
    if not (connected and accounts):
        return "".join(out)

    held = _held(conn, person)
    saved = wealthfolio.mapping(cfg)
    saved_held = wealthfolio.mapping(cfg, {c["id"] for c in held})
    if saved_held:   # a card added since: suggested for its account, as the next import would match it
        used = {r["card_id"] for r in conn.execute("SELECT DISTINCT card_id FROM stmt_lines WHERE person = %s AND card_id = ANY(%s)",
                                                   (pid, [c["id"] for c in held] or [""])).fetchall()}
        hints = wealthfolio.new_matches(accounts, held, saved_held, wealthfolio.declined(cfg), used)
    else:            # nothing matched yet: every account whose name plainly names a held card
        hints = wealthfolio.suggest(accounts, held)
    rows = []
    for i, a in enumerate(accounts):
        chosen = saved.get(a["id"]) or hints.get(a["id"]) or ""
        opts = "<option value=''>not a card</option>" + "".join(
            f"<option value='{e(c['id'])}' {'selected' if c['id'] == chosen else ''}>{e(c['name'])}</option>" for c in held)
        rows.append(f"<div class=row><input type=hidden name=acct_{i} value='{e(a['id'])}'><span class=grow>{e(a['name'])}"
                    f"<small class=w>{e(a.get('type') or '')}{' · suggested from the name' if a['id'] in hints else ''}</small></span>"
                    f"<select name=card_{i} aria-label='Card for {e(a['name'])}'>{opts}</select></div>")
    out.append("<h3 style='font-size:14px;margin:18px 0 4px'>Which account is which card</h3>")
    if not held:
        out.append("<p class=muted>Pick the cards held first (top of this page): they are the choices here.</p>")
    since, until = wealthfolio.window(cfg)
    try:
        fed = wealthfolio.fed_cards(conn, pid, since, until) & set(saved.values())
    except Exception:
        fed = set()
    fed -= {c for c, src in pipeline.chosen(person).items() if src == "wealthfolio"}
    if fed:
        names = ", ".join(c["name"] for c in held if c["id"] in fed)
        out.append(f"<p class=warn><small>A feeder already sends the transactions of: {e(names)}. The import leaves those cards "
                   "alone, so nothing is counted twice. The button gives the cards matched below to Wealthfolio: the feeder's "
                   "lines of them are removed, the feeder is no longer listened to for them, and they are imported from "
                   "Wealthfolio. One card at a time is done under <a href='#sources'>Where each card's transactions come "
                   "from</a>.</small></p>"
                   "<form method=post action=/settings/wealthfolio><p><button class=quiet name=action value=takeover>"
                   "Take these cards over from the feeder</button></p></form>")
    out.append(
        f"<form method=post action=/settings/wealthfolio><div class=list>{''.join(rows)}</div>"
        + _field("Days to keep in step", "wf_days", cfg["wf_days"], kind="number",
                 hint="Each run re-reads this many days: a line changed or deleted in Wealthfolio within them is changed or removed here.")
        + _switch("Import on a schedule", "wf_on", cfg["wf_on"] == "1",
                  "The import then runs whenever <code>ingest</code> runs (the compose file runs it every 15 minutes).")
        + "<p><button name=action value=import>Save and import now</button> <button class=quiet name=action value=map>Save</button></p></form>"
        "<p><small>Each import lists Wealthfolio's accounts again. A card added here later is matched to its account by "
        "itself when the account's name plainly names it (the next import shows \"accounts matched to a new card\"); "
        "change it here if it is wrong.</small></p>")
    # ---- older purchases, once
    hist = wealthfolio.history_state(cfg)
    out.append("<h3 style='font-size:14px;margin:18px 0 4px'>Older purchases</h3><p class=muted>The schedule keeps the last "
               f"{e(cfg['wf_days'])} days in step. Everything older can be read once: a month at a time, in the background. A "
               "month in which a card already has lines from a statement, an alert or a feeder is left alone, so nothing is "
               "counted twice. It is safe to run again.</p>"
               + (f"<p><small>{_hist_said(hist)}</small></p>" if hist else "")
               + "<form method=post action=/settings/wealthfolio><p><label>From <input type=date name=since></label> "
               "<small>empty = everything Wealthfolio has</small></p>"
               "<p><button class=quiet name=action value=history>Import history</button></p></form>")
    # ---- a card whose rate depends on a savings balance
    boosts = conn.execute("SELECT b.id, b.label, c.name card FROM card_boosts b JOIN cards c ON c.id = b.card_id WHERE b.card_id = ANY(%s) "
                          "ORDER BY c.name, b.label", (list(person["held_cards"]) or [""],)).fetchall()
    typed = any(a.get("type") == wealthfolio.CASH_TYPE for a in accounts)
    cash = [a for a in accounts if a.get("type") == wealthfolio.CASH_TYPE or not typed]
    if boosts and cash:
        acct, bid = wealthfolio.boost_choice(cfg)
        hint = ""
        if not acct:   # nothing chosen yet: offer the account whose name fits, for the person to confirm
            for bst in boosts:
                guess = wealthfolio.suggest_boost(bst["label"], cash)
                if guess:
                    acct, bid, hint = guess, str(bst["id"]), " <small class=w>suggested from the name: press Save to confirm</small>"
                    break
        bo = "".join(f"<option value='{b['id']}' {'selected' if str(b['id']) == bid else ''}>{e(b['card'])}: {e(b['label'])}</option>" for b in boosts)
        ao = "<option value=''>none</option>" + "".join(
            f"<option value='{e(a['id'])}' {'selected' if a['id'] == acct else ''}>{e(a['name'])}</option>" for a in cash)
        out.append("<h3 style='font-size:14px;margin:18px 0 4px'>Savings balance for a boosted rate</h3><p class=muted>Some cards "
                   "pay more when a linked savings account holds enough. Name that account and each import works out its "
                   "average balance for the month, from the running balance Wealthfolio keeps. Only its balance is read.</p>"
                   f"<form method=post action=/settings/wealthfolio><p><select name=boost aria-label=Boost>{bo}</select> "
                   f"<select name=boost_account aria-label='Savings account'>{ao}</select> "
                   f"<button class=quiet name=action value=boost>Save</button>{hint}</p></form>")
    return "".join(out)


def post_wealthfolio(conn, form, person):
    """-> (ok, sentence) to show on the Settings page, or None for a plain save."""
    pid = person["id"]
    get = lambda k: (form.get(k) or [""])[0].strip()
    action = get("action") or "save"
    if action == "disconnect":
        for key in ("wf_url", "wf_token", "wf_map", "wf_accounts", "wf_declined", "wf_last"):
            config.put(conn, key, "", pid)
        config.put(conn, "wf_on", "0", pid)
        conn.commit()
        return True, ("Disconnected. The stored token is gone; revoke it in Wealthfolio as well. Lines already imported stay "
                      "as they are.")
    if action in ("test", "save"):
        if get("wf_url"):
            try:
                wealthfolio.endpoint(get("wf_url"))
            except wealthfolio.Problem as ex:
                return False, str(ex)
        config.save_form(conn, form, ("wf_url", "wf_token"), pid)
        conn.commit()
        if action == "save":
            return None
        cfg = config.load(conn, pid)
        if not (cfg["wf_url"] and cfg["wf_token"]):
            return False, "Not tried: " + ("the address" if not cfg["wf_url"] else "the access token") + " is missing."
        ok, said, accounts = wealthfolio.test(cfg["wf_url"], cfg["wf_token"])
        if accounts is not None:
            config.put(conn, "wf_accounts", json.dumps(accounts), pid)
            conn.commit()
        return ok, said
    if action == "takeover":
        cfg = config.load(conn, pid)
        cards = set(wealthfolio.mapping(cfg, set(person["held_cards"] or [])).values())
        if not cards:
            return False, "Nothing taken over: no Wealthfolio account is matched to a card yet."
        n = wealthfolio.take_over(conn, person, cards)
        conn.commit()
        person = conn.execute("SELECT * FROM people WHERE id = %s", (pid,)).fetchone()
        try:
            counts = wealthfolio.run_person(conn, person)
        except wealthfolio.Problem as ex:
            return False, f"{n} feeder lines were removed, but the import did not run: {ex}"
        return True, f"{n} feeder lines removed. Imported from Wealthfolio: {_wf_counts(counts)}."
    if action == "boost":
        acct, bid = get("boost_account"), get("boost")
        cfg = config.load(conn, pid)
        ok_acct = acct in {a["id"] for a in wealthfolio.known_accounts(cfg)}
        ok_boost = conn.execute("SELECT 1 FROM card_boosts WHERE id::text = %s AND card_id = ANY(%s)", (bid, list(person["held_cards"]) or [""])).fetchone()
        config.put(conn, "wf_boost", json.dumps({"account": acct, "boost": bid}) if ok_acct and ok_boost else "", pid)
        conn.commit()
        return None
    if action == "history":
        cfg = config.load(conn, pid)
        since = None
        if get("since"):
            try:
                since = datetime.date.fromisoformat(get("since"))
            except ValueError:
                return False, "Not started: the start date is not a date."
        h = wealthfolio.history_state(cfg) or {}
        try:
            fresh = (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(h.get("at"))).total_seconds() < 600
        except (TypeError, ValueError):
            fresh = False
        if h.get("state") == "running" and fresh:
            return False, "A history import is already running. Its progress is shown below."
        if not (cfg["wf_url"] and cfg["wf_token"] and wealthfolio.mapping(cfg, set(person["held_cards"] or []))):
            return False, "Not started: connect Wealthfolio and match at least one account to a card first."
        wealthfolio.history_note(conn, pid, "running", {})
        conn.commit()
        wealthfolio.start_history(pid, since)
        return True, "The history import has started. Its progress is shown under \"Older purchases\"; reload this page to follow it."
    # map / import: which account is which card, the window, the switch
    held = {c["id"] for c in _held(conn, person)}
    amap, no, i = {}, {}, 0
    while f"acct_{i}" in form:
        card = get(f"card_{i}")
        if card in held:
            amap[get(f"acct_{i}")] = card
        else:   # "not a card": not matched by itself later to a card held now (a card added later still can be)
            no[get(f"acct_{i}")] = sorted(held)
        i += 1
    if len(set(amap.values())) < len(amap):
        return False, "Not saved: two accounts were given the same card. One card is one account."
    try:
        days = int(get("wf_days") or wealthfolio.DAYS)
    except ValueError:
        return False, f"Not saved: {get('wf_days')} is not a number of days."
    if not 7 <= days <= 400:
        return False, "Not saved: the days must be between 7 and 400."
    config.put(conn, "wf_map", json.dumps(amap), pid)
    keep = {a: v for a, v in wealthfolio.declined(config.load(conn, pid)).items() if a not in amap}
    config.put(conn, "wf_declined", json.dumps(dict(keep, **no)), pid)
    config.put(conn, "wf_days", str(days), pid)
    config.put(conn, "wf_on", "1" if form.get("wf_on") else "0", pid)
    conn.commit()
    if action != "import":
        return None
    try:
        counts = wealthfolio.run_person(conn, person)
    except wealthfolio.Problem as ex:
        return False, str(ex)
    return True, f"Imported: {_wf_counts(counts)}."


# ---------------------------------------------------------------- each person: one source per card ----------------------------------------------------------------
def sources_section(conn, person, note=None):
    """Where each held card's transactions come from, with a way to move a card. Someone who only uses mail and
    statement files sees one sentence."""
    pid = person["id"]
    try:
        cfg = config.load(conn, pid)
    except crypto.Unavailable:
        return ""
    held = _held(conn, person)
    wf_on = bool(cfg["wf_url"] and cfg["wf_token"] and cfg["wf_on"] == "1")
    wf_cards = set(wealthfolio.mapping(cfg).values())
    eff = pipeline.effective(conn, person, wf_cards if wf_on else ())
    names = dict(pipeline.SOURCES)
    out = ["<h2 id=sources>Where each card's transactions come from</h2>"]
    if note:
        out.append(f"<div class=hint><span class={'ok' if note[0] else 'bad'}>{'Done.' if note[0] else 'Not changed.'}</span> {e(note[1])}</div>")
    others = wf_cards or config.has_token(conn, pid, "feed") or any(v[0] != "mail" for v in eff.values())
    if not others:
        out.append("<p class=muted>All cards: card mail and statement files (below). Nothing to choose.</p>")
        return "".join(out)
    out.append("<p class=muted>One source speaks for a card, so nothing is counted twice. \"Automatic\" follows what is there: "
               "a feeder that sends the card, else Wealthfolio when the card is matched to an account there, else mail and "
               "statement files. Moving a card removes the lines the old source imported for it; statement files stay.</p>")
    rows = []
    for c in held:
        src, picked = eff.get(c["id"], ("mail", False))
        opts = f"<option value='' {'' if picked else 'selected'}>Automatic (now: {e(names[src])})</option>" + "".join(
            f"<option value={k} {'selected' if picked and src == k else ''}>{e(v)}</option>" for k, v in pipeline.SOURCES)
        rows.append(f"<div class=row><span class=grow>{e(c['name'])}</span><select name='src_{e(c['id'])}' aria-label='Source for {e(c['name'])}'>{opts}</select></div>")
    out.append(f"<form method=post action=/settings/sources><div class=list>{''.join(rows) or '<div class=row>Pick your cards first.</div>'}</div>"
               "<p style='margin-top:12px'><button>Save</button></p></form>")
    return "".join(out)


def post_sources(conn, form, person):
    """-> (ok, sentence) or None when nothing changed."""
    pid = person["id"]
    cfg = config.load(conn, pid)
    wf_cards = set(wealthfolio.mapping(cfg).values())
    mine = pipeline.chosen(person)
    moved, removed = [], 0
    names = {c["id"]: c["name"] for c in _held(conn, person)}
    for cid in names:
        want = (form.get(f"src_{cid}") or [""])[0]
        if want not in dict(pipeline.SOURCES):
            want = ""
        if want == (mine.get(cid) or ""):
            continue
        if want == "wealthfolio" and cid not in wf_cards:
            return False, f"{names[cid]} is not matched to a Wealthfolio account yet. Match it under Wealthfolio first."
        removed += pipeline.set_source(conn, person, cid, want)
        person = conn.execute("SELECT * FROM people WHERE id = %s", (pid,)).fetchone()
        moved.append(names[cid])
    conn.commit()
    if not moved:
        return None
    said = f"Moved: {', '.join(moved)}." + (f" {removed} line(s) of the old source were removed." if removed else "")
    if any(pipeline.chosen(person).get(c) == "wealthfolio" for c in names) and cfg["wf_url"] and cfg["wf_token"]:
        try:
            counts = wealthfolio.run_person(conn, person)
            said += f" Imported from Wealthfolio: {_wf_counts(counts)}."
        except wealthfolio.Problem as ex:
            said += f" The Wealthfolio import did not run: {ex}"
    return True, said


# ---------------------------------------------------------------- each person: statement closing days ----------------------------------------------------------------
def closing_section(conn, person):
    held = _held(conn, person)
    if not held:
        return ""
    days, srcs = person.get("statement_days") or {}, person.get("statement_days_src") or {}
    rows = []
    for c in held:
        d = days.get(c["id"])
        src = srcs.get(c["id"]) or ("feed" if d else None)
        rows.append(f"<div class=row><span class=grow>{e(c['name'])}<small class=w>{e(pipeline.CLOSE_SRC[src]) if src else 'calendar month'}</small></span>"
                    f"<input name='day_{e(c['id'])}' inputmode=numeric maxlength=2 placeholder='day' value='{e(d or '')}' style='width:5em' "
                    f"aria-label='Closing day of {e(c['name'])}'><input type=hidden name='was_{e(c['id'])}' value='{e(d or '')}'></div>")
    return ("<h2 id=closing>Statement closing days</h2><p class=muted>The day of the month each card's statement closes (1 to 31). "
            "Caps and minimum spends that run per statement use it; empty = the calendar month. It is filled in from a statement "
            "file when one is read. A day typed here is kept.</p>"
            f"<form method=post action=/settings/closing><div class=list>{''.join(rows)}</div><p style='margin-top:12px'><button>Save</button></p></form>")


def post_closing(conn, form, person):
    for c in _held(conn, person):
        raw, was = (form.get(f"day_{c['id']}") or [""])[0].strip(), (form.get(f"was_{c['id']}") or [""])[0].strip()
        if raw == was:
            continue                                   # untouched: whatever filled it in keeps its place
        if raw == "":
            pipeline.set_close_day(conn, person["id"], c["id"], None, "you")
        elif raw.isdigit() and 1 <= int(raw) <= 31:
            pipeline.set_close_day(conn, person["id"], c["id"], int(raw), "you")
    conn.commit()


# ---------------------------------------------------------------- each person: how each shop is paid ----------------------------------------------------------------
def paid_section(conn, person):
    try:
        mem = ingest.settings(conn, person["id"]).get("paid") or {}
    except crypto.Unavailable:
        return ""
    names = dict(pipeline.CHANNELS)
    out = [f"<h2 id=paid>How each shop is paid</h2><p class=muted>{len(mem)} shop(s) remembered. An answer is given on a purchase under "
           "<a href=/spend/lines>Transactions</a> (\"How was this paid?\") and is then used for that shop: for the open month and "
           "everything after. A line whose bank says how it was paid keeps the bank's word. Stored encrypted.</p>"]
    if mem:
        rows = "".join(f"<div class=row><span class=grow>{e(v.get('name') or k)}<small class=w>{e(names.get(v.get('ch'), v.get('ch')))} · since {e(v.get('since'))}</small></span>"
                       f"<button class='quiet sm' name=forget value='{e(k)}'>Forget</button></div>" for k, v in sorted(mem.items()))
        out.append(f"<form method=post action=/settings/paid><div class=list>{rows}</div></form>")
    return "".join(out)


def fixes_section(conn, person):
    try:
        fixes = ingest.settings(conn, person["id"]).get("fixes") or {}
    except crypto.Unavailable:
        return ""
    out = [f"<h2 id=fixes>Corrections</h2><p class=muted>{len(fixes)} shop(s) corrected. A correction is made on a purchase under "
           "<a href=/spend/lines>Transactions</a> (\"Fix this purchase\"): the currency it was charged in and the shop type. "
           "It counts for every purchase at that shop. Stored encrypted.</p>"]
    if fixes:
        rows = "".join(f"<div class=row><span class=grow>{e(v.get('name') or k)}<small class=w>"
                       + " · ".join(x for x in ((f"charged in {v['currency']}" if v.get("currency") else ""),
                                                (f"shop type {v['mcc']}" if v.get("mcc") else "")) if x)
                       + f"</small></span><button class='quiet sm' name=forget value='{e(k)}'>Forget</button></div>" for k, v in sorted(fixes.items()))
        out.append(f"<form method=post action=/settings/fixes><div class=list>{rows}</div></form>")
    return "".join(out)


def post_fixes(conn, form, person):
    key = (form.get("forget") or [""])[0]
    st = ingest.settings(conn, person["id"])
    fixes = dict(st.get("fixes") or {})
    if fixes.pop(key, None) is not None:
        st["fixes"] = fixes
        ingest.save_settings(conn, person["id"], st)
        conn.commit()


def post_paid(conn, form, person):
    key = (form.get("forget") or [""])[0]
    st = ingest.settings(conn, person["id"])
    mem = dict(st.get("paid") or {})
    if mem.pop(key, None) is not None:
        st["paid"] = mem
        ingest.save_settings(conn, person["id"], st)
        conn.commit()


# ---------------------------------------------------------------- each person: API tokens ----------------------------------------------------------------
TOKEN_TEXT = {"feed": ("Feeder token", "Lets a program send this person's card transactions to the app (POST /api/feed)."),
              "bot": ("Bot token", "Lets a chat bot ask which card to use and read the caps (GET /api/lookup, /api/shop, /api/caps).")}


def awardwallet_section(conn, person, note=None):
    """Settings: the person's own AwardWallet Business API key, to keep the KrisFlyer account balance current."""
    pid = person["id"]
    cfg, where = config.load(conn, pid), config.where(conn, pid)
    out = ["<h2 id=awardwallet>KrisFlyer balance from AwardWallet</h2><p class=muted>Optional. With an AwardWallet Business "
           "API key the app reads the KrisFlyer account balance of one connected user a few times a day, for the Points "
           "and Flight goals pages. Only the key and that user's id go out. Without it, type the balance on "
           "<a href=/points>Points</a>.</p>"]
    if note:
        out.append(f"<p class={'ok' if note[0] else 'bad'}>{e(note[1])}</p>")
    try:
        last = json.loads(cfg.get("aw_last") or "{}")
    except ValueError:
        last = {}
    if last.get("at"):
        out.append(f"<p><small>Last asked {e(str(last['at']).replace('T', ' ')[:16])}: "
                   + (f"<span class=bad>{e(last['error'])}</span>" if last.get("error") else "answered") + ".</small></p>")
    out.append("<form method=post action=/settings/awardwallet>"
               + _secret("API key", "aw_key", where["aw_key"], "Stored encrypted. Never shown again.")
               + _field("Connected user id", "aw_user", cfg["aw_user"], hint="The number of the connected user in the AwardWallet Business account.")
               + "<p><button name=action value=test>Save and test</button> <button class=quiet name=action value=save>Save</button></p></form>")
    return "".join(out)


def post_awardwallet(conn, form, person):
    """-> (ok, sentence), or None for a plain save."""
    from . import points
    pid = person["id"]
    config.save_form(conn, form, ("aw_key", "aw_user"), pid)
    conn.commit()
    if (form.get("action") or [""])[0] != "test":
        return None
    cfg = config.load(conn, pid)
    if not (cfg["aw_key"] and cfg["aw_user"]):
        return False, "Not tried: the key and the connected user id are both needed."
    try:
        got = points.aw_fetch(cfg["aw_key"], cfg["aw_user"])
    except ValueError as ex:
        return False, str(ex)
    points.aw_refresh(conn, pid, force=True)
    return True, f"Connected: the KrisFlyer account holds {got['balance']:,} miles."


def tokens_section(conn, person, made=None):
    out = ["<h2 id=tokens>Feeder and bot</h2><p class=muted>Optional. A token is shown once, when it is made; the app keeps only a "
           "fingerprint of it. It is sent as <code>Authorization: Bearer &lt;token&gt;</code>.</p>"]
    if made:
        out.append(f"<div class=hint>New {e(TOKEN_TEXT[made[0]][0].lower())}, shown this once:<br><code style='word-break:break-all'>{e(made[1])}</code></div>")
    row = conn.execute("SELECT feed_at FROM people WHERE id = %s", (person["id"],)).fetchone()
    n_feed = conn.execute("SELECT count(*) n FROM stmt_lines WHERE statement_id = %s", (f"feed-{person['id']}",)).fetchone()["n"]
    if row and row["feed_at"]:
        from . import points
        alive = points.feed_alive(conn, person["id"])
        out.append(f"<p><small>The feeder last sent something on {row['feed_at'].astimezone():%d %b %Y at %H:%M}"
                   + ("." if alive else f": more than {points.FEED_DAYS} days ago, so its miles wallet is no longer shown and the "
                      "app's own numbers are used.") + "</small></p>")
    if (row and row["feed_at"]) or n_feed or config.has_token(conn, person["id"], "feed"):
        out.append("<form method=post action=/settings/token><input type=hidden name=kind value=feed><p><button class='quiet sm' "
                   "name=action value=stop>Stop listening to the feeder</button> <small class=w>Removes its token and everything "
                   "only it supplied (its lines, miles wallet, fee list, boost balances and closing days). What statements, "
                   "Wealthfolio or typed values gave stays.</small></p></form>")
    for kind, (label, what) in TOKEN_TEXT.items():
        has = config.has_token(conn, person["id"], kind)
        out.append(f"<form method=post action=/settings/token><input type=hidden name=kind value={kind}><div class=list><div class=row>"
                   f"<span class=grow>{e(label)} <span class=chip>{'made' if has else 'none'}</span><small class=w>{e(what)}</small></span>"
                   f"<button class='quiet sm' name=action value=new>{'Replace' if has else 'Make one'}</button>"
                   + ("<button class='quiet sm' name=action value=drop>Remove</button>" if has else "") + "</div></div></form>")
    return "".join(out)


def post_token(conn, form, person):
    kind = (form.get("kind") or [""])[0]
    if kind not in config.TOKENS:
        return None
    if (form.get("action") or [""])[0] == "stop" and kind == "feed":
        from . import statements
        statements.stop_feed(conn, person)
        config.drop_token(conn, person["id"], kind)
        conn.commit()
        return None
    if (form.get("action") or [""])[0] == "drop":
        config.drop_token(conn, person["id"], kind)
        conn.commit()
        return None
    token = config.new_token(conn, person["id"], kind)
    conn.commit()
    return kind, token


# ---------------------------------------------------------------- admins: the App page ----------------------------------------------------------------
def admin_link(conn, person):
    if not config.is_admin(conn, person):
        return ""
    return ("<h2>The app</h2><p class=muted>People, the model, messages and the catalogue run, for everyone who uses this "
            "installation.</p><p><a class=btn href=/admin>App settings</a></p>")


def admin_page(conn, q, person, note=None):
    if not config.is_admin(conn, person):
        return "App", "<h1>App settings</h1><div class=panel><p>Only an admin can change these.</p></div>"
    try:
        cfg, where = config.load(conn, ""), config.where(conn, "")
    except crypto.Unavailable:
        return "App", "<h1>App settings</h1><p class=muted>Unavailable right now (encryption).</p>"
    out = ["<h1>App settings</h1>"]
    if note:
        out.append(f"<div class=hint><span class={'ok' if note[0] else 'bad'}>{'Done.' if note[0] else 'Not done.'}</span> {e(note[1])}</div>")
    elif q.get("saved"):
        out.append("<div class=hint>Saved.</div>")

    # people
    rows = conn.execute("SELECT id, display, admin FROM people ORDER BY id").fetchall()
    out.append("<h2 id=people>People</h2><p class=muted>Each person sees only their own cards, spend and settings. The id is the "
               "name the sign-in proxy sends (Remote-User). An admin can also open this page. Removing a person deletes "
               "their statements, transactions, goals and settings for good.</p>")
    for r in rows:
        out.append(
            "<form method=post action=/admin/person><div class=list style='margin-bottom:10px'><div class=row>"
            f"<input type=hidden name=id value='{e(r['id'])}'><span><code>{e(r['id'])}</code></span>"
            f"<span class=grow><input name=display value='{e(r['display'])}' aria-label=Name style='width:100%'></span>"
            f"<label class=chip><input type=checkbox name=admin value=1 {'checked' if r['admin'] else ''}> admin</label>"
            "<button class='quiet sm' name=action value=save>Save</button></div>"
            + (f"<div class=row><span class=grow><small class=w>To remove, type the id and press Remove.</small></span>"
               f"<input name=confirm placeholder='{e(r['id'])}' aria-label='Type the id to remove' style='width:9em'>"
               "<button class='quiet sm' name=action value=remove>Remove</button></div>" if r["id"] != person["id"] else "")
            + "</div></form>")
    out.append("<form method=post action=/admin/person><p style='margin-top:12px'><input name=id placeholder='id (a-z, 0-9, _)' "
               "aria-label='New id' style='width:11em'> <input name=display placeholder=Name aria-label=Name style='width:11em'> "
               "<button class=quiet name=action value=add>Add a person</button></p></form>")

    # model
    out.append(
        "<h2 id=model>Model</h2><p class=muted>Optional. Any OpenAI-compatible chat completions endpoint. It reads statement "
        "PDFs and alert mails, guesses merchant categories, answers on the Ask page and words the Advisor's paragraphs. "
        "Statement text is sent to it, so use a model that is trusted with that.</p><form method=post action=/admin/model>"
        + _field("Address", "llm_url", cfg["llm_url"], kind="url", hint="The full URL, ending in /v1/chat/completions.")
        + _field("Model name", "llm_model", cfg["llm_model"])
        + _secret("Key", "llm_key", where["llm_key"], "The key also switches the model on. An endpoint without keys still needs any value here.")
        + _field("Seconds between calls", "llm_min_gap", cfg["llm_min_gap"], hint="Keeps the app under a gateway's request limit.")
        + _switch("The endpoint is vLLM without a gateway", "llm_direct", cfg["llm_direct"] == "1", "Switches the model's thinking off in each request.")
        + "<p><button name=action value=test>Save and test</button> <button class=quiet name=action value=save>Save</button></p></form>")

    # messages
    who = "".join(f"<option value='{e(r['id'])}' {'selected' if r['id'] == cfg['notify_person'] else ''}>{e(r['display'])}</option>" for r in rows)
    out.append(
        "<h2 id=messages>Messages</h2><p class=muted>Optional. The daily summary and the alerts (a cap almost used, a minimum "
        "at risk, a fee due) for one person, sent through a Telegram bot. While sending is off they are only shown on "
        "<a href=/summary>the preview page</a>. The commands <code>notify summary</code> and <code>notify alerts</code> "
        "send them; a scheduler runs those.</p><form method=post action=/admin/messages>"
        + _secret("Telegram bot token", "telegram_token", where["telegram_token"], "From @BotFather.")
        + _field("Telegram chat id", "telegram_chat", cfg["telegram_chat"])
        + f"<p><label>About whom<br><select name=notify_person><option value=''>the first person</option>{who}</select></label></p>"
        + _switch("Send the messages", "notify", cfg["notify"] == "1")
        + "<p><button name=action value=test>Save and send a test</button> <button class=quiet name=action value=save>Save</button></p></form>")

    # outside services
    try:
        seats_last = json.loads(cfg.get("seats_at") or "{}")
    except ValueError:
        seats_last = {}
    out.append(
        "<h2 id=services>Match results and award seats</h2><p class=muted>Both optional; each needs a key of its own and "
        "sends nothing about anyone.</p>"
        "<form method=post action=/admin/services><h3 style='font-size:14px;margin:12px 0 4px'>Match results</h3><p class=muted>"
        "Some cards pay more on the day a football team wins or draws. With a free football-data.org key the results are "
        "fetched a few times a day and those rates apply to purchases on such days. Without a key these cards earn their "
        f"base rate.{' Last fetched ' + e(str(cfg.get('fixtures_at')).replace('T', ' ')[:16]) + ' UTC.' if cfg.get('fixtures_at') else ''}</p>"
        + _secret("football-data.org key", "football_key", where["football_key"])
        + "<p><button name=action value=test_football>Save and test</button> <button class=quiet name=action value=save>Save</button></p>"
        "<h3 style='font-size:14px;margin:12px 0 4px'>Dates with award seats</h3><p class=muted>With a seats.aero partner key "
        "the weekly catalogue run asks which dates from Singapore have award seats in the next year, for the flight goals. "
        "Prices always come from Singapore Airlines' chart."
        + (f" Last scan {e(str(seats_last.get('at', '')).replace('T', ' '))}: {seats_last.get('destinations', 0)} destinations, "
           f"{seats_last.get('rows', 0)} dates." if seats_last.get("at") else "") + "</p>"
        + _secret("seats.aero key", "seats_key", where["seats_key"])
        + "<p><button name=action value=test_seats>Save and test</button> <button class=quiet name=action value=scan>Save and scan now</button></p></form>")

    # catalogue
    out.append(
        "<h2 id=catalogue>Catalogue run</h2><p class=muted>How <code>catalogue</code> behaves when it refreshes the cards from "
        "public pages and checks them against the banks' documents.</p><form method=post action=/admin/catalogue>"
        + _field("Picture search address", "searx_url", cfg["searx_url"], kind="url",
                 hint="A SearXNG /search endpoint with JSON output. Empty = no card pictures are searched.")
        + _field("User-Agent", "catalogue_ua", cfg["catalogue_ua"], hint="What the run calls itself to the sites it reads. Empty = the tool's name and its repository address.")
        + _field("Seconds between requests to bank sites", "tc_pause", cfg["tc_pause"])
        + _field("Bank documents fetched per run", "tc_limit", cfg["tc_limit"])
        + _field("CardUp fee", "cardup_fee", cfg["cardup_fee"], hint="The share used when costing a sign-up bonus paid through CardUp (0.0245 = 2.45%).")
        + _switch("Also ask the model about bank documents (experimental)", "tc_model", cfg["tc_model"] == "1")
        + "<p><button>Save</button></p></form>")
    return "App", "".join(out)


def _remove_person(conn, pid):
    for table in PERSON_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE person = %s", (pid,))
    conn.execute("DELETE FROM app_settings WHERE scope = %s", (pid,))
    conn.execute("DELETE FROM people WHERE id = %s", (pid,))


def post_admin(conn, path, form, person):
    """-> None (saved) or (ok, sentence). Refuses anyone who is not an admin."""
    if not config.is_admin(conn, person):
        return False, "Only an admin can change these."
    get = lambda k: (form.get(k) or [""])[0].strip()
    action = get("action") or "save"
    if path == "/admin/person":
        pid = get("id").lower()
        if action == "add":
            if not people_mod.ID.fullmatch(pid):
                return False, "The id may hold lower-case letters, digits and _ only (at most 32)."
            if conn.execute("SELECT 1 FROM people WHERE id = %s", (pid,)).fetchone():
                return False, f"There is already a person with the id {pid}."
            conn.execute("INSERT INTO people (id, display) VALUES (%s, %s)", (pid, get("display") or pid.capitalize()))
            conn.commit()
            return True, f"{get('display') or pid.capitalize()} was added. They get in once the sign-in proxy sends {pid} as Remote-User."
        row = conn.execute("SELECT id, admin FROM people WHERE id = %s", (pid,)).fetchone()
        if not row:
            return False, "No such person."
        admins = conn.execute("SELECT count(*) n FROM people WHERE admin").fetchone()["n"]
        if action == "remove":
            if pid == person["id"]:
                return False, "Nobody removes themselves. Another admin can."
            if get("confirm").lower() != pid:
                return False, f"Not removed: type {pid} in the box next to Remove to confirm."
            if row["admin"] and admins <= 1:
                return False, "Not removed: this is the only admin."
            _remove_person(conn, pid)
            conn.commit()
            return True, f"{pid} and everything stored for them were removed."
        want_admin = bool(form.get("admin"))
        if row["admin"] and not want_admin and admins <= 1:
            return False, "Not saved: this is the only admin. Make someone else admin first."
        conn.execute("UPDATE people SET display = %s, admin = %s WHERE id = %s", (get("display") or pid.capitalize(), want_admin, pid))
        conn.commit()
        return None
    try:
        if path == "/admin/model":
            config.save_form(conn, form, ("llm_url", "llm_model", "llm_key", "llm_min_gap", "llm_direct"))
            conn.commit()
            if action == "test":
                from . import llm
                if not llm.key():
                    return False, "No test: the model needs an address and a key."
                try:
                    reply = llm.chat("Reply with the single word: ok", max_tokens=20, timeout=45)
                    return True, f"The model answered: {reply.strip()[:80] or '(an empty reply)'}"
                except Exception as ex:
                    code = getattr(ex, "code", None)
                    why = {401: "the key was refused", 403: "the key was refused", 404: "nothing answers at that address, or the model name is unknown there",
                           429: "the endpoint says too many requests"}.get(code, type(ex).__name__)
                    return False, f"The model did not answer: {why}{f' (HTTP {code})' if code else ''}."
        elif path == "/admin/messages":
            config.save_form(conn, form, ("telegram_token", "telegram_chat", "notify", "notify_person"))
            conn.commit()
            if action == "test":
                from . import notify
                if not (config.app("telegram_token") and config.app("telegram_chat")):
                    return False, "No test: the bot token and the chat id are both needed."
                try:
                    notify.telegram("sg-miles-advisor: test message. Sending works.", force=True)
                    return True, "A test message was sent to the chat."
                except Exception as ex:
                    code = getattr(ex, "code", None)
                    why = {401: "Telegram refused the bot token", 400: "Telegram refused the chat id (the bot must have been "
                           "messaged from that chat first)", 403: "the bot may not write to that chat"}.get(code, type(ex).__name__)
                    return False, f"The test message was not sent: {why}{f' (HTTP {code})' if code else ''}."
        elif path == "/admin/services":
            config.save_form(conn, form, ("football_key", "seats_key"))
            conn.commit()
            if action == "test_football":
                from . import fixtures
                if not config.app("football_key"):
                    return False, "No test: the key is missing."
                ok, said = fixtures.test(config.app("football_key"))
                if ok:
                    fixtures.refresh(conn, force=True)
                return ok, said
            if action in ("test_seats", "scan"):
                from . import awards
                if not config.app("seats_key"):
                    return False, "No test: the key is missing."
                if action == "test_seats":
                    return awards.seats_test(config.app("seats_key"))
                import threading

                def scan():
                    from . import db
                    try:
                        with db.connect() as c2:
                            awards.seats_run(c2)
                    except Exception as ex:
                        print(f"seat scan: {type(ex).__name__}", flush=True)
                threading.Thread(target=scan, daemon=True).start()
                return True, "The scan runs in the background; it takes a minute or two. Its result appears here."
        elif path == "/admin/catalogue":
            for key in ("tc_pause", "tc_limit", "cardup_fee"):
                if get(key):
                    try:
                        float(get(key))
                    except ValueError:
                        return False, f"Not saved: {get(key)} is not a number."
            config.save_form(conn, form, ("searx_url", "catalogue_ua", "tc_pause", "tc_limit", "cardup_fee", "tc_model"))
            conn.commit()
        else:
            return False, "No such page."
    except crypto.Unavailable:
        return False, "Not saved: encryption is unavailable right now."
    return None
