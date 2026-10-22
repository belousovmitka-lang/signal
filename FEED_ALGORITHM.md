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

Outputs to the app: the **feed**, the **niche map** (rings, trajectory, weekly snapshots), and the **profile niches** (subject to user approval).

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

**As built (2026-10-09, prompt v1):** `signal_feed/enrichment/`. Tagger `claude-haiku-5-5` (effort low); escalation to `claude-opus-5-5` (effort medium, server-side `fallbacks: "default"`) on a refusal or confidence < 0.5; invalid output retried once, then `needs_review`. The model sees the post wrapped as untrusted data, cut at 6,000 characters with a visible marker. The top of `industry_path` is always one of the 15 seed industries or "Other"; skills prefer the starter list in `config/skills.yaml`. Pre-filter: posts under 30 characters and structured record sources (`open_data:*`, `cfpb`) are stored as `skipped` without a model call (counted in aggregate later). Backfills go through the Message Batches API (50% off); a batch reserves its worst-case cost against the budget until it is collected. Embeddings: Voyage AI `voyage-4` (1,024 dims) over pain statement + title + text, since Anthropic has no embeddings endpoint. Every call is metered in `feed_costs`; the guard checks worst-case cost (input at 2 chars/token, full `max_tokens` out) before each call. Enrichment of posts purged at the origin is deleted on each run. Quality check: `evals/enrichment_labels.yaml` (200 posts, stratified by source type) and `scripts/enrich_eval.py score`. **Update (2026-10-15):** escalation moved to `claude-sonnet-5-5` (half Opus's price; ~3.5% of v9 posts escalate). `collect` makes no paid calls: retries and escalations are marked on the row (`followup`) and run by `scripts/enrich.py escalate`, at most `escalation.max_per_run` (100) per run. Metering bug fixed: with server fallbacks, the requested model's usage entry has no model name and was dropped, so 371 Opus escalations (2026-10-11 and 2026-10-15) went unmetered; an estimated $5.94 (`purpose: adjust_est`, $0.016 per call) was added to the ledger.

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

**Seeded branches (2026-10-11):** the tree's second level is not left to the data. `config/niches.yaml` lists 8–13 branches per industry (155 in total), named for the work, not the buyer: a tender goes under what it buys, so there are no procurement branches. They exist from day one (shown as "no posts yet") so builders can pick any of them, are never retired, and survive any merge. The tagging prompt (v8) lists each industry's branches and the tagger copies the first sub-niche from that list when one fits. A post whose tag names no branch goes to the branch whose description embedding (Voyage, "Industry > Branch: covers", re-embedded when edited) is nearest, if it beats the runner-up by `seed_match_margin` 0.04; calibrated on tag-matched posts, where the nearest branch was the tagged one 97% of the time, and about 70% of untagged posts clear the margin. A branch removed from the file is retired (posts reached it only by name, so it could never fill again); its posts fall back to the parent and the weekly job moves user state there. New industries approved by the connectors session (dental, veterinary) need branches added; `build` lists any that have none; growth still adds branches next to and under the seeds where the data shows a group they don't cover.

**As built (2026-10-11):** `signal_feed/niches/`. Roots are seeded by name (16 industries incl. "Other", 32 starter skills plus "Other skills"); a post reaches its root by its tagged industry or skill, and a seeded branch by its first tagged sub-niche, then descends into the closest child whose `assign_min_cosine` it clears (set per child at creation: the 10th percentile of its members' cosine). Placements live in `post_niches` (one industry row per post, one row per skill). Weekly job (`scripts/problems.py build`): retire (below `child_min_posts` for 8 recorded weeks), split (PCA to 50 dims, HDBSCAN on the posts sitting directly on a node with ≥ 200 recent posts), merge (sibling centroids ≥ 0.92), re-place, record weekly support in `niche_support`. Only the trees in `niches.grow_trees` grow (industry only for now: clustering inside a skill grouped posts by industry, not sub-skill, and named them after roles); grown niches of other trees are retired. Most posts have no author (tenders, device reports, news), so each authorless post counts as one voice (`authorless_counts_as_distinct`). Grown niches must be tight: a cluster becomes a child only if the posts it would attract with its own floor have median cosine ≥ 0.62 to its centroid (`child_min_cohesion`), and the weekly job retires grown niches that fall below it (reason `loose`); a post enters a grown child only at cosine ≥ 0.55 (`child_min_cosine`) and when it is closer to the child than to the parent. (Added 2026-10-11 after "Other > Video editing" turned out to be a catch-all of unrelated consumer complaints.) New children are named by Claude (Haiku 5.5) from sample pain statements and the tagged sub-niches, checked against siblings; without the LLM the most common tagged sub-niche (industries) or role (skills) is used, else "Cluster N".

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

**As built (2026-10-11):** `signal_feed/problems/`. Clusters use a separate embedding of `pain_statement` alone (`enriched_posts.pain_embedding`), average-linkage within each niche's directly placed posts; cluster pairs between 0.78 and 0.82 go to a Claude same-problem check (≤ 200 per run). Problems are rebuilt each run and keep their id when they overlap an old problem by Jaccard ≥ 0.3; old problems without a successor are marked `gone`, not deleted. Title = the pain statement closest to the centroid; `competitors` come from a new tag field, `tools_mentioned` (prompt v6+). Components: frequency = log(1+voices)/log(51); trend = sigmoid of a Poisson z (last 28 days vs the 28 before); intensity = mean; money = share of posts stating money; tool gap = share of `tool_gap`; source diversity = source types / 4; buyer = share with buyer likelihood ≥ 0.67. Confidence = log(1+posts)/log(21), "early signal" below 0.5.

**First run (2026-10-11, prompt v7, 3,422 posts):** 26 new niches, 1,984 problems. Tender clusters scored money 1.0 and buyer 1.0 by construction, so 2-post tender clusters (≈ 0.54) outranked large pain clusters (e.g. 73 FDA reports of failed CGM alarms, ≈ 0.30).

**Demand items (2026-10-11, user decision):** tenders, job posts and gigs (`problems.demand_types`, now `paid_demand`) are their own category on the money side, not pain problems. Within each niche they cluster separately from pain posts (`problems.kind` = `pain` | `demand`) and get their own score, `demand_score_weights`: frequency (distinct buyers asking for the same thing) 0.40, money stated 0.25, trend 0.20, source diversity 0.15. Pain problems keep the table above, with money counting only amounts stated in pain posts. `scripts/problems.py top` lists a niche's problems and its demand apart (`--kind pain|demand`). Demand clusters in one niche whose posts all come from one author (a maintainer's string of bounties) are joined into one item: one buyer asking, not many; authorless tenders are never joined this way. Rebuild: 701 pain problems, 1,275 demand items.

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

**Skills are a small bonus, never a filter** *(2026-10-11, founder decision)*: nobody can list all their skills, and neither a resume nor LinkedIn captures them fully, so a skill the user didn't name must never cost them posts. Skill match is 8% of candidates (was 15%) and the skill feature's ranker coefficients are half the first values; skills a user never named stay neutral rather than negative, and they rise on their own from reactions (every event also updates the post's skills).

**Open: a smarter skills quiz.** Asking for a list under-reports skills. Direction to design: infer skills from what the person has built or does (free-text "what have you built?", portfolio or repo links, job titles), mapped to the skill vocabulary by the LLM with confidence, plus a few quick either/or cards; treat the result as soft priors, and keep learning from reactions.

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
- **Clips (built 2026-10-21, `feed/clips.py`):** a page the user saves with the "Clip to Signal" button counts like a strong save, with weight 6 *(tune)* against 4 for a save. It isn't one of our posts, so it is placed by its nearest posts. Title, selection and note are embedded once, and the 10 closest posts with cosine ≥ 0.5 vote on its niche. If no leaf gets 35% of the vote, the vote moves up to the parent niche. Skills held by 30% of those posts get evidence too. With fewer than 3 close posts, interests are left alone. The clip's vector also joins the saved posts in `save_sim`. Clips stay the user's own: feed.db keeps only the vector and the evidence it added, so deleting or editing a clip undoes or redoes exactly that. A clip is learned when that user's next feed page loads; `enrich.py embed` also syncs all users.

### 6.5 Narrowing (how "Legal" becomes "Citation verification")
For each node the user engages with, check weekly:
- If ≥ 60% of the node's positive weight in the last 4 weeks falls in one child niche, and that child has ≥ 5 positive events *(tune)*, **promote** the child: it joins the user's map and inherits the parent's affinity × 0.9; the parent becomes a context node on the map.
- If a node gets ≥ 10 negative events and no positives for 3 weeks, it drifts to Exploring, and later off the map.
- Each change writes a **map change log** entry in plain language ("Legal split into Litigation support and Small-firm operations", "Healthcare fading after 12 skips"), **kept internal** *(changed 2026-10-11, founder decision: the map is silent, like the feed)*: the map changes without telling the user how or why. The log serves debugging and profile sync; the app gets no messages, no rising/fading/new flags and no node sources.

### 6.6 Map output
- **Rings:** Core ≥ 0.6, Regular 0.3–0.6, Exploring < 0.3 *(tune)*.
- **Position:** distance from center = `R × (1 − affinity)`; angle stays stable per node (children placed near their parent's angle) so the map doesn't jump between sessions.
- **Weekly snapshot** of the whole user map, kept for Replay and the profile's "How my focus narrowed".
- **Profile sync:** when a node enters Core or leaves the map, create a pending profile update; nothing is published until the user approves (unless they turned on auto-publish). Raw events are never exposed.

**As built (2026-10-11, Phase 4):** `signal_feed/feed/usermap.py`, `weekly.py`. Map membership is explicit (`user_map_nodes`: `active`, `context`, `fading`, `off`; source `quiz`, `pin`, `drop_in`, `promoted`, `discovered`), because affinities exist for many more nodes than the user's map (events propagate upward). The quiz niches join at onboarding. Angles are fixed when a node joins: next to the nearest ancestor on the map (±14° steps), otherwise in the middle of the widest free gap. Radius = 1 − affinity. Flags: `new` (joined in the last 7 days), `rising` / `fading` (±0.10 since last week's snapshot; `fading` also while the node is fading). Actions: pin (affinity = 1 − radius, optional angle; un-removes), unpin (learned value returns), drop-in (affinity raised to at least 0.35, 8 exposures), remove (the node and everything under it off the map, affinity 0; `allow_retest` re-opens it after 60 days), nudge ("Show more" / "Show less" = an event of weight 3 on the node itself), reset (back to the quiz priors; hidden sources and "already exists" marks stay). Affinities are only ever raised by adding evidence, never rewritten, because decay assumes alpha and beta only grow above the prior. Weekly job (`scripts/map_weekly.py`, after the tree build): move user state off merged niches (to the survivor) and retired ones (to the parent); narrowing as specified (the promoted child gets at least the parent's affinity × 0.9; the parent becomes `context` unless pinned); **discovery** (added): an off-map niche with nothing above it on the map, ≥ 5 positive events in 4 weeks and affinity ≥ 0.3 joins the map, so exploration that works becomes part of the map; fading as specified (≥ 10 negatives and no positives in 3 weeks → `fading`; 3 more weeks without positives → `off`; any positive brings it back); weekly snapshot. Internal change-log messages (never returned by the API: the map is silent): "Healthcare narrowed to Medical devices", "Farm equipment joined your map", "Healthcare fading after 12 skips", "… left your map", plus the user's own actions. `GET /map/node/{id}` returns path, placement and activity counts (saves, reads, skips), no prose about why. Profile sync came in Phase 5 and committed mode afterwards (6.7).

### 6.7 Modes
- **Exploring:** default. Feed composition as in section 9.
- **Committed:** the user picked a problem. The feed narrows to that problem's niche, its evidence, competitors, related paid demand and builders working nearby; everything else is capped at 10% *(tune)*.

**Committed mode as built (2026-10-11):** `POST /mode {mode: committed, problem_id}` (an active problem; `exploring` switches back; a problem that later disappears drops the user back to exploring). Candidates come first from the problem's own posts (its evidence, all of them: the one-post-per-problem collapse doesn't apply to it), its niche, posts naming its competitors (`tools_mentioned` matching the problem's `competitors`), and related paid demand: tenders and jobs in the problem's niche (the parent's only if it has none), closest to the problem's posts first, at most 50. Ranking treats the problem's niche as liked (affinity at least 0.9, `committed_affinity`) without touching the learned map. Composition caps everything outside the problem at 10% and paid demand at 25% (`committed_demand_max`: evidence comes first), drops the exploration and demand minimums, and ends the page ("caught up") rather than fill past the cap. The filter-bubble guard is off, as specified. "Builders working nearby" waits for the community phase. On the real corpus, a builder committed to "a flash glucose monitor's alarm failed to sound" (74 posts) got 12 evidence posts, 5 related tenders, 2 from the niche and 2 from elsewhere.

**As built (2026-10-11, Phase 3):** `signal_feed/feed/usermodel.py`. `POST /onboarding` takes the quiz (industries may name a branch, "Healthcare > Pharmacy"; `GET /onboarding/options` lists industries, branches and skills) and stores each choice as Beta(0.6 × 2, 0.4 × 2): a weak prior that a few events move; untouched nodes count as 0.1. Each stored event updates the model in `POST /events`: evidence = weight × 0.25 (a save adds 1) on the post's leaf niche, 40% to its parent and 15% to its grandparent, and on its skills; type and source preferences move by 0.02 and 0.01 per unit of weight (sources capped at ±0.5). "Not for me" reasons route as in 6.3 (*too crowded* is only counted: no competition data yet); "Hide source" hides the source and touches no niche. Evidence above the prior halves every 21 days, applied when the state is read. Calibration answers are a new event, `interested` (+2). Narrowing (6.5) and map output (6.6) came in Phase 4, committed mode (6.7) after Phase 7.

---

## 7. Stage E — Candidate generation

Per session, gather ~500 candidates *(tune)* from these sources, then de-duplicate:

| Source | Share target | How |
|---|---|---|
| Niche match | ~50% (was 45%) | nearest neighbours to the user's top niches (affinity-weighted centroids) |
| Skill match | ~8% (was 15%) | posts whose `skills_needed` match the user's top skills |
| Similar builders | ~10% | what builders with similar maps saved recently |
| Trending nearby | ~10% | rising problems in parent and sibling niches |
| Exploration | ~12% (was 10%) | niches outside the map, chosen by Thompson sampling |
| Pins and drop-ins | as needed | guaranteed candidates for pinned and dropped nodes |
| Followed builders | as needed | posts from builders the user follows (community phase) |
| Company-posted problems | later | when the company side launches |

Exclude: posts seen in the last 30 days, problems marked "already exists" or removed, hidden sources, posts with `spam_score` above threshold.

**As built (2026-10-11):** `signal_feed/feed/candidates.py`. Sources fill their share of 500 in order: pins and drop-ins (guaranteed), niche match (the user's top 8 niches by affinity, each a share proportional to it, posts nearest the niche centroid first), skill match (skills with affinity ≥ 0.3), similar builders (quiz Jaccard ≥ 0.3, their positive actions in 30 days; share decays from 20 to 100 events), trending nearby (problems with trend ≥ 0.6 in parent and sibling niches), exploration (Thompson sampling: one Beta draw per niche off the map, best draws explored; a chosen industry's own branches don't count as off the map), then a `fill` top-up so a thin map never yields a thin feed. Excluded as specified, plus pain problems below the user's "too small" minimum.

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

**As built (2026-10-11): v1 only** (`signal_feed/feed/ranker.py`). Features: niche affinity (best of leaf, parent × 0.9, grandparent × 0.8; 0 under a removed node), skill match, similarity to the last 20 liked and "not for me" posts (cosine 0.4–0.8 mapped to 0–1), type and source preference, problem rank, intensity, money, buyer, quality, freshness (halves every 72 h), similar builders' saves. Each action's probability is a linear function with hand-set coefficients in `ranking.v1`, clipped to [0, 1]. The problem feature is the post's problem score as a **percentile within its kind**: pain and demand scores come from different formulas, and mixing raw scores let tenders crowd the feed. Quality factor as specified. Surveys, the value model and v2 came in Phase 6 (section 10 "As built"); v3 is not built.

**Audit changes (2026-10-11):** (1) *Relevance gates value.* In the first v1, deep actions (save, "I'd build this", idea, outreach) added the post's problem rank, money and buyer likelihood regardless of fit, so a strong tender outside a user's interests scored like a relevant post (about 1.5) and "You're caught up" never came. Those terms now use relevance × value, relevance = the best of niche affinity, half the skill match, and similarity to liked posts. Over 3 simulation seeds (25 builders × 5 days): hidden interest of what's shown 0.398 → 0.413, saves per item 9.1% → 9.7%, "Not for me" per item 9.4% → 8.6%. (2) *Floor recalibrated* to 0.2 (was 0.5): on new users, off-map posts score about −0.06 and in-map posts 0.29 and up. (3) *Thompson prior* for post types 10 → 40: at 10 the draws moved scores by ±0.33, more than the in-map/off-map gap. (4) A session never opens empty: if nothing on the map is strong enough, its first page is "Explore more" items, flagged `explore`.


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
5. **Session end:** after the strong items run out (predicted score below a floor *(tune)*), show "You're caught up" with an optional "Explore more" that switches to exploration-heavy items. *(Built: floor 0.2; a session whose first page would be empty opens with "Explore more" items instead.)*
6. **No visible explanation** *(changed 2026-10-11, founder decision)*: the feed never tells the user why an item is there ("the magic of a good feed is that no one sees why it is good"). Each item still gets an internal reason ("Citation verification · you saved 4 similar posts", "Exploring: Insurance claims"), logged with the impression and in `score_breakdown.reason` for debugging and evaluation; the app must not display it.

**As built (2026-10-11):** `signal_feed/feed/compose.py`, one page of 20 at a time. Rules in order: best post per problem with "+N similar"; minimums for paid demand (15%) and exploration (10%) and freshness in the first 10 (40% under 48 h) are paced through the page (a slot is forced when the running count falls below share × position), maximums for change (20%) and exploration (15%) are hard; diversity (no repeat of problem or source next to each other, at most 2 of one post type in a row: `max_same_type_in_a_row`, relaxed from the spec's 1 on 2026-10-11; ≤ 3 of one leaf in any 10) wins over a quota for one slot; pins and drop-ins are spread evenly (up to 2 per page), and a drop-in spends one exposure per page it appears on; "You're caught up" when the best remaining score is below 0.5, and `GET /feed?explore=true` switches to mostly-exploration candidates with quotas off. Internal reasons (logged, never shown): "Pinned: …", "You added … · trying it for a few sessions", "… · needs AI agents, one of your skills" (or "a skill in posts you liked" when learned), "… · builders like you saved this", "Rising in …", "Exploring: … · new for you", "… · close to N posts you liked", "… · in your Core / in your interests", with "· paid demand" on tenders. A new user's first page is the calibration deck (6.2): 8 cards across the chosen niches, alternating demand and non-demand, plus 2 wildcards from other industries. The spec's "never the same post type twice in a row" made a page alternate when the strongest posts were mostly one type, so the second type (often tenders) filled about every other slot even for a user who rejected it; hence 2 in a row.

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

**As built (2026-10-11, Phase 6):** `signal_feed/learning/`.
- *Real time:* events update affinities in `POST /events` (Phase 3).
- *Hourly:* `scripts/problems.py rescore` refreshes every active problem's score (the trend moves with time) without re-clustering.
- *Nightly:* `scripts/feed_nightly.py`. Training data comes from what the feed already logs: each impression's ranker features (`score_breakdown.features`) and what the user then did with it. The **value model** is a logistic regression on "Was this useful?" answers (≥ 200 needed), stored as coefficients in the `models` table and used at once. It is blended into the score as the expected survey weight, `(1 − 0.2) × score + 0.2 × (3·p − 3·(1 − p))`. The **v2 models** are one gradient-boosted classifier per action (scikit-learn's `HistGradientBoostingClassifier`: no new dependency; ≥ 50k labelled impressions needed), saved under `data/models/`. A trained v2 is registered as a `candidate` and never used until it is activated after beating v1 offline (section 13). A held-out-users AUC per action is recorded with each model.
- *Surveys:* every 30th item a user sees carries `ask_useful` (never a calibration card); the app asks after the user reads it.
- *Thompson sampling over post types:* the ranker's `type_pref` feature is one draw per post type per page from Beta(5 + positive evidence, 5 + negative evidence), mapped to −1..1. Niches were already explored by Thompson sampling (Phase 3).
- *Filter-bubble guard:* as specified; when it fires, both the exploration share of candidates and the page's exploration quota go to 20% (committed mode doesn't exist yet, so no exception is needed).
- *Similar-builder neighbours* are computed per request from quiz overlap, so there is nothing to rebuild nightly.
- *Simulator* (`signal_feed/learning/sim.py`, demo `scripts/feed_simulate.py` on a throwaway database copy): builders with hidden tastes, who name only part of them in the quiz, use the real feed. First runs: the mean hidden interest of what's shown is 0.40–0.48 against 0.16–0.18 for a random feed, and the save rate rises over days (6.7% → 11.0% over 3 days with 10 builders). These are synthetic numbers that check the machinery, not evidence about real users.

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
| `GET /feed?session_id=&cursor=` | next feed items with explanations (built: also takes `user_id` until auth exists, and `explore=true` for "Explore more") |
| `POST /onboarding`, `GET /onboarding/options` | quiz answers and the choices to show (added in Phase 3) |
| `POST /events` | batch of user events |
| `GET /map` | current map: nodes, rings, positions, parents (built silent: no rising/fading/new statuses, no change messages) |
| `GET /map/history` | weekly snapshots for Replay (built without the change log: the map is silent) |
| `POST /map/pin` | pin a node at a position |
| `POST /map/drop-in` | add a node from the catalog |
| `POST /map/remove` | remove a node |
| `POST /map/nudge` | "Show more" / "Show less" on a node |
| `GET /map/node/{id}` | path, saves, reads, skips, and where it sits (no prose about why) |
| `POST /map/unpin`, `POST /map/reset` | added in Phase 4: undo a pin; reset the map to the quiz (section 14) |
| `GET /profile/niches/pending` and `POST /profile/niches/approve` | profile sync with user approval |
| `GET /profile/niches?audience=`, `POST /profile/niches/unpublish`, `POST /profile/auto-publish` | added in Phase 5: the published profile (public = broad areas), taking a niche off, auto-publish |
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

**As built (2026-10-11, Phase 7):** `signal_feed/evaluation/`.
- *Metrics* (`scripts/feed_metrics.py`, text or `--json`; the dashboard is this report): every number above, over a window (default 7 days), calibration cards excluded. Definitions, where the spec leaves room: save rate = saves per 100 items shown, plus the share of sessions with a save; idea conversion = ideas per save; outreach rate = outreach per idea; diversity = distinct leaf niches and distinct sources per block of 20 consecutive items in a session; exploration acceptance = saves per exploration item shown; coverage = active problems with a post shown to anyone in 30 days; retention d7 / d30 = came back in days 7–13 / 30–59 after their first item. Guardrails: dwell minutes per user, sessions per user per day, scroll depth (items per session).
- *Offline replay* (`scripts/feed_evaluate.py [--promote]`): held-out users (the same split v2 training uses), each session re-ranked from the features and quality factor logged at impression time; NDCG@20 on saves and on ideas for the logged order, v1, and the latest v2 candidate. A candidate is activated only with ≥ 30 held-out sessions containing a save, saves NDCG ≥ v1 + 0.01, and ideas NDCG not worse; the comparison is stored on the model.
- *A/B tests:* experiments live in `evaluation.experiments`. Each arm has a share, config overrides by dotted path (validated, so a typo fails), and optionally `ranker: v2` with a model id. Users are assigned by a stable hash on first sight and the arm is stored (sticky when shares are edited), and every impression records the user's arms. `scripts/feed_metrics.py --experiment NAME` reports per arm and applies the ship rule: north star significantly better (two-proportion z ≥ 1.96), "Not for me" rate not significantly higher, diversity not down more than 5%.
- *First end-to-end run* (simulated, 40 builders × 7 days on the real corpus): north star 33 of 40, 8.5 saves per 100 items, 10 niches and 8 sources per 20 items, coverage 56%. Offline, a v2 trained on that behaviour beat v1 on held-out builders (saves NDCG 0.356 vs 0.288, ideas 0.253 vs 0.221), and the logged order matched v1 (0.2878 vs 0.2877) as it should. Synthetic numbers: they check the machinery, not the product.

---

## 14. Safety and privacy

- Demote outrage, toxicity and engagement bait; never reward them.
- Anonymize authors in the app ("a practice manager"); link to originals.
- Honor deletions required by source platforms' terms.
- Niche data is personal: profile niches publish only with user approval; public profiles show broad areas, connections see exact niches; raw events never leave the backend.
- Users can see and reset their map at any time.

**Profile sync as built (2026-10-11, Phase 5):** `signal_feed/feed/profile.py`. A Core niche on the map is suggested for the profile once the user has ≥ 3 positive actions in it or pinned it there (quiz choices start in Core, so without that rule every new user would be asked about their own answers); a published niche that left the map, or was removed, is suggested for removal. Suggestions are neutral (`GET /profile/niches/pending`: niche, path, add or remove, nothing about why), decided with `POST /profile/niches/approve` (approve or decline per update); a declined niche isn't offered again for 90 days; a suggestion that stops holding (the niche left Core) is withdrawn. Auto-publish (`POST /profile/auto-publish`) applies updates at once and records them as approved. `GET /profile/niches?audience=public` returns only broad areas (the industries); `connection` and `self` return the exact niches; `POST /profile/niches/unpublish` takes one off directly. Sync runs in the weekly job and whenever pending updates are read. Until app auth exists, the audience is a query parameter: the app must pass `public` for anyone but the user and their connections.

---

## 15. Build order

Finish each phase with tests and a short demo script before starting the next.

1. **Phase 0 — foundations:** tables, `config/feed.yaml`, event logging, `POST /events`. — done (2026-10-09): package `signal_feed/`, tests in `tests/feed/`, demo `scripts/feed_demo.py`. Feed tables live in a separate database for now (`FEED_DATABASE_URL`, default `data/feed.db`); raw posts are read read-only from the connectors' `DATABASE_URL`. `POST /events` takes `user_id` in the body until app auth exists.
2. **Phase 1 — enrichment:** Stage A with schema validation, cost guard and a labeled sample of 200 posts to check tagging quality. — built (2026-10-09); run live (2026-10-10), prompt v5, post_type accuracy 86.9% over 15 types; backfill (2026-10-10): 7,068 posts, 3,161 `ok`, 2,249 `dropped`, 1,658 `skipped`; $0.54 through the Batch API, 90 escalations (1.8%). Embeddings paced to Voyage's no-card limits (3 requests, 10k tokens per minute) in `config/feed.yaml`. The 200 reference labels were written by Claude; 34 flagged `unsure` await human review.
3. **Phase 2 — niche tree and problems:** seed tree, problem clustering and scoring; a script that prints the top problems per niche. — built (2026-10-11): `scripts/problems.py build | tree | top`; first run without LLM naming (Anthropic credit ran out), see 4 and 5 "As built". Seeded industry branches added (prompt v8 lists them; re-tag pending Anthropic credit, ~$2–3 because the prompt grew to ~5k tokens). Tagger moved to effort medium (prompt v7: industry 82.3%, kept-in-feed P 91.2% / R 78.5% on the eval set; ~$1.30 per full backfill).
4. **Phase 3 — MVP feed:** user model with quiz priors and calibration, candidate generation, v1 linear ranker, feed composition with quotas, diversity and explanations; `GET /feed`. — built (2026-10-11): `signal_feed/feed/`, `POST /onboarding`, `GET /onboarding/options`, `GET /feed`; events update the user model; demo `scripts/feed_session.py` (runs on a throwaway copy of the feed database).
5. **Phase 4 — map:** affinity-to-map conversion, pins, drop-ins, remove, nudge, weekly snapshots, change log, narrowing checks; all `/map` endpoints. — built (2026-10-11): `/map`, `/map/history`, `/map/node/{id}`, `/map/pin`, `/map/unpin`, `/map/drop-in`, `/map/remove`, `/map/nudge`, `/map/reset`; `scripts/map_weekly.py`; demo `scripts/feed_session.py --map`.
6. **Phase 5 — profile sync:** pending updates and approval endpoints. — built (2026-10-11): `GET /profile/niches/pending`, `POST /profile/niches/approve`, `GET /profile/niches`, `POST /profile/niches/unpublish`, `POST /profile/auto-publish`; synced by the weekly job.
7. **Phase 6 — learning:** "Was this useful?" surveys, Thompson-sampling exploration, filter-bubble guard, nightly v2 models once there is enough data. — built (2026-10-11): see section 10 "As built"; `scripts/feed_nightly.py`, `scripts/problems.py rescore`, `scripts/feed_simulate.py`. No real usage data yet: value and v2 models train only on simulated builders so far.
8. **Phase 7 — evaluation:** offline replay harness, metrics dashboard, A/B assignment. — built (2026-10-11): see section 13 "As built"; `scripts/feed_metrics.py`, `scripts/feed_evaluate.py`, experiments in `config/feed.yaml`.

---

## 17. Inferred problems

Specified and tracked in `INFERRED_PROBLEMS.md` (kept as a separate file, as it allows), including what has been built.

---

## 16. Working conventions for Claude Code

- Keep every weight, threshold and quota in `config/feed.yaml`.
- Every ranking change ships with an offline comparison against the current version.
- Log a `score_breakdown` for every impression so any feed item can be explained.
- When behavior depends on real usage data that doesn't exist yet, build it behind a flag and use the v1 rules.
- Keep this file current as the source of truth for how the feed works.
