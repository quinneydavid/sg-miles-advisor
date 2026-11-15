-- cards schema v1 (9 Nov 2026). Catalogue tables = public data, plain. Personal tables (spend, uploads) come in a
-- later migration with app-level encryption (crypto.py).

CREATE TABLE IF NOT EXISTS schema_version (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS banks (
    id          text PRIMARY KEY,               -- 'dbs', 'uob', 'ocbc', 'citi', 'hsbc', 'sc', 'maybank', 'amex', ...
    name        text NOT NULL,
    listing_url text
);

CREATE TABLE IF NOT EXISTS programmes (
    id              text PRIMARY KEY,           -- 'DBS_POINTS', 'UNI$', 'KF', 'CASHBACK', ...
    name            text NOT NULL,
    bank_id         text REFERENCES banks(id),
    points_per_mile numeric,                    -- currency units per 1 KF mile (cashback: NULL)
    expiry          text
);

CREATE TABLE IF NOT EXISTS cards (
    id            text PRIMARY KEY,             -- slug: 'dbs_womens_world'
    bank_id       text NOT NULL REFERENCES banks(id),
    name          text NOT NULL,
    network       text,                         -- visa / mastercard / amex
    kind          text NOT NULL DEFAULT 'miles' CHECK (kind IN ('miles', 'cashback', 'points', 'other')),
    programme_id  text REFERENCES programmes(id),
    annual_fee    numeric,
    fee_waiver    text,
    min_income    numeric,
    base_sgd      numeric,                      -- mpd, or % for cashback
    base_fcy      numeric,
    rounding      text,
    url           text,
    tc_url        text,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'discontinued')),
    depth         text NOT NULL DEFAULT 'headline' CHECK (depth IN ('headline', 'full')),
    needs_check   boolean NOT NULL DEFAULT false,
    notes         text,
    first_seen    date NOT NULL DEFAULT current_date,
    checked_at    timestamptz
);

-- one row per bonus bucket per version; a change closes the row (valid_to) and opens a new one
CREATE TABLE IF NOT EXISTS card_rules (
    id             bigserial PRIMARY KEY,
    card_id        text NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    bucket         text NOT NULL,               -- 'online', 'contactless', 'fcy', 'dining', ...
    label          text,
    rate           numeric NOT NULL,            -- mpd, or % cashback
    match          jsonb NOT NULL DEFAULT '{}', -- engine match: {"channel": "online"} / {"mcc": [...]} / {"fcy": true}
    cap            numeric,                     -- S$ of spend at the bonus rate
    cap_period     text CHECK (cap_period IN ('calendar', 'statement', 'match_day', 'membership_year', 'none')),
    min_spend      numeric,
    min_period     text,
    rounding       text,
    mcc_list_known boolean NOT NULL DEFAULT false,
    conditions     text,
    promo_until    date,
    depth          text NOT NULL DEFAULT 'headline' CHECK (depth IN ('headline', 'full')),
    source_url     text,
    valid_from     date NOT NULL DEFAULT current_date,
    valid_to       date,
    checked_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS card_rules_current ON card_rules (card_id) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS mcc_codes (
    mcc       text PRIMARY KEY,                 -- 4 digits, kept as text ('0742')
    name      text NOT NULL,
    grp       text                              -- 'dining', 'transport', 'airline', ...
);

-- the MCCs a rule names: include (earns the bonus) / exclude (earns nothing or base) / reduced (own rate)
CREATE TABLE IF NOT EXISTS card_rule_mccs (
    rule_id  bigint NOT NULL REFERENCES card_rules(id) ON DELETE CASCADE,
    mcc      text NOT NULL,
    effect   text NOT NULL CHECK (effect IN ('include', 'exclude', 'reduced')),
    rate     numeric,
    PRIMARY KEY (rule_id, mcc)
);
CREATE INDEX IF NOT EXISTS card_rule_mccs_mcc ON card_rule_mccs (mcc);

-- card-wide MCC exclusions (no points at all on this card), versioned like rules
CREATE TABLE IF NOT EXISTS card_mcc_exclusions (
    card_id    text NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    mcc        text NOT NULL,
    valid_from date NOT NULL DEFAULT current_date,
    valid_to   date,
    source_url text,
    PRIMARY KEY (card_id, mcc, valid_from)
);

-- data points: merchant X coded MCC Y did / didn't earn on card Z
CREATE TABLE IF NOT EXISTS mcc_reports (
    id        bigserial PRIMARY KEY,
    card_id   text REFERENCES cards(id) ON DELETE CASCADE,
    merchant  text NOT NULL,                    -- public merchant name only (never a person's transaction)
    mcc       text,
    outcome   text CHECK (outcome IN ('earned', 'not_earned', 'base_only', 'unknown')),
    source    text NOT NULL,                    -- 'wiki' | 'reconciled'
    reported  date,
    detail    text,
    UNIQUE (card_id, merchant, mcc, source)
);
CREATE INDEX IF NOT EXISTS mcc_reports_mcc ON mcc_reports (mcc);

CREATE TABLE IF NOT EXISTS signup_offers (
    id           bigserial PRIMARY KEY,
    card_id      text REFERENCES cards(id) ON DELETE CASCADE,
    card_name    text NOT NULL,                 -- kept even when the card isn't in the catalogue yet
    bonus        numeric NOT NULL,              -- miles (or S$ for cashback)
    bonus_unit   text NOT NULL DEFAULT 'miles',
    min_spend    numeric,
    window_text  text,
    ends         date,
    fee_text     text,
    eligibility  text,                          -- new / existing customer rules
    cardup       text,
    cents_per_mile numeric,
    source_url   text,
    first_seen   date NOT NULL DEFAULT current_date,
    last_seen    date NOT NULL DEFAULT current_date,
    active       boolean NOT NULL DEFAULT true,
    UNIQUE (card_name, bonus, ends)
);

CREATE TABLE IF NOT EXISTS transfer_partners (
    programme_id text NOT NULL REFERENCES programmes(id) ON DELETE CASCADE,
    partner      text NOT NULL,                 -- 'KrisFlyer', 'Asia Miles', ...
    ratio_from   numeric NOT NULL,
    ratio_to     numeric NOT NULL,
    block        numeric,
    fee          text,
    source_url   text,
    checked_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (programme_id, partner)
);

CREATE TABLE IF NOT EXISTS catalogue_changes (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    card_id    text,
    what       text NOT NULL,                   -- 'new card', 'rate', 'cap', 'fee', 'offer', 'mcc', 'discontinued'
    old_value  text,
    new_value  text,
    source_url text
);

CREATE TABLE IF NOT EXISTS sources (
    url         text PRIMARY KEY,
    kind        text NOT NULL,                  -- 'bank_listing', 'card_page', 'tc_pdf', 'blog', 'wiki'
    last_status int,
    last_hash   text,
    fetched_at  timestamptz
);

-- people who may use the app: id = the login name the proxy sends as Remote-User (added by an admin on the App page)
CREATE TABLE IF NOT EXISTS people (
    id            text PRIMARY KEY,             -- 'alice', 'bob'
    display       text NOT NULL,
    excluded_banks text[] NOT NULL DEFAULT '{}',
    held_cards    text[] NOT NULL DEFAULT '{}'
);


-- MCCs named in a card's T&C documents (SG Miles wiki extraction): whitelist / blacklist / mention, with the PDF
CREATE TABLE IF NOT EXISTS card_tc_mccs (
    card_id    text NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    mcc        text NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('whitelist', 'blacklist', 'mention')),
    source_url text NOT NULL,
    seen       date NOT NULL DEFAULT current_date,
    PRIMARY KEY (card_id, mcc, kind, source_url)
);

-- public merchant -> MCC references (wiki + check-mcc), used to rate a transaction when the bank gives no MCC
CREATE TABLE IF NOT EXISTS merchants (
    slug    text PRIMARY KEY,
    name    text NOT NULL,
    mccs    text[] NOT NULL DEFAULT '{}',
    source  text NOT NULL,
    seen    date NOT NULL DEFAULT current_date
);
CREATE INDEX IF NOT EXISTS merchants_name ON merchants (lower(name));

ALTER TABLE cards ADD COLUMN IF NOT EXISTS wiki_slug text;
-- household additions/corrections: beat public data, never overwritten by the weekly refresh
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS channel text CHECK (channel IN ('online', 'in_person', 'both'));
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS added_by text;
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE signup_offers ADD COLUMN IF NOT EXISTS total_spend numeric;

-- boosts: keep money somewhere (or meet a condition) and a card earns more, e.g. UOB Lady's Savings balance tiers on
-- Lady's Solitaire, HSBC Everyday Global Account S$50K -> Revolution 8 mpd. tiers = [[amount, value], ...] ascending;
-- mode 'add' = +value mpd on the matching rules, 'set' = rate becomes value (cap_override replaces the cap)
CREATE TABLE IF NOT EXISTS card_boosts (
    id           bigserial PRIMARY KEY,
    card_id      text NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    label        text NOT NULL,                 -- 'UOB Lady''s Savings balance'
    requirement  text,                          -- how it's measured: 'monthly average balance', ...
    tiers        jsonb NOT NULL,
    mode         text NOT NULL DEFAULT 'add' CHECK (mode IN ('add', 'set')),
    cap_override numeric,
    applies_to   text[] NOT NULL DEFAULT '{}',  -- rule buckets; empty = every bonus rule of the card
    depth        text NOT NULL DEFAULT 'headline' CHECK (depth IN ('headline', 'full')),
    source_url   text,
    added_by     text,
    UNIQUE (card_id, label)
);
-- household photos of card fronts (shared), served from /img/<card_id>
CREATE TABLE IF NOT EXISTS card_images (
    card_id    text PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
    mime       text NOT NULL,
    data       bytea NOT NULL,
    added_by   text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE card_images ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'household';   -- household | search
ALTER TABLE card_images ADD COLUMN IF NOT EXISTS source_url text;
ALTER TABLE cards ADD COLUMN IF NOT EXISTS image_blocked boolean NOT NULL DEFAULT false;      -- someone removed the found picture
-- each person's own amount per boost (e.g. {"12": 10006}) - their balance, not shared
ALTER TABLE people ADD COLUMN IF NOT EXISTS boost_amounts jsonb NOT NULL DEFAULT '{}';
-- from the person's feeder: card id -> statement closing day; boost id -> {"month", "source"} for balances it measures
ALTER TABLE people ADD COLUMN IF NOT EXISTS statement_days jsonb NOT NULL DEFAULT '{}';
ALTER TABLE people ADD COLUMN IF NOT EXISTS boost_auto jsonb NOT NULL DEFAULT '{}';
-- miles wallet from the feeder (KrisFlyer balance + bank pools in transfer blocks): encrypted, it's personal
ALTER TABLE people ADD COLUMN IF NOT EXISTS wallet_enc bytea;
ALTER TABLE people ADD COLUMN IF NOT EXISTS wallet_at timestamptz;
-- per person, encrypted: {"last4": {"1234": card id}, "pdf_passwords": [...]} (ingest of alerts + statement PDFs)
ALTER TABLE people ADD COLUMN IF NOT EXISTS settings_enc bytea;
-- per person, encrypted: rewards balances printed on statements {programme: {balance, date, bank, expiry}}
ALTER TABLE people ADD COLUMN IF NOT EXISTS points_enc bytea;

-- what the ingest already handled (keyed HMAC of the mail Message-ID / file name+size), per person
CREATE TABLE IF NOT EXISTS ingest_seen (
    person  text NOT NULL REFERENCES people(id),
    kind    text NOT NULL CHECK (kind IN ('mail', 'file')),
    hmac    text NOT NULL,
    outcome text NOT NULL,                     -- 'alert', 'statement', 'duplicate', 'skipped', 'failed'
    at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (person, kind, hmac)
);
ALTER TABLE signup_offers ADD COLUMN IF NOT EXISTS income_req text;

CREATE TABLE IF NOT EXISTS meta (key text PRIMARY KEY, value text NOT NULL);

-- award prices from SIN (public data, sent by a feeder): one-way miles per cabin
CREATE TABLE IF NOT EXISTS award_prices (
    dest       text NOT NULL,                  -- airport code
    cabin      text NOT NULL CHECK (cabin IN ('Y', 'W', 'J', 'F')),
    city       text,
    region     text,
    saver      int,
    advantage  int,
    seen_dates int,
    next_date  date,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dest, cabin)
);

-- flight goals per person (routes only, no money values)
CREATE TABLE IF NOT EXISTS goals (
    id         text PRIMARY KEY,
    person     text NOT NULL REFERENCES people(id),
    dest       text NOT NULL,
    cabin      text NOT NULL CHECK (cabin IN ('Y', 'W', 'J', 'F')),
    trip       text NOT NULL DEFAULT 'oneway' CHECK (trip IN ('oneway', 'return')),
    pax        int NOT NULL DEFAULT 1 CHECK (pax BETWEEN 1 AND 6),
    miles      int,                            -- override; NULL = Saver price x trip x pax
    position   int NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS goals_person ON goals (person, position);

-- ===== personal data: values encrypted in the app (crypto.py) =====
-- plain columns are only ids, person, card ids, dates and small enums; every value lives in an *_enc bytea column.
CREATE TABLE IF NOT EXISTS encryption_keys (
    version    int PRIMARY KEY,
    wrapped    text NOT NULL,                   -- the data key, wrapped by Vault Transit or the local key
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS statements (
    id          text PRIMARY KEY,               -- uuid4 hex (also the AAD row key)
    person      text NOT NULL REFERENCES people(id),
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL CHECK (status IN ('reading', 'needs_cards', 'ready', 'failed')),
    period_from date,
    period_to   date,
    n_lines     int NOT NULL DEFAULT 0,
    checked     boolean NOT NULL DEFAULT false, -- lines add up to the printed total
    meta_enc    bytea                           -- {bank, printed_total, sum, sections, error, file_name}
);
CREATE INDEX IF NOT EXISTS statements_person ON statements (person, uploaded_at DESC);
-- duplicate detection (keyed HMACs: reveal nothing without the app's data key)
ALTER TABLE statements ADD COLUMN IF NOT EXISTS file_hmac text;     -- same PDF bytes
ALTER TABLE statements ADD COLUMN IF NOT EXISTS fingerprint text;   -- same statement: bank + card last4s + period end + printed total
CREATE INDEX IF NOT EXISTS statements_dupes ON statements (person, file_hmac);

CREATE TABLE IF NOT EXISTS stmt_lines (
    id           text PRIMARY KEY,              -- uuid4 hex
    statement_id text NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    person       text NOT NULL REFERENCES people(id),
    card_id      text REFERENCES cards(id),     -- the card it was charged to (set once sections are mapped)
    section      text NOT NULL DEFAULT '',      -- which card section of the statement (hash, not the printed name)
    txn_date     date NOT NULL,
    data_enc     bytea NOT NULL                 -- {merchant, amount, currency, fcy, online, mcc, mcc_src}
);
CREATE INDEX IF NOT EXISTS stmt_lines_person ON stmt_lines (person, txn_date);


INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;

-- ===== 0.4.0 =====
-- Bank T&C documents (public): fetched ONLY from the bank's own website. Text kept so a rule can be shown next to the
-- sentence it rests on, and a changed document (sha256) is noticed.
CREATE TABLE IF NOT EXISTS tc_docs (
    url        text PRIMARY KEY,
    bank_id    text REFERENCES banks(id),
    title      text,
    status     text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'ok', 'failed', 'unreadable', 'blocked')),
    http_status int,
    sha256     text,
    pages      int,
    body       text,                            -- extracted text
    fetched_at timestamptz,
    changed_at timestamptz,                     -- last time the sha256 changed
    error      text
);
CREATE TABLE IF NOT EXISTS card_tc_docs (
    card_id text NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    url     text NOT NULL REFERENCES tc_docs(url) ON DELETE CASCADE,
    kind    text NOT NULL DEFAULT 'tc' CHECK (kind IN ('tc', 'exclusions', 'faq', 'promo', 'fees')),
    found_via text,                             -- where the link came from (bank card page, wiki index, hand)
    PRIMARY KEY (card_id, url)
);
-- One row per thing we claim about a card, checked against its bank documents. subject: 'base_sgd', 'base_fcy',
-- 'annual_fee', 'exclusions', 'rule:<bucket>:rate', 'rule:<bucket>:cap', 'rule:<bucket>:min_spend', 'rule:<bucket>:mccs'.
CREATE TABLE IF NOT EXISTS card_tc_checks (
    card_id    text NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    subject    text NOT NULL,
    status     text NOT NULL CHECK (status IN ('match', 'differs', 'not_found', 'info')),   -- info = the bank states a fact we had no figure for
    ours       text,                            -- what the catalogue says
    theirs     text,                            -- what the bank document says (null when not found)
    quote      text,                            -- verbatim sentence from the document (must occur in tc_docs.body)
    url        text REFERENCES tc_docs(url) ON DELETE SET NULL,
    method     text,                            -- 'pattern' | 'model'
    data       jsonb,                           -- structured value: annual_fee {"fee": 196.2, ...}, fee_waiver {"kind": "spend", "amount": 25000, ...}
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (card_id, subject)
);

-- Cloudflare Access login (public door): keyed hash of the person's login email, learned at an internal
-- (forward-auth proxy) login. No plaintext email is stored.
ALTER TABLE people ADD COLUMN IF NOT EXISTS email_hmac text;

-- Written card advice (the model's paragraph for one person + card or wallet). Encrypted: it quotes their numbers.
CREATE TABLE IF NOT EXISTS advice (
    person     text NOT NULL REFERENCES people(id),
    subject    text NOT NULL,                   -- 'card:<id>' | 'wallet'
    facts_hmac text NOT NULL,                   -- keyed hash of the facts it was written from
    status     text NOT NULL DEFAULT 'writing' CHECK (status IN ('writing', 'ready', 'failed')),
    text_enc   bytea,
    at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (person, subject)
);

-- What each held card really costs the person a year (0 when the bank waives it): {"card id": 196.2}. The catalogue
-- fee is the list price and unknown for most cards; the advisor needs the real figure to say keep or cancel.
ALTER TABLE people ADD COLUMN IF NOT EXISTS card_fees jsonb NOT NULL DEFAULT '{}';

-- How the person pays in shops: 'mobile' = phone or watch only (Apple Pay; never the physical card), 'mixed' = phone
-- mostly, the card sometimes, 'tap' = the physical card. NULL = not said yet (treated as mixed). Drives the default on
-- Pay, how in-store lines are replayed, and the routing table the bots read.
ALTER TABLE people ADD COLUMN IF NOT EXISTS pay_method text;

-- Annual fee tracking per person. fee_dates: {"card id": {"month": 3}} entered in Settings (the month the fee posts).
-- fees_enc: fee charges and reversals seen on the person's statements / feed (amounts are theirs, so encrypted):
-- [{"card": id, "date": "2026-03-14", "amount": 196.2, "text": "ANNUAL FEE"}] (negative amount = waived / reversed).
ALTER TABLE people ADD COLUMN IF NOT EXISTS fee_dates jsonb NOT NULL DEFAULT '{}';
ALTER TABLE people ADD COLUMN IF NOT EXISTS fees_enc bytea;

-- The bank's own promotion terms for a sign-up offer, checked by hand (cards/data/offer-terms.json): who qualifies
-- (new-to-bank only / existing too / smaller gift for existing), what "new" means, the clause quoted, the document URL.
ALTER TABLE signup_offers ADD COLUMN IF NOT EXISTS terms jsonb;

-- 0.4.7: MileLion's review of the card (links.py); cards.url = the bank's own card page
ALTER TABLE cards ADD COLUMN IF NOT EXISTS review_url text;
ALTER TABLE cards ADD COLUMN IF NOT EXISTS review_title text;

-- 0.6.0: settings kept in the app instead of the environment. scope '' = the whole app, otherwise a person id.
-- value = plain settings (addresses, switches, the SHA-256 of an API token); secret = passwords and keys, encrypted
-- by the app (crypto.py, AAD "app_settings.secret:<scope>:<key>") and never shown again.
CREATE TABLE IF NOT EXISTS app_settings (
    scope      text NOT NULL DEFAULT '',
    key        text NOT NULL,
    value      text,
    secret     bytea,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
-- Admins see the App page (people, model, messages, catalogue). The first person to open Settings while nobody is
-- admin becomes one; admins can make others admin.
ALTER TABLE people ADD COLUMN IF NOT EXISTS admin boolean NOT NULL DEFAULT false;

-- 0.8.0: one source of transactions per card (pipeline.py). {card id: 'wealthfolio' | 'mail' | 'feed'}; a card not
-- named here is worked out from what is there.
ALTER TABLE people ADD COLUMN IF NOT EXISTS card_sources jsonb NOT NULL DEFAULT '{}';
-- where each statement closing day in statement_days came from: {card id: 'you' | 'statement' | 'feed'}
ALTER TABLE people ADD COLUMN IF NOT EXISTS statement_days_src jsonb NOT NULL DEFAULT '{}';
-- 0.8.3: suggestions are miles cards only unless the person asks for cashback cards too
ALTER TABLE people ADD COLUMN IF NOT EXISTS cashback_too boolean NOT NULL DEFAULT false;
-- 0.9.0: results of the matches some card rules depend on (fixtures.py): public data
CREATE TABLE IF NOT EXISTS match_results (
    fixtures   text NOT NULL,                  -- 'football-data/<competition>/<team id>', as named in data/cards.json
    day        date NOT NULL,                  -- the match's start date in the United Kingdom
    result     text NOT NULL,                  -- win | draw | loss | pending | other
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fixtures, day)
);
-- 0.9.0: when a feeder last sent anything for this person (pages stop showing its numbers when it goes quiet)
ALTER TABLE people ADD COLUMN IF NOT EXISTS feed_at timestamptz;

-- 0.10.8: shops Atome (buy now, pay in 3) lists in Singapore, from atome.sg's own merchant sitemap (catalogue step
-- `atome`). Atome charges the card as MCC 5999 online, one third a month, so a big purchase can spread over 3 months
-- of bonus limits. Rows not seen for 30 days are dropped.
CREATE TABLE IF NOT EXISTS atome_merchants (
    slug text PRIMARY KEY,
    seen date NOT NULL DEFAULT current_date
);
