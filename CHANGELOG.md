# Changelog

## 0.10.8
- Which card? takes the price (optional). Each card then shows the miles that purchase earns: the part that fits the bonus limit left this period earns the bonus rate, the rest the card's normal rate, and the cards are ranked by miles earned. A purchase that reaches a card's minimum spend by itself counts as reaching it. When the best card's bonus limit runs out part way, a tip says how to split the bill with the next card. The bot API takes `amt` and answers `for_price` per card.
- Which card? says when Atome lists the shop (new catalogue step `atome` reads the shop list from atome.sg's own sitemap each week). With a price it compares paying in 3 Atome parts (charged as shop type 5999 online, one part a month, the next two in fresh bonus-limit months) with paying at once. The bot API answers `atome`. Atome changes the shop type: the panel says so (5999 online instead of the shop's own type), so a shop type a card excludes, such as schools, can still earn through Atome; without a price it compares the best rate through Atome with the best rate direct. Warnings where people report Atome not earning: UOB Preferred Visa, and education shops.
- Which card? says when the shop is on Kris+ and how many KrisPay miles per S$ it gives (new catalogue step `kris` reads MainlyMiles' monthly list of Kris+ earning shops). Kris+ keeps the shop's own type but the bank sees an online payment, so the panel rates your cards as an online payment, adds the KrisPay miles and compares with paying direct. The bot API answers `kris_plus`.

## 0.10.7
- Maybank World Mastercard: the bank's 10X TREATS Points shops (4 miles per S$) besides petrol: the listed golf clubs, Imperial Treasure, the Les Amis group restaurants, Valiram stores and Luxury Galleria at Resorts World Sentosa (capped at 50,000 points, counted per calendar month). 10X is never paid online, so it is matched for payments in a shop only, petrol included (new channel `in_store`). Read from the bank's card page on 14 Nov 2026.

## 0.10.6
- UOB Visa Signature: each S$1,000 minimum counts only its own kind of spend (foreign currency for the foreign bonus; taps, petrol and SimplyGo in Singapore dollars for the local one), as UOB's terms say. A foreign tap no longer counts toward the local minimum. Rule option `min_own`.
- Maybank Man Utd: the Selected Categories of the card's terms (medical, schools, hospitals, telecoms, rent and others) earn 0.4 miles per S$ in Singapore dollars and never a match-day bonus. A rule can now set a rate below the card's normal one (`sets_base`).
- OCBC Rewards: spend in the 6 mpd promotion uses up the 4 mpd bonus limit as well, since OCBC may count it toward the same 10,000 OCBC$ (`shares_cap`).
- Agoda and Kaligo bonuses that are paid only when booked through the bank's own link (Citi PremierMiles, UOB PRVI Miles) are shown on Which card? with that condition, but past spend counts at the normal rate (`link_only`).
- Shop-name lists from the catalogue find the name inside statement text ("COLD STORAGE - JELITA") instead of matching the whole text only. Lists stored by earlier versions are rewritten at the next start.

## 0.10.5
- DBS yuu Visa and DBS yuu American Express are hand-checked against DBS's own terms (updated 30 Oct 2026): 2.78 miles per S$ at yuu shops with no minimum; 10 miles per S$ in a calendar month with S$800 of eligible spend on the card and purchases at 4 different yuu shops; both draw on one bonus limit of 28,800 yuu Points (8,000 miles) a month; 0.14 miles per S$ elsewhere; DBS's exclusion list; Singapore only. Shop names now match the way they appear on statements (for example "COLD STORAGE - JELITA", "BUS/MRT").
- A minimum can now also ask for purchases at a number of different shops (`min_shops` in a rule's match). Wallet, Which card?, the summary and the alerts say how many shops are still needed and which ones are open.
- A higher tier can replace a lower one for the period once its target is met (`instead_of`), so two tiers that share one bonus limit are not both counted.

## 0.10.4
- Which card? no longer lists "Your usual places" under the search box.

## 0.10.3
- Renamed SG Miles Advisor. The repository is `quinneydavid/sg-miles-advisor` and the image is `ghcr.io/quinneydavid/sg-miles-advisor`; the old repository address redirects. An installation that names the old image keeps running on 0.10.2 until its compose file names the new one. No change to the data or the settings.
- README: the problem the app solves comes first, then what each part does.

## 0.10.2
The first version published on GitHub.
- With `CARDS_SINGLE_USER` (no login) the app answers only when it is opened as `127.0.0.1`, `localhost` or `[::1]`, or under a name listed in the new `CARDS_HOSTS` (comma-separated). Any other `Host` gets a 421. This stops a web page that points its own name at 127.0.0.1 (DNS rebinding) from reading the app. `/healthz` is not checked. A single-user deployment that is opened under another name (a proxy, a container name, a LAN address) must set `CARDS_HOSTS`.
- The picture search fetches only `http` and `https` addresses.
- Every page ends with one line: not financial advice, not affiliated with any bank or card issuer.
- The seeded sign-up offers (`recommended-cards.json`) state the offer only: who it is for, the fee, the minimum spend and the cost per mile by the app's own sum. An existing database keeps one row per offer; its wording is updated at the next start.
- `compose.yaml` names the published image (`ghcr.io/quinneydavid/sg-card-advisor`), so `docker compose up` needs no build, and has a health check for the app. `PGPASSWORD` in `.env.example` is empty and must be chosen.
- CI runs the tests on Python 3.12, where `pgserver` installs. `catalogue --only` help lists `seats` and `matches`.
- The README has screenshots, all of invented demo data. `tools/demo/screenshots.py` retakes them from a throwaway installation; `tools/demo/seed.py` loads the demo household and refuses anything but a fresh single-user installation on 127.0.0.1. Neither is in the image.
- App settings: the people in the list no longer touch each other.

## 0.10.1
- Compare shows every chosen card (up to 24, the person's own first) in a compact table that uses the whole window width; on a phone the kind of purchase stays in view while the cards scroll sideways. The rule behind a rate is in its tooltip.

## 0.10.0
- Flights is its own section: "Where can I go" lists every destination from Singapore for a cabin, one-way or return, for one to four people, against the miles on hand, with the month the rest would be earned and the next date with Saver seats; "Save for it" adds a flight to My flights (the former Flight goals page, moved here from Wallet).
- Cards: "My cards" beside "All cards", tick boxes on every card and a Compare page that puts two to eight cards side by side: what each earns for the usual kinds of purchase (best in each row shaded), fee, sign-up offer, programme.

## 0.9.1
- Match results are asked for every 10 minutes while a match day's result is pending (3 hours otherwise), so a win or draw shows soon after the final whistle.

## 0.9.0
Nothing needs a feeder any more.
- Points: the miles wallet is worked out in the app. Per programme the newer of the statement balance and a balance typed on the Points page, plus points earned since; the KrisFlyer account typed, from a statement, or kept current by AwardWallet (optional, the person's own Business API key on Settings). `/api/wallet` and `/api/summary` for bots.
- Match-day rates: card rules that pay more on a win or draw day now work, with results from football-data.org (optional key in App settings). Caps per match day and bonus days per month are honoured; a purchase on a day without a result yet is marked as possibly rising. The Pay page says how today stands.
- Dates with award seats: optional seats.aero key in App settings; the weekly run fills them (`catalogue --only seats`).
- Annual fees and their reversals are read from the person's own lines from any source; tested with Wealthfolio history.
- Wealthfolio notes may name the foreign currency: a remark that is exactly `USD 20.00`, or an alert remark with `estimated from USD 20.00`. The named currency wins over text detection, the foreign amount is shown, and estimated lines say "estimated, statement pending".
- Settings suggests the savings account for a boosted rate when one account's name fits.
- A feeder is optional everywhere: one that sent nothing for three days no longer speaks for the miles wallet, Settings shows when it last sent anything, and "Stop listening to the feeder" removes its token and what only it supplied.
- New: table `match_results`, column `people.feed_at`. Outside services the app may call, all optional: `api.football-data.org`, `seats.aero`, `business.awardwallet.com`.

## 0.8.3
- Advice is about miles: cashback cards are no longer suggested as a way to earn miles (rankings, better-card tips, "could have earned", Advisor, the bot answers). A cashback card the person holds still counts, and Settings has a switch to include cashback cards again (valued at 1.5¢ a mile).

## 0.8.2
- Catalogue: a card listed under both miles and cashback on the public wiki is one kind with one unit. Before, such a card was marked cashback while keeping its miles rules, so a 4 mpd rule was read as 4% (and valued at 2.67 mpd) in rankings and suggestions. Miles win when the card has any miles rule; the other unit's rows are left out.

## 0.8.1
- Points-conversion fees ("FFP CONVERSION FEE", "... CONVERSION FEE") and lines that start with "GST" are the bank's own charges, not purchases.
- The Wealthfolio history import keeps a month it reads again in step: a line that is gone upstream, or is now recognised as a bank charge, is removed.

## 0.8.0
- One source of transactions per card. Settings shows where each held card's lines come from (Wealthfolio, mail and
  statement files, or a feeder) and moves a card between them. The import paths and the feed respect the choice; where
  nothing is chosen the source is worked out from what is there. Someone who uses only mail and statement files sees
  nothing new to set.
- One clean-up for every source (`pipeline.py`). Wealthfolio lines, statement files, alert mails and feed lines go
  through the same steps, so the same text gives the same merchant, category, currency and online answer. Statement
  and alert lines now also recognise a purchase abroad from its text, and drop the bank's own charges.
- How a purchase was paid. A "How was this paid?" choice on each purchase is remembered for that shop and used for the
  open month and after. Order: the source, the saved answer, an online shop from the text, the usual way (Settings).
  Settings lists the remembered shops. The rating of a line now uses this, not only online or not.
- Statement closing day per card on Settings, filled from a statement file when one is read. A typed day is kept.
- Wealthfolio:
  - "Import history" reads everything older than the rolling window once, a month at a time, in the background, with
    progress, final counts and the oldest purchase found on the page; `wealthfolio --person ID --since DATE` or `--all`
    by hand. Safe to run again. A month in which a card already has lines from another source is left alone and listed.
  - The rolling import no longer removes any line for being older than the window, and no longer removes the lines of
    a card a feeder also sends: one source speaks for a card and month when lines are read.
  - The merchant is the activity's note; a remark after ` | ` is split off. A remark starting with `alert` marks an
    unconfirmed line; one naming a statement month gives a first estimate of the card's closing day.
  - Only credit-card accounts are suggested as cards when Wealthfolio types its accounts.
  - A savings account can be named for a card's balance-linked boost; its monthly average balance is worked out from
    the running balance on its activities. No further token scope is needed.
- Past months: a purchase made before a recorded rule change is valued with the rule of its time. The Spend pages say
  when past months are valued with today's rules instead.
- Flight-goal prices are loaded from the shipped Singapore Airlines chart by `seed` and by the weekly catalogue run
  (`--only awards`), so goals work without a feeder. A feeder's fresher rows (with seat dates) are kept for 30 days.
- Tests that need a database run on a throwaway Postgres (`pip install pgserver`); they are skipped where it is missing.

## 0.7.0
- Wealthfolio connection. On the Settings page a person enters their Wealthfolio address and a read-only access token,
  says which account is which card, and the app imports the purchases and refunds of those accounts. "Save and test"
  names what is wrong (address, token, scope). The import runs with `ingest`, or by hand with `wealthfolio`. It keeps a
  rolling window in step with Wealthfolio, skips a card a feeder already sends, and never writes to Wealthfolio.
- The line cache now notices a line that was corrected in place.

## 0.6.1
- `import-env --admin ID` also makes an existing person an admin, for deployments set up without opening the page.

## 0.6.0

Renamed sg-card-advisor and prepared for release; not published.

- Settings live in the app. The environment keeps only the database, the key source, the sign-in door and the drop
  folder path. An environment value still works as a starting value until the setting is saved in the app;
  `import-env` stores them all.
- Settings page: mailbox connection with provider presets, a connection test that says what failed, the time and
  counts of the last mail read, and setup steps written for the person's provider and banks.
- Gmail filter file download (`mailFilters.xml`) with one rule per bank the person holds a card from.
- Feeder and bot tokens are made on the Settings page, shown once and stored as a SHA-256 fingerprint.
- App settings page for admins: people, the model, Telegram messages, the catalogue run; test buttons for the model
  and for messages. The first person to open Settings becomes the admin.
- With an empty app, the first person the sign-in proxy names is added. `CARDS_PEOPLE` is optional.
- `ingest --every MINUTES`; the compose file runs it every 15 minutes.
- `catalogue --card ID --force` replace `TC_ONLY` and `TC_FORCE`.
- The catalogue run's User-Agent is the tool's name and repository address.

## 0.5.0

Prepared for release; not published.

- People come from `CARDS_PEOPLE`. Feed and bot tokens are `CARDS_FEED_TOKEN_<ID>` and `CARDS_BOT_TOKEN_<ID>`.
- `CARDS_SINGLE_USER` for one person without a login proxy.
- Data keys can be wrapped by a local key (`CARDS_KEY_FILE`, `CARDS_LOCAL_KEY`) as well as Vault Transit. Vault is
  used only when `VAULT_ADDR` is set; `VAULT_TOKEN` works outside Kubernetes.
- The model is used only when `LLM_URL` and `LLM_API_KEY` are set. No default endpoint.
- Telegram settings are `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- `seed` no longer assigns held cards or a way of paying.
- Picture search runs only when `SEARX_URL` is set.
- Removed `CARDS_DEV_KEY` and `DEV_USER`.
- `compose.yaml`, `.env.example`, CI and release workflows.

Earlier versions (0.1 to 0.4.25) were private.
