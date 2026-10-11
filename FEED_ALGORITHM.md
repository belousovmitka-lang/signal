# Signal — feed algorithm spec

This file specifies how Signal turns raw posts from the connectors into each builder's feed, niche map and profile niches. It sits next to `CLAUDE.md` (the connectors brief) and builds on its `RawPost` records.

Read this whole file before writing code. Every number marked *(tune)* is a starting value that lives in `config/feed.yaml`, never hard-coded.

---

## 1. What the feed optimizes for

**North star:** builders who find a problem worth building and act on it.

The algorithm optimizes, in this order of value:
1. **Outreach** to a potential customer
2. **Converting a save into an idea**
3. **"I'd build this"** and **saves**
4. **Reading a post to the end** and opening its source

It must **not** optimize for time spent, sessions per day or scroll depth. Those are tracked only as guardrails. A session ends with "You're caught up" when strong items run out; more can be loaded on purpose, but never infinitely by default.

Three rules come from how Instagram, TikTok, YouTube and X rank feeds:
- **Rank by predicted actions, weighted by value** (X's model): deep actions count many times more than shallow ones, and negative feedback counts heavily against.
- **Rank for value, not just engagement** (YouTube's "valued watch time"): occasionally ask "Was this useful?" and train on the answers.
- **Keep exploring** (TikTok): reserve slots for content outside the user's map so the feed never collapses into one topic.

---

## 2. System overview

```
connectors ──► RawPost
                 │
          [A] Enrichment (LLM tagging, embeddings, spam/PII filter)
                 │
          [B] Niche tree (industries and skills, growing narrower over time)
                 │
          [C] Problem clustering and scoring
                 │
   events ─► [D] User model (niche and skill affinities, pins, drop-ins, mode)
                 │
          [E] Candidate generation (~500 per session)
                 │
          [F] Ranking (predicted actions × value weights)
                 │
          [G] Feed composition (mix quotas, diversity, exploration, explanations)
                 │
          feed ─► app ─► events ─► [H] Learning loop (online + nightly + weekly)
```

Outputs to the app: the **feed**, the **niche map** (rings, trajectory, weekly snapshots), **why-you're-seeing-this** explanations, and the **profile niches** (subject to user approval).

---

## 3. Stage A — Enrichment

Every `RawPost` is enriched **once** (and re-enriched only when the prompt version changes).

### 3.1 EnrichedPost fields

| Field | Type | Notes |
|---|---|---|
| `post_type` | enum | actionable: `pain`, `paid_demand`, `tool_gap`, `change` (news, rules, launches), `idea`, `builder` (posts by Signal users); everything else says what it is: `research`, `listing`, `company_news`, `general_news`, `notice`, `opinion`, `guide`, `praise`, `spam`, `off_topic` (replaced a single `noise` type, 2026-10-10) |
| `industry_path` | list[str] | broad → narrow, e.g. `["Legal", "Litigation support", "Appellate brief prep"]` |
| `skills_needed` | list[str] | e.g. `["Document parsing", "Retrieval"]` |
| `who_has_pain` | str | role as stated or inferred: "litigation paralegal" |
| `pain_statement` | str | one normalized sentence: "Checking citations in briefs before filing is manual and slow" |
| `current_workaround` | str \| null | "manual checks, spreadsheets" |
| `intensity` | 0–1 | frustration, urgency, repetition ("every Friday", "never again") |
| `money_signal` | object \| null | budget, rate, salary, "I'd pay", RFP value |
| `buyer_likelihood` | 0–1 | is the poster likely the buyer or a decision maker |
| `quality` | 0–1 | specificity and credibility; low for vague or recycled content |
| `spam_score` | 0–1 | self-promotion, ads, engagement bait, outrage bait |
| `toxicity` | 0–1 | used to demote, never to promote |
| `embedding` | vector | of `pain_statement` + key text |
| `language` | str | |
| `prompt_version` | str | for re-enrichment |

### 3.2 How
- Use the Claude API with a small, fast model for tagging and a larger model only for hard or ambiguous posts. Check current model names, prices and the batch API in Anthropic's docs before choosing; use batch processing for backfills.
- One structured-output prompt per post, returning JSON validated against a pydantic schema. Retry once on invalid JSON, then mark `needs_review`.
- Drop the non-actionable types (`enrichment.drop_post_types`, all ten for now) and posts with `spam_score > 0.7` *(tune)* from ranking (keep them stored for audits). A type can be brought into the feed later by editing that list, without re-tagging.
- Strip personal data before storage (names, emails, phone numbers in text).
- Cost guard: monthly enrichment budget in config; prefer cheap pre-filters (keyword and language checks) before calling the model.

**As built (2026-10-09, prompt v1):** `signal_feed/enrichment/`. Tagger `claude-haiku-5-5` (effort low); escalation to `claude-opus-5-5` (effort medium, server-side `fallbacks: "default"`) on a refusal or confidence < 0.5; invalid output retried once, then `needs_review`. The model sees the post wrapped as untrusted data, cut at 6,000 characters with a visible marker. The top of `industry_path` is always one of the 15 seed industries or "Other"; skills prefer the starter list in `config/skills.yaml`. Pre-filter: posts under 30 characters and structured record sources (`open_data:*`, `cfpb`) are stored as `skipped` without a model call (counted in aggregate later). Backfills go through the Message Batches API (50% off); a batch reserves its worst-case cost against the budget until it is collected. Embeddings: Voyage AI `voyage-4` (1,024 dims) over pain statement + title + text, since Anthropic has no embeddings endpoint. Every call is metered in `feed_costs`; the guard checks worst-case cost (input at 2 chars/token, full `max_tokens` out) before each call. Enrichment of posts purged at the origin is deleted on each run. Quality check: `evals/enrichment_labels.yaml` (200 posts, stratified by source type) and `scripts/enrich_eval.py score`.

**Eval results (2026-10-10, against Claude-written labels):** $0.04 per 200 posts, ~1% escalated to Opus. v1: post_type 67.7%, money precision 63%. v2 added close-call rules (named-software complaints are `tool_gap`; routine company news is `noise`; money only when tied to demand). v3 fixed v2's over-corrections (device failures stay `pain`; court rulings, policy deals and new deployed technology are `change`; papers and model listings are `noise`). **v3: post_type 85.9%, industry 74.2%, money P 82.6% / R 95.0%, spam drop P 76.9% / R 90.9%, intensity level 85.4%, buyer level 85.9%.** Weakest: `pain` recall 7/13 (advice articles built on a real problem go to `noise`) and industry `Other` vs a specific industry. 

**Noise split (2026-10-10, prompt v5):** the user reviewed the labels and asked to replace `noise`, which said nothing about a post, with types that do (see 3.1). The 121 noise labels were re-labeled into those types by Claude (not yet reviewed; the other 79 labels are user-reviewed). v4 (new types) let explainers, launches and rulings slip into `guide`, `company_news` and `general_news`; v5 checks the actionable types first. **v5: post_type 86.9% (15 types), kept-in-feed precision 95.7% / recall 83.5%, industry 75.8%, money P 79.2% / R 95.0%, intensity 86.4%, buyer 85.4%.** `scripts/enrich_eval.py score` now reports the kept-in-feed decision, which matters more than the exact non-actionable type.

---

## 4. Stage B — The niche tree

The niche tree is the backbone of the map, the profile and the ranking.

### 4.1 Structure
- Two trees: **industries** (`Legal → Litigation support → Appellate brief prep → Citation verification`) and **skills** (`AI agents → Retrieval over case law`).
- Each node: `id`, `label`, `parent_id`, `level`, `centroid_embedding`, `support` (posts in the last 90 days), `created_at`, `status` (`active`, `merged`, `retired`), `version`.
- Seeded from the 15 industry maps in `CLAUDE.md` section 8, plus a starter skill list.

### 4.2 Growth (weekly job)
1. For each node with enough support (≥ 200 posts in 90 days *(tune)*), cluster its posts' embeddings (HDBSCAN or similar).
2. A cluster becomes a **child niche** when it has ≥ 40 posts from ≥ 15 distinct authors across ≥ 2 sources *(tune)* and is clearly separated from siblings.
3. An LLM names the cluster in 2–4 plain words ("Citation verification"), checked against existing names to avoid duplicates.
4. Merge siblings whose centroids are too close; retire niches whose support stays below threshold for 8 weeks.
5. Every change is versioned so user affinities can be migrated (a merged niche passes affinity to the survivor; a split passes it to children in proportion to the user's past positives).

The global tree is shared. Each user only sees the part of it their behavior touches.

---

## 5. Stage C — Problems

A **problem** is a cluster of posts describing the same pain. The feed shows posts, but problems power scoring, de-duplication, the "+12 similar posts" collapse and the evidence screen.

### 5.1 Clustering
- Within each leaf niche, cluster by `pain_statement` embedding similarity (cosine ≥ 0.82 *(tune)*), then confirm borderline merges with the LLM.
- A problem stores: `title`, `niche_id`, `posts`, distinct authors, sources, first/last seen, money signals, current workarounds, competitors mentioned.

### 5.2 Problem score (0–1)
Weighted sum *(tune)*, each component normalized 0–1:

| Component | Weight | Meaning |
|---|---|---|
| Frequency | 0.20 | distinct authors, log-scaled |
| Trend | 0.15 | this 4 weeks vs previous 4 weeks (z-score) |
| Intensity | 0.15 | mean intensity of its posts |
| Money signal | 0.20 | paid gigs, jobs, budgets, "I'd pay" |
| Tool gap | 0.10 | tool-gap posts and bad reviews of current tools |
| Source diversity | 0.10 | number of different source types |
| Buyer likelihood | 0.10 | share of posts from probable buyers |

Confidence is reported separately (low evidence = "early signal"), never hidden inside the score.

---

## 6. Stage D — The user model

### 6.1 State per user
- `niche_affinity[node_id]`: value 0–1 plus an uncertainty (Beta distribution: `alpha`, `beta`)
- `skill_affinity[node_id]`: same
- `type_preference[post_type]`: how much they value pain vs paid demand vs news vs builder posts
- `source_preference[source]`: learned lightly; never lets one source dominate
- `pins`: user-placed nodes with a fixed affinity
- `drop_ins`: nodes the user added, with an exploration budget
- `removed`: nodes the user removed (affinity forced to 0, still re-testable after 60 days only if the user allows)
- `mode`: `exploring` or `committed` (with `committed_problem_id`)
- `seen`: post and problem IDs shown, with timestamps

### 6.2 Cold start
1. **Quiz priors:** chosen industries and skills start at affinity 0.6, with high uncertainty; resume skills at 0.5.
2. **Calibration round:** the first session opens with 10 quick cards spanning the chosen industries and two wildcards; each answer ("Interested" / "Not for me") updates affinities immediately.
3. **Similar builders:** until a user has 20 events, blend in what builders with similar quiz answers saved (weight decays to 0 by 100 events *(tune)*).

### 6.3 Events and their weights *(tune)*

| Event | Weight | Notes |
|---|---|---|
| Impression only | 0 | logged for exposure counts |
| Scrolled past quickly | −0.3 | only counts when dwell < 1.5 s and the item was fully visible |
| Read | +1 | dwell ≥ 60% of expected reading time for its length |
| Expanded | +0.5 | |
| Opened source link | +1.5 | |
| Saved | +4 | |
| Shared | +3 | |
| "I'd build this" | +5 | |
| Converted to idea | +8 | |
| Outreach started | +12 | |
| "Was this useful?" yes / no | +3 / −3 | |
| "Not for me" | −6 | plus a reason modifier below |
| Hide source | −4 | on the source, not the niche |
| Report | −20 | also flags for moderation |

"Not for me" reasons route the penalty: *not my skills* → the skill nodes; *industry doesn't interest me* → the niche path; *too crowded* → lowers the competition-gap tolerance; *too small* → raises the minimum problem score; *already exists* → marks the problem as served for this user.

### 6.4 Updating affinities
- Each event updates the Beta parameters of the post's leaf niche and its skills immediately (positive weight adds to `alpha`, negative to `beta`).
- **Hierarchy propagation:** the parent gets 40% of the update, the grandparent 15% *(tune)*. Siblings are untouched.
- **Decay:** evidence halves every 21 days *(tune)*, so interests can move.
- **Pins override:** a pinned node's affinity is fixed at the value implied by where the user placed it on the map; events still accumulate underneath so unpinning restores learned behavior.
- **Drop-ins:** start at affinity 0.35 with a budget of 8 exposures over the next sessions; after the budget, the node keeps whatever affinity the user's reactions earned.

### 6.5 Narrowing (how "Legal" becomes "Citation verification")
For each node the user engages with, check weekly:
- If ≥ 60% of the node's positive weight in the last 4 weeks falls in one child niche, and that child has ≥ 5 positive events *(tune)*, **promote** the child: it joins the user's map and inherits the parent's affinity × 0.9; the parent becomes a context node on the map.
- If a node gets ≥ 10 negative events and no positives for 3 weeks, it drifts to Exploring, and later off the map.
- Each change writes a **map change log** entry in plain language ("Legal split into Litigation support and Small-firm operations", "Healthcare fading after 12 skips") for the map screen and profile prompts.

### 6.6 Map output
- **Rings:** Core ≥ 0.6, Regular 0.3–0.6, Exploring < 0.3 *(tune)*.
- **Position:** distance from center = `R × (1 − affinity)`; angle stays stable per node (children placed near their parent's angle) so the map doesn't jump between sessions.
- **Weekly snapshot** of the whole user map, kept for Replay and the profile's "How my focus narrowed".
- **Profile sync:** when a node enters Core or leaves the map, create a pending profile update; nothing is published until the user approves (unless they turned on auto-publish). Raw events are never exposed.

### 6.7 Modes
- **Exploring:** default. Feed composition as in section 9.
- **Committed:** the user picked a problem. The feed narrows to that problem's niche, its evidence, competitors, related paid demand and builders working nearby; everything else is capped at 10% *(tune)*.

---

## 7. Stage E — Candidate generation

Per session, gather ~500 candidates *(tune)* from these sources, then de-duplicate:

| Source | Share target | How |
|---|---|---|
| Niche match | ~45% | nearest neighbours to the user's top niches (affinity-weighted centroids) |
| Skill match | ~15% | posts whose `skills_needed` match the user's top skills |
| Similar builders | ~10% | what builders with similar maps saved recently |
| Trending nearby | ~10% | rising problems in parent and sibling niches |
| Exploration | ~10% | niches outside the map, chosen by Thompson sampling |
| Pins and drop-ins | as needed | guaranteed candidates for pinned and dropped nodes |
| Followed builders | as needed | posts from builders the user follows (community phase) |
| Company-posted problems | later | when the company side launches |

Exclude: posts seen in the last 30 days, problems marked "already exists" or removed, hidden sources, posts with `spam_score` above threshold.

---

## 8. Stage F — Ranking

### 8.1 What the ranker predicts
For each candidate: probabilities of `read`, `save`, `id_build`, `idea`, `outreach`, `not_for_me`, `scroll_past`.

**Score = Σ (event weight × predicted probability)**, using the weights in 6.3. Then multiply by a quality factor: `(0.6 + 0.4 × problem_score) × (1 − 0.5 × toxicity)` *(tune)*.

### 8.2 Model versions
- **v1 (launch):** no trained model. Probabilities estimated from features with hand-set coefficients (a transparent linear score). Ship this first.
- **v2 (≥ 50k labeled impressions):** one gradient-boosted model per action (LightGBM or similar), trained nightly.
- **v3 (later):** one multi-task neural model sharing user and post embeddings.

### 8.3 Features
- User × post: affinity of the post's leaf niche and its parents, skill match, type preference, source preference, similarity to the user's last 20 saves, similarity to "Not for me" items
- Post: post type, problem score and confidence, intensity, money signal, buyer likelihood, quality, freshness (hours old), source type
- Social: saves and "I'd build this" from similar builders, number of builders already working on this problem (too many lowers the score for users who said "too crowded")
- Context: mode, session position, time since last session

### 8.4 Value model (YouTube-style)
About 1 in 30 items *(tune)* gets a one-tap "Was this useful?" after the user reads it. Train a model to predict the answer for every item and blend it into the score (start at 20% of the final score *(tune)*). This keeps the feed honest when clicks and value disagree.

---

## 9. Stage G — Feed composition

The ranked list becomes a feed through rules applied in order.

1. **Collapse duplicates:** show the best post per problem, with "+N similar posts" on it.
2. **Mix quotas per 20 items** *(tune)*:
   - Core and Regular niches: ~60%
   - Paid demand: at least 15%
   - Change signals (news, rules, launches): at most 20%
   - Exploration: 10–15%
   - Builder posts: up to 15% once the community exists
3. **Diversity:**
   - Never two items in a row from the same problem, same source, or same post type
   - At most 3 items from the same leaf niche in any 10
   - At least 40% of the first 10 items are under 48 hours old, when available
4. **Pins and drop-ins:** guaranteed exposure as budgeted.
5. **Session end:** after the strong items run out (predicted score below a floor *(tune)*), show "You're caught up" with an optional "Explore more" that switches to exploration-heavy items.
6. **Explanation:** every item carries a short reason: "Citation verification · you saved 4 similar posts" or "Exploring: Insurance claims · new for you".

---

## 10. Stage H — Learning loop

| Cadence | Job |
|---|---|
| Real time | update affinities after each event; the next items in the same session reflect it |
| Hourly | refresh problem trend scores for active niches |
| Nightly | retrain ranking and value models (v2+); recompute similar-builder neighbours |
| Weekly | grow, merge and retire niches (4.2); run narrowing checks (6.5); write map snapshots and change logs |

**Exploration:** Thompson sampling over niches and post types using each user's Beta distributions, so uncertain areas get tested and confident dislikes stay out.

**Guard against filter bubbles:** if one leaf niche exceeds 50% of a user's last 100 impressions *(tune)*, raise exploration to 20% for the next sessions, except in committed mode.

---

## 11. Data model

Tables (SQLAlchemy; in a separate feed database for now, movable to the connectors' database by config). `post_id` columns are not foreign keys, so connectors can purge deleted posts at any time:

- `enriched_posts` (1:1 with `raw_posts`)
- `niches` (tree, versioned), `niche_versions`
- `problems`, `problem_posts`
- `users`, `user_quiz`
- `events` (append-only: user, item, event type, value, dwell, reason, session, timestamp)
- `impressions` (what was shown, position, explanation, score breakdown)
- `user_niche_affinity`, `user_skill_affinity` (alpha, beta, updated_at)
- `user_pins`, `user_drop_ins`, `user_removed`
- `user_map_snapshots` (weekly JSON of positions, rings, labels)
- `map_change_log` (plain-language entries)
- `profile_pending_updates`
- `surveys` ("Was this useful?" answers)

---

## 12. API

| Endpoint | Purpose |
|---|---|
| `GET /feed?session_id=&cursor=` | next feed items with explanations |
| `POST /events` | batch of user events |
| `GET /map` | current map: nodes, rings, positions, parents, statuses (rising, fading, new) |
| `GET /map/history` | weekly snapshots and change log for Replay |
| `POST /map/pin` | pin a node at a position |
| `POST /map/drop-in` | add a node from the catalog |
| `POST /map/remove` | remove a node |
| `POST /map/nudge` | "Show more" / "Show less" on a node |
| `GET /map/node/{id}` | path, saves, reads, skips, and why it's placed there |
| `GET /profile/niches/pending` and `POST /profile/niches/approve` | profile sync with user approval |
| `POST /mode` | switch exploring / committed |

Every feed item response includes a `score_breakdown` (hidden from users, used for debugging and evaluation).

---

## 13. Measuring it

**Product metrics (weekly):**
- Builders who save ≥ 3 posts and convert ≥ 1 idea (north star)
- Save rate per session, idea conversion rate, outreach rate
- "Not for me" rate and "Was this useful?" share
- Retention at day 7 and day 30

**Feed health:**
- Diversity: distinct niches and sources per 20 items
- Exploration acceptance: saves from exploration items
- Coverage: share of the problem catalog shown to anyone in 30 days

**Guardrails (watch, never optimize):** time spent, sessions per day, scroll depth. If time spent rises while saves fall, the feed is getting worse.

**Offline evaluation:** replay held-out events; NDCG@20 on saves and ideas; compare every ranking change against the current version before shipping.

**Online:** A/B tests with per-user assignment; ship only changes that improve the north star without hurting "Not for me" rate or diversity.

---

## 14. Safety and privacy

- Demote outrage, toxicity and engagement bait; never reward them.
- Anonymize authors in the app ("a practice manager"); link to originals.
- Honor deletions required by source platforms' terms.
- Niche data is personal: profile niches publish only with user approval; public profiles show broad areas, connections see exact niches; raw events never leave the backend.
- Users can see and reset their map at any time.

---

## 15. Build order

Finish each phase with tests and a short demo script before starting the next.

1. **Phase 0 — foundations:** tables, `config/feed.yaml`, event logging, `POST /events`. — done (2026-10-09): package `signal_feed/`, tests in `tests/feed/`, demo `scripts/feed_demo.py`. Feed tables live in a separate database for now (`FEED_DATABASE_URL`, default `data/feed.db`); raw posts are read read-only from the connectors' `DATABASE_URL`. `POST /events` takes `user_id` in the body until app auth exists.
2. **Phase 1 — enrichment:** Stage A with schema validation, cost guard and a labeled sample of 200 posts to check tagging quality. — built (2026-10-09); run live (2026-10-10), prompt v5, post_type accuracy 86.9% over 15 types; backfill (2026-10-10): 7,068 posts, 3,161 `ok`, 2,249 `dropped`, 1,658 `skipped`; $0.54 through the Batch API, 90 escalations (1.8%). Embeddings paced to Voyage's no-card limits (3 requests, 10k tokens per minute) in `config/feed.yaml`. The 200 reference labels were written by Claude; 34 flagged `unsure` await human review.
3. **Phase 2 — niche tree and problems:** seed tree, problem clustering and scoring; a script that prints the top problems per niche.
4. **Phase 3 — MVP feed:** user model with quiz priors and calibration, candidate generation, v1 linear ranker, feed composition with quotas, diversity and explanations; `GET /feed`.
5. **Phase 4 — map:** affinity-to-map conversion, pins, drop-ins, remove, nudge, weekly snapshots, change log, narrowing checks; all `/map` endpoints.
6. **Phase 5 — profile sync:** pending updates and approval endpoints.
7. **Phase 6 — learning:** "Was this useful?" surveys, Thompson-sampling exploration, filter-bubble guard, nightly v2 models once there is enough data.
8. **Phase 7 — evaluation:** offline replay harness, metrics dashboard, A/B assignment.

---

## 16. Working conventions for Claude Code

- Keep every weight, threshold and quota in `config/feed.yaml`.
- Every ranking change ships with an offline comparison against the current version.
- Log a `score_breakdown` for every impression so any feed item can be explained.
- When behavior depends on real usage data that doesn't exist yet, build it behind a flag and use the v1 rules.
- Keep this file current as the source of truth for how the feed works.
