"""The model, when one is configured: any OpenAI-compatible chat endpoint, set by an admin on the App page (address,
model name, key; config.py). Meant for a model the operator runs or trusts: statement text and questions go to it.
Without an address and a key every caller gets "no model" and the pages fall back to what the code alone can do."""
import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import config


def _url():
    return config.app("llm_url")          # full URL of the chat completions endpoint


def _model():
    m = config.app("llm_model")
    return {"model": m} if m else {}


def _direct():
    return config.flag("llm_direct")

# Gateways limit requests per minute, and the weekly picture check alone makes many calls. So: every call waits its turn
# (at most one every "seconds between calls" from this process), and a 429 waits for the limit to reset and tries again.


def key():
    """The API key when a model is configured (address + key), else None. An endpoint that needs no key still wants
    the key set to any value: the key is also the switch."""
    return (config.app("llm_key") or None) if _url() else None


_gap = None    # tests set a number; otherwise the stored setting is used
_turn = threading.Lock()
_last = [0.0]


def _send(req, timeout):
    """POST with pacing + patience on 429. Returns the parsed JSON, or raises the last error."""
    for attempt in range(4):
        with _turn:
            wait = (config.number("llm_min_gap") if _gap is None else _gap) - (time.monotonic() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as ex:
            if ex.code != 429 or attempt == 3:
                raise
            try:
                pause = min(65.0, float(ex.headers.get("Retry-After") or 0)) or 20.0 * (attempt + 1)
            except ValueError:
                pause = 20.0 * (attempt + 1)
            time.sleep(pause)


PROMPT = """You assign ISO 18245 merchant category codes (MCC) to Singapore merchants.
Merchant: {name}
What they sell / extra info: {info}
How it's sold: {channel}
Reply with JSON only: {{"mcc": "4-digit code", "why": "max 12 words", "confidence": "high|medium|low"}}"""


def suggest_mcc(name, info="", channel=""):
    key_ = key()
    if not key_:
        return None
    body = {**_model(), "temperature": 0, "max_tokens": 120,
            "messages": [{"role": "user", "content": PROMPT.format(name=name[:80], info=(info or "-")[:200],
                                                                    channel=channel or "unknown")}]}
    req = urllib.request.Request(_url(), data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key_})
    try:
        txt = _send(req, 30)["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", txt, re.S)
        d = json.loads(m.group(0)) if m else {}
        if re.fullmatch(r"\d{4}", str(d.get("mcc", ""))):
            return {"mcc": str(d["mcc"]), "why": str(d.get("why", ""))[:120], "confidence": str(d.get("confidence", ""))[:10]}
    except Exception as ex:
        print(f"llm suggest failed: {type(ex).__name__}", flush=True)
    return None


VERIFY = ('This should be a product picture of the Singapore credit card "{name}" ({bank}). Answer about the image: '
          '"card_front": is it the front face of a payment card? "product_shot": does the card itself fill most of the picture '
          '(not a lifestyle photo, scene, hand, banner or document)? "bank_seen" and "card_name_seen": what is printed on it. '
          '"match": is it this exact card ({name}), not a sibling card of the same bank? Reply JSON only.')


def verify_card_image(jpeg, name, bank):
    """Local vision check of a candidate card picture (public image, public card name). Returns dict or None."""
    import base64
    key_ = key()
    if not key_:
        return None
    body = {**_model(), "temperature": 0, "max_tokens": 160, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
        {"type": "text", "text": VERIFY.format(name=name, bank=bank)}]}]}
    if _direct():   # straight against vLLM: switch the chat template's thinking off
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(_url(), data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key_})
    try:
        txt = _send(req, 90)["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as ex:
        print(f"llm verify failed: {type(ex).__name__}{' ' + str(ex.code) if isinstance(ex, urllib.error.HTTPError) else ''}", flush=True)
        return None


def chat(content, max_tokens=800, timeout=240):
    """One local-model call. content = str or OpenAI content list. Returns the text, or raises."""
    key_ = key()
    if not key_:
        raise RuntimeError("no LLM key")
    body = {**_model(), "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}]}
    if _direct():
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(_url(), data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key_})
    return _send(req, timeout)["choices"][0]["message"]["content"]


def first_json(txt):
    """The first JSON object/array in a model reply (tolerates ```json fences and chatter)."""
    txt = re.sub(r"```(?:json)?", "", txt)
    starts = [i for i in (txt.find("{"), txt.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in reply")
    return json.JSONDecoder().raw_decode(txt[min(starts):])[0]


STATEMENT = """You read Singapore credit card statements. Below is the text of one statement PDF.
Return JSON only, exactly this shape:
{{"bank": "issuer name", "period_from": "YYYY-MM-DD or null", "period_to": "YYYY-MM-DD or null",
 "printed_total": number or null,
 "sections": [{{"name": "card name as printed", "last4": "last 4 digits or null",
   "lines": [{{"date": "YYYY-MM-DD", "description": "merchant text as printed", "amount": 12.34,
              "currency": "SGD", "fx_amount": null, "online": true/false/null}}]}}]}}
Rules:
- One section per card printed on the statement (combined statements list several cards).
- amount is in SGD: positive for a purchase/charge, negative for a payment, refund or credit.
- Include every transaction line. Skip balance lines, sub-totals, previous balance, minimum payment, interest summaries.
- The statement shows dates without a year: use the statement period to pick the year.
- currency = the original currency when a foreign amount is shown (fx_amount = that amount), else "SGD".
- online = true only when the description clearly is an online merchant (".COM", "AMAZON", "SHOPEE", "GRAB*" app, etc.).
- printed_total = the statement's total of new purchases/charges for the period if printed, else null.
- Also add "rewards": [{{"programme": "name as printed (UNI$, DBS Points, OCBC$, TREATS Points, ThankYou Points...)",
  "balance": number, "expiry": "text or null"}}] for every rewards / points balance printed on the statement
  (closing balance), or [] if none.

Statement text:
{text}"""


def read_statement(text):
    reply = chat(STATEMENT.format(text=text[:60000]), max_tokens=16000, timeout=600)
    return first_json(reply)


MCC_GUESS = """Assign the most likely ISO 18245 MCC to each Singapore card-statement merchant text below.
Some lines list candidate MCCs this merchant is known to use: then pick one of those.
Common Singapore examples: taxis / ComfortDelGro / Grab rides 4121, MRT/bus/SimplyGo 4111, restaurants 5812,
fast food / cafes 5814, supermarkets (FairPrice, Cold Storage) 5411, online marketplaces (Shopee, Lazada) 5262 or 5399,
Amazon retail 5999 or 5311, streaming (Netflix, Spotify) 4899, app stores / Apple.com/bill 5818, airlines 3000-3299
or 4511, hotels 7011, online travel (Agoda, Klook, Expedia) 4722, pharmacies (Watsons, Guardian) 5912, petrol 5541.
Return JSON only: {{"<merchant text>": "4-digit MCC", ...}} with every merchant text (before any " | ") as a key.
Merchants:
{names}"""


def guess_mccs(names, candidates=None):
    """Batch MCC guesses (the configured model). candidates: {merchant text: [known MCCs]} narrows the
    choice for shops seen under several MCCs; a guess outside the candidates is ignored."""
    out = {}
    candidates = candidates or {}
    names = [n for n in dict.fromkeys(names) if n]
    for i in range(0, len(names), 60):
        chunk = names[i:i + 60]
        try:
            listing = "\n".join(n + (f" | candidates: {', '.join(candidates[n][:40])}" if candidates.get(n) else "") for n in chunk)
            d = first_json(chat(MCC_GUESS.format(names=listing), max_tokens=3000))
            for k, v in d.items():
                k = k.split(" | ")[0]
                if re.fullmatch(r"\d{4}", str(v)):
                    out[k] = str(v)
        except Exception as ex:
            print(f"mcc guess failed: {type(ex).__name__}", flush=True)
    return out
