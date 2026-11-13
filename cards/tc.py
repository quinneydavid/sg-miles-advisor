"""Check the catalogue against the banks' OWN terms and conditions.

  A. discover   per card: cards.tc_url, the PDFs the wiki index names (card_tc_mccs.source_url) and terms-looking links
                on the bank's card page. Only URLs on the card's bank's own domains are kept (https, exact host suffix);
                blogs, wikis and aggregators are never read here.
  B. fetch      one document at a time with a pause, no retries; a refused request is recorded as 'blocked' and left
                alone. PDF/HTML -> text in tc_docs.body, sha256 so a changed document is noticed and logged.
  C. check      every catalogue fact (base rates, fee, exclusions, each rule's rate / cap / minimum spend / MCC list) is
                looked up in the card's documents and written to card_tc_checks with the sentence it rests on.
                'pattern' = deterministic regexes (always on). 'model' = the model, only with a model set, and
                only believed when the sentence it quotes really is in the document.

The checker reports. It never edits rates, caps or MCC lists: a difference is a row + one catalogue_changes line."""
import datetime
import gzip
import hashlib
import html
import io
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from psycopg.types.json import Jsonb

from . import config, llm, store



def pause():
    time.sleep(config.number("tc_pause"))        # seconds between requests to bank sites
MAX_BYTES = 15_000_000
TIMEOUT = 60
QUOTE_MAX = 300
KINDS = ("tc", "exclusions", "faq", "promo", "fees")

# The bank's own sites, per bank id. A host is accepted when it equals one of these or ends with "." + one of them.
BANK_HOSTS = {
    "dbs": ("dbs.com.sg", "dbs.com", "posb.com.sg"),
    "uob": ("uob.com.sg", "uobgroup.com"),
    "ocbc": ("ocbc.com", "ocbc.com.sg", "frankbyocbc.com"),
    "citi": ("citibank.com.sg",),
    "hsbc": ("hsbc.com.sg",),
    "sc": ("sc.com",),
    "maybank": ("maybank2u.com.sg", "maybank.com.sg"),
    "amex": ("americanexpress.com",),
    "cimb": ("cimb.com.sg",),
    "boc": ("bankofchina.com",),
    "trust": ("trustbank.sg",),
    "dcs": ("dcscc.com", "dcscard.com"),
    "icbc": ("icbc.com.sg", "icbc.com.cn"),
}
# The bank's own fee schedule (annual fees + waiver rules), linked to every card of the bank. Found by hand; a page that
# moves simply fails to fetch and the fee checks fall back to the card's own terms.
FEE_PAGES = {
    "uob": ("https://www.uob.com.sg/personal/cards/services/annual-fees.page",),
    "dbs": ("https://www.dbs.com.sg/personal/support/card-charges-and-fees-annual-fee.html",),
    "sc": ("https://www.sc.com/sg/pricing-guide/",),
    "maybank": ("https://www.maybank2u.com.sg/en/bank-charges/cc/credit-cards.page",),
    "citi": ("https://www.citibank.com.sg/content/cgcpc/sg/prelogin/www-citibank-com-sg/fees-and-rates.html",),
    "dcs": ("https://dcscc.com/legal/credit-card-fees",),
}
TRACKING = re.compile(r"^(i_cid|vid|adobe_mc|utm_\w+|gclid|fbclid|cid|icid|pid|intcid)$", re.I)


def bank_hosts(bank_id):
    """images.BANK_DOMAINS (when it holds a real domain) + the extras above."""
    hosts = set(BANK_HOSTS.get(bank_id, ()))
    try:
        from .images import BANK_DOMAINS
        dom = BANK_DOMAINS.get(bank_id, "")
        if "." in dom:
            hosts.add(dom)
    except Exception:   # Pillow missing in a slim test environment: the list above is enough
        pass
    return hosts


def allowed(bank_id, url):
    """True only for an https URL whose host is the bank's own domain or a subdomain of it."""
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if u.scheme != "https" or not host or u.username or (u.port not in (None, 443)):
        return False
    return any(host == d or host.endswith("." + d) for d in bank_hosts(bank_id))


def clean_url(url):
    """One spelling per document: entities decoded, http -> https, tracking parameters and fragments dropped."""
    url = html.unescape((url or "").strip()).replace(" ", "%20")
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    query = "&".join(p for p in u.query.split("&") if p and not TRACKING.match(p.split("=")[0]))
    if u.path.lower().endswith(".pdf"):
        query = ""   # a PDF is the same file whatever the campaign parameters say
    return urllib.parse.urlunsplit(("https", u.netloc.lower(), u.path, query, ""))


def guess_kind(url, title=""):
    t = urllib.parse.unquote(f"{url} {title or ''}").lower()
    if re.search(r"exclu", t):
        return "exclusions"
    if re.search(r"faq|frequently", t):
        return "faq"
    if re.search(r"\bfees?\b|fee[-_ ]|charges?\b|pricing|tariff", t):
        return "fees"
    if re.search(r"promo|campaign|cmpg|offer|sign-?up|welcome|gift|acq|launch|spend-and-get|lucky|ntb", t):
        return "promo"
    return "tc"


# --- A. discover -----------------------------------------------------------------------------------------------------

LINK = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
TERMS_LINK = re.compile(r"terms|t\s*&(?:amp;)?\s*cs?\b|tn?cs?\b|tnc|exclusion|rewards?[-_ ]program|conditions|"
                        r"cardmember[-_ ]agreement|fees?[-_ ]and[-_ ]charges", re.I)


def page_links(page, base):
    """Terms-looking links on a bank card page: [(absolute url, link text)]. PDFs, or pages whose own text says terms."""
    out = {}
    for href, inner in LINK.findall(page):
        label = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        url = clean_url(urllib.parse.urljoin(base, html.unescape(href.strip())))
        if not url:
            continue
        path = urllib.parse.urlsplit(url).path.lower()
        if path.endswith(".pdf") and (TERMS_LINK.search(path) or TERMS_LINK.search(label)):
            out.setdefault(url, label[:200])
        elif TERMS_LINK.search(label) and re.search(r"terms|conditions|exclusion", path):
            out.setdefault(url, label[:200])
    return list(out.items())


def add_doc(conn, stats, card, url, via, title=None, kind=None):
    url = clean_url(url)
    if not url:
        return False
    if not allowed(card["bank_id"], url):
        stats["links not on the bank's site (skipped)"] += 1
        return False
    conn.execute("INSERT INTO tc_docs (url, bank_id, title) VALUES (%s,%s,%s) ON CONFLICT (url) DO NOTHING",
                 (url, card["bank_id"], title))
    n = conn.execute("INSERT INTO card_tc_docs (card_id, url, kind, found_via) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (card["id"], url, kind or guess_kind(url, title), via)).rowcount
    stats["document links"] += n
    return True


def discover(conn, stats, only=None):
    cards = conn.execute("SELECT id, bank_id, name, url, tc_url FROM cards WHERE status = 'active' AND (%s::text IS NULL "
                         "OR id = %s) ORDER BY id", (only, only)).fetchall()
    pages = {}
    for card in cards:
        if card["tc_url"]:
            add_doc(conn, stats, card, card["tc_url"], "wiki index (first stored T&C)")
        for r in conn.execute("SELECT DISTINCT source_url FROM card_tc_mccs WHERE card_id = %s", (card["id"],)).fetchall():
            add_doc(conn, stats, card, r["source_url"], "wiki index (T&C MCC source)")
        for url in FEE_PAGES.get(card["bank_id"], ()):
            add_doc(conn, stats, card, url, "bank fee schedule", kind="fees")
        page = clean_url(card["url"]) if card["url"] else None
        if page and allowed(card["bank_id"], page):
            if page not in pages:
                try:
                    status, data, ctype = fetch_doc(page, card["bank_id"])
                    pages[page] = page_links(data.decode("utf-8", "replace"), page) if "pdf" not in ctype else []
                except Exception as ex:
                    pages[page] = []
                    stats["card pages not read"] += 1
                    print(f"tc: card page {page}: {type(ex).__name__}", flush=True)
                pause()
            for url, label in pages[page]:
                add_doc(conn, stats, card, url, "bank card page", label or None)
        conn.commit()
    return cards


# --- B. fetch --------------------------------------------------------------------------------------------------------

class Blocked(Exception):
    """The bank's site refused an automated request. We do not work around it."""


class _SameBank(urllib.request.HTTPRedirectHandler):
    """A redirect may only lead to the same bank's own domain (never to a CDN login, a partner or a tracker)."""

    def __init__(self, bank_id):
        self.bank_id = bank_id

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(self.bank_id, newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect leaves the bank's site", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


BLOCK_PAGE = re.compile(r"access denied|request unsuccessful|incapsula|captcha|are you a robot|unusual traffic|"
                        r"attention required|pardon our interruption|request blocked|errors\.edgesuite", re.I)


def fetch_doc(url, bank_id):
    """(http status, bytes, content type). Raises Blocked for a refusal, ValueError for an oversized body."""
    from . import catalogue   # late: catalogue registers this module as a source
    req = urllib.request.Request(url, headers={"User-Agent": catalogue.ua(), "Accept-Encoding": "gzip",
                                               "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"})
    opener = urllib.request.build_opener(_SameBank(bank_id))
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("document larger than 15 MB")
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            ctype = (r.headers.get("Content-Type") or "").lower()
            status = r.status
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403, 406, 429, 451, 503):
            raise Blocked(f"HTTP {ex.code}") from None
        raise
    if not data.startswith(b"%PDF") and len(data) < 20000 and BLOCK_PAGE.search(data[:20000].decode("utf-8", "replace")):
        raise Blocked("block page")
    return status, data, ctype


def tidy(text):
    """Stored text: no NULs, spaces collapsed inside a line, lines kept (tables stay row by row)."""
    text = text.replace("\x00", "").replace(" ", " ").replace("­", "")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_text(page):
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", page)
    page = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h\d|/td|/th)\b[^>]*>", "\n", page)
    return tidy(html.unescape(re.sub(r"<[^>]+>", " ", page))), (tidy(html.unescape(title.group(1))) if title else None)


def extract(data, ctype=""):
    """bytes -> (text, pages, title). pages is None for HTML. Raises on a PDF pypdf cannot open."""
    if data.startswith(b"%PDF") or (b"%PDF" in data[:1024] and "html" not in ctype):
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted and not r.decrypt(""):
            raise ValueError("PDF is password-protected")
        text = tidy("\n".join((p.extract_text() or "") for p in r.pages))
        title = None
        try:
            title = (r.metadata.title or "").strip() or None if r.metadata else None
        except Exception:
            pass
        if not title or re.fullmatch(r"(?i)(microsoft word - .*|untitled|document\d*|.*\.(docx?|pdf|indd))", title):
            title = next((ln for ln in text.split("\n") if len(ln) > 8), None)
        return text, len(r.pages), (title or "")[:200] or None
    text, title = html_text(data.decode("utf-8", "replace"))
    return text, None, (title or "")[:200] or None


def fetch_all(conn, stats, limit, only=None, force=False):
    """Fetch up to `limit` documents (never-fetched first, then the oldest). Returns the URLs whose text changed."""
    docs = conn.execute(
        "SELECT d.* FROM tc_docs d WHERE (%s::text IS NULL OR EXISTS (SELECT 1 FROM card_tc_docs l WHERE l.url = d.url AND "
        "l.card_id = %s)) ORDER BY d.fetched_at NULLS FIRST, d.url LIMIT %s", (only, only, limit)).fetchall()
    changed = set()
    for i, d in enumerate(docs):
        if i:
            pause()
        url = d["url"]
        try:
            if not allowed(d["bank_id"], url):
                raise Blocked("not the bank's own site")
            status, data, ctype = fetch_doc(url, d["bank_id"])
            is_pdf = data.startswith(b"%PDF")
            text = pages = title = None
            if not is_pdf:   # a web page carries tokens and timestamps: hash what a person would read
                text, pages, title = extract(data, ctype)
                sha = hashlib.sha256(text.encode()).hexdigest()
            else:
                sha = hashlib.sha256(data).hexdigest()
            if sha == d["sha256"] and d["status"] == "ok" and d["body"] and not force:
                conn.execute("UPDATE tc_docs SET fetched_at = now(), http_status = %s, error = NULL WHERE url = %s", (status, url))
                stats["documents unchanged"] += 1
                conn.commit()
                continue
            if is_pdf:
                text, pages, title = extract(data, ctype)
            ok = len(text) >= 200
            conn.execute("UPDATE tc_docs SET status = %s, http_status = %s, sha256 = %s, pages = %s, body = %s, title = "
                         "COALESCE(%s, title), fetched_at = now(), changed_at = CASE WHEN sha256 IS DISTINCT FROM %s THEN "
                         "now() ELSE changed_at END, error = %s WHERE url = %s",
                         ("ok" if ok else "unreadable", status, sha, pages, text if ok else None, title, sha,
                          None if ok else "no text in the document (scanned image?)", url))
            stats["documents read" if ok else "documents unreadable (no text)"] += 1
            if d["sha256"] and d["sha256"] != sha:
                for l in conn.execute("SELECT card_id FROM card_tc_docs WHERE url = %s", (url,)).fetchall():
                    store.log(conn, l["card_id"], "T&C document changed", d["sha256"][:12], sha[:12], url)
                stats["documents changed"] += 1
            if ok:
                changed.add(url)
        except Blocked as ex:
            conn.rollback()
            conn.execute("UPDATE tc_docs SET status = 'blocked', fetched_at = now(), error = %s WHERE url = %s", (str(ex)[:200], url))
            stats["documents blocked"] += 1
        except Exception as ex:
            conn.rollback()
            code = ex.code if isinstance(ex, urllib.error.HTTPError) else None
            conn.execute("UPDATE tc_docs SET status = 'failed', http_status = %s, fetched_at = now(), error = %s WHERE url = %s",
                         (code, f"{type(ex).__name__}: {ex}"[:200], url))
            stats["documents failed"] += 1
        conn.commit()
    return changed


# --- C. check: reading a document ------------------------------------------------------------------------------------

BOUNDARY = re.compile(r"(?<=[.;!?])[ \n]+(?=[A-Z(\"“‘\[])|\n\s*\n|\n(?=(?:[•●▪■◦○*–-]|\(?[a-zA-Z0-9]{1,3}[.)]|\d+(?:\.\d+)+\.?) )")
MCC_WORD = re.compile(r"\bMCCs?\b|merchant\s+categor(?:y|ies)\s+codes?", re.I)
CODE = re.compile(r"(?<![\d$.#:-])(\d{4})(?:\s*(?:-|–|—|to|through|till|until)\s*(?:MCCs?\s*)?(\d{4}))?(?!\d|\.\d|\s?%)")
MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
NOT_CODE_BEFORE = re.compile(rf"(?:S\$|SGD|USD|US\$|\$|\+65|\b1800|\b(?:{MONTHS})[a-z]*\.?,?|\b(?:tel|fax|call|hotline|clause|section|"
                             rf"paragraph|page|version|ver|no|reg|year|since|before|after|by|until|from)\b\.?:?)\s*$", re.I)
NOT_CODE_AFTER = re.compile(r"\s*(?:points?|miles?|dollars?|hrs?|hours|am\b|pm\b|times|days|months|transactions)\b", re.I)
EXCLUDE_CUE = re.compile(
    r"(exclu(?:de[sd]?|ding|sions?))|\bnot\s+(?:be\s+)?(?:eligible|earn|awarded|qualify|qualified|applicable|included?|entitled|"
    r"accrue|considered|count(?:ed)?|valid|constitute|apply)|will\s+not|shall\s+not|do(?:es)?\s+not|no\s+longer|\bno\s+(?:\w+\s+){0,3}"
    r"(?:points?|miles?|cashback|rebates?|rewards?)\s+(?:will|shall|are|is)|ineligible|non-(?:qualifying|eligible)|"
    r"other\s+than", re.I)
# neutral wording ('classified under', 'fall within', 'include') decides nothing: both kinds of list use it
INCLUDE_CUE = re.compile(r"eligible|qualif(?:y|ying|ies|ied)|will\s+earn|\bearns?\b|bonus|awarded|\bmeans?\b|defined\s+as|"
                         r"\brefers?\s+to|shall\s+include|selected\s+categor", re.I)
POINTS_AT_LIST = re.compile(r"following|\bMCCs?\b|merchant\s+categor|:", re.I)
RESTART = re.compile(r"\brefers?\s+to|\bmeans?\b|defined\s+as|\bfollowing\b", re.I)
ENUM = re.compile(r"(?:^|\n)(\(?)([a-z]{1,5})([.)]) ")
ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii"}
TURN_EXCLUDE = re.compile(r"exclud(?:e[sd]?|ing)|\bnot\s+(?:be\s+)?(?:eligible|awarded|earn|qualify)|will\s+not|shall\s+not|"
                          r"do(?:es)?\s+not|no\s+longer", re.I)
TURN_INCLUDE = re.compile(r"will\s+earn|eligible|qualif\w+|bonus|awarded", re.I)
PHONE_CUE = re.compile(r"call|tel\b|telephone|hotline|contact|phone|fax|\+65|\b1800\b|\d{3,4}\s+or\s*$", re.I)
GAP = 220          # characters allowed between two codes of one list (a table row's description fits)
LEAD_FAR = 1200    # ... and how far for a definition ('"Dining" means ...') a few sentences up
LEAD = 420         # how far back the sentence / heading that governs a list is looked for


class Doc:
    """One fetched document, read once: sentence spans + MCC lists. `flat` = body with newlines as spaces (same
    offsets), so a pattern can run across PDF line breaks while a quote stays an exact slice of the stored body."""

    def __init__(self, url, body, kind="tc", title=None):
        self.url, self.body, self.kind, self.title = url, body or "", kind, title
        self.flat = self.body.replace("\n", " ")
        cuts = [0] + [m.end() for m in BOUNDARY.finditer(self.body)] + [len(self.body)]
        self.spans = [(a, b) for a, b in zip(cuts, cuts[1:]) if self.body[a:b].strip()]
        self._lists = self._names = self._norm = None

    def span_at(self, pos):
        lo, hi = 0, len(self.spans) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.spans[mid][0] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return self.spans[lo] if self.spans else (0, 0)

    def quote(self, a, b=None, exact=False):
        """The sentence around [a, b) as shown to people: at most QUOTE_MAX characters, whole words, whitespace collapsed.
        It is a run of the document's own words (verbatim once whitespace is normalised)."""
        b = a + 1 if b is None else b
        s, e = (a, b) if exact else self.span_at(a)     # exact: a table cell is its own sentence
        if b - s > QUOTE_MAX or e <= a:      # the interesting part does not fit from the sentence start: start at it
            s = a
            while s > 0 and not self.flat[s - 1].isspace() and a - s < 40:
                s -= 1
        e = max(min(e, s + QUOTE_MAX), min(b, s + QUOTE_MAX))
        if e < len(self.flat) and not self.flat[e].isspace() and not self.flat[e - 1].isspace():
            cut = self.flat.rfind(" ", s + 1, e)   # never stop inside a word
            e = cut if cut > s else e
        return " ".join(self.flat[s:e].split())

    @property
    def norm(self):
        if self._norm is None:
            self._norm = " ".join(self.body.split())
        return self._norm

    @property
    def names(self):
        """Where the text names a card product: [(position, name words)], in order."""
        if self._names is None:
            self._names = [(m.start(), n, networks(m.group(0))) for m in NAMING.finditer(self.flat)
                           for n in [name_words(m.group(1))] if n]
        return self._names

    @property
    def lists(self):
        if self._lists is None:
            self._lists = mcc_lists(self)
        return self._lists


def expand(a, b=None):
    if b is None:
        return [a]
    return [f"{i:04d}" for i in range(int(a), int(b) + 1)]


def code_tokens(flat):
    """Every 4-digit code / range that can be an MCC: (start, end, [codes]). Years, money, phone numbers are dropped."""
    out = []
    for m in CODE.finditer(flat):
        a, b = m.group(1), m.group(2)
        if b and not (int(a) < int(b) <= int(a) + 1500):
            b = None
        if not b and m.group(2):   # "2024 - 2025" and other non-ranges: neither number is a code
            continue
        if 1900 <= int(a) <= 2099 and not b:
            continue               # a year; no MCC lives in 1900-2099
        before, after = flat[max(0, m.start() - 24):m.start()], flat[m.end():m.end() + 16]
        if (before.endswith("/") and not re.search(r"\d{4}/$", before)) or (after.startswith("/") and not re.match(r"/\d{4}\b", after)):
            continue               # part of a date, a path or a reference number; '5811/5812' stays
        if re.search(r"\d,$", before) and not re.search(r"(?<!\d)\d{4},$", before) or re.match(r",\d", after) and not re.match(
                r",\d{4}(?!\d)", after):
            continue               # a thousands separator ('12,5000' never, '1,0000'); '5811,5812,5814' stays
        if NOT_CODE_BEFORE.search(before) or NOT_CODE_AFTER.match(after):
            continue
        if not b and PHONE_CUE.search(flat[max(0, m.start() - 45):m.start()]) and (
                re.search(r"\b\d{3,4}\s$", before) or re.match(r"\s\d{3,4}\b(?!\s*[A-Za-z(])", after)):
            continue               # a phone number: '1800 222 2121', 'call 6222 2121'
        out.append((m.start(), m.end(), expand(a, b)))
    return out


def cue_kind(text, to_list=True):
    """'exclude' / 'include' / None for a sentence or heading that introduces a list. Exclusion wording wins ('the
    following are not eligible' contains 'eligible'), except an aside that is about something else: 'tagged with the
    following MCCs, excluding hotel dining.' or '(excluding hotel dining) which fall under the following MCC:' exclude
    hotel dining, not the list. to_list: the text runs right up to the first code."""
    for m in EXCLUDE_CUE.finditer(text):
        if not m.group(1):
            return "exclude"
        before, tail = text[:m.start()], text[m.end():]
        if before.count("(") > before.count(")") and ")" in tail[:-1]:
            continue   # inside brackets that close before the list
        clause = re.split(r"[.;]\s", tail, maxsplit=1)   # what the excluding is about ends with its sentence
        if (to_list and len(clause) == 1 and len(tail) <= 25) or POINTS_AT_LIST.search(clause[0]):
            return "exclude"
    return "include" if INCLUDE_CUE.search(text) else None


def nearest_cue(doc, lo, hi):
    """The sentence nearest to `hi` (looking back to `lo`) that says exclude / include: (kind, span) or (None, None)."""
    cuts = [lo] + [m.end() for m in BOUNDARY.finditer(doc.body, lo, hi)] + [hi]
    for a, b in reversed(list(zip(cuts, cuts[1:]))):
        kind = cue_kind(doc.flat[a:b], to_list=False) if b - a > 3 else None
        if kind:
            return kind, (a, b)
    return None, None


def enum_kind(doc, start):
    """A list under '(s) any transaction with the following MCCs:' takes its meaning from the sentence that opened the
    enumeration ('... will not be awarded for: (a) ...'), often a page earlier. Only when the item is that one clause:
    'a. Dining is defined as ... . For the avoidance of doubt ...' explains itself. Returns (kind, cue span)."""
    body = doc.body
    marks = list(ENUM.finditer(body, max(0, start - LEAD), start))
    if not marks:
        return None, None
    m = marks[-1]
    if re.search(r"[.!?]\s+[A-Z]", body[m.end():start]):
        return None, None
    opener, mark, closer = m.groups()
    letter_before = len(mark) == 1 and mark > "a" and f"\n{opener}{chr(ord(mark) - 1)}{closer} " in body[max(0, m.start() - 3000):m.start() + 1]
    first = "i" if mark in ROMAN and not letter_before else "a"
    pos = m.start() if mark == first else body.rfind(f"\n{opener}{first}{closer} ", max(0, m.start() - 8000), m.start())
    if pos < 0:
        return None, None
    return nearest_cue(doc, max(0, pos - 600), pos + 1)


def far_kind(flat, start, reach=6000):
    """Last resort: the nearest earlier clause ending in a colon that says exclude / include."""
    lo = max(0, start - reach)
    pos = start
    while True:
        pos = flat.rfind(":", lo, pos)
        if pos < 0:
            return None, None
        if re.match(r":(?://|\d)", flat[pos:pos + 3]):
            continue   # a time or a URL, not the end of an introducing clause
        cut = max(flat.rfind(c, max(lo, pos - 300), pos) for c in ".;:")
        s = cut + 1 if cut >= 0 else max(lo, pos - 300)
        kind = cue_kind(flat[s:pos + 1])
        if kind:
            return kind, (s, pos + 1)


def turn_kind(gap):
    """Wording between two codes that starts a new list with another meaning ('... MCC 5812, but not MCC 5814'). Only
    with a fresh 'MCC' in between: table descriptions ('Shops, Except Appliances', 'Not Elsewhere Classified') and
    category labels ('Dining excluding hotel dining') must not split a table."""
    if len(gap) <= 12 or not MCC_WORD.search(gap):
        return None
    if TURN_EXCLUDE.search(gap):
        return "exclude"
    return "include" if TURN_INCLUDE.search(gap) else None


def mcc_lists(doc):
    """MCC lists in a document: [{"kind": 'exclude'|'include'|None, "codes": set, "start", "end", "cue": (a, b), "rows"}].
    A list = codes close together, introduced by text that says MCC. A new definition between two codes ('"Shopping
    Merchants" refer to ... the following MCCs') starts a new list. The kind comes, in this order, from: the sentence
    right before the list; a definition between it and the list above; the sentence that opened the enumeration it sits in; the list just above (a table carried
    over a page break); the sentences shortly before; the nearest clause ending in a colon.
    rows = labelled table rows ('Dining 5811, 5812 ...')."""
    flat, lists, cur, prev_end = doc.flat, [], None, None
    for start, end, codes in code_tokens(flat):
        gap = flat[prev_end:start] if prev_end is not None else ""
        restart = len(gap) > 12 and bool(RESTART.search(gap))
        joined = cur is not None and len(gap) <= GAP and not restart
        if joined and cur.get("parent") is not None and "\n" in doc.body[prev_end:start]:
            cur = cur["parent"]   # an aside ('... no longer awarded for MCC 5965') ends with its line; the table goes on
        turn = turn_kind(gap) if joined else None
        if joined and (turn is None or turn == cur["kind"]):
            cur["codes"].update(codes)
            cur["end"] = end
            cur["tokens"].append((start, end, codes))
        else:
            parent = cur if joined else None
            if joined:
                kind, anchored, cue = turn, True, (prev_end, start)
            else:
                s = max(0, start - LEAD, prev_end or 0)
                cuts = [s] + [c for c in (m.end() for m in BOUNDARY.finditer(doc.body, s, start))]
                near_cuts = [c for c in cuts if start - c >= 40] or [s]
                lead = near_cuts[-1]
                kind, cue = cue_kind(flat[lead:start]), (lead, start)
                anchored = bool(MCC_WORD.search(flat[max(0, start - LEAD):start]))
                if kind is None and cur is not None and len(gap) <= GAP:   # a new definition inside the gap
                    kind, c2 = nearest_cue(doc, prev_end, start)
                    cue = c2 or cue
            cur = {"kind": kind, "codes": set(codes), "start": start, "end": end, "anchored": anchored, "cue": cue,
                   "tokens": [(start, end, codes)], "fresh": not joined, "floor": prev_end or 0, "parent": parent,
                   "restart": restart}
            lists.append(cur)
        if MCC_WORD.search(flat[start:min(len(flat), end + 40)]) or MCC_WORD.search(gap[-40:]):
            cur["anchored"] = True
        prev_end = end
    out = []
    for l in lists:
        if not l["anchored"]:
            continue
        if l["kind"] is None and l["fresh"]:
            kind, cue = enum_kind(doc, l["start"])
            if not kind and out and l["start"] - out[-1]["end"] < 600 and not l["restart"]:
                kind, cue = out[-1]["kind"], out[-1]["cue"]
            if not kind:   # the sentences before it, nearest first, not reaching into the list above
                kind, cue = nearest_cue(doc, max(l["floor"], l["start"] - LEAD_FAR), l["start"])
            if not kind:
                kind, cue = far_kind(flat, l["start"])
            if kind:
                l["kind"], l["cue"] = kind, cue
        l["rows"] = table_rows(doc.body, l["tokens"])
        out.append(l)
    return out


def table_rows(body, tokens):
    """Rows of a category table: [(label, codes, start, end)]. A row starts where a line has words before its first
    code ('Dining 5811, 5812'), or where a short line of words sits right above a line of nothing but codes; lines that
    begin with a code continue the row. Plain 'code description' tables have no labels and give no rows."""
    rows, last_line = [], None

    def words(text):
        text = re.sub(r"\bMCCs?\b|\b(?:and|or|to|the|with|for)\b|[^A-Za-z&' ]", " ", text, flags=re.I)
        return " ".join(text.split()) if re.search(r"[A-Za-z]{4}", text) else ""

    for start, end, codes in tokens:
        ls = body.rfind("\n", 0, start) + 1
        if ls != last_line:   # first code on this line
            label, row_start = words(body[ls:start]), ls
            le = body.find("\n", start)
            only_codes = not body[ls:start].strip() and not re.search(r"[A-Za-z]{4}", body[end:le if le >= 0 else len(body)])
            if not label and only_codes and ls > 0:
                pe = len(body[:ls].rstrip())          # the line above, blank lines skipped
                ps = body.rfind("\n", 0, pe) + 1
                prev = body[ps:pe]
                continued = rows and not body[rows[-1][3]:ls].strip(" ,;\n")
                if not continued and len(prev) <= 60 and not re.search(r"\d{4}", prev):
                    label, row_start = words(prev), ps
            if label:
                rows.append([label[:80], set(), row_start, end])
            last_line = ls
        if rows:
            rows[-1][1].update(codes)
            rows[-1][3] = end
    return [tuple(r) for r in rows] if len(rows) >= 2 else []


def fmt_codes(codes, limit=14):
    """{'5811','5812','5813','5814','6300'} -> '5811-5814, 6300' (runs of 3+ collapsed, long lists cut)."""
    nums, parts, i = sorted(int(c) for c in codes), [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{nums[i]:04d}-{nums[j]:04d}")
        else:
            parts.extend(f"{n:04d}" for n in nums[i:j + 1])
        i = j + 1
    return ", ".join(parts[:limit]) + (f" (+{len(parts) - limit} more)" if len(parts) > limit else "")


def compare_codes(ours, theirs):
    """(status, ours text, theirs text) for two MCC sets."""
    ours, theirs = set(ours), set(theirs)
    n = lambda k: f"{k} MCC" + ("" if k == 1 else "s")   # noqa: E731
    t = n(len(theirs))
    if ours == theirs:
        return "match", n(len(ours)), t
    extra, missing = theirs - ours, ours - theirs
    if extra:
        t += f"; in the document but not ours: {fmt_codes(extra)}"
    if missing:
        t += f"; ours but not in the document: {fmt_codes(missing)}"
    return "differs", n(len(ours)) if ours else "no MCCs recorded", t


# --- C. check: numbers -----------------------------------------------------------------------------------------------

NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
MONEY = re.compile(r"(?:\bS\$|\bSGD\s?|(?<![A-Za-z$])\$)\s?" + NUM + r"(?!\s?(?:million|billion|k\b))", re.I)
PER = r"(?:per|for\s+every|for\s+each|with\s+every|on\s+every|on\s+each|for)\s+(?:(?:S\$|SGD|\$)\s?" + NUM + r"|dollar)"
MILES = re.compile(NUM + r"\s*(?:(?:KrisFlyer|air|bonus|base|Asia|KF)\s+)*miles?\s*(?:\([^)]{0,40}\)\s*)?" + PER, re.I)
MILES_REV = re.compile(r"(?:S\$|SGD|\$)\s?" + NUM + r"\s*(?:spen[dt]\s*)?=\s*" + NUM + r"\s*(?:(?:KrisFlyer|air|KF)\s+)?miles?", re.I)
MPD = re.compile(NUM + r"\s*mpd\b", re.I)
POINTS = re.compile(r"(?:(?:UNI\$|OCBC\$|STAR\$)\s?" + NUM + r"|" + NUM + r"\s*(?:[A-Za-z$®™]+\s+){0,3}?(?:points?|UNI\$|OCBC\$))"
                    r"\s*(?:\([^)]{0,40}\)\s*)?" + PER, re.I)
PERCENT = re.compile(NUM + r"\s?%\s*(?:(?:unlimited|bonus|base|additional|total|cash)\s+)*(?:cash\s?back|cash\s?rebates?|rebates?)|"
                     r"(?:cash\s?back|cash\s?rebates?|rebates?)\s+(?:of\s+)?(?:up\s+to\s+)?" + NUM + r"\s?%", re.I)
CAP_WORD = re.compile(r"capped\s+at|\bcap\s+of|cap\s+is|maximum\s+(?:of\s+)?|max\.?\s+(?:of\s+)?|up\s+to(?:\s+a\s+maximum\s+of)?|"
                      r"limited\s+to|first", re.I)
MIN_WORD = re.compile(r"min(?:imum|\.)?\s+(?:(?:total|monthly|retail|qualifying|eligible|aggregate|accumulated|card|local)\s+)*"
                      r"(?:spend(?:ing)?|purchases?|amount|charge|retail\s+transactions?)(?:\s+of)?|"
                      r"spend(?:s|ing)?\s+(?:of\s+)?(?:at\s+least|a\s+min(?:imum)?(?:\s+of)?)|at\s+least|or\s+more|and\s+above", re.I)
FEE_WORD = re.compile(r"annual\s+(?:card\s+|membership\s+|principal\s+)*fees?|annual\s+card\s+membership", re.I)
FCY_WORD = re.compile(r"foreign\s+currenc|overseas|non-?SGD|outside\s+(?:of\s+)?singapore|in\s+currencies\s+other", re.I)
BASE_WORD = re.compile(r"\blocal\s+(?:spend|retail|transactions?|purchases?|currency)|singapore\s+dollars?|\bbase\b|all\s+other|"
                       r"general\s+spend|all\s+(?:eligible\s+|retail\s+)?(?:spend|purchases|transactions)", re.I)
CAP_LEAD = re.compile(r"(?:\bfirst|capped\s+at|\bcap\s+(?:of|on|at|is)|maximum(?:\s+of)?|limited\s+to|equivalent\s+to|up\s+to)\s*"
                      r"(?:a\s+|the\s+first\s+|the\s+|(?:total|maximum)\s+of\s+)?$", re.I)
SPEND_AFTER = re.compile(r"\s*(?:\(?[\w’'-]+\)?,?\s+){0,3}?(?:spen[dt]|spending)\b", re.I)
SPEND_BEFORE = re.compile(r"spend(?:ing)?\s+(?:cap|limit)\s+(?:of|is|at)\s*$", re.I)
MCC_WORDS = (((5811, 5814), r"dining|restaurant|food"), ((5541, 5542), r"petrol|fuel|service\s+station"),
             ((5411, 5411), r"supermarket|grocer"), ((4111, 4121), r"transport|taxi|ride|commut"),
             ((3000, 3350), r"airline|air\s+ticket|flight"), ((4511, 4511), r"airline|air\s+ticket|flight"),
             ((7011, 7011), r"hotel|accommodation"), ((3501, 3999), r"hotel|accommodation"),
             ((5311, 5311), r"department\s+store|shopping"), ((5912, 5912), r"pharmac|drug\s+store"))
BORING = {"bonus", "local", "promo", "rule", "spend", "card", "cards", "other", "general", "base", "rate", "accelerated",
          "with", "and", "the", "for", "hotels", "four", "six"}


def num(s):
    return float(str(s).replace(",", ""))


def g(v):
    return f"{float(v):g}"


def money(v):
    return f"S${float(v):,.2f}".replace(".00", "")


def near(a, b, tol=0.011):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def rates_in(text, cashback, ppm=None):
    """Earn rates stated in a piece of text: [(value in our unit, match start, match end, wording)]. Miles cards: mpd,
    straight from 'N miles per S$M', or from 'N points per S$M' only when the programme's points-per-mile is known."""
    out = []
    if cashback:
        for m in PERCENT.finditer(text):
            out.append((num(m.group(1) or m.group(2)), m.start(), m.end(), m.group(0)))
        return out
    for m in MILES.finditer(text):
        out.append((num(m.group(1)) / num(m.group(2) or 1), m.start(), m.end(), m.group(0)))
    for m in MILES_REV.finditer(text):
        out.append((num(m.group(2)) / num(m.group(1)), m.start(), m.end(), m.group(0)))
    for m in MPD.finditer(text):
        out.append((num(m.group(1)), m.start(), m.end(), m.group(0)))
    if ppm:
        for m in POINTS.finditer(text):
            pts, per = num(m.group(1) or m.group(2)), num(m.group(3) or 1)
            out.append((round(pts / per / float(ppm), 4), m.start(), m.end(), m.group(0)))
    return [r for r in out if 0 < r[0] <= 100]


def amounts_after(text, word, reach=70):
    """S$ amounts that follow a cap / minimum-spend / fee wording within `reach` characters: [(value, start, end)]."""
    out = []
    for w in word.finditer(text):
        for m in MONEY.finditer(text, w.end(), min(len(text), w.end() + reach)):
            out.append((num(m.group(1)), w.start(), m.end()))
            break
    return out


def rule_mccs(rule, rows=()):
    """The MCCs a rule earns on: card_rule_mccs 'include' rows + match.mcc (also inside any_of), ranges expanded."""
    codes = {r["mcc"] for r in rows if r["effect"] == "include"}

    def walk(m):
        if isinstance(m, dict):
            for spec in m.get("mcc") or []:
                spec = str(spec)
                a, _, b = spec.partition("-")
                if re.fullmatch(r"\d{4}", a) and (not b or re.fullmatch(r"\d{4}", b)):
                    codes.update(expand(a, b or None))
            for alt in m.get("any_of") or []:
                walk(alt)
    walk(rule.get("match") or {})
    return codes


def rule_words(rule, codes=()):
    """Words that tie a sentence to this rule (online, contactless, foreign currency, dining ...). None = nothing
    specific enough, so a lone number in the document cannot be pinned on the rule."""
    pats = []
    match = rule.get("match") or {}
    alts = [match] + [a for a in match.get("any_of") or [] if isinstance(a, dict)]
    for m in alts:
        ch = m.get("channel")
        if ch == "online":
            pats.append(r"online|e-?commerce|internet")
        elif ch in ("contactless", "mobile"):
            pats.append(r"contactless|paywave|mobile\s+(?:wallet|payment|contactless)|apple\s+pay|google\s+pay|in-app")
        if m.get("fcy"):
            pats.append(FCY_WORD.pattern)
    for (lo, hi), words in MCC_WORDS:
        if any(lo <= int(c) <= hi for c in codes):
            pats.append(words)
    for w in re.findall(r"[a-z]{4,}", f"{rule.get('bucket') or ''} {rule.get('label') or ''}".lower()):
        if w not in BORING and not re.fullmatch(r"w\d+", w):
            pats.append(re.escape(w))
    return re.compile("|".join(dict.fromkeys(pats)), re.I) if pats else None


NAMING = re.compile(r"((?:[A-Z][\w’'$+®™&-]*\s+){1,7})(?:Card(?:member|holder)s?\b|Cards?\b(?!\s+(?:Transactions?|Accounts?|Spend|"
                    r"Promotions?|Programmes?|Programs?|Rewards|Design|Terms|Members?|Centre|Statements?|Agreement|Applications?)))")
NAME_GENERIC = {"card", "cards", "credit", "debit", "the", "visa", "mastercard", "master", "american", "express", "amex", "singapore",
                "sg", "bank", "dbs", "posb", "uob", "ocbc", "citi", "citibank", "hsbc", "standard", "chartered", "maybank", "cimb",
                "boc", "of", "china", "trust", "dcs", "icbc", "your", "this", "that", "each", "any", "all", "such", "an", "other",
                "principal", "supplementary", "basic", "eligible", "relevant", "new", "existing", "selected", "local", "overseas",
                "qualifying", "said", "replacement", "physical", "virtual", "for", "if", "to", "with", "and", "or", "in", "on",
                "when", "where", "our", "their", "his", "her", "its", "these", "those", "both", "only", "no", "by", "upon", "as"}
REWARD_WORD = re.compile(r"cash\s?back|rebates?|points?|miles|UNI\$|OCBC\$|bonus|rewards?|\bearn", re.I)
SPEND_WORD = re.compile(r"spen[dt]|charged?|transactions?|purchases?", re.I)
PERIODS = ((r"quarter", "quarter", " per quarter"), (r"per\s+(?:calendar\s+|membership\s+)?year|annual|yearly", "year", " per year"),
           (r"statement", "month", " per statement month"), (r"calendar\s+month|per\s+month|monthly|each\s+month|a\s+month", "month",
                                                              " per calendar month"))


def name_words(text):
    """The words that tell one card from its siblings: 'UOB Lady’s Solitaire Card' -> {lady, solitaire}."""
    text = re.sub(r"\bman\s+utd\b", "manchester united", re.sub(r"[’']s\b", "", text.lower()))
    return frozenset(w for w in re.findall(r"[a-z0-9+]+", text) if len(w) > 1 and w not in NAME_GENERIC)


NETWORKS = (("amex", r"american\s+express|\bamex\b"), ("visa", r"\bvisa\b"), ("mastercard", r"master\s?card"), ("unionpay", r"union\s?pay"))


def networks(text):
    """Card schemes a name mentions. 'PRVI Miles American Express Cardmembers only' is not about the PRVI Miles Visa."""
    return frozenset(k for k, pat in NETWORKS if re.search(pat, text or "", re.I))


def owned(doc, a, b, mine, bigger=(), nets=frozenset()):
    """Is the sentence [a, b) about this card? Programme documents cover many cards ('UOB Preferred Visa Cardmembers
    earn ...'), so: a sentence that names cards must name ours (or its family: 'UOB Lady's Card' for Lady's Solitaire);
    a sentence that names none counts only when the last card named above it is ours, or, in a document that names no
    other card at all, on its own. bigger: name words of sibling cards that contain ours and more (a 'Lady's
    Solitaire' sentence is not about the plain Lady's card)."""
    def ours(x):
        n, their_nets = x
        if nets and their_nets and not (nets & their_nets):
            return False   # same family, another scheme
        return bool(mine) and ((n >= mine and not any(n >= x for x in bigger)) or n <= mine)
    inside = [x[1:] for x in doc.names if a <= x[0] < b]
    if inside:
        return any(ours(x) for x in inside)
    before = [x[1:] for x in doc.names if a - 1500 <= x[0] < a]
    if before:
        return ours(before[-1])
    return not any(not ours(x[1:]) for x in doc.names)   # several cards in the document and none named here: unknown


def excluded(doc):
    """The document's card-wide exclusion lists. Left out: lists of one or two codes (nearly always a side remark inside
    another rule: 'petrol, excluding SPC, under MCC 5541') and lists that shut out the airline codes (3000-3299): no
    card refuses points on all travel, that is one bonus category's carve-out ('online spend, excluding travel')."""
    return [l for l in doc.lists if l["kind"] == "exclude" and len(l["codes"]) >= 3 and "3000" not in l["codes"]]


def check(subject, status, ours, theirs=None, quote=None, url=None, method="pattern", data=None):
    return {"subject": subject, "status": status, "ours": ours, "theirs": theirs, "quote": quote, "url": url, "method": method,
            "data": data}


def sentences_with(doc, *patterns, without=None):
    """(a, b, text) of each sentence that satisfies every pattern (and not `without`)."""
    for a, b in doc.spans:
        t = doc.flat[a:b]
        if len(t) > 1500:
            continue   # a whole table or a page with no punctuation: not a sentence
        if all(p.search(t) for p in patterns if p) and not (without and without.search(t)):
            yield a, b, t


# --- C. check: annual fee and its waiver ------------------------------------------------------------------------------

FEE_CELL = re.compile(r"^(?:S\$\s?([\d,]+(?:\.\d+)?)|(free|no annual fee|nil)\b(?!\s+for\s+(?:the\s+)?(?:first|1st|\d)))", re.I)
FEE_LABEL = re.compile(r"^(?:(?:principal|basic|main)\s+card\b.{0,20}|(?:\d(?:st|nd|rd)\s+)?supplementary\s+cards?\b.{0,25}|\(.*\))$", re.I)
NOT_WAIVED = re.compile(r"non-?\s?waivable|cannot\s+be\s+waived|no\s+fee\s+waiver|waiver\s+is\s+not\s+applicable|not\s+(?:be\s+)?waived|"
                        r"(?:will\s+be\s+)?no\s+waiver|not\s+eligible\s+for\s+(?:any\s+)?(?:annual\s+)?fee\s+waiver", re.I)
WAIVE = re.compile(r"waive[dr]?|waiver|waivable|\bfree\b|no\s+annual\s+fee", re.I)
CEASED = re.compile(r"waiver.{0,160}?(?:will\s+be\s+ceased|will\s+cease|has\s+ceased|discontinued|no\s+longer\s+(?:apply|be\s+offered|available))", re.I)
FROM_DATE = re.compile(r"(?:with\s+effect\s+from|effective(?:\s+from)?|from)\s+(\d{1,2})\s+([A-Za-z]{3,9})\s*,?\s+(20\d\d)", re.I)
FIRST_YEARS = re.compile(r"(?:(?<![\d.,$])\b(\d|one|two|three|five)[\s-]*\(?\d?\)?\s*-?years?\s+(?:annual\s+)?fee\s+waiver|(?:free|waived(?:\s+off)?|waiver)\s+"
                         r"(?:for\s+)?(?:the\s+)?(?:first|1st)\s*(?:\(?(\d+|one|two|three)\)?\s*)?years?|(?:first|1st)[\s-]+year\s+"
                         r"(?:annual\s+)?(?:card\s+)?(?:membership\s+)?fee\s+(?:is\s+|will\s+be\s+)?(?:waive[dr]|free))", re.I)
FOREVER = re.compile(r"perpetual(?!\s+fee\s+waiver\s+for\s+all\s+supp)|free\s+for\s+life|lifetime|no\s+annual\s+fee|waived\s+as\s+long\s+as", re.I)
ON_REQUEST = re.compile(r"request\s+(?:for\s+)?(?:an?\s+)?(?:annual\s+)?(?:fee\s+)?waiver|waiver\s+request|call\s+.{0,40}waive", re.I)
WORD_NUM = {"one": 1, "two": 2, "three": 3, "five": 5}
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS.split("|"))}


def fee_rows(doc):
    """Rows of a fee table as the page text gives them, one cell per line: a card name line followed by its fee cell
    ('S$196.20' / 'Free'), then the other cells until the next card name. Only rows under an annual / membership fee
    heading (not 'Quarterly Service Fee', 'Card Replacement'). [{name, a, fee, supp, cells: [(a, b, text)]}]"""
    lines = [(m.start(), m.end(), m.group()) for m in re.finditer(r"[^\n]+", doc.body)]
    heads, annual = [], None
    for i, (a, b, t) in enumerate(lines):
        if len(t) <= 60 and "S$" not in t and re.search(r"\bfees?\b|charges?\b", t, re.I):
            annual = bool(re.search(r"annual|membership", t, re.I)) if re.search(r"annual|membership|quarterly|monthly|service|late|"
                                                                                 r"cash|replacement|charges", t, re.I) else annual
        if not FEE_CELL.match(t) or not annual:
            continue
        j = i - 1
        while j >= 0 and FEE_LABEL.match(lines[j][2]):
            j -= 1
        name = lines[j][2] if j >= 0 else ""
        if (not name or FEE_CELL.match(name) or len(name) > 90 or "$" in name or name.endswith((".", ":", ";"))
                or not re.search(r"[A-Z][A-Za-z]", name) or re.match(r"free\b|no\s+annual|waive|n\.?a\b|this\s", name, re.I)
                or (heads and heads[-1][0] == j)):
            continue
        heads.append((j, i))
    rows = []
    for k, (j, i) in enumerate(heads):
        stop = min(heads[k + 1][0] if k + 1 < len(heads) else len(lines), i + 7)
        m = FEE_CELL.match(lines[i][2])
        fee = num(m.group(1)) if m.group(1) else 0.0
        if 0 < fee < 20:
            continue   # not an annual card fee (a per-item charge)
        supp, skip = None, []
        for a, b, t in lines[i + 1:stop]:
            if FEE_LABEL.match(t):
                continue
            ms = FEE_CELL.match(t)
            if ms:
                supp, skip = (num(ms.group(1)) if ms.group(1) else 0.0), skip + [t]
            break
        # what is left says how the fee can be waived: the fee columns themselves ('Free #' = the supplementary card) do not
        rows.append({"name": lines[j][2], "a": lines[j][0], "fee": fee, "supp": supp, "fee_end": lines[i][1],
                     "cells": [c for c in lines[i + 1:stop] if c[2] not in skip and not FEE_LABEL.match(c[2])]})
    return rows


def row_for(rows, name, bigger=(), nets=frozenset(), ours=None):
    """The fee-table row for this card, or None when no row can be tied to it. The row must carry every word that sets
    the card apart and not be another scheme's ('... American Express' for the Visa). Of several: the one that states
    the fee we hold (a card sold in variants, 'Visa Infinite' / 'Visa Infinite Metal'), else the one closest to the
    card's full name; two different fees equally close = unknown."""
    mine = name_words(name)
    tokens = set(re.findall(r"[a-z0-9+]+", re.sub(r"[’']s\b", "", name.lower())))
    if not mine:
        return None
    ranked = []
    for r in rows:
        n, their_nets = name_words(r["name"]), networks(r["name"])
        if n >= mine and not any(n >= x for x in bigger) and not (nets and their_nets and not nets & their_nets):
            theirs = re.findall(r"[a-z0-9+]+", re.sub(r"[’']s\b", "", r["name"].lower()))
            same_fee = ours is not None and abs(float(ours) - r["fee"]) <= 1
            ranked.append(((same_fee, n == mine, len(tokens & set(theirs)), -len(set(theirs) - tokens)), r))
    if not ranked:
        return None
    top = max(k for k, _ in ranked)
    best = [r for k, r in ranked if k == top]
    return best[0] if len({r["fee"] for r in best}) == 1 else None


def read_waiver(texts, today=None, table=False):
    """What a run of sentences / table cells [(a, b, text)] says about waiving the annual fee: (data, plain sentence,
    (a, b) of the wording it rests on), or None when they say nothing. Only what is written: a spend waiver the bank
    says has ended is reported as ended, never as available. table: the texts are the waiver cells of a fee-table row
    ('Spend p.a. S$50,000', 'UNI$6500'), so they need not say 'waive' themselves."""
    today = today or datetime.date.today()
    spend = first = never = forever = request = ended = points = cond = None
    for a, b, t in texts:
        if CEASED.search(t):
            d = FROM_DATE.search(t)
            try:
                when = datetime.date(int(d.group(3)), MONTH_NUM[d.group(2)[:3].lower()], int(d.group(1))) if d else None
            except (KeyError, ValueError):
                when = None
            ended = (a, b, t, when)
            continue
        if re.fullmatch(r"UNI\$\s?[\d,]+", t.strip()) and points is None:
            points = (a, b, t, num(t.strip()[4:]))
            continue
        if re.search(r"supplementary", t, re.I) and not re.search(r"principal|basic", t, re.I):
            continue   # about the supplementary card's fee, not this card's
        m = MONEY.search(t)
        if not WAIVE.search(t):
            if re.search(r"\d+\s+transactions?\s+in\s+the\s+last\s+12\s+months", t, re.I):
                cond = (a, b, t)
            if not (table and m and re.match(r"\s*spend\b", t, re.I)):
                continue
        if m and re.search(r"spen[dt]|charge|retail", t, re.I) and (table or re.search(r"waive[dr]?|waiver", t, re.I)) and spend is None:
            period = ("calendar_year" if re.search(r"calendar\s+year", t, re.I) else
                      "12_months" if re.search(r"12\s+months|twelve\s+months", t, re.I) else
                      "membership_year" if re.search(r"membership\s+year|card\s+year|anniversary|each\s+year|per\s+year|"
                                                     r"following\s+year|preceding\s+year|previous\s+year", t, re.I) else None)
            spend = (a, b, t, num(m.group(1)), period)
        f = FIRST_YEARS.search(t)
        if f and first is None:
            n = next((x for x in f.groups() if x), "1")
            first = (a, b, t, WORD_NUM.get(n.lower()) or int(n))
        if NOT_WAIVED.search(t) and never is None:
            never = (a, b, t)
        if FOREVER.search(t) and forever is None and not m:
            forever = (a, b, t)
        if ON_REQUEST.search(t) and request is None and not table:
            request = (a, b, t)
        if re.search(r"waived\s+if|waived\s+(?:when|with)\b", t, re.I) and not m and cond is None:
            cond = (a, b, t)
    data = {"kind": "none_stated", "amount": None, "period": None, "years_free": first[3] if first else None}
    over = bool(ended and spend and (ended[3] is None or ended[3] <= today))
    if spend and not over:
        data.update(kind="spend", amount=spend[3], period=spend[4])
        text = f"Waived with S${spend[3]:,.0f} of spend" + {"membership_year": " in a membership year", "calendar_year": " in a calendar year",
                                                              "12_months": " in 12 months"}.get(spend[4], "")
        text += f"; first {'year' if first[3] == 1 else f'{first[3]} years'} waived" if first else ""
        if ended:
            text += f"; the bank says this ends on {ended[3]:%-d %B %Y}"
        return data, text, spend[:2]
    tail = (f"; the S${spend[3]:,.0f} spend waiver ended" + (f" on {ended[3]:%-d %B %Y}" if ended[3] else "")) if over else ""
    if first and not (never and never[2] is first[2]):
        data["kind"] = "first_year"
        return data, f"First {'year' if first[3] == 1 else f'{first[3]} years'} waived" + tail, (ended or first)[:2] if over else first[:2]
    if never:
        data["kind"] = "not_waivable"
        return data, "Not waivable" + tail, (ended or never)[:2] if over else never[:2]
    if over:
        data["kind"] = "not_waivable"
        return data, "No waiver stated" + tail, ended[:2]
    if forever:
        data["kind"] = "automatic"
        return data, "Waived automatically: " + " ".join(forever[2].split())[:140], forever[:2]
    if cond:
        data["kind"] = "spend"
        return data, "Waived on a condition: " + " ".join(cond[2].split())[:160], cond[:2]
    if points:
        data.update(kind="points", points=points[3])
        return data, f"Can be paid with points: full waiver for UNI${points[3]:,.0f}", points[:2]
    if request:
        data["kind"] = "on_request"
        return data, "Waiver on request", request[:2]
    return None


def fee_text(v):
    return "no annual fee" if not v else f"{money(v)} a year"


def fee_checks(card, docs, mine, bigger, nets=frozenset(), today=None):
    """'annual_fee' and 'fee_waiver' from the bank's fee schedule (the row that carries this card's name) or, failing
    that, from sentences of the card's own terms. ours = cards.annual_fee; with none on file the bank's figure is
    'info'. A figure before GST and one with 9 % GST are the same fee."""
    ours = card.get("annual_fee")
    ours_text = money(ours) if ours is not None else None
    docs = sorted((d for d in docs if d.kind in ("fees", "tc", "faq")), key=lambda d: d.kind != "fees")   # the fee schedule first
    tables = {d.url: fee_rows(d) for d in docs}

    def sentences(*patterns):
        """Sentences of the card's own terms about its fee. Not from a fee-table page (cells are not sentences), not a
        run of text that names three or more cards (a table printed as one paragraph)."""
        for d in docs:
            if tables[d.url]:
                continue
            for a, b, t in sentences_with(d, *patterns):
                if len(t) <= 600 and len({x[1] for x in d.names if a <= x[0] < b}) < 3 and owned(d, a, b, mine, bigger, nets):
                    yield d, a, b, t

    fee = waiver = None          # fee = (value, supp, quote, document, before GST?)
    for d in docs:
        row = row_for(tables[d.url], card.get("name") or "", bigger, nets, ours)
        if row:
            fee = (row["fee"], row["supp"], d.quote(row["a"], row["fee_end"], exact=True), d, False)
            w = read_waiver(row["cells"], today, table=True) if row["fee"] else None
            if w:
                waiver = (w, d, True)
            elif not row["fee"]:   # the table says there is no fee: nothing to waive, and that cell is the wording
                waiver = (({"kind": "automatic", "amount": None, "period": None, "years_free": None}, "No annual fee to pay",
                           (row["a"], row["fee_end"])), d, True)
            break
    if fee is None:
        for d, a, b, t in sentences(FEE_WORD):
            found = []
            for w in FEE_WORD.finditer(t):   # 'annual fee of S$91', not 'annual fee will be waived with a spend of S$25,000'
                m = MONEY.search(t, w.end(), w.end() + 60)
                if m and re.search(r"(?:\bof|\bis|\bat|:)\s*$", t[w.end():m.start()]) and not re.search(
                        r"waiv|spen[dt]|minimum|supplementary", t[:m.start()], re.I) and num(m.group(1)) >= 20:
                    found.append((num(m.group(1)), w.start(), m.end()))
            if found:
                v, s, e = found[0]
                fee = (v, None, d.quote(a + s, a + e), d, bool(re.search(r"exclud\w+\s+(?:prevailing\s+)?GST|before\s+GST", t, re.I)))
                break
    if waiver is None:
        by_doc = {}
        for d, a, b, t in sentences(FEE_WORD, WAIVE):
            by_doc.setdefault(d.url, (d, []))[1].append((a, b, t))
        for d, texts in by_doc.values():
            w = read_waiver(texts, today)
            if w:
                waiver = (w, d, False)
                break
    out = []
    first_free = None
    if waiver:
        wd = waiver[0][0]
        first_free = True if wd.get("years_free") else None
    if fee:
        v, supp, quote, d, ex_gst = fee
        theirs = fee_text(v) + (" before GST" if ex_gst else "") + (" (first year waived)" if first_free else "")
        data = {"fee": v, "first_year_free": first_free, "supp_fee": supp}
        if ours is None:
            status = "info"
        elif abs(float(ours) - v) <= 1:
            status = "match"
        elif abs(float(ours) - v * 1.09) <= 1 or abs(float(ours) * 1.09 - v) <= 1:
            status, theirs = "match", theirs + "; the same fee as ours once 9% GST is added"
        else:
            status = "differs"
        out.append(check("annual_fee", status, ours_text, theirs, quote, d.url, data=data))
    else:
        out.append(check("annual_fee", "not_found", ours_text))
    if waiver:
        (wd, text, (a, b)), d, cell = waiver
        out.append(check("fee_waiver", "info", None, text, d.quote(a, b, exact=cell), d.url, data=wd))
    else:
        out.append(check("fee_waiver", "not_found", None, data={"kind": "none_stated", "amount": None, "period": None, "years_free": None}))
    return out


def period_of(text):
    for pat, key, label in PERIODS:
        if re.search(pat, text, re.I):
            return key, label
    return None, ""


def mcc_count(n):
    return f"{n} MCC" + ("" if n == 1 else "s")


def pattern_checks(card, rules, rule_rows, exclusions, docs, ppm=None, siblings=(), today=None):
    """All checks for one card from its documents, deterministically. card: id, name, kind, base_sgd, base_fcy,
    annual_fee. rules: current card_rules rows. rule_rows: {rule id: card_rule_mccs rows}. exclusions: set of MCCs.
    docs: [Doc]. siblings: names of the bank's other cards (to keep their sentences apart from ours).

    Numbers are only ever compared inside a sentence that belongs to the card (see owned) and to the rule (rule_words).
    A red flag has to be trustworthy, so 'differs' is rare for numbers: never for a rate, and for a cap / minimum spend
    only when the same sentence also states the rule's rate. Another figure found without that tie is kept as the
    bank's figure (theirs + quote) on a 'not_found' row: shown, not judged. MCC lists are compared code by code."""
    out = []
    cashback = card.get("kind") == "cashback"
    unit = "%" if cashback else " mpd"
    mine = name_words(card.get("name") or "")
    bigger = [w for w in (name_words(x) for x in siblings) if w > mine]
    nets = networks(f"{card.get('name') or ''} {card.get('network') or ''}")
    docs = [d for d in docs if "debit" in (card.get("name") or "").lower() or not re.search(r"debit", d.url, re.I)]
    terms = [d for d in docs if d.kind in ("tc", "exclusions", "faq")]

    def sentences(pool, *patterns, without=None):
        for d in pool:
            for a, b, t in sentences_with(d, *patterns, without=without):
                if owned(d, a, b, mine, bigger, nets):
                    yield d, a, t

    def says(subject, ours_text, found, same, tied=False):
        """found = (theirs, quote, url). The same value -> match. Another value -> 'differs' only when the sentence is
        tied to the rule twice over (tied); otherwise the bank's figure is shown next to ours without a verdict."""
        return check(subject, "match" if same else "differs" if tied else "not_found", ours_text, *found)

    def rate_check(subject, ours, want, without=None):
        """Rates: match or nothing. Documents state rates in parts (base + bonus) and tiers, so another number in a
        sentence about the same thing is not a disagreement a pattern can call."""
        for d, a, t in sentences(terms, want, without=without):
            for v, s, e, w in rates_in(t, cashback, ppm):
                if near(v, ours):
                    return says(subject, f"{g(ours)}{unit}", (f"{g(v)}{unit} ({' '.join(w.split())})"[:200], d.quote(a + s, a + e), d.url), True)
        return check(subject, "not_found", f"{g(ours)}{unit}")

    def has_rate(t, rate):
        return rate is not None and any(near(v, rate) for v, *_ in rates_in(t, cashback, ppm))

    def amount_check(subject, ours, ours_text, word, want, pool, reach=70, need=None, rate=None):
        """Minimum spend / annual fee: our amount after the wording -> match. One other amount, in a sentence that also
        states the rule's rate -> differs. One other amount otherwise -> shown as the bank's figure, no verdict."""
        other = {}
        for d, a, t in sentences(pool, word, want, need):
            for v, s, e in amounts_after(t, word, reach):
                found = (money(v), d.quote(a + s, a + e), d.url)
                if near(v, ours):
                    return says(subject, ours_text, found, True)
                other[round(v, 2)] = says(subject, ours_text, found, False, has_rate(t, rate))
        return next(iter(other.values())) if len(other) == 1 else check(subject, "not_found", ours_text)

    def cap_check(subject, rule, ours_text, want, period):
        """Our cap is dollars of SPEND at the bonus rate. It is compared only with an amount the document clearly gives
        as spend ('the first S$1,000 of online spend', 'equivalent to S$1,000 spent'), or, on a cashback card, with a
        cashback cap that converts to it exactly with the one rate in the same sentence. Any other cashback cap is
        shown as the bank's figure without a verdict; points caps ('UNI$2,400') are not read at all."""
        ours, other = float(rule["cap"]), {}
        for d, a, t in sentences(terms, CAP_WORD, want, REWARD_WORD):
            for m in MONEY.finditer(t):
                if not CAP_LEAD.search(t[max(0, m.start() - 45):m.start()]):
                    continue
                v, after = num(m.group(1)), t[m.end():m.end() + 45]
                key, label = (period_of(t[m.start():m.end() + 70]) if period else (None, ""))
                if period and not key:
                    key, label = period_of(t)
                quote = d.quote(a + m.start(), a + m.end())
                if SPEND_AFTER.match(after) or SPEND_BEFORE.search(t[max(0, m.start() - 45):m.start()]):
                    found, same = (f"{money(v)} of spend{label}", quote, d.url), near(v, ours)
                    if same and (not key or key == period):
                        return says(subject, ours_text, found, True)
                    other[(round(v, 2), key)] = says(subject, ours_text, found, False, has_rate(t, rule["rate"]))
                elif cashback and re.search(r"cash\s?back|rebate", t, re.I) and not re.match(r"\s*(?:points?|miles)", after, re.I):
                    pcts = {round(r[0], 3) for r in rates_in(t, True)}
                    spend = v * 100 / next(iter(pcts)) if len(pcts) == 1 else None
                    found = (f"{money(v)} cashback{label}" + (f" (= {money(spend)} of spend at {g(next(iter(pcts)))}%)" if spend else ""),
                             quote, d.url)
                    if spend and near(spend, ours, 1) and (not key or key == period):
                        return says(subject, ours_text, found, True)
                    other[(round(v, 2), key)] = says(subject, ours_text, found, False)   # units unsure: never 'differs'
        return next(iter(other.values())) if len(other) == 1 else check(subject, "not_found", ours_text)

    if card.get("base_sgd") is not None:
        out.append(rate_check("base_sgd", card["base_sgd"], BASE_WORD, without=FCY_WORD))
    if card.get("base_fcy") is not None:
        out.append(rate_check("base_fcy", card["base_fcy"], FCY_WORD))
    out.extend(fee_checks(card, docs, mine, bigger, nets, today))

    # card-wide exclusions: the document whose exclusion list is closest to ours (or the longest when we hold none)
    best = None
    for d in terms:
        codes = set().union(*[l["codes"] for l in excluded(d)] or [set()])
        if codes:
            score = (len(codes & exclusions) / len(codes | exclusions), len(codes)) if exclusions else (0, len(codes))
            if best is None or score > best[0]:
                best = (score, d, codes)
    if best:
        _, d, codes = best
        first = max(excluded(d), key=lambda l: len(l["codes"]))
        status, o, t = compare_codes(exclusions, codes)
        if status == "differs" and codes <= exclusions:
            # every code the bank lists is excluded here too; what we hold beyond that usually stands for exclusions
            # the document gives in words ('utilities', 'insurance premiums'): nothing a red flag should rest on
            status = "match"
            t = f"{mcc_count(len(codes))}, all excluded in ours too; ours holds {len(exclusions - codes)} more that the document does not list by code"
        out.append(check("exclusions", status, o if exclusions else "none recorded", t,
                         d.quote(*first["cue"]), d.url))
    elif exclusions:
        out.append(check("exclusions", "not_found", mcc_count(len(exclusions))))

    for rule in rules:
        bucket, codes = rule["bucket"], rule_mccs(rule, rule_rows.get(rule["id"], ()))
        words = rule_words(rule, codes)
        ours_rate = f"{g(rule['rate'])}{unit}"
        out.append(rate_check(f"rule:{bucket}:rate", rule["rate"], words) if words else
                   check(f"rule:{bucket}:rate", "not_found", ours_rate))
        if rule.get("cap") is not None:
            period = {"calendar": ("month", " per calendar month"), "statement": ("month", " per statement month"),
                      "membership_year": ("year", " per year")}.get(rule.get("cap_period"), (None, ""))
            ours_cap = f"{money(rule['cap'])}{period[1]}"
            out.append(cap_check(f"rule:{bucket}:cap", rule, ours_cap, words, period[0]) if words else
                       check(f"rule:{bucket}:cap", "not_found", ours_cap))
        if rule.get("min_spend") is not None:
            ours_min = money(rule["min_spend"])
            out.append(amount_check(f"rule:{bucket}:min_spend", rule["min_spend"], ours_min, MIN_WORD, words, terms,
                                    need=SPEND_WORD, rate=rule["rate"]) if words else
                       check(f"rule:{bucket}:min_spend", "not_found", ours_min))
        if codes:
            best = None
            for d in terms:
                # candidates: each qualifying list, each labelled row of a category table ('Dining 5811, 5812, 5814'),
                # and the lists that lie mostly inside ours taken together (a rule built from several definitions)
                cands = []
                for l in d.lists:
                    if l["kind"] == "include":
                        cands.append((l["codes"], l["cue"][0], l["end"], 1))
                        cands.extend((r[1], r[2], r[3], 1) for r in l["rows"])
                inside = [c for c in cands if len(c[0] & codes) >= 0.5 * len(c[0])]
                if len(inside) > 1:
                    big = max(inside, key=lambda c: len(c[0]))
                    cands.append((set().union(*[c[0] for c in inside]), big[1], big[2], len(inside)))
                for theirs, qa, qb, n in cands:
                    shared = len(theirs & codes)
                    score = shared / len(theirs | codes)
                    # it has to be about this rule: a good share of our codes, not one stray code in common
                    if shared and (shared >= max(2, 0.3 * len(codes)) or score >= 0.5) and (best is None or score > best[0]):
                        best = (score, d, theirs, qa, qb, n)
            if best:
                _, d, theirs, qa, qb, n = best
                status, o, t = compare_codes(codes, theirs)
                out.append(check(f"rule:{bucket}:mccs", status, o, t + (f" (from {n} lists in the document)" if n > 1 else ""),
                                 d.quote(qa, min(qb, qa + QUOTE_MAX)), d.url))
            else:
                out.append(check(f"rule:{bucket}:mccs", "not_found", mcc_count(len(codes))))
    return [verified(c, docs) for c in out]


def norm(text):
    return " ".join((text or "").split())


def verified(c, docs):
    """The gate every row passes: a quote must be in the document it names, word for word (whitespace normalised on
    both sides), at most ~300 characters. Without that a match / differs becomes not_found and nothing is shown."""
    doc = next((d for d in docs if d.url == c["url"]), None)
    quote = norm(c["quote"])
    if doc and quote and len(quote) <= QUOTE_MAX + 20 and quote in doc.norm:
        c["quote"] = quote
        return c
    return check(c["subject"], "not_found", c["ours"], method=c["method"],
                 data={"kind": "none_stated", "amount": None, "period": None, "years_free": None} if c["subject"] == "fee_waiver" else None)


# --- C. check: the model (optional) ----------------------------------------------------------------------------

MODEL_PROMPT = """You check a Singapore credit card catalogue against the bank's own terms document.
Card: {name}. Rates are in {unit}.{conv}
Catalogue facts to verify (subject: what our catalogue says):
{facts}

Document ({url}), excerpt:
<<<
{text}
>>>

For each subject say whether the document states the same thing. Reply JSON only:
{{"<subject>": {{"status": "match" | "differs" | "not_found", "theirs": "what the document says, max 15 words",
  "quote": "ONE sentence copied character for character from the excerpt that proves it"}}}}
Rules:
- quote must be copied exactly from the excerpt (no paraphrase, no ellipsis, max 300 characters).
- "match" only when the document clearly states our value for that subject; "differs" only when it clearly states another.
- The sentence must be about THIS card. A sentence about another card of the bank, or about cards in general in a
  document that covers several cards: "not_found".
- A cap in our catalogue is dollars of SPEND at the bonus rate. A cap the document gives in points or in cashback
  dollars is not the same unit: "not_found", unless the same sentence also gives the spend amount.
- If the excerpt does not clearly state it, or you would have to guess a points-to-miles conversion: "not_found"."""


def find_verbatim(body, quote):
    """`quote` as it stands in `body` (whitespace normalised on both sides), or None. This is what keeps a model's
    answer honest: no such run of words in the document, no claim."""
    quote = norm(quote)
    return quote if len(quote.split()) >= 3 and quote in norm(body) else None


def excerpt(doc, words, budget=14000, size=1200):
    """The parts of a document that mention what we ask about, in document order, within the prompt budget."""
    body = doc.body
    if len(body) <= budget:
        return body
    scored = []
    for i in range(0, len(body), size):
        chunk = body[i:i + size].lower()
        scored.append((sum(chunk.count(w) for w in words), i))
    keep = sorted(i for s, i in sorted(scored, reverse=True)[:budget // size] if s > 0)
    return "\n[...]\n".join(body[i:i + size] for i in keep)


def model_answers(card, asks, doc, ppm=None, siblings=()):
    """Ask the model about `asks` ({subject: ours}) against one document. Returns {subject: check} holding only
    answers whose quote is in the document; everything else is dropped (the caller keeps not_found)."""
    words = {"mcc", "exclu", "minimum", "capped", "annual fee", "miles", "points", "cashback", "rebate"}
    for s, ours in asks.items():
        words.update(w for w in re.findall(r"[a-z]{4,}|\d[\d,.]*\d|\d", f"{s} {ours}".lower()) if w not in ("rule", "rate"))
    text = excerpt(doc, words)
    if not text.strip():
        return {}
    cashback = card.get("kind") == "cashback"
    mine = name_words(card.get("name") or "")
    bigger = [w for w in (name_words(x) for x in siblings) if w > mine]
    conv = "" if cashback or not ppm else (f" Conversion for this card's programme: {g(ppm)} point(s) = 1 mile "
                                           f"(so N points per S$1 = N / {g(ppm)} miles per S$1).")
    prompt = MODEL_PROMPT.format(name=card["name"], unit="% cashback" if cashback else "miles per S$1 (mpd)", conv=conv,
                                 facts="\n".join(f"- {s}: {o}" for s, o in asks.items()), url=doc.url, text=text)
    reply = llm.first_json(llm.chat(prompt, max_tokens=1500))
    out = {}
    if not isinstance(reply, dict):
        return out
    for subject, a in reply.items():
        if subject not in asks or not isinstance(a, dict) or a.get("status") not in ("match", "differs"):
            continue
        quote = find_verbatim(doc.body, str(a.get("quote") or ""))
        theirs = " ".join(str(a.get("theirs") or "").split())[:200]
        if not quote or not theirs:
            continue   # HARD RULE: no verbatim sentence in the bank's document = no answer
        if len(quote) > QUOTE_MAX:
            quote = quote[:QUOTE_MAX].rsplit(" ", 1)[0]
        named = [n for n in (name_words(m.group(1)) for m in NAMING.finditer(quote)) if n]
        if named and not any((n >= mine and not any(n >= x for x in bigger)) or n <= mine for n in named if mine):
            continue   # the sentence names another card
        if not subject.endswith((":mccs", "exclusions")) and not re.search(r"\d", quote):
            continue   # a number has to be in the sentence that is supposed to state it
        out[subject] = check(subject, a["status"], asks[subject], theirs, quote, doc.url, "model")
    return out


def merge(pattern, model):
    """A pattern result that rests on a verbatim quote stands, whatever the model says (when they agree nothing changes
    either). The model only fills what the patterns could not find."""
    if pattern["status"] in ("match", "differs") and pattern.get("quote"):
        return pattern
    return model if model and model["status"] in ("match", "differs") and model.get("quote") else pattern


def model_checks(card, checks, docs, old, changed, ppm=None, force=False, stats=None, siblings=()):
    """Fill pattern 'not_found' rows with the model. A previous model answer is kept (no new call) while our value and
    the card's documents are unchanged and its quote is still in the document."""
    by_url = {d.url: d for d in docs}
    doc_changed = any(d.url in changed for d in docs)
    out, asks = [], {}
    for c in checks:
        prev = old.get(c["subject"])
        if c["status"] != "not_found" or c["subject"].endswith(":mccs") or c["subject"] in ("exclusions", "annual_fee", "fee_waiver"):
            out.append(c)   # MCC lists and fee rows stay with the deterministic readers: a model must not recite tables
        elif prev and prev["method"] == "model" and prev["ours"] == c["ours"] and not doc_changed and not force and (
                prev["status"] == "not_found" or (prev["url"] in by_url and prev["quote"] and norm(prev["quote"]) in by_url[prev["url"]].norm)):
            out.append(check(c["subject"], prev["status"], c["ours"], prev["theirs"], prev["quote"], prev["url"], "model", prev.get("data")))
        else:
            asks[c["subject"]] = c["ours"]
            out.append(c)
    if not asks:
        return out
    answers = {}
    for d in sorted((d for d in docs if d.kind in ("tc", "exclusions", "faq", "fees")), key=lambda d: KINDS.index(d.kind))[:3]:
        todo = {s: o for s, o in asks.items() if s not in answers}
        if not todo:
            break
        try:
            answers.update(model_answers(card, todo, d, ppm, siblings))
            if stats is not None:
                stats["model calls"] += 1
        except Exception as ex:
            if stats is not None:
                stats["model calls failed"] += 1
            print(f"tc: model check failed for {card['id']}: {type(ex).__name__}", flush=True)
    merged = []
    for c in out:
        if c["subject"] in asks:
            c = merge(c, answers.get(c["subject"]))
            if c["status"] == "not_found":
                c["method"] = "model"   # asked and not found: remembered, so the next run does not ask again
        merged.append(verified(c, docs))
    return merged


# --- C. check: per card ----------------------------------------------------------------------------------------------

def write_checks(conn, stats, card_id, checks):
    """Upsert the rows; drop subjects that no longer exist; log a 'differs' once (new row, or status / theirs changed)."""
    old = {r["subject"]: r for r in conn.execute("SELECT * FROM card_tc_checks WHERE card_id = %s", (card_id,)).fetchall()}
    for c in checks:
        prev = old.pop(c["subject"], None)
        conn.execute(
            "INSERT INTO card_tc_checks (card_id, subject, status, ours, theirs, quote, url, method, data, checked_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (card_id, subject) DO UPDATE SET status = EXCLUDED.status, ours = "
            "EXCLUDED.ours, theirs = EXCLUDED.theirs, quote = EXCLUDED.quote, url = EXCLUDED.url, method = EXCLUDED.method, "
            "data = EXCLUDED.data, checked_at = now()",
            (card_id, c["subject"], c["status"], c["ours"], c["theirs"], c["quote"], c["url"], c["method"],
             Jsonb(c["data"]) if c.get("data") is not None else None))
        stats[f"checks {c['status']}"] += 1
        if c["status"] == "differs" and (prev is None or prev["status"] != "differs" or prev["theirs"] != c["theirs"]):
            store.log(conn, card_id, f"T&C differs: {c['subject']}", c["ours"], c["theirs"], c["url"])
    for subject in old:
        conn.execute("DELETE FROM card_tc_checks WHERE card_id = %s AND subject = %s", (card_id, subject))


def check_card(conn, stats, card, changed=(), cache=None, force=False):
    cache = {} if cache is None else cache
    docs = []
    for r in conn.execute("SELECT d.url, d.title, l.kind FROM card_tc_docs l JOIN tc_docs d ON d.url = l.url WHERE l.card_id = %s "
                          "AND d.status = 'ok' AND d.body IS NOT NULL ORDER BY d.url", (card["id"],)).fetchall():
        if r["url"] not in cache:
            body = conn.execute("SELECT body FROM tc_docs WHERE url = %s", (r["url"],)).fetchone()["body"]
            cache[r["url"]] = Doc(r["url"], body, title=r["title"])
        d = cache[r["url"]]
        doc = Doc.__new__(Doc)          # same parsed text, this card's view of what kind of document it is
        doc.__dict__ = dict(d.__dict__, kind=r["kind"])
        doc._lists = d.lists
        docs.append(doc)
    if not docs:
        conn.execute("DELETE FROM card_tc_checks WHERE card_id = %s", (card["id"],))
        stats["cards without a readable bank document"] += 1
        return
    rules = conn.execute("SELECT * FROM card_rules WHERE card_id = %s AND valid_to IS NULL ORDER BY bucket", (card["id"],)).fetchall()
    rule_rows = {}
    for r in conn.execute("SELECT m.rule_id, m.mcc, m.effect FROM card_rule_mccs m JOIN card_rules r ON r.id = m.rule_id WHERE "
                          "r.card_id = %s AND r.valid_to IS NULL", (card["id"],)).fetchall():
        rule_rows.setdefault(r["rule_id"], []).append(r)
    exclusions = {r["mcc"] for r in conn.execute(
        "SELECT mcc FROM card_mcc_exclusions WHERE card_id = %s AND valid_to IS NULL", (card["id"],)).fetchall()}
    ppm = None
    if card.get("programme_id"):
        ppm = (conn.execute("SELECT points_per_mile FROM programmes WHERE id = %s", (card["programme_id"],)).fetchone() or {}
               ).get("points_per_mile")
    siblings = [r["name"] for r in conn.execute("SELECT name FROM cards WHERE bank_id = %s AND id <> %s AND status = 'active'",
                                                (card["bank_id"], card["id"])).fetchall()]
    checks = pattern_checks(card, rules, rule_rows, exclusions, docs, ppm, siblings)
    # The model path has only run against a stub so far: it stays off until an admin switches it on (App page).
    if llm.key() and config.flag("tc_model"):
        old = {r["subject"]: r for r in conn.execute("SELECT * FROM card_tc_checks WHERE card_id = %s", (card["id"],)).fetchall()}
        checks = model_checks(card, checks, docs, old, changed, ppm, force, stats, siblings)
    write_checks(conn, stats, card["id"], checks)
    stats["cards checked"] += 1


def run_tc(conn, stats, today):
    """Catalogue source 'tc'. Documents per run and the pause between requests are App page settings. One run can be
    narrowed with `catalogue --only tc --card ID` and made to re-read everything with `--force` (TC_ONLY, TC_FORCE)."""
    limit, only = int(config.number("tc_limit")), os.environ.get("TC_ONLY") or None
    force = os.environ.get("TC_FORCE") == "1"
    discover(conn, stats, only)
    conn.commit()
    changed = fetch_all(conn, stats, limit, only, force) if limit > 0 else set()
    cache = {}
    cards = conn.execute("SELECT id, bank_id, name, network, kind, programme_id, base_sgd, base_fcy, annual_fee FROM cards WHERE status = "
                         "'active' AND (%s::text IS NULL OR id = %s) ORDER BY id", (only, only)).fetchall()
    for card in cards:
        try:
            check_card(conn, stats, card, changed, cache, force)
            conn.commit()
        except Exception as ex:
            conn.rollback()
            stats["cards failed"] += 1
            print(f"tc: check failed for {card['id']}: {type(ex).__name__}: {str(ex)[:200]}", flush=True)


# --- read API for the web layer --------------------------------------------------------------------------------------

def summary(conn, card_id):
    """Everything the card page shows about its bank documents and what was checked against them."""
    docs = conn.execute("SELECT d.url, d.title, l.kind, d.status, d.fetched_at, d.changed_at, d.pages FROM card_tc_docs l JOIN "
                        "tc_docs d ON d.url = l.url WHERE l.card_id = %s ORDER BY array_position(%s::text[], l.kind), d.url",
                        (card_id, list(KINDS))).fetchall()
    checks = conn.execute("SELECT subject, status, ours, theirs, quote, url, method, checked_at, data FROM card_tc_checks WHERE "
                          "card_id = %s ORDER BY array_position(ARRAY['differs', 'match', 'info', 'not_found'], status), subject",
                          (card_id,)).fetchall()
    counts = {"match": 0, "differs": 0, "not_found": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1   # 'info' (the bank states a fact we hold no figure for) too
    return {"docs": [dict(d) for d in docs], "checks": [dict(c) for c in checks], "counts": counts}


def badge(counts):
    """'differs' as soon as one fact disagrees with the bank; 'verified' needs at least one confirmed fact."""
    if counts.get("differs"):
        return "differs"
    return "verified" if counts.get("match") else "unverified"


def main():
    from . import db
    stats = Counter()
    with db.connect() as conn:
        run_tc(conn, stats, datetime.date.today())
    print("tc: " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items())), flush=True)


if __name__ == "__main__":
    main()
