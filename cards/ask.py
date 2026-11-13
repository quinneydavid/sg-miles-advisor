"""Ask: a question in plain words, answered by the configured model (llm.py) from the
person's own figures. Nothing leaves the house, nothing is stored: the question and the answer live for one request.

The model gets facts worked out by the app (the same numbers the pages show), never the database:
  wallet    the person's cards with what each earns (base, foreign currency, every bonus with cap and minimum)
  now       this period's cap use and minimums, the card to use per kind of purchase, fees due, points  (notify.caps)
  shops     when the question names a shop we know: the app's own "which card" answer for it        (web.api_lookup)
  spend     the person's recent purchases: what was paid with, what it earned, what would have earned more
It is told to answer only from those facts and to say so when they don't cover the question."""
import datetime
import json
import re
import threading

from . import cache, crypto, llm, notify, replay

MAX_LINES = 140
BUSY = {}          # person id -> lock: one question at a time each
LOCK = threading.Lock()
EXAMPLES = ["Which card should I use for a S$1,500 Singapore Airlines ticket?", "Which caps have I nearly used up this month?",
            "What did I spend the most on last month, and did I use the right cards?", "Which card for Grab rides right now?",
            "Is any annual fee coming up?"]
PROMPT = """You answer questions about one person's Singapore credit cards inside their household card app.
Today is {today}. The person is {name}.

Rules:
- Use ONLY the facts in the JSON below. Never use card knowledge of your own: rates, caps and fees change.
- If the facts don't answer the question, say what is missing in one sentence. Do not guess.
- Say which card and why, with the numbers (miles per S$, cap left, minimum still to spend). mpd = miles per S$.
- "now.caps" is this period's real cap use; a bonus whose cap has 0 left does not apply any more this period.
- A purchase bigger than the cap left can be paid in two parts: the cap left on the best card, the rest on the next best.
- Be short: at most 120 words, plain sentences, no headings, no tables, no markdown. Amounts as S$1,234.

FACTS:
{facts}
{history}
QUESTION: {question}"""


def wallet(conn, person):
    cat = cache.catalogue(conn)
    out = []
    for cid in person["held_cards"]:
        c = cat["active"].get(cid)
        if not c:
            continue
        cash = c["kind"] == "cashback"
        rules = []
        for r in cat["rules"].get(cid, []):
            x = {"for": r["label"] or r["bucket"], ("cashback_pct" if cash else "mpd"): float(r["rate"])}
            if r.get("cap"):
                x["cap"] = f"S${float(r['cap']):,.0f} per {'statement month' if r.get('cap_period') == 'statement' else 'month'}"
            if r.get("min_spend"):
                x["minimum_spend_on_card_per_month"] = float(r["min_spend"])
            if r.get("promo_until"):
                x["until"] = r["promo_until"].isoformat()
            if r.get("conditions") and r["conditions"] != r.get("label"):
                x["note"] = str(r["conditions"])[:160]
            rules.append(x)
        out.append({"card": c["name"], "kind": c["kind"], "base_sgd": float(c["base_sgd"] or 0), "base_foreign": float(c["base_fcy"] or 0),
                    "annual_fee": float(c["annual_fee"]) if c.get("annual_fee") is not None else None, "bonuses": rules})
    return out


def shops(conn, person, question):
    """The app's own answer for every known shop the question names (longest names first, at most 3)."""
    from . import web
    rows = conn.execute("SELECT name FROM merchants WHERE length(name) >= 4 AND position(lower(name) in lower(%s)) > 0 "
                        "ORDER BY length(name) DESC LIMIT 3", (question,)).fetchall()
    out = []
    for r in rows:
        a = web.api_lookup(conn, {"q": r["name"]}, person)
        keep = lambda x: {k: x[k] for k in ("card", "rate", "unit", "why", "cap", "min_spend", "held")} if x else None
        out.append({"shop": r["name"], "mcc": a.get("mcc"), "mcc_name": a.get("mcc_name"), "paid": a.get("pay"),
                    "use": keep(a.get("use")), "then": [keep(x) for x in a.get("then") or []][:2], "assumes": a.get("assumes")})
    return out


def spend(conn, person):
    """Recent purchases (this month and the two before), newest first, and the spend per card per month."""
    today = datetime.date.today()
    months = sorted({(today.replace(day=1) - datetime.timedelta(days=d)).strftime("%Y-%m") for d in (0, 28, 59)})
    lines = cache.lines(conn, person["id"], months)
    if not lines:
        return None
    base, allowed = replay.load(conn, person["excluded_banks"], person["boost_amounts"], person["statement_days"], replay.in_store(person),
                                  person["held_cards"], person.get("cashback_too"))
    held = list(person["held_cards"])
    res, tot, over = cache.memo(conn, person["id"], ("ask", tuple(months), cache.person_key(person)),
                                lambda: replay.run(base, lines, held, held))
    name = lambda cid: base.cards[cid]["name"] if cid in base.cards else (cid or "unknown card")
    per = {}
    for x in res:
        k = f"{x['date']:%Y-%m} {name(x.get('card_id'))}"
        per[k] = round(per.get(k, 0) + x["amount"], 2)
    rows = []
    for x in sorted(res, key=lambda x: x["date"], reverse=True)[:MAX_LINES]:
        row = {"date": x["date"].isoformat(), "shop": x["merchant"][:32], "S$": round(x["amount"], 2), "card": name(x.get("card_id")),
               "miles": round(x["actual"])}
        ra = x.get("real_alts") or []
        if ra and ra[0][1] != x.get("card_id") and ra[0][0] - x["actual"] >= 1:
            row["better_on_the_day"] = f"{name(ra[0][1])} (+{ra[0][0] - x['actual']:.0f} miles)"
        if x.get("online"):
            row["online"] = True
        if x.get("fcy"):
            row["foreign"] = True
        rows.append(row)
    return {"months": months, "spend_per_month_and_card": per, "purchases_newest_first": rows,
            "caps_gone_over": [{"card": name(o["card"]), "bonus": o["label"], "on": o["hit"].isoformat(),
                                "spend_over_cap": round(o["over_spend"], 2), "miles_lost": round(o["lost"])} for o in over[:8]]}


def facts(conn, person, question):
    now = notify.caps(conn, person)
    for k in ("at", "person"):
        now.pop(k, None)
    out = {"wallet": wallet(conn, person), "now": now, "shops_named": shops(conn, person, question)}
    try:
        out["spend"] = spend(conn, person)
    except crypto.Unavailable:
        out["spend"] = None
    return out


def earlier(history):
    """The last few turns of this chat (kept only in the person's browser), so a follow-up like "and for S$2,000?" works."""
    turns = []
    for t in (history or [])[-3:]:
        if isinstance(t, dict) and t.get("q") and t.get("a"):
            turns.append(f"Earlier question: {str(t['q'])[:400]}\nYour earlier answer: {str(t['a'])[:900]}")
    return ("\nEARLIER IN THIS CHAT (for context only; the facts above are newer):\n" + "\n".join(turns) + "\n") if turns else ""


def answer(conn, person, question, history=None):
    """(text, None) or (None, why not)."""
    question = re.sub(r"\s+", " ", question or "").strip()[:400]
    if len(question) < 3:
        return None, "Type a question first."
    with LOCK:
        lock = BUSY.setdefault(person["id"], threading.Lock())
    if not lock.acquire(blocking=False):
        return None, "Your last question is still being answered. Give it a moment."
    try:
        blob = json.dumps(facts(conn, person, question), ensure_ascii=False, default=str, separators=(",", ":"))
        text = llm.chat(PROMPT.format(today=f"{datetime.date.today():%A %d %B %Y}", name=person["display"], facts=blob,
                                      history=earlier(history), question=question), max_tokens=450, timeout=120)
        text = re.sub(r"[*#`]+", "", (text or "")).strip()
        return (text[:2500], None) if text else (None, "The model gave no answer. Try again.")
    except RuntimeError:
        return None, "The model isn't set up for this app."
    except Exception as ex:
        print(f"ask: {type(ex).__name__}", flush=True)
        return None, "The model didn't answer in time. Try again in a minute."
    finally:
        lock.release()


def api(conn, person, form):
    """POST /ask/api from the chat: {answer} or {error}. h = the browser's own copy of the last turns (JSON)."""
    try:
        hist = json.loads((form.get("h") or ["[]"])[0])[:6]
    except (ValueError, TypeError):
        hist = []
    text, err = answer(conn, person, (form.get("q") or [""])[0], hist if isinstance(hist, list) else [])
    return {"answer": text} if text else {"error": err}


def page(conn, q, person, asked=None):
    """A chat window. With scripts: bubbles, a typing indicator while the model works, follow-up questions; the thread
    lives in the browser tab only. Without scripts the form posts and the answer comes back in the page."""
    from .web import e
    first = ("Ask me about your cards, caps, fees or spend. I answer from your own figures in this app, using the model "
             "this app is set up with. The conversation is not stored.")
    thread = f"<div class='msg bot'><p>{e(first)}</p></div>"
    if asked:   # no-script fallback
        text, err = answer(conn, person, asked)
        thread += f"<div class='msg me'><p>{e(asked[:400])}</p></div>"
        paras = "".join(f"<p>{e(p)}</p>" for p in re.split(r"\n\s*\n|\n", text or err) if p.strip())
        thread += f"<div class='msg bot{'' if text else ' err'}'>{paras}</div>"
    chips = "".join(f"<button type=button class=chip data-ask='{e(x)}'>{e(x)}</button>" for x in EXAMPLES)
    body = ("<div class=chat id=chat><div class=chathead><b>Ask</b><span class=muted>your figures only</span>"
            "<button type=button class='quiet sm' id=chatclear hidden>New chat</button></div>"
            f"<div class=thread id=thread aria-live=polite>{thread}</div>"
            f"<div class=suggest id=suggest>{chips}</div>"
            "<form class=composer method=post action=/ask id=composer><textarea name=q rows=1 maxlength=400 required "
            "placeholder='Ask about your cards…' aria-label='Your question'></textarea>"
            "<button aria-label=Send>Send</button></form>"
            "<p class=muted><small>It can misread figures: for a number that matters, check Pay, Wallet or Spend.</small></p></div>")
    return "Ask", body
