"""Real card pictures: image search through a SearXNG instance, pick the most card-like result, then
download, check, trim and resize it into card_images (source 'search'). Household uploads (source 'household') are never
touched, and a card whose picture someone removed (cards.image_blocked) is not searched again."""
import io
import json
import re
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageChops

from . import catalogue, config, llm


def searx():
    return config.app("searx_url")   # a SearXNG /search endpoint with JSON output; empty = no picture search


CARD_RATIO = 1.586
BANK_DOMAINS = {"dbs": "dbs.com.sg", "uob": "uob.com.sg", "ocbc": "ocbc.com", "citi": "citibank.com.sg", "hsbc": "hsbc.com.sg",
                "sc": "sc.com", "maybank": "maybank2u.com.sg", "amex": "americanexpress.com", "cimb": "cimb.com.sg",
                "boc": "bankofchina.com", "trust": "trustbank.sg", "dcs": "dcscard.com", "icbc": "icbc"}
GOOD = ("photos.cdn-moneysmart.com/credit_cards", "singsaver", "milelion.com", "mainlymiles.com", "heymax")
BAD = ("pinterest", "pinimg", "squarespace", "facebook", "instagram", "fbcdn", "tiktok", "youtube", "ytimg", "reddit",
       "shutterstock", "alamy", "istock", "gettyimages", "x.com", "twimg")


GENERIC = {"card", "credit", "the", "visa", "mastercard", "master", "american", "express", "amex", "singapore", "sg", "bank"}
BANK_WORDS = {"dbs": {"dbs", "posb"}, "uob": {"uob"}, "ocbc": {"ocbc"}, "citi": {"citi", "citibank"}, "hsbc": {"hsbc"},
              "sc": {"standard", "chartered", "stanchart", "scb"}, "maybank": {"maybank"}, "amex": {"amex"},
              "cimb": {"cimb"}, "boc": {"boc", "china", "of"}, "trust": {"trust"}, "dcs": {"dcs"}, "icbc": {"icbc"}}


def flat(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower().replace("&", "and").replace("+", "plus"))


def key_words(card):
    """The words that tell this card apart from its siblings ('spree', 'm1', 'travelone', 'lady'+'solitaire')."""
    words = re.findall(r"[a-z0-9]+", card["name"].lower().replace("+", " plus"))
    return [w for w in words if len(w) > 1 and w not in GENERIC and w not in BANK_WORDS.get(card["bank_id"], set())]


def names_match(card, text, siblings):
    """All of this card's key words appear, and no more specific sibling (a superset of our words) matches better."""
    t = flat(text)
    mine = key_words(card)
    if not mine or not all(flat(w) in t for w in mine):
        return False
    for sib in siblings:
        theirs = key_words(sib)
        if sib["id"] != card["id"] and set(mine) < set(theirs) and all(flat(w) in t for w in theirs):
            return False   # e.g. text names "Lady's Solitaire" while we look for "Lady's"
    return True


def resolution(r):
    m = re.search(r"(\d+)\s*[x×]\s*(\d+)", str(r.get("resolution") or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def score(r, card, siblings=()):
    src, page = (r.get("img_src") or "").lower(), (r.get("url") or "").lower()
    if not src.startswith("http") or any(b in src or b in page for b in BAD):
        return None
    s = 0.0
    if names_match(card, " ".join([urllib.parse.unquote(src), urllib.parse.unquote(page), r.get("title") or ""]), siblings):
        s += 4
    dom = BANK_DOMAINS.get(card["bank_id"], "")
    if dom and (dom in src or dom in page):
        s += 5
    if any(g in src or g in page for g in GOOD):
        s += 4
    if re.search(r"card[-_ ]?face|cardface|card-art|cardart", src):
        s += 3
    w, h = resolution(r)
    if w and h:
        ratio = w / h
        if ratio < 1.2 or w < 240:
            return None   # portrait, square-ish or tiny: not a card face
        s += 3 if 1.45 <= ratio <= 1.75 else (1 if ratio <= 2.0 else -1)
    return s


def search(card, siblings=()):
    q = f"{card['name']} credit card Singapore"
    url = searx() + "?" + urllib.parse.urlencode({"q": q, "categories": "images", "format": "json", "safesearch": 1})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": catalogue.ua()}), timeout=40) as r:
        res = json.load(r).get("results", [])
    ranked = []
    for x in res[:60]:
        sc = score(x, card, siblings)
        if sc is not None:
            ranked.append((sc, x))
    ranked.sort(key=lambda t: -t[0])
    return ranked


def fetch_image(url, limit=6_000_000):
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):   # a search result may name anything: no file:, ftp:, data:
        raise ValueError("not a web address")
    req = urllib.request.Request(url, headers={"User-Agent": catalogue.ua(), "Accept": "image/*"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too big")
    return data


def jpeg_small(webp):
    im = Image.open(io.BytesIO(webp)).convert("RGB")
    im.thumbnail((512, 512))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def tidy(data):
    """Open, drop a plain border, check it looks like a card, resize to 640 px wide, return WebP bytes (or None)."""
    im = Image.open(io.BytesIO(data))
    im.load()
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, im.getpixel((0, 0)))
    diff = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 18 else 0)
    box = diff.getbbox()
    if box and (box[2] - box[0]) > im.width * 0.4 and (box[3] - box[1]) > im.height * 0.4:
        im = im.crop(box)
    ratio = im.width / im.height
    if not 1.4 <= ratio <= 1.8 or im.width < 200:
        return None
    if im.width > 640:
        im = im.resize((640, round(640 / ratio)), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=88)
    return out.getvalue()


def run(conn, stats, limit=None, pause=2.0, redo=False):
    if not searx():
        print("images: no picture search address set (App page), picture search skipped", flush=True)
        return
    if redo:   # throw away every picture the search chose (household photos stay) and look again
        conn.execute("DELETE FROM card_images WHERE source = 'search'")
        conn.commit()
    cards = conn.execute("SELECT c.id, c.name, c.bank_id, b.name bank_name FROM cards c JOIN banks b ON b.id = c.bank_id WHERE c.status = 'active' AND NOT c.image_blocked AND "
                         "NOT EXISTS (SELECT 1 FROM card_images i WHERE i.card_id = c.id) ORDER BY c.depth = 'full' DESC, c.id"
                         ).fetchall()
    every = conn.execute("SELECT id, name, bank_id FROM cards WHERE status = 'active'").fetchall()
    for card in cards[:limit] if limit else cards:
        siblings = [x for x in every if x["bank_id"] == card["bank_id"]]
        try:
            ranked = search(card, siblings)
        except Exception as ex:
            stats["image search failed"] += 1
            print(f"images: search failed for {card['id']}: {type(ex).__name__}", flush=True)
            time.sleep(pause)
            continue
        vision = bool(llm.key())
        for sc, r in ranked[:6]:
            if sc < (2 if vision else 8):
                break   # without the vision check only near-certain text matches are used
            try:
                webp = tidy(fetch_image(r["img_src"]))
            except Exception:
                continue
            if not webp:
                continue
            if vision:
                v = llm.verify_card_image(jpeg_small(webp), card["name"], card["bank_name"]) or {}
                stats["vision checks"] += 1
                if not (v.get("card_front") and v.get("product_shot") and v.get("match")):
                    stats["vision rejected"] += 1
                    continue
            conn.execute("INSERT INTO card_images (card_id, mime, data, added_by, source, source_url) VALUES "
                         "(%s,'image/webp',%s,'search','search',%s) ON CONFLICT (card_id) DO NOTHING",
                         (card["id"], webp, r.get("url") or r["img_src"]))
            conn.commit()
            stats["images"] += 1
            break
        else:
            stats["no image"] += 1
        time.sleep(pause)
