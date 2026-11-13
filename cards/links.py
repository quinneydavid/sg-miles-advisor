"""Where to read more about a card: the bank's own card page and MileLion's review.

  reviews     MileLion's review index names each review; a title is matched to our cards by its words (exact name first).
              A card without a review gets a MileLion search link on its page instead (made in web.py, nothing stored).
  bank pages  each bank's public card listing is read once; a link on the bank's own domain whose text or address carries
              every word of the card's name becomes cards.url. A listing that refuses us is left alone.

Nothing here changes rates or rules. cards.url also gives the T&C check (tc.py) a card page to look for terms on."""
import html
import re
import time
import urllib.parse

from . import tc

REVIEWS = "https://milelion.com/credit-cards/credit-card-reviews/"
REVIEW_LINK = re.compile(r'<a\b[^>]*href="(https://milelion\.com/(20\d\d/\d\d/\d\d)/[^"#?]*review[^"#?]*)"[^>]*>(.*?)</a>', re.I | re.S)
ALIAS = (("stanchart", "standard chartered"), ("amex", "american express"), ("citibank", "citi"), ("women", "woman"),
         ("ladys", "lady"), ("womans", "woman"), ("mastercard", "mc"), ("master card", "mc"))
NETWORK = {"visa", "mc", "american", "express", "signature", "world", "credit"}   # a card may add these to a review's name
NOISE = {"card", "cards", "the", "singapore", "airlines", "and", "s", "sg", "personal", "index", "page", "html",
         "en", "www", "com", "https", "default", "rewards-cards", "apply", "now", "learn", "more", "find", "out", "view"}
PAUSE = 2.0


def words(text):
    t = html.unescape(str(text or "")).lower().replace("’", "'").replace("'s", "").replace("'", "")
    t = re.sub(r"[^a-z0-9°+]+", " ", t)
    for a, b in ALIAS:
        t = re.sub(rf"\b{a}\b", b, t)
    return frozenset(w for w in t.split() if w not in NOISE)


def reviews(page):
    """[(title, url, 'YYYY/MM/DD')] from the review index, one per URL."""
    seen = {}
    for url, date, inner in REVIEW_LINK.findall(page):
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        if title and url not in seen:
            seen[url] = (title, url, date)
    return list(seen.values())


def match_reviews(cards, found):
    """{card id: (url, title)}. A review belongs to a card when one of its names (a title can name two cards, joined by
    '&') has the card's words, give or take the network ("DBS Altitude" covers both Altitude cards) and the word
    "credit". Any other extra word on either side (Reserve, Charge, Solitaire, World) is a different card. The closest
    name wins, then the newest review."""
    out = {}
    exact = {words(c["name"]) for c in cards}
    for c in cards:
        cw = words(c["name"])
        best = None
        for title, url, date in found:
            for part in re.split(r"\s*(?:&|/|\band\b)\s*", title):
                rw = words(part)
                if len(rw & cw) < 2 or not (cw - rw <= NETWORK) or not (rw - cw <= {"credit"}):
                    continue
                if rw != cw and rw in exact:
                    continue   # another card carries exactly this name (Platinum Credit is not Platinum)
                key = (-len(cw ^ rw), date)
                if best is None or key > best[0]:
                    best = (key, url, title)
        if best:
            out[c["id"]] = (best[1], best[2])
    return out


def match_pages(cards, links):
    """{card id: url} from a bank listing's links [(url, text)]: every word of the card's name (bank name aside) must be
    in the link's text or address; the link with the fewest other words wins, and it must win alone."""
    out = {}
    for c in cards:
        bank = words(c["bank"]) | words(c["bank_id"])
        bank = bank | {"credit"}
        cw = words(c["name"]) - bank
        if not cw:
            continue
        scored = {}
        for url, text in links:
            path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
            if re.search(r"\.(pdf|jpe?g|png|svg|webp)$|debit|prepaid|/(promotions?|fees?|faq|help|support|apply|application)\b", path, re.I):
                continue
            if "card" not in path.lower():
                continue   # a savings account or a loan can carry the card's name (KrisFlyer UOB account)
            for lw in (words(text) - bank, words(path) - bank):
                if cw <= lw:
                    extra = len(lw - cw)
                    scored[url] = min(scored.get(url, 99), extra)
        if not scored:
            continue
        low = min(scored.values())
        top = sorted(u for u, s in scored.items() if s == low)
        if low <= 2 and len(top) == 1:
            out[c["id"]] = top[0]
    return out


def run(conn, stats):
    from . import catalogue
    cards = conn.execute("SELECT c.id, c.name, c.bank_id, c.url, b.name bank, b.listing_url FROM cards c JOIN banks b ON "
                         "b.id = c.bank_id WHERE c.status = 'active' ORDER BY c.id").fetchall()
    try:
        status, data = catalogue.fetch(REVIEWS)
        found = reviews(data.decode("utf-8", "replace"))
        if len(found) < 10:
            raise ValueError(f"only {len(found)} reviews on the index")
        for cid, (url, title) in match_reviews(cards, found).items():
            conn.execute("UPDATE cards SET review_url = %s, review_title = %s WHERE id = %s AND review_url IS DISTINCT FROM %s",
                         (url, title[:200], cid, url))
            stats["reviews linked"] += 1
    except Exception as ex:   # the stored links stay
        stats["review index not read"] += 1
        print(f"links: review index: {type(ex).__name__}: {ex}", flush=True)
    conn.commit()
    by_bank = {}
    for c in cards:
        by_bank.setdefault(c["bank_id"], []).append(c)
    for bank_id, mine in sorted(by_bank.items()):
        listing = mine[0]["listing_url"]
        if not listing or not tc.allowed(bank_id, listing) or all(c["url"] for c in mine):
            continue
        try:
            _, data, ctype = tc.fetch_doc(listing, bank_id)
        except Exception as ex:
            stats["bank listings not read"] += 1
            print(f"links: {bank_id} listing: {type(ex).__name__}", flush=True)
            time.sleep(PAUSE)
            continue
        links = []
        for href, inner in tc.LINK.findall(data.decode("utf-8", "replace")):
            url = tc.clean_url(urllib.parse.urljoin(listing, html.unescape(href.strip())))
            if url and tc.allowed(bank_id, url):
                links.append((re.sub(r"\?intlink=[^&]*$", "", url), re.sub(r"<[^>]+>", " ", inner)))
        for cid, url in match_pages([c for c in mine if not c["url"]], links).items():
            conn.execute("UPDATE cards SET url = %s WHERE id = %s AND url IS NULL", (url, cid))
            stats["bank pages linked"] += 1
        conn.commit()
        time.sleep(PAUSE)
