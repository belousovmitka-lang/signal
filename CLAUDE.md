# Signal — source connectors

This repo holds the data-collection layer for **Signal**, an app that helps builders find problems worth building. Signal shows each builder a feed of real problems, paid demand, tool gaps and industry news matched to their skills and interests. The builder saves posts, Signal turns saves into structured ideas, and then it helps them reach a first customer.

This repo does one job: **collect raw posts from every source below and store them as clean, uniform records.** Ranking, clustering, matching and the app itself live elsewhere and are out of scope here.

The guiding principle from the founder: **build more connectors than users will need, rather than leave anyone with a thin feed.** Every platform in the catalog below gets a connector or a search profile, unless it is marked blocked.

**Industries are built on demand (founder decision, 2026-10-11).** Users may name any industry, so industry maps are not pre-built for every case: when a user names one, `signal_connectors/on_demand.py` matches an existing map or builds a new one live (sources found, access-checked, then collected). The 15 seed maps in section 8 are a starting set, not a target.

---

## 1. Rules (non-negotiable)

1. **Official routes only.** Use a platform's official API, its public RSS feeds, its public logged-out pages, or a site-limited search engine API. Never log in with personal or fake accounts to collect data, never rotate accounts, and never bypass logins, paywalls, CAPTCHAs or rate limits.
2. **Respect terms and robots.txt.** Before a public-pages connector reads a site, check its robots.txt and terms. If either forbids automated access, read that site through the search route only (titles, snippets, links) or skip it. Record the decision in `sources.yaml` (`access_check` field).
3. **Rate limits and politeness.** Obey documented limits. For public pages: one request per second per domain by default, a descriptive User-Agent with a contact address, and exponential backoff on 429 and 5xx responses.
4. **Minimal personal data.** Store the post text, URL, timestamp and engagement counts. Store the author only as a salted hash plus a self-described role if the post states one ("practice manager," "paralegal"). Never scrape profiles, emails or phone numbers.
5. **Link back, don't republish.** The app shows summaries and links to originals. Keep full text internally for processing only, and honor deletions where a platform's terms require it (Reddit and X do): re-check and purge deleted items on the schedule their terms set.
6. **Costs are tracked.** Every paid call is metered (see section 6). No connector may exceed its monthly budget in `sources.yaml`.
7. **Verify before implementing.** Endpoints, limits and prices in this file come from research as of October 2026 and general knowledge. Before building any connector, read the platform's current official developer docs and terms, and update this file if anything changed.

---

## 2. Stack and layout

- **Language:** Python 3.12+
- **HTTP:** `httpx` (async), `tenacity` for retries
- **Parsing:** `feedparser` (RSS/Atom), `selectolax` or `beautifulsoup4` (HTML), `pydantic` v2 (records)
- **Storage:** SQLite now (`data/signal.db`), Postgres later; use SQLAlchemy 2.0 so the switch is a config change
- **Scheduling:** a simple async scheduler locally (`scripts/run.py`); later a cron job or worker on a server
- **Tests:** `pytest`, `pytest-asyncio`, `vcrpy` cassettes for recorded responses
- **Secrets:** `.env` (loaded with `python-dotenv`), never committed; `.env.example` lists every key

```
signal-connectors/
  CLAUDE.md
  .env.example
  sources.yaml              # every source: connector, queries, cadence, budget, access check
  industries/               # one YAML seed map per industry (section 8)
  signal_connectors/
    models.py               # RawPost, SourceQuery, FetchResult, HealthStatus
    base.py                 # Connector protocol, BaseConnector, shared HTTP client, rate limiter
    robots.py               # robots.txt check (RFC 9309, wildcards), 24h cache
    costs.py                # cost meter, budget guard, quota windows
    dedupe.py               # URL + content hashing
    privacy.py              # salted author hashes, self-described role extraction
    store.py                # database writes (posts, runs, cost ledger)
    config.py               # sources.yaml schema (pydantic) and loader
    settings.py             # env settings (.env)
    services.py             # bundle of shared services handed to every connector
    runner.py               # run one source: fetch, store, meter, record run; per-query cursors
    scheduler.py            # what's due by cadence (last run of any status), bounded concurrency, deletion re-checks
    status.py               # collection health per source (scripts/status.py)
    industries.py           # industry seed map schema and loader
    forum_detect.py         # detect forum software + feeds from a logged-out page
    verify.py               # live checks for seed maps (liveness, robots, login walls, moved feeds)
    discovery.py            # search APIs as pointers only: URLs in memory, never stored
    discovery_agent.py      # Phase 6: draft industries/<key>.yaml from seeds, for human review
    on_demand.py            # match or build an industry map when a user names an industry
    live.py                 # live lane: Brave/YouTube results shown at view time, never stored (7.5)
    x_search.py             # on-demand stored X search for the feed (search_x / asearch_x)
    clips.py                # Clip to Signal: per-user saved clips, FastAPI router (7.5)
    ws.py                   # minimal RFC 6455 WebSocket client (Jetstream)
    parsing.py              # HTML to text, lenient dates
    smoke.py                # smoke CLI logic (scripts/smoke.py is the entry point)
    connectors/
      search_api.py         # Brave + Exa, site-limited search profiles
      rss.py
      reddit.py
      discourse.py
      classic_forum.py      # XenForo, vBulletin, phpBB, generic HTML forums
      job_boards.py         # Greenhouse, Lever, Ashby, SmartRecruiters, USAJOBS
      youtube.py
      podcasts.py
      open_data.py          # Socrata, CKAN
      app_reviews.py
      jetstream.py          # Bluesky firehose, filtered to first-person work pain
      ... one module per platform-specific connector in section 7
  scripts/
    run.py                  # run due connectors on schedule: --once (cron), --loop, --dry-run, --only a,b
    smoke.py                # run one connector live: python scripts/smoke.py rss --limit 20
    verify_industries.py    # check every seed-map site and write statuses back
    discover_industry.py    # python scripts/discover_industry.py <key> --name ... --roles ... --keywords ...
    recheck_deletions.py    # purge posts deleted at the source (Bluesky, Mastodon, Lemmy, Reddit)
    status.py               # health table: last run, failure streak, new posts 7d, spend, stored totals
    clip_server.py          # local server (127.0.0.1) for the Clip to Signal button
  clipper/                  # Chrome MV3 extension: the Clip to Signal button
  tests/
    cassettes/
```

---

## 3. The record every connector outputs

Every connector, whatever its source, returns `RawPost` records. Downstream code only ever sees this shape.

```python
class RawPost(BaseModel):
    id: str                    # sha256(source + canonical_url)
    source: str                # "reddit", "hn", "brave:linkedin.com", "rss:law360"
    source_type: Literal["community", "social", "paid_demand", "review",
                         "news", "government", "media", "startup", "research"]
    route: Literal["own_api", "search_api", "rss", "public_pages"]
    url: str                   # canonical link to the original
    title: str | None
    text: str                  # full text if available, else snippet
    is_snippet: bool           # True when only a search snippet was available
    author_hash: str | None    # salted hash, never the raw handle
    author_role: str | None    # self-described role if stated in the post
    community: str | None      # subreddit, forum board, channel, publication
    published_at: datetime
    fetched_at: datetime
    language: str | None
    engagement: dict[str, int] # upvotes, comments, views, likes, as available
    money: dict | None         # budget/rate/price if the post states one (gigs, RFPs, jobs)
    industry_hint: str | None  # from the industry map that produced the query
    query: str | None          # the query that found it
    raw: dict                  # original payload, trimmed, for reprocessing
```

`SourceQuery` carries what to fetch: keywords, a community list, a domain filter, a `since` timestamp and a result limit.

---

## 4. The connector interface

```python
class Connector(Protocol):
    name: str
    route: str
    cadence: str               # "hourly" | "daily" | "weekly" | "monthly"
    async def fetch(self, query: SourceQuery) -> AsyncIterator[RawPost]: ...
    async def healthcheck(self) -> HealthStatus: ...   # auth ok, quota left, last success
```

Rules for every connector:
- Incremental: fetch only items newer than the last successful run (`since`).
- Idempotent: re-running never creates duplicates (dedupe on `id`, then on normalized text hash).
- Fails loudly in logs, quietly for the user: one broken source never stops the others.
- Reports cost per run to `costs.py`.

**Definition of done for a connector:**
1. Reads its section of `sources.yaml`.
2. Has a recorded-cassette test that checks parsing into `RawPost`.
3. `scripts/smoke.py <name>` runs live and stores at least 20 valid records (or all available, if fewer).
4. Respects rate limits and budget; healthcheck passes.
5. Entry in section 7 marked done with the date.

---

## 5. Shared services

- **HTTP client:** one shared `httpx.AsyncClient` with per-domain rate limiters, retries with jitter, timeouts, and a User-Agent like `SignalBot/0.1 (+contact@yourdomain)`.
- **robots.py:** caches robots.txt per domain for 24 hours; public-pages connectors must call it before every new path pattern.
- **dedupe.py:** canonicalize URLs (strip tracking params), hash them; also hash normalized text to catch cross-posts.
- **Scheduler cadences:** hourly for social and gigs, daily for communities, reviews and news, weekly for jobs and reports, monthly for research data.
  Built (2026-10-10): `scripts/run.py --once` from cron (e.g. `*/15 * * * *` so hourly sources keep their cadence) or `--loop`. A source is due one interval after its last run of any status (5-minute slack), so failures wait for the next slot. Sources with `deletion_recheck_days` get a recorded `<name>:recheck` run on that schedule.

---

## 6. Costs and keys

`.env.example` must list:

```
BRAVE_API_KEY=
EXA_API_KEY=
X_BEARER_TOKEN=
YOUTUBE_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
THREADS_ACCESS_TOKEN=
META_GRAPH_TOKEN=            # Instagram hashtag search, Facebook Pages
PINTEREST_TOKEN=
UPWORK_CLIENT_ID=
UPWORK_CLIENT_SECRET=
FREELANCER_TOKEN=
USAJOBS_API_KEY=
USAJOBS_EMAIL=
SAM_GOV_API_KEY=
REGULATIONS_GOV_API_KEY=
COURTLISTENER_TOKEN=
LEGISCAN_API_KEY=
OPENSTATES_API_KEY=
PODCASTINDEX_KEY=
PODCASTINDEX_SECRET=
PRODUCTHUNT_TOKEN=
GITHUB_TOKEN=
KAGGLE_USERNAME=
KAGGLE_KEY=
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
GOOGLE_ADS_DEVELOPER_TOKEN=  # Keyword Planner
SEMANTIC_SCHOLAR_KEY=
DOL_API_KEY=                 # OSHA enforcement data (dataportal.dol.gov)
CENSUS_API_KEY=              # api.census.gov now requires a key
STACKEXCHANGE_KEY=           # optional; raises quota 300 -> 10,000/day
BLUESKY_HANDLE=              # unused: Jetstream and the public AppView need no login
BLUESKY_APP_PASSWORD=
# Paid, optional (feature-flagged off by default):
YELP_API_KEY=
GOOGLE_PLACES_KEY=
NEWSAPI_KEY=
CRUNCHBASE_KEY=
SEMRUSH_KEY=
PACER_USERNAME=
```

Known prices (October 2026; confirm before budgeting):
- Brave Search API $5 per 1,000 searches, $5 free credit monthly (card required at signup, not charged within the credit), 50 queries/s. Exa $7 per 1,000 Fast/Auto searches ($4 Instant), up to 10 results; $10 free credit monthly, no card needed (verified 2026-10-09).
- X API pay-per-use ~$0.005 per post read, up to 2M reads/month.
- YouTube Data API free: 100 `search.list` calls/day in their own bucket, plus 10,000 units/day for other endpoints (list calls ~1 unit) (verified 2026-10-09; this replaced the old 100-units-per-search model).
- Reddit: free tier is non-commercial only; commercial access by application, reported ~$12,000+/month.
- Threads keyword search: free, 2,200 queries per rolling 24 hours per user. Before `threads_keyword_search` app review it searches only the token owner's own posts (verified 2026-10-09).

`costs.py` must keep a monthly ledger per connector and refuse calls once `monthly_budget_usd` in `sources.yaml` is reached.

---

## 7. Connector catalog — every platform

Status values: **todo**, **done (date)**, **awaiting approval**, **paid (flag off)**, **blocked**.

### 7.1 Generic connectors (build first; they cover most platforms)

| # | Connector | Covers | Route | Notes |
|---|---|---|---|---|
| G1 | `search_api` — **done (2026-10-09), pointer design** | Platforms whose posts can be read through an allowed route once found | search_api → platform route | Search results are pointers only: Brave API Terms 3(b)(i) forbid storing results beyond transient storage, Exa ToS 4.2(a) forbid copying output. `discovery.py` returns URLs held in memory; each URL goes to a resolver that reads the post from its own platform (Bluesky AT Protocol, Medium and Substack RSS); URLs no resolver accepts are dropped unstored. Exa is asked for no contents ($0.007/search). Shared monthly caps per provider: `defaults.provider_budgets`. Adding a platform = adding a resolver, after its robots.txt **and** terms allow automated reading. |
| G2 | `rss` — **done (2026-10-09)** | Trade press, newsletters (Substack, Beehiiv), Medium tags, blogs, press release wires, podcast feeds, VC blogs, bar/association news, Stack Exchange feeds | rss | Google News keyword feeds are **blocked** (robots.txt + non-commercial terms, checked 2026-10-09): keyword news goes through G1. Medium: `https://medium.com/feed/tag/<tag>`. Substack: `<pub>/feed`. |
| G3 | `reddit` | All subreddits in the industry maps and section 7.2 | search_api now, own_api after approval | Until commercial approval, run through G1 with `site:reddit.com/r/<sub>`. After approval: OAuth API, `/r/<sub>/new`, `/r/<sub>/search`, comments. Honor deletions per Reddit terms. |
| G4 | `discourse` — **todo: no forum with permissive terms yet (2026-10-09)** | Any Discourse forum (many community and vendor forums) | public_pages | robots.txt on Discourse sites disallows `/search.json` by default, so no keyword search. The default Discourse ToS forbids copying content without written permission; Shopify, HubSpot, Spiceworks and HashiCorp were read and all forbid it. Build when a forum's terms or its admin allow it. |
| G5 | `classic_forum` — **done (2026-10-09)** | XenForo, vBulletin, phpBB and generic HTML forums | rss / public_pages | Reads board RSS only for industry-map forums with `terms: allowed` (read by hand). Live: TruckersReport, ServeTheHome, A to Z Teacher Stuff (PLCtalk answers our bot with 403 since 2026-10-21: blocked). Forbidden (clause recorded in the industry maps): VerticalScope forums (Contractor Talk, HVAC-Talk, Electrician Talk, PlumbingZone), InsuranceForums, Practical Machinist (AI datasets), Student Doctor Network (CC BY-NC-SA), AccountingWEB. |
| G6 | `job_boards` — **done (2026-10-11)** | Company job posts across all industries | own_api | Greenhouse `boards-api.greenhouse.io/v1/boards/<token>/jobs`, Lever `api.lever.co/v0/postings/<company>`, Ashby `api.ashbyhq.com/posting-api/job-board/<name>`, no key. Boards come from each industry map's `job_boards` (`scripts/find_job_boards.py` finds boards of an industry's incumbent tools). Blocked by robots.txt (2026-10-11): SmartRecruiters (allows only LinkedInBot), USAJOBS (`Disallow: /`). Recruiter emails and phones redacted; stated pay kept in `money`. |
| G7 | `youtube` — **blocked (2026-10-09)** | YouTube videos, comments, conference talks, webinars posted to YouTube | own_api → G1 | YouTube API Developer Policies III.E forbid aggregating API Data across channels not owned by one content owner, using API Data "to create new or derived data or metrics", and storing non-statistics data over 30 days. Signal's cross-channel feed, ranking and idea generation do all three. Until then: G1 search profile `youtube.com/watch` (titles + snippets). Unblocks with: YouTube API compliance audit and the derived-metrics permission (policy section L, via the quota extension form, since June 2026). |
| G8 | `podcasts` — **built (2026-10-11), needs key** | Industry podcasts | rss + own_api | Podcast Index API discovers shows (`scripts/find_podcasts.py`, needs PODCASTINDEX_KEY/SECRET, not yet received); episodes come from each show's own RSS feed, no key. Podcasting 2.0 `<podcast:transcript>` links are appended to show notes; no speech-to-text, audio never downloaded. |
| G9 | `open_data` — **done (2026-10-09)** | City/state open data portals, data.gov, FCC data | own_api | Socrata SODA API per dataset with an explicit field map (only named fields stored, so no applicant names or addresses): NYC restaurant inspections, Chicago building permits, NYC 311. CKAN new-dataset listings (open.canada.ca; catalog.data.gov's CKAN API returns 404 as of 2026-10-09). |
| G10 | `app_reviews` — **done (2026-10-09): WordPress.org** | App Store, app marketplaces | rss | WordPress.org plugin review + support feeds (`/support/plugin/<slug>/reviews/feed/`); rating and reply counts parsed. Blocked 2026-10-09: Apple reviews feed (itunes.apple.com robots.txt `Disallow: /*/rss/*`), Chrome Web Store and Atlassian Marketplace REST API (robots.txt), Shopify App Store (ToS forbids robots). api.wordpress.org plugin info API is robots-disallowed. Google Play allows robots-compliant access but serves reviews via internal APIs: not used. |

### 7.2 Platform-specific connectors (own API or public data)

**Communities and forums**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Reddit | G3 `reddit` | own_api only | reddit.com robots.txt disallows our bot, and search results can't be stored, so the search route is closed (2026-10-09). Commercial Data API request refused by Reddit (2026-10-11). Shown at view time through the live lane (7.5); an official Reddit data partner (e.g. Brandwatch) once there's revenue | hourly | blocked (refused 2026-10-11) |
| Hacker News | `hackernews` | own_api | Algolia HN Search API `hn.algolia.com/api/v1/search_by_date` (keyword phrases, tags) + Firebase API `hacker-news.firebaseio.com/v0` (ask/show/job lists; MIT, no rate limit) | hourly | done (2026-10-09) |
| Stack Exchange network (incl. Stack Overflow, Law, Workplace, Money, Academia, DIY) | `stackexchange` | own_api | `api.stackexchange.com/2.3/search/advanced?site=<site>`; 300 req/day per IP without a key, 10,000 with a free stackapps.com key; obey the `backoff` field. Content is CC BY-SA 4.0: the app must credit Stack Exchange and link to each question | daily | done (2026-10-09) |
| Telegram public channels | `telegram` | — | Telegram ToS (checked 2026-10-09): "Telegram additionally prohibits data scraping as part of its Content Licensing and AI Scraping Terms, which apply to all users, businesses, and third-party services accessing the platform". Covers t.me/s previews too | daily | blocked (2026-10-09) |
| Lemmy instances | `lemmy` | own_api | `/api/v3/search` (Posts, New) and `/api/v3/post/list?community_name=` on lemmy.world, no login. lemmy.world robots.txt sets Crawl-delay 60 (applied); its ToS bot rules cover posting/voting bots only. Skip bot accounts and deleted/removed posts; re-check deletions on the origin instance | daily | done (2026-10-09) |
| Mastodon instances | `mastodon` | own_api | `/api/v1/timelines/tag/<tag>` on mastodon.social, no login (`/api/v2/search` returns no statuses without login). Its terms bind registered users only. Consent: keep only authors with `indexable: true`, not `noindex`, not bots, no `#nobot`/`#noindex` in bio (~30% of posts). Deletions re-checked on each post's home server | daily | done (2026-10-09) |
| Niche industry forums (Contractor Talk, Spiceworks, BiggerPockets, AllNurses, Fishbowl, TruckersReport, HVAC-Talk, Mike Holt, Practical Machinist, Eng-Tips, New Ag Talk, DentalTown, Student Doctor Network, ActiveRain and every forum in section 8) | G4 / G5, else G1 | public_pages / rss | detect forum software per site | daily | partly done (2026-10-09): most large forums forbid automated or commercial use; ask admins for permission (many small forums will say yes) |
| Vendor community forums (Shopify Community, Salesforce, QuickBooks Community, HubSpot, Amazon Seller Forums, eBay Community, SHRM community) | G4 / G5, else G1 | public_pages / rss | detect per site | daily | todo |
| Mumsnet and parenting forums | G5, else G1 | public_pages | Mumsnet has objected to AI scraping: check terms carefully; default to G1 | daily | todo |

**Social platforms**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| X (Twitter) | `x` (`signal_connectors/x_search.py`) | own_api | API v2 recent search `/2/tweets/search/recent`, called on demand by the feed (evidence for inferred problems), never on a schedule; $0.005 per post returned, $20/month budget. No user lookups; author_id hashed; shown with X's embed. Deletions: Batch Compliance job over every stored post daily (Developer Policy: within 24 h); jobs outlast a run, so the run records `partial` and the scheduler applies the job on 15-minute retries (2026-10-21). Enabled by the owner; X Enterprise needed beyond early stage | on demand | done (2026-10-16) |
| Threads | `threads` | own_api + search_api | `GET graph.threads.net/v1.0/keyword_search` (needs `threads_basic` + `threads_keyword_search` app review; 2,200 queries/24h); overflow through G1 `site:threads.net` | daily | awaiting approval |
| Bluesky | `jetstream` | own_api | Jetstream firehose `wss://jetstream.us-east.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents` (no key, not metered, 36 h lookback): each run replays every new post since the last run (~4 GB/day) and keeps English posts with a first-person workflow pain that name an industry (role or vocabulary term); everything else dropped unstored. Yield ~30-50 posts/day, mostly noise; the feed's tagging filters. Delete events purge stored posts at once; older posts re-checked with `getPosts`. Skip authors labelled `!no-unauthenticated`; guidelines require a monitored contact email. Replaced the Exa route (2026-10-17) | daily | done (2026-10-17) |
| Instagram | `instagram` | own_api + search_api | Graph API via a business account: `ig_hashtag_search` → `/<hashtag-id>/recent_media` (limited unique hashtags per 7 days); plus G1 profile | daily | todo |
| Facebook Pages | `facebook_pages` | own_api + search_api | Graph API with Page Public Content Access (needs Meta app review); plus G1 profile | daily | awaiting approval |
| YouTube | G7 → G1 | search_api | own API blocked by YouTube developer policies (see G7); G1 `youtube.com/watch` | daily | blocked (2026-10-09) |
| Pinterest | `pinterest` | own_api | API v5; confirm which search endpoints your app can use | weekly | todo |
| Substack Notes and posts | G2 | rss | per-publication feeds | daily | done (2026-10-09); add publications per industry map |
| Medium | G2 | rss | tag feeds | daily | done (2026-10-09); add tags per industry map |

**Paid demand**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Upwork | `upwork` | own_api (after approval), G1 until then | GraphQL `marketplaceJobPostingsSearch` | hourly | awaiting approval; apply now |
| Freelancer.com | `freelancer` | own_api | Projects API (active projects search) | hourly | todo |
| Contra | G5 / G1 | public_pages | public project listings | daily | todo |
| PeoplePerHour, Guru, Malt | G2 / G5, else G1 | public_pages / rss | public job listings and feeds where offered | daily | todo |
| Wellfound, YC Work at a Startup | G6 + public pages, else G1 | public_pages | many startups post through Greenhouse/Lever/Ashby | weekly | todo |
| USAJOBS | G6 | — | data.usajobs.gov robots.txt `Disallow: /` (2026-10-11) | weekly | blocked (2026-10-11) |
| State government job boards | G2 / G5, else G1 | rss / public_pages | per state | weekly | todo |
| Industry job boards (legal, healthcare, construction, logistics) | G2 / G5, else G1 | rss / public_pages | per board | weekly | todo |
| Company career pages (all industries) | G6 | own_api | Greenhouse, Lever, Ashby (SmartRecruiters blocked by robots.txt) | weekly | done (2026-10-11) |
| GitHub bounties, Algora, Gitcoin | `github` | own_api | GitHub search issues `label:bounty` and Algora's `label:"💎 Bounty"` (paid_demand); Algora/Gitcoin public pages not checked | daily | done (2026-10-09, GitHub part) |
| IssueHunt and other bounty boards | G5, else G1 | public_pages | public listings | daily | todo |
| Kaggle | `kaggle` | own_api | Kaggle API competitions list | weekly | todo |
| HeroX, Wazoku (InnoCentive) | G5, else G1 | public_pages | public challenge listings | weekly | todo |
| Devpost and sponsored hackathons | G5, else G1 | public_pages | public hackathon listings | weekly | todo |
| Amazon Mechanical Turk | G5, else G1 | public_pages | only if task listings are visible logged-out; otherwise blocked | weekly | todo |

**Reviews, app stores, feature requests**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Apple App Store | G10 | — | itunes.apple.com robots.txt disallows `/*/rss/*` (reviews feed); App Store Connect API covers own apps only | daily | blocked (2026-10-09) |
| Chrome Web Store | G10 | — | robots.txt disallows review pages | weekly | blocked (2026-10-09) |
| Shopify App Store, Salesforce AppExchange, Atlassian Marketplace, WordPress plugins | G10 | rss | WordPress plugins: done (2026-10-09). Shopify: ToS forbids robots. Atlassian: REST API disallowed by robots.txt. AppExchange: reviews load via internal APIs, no clean route | weekly | partly done |
| Public feature boards (Canny, UserVoice, Productboard portals) | `feature_boards` | public_pages | robots.txt and terms allow reading; but Canny boards ship data only as an embedded JS object (not JSON, not a published interface) and UserVoice needs JavaScript. Revisit if a vendor offers a public feed or API | weekly | todo (deferred 2026-10-09) |
| GitHub issues and discussions | `github` | own_api | REST search for feature-request issues; GraphQL `search(type: DISCUSSION)` for "feature request" and "is there a way" discussions (category qualifiers are ignored there; keywords + created:> work). Usernames hashed | daily | done (2026-10-10) |
| Better Business Bureau | G5, else G1 | public_pages | check terms; default G1 | weekly | todo |

**News, trade press, funding**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Google News (keyword) | G2 | rss | Google News RSS search | hourly | blocked (2026-10-09): robots.txt disallows `/rss`, feeds are personal/non-commercial only; use G1 news search + `gdelt` |
| GDELT | `gdelt` | own_api | GDELT DOC 2.0 API; free for commercial use with citation. Built, but **disabled**: returned 429 even at 1 request/20s (2026-10-09), and OR'd keyword queries were noisy. Revisit with one combined daily query or GDELT's bulk datasets | daily | todo (built, disabled) |
| Wire services, business press, tech press, trade publications, press release wires, newsletters, company and industry blogs, local news, VC and accelerator blogs | G2 | rss | headlines + summaries; full text only where the feed provides it | hourly to daily | done (2026-10-09): 36 trade-press feeds live; more per industry map |
| SEC EDGAR (Form D funding filings) | `edgar` | own_api | `/cgi-bin/` is robots-disallowed, so: daily form index `/Archives/edgar/daily-index/<y>/QTR<n>/form.<date>.idx` → each Form D's `primary_doc.xml`. User-Agent with contact email; 5 req/s (SEC max 10). Executives' names and addresses not stored; pooled funds skipped | daily | done (2026-10-09) |
| Layoff and shutdown trackers | G5, else G1 | public_pages | check terms per tracker | weekly | todo |

**Government, regulatory, legal**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| SAM.gov | `sam_gov` | own_api | Opportunities API v2, free key; daily limits by role aren't published, so capped at 10 requests/day (1 request = 1,000 notices). Links to the public `sam.gov/opp/<id>/view` page (uiLink points into the logged-in workspace). pointOfContact not stored | daily | done (2026-10-09) |
| State/city procurement portals, BidNet, DemandStar | G5 / G2, else G1 | public_pages | per portal; many publish RSS | daily | todo |
| EU TED | `ted` | own_api | TED API v3 `POST /v3/notices/search`, anonymous; CPV filter, `SORT BY publication-date DESC`; reuse under Decision 2011/833/EU | daily | done (2026-10-09) |
| UK Contracts Finder / Find a Tender | `uk_tenders` | own_api | OCDS APIs (Contracts Finder search, Find a Tender release packages), no key, cursor paging; Open Government Licence v3.0 | daily | done (2026-10-09) |
| School district and university RFPs | G5 / G2, else G1 | public_pages | per site | weekly | todo |
| CFPB complaint database | `cfpb` | own_api | Consumer Complaint Database API, no key, CC0. Narratives are no longer in the API (2026-10-09): records are structured (product, issue, company, response). Paging needs `_meta.break_points` (`frm` alone is ignored). ~4-5k/day, capped per run; ZIP not stored | daily | done (2026-10-09) |
| FDA device reports (MAUDE) | `openfda` | own_api | openFDA `/device/event.json`, no key, CC0. MAUDE lags 5-6 weeks and `date_added` can't be sorted, so runs look back 60 days (`filter_since = False`); contact names/phones not stored | weekly | done (2026-10-09) |
| NHTSA complaints | `nhtsa` | own_api / downloads | The complaints API only looks up by make + model + year or ODI number, so runs read the daily-rebuilt bulk file `static.nhtsa.gov/odi/ffdd/cmpl/COMPLAINTS_RECEIVED_<5-year chunk>.zip` (~43 MB, ~10,000 complaints/month), one post per ODI number with every component; owner narratives kept (contacts redacted), VIN, city, dealer and operator name not stored; linked to `api.nhtsa.gov/complaints/odinumber`. Hinted `auto_repair` | weekly | done (2026-10-28) |
| FCC consumer complaints | G9 | own_api | opendata.fcc.gov Socrata `3xyp-aqkj` (public domain). Rows have no narrative and carry caller numbers and ZIPs, so only weekly counts per issue type/issue are stored (`kind: socrata_counts`), with the change from the week before | weekly | done (2026-10-28) |
| FTC published complaint datasets | G9 / downloads | public_pages | published datasets only | monthly | todo |
| OSHA enforcement data | `osha` | own_api | DOL data API v4 (`DOL_API_KEY`): violations of the last 30 days joined to inspections in bulk (~6 calls a run), summed per industry map by NAICS prefix: inspections, violation types, penalties, most-cited standards. Aggregates only, no establishment names | monthly | done (2026-10-15) |
| Federal Register | `federal_register` | own_api | `federalregister.gov/api/v1/documents.json`: every new rule and proposed rule, plus keyword notices; comment deadlines and agencies kept; no key, public domain | daily | done (2026-10-09) |
| Regulations.gov | `regulations_gov` | own_api | API v4 public comments per agency (SBA, FMCSA, CMS, OSHA, FDA, IRS, FTC, HHS), detail call per comment; 1,000 req/hour. Titles rebuilt (they name the commenter); no submitter names, addresses, emails, phones; organization and category kept | daily | done (2026-10-09) |
| EU and national official journals | `eurlex` | own_api | eur-lex.europa.eu answers bots with a 202 challenge (never bypassed), so the Publications Office CELLAR SPARQL endpoint is used: new regulations, directives, decisions with CELEX id and English title; sanctions acts excluded. Reuse under Decision 2011/833/EU | daily | done (2026-10-10) |
| State legislature trackers | `legiscan` / `openstates` | own_api | Open States v3 `/bills` (public domain data; limits unpublished, paced 9/min, 400/day): done (2026-10-09). LegiScan Pull API (CC BY 4.0, credit + link required): getSearchRaw per industry's keywords, new bills read with getBill; free tier 10,000 queries/month, capped 300/day; sponsors not stored: done (2026-10-15) | daily | done (2026-10-15) |
| CourtListener (incl. RECAP copies of PACER filings) | `courtlistener` | own_api | REST API v4 search (opinions), token. Free tier 5/min, 50/hour, 125/day: paced 13s, capped 100/day. Attorney/judge fields not stored. Commercial agreements exist: confirm with Free Law Project | daily | done (2026-10-09) |
| PACER (direct) | `pacer` | own_api (paid) | $0.10/page, $3 cap/document; only for specific documents | on demand | paid (flag off) |
| Bar associations, medical boards, licensing bodies | G2 / G5, else G1 | rss / public_pages | per body | weekly | todo |
| City council and school board agendas and minutes | G5 / G9, else G1 | public_pages | many cities use the same agenda platforms; detect per site | weekly | todo |
| Open data portals (data.gov and city portals) | G9 | own_api | Socrata / CKAN; add datasets per industry in sources.yaml | weekly | done (2026-10-09) |
| Patents (USPTO, Google Patents) | `patents` | own_api | USPTO open data / PatentsView APIs | monthly | todo |

**Podcasts, video, events**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Podcasts (Apple, Spotify, Podcast Index) | G8 | rss + own_api | Podcast Index API + RSS (see G8) | weekly | built (2026-10-11), waiting on the Podcast Index key |
| YouTube, recorded conference talks, webinars on YouTube | G7 → G1 | search_api | own API blocked (see G7); G1 `youtube.com/watch` | daily | blocked (2026-10-09) |
| Twitch | `twitch` | own_api | Keys work (app token + Helix verified 2026-10-10), but the Developer Services Agreement section VI forbids storing copies of Twitch content beyond a 24-hour cache without written authorization, and selling or commercially using Twitch data "or insights derived from that data" without permission. Needs Twitch's written permission | weekly | blocked (2026-10-10) |
| Webinars hosted elsewhere | G5, else G1 | public_pages | public pages and descriptions | weekly | todo |
| Event agendas (Eventbrite, Meetup, Luma, conference sites) | `events` + G1 | public_pages / own_api | Eventbrite has no public event search API: use G1; Meetup GraphQL API (check access); Luma and conference sites public pages | weekly | todo |

**Startup and builder ecosystem**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Product Hunt | `producthunt` | own_api | GraphQL API v2 `posts(order: NEWEST, postedAfter:)` + optional comments; developer token works (2026-10-10). Docs: the API "must not be used for commercial purposes" without permission (requested from hello@producthunt.com); 6,250 complexity points/15 min; attribution requested. Built, disabled until permission | daily | awaiting approval |
| Indie Hackers | — | — | answers our bot with 403 (Cloudflare) on its terms page and content (2026-10-09) | daily | blocked |
| YC Requests for Startups | — | — | YC terms: "you will not engage in or use any data mining, robots, scraping or similar data gathering or extraction methods" (2026-10-09) | monthly | blocked |
| BetaList, AlternativeTo | — | — | BetaList terms: personal, non-commercial viewing only, no commercial use (covers its RSS); AlternativeTo answers our bot with 403 (2026-10-09) | weekly | blocked |
| Acquire.com, Flippa listings | — | — | Acquire terms forbid any robot or automatic means; Flippa terms forbid copying or extracting listings (2026-10-09) | weekly | blocked |
| GitHub trending | `github` | own_api | Search API: repos created since the cursor, sorted by stars (the trending page is not scraped) | daily | done (2026-10-09) |
| Hugging Face | `huggingface` | own_api | Hub API `/api/spaces` and `/api/models` sorted by trendingScore, no key; snapshot (`filter_since = False`), id-only dedupe | weekly | done (2026-10-09) |
| Kickstarter, Indiegogo | — | — | both answer our bot with 403 (2026-10-09) | weekly | blocked |

**Research and search data**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Google Trends | `trends` | public_pages | apply for the official Trends API (limited testing); unofficial libraries break often, use only as a stopgap | weekly | todo |
| Google Keyword Planner | `keyword_planner` | own_api | Google Ads API KeywordPlanIdeaService | monthly | todo |
| Google autocomplete / "People also ask" | G1 | search_api | via search API results | weekly | todo |
| Industry association surveys, vendor benchmark reports, analyst firms' free reports | G2 / G5 | rss / public_pages | per publisher | monthly | todo |
| Government statistics (BLS, Census, Eurostat) | `stats` (source `census_bfs`) | own_api | Census Business Formation Statistics (`CENSUS_API_KEY`): new business applications per month by NAICS sector, with month-over-month and year-over-year change; app must show the Census API notice (raw.notice): done (2026-10-15). api.bls.gov robots.txt `Disallow: /` (blocked, 2026-10-09); Eurostat returns statistics, not posts, no RSS (2026-10-10): not built | monthly | done (2026-10-15, Census) |
| Academic papers | `papers` | rss (arXiv) / own_api | arXiv via RSS category feeds (rss.arxiv.org, 1 request/3s; metadata CC0): done (2026-10-09). export.arxiv.org (query API) is robots-disallowed for all agents: not used. PubMed E-utilities: eutils.ncbi.nlm.nih.gov robots.txt disallows our path (2026-10-09): blocked. Semantic Scholar: key optional, todo | daily | partly done |
| Wikipedia | `wikipedia` | own_api | Wikimedia pageviews top articles (wikimedia.org, UA with contact email). Built but **disabled**: daily trending is mostly people in the news, low signal for builders | weekly | done (2026-10-09, disabled) |

**Paid connectors (build, keep behind a feature flag, off by default)**

| Platform | Connector | Cost | Why build it anyway |
|---|---|---|---|
| Yelp Fusion | `yelp` — **blocked (2026-10-09)** | from ~$229/month; 3 short excerpts per business | API Terms 5(a): no storing content over 24 hours; 5(d): no commercial use without consent; 9.4: no ingesting into generative AI. Needs a Yelp Data Licensing agreement |
| Google Places reviews | `google_places` — **blocked (2026-10-09)** | pay per call; 5 reviews per place | Maps Platform terms: no caching except listed fields (Places: lat/lng 30 days, place_id), and "Customer will not create content based on Google Maps Content". Reviews can't be stored |
| NewsAPI.org or similar | `newsapi` — **built, flag off (2026-10-09)** | Developer free (development only); Business $449/mo, Advanced $1,749/mo; $0.0018/extra request | broader news search; replaces Google News / GDELT if bought |
| Crunchbase | `crunchbase` — **built, flag off (2026-10-09)** | enterprise plan, ~$20,000/year | structured funding data: v4 `searches/funding_rounds`, announced_on ≥ cursor, after_id paging, category filter. Storage rights per license |
| Semrush / Ahrefs | `keywords_paid` — **built, flag off (2026-10-09)** | ~$500+/month | search volume at scale: Semrush `phrase_questions` / `phrase_related`, 40 units per line, metered in units and dollars |
| PACER direct | `pacer` — **deferred (2026-10-09)** | per page ($0.10, $3 cap/document) | specific court documents, on demand. PACER Case Locator API needs an authenticated PACER session; use `courtlistener` (RECAP) first and design this around a concrete need |
| Social listening tool (Brandwatch, Talkwalker) | `listening` — **deferred (2026-10-09)** | ~$800–5,000/month | licensed coverage of LinkedIn, Instagram, TikTok, Facebook at scale; APIs are vendor- and contract-specific: build once a vendor is chosen |
| Reddit commercial API | G3 `reddit` — **built, flag off (2026-10-09)** | ~$12,000+/month reported | full Reddit coverage after approval. OAuth app-only, /r/<sub>/new and /search, deletion re-check via /api/info. Needs the commercial agreement first (Data API Terms) |

### 7.3 Search profiles (G1) — platforms reached only through search

**Status (2026-10-09):** search results can't be stored (Brave 3(b)(i), Exa 4.2(a)), so a profile only works if the found page can then be read through an allowed route. robots.txt disallows our bot on Reddit, Quora, LinkedIn, Facebook, Instagram, Threads, Indeed jobs, Trustpilot and Yelp. YouTube, Craigslist, Amazon, Glassdoor, Upwork, TikTok, G2, Capterra and Gartner allow it in robots.txt, but their terms are believed to forbid automated collection: check each one's terms before writing a resolver. These rows are **blocked** until then; unblock through official APIs, partnerships or the paid `listening` connector.

Each row is a search profile in `sources.yaml`: domain/path filters + query templates such as `"<role>" ("is there a tool" OR "so tired of" OR "manually" OR "takes forever")`. All return snippets (`is_snippet=True`) with links.

| Platform | Domain / path filter |
|---|---|
| Reddit (until approval) | `reddit.com/r/<sub>` |
| Quora | `quora.com` |
| Facebook public groups | `facebook.com/groups` (public groups only) |
| Facebook Pages (overflow) | `facebook.com` |
| LinkedIn posts | `linkedin.com/posts`, `linkedin.com/pulse` |
| LinkedIn Jobs | `linkedin.com/jobs/view` |
| Indeed jobs | `indeed.com/viewjob` |
| Glassdoor jobs and company reviews | `glassdoor.com` |
| Indeed company reviews | `indeed.com/cmp` |
| Instagram (overflow) | `instagram.com/p`, `instagram.com/reel` |
| TikTok | `tiktok.com/@` (titles and captions) |
| Threads (overflow) | `threads.net` |
| YouTube (own API blocked, see G7) | `youtube.com/watch` (titles and descriptions) |
| Fiverr | `fiverr.com` |
| Upwork (until approval) | `upwork.com/freelance-jobs` |
| Craigslist gigs and services | `craigslist.org` gig and service sections |
| G2 | `g2.com/products` |
| Capterra, GetApp, Software Advice | `capterra.com`, `getapp.com`, `softwareadvice.com` |
| TrustRadius | `trustradius.com` |
| Gartner Peer Insights | `gartner.com/reviews` |
| Google Play | `play.google.com/store/apps` |
| Amazon reviews | `amazon.com` product review pages |
| Trustpilot | `trustpilot.com/review` |
| Yelp (free route) | `yelp.com/biz` |
| Regional platforms (Weibo, VK, Naver Cafe and others) | per platform; low priority |
| Any niche site with no matching connector | added automatically by discovery |

### 7.4 Blocked (no connector possible now)

Track these so they are not forgotten; revisit when partnerships exist.

| Platform | Why blocked | Unblocks with |
|---|---|---|
| Discord servers | private | admin partnerships (bot added with consent) |
| Slack communities | private | admin partnerships |
| Circle, Skool, private Discourse communities | private | admin partnerships |
| LinkedIn Groups | behind login | partnership |
| Nextdoor | behind login | partnership |
| Toptal, Catalant | projects not public | partnership |
| TaskRabbit, Thumbtack, Angi | requests are private | partnership |
| Sermo, Doximity | private clinician networks | partnership |
| X Spaces, Clubhouse-style audio | no access to audio | none currently |

### 7.5 Live lane and Clip to Signal (platforms we can't store)

For the most useful closed platforms (Reddit, X, Facebook groups, YouTube; LinkedIn, Quora on request) two routes stay within their terms:

- **Live lane** — `signal_connectors/live.py`, done (2026-10-15), **paused by the founder (2026-10-17)**. At view time, one site-limited Brave search per platform plus a YouTube Data API search; results go straight to the app with links and are never written to the database (Brave 3(b)(i) transient storage only, 3(b)(ii) no derivative works, 3(b)(xiii) no AI training; YouTube III.E.4.h no derived data). Summarizing results into stored "value" is a derivative work and not allowed. TikTok dropped. Before launch: the app's terms must bind users per Brave 4(c)(iii), and a paid Brave plan (storage rights are sold separately: api-sales@brave.com).
- **Clip to Signal** — `signal_connectors/clips.py` + `clipper/` (Chrome extension), done (2026-10-17). A user selects text on any post and clicks the button; only the link, page title, the selection and the user's note are sent (contacts redacted), and only on click. Stored per user in the `clips` table, apart from `posts`; the feed (`signal_feed/feed/clips.py`) reads it read-only and detects edits by `updated_at`, so keep `ClipRow`'s columns stable. Routes: `POST /clips`, `GET /clips?user_id=`, `DELETE /clips/{id}`; local server `scripts/clip_server.py` (127.0.0.1 only). No auth yet: needed before launch.

---

## 8. Industry seed maps

Create one file per industry in `industries/<industry>.yaml` with: subreddits, forums (with detected connector), RSS feeds, associations, job boards, YouTube channels, podcasts, and query vocabulary (roles, tasks, pain phrases, jargon). Start with these seeds; verify each site is live and readable before enabling it.

| Industry | Communities and forums | Trade press and newsletters | Associations and other |
|---|---|---|---|
| Legal | r/Lawyertalk, r/paralegal, r/LawFirm, Lawyerist community | Above the Law, Law.com, Artificial Lawyer, LawSites | ABA, state bars, NALA; Clio Legal Trends Report; Legal Talk Network podcasts |
| Healthcare | r/medicine, r/nursing, r/healthIT, AllNurses, Student Doctor Network, DentalTown | Fierce Healthcare, Healthcare IT News, STAT, Becker's | HIMSS, MGMA |
| Construction and trades | Contractor Talk, r/Construction, r/electricians, r/HVAC, r/Plumbing, HVAC-Talk, Mike Holt forums | Construction Dive, ENR, JLC | AGC, NAHB; city permit data |
| Real estate | BiggerPockets, ActiveRain, r/realtors, r/CommercialRealEstate | Inman, The Real Deal, HousingWire | NAR, local realtor associations |
| Logistics and trucking | TruckersReport, r/Truckers, r/FreightBrokers, r/logistics | FreightWaves, Supply Chain Dive, DC Velocity | FMCSA data, trucking associations |
| E-commerce and retail | Shopify Community, Amazon Seller Forums, eBay Community, r/ecommerce, r/FulfillmentByAmazon | Modern Retail, Retail Dive, Marketplace Pulse | Shopify App Store reviews |
| Restaurants and hospitality | r/KitchenConfidential, r/restaurateur, r/TalesFromYourServer | Restaurant Dive, Nation's Restaurant News, Skift | National Restaurant Association, state associations |
| Accounting and finance | r/Accounting, r/taxpros, QuickBooks Community | Going Concern, Accounting Today, CPA Practice Advisor | AICPA, state CPA societies; IRS and SEC rule changes |
| Education | r/Teachers, r/Professors, r/edtech | EdSurge, Education Week, Inside Higher Ed | ISTE, EDUCAUSE; school district RFPs |
| Marketing and agencies | r/marketing, r/PPC, r/SEO, r/agency | Search Engine Land, Search Engine Journal, Marketing Brew, Adweek | MarketingProfs; Google Ads and Meta platform updates |
| HR and recruiting | r/humanresources, r/recruiting, SHRM community | HR Dive, ERE, HR Brew | SHRM; Department of Labor rule changes |
| Insurance | r/Insurance, r/InsuranceAgent | Insurance Journal, Carrier Management, Digital Insurance | state insurance department filings and complaint data |
| Manufacturing | Practical Machinist, Eng-Tips, r/manufacturing, r/PLC | Manufacturing Dive, IndustryWeek, Modern Machine Shop | NAM; OSHA inspection data |
| Agriculture | New Ag Talk, r/farming, r/agriculture | Successful Farming, DTN/Progressive Farmer, AgFunderNews | USDA data and reports, farm bureaus |
| IT and developer tools | r/sysadmin, r/devops, r/msp, Spiceworks Community, Lobsters, DEV Community | The Register, InfoQ, The New Stack | GitHub issues, Stack Overflow, vendor status pages |

---

## 9. Build order

Work top to bottom. Finish each connector to its definition of done before starting the next.

**Phase 0 — foundation** — done (2026-10-09)
1. Repo scaffold, `models.py`, `base.py`, `store.py`, `dedupe.py`, `costs.py`, `robots.py`, `sources.yaml` schema, `.env.example`, `scripts/smoke.py`.

**Phase 1 — reach every industry (MVP)**
2. G1 `search_api` — done (2026-10-09) as discovery + resolvers (Bluesky, Medium, Substack). 7.3 profiles need a resolver each; see the note under 7.3
3. G2 `rss`
4. ~~G3 `reddit` (search route)~~ blocked (2026-10-09): robots.txt disallows us and search results can't be stored; needs the commercial API
5. `hackernews`, `stackexchange`
6. `x` — done (2026-10-16, on-demand stored search); `bluesky` — done (2026-10-17, Jetstream); `threads` awaiting approval
7. ~~G7 `youtube`~~ blocked by YouTube developer policies (2026-10-09); covered by a G1 search profile
8. Industry seed maps for all 15 industries (section 8) — done (2026-10-09): 15 files, 68 live feeds, 19 live forums (9 XenForo, 4 Discourse with feeds); subreddits unverified until G1 checks them

**Phase 2 — forums and paid demand**
9. G4 `discourse` (waiting for a forum whose terms allow it), G5 `classic_forum` — done (2026-10-09)
10. G6 `job_boards` — done (2026-10-11); `freelancer`, `upwork` (after approval)
11. `github`, `kaggle`, bounty and challenge boards
12. `instagram`, `facebook_pages` (after Meta review), `pinterest`, `telegram`, `lemmy`, `mastodon`

**Phase 3 — tool gaps and change signals**
13. G10 `app_reviews` — done (2026-10-09, WordPress.org); `feature_boards` deferred (no stable public interface)
14. `gdelt`, `edgar`, procurement (`sam_gov`, `ted`, `uk_tenders`)
15. Regulatory and legal: `federal_register`, `regulations_gov`, `legiscan`/`openstates`, `courtlistener`, `cfpb`, `openfda`, `nhtsa`, `osha`, `eurlex`
16. G9 `open_data` — done (2026-10-09)

**Phase 4 — media, startup ecosystem, research**
17. G8 `podcasts` — built (2026-10-11), waiting on the key; `twitch` blocked (2026-10-10); `events`
18. `producthunt` (awaiting approval), `huggingface` — done (2026-10-09); startup public pages all blocked by terms or bot walls (see 7.2)
19. `trends`, `keyword_planner`, `stats` (Census BFS done 2026-10-15), `papers`, `wikipedia`

**Phase 5 — paid connectors (flag off until budgeted)** — done (2026-10-09)
20. Done (2026-10-09): `newsapi`, `reddit` (own API), `crunchbase`, `keywords_paid` built behind feature flags (off). Blocked by public terms: `yelp`, `google_places`. Deferred: `pacer`, `listening`

**Phase 6 — discovery agent** — done (2026-10-10)
21. Given a new industry, run site-limited searches to find its communities, publications and associations; detect forum software; run the access check; write a new `industries/<industry>.yaml` for human review.
    Built as `scripts/discover_industry.py` (`signal_connectors/discovery_agent.py`):
    - ~7 Exa/Brave searches from seed roles and keywords (~$0.06 per industry; capped by `--budget` and `defaults.discovery_monthly_budget_usd`). Results are pointers only: reduced to site origins in memory.
    - Each site's homepage is then fetched (robots.txt first) and classified from what the site itself returns: forum (software or forum path), publication (live feed), association (its own title). Then the usual `verify.py` checks.
    - Every forum gets `terms: unchecked` and a `terms_url` to read, unless another map already decided that domain's terms (carried over).
    - Jargon candidates come from frequent phrases in the publications' headlines. Subreddits are not searched (reddit.com is unreadable).
    - Drafts are written to `industries/drafts/<key>.yaml`, which nothing loads (the feed work reads `industries/*.yaml` too). Approving a draft = move it to `industries/` and set `discovery.needs_review: false`; the config also ignores any map still marked needs_review.

---

## 10. Working conventions for Claude Code

- Before building a connector, read the platform's official docs and terms, then update its row in section 7 if anything differs.
- One connector per pull request or commit series; include its cassette test and a smoke-run log.
- Never commit `.env`, cassettes containing secrets, or raw personal data. `tests/scrub.py` redacts personal fields (authors, handles, contact names, emails, phones, addresses) from every recorded body; check new cassettes with a grep before committing.
- If a platform's terms forbid the planned route, stop, mark the row **blocked** with the reason, and move it to the search route if allowed.
- Keep this file current: it is the source of truth for what exists and what's next.
