# Signal — source connectors

This repo holds the data-collection layer for **Signal**, an app that helps builders find problems worth building. Signal shows each builder a feed of real problems, paid demand, tool gaps and industry news matched to their skills and interests. The builder saves posts, Signal turns saves into structured ideas, and then it helps them reach a first customer.

This repo does one job: **collect raw posts from every source below and store them as clean, uniform records.** Ranking, clustering, matching and the app itself live elsewhere and are out of scope here.

The guiding principle from the founder: **build more connectors than users will need, rather than leave anyone with a thin feed.** Every platform in the catalog below gets a connector or a search profile, unless it is marked blocked.

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
    industries.py           # industry seed map schema and loader
    forum_detect.py         # detect forum software + feeds from a logged-out page
    verify.py               # live checks for seed maps (liveness, robots, login walls, moved feeds)
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
      ... one module per platform-specific connector in section 7
  scripts/
    run.py                  # run due connectors on schedule
    smoke.py                # run one connector live: python scripts/smoke.py rss --limit 20
    verify_industries.py    # check every seed-map site and write statuses back
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
STACKEXCHANGE_KEY=           # optional; raises quota 300 -> 10,000/day
BLUESKY_HANDLE=              # pending rule-1 decision
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
| G1 | `search_api` | Every platform in 7.3 plus any niche site without its own connector | search_api | Brave and Exa behind one interface. Each platform gets a **search profile**: domains/path filters + query templates. Store snippets with `is_snippet=True`. |
| G2 | `rss` — **done (2026-10-09)** | Trade press, newsletters (Substack, Beehiiv), Medium tags, blogs, press release wires, podcast feeds, VC blogs, bar/association news, Stack Exchange feeds | rss | Google News keyword feeds are **blocked** (robots.txt + non-commercial terms, checked 2026-10-09): keyword news goes through G1. Medium: `https://medium.com/feed/tag/<tag>`. Substack: `<pub>/feed`. |
| G3 | `reddit` | All subreddits in the industry maps and section 7.2 | search_api now, own_api after approval | Until commercial approval, run through G1 with `site:reddit.com/r/<sub>`. After approval: OAuth API, `/r/<sub>/new`, `/r/<sub>/search`, comments. Honor deletions per Reddit terms. |
| G4 | `discourse` | Any Discourse forum (many community and vendor forums) | public_pages | Discourse serves JSON by appending `.json` (e.g. `/latest.json`, `/t/<slug>/<id>.json`, `/search.json?q=`). Detect Discourse from page meta. |
| G5 | `classic_forum` | XenForo, vBulletin, phpBB and generic HTML forums | rss / public_pages | Prefer each board's RSS (`/forums/-/index.rss`, `external.php?type=RSS2`, `feed.php`). Otherwise parse public thread lists. Check robots.txt first. |
| G6 | `job_boards` | Company job posts across all industries | own_api | Greenhouse `boards-api.greenhouse.io/v1/boards/<token>/jobs`, Lever `api.lever.co/v0/postings/<company>`, Ashby `api.ashbyhq.com/posting-api/job-board/<name>`, SmartRecruiters public postings API, USAJOBS `data.usajobs.gov/api/search`. Build a company list per industry. |
| G7 | `youtube` — **blocked (2026-10-09)** | YouTube videos, comments, conference talks, webinars posted to YouTube | own_api → G1 | YouTube API Developer Policies III.E forbid aggregating API Data across channels not owned by one content owner, using API Data "to create new or derived data or metrics", and storing non-statistics data over 30 days. Signal's cross-channel feed, ranking and idea generation do all three. Until then: G1 search profile `youtube.com/watch` (titles + snippets). Unblocks with: YouTube API compliance audit and the derived-metrics permission (policy section L, via the quota extension form, since June 2026). |
| G8 | `podcasts` | Industry podcasts | rss + own_api | Podcast Index API to discover shows; RSS for episodes; transcribe audio with a speech-to-text service (cost-metered) or use publisher transcripts when provided. |
| G9 | `open_data` | City/state open data portals, data.gov, FCC data | own_api | Socrata SODA API and CKAN API. Datasets: 311 requests, permits, inspections, complaints. |
| G10 | `app_reviews` | App Store, app marketplaces | own_api / public_pages | App Store reviews feed: `https://itunes.apple.com/<country>/rss/customerreviews/id=<appId>/sortBy=mostRecent/json`. WordPress plugins: `api.wordpress.org` plugin info + support forum RSS. Atlassian Marketplace REST API. Shopify App Store, Salesforce AppExchange, Chrome Web Store: public pages if allowed, else G1. |

### 7.2 Platform-specific connectors (own API or public data)

**Communities and forums**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Reddit | G3 `reddit` | search_api → own_api | see G3 | hourly | todo; apply for commercial access now |
| Hacker News | `hackernews` | own_api | Algolia HN Search API `hn.algolia.com/api/v1/search_by_date` (keyword phrases, tags) + Firebase API `hacker-news.firebaseio.com/v0` (ask/show/job lists; MIT, no rate limit) | hourly | done (2026-10-09) |
| Stack Exchange network (incl. Stack Overflow, Law, Workplace, Money, Academia, DIY) | `stackexchange` | own_api | `api.stackexchange.com/2.3/search/advanced?site=<site>`; 300 req/day per IP without a key, 10,000 with a free stackapps.com key; obey the `backoff` field. Content is CC BY-SA 4.0: the app must credit Stack Exchange and link to each question | daily | done (2026-10-09) |
| Telegram public channels | `telegram` | public_pages | Public channel previews `https://t.me/s/<channel>`; do not use MTProto with a personal account | daily | todo |
| Lemmy instances | `lemmy` | own_api | `/api/v3/search`, `/api/v3/post/list?community_name=` | daily | todo |
| Mastodon instances | `mastodon` | own_api | `/api/v1/timelines/tag/<tag>`, `/api/v2/search` | daily | todo |
| Niche industry forums (Contractor Talk, Spiceworks, BiggerPockets, AllNurses, Fishbowl, TruckersReport, HVAC-Talk, Mike Holt, Practical Machinist, Eng-Tips, New Ag Talk, DentalTown, Student Doctor Network, ActiveRain and every forum in section 8) | G4 / G5, else G1 | public_pages / rss | detect forum software per site | daily | todo |
| Vendor community forums (Shopify Community, Salesforce, QuickBooks Community, HubSpot, Amazon Seller Forums, eBay Community, SHRM community) | G4 / G5, else G1 | public_pages / rss | detect per site | daily | todo |
| Mumsnet and parenting forums | G5, else G1 | public_pages | Mumsnet has objected to AI scraping: check terms carefully; default to G1 | daily | todo |

**Social platforms**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| X (Twitter) | `x` | own_api | API v2 recent search `/2/tweets/search/recent`; pay-per-use, cost-metered | hourly | todo (needs credits) |
| Threads | `threads` | own_api + search_api | `GET graph.threads.net/v1.0/keyword_search` (needs `threads_basic` + `threads_keyword_search` app review; 2,200 queries/24h); overflow through G1 `site:threads.net` | daily | awaiting approval |
| Bluesky | `bluesky` | own_api | AT Protocol `app.bsky.feed.searchPosts`. As of 2026-10-09 the public AppView returns 403 to unauthenticated search; search needs an authenticated session (app password). Decision pending on whether a disclosed bot account fits rule 1; until then G1 `site:bsky.app` | hourly | todo |
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
| USAJOBS | G6 | own_api | `data.usajobs.gov/api/search` | weekly | todo |
| State government job boards | G2 / G5, else G1 | rss / public_pages | per state | weekly | todo |
| Industry job boards (legal, healthcare, construction, logistics) | G2 / G5, else G1 | rss / public_pages | per board | weekly | todo |
| Company career pages (all industries) | G6 | own_api | Greenhouse, Lever, Ashby, SmartRecruiters | weekly | todo |
| GitHub bounties, Algora, Gitcoin | `github` + G1 | own_api | GitHub search for issues with bounty labels; Algora/Gitcoin public pages | daily | todo |
| IssueHunt and other bounty boards | G5, else G1 | public_pages | public listings | daily | todo |
| Kaggle | `kaggle` | own_api | Kaggle API competitions list | weekly | todo |
| HeroX, Wazoku (InnoCentive) | G5, else G1 | public_pages | public challenge listings | weekly | todo |
| Devpost and sponsored hackathons | G5, else G1 | public_pages | public hackathon listings | weekly | todo |
| Amazon Mechanical Turk | G5, else G1 | public_pages | only if task listings are visible logged-out; otherwise blocked | weekly | todo |

**Reviews, app stores, feature requests**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Apple App Store | G10 | own_api | customer reviews feed per app and country | daily | todo |
| Chrome Web Store | G10 | public_pages, else G1 | public listing pages | weekly | todo |
| Shopify App Store, Salesforce AppExchange, Atlassian Marketplace, WordPress plugins | G10 | own_api / public_pages | Atlassian REST API, WordPress.org API + support RSS, others public pages or G1 | weekly | todo |
| Public feature boards (Canny, UserVoice, Productboard portals) | `feature_boards` | public_pages | public board pages; detect vendor per site | weekly | todo |
| GitHub issues and discussions | `github` | own_api | REST/GraphQL search issues and discussions | daily | todo |
| Better Business Bureau | G5, else G1 | public_pages | check terms; default G1 | weekly | todo |

**News, trade press, funding**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Google News (keyword) | G2 | rss | Google News RSS search | hourly | blocked (2026-10-09): robots.txt disallows `/rss`, feeds are personal/non-commercial only; use G1 news search + `gdelt` |
| GDELT | `gdelt` | own_api | GDELT DOC 2.0 API | daily | todo |
| Wire services, business press, tech press, trade publications, press release wires, newsletters, company and industry blogs, local news, VC and accelerator blogs | G2 | rss | headlines + summaries; full text only where the feed provides it | hourly to daily | done (2026-10-09): 36 trade-press feeds live; more per industry map |
| SEC EDGAR (Form D funding filings) | `edgar` | own_api | EDGAR full-text search and submissions APIs (set a User-Agent with contact email) | daily | todo |
| Layoff and shutdown trackers | G5, else G1 | public_pages | check terms per tracker | weekly | todo |

**Government, regulatory, legal**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| SAM.gov | `sam_gov` | own_api | Opportunities API | daily | todo |
| State/city procurement portals, BidNet, DemandStar | G5 / G2, else G1 | public_pages | per portal; many publish RSS | daily | todo |
| EU TED | `ted` | own_api | TED API | daily | todo |
| UK Contracts Finder / Find a Tender | `uk_tenders` | own_api | official APIs | daily | todo |
| School district and university RFPs | G5 / G2, else G1 | public_pages | per site | weekly | todo |
| CFPB complaint database | `cfpb` | own_api | Consumer Complaint Database API | daily | todo |
| FDA device reports (MAUDE) | `openfda` | own_api | openFDA device/event API | weekly | todo |
| NHTSA complaints | `nhtsa` | own_api | NHTSA complaints API | weekly | todo |
| FCC consumer complaints | G9 | own_api | FCC open data (Socrata) | weekly | todo |
| FTC published complaint datasets | G9 / downloads | public_pages | published datasets only | monthly | todo |
| OSHA enforcement data | `osha` | own_api | Department of Labor enforcement data API | monthly | todo |
| Federal Register | `federal_register` | own_api | `federalregister.gov/api/v1/documents` | daily | todo |
| Regulations.gov | `regulations_gov` | own_api | API v4 (documents and public comments) | daily | todo |
| EU and national official journals | `eurlex` + G2 | own_api / rss | EUR-Lex web services and RSS | weekly | todo |
| State legislature trackers | `legiscan` / `openstates` | own_api | LegiScan API, Open States API | daily | todo |
| CourtListener (incl. RECAP copies of PACER filings) | `courtlistener` | own_api | REST API v4 search + opinions + dockets | daily | todo |
| PACER (direct) | `pacer` | own_api (paid) | $0.10/page, $3 cap/document; only for specific documents | on demand | paid (flag off) |
| Bar associations, medical boards, licensing bodies | G2 / G5, else G1 | rss / public_pages | per body | weekly | todo |
| City council and school board agendas and minutes | G5 / G9, else G1 | public_pages | many cities use the same agenda platforms; detect per site | weekly | todo |
| Open data portals (data.gov and city portals) | G9 | own_api | Socrata / CKAN | weekly | todo |
| Patents (USPTO, Google Patents) | `patents` | own_api | USPTO open data / PatentsView APIs | monthly | todo |

**Podcasts, video, events**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Podcasts (Apple, Spotify, Podcast Index) | G8 | rss + own_api | Podcast Index API + RSS | weekly | todo |
| YouTube, recorded conference talks, webinars on YouTube | G7 → G1 | search_api | own API blocked (see G7); G1 `youtube.com/watch` | daily | blocked (2026-10-09) |
| Twitch | `twitch` | own_api | Helix API: streams, videos (VOD metadata) by category/keyword | weekly | todo |
| Webinars hosted elsewhere | G5, else G1 | public_pages | public pages and descriptions | weekly | todo |
| Event agendas (Eventbrite, Meetup, Luma, conference sites) | `events` + G1 | public_pages / own_api | Eventbrite has no public event search API: use G1; Meetup GraphQL API (check access); Luma and conference sites public pages | weekly | todo |

**Startup and builder ecosystem**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Product Hunt | `producthunt` | own_api | GraphQL API v2; commercial use needs Product Hunt's permission (request it) | daily | awaiting approval |
| Indie Hackers | G5, else G1 | public_pages | check terms | daily | todo |
| YC Requests for Startups | G5 | public_pages | single page, monthly diff | monthly | todo |
| BetaList, AlternativeTo | G5, else G1 | public_pages | check terms | weekly | todo |
| Acquire.com, Flippa listings | G5, else G1 | public_pages | logged-out listings only | weekly | todo |
| GitHub trending | `github` | public_pages / own_api | trending page or search by recent stars | daily | todo |
| Hugging Face | `huggingface` | own_api | Hub API: trending models, spaces | weekly | todo |
| Kickstarter, Indiegogo | G5, else G1 | public_pages | check terms | weekly | todo |

**Research and search data**

| Platform | Connector | Route | Endpoint / method | Cadence | Status |
|---|---|---|---|---|---|
| Google Trends | `trends` | public_pages | apply for the official Trends API (limited testing); unofficial libraries break often, use only as a stopgap | weekly | todo |
| Google Keyword Planner | `keyword_planner` | own_api | Google Ads API KeywordPlanIdeaService | monthly | todo |
| Google autocomplete / "People also ask" | G1 | search_api | via search API results | weekly | todo |
| Industry association surveys, vendor benchmark reports, analyst firms' free reports | G2 / G5 | rss / public_pages | per publisher | monthly | todo |
| Government statistics (BLS, Census, Eurostat) | `stats` | own_api | BLS API, Census API, Eurostat API | monthly | todo |
| Academic papers | `papers` | own_api | arXiv API, PubMed E-utilities, Semantic Scholar API | weekly | todo |
| Wikipedia | `wikipedia` | own_api | MediaWiki API (recent changes) + Wikimedia pageviews API | weekly | todo |

**Paid connectors (build, keep behind a feature flag, off by default)**

| Platform | Connector | Cost | Why build it anyway |
|---|---|---|---|
| Yelp Fusion | `yelp` | from ~$229/month; 3 short excerpts per business | local business complaints for restaurant, trades and service industries |
| Google Places reviews | `google_places` | pay per call; 5 reviews per place | same |
| NewsAPI.org or similar | `newsapi` | ~$25–449/month | broader news search if RSS gaps appear |
| Crunchbase | `crunchbase` | enterprise plan, ~$20,000/year | structured funding data |
| Semrush / Ahrefs | `keywords_paid` | ~$500+/month | search volume at scale |
| PACER direct | `pacer` | per page | specific court documents |
| Social listening tool (Brandwatch, Talkwalker) | `listening` | ~$800–5,000/month | licensed coverage of LinkedIn, Instagram, TikTok, Facebook at scale |
| Reddit commercial API | G3 | ~$12,000+/month reported | full Reddit coverage after approval |

### 7.3 Search profiles (G1) — platforms reached only through search

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
| Bluesky (until the auth decision) | `bsky.app/profile/*/post` |
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
2. G1 `search_api` (Brave first, then Exa) with search profiles for every row in 7.3
3. G2 `rss`
4. G3 `reddit` (search route)
5. `hackernews`, `stackexchange`
6. `x` (once credits are bought), `bluesky`, `threads`
7. ~~G7 `youtube`~~ blocked by YouTube developer policies (2026-10-09); covered by a G1 search profile
8. Industry seed maps for all 15 industries (section 8) — done (2026-10-09): 15 files, 68 live feeds, 19 live forums (9 XenForo, 4 Discourse with feeds); subreddits unverified until G1 checks them

**Phase 2 — forums and paid demand**
9. G4 `discourse`, G5 `classic_forum`
10. G6 `job_boards`, `freelancer`, `upwork` (after approval)
11. `github`, `kaggle`, bounty and challenge boards
12. `instagram`, `facebook_pages` (after Meta review), `pinterest`, `telegram`, `lemmy`, `mastodon`

**Phase 3 — tool gaps and change signals**
13. G10 `app_reviews`, `feature_boards`
14. `gdelt`, `edgar`, procurement (`sam_gov`, `ted`, `uk_tenders`)
15. Regulatory and legal: `federal_register`, `regulations_gov`, `legiscan`/`openstates`, `courtlistener`, `cfpb`, `openfda`, `nhtsa`, `osha`, `eurlex`
16. G9 `open_data`

**Phase 4 — media, startup ecosystem, research**
17. G8 `podcasts`, `twitch`, `events`
18. `producthunt`, `huggingface`, startup public pages
19. `trends`, `keyword_planner`, `stats`, `papers`, `wikipedia`

**Phase 5 — paid connectors (flag off until budgeted)**
20. `yelp`, `google_places`, `newsapi`, `crunchbase`, `keywords_paid`, `pacer`, `listening`, Reddit own API

**Phase 6 — discovery agent**
21. Given a new industry, run site-limited searches to find its communities, publications and associations; detect forum software; run the access check; write a new `industries/<industry>.yaml` for human review.

---

## 10. Working conventions for Claude Code

- Before building a connector, read the platform's official docs and terms, then update its row in section 7 if anything differs.
- One connector per pull request or commit series; include its cassette test and a smoke-run log.
- Never commit `.env`, cassettes containing secrets, or raw personal data.
- If a platform's terms forbid the planned route, stop, mark the row **blocked** with the reason, and move it to the search route if allowed.
- Keep this file current: it is the source of truth for what exists and what's next.
