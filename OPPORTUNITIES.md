# Signal: opportunities, the feed's new unit (addendum to FEED_ALGORITHM.md)

Add this to `FEED_ALGORITHM.md` as **section 18**, or keep it as a separate file next to it. It changes what the feed shows, so read it before continuing work on sections 5, 8 and 9. Every threshold marked *(tune)* lives in `config/feed.yaml`.

**Summary of the change:** the feed no longer shows raw posts. It shows **opportunities**: problems that many people share, that cost them money or time today, and that one or two builders could ship a solution for in weeks. Raw posts become the evidence behind each opportunity.

---

## 18. Opportunities

### 18.1 Why

Reading the current feed, a builder cannot tell what most items mean or what they could build. Two real examples:

- **"Developers can't remove a deleted Git contributor from a GitHub repo homepage after a history rewrite."** Only GitHub can fix this. No outside builder can ship a solution, so it should never reach the feed.
- **"A UK cooking sauce founder needs a print-ready label and jar mockup."** This is one person's design job, not a product. But many small food brands paying for labels every month *is* a pattern someone could build a product for.

One post is almost never buildable. The value is in the pattern across many posts, written so a builder understands **who** has the problem, **what** they do today, and **what they could ship**.

### 18.2 Three layers

```
Signals       raw posts, gigs, job ads, rules       (RawPost + EnrichedPost)    → evidence only
   │
Problems      clusters of the same pain              (section 5, unchanged)
Job clusters  clusters of the same paid task         (new, 18.4)
Inferred      problems from linked evidence          (section 17)
   │
Opportunities builder-facing, gated, scored          (new)                       → the feed
```

Only opportunities appear in the feed. Posts appear inside an opportunity's page as linked evidence.

### 18.3 Enrichment additions (Stage A)

Add these fields to `EnrichedPost` (section 3.1):

| Field | Type | Notes |
|---|---|---|
| `job_statement` | str | normalized "[buyer type] needs [task or deliverable]": "small food brand needs print-ready product label" |
| `deliverable` | str \| null | for paid demand: the thing being bought ("print-ready label", "monthly bookkeeping") |
| `buyer_type` | enum | `consumer`, `solo`, `small_business`, `mid_market`, `enterprise`, `public_sector`, `developer` |
| `buyer_segment` | str | plain words: "small food and drink brands", "solo immigration lawyers" |
| `recurrence` | enum | `once`, `occasional`, `recurring` (does this buyer need it again?) |
| `one_off_request` | bool | true when the post is a single deliverable request (most gigs) |
| `fixable_by_third_party` | enum | `yes`, `platform_owner_only`, `unclear` |
| `platform_owner` | str \| null | when `platform_owner_only`: "GitHub", "Shopify" |
| `time_cost` | object \| null | stated time spent: `{hours, per}` ("4 hours a week") |
| `plain_summary` | str | one sentence a student with no industry experience understands |

`fixable_by_third_party` is the key new judgement. A bug or missing setting inside one vendor's product is `platform_owner_only`, unless the post or its replies show an outside route (an API, an extension, an integration, a migration tool).

### 18.4 Job clusters (new)

Paid demand clusters badly on `pain_statement`, because gigs describe deliverables, not pain. So:

- Within each `buyer_type` + industry niche, cluster paid-demand and job-ad posts by `job_statement` embedding (cosine ≥ 0.80 *(tune)*), and confirm borderline merges with the LLM.
- A job cluster stores: `job_statement`, `buyer_segment`, posts, distinct buyers, sources, price stats (median, p25, p75 and count, computed in code), recurrence share, first/last seen.
- Reported pain posts about the same task (for example "redoing labels for the printer took a week") attach to the job cluster too.

### 18.5 Shippability gates

A candidate (problem, job cluster or inferred problem) becomes an opportunity only if it passes **all** of these. Each failure is stored with its reason in `opportunity_gates`, for audits and tuning.

| Gate | Passes when *(tune)* | Checked by |
|---|---|---|
| **G1 Repeated** | ≥ 8 distinct authors or buyers and ≥ 2 source types in the last 90 days. A single post or gig never passes | code |
| **G2 Fixable from outside** | < 30% of evidence is `platform_owner_only`; if the problem lives inside one platform, a named outside route exists (API, extension, integration) | code + critic |
| **G3 Buildable** | a concrete MVP that 1–2 people could ship in ≤ 8 weeks with common tools; reject anything needing hardware manufacturing, a licence (bank, medical device, law firm) or proprietary data the builder cannot get | synthesis + critic |
| **G4 Already costing them** | money signals (gigs, jobs, budgets, paid tools) or stated time costs in ≥ 3 evidence items | code |
| **G5 Reachable** | at least 2 named public places where these buyers can be found and contacted (directories, job boards, marketplaces, communities) | synthesis + critic |
| **G6 Not already solved** | no cheap, widely used tool that already fully covers this buyer segment; a gap must be stated (price, segment, missing feature) | critic |

**G7 Fits you** is applied per user at ranking time, not at publishing (18.8).

### 18.6 Opportunity schema

| Field | Notes |
|---|---|
| `id`, `status` | `candidate`, `published`, `rejected`, `expired` |
| `title` | ≤ 14 words, buyer as subject, plain words: "Small food brands pay designers for every print-ready label" |
| `plain_words` | 2–3 sentences anyone understands: who, what goes wrong, why it matters |
| `who` | buyer segment and the role that feels the pain |
| `today` | what they do now (freelancers, spreadsheets, manual checks) |
| `cost_today` | computed stats: number of paid gigs or jobs, median price, stated hours; never invented by the LLM |
| `angles` | 1–3 things to ship, each: `name`, `one_line`, `mvp_scope` (3–5 bullets), `skills_needed`, `weeks_to_mvp` |
| `why_now` | optional trigger: a new rule, a platform change, a price jump |
| `reach` | named channels where buyers can be found |
| `competition` | existing tools: name, price, who it serves, the gap |
| `evidence` | links to problems, job clusters, inferred problems and posts, with counts by source type and a 12-week trend |
| `scores` | 18.7 |
| `confidence` | `high`, `medium`, `low` (as in 17.6) |
| `niche_id`, `skills` | for matching |
| `glossary` | short definitions for any unavoidable term ("dieline: the cut outline printers need") |
| `created_at`, `refreshed_at`, `prompt_version` | |

### 18.7 Opportunity score (replaces 5.2 for ranking)

Each component is normalized 0–1 and the components are combined as a weighted sum *(tune)*:

| Component | Weight | Meaning |
|---|---|---|
| People affected | 0.20 | distinct buyers, log-scaled |
| Money already spent | 0.25 | gig and job volume × median price, plus paid-tool evidence |
| Build effort | 0.15 | inverse of the best angle's `weeks_to_mvp` |
| Reachability | 0.10 | number and quality of channels |
| Competition gap | 0.15 | from the critic's competition review |
| Trend | 0.10 | last 4 weeks vs previous 4 |
| Evidence strength | 0.05 | source diversity and confidence |

**Shown to users** as five plain meters, replacing Pain / Paid / Tool gap / Change / Size: **People · Money spent · Effort · Reach · Competition**. Each meter opens the evidence behind it.

### 18.8 Feed changes (sections 7–9)

- **Items:** opportunities only (plus builder posts once the community exists). Raw posts never appear as standalone items, so the "+N similar posts" collapse is removed.
- **Candidates:** match on opportunity niche, skills and buyer segment instead of post embeddings.
- **G7 Fits you:** boost when `skills_needed` overlaps the user's skills; demote when `weeks_to_mvp` exceeds what the user's quiz time commitment allows (a few hours a week → prefer ≤ 4 weeks *(tune)*).
- **Volume:** 5–10 opportunities per user per day *(tune)*, then "You're caught up".
- **Tabs:** For you · Most paid (money spent high) · Quick to build (effort low) · New rules (why_now from rule changes).
- **Explanation line:** "Matches your design + Python · about 3 weeks to a first version".
- **"Not for me" reasons:** Not my skills · Industry doesn't interest me · Too big to build · Too small · Already solved · **I don't understand it**. The last one sends the opportunity to the review queue (18.11) and lowers it for everyone until reviewed.
- **Events:** weights unchanged (6.3). Opening an evidence post counts as Expanded (+0.5) on the opportunity.

### 18.9 Opportunity page (replaces the post detail page)

In order:
1. Title and plain words
2. Who has it, and what they do today, with its cost
3. What you could ship: each angle with MVP scope, skills and weeks
4. Evidence: linked posts grouped by source type, with counts and a 12-week trend
5. Competition and the gap
6. Where to find buyers, leading into First customers
7. Confidence, and insider votes for inferred items
8. Builders exploring it
9. Actions: Save · Turn into idea · Find first customers · Not for me · Share

### 18.10 Synthesis pipeline

Runs daily for new candidates, and weekly to refresh published opportunities.

1. **Candidates:** problems (5.1), job clusters (18.4) and inferred problems (17).
2. **Pre-gates in code:** G1 and G4 counts, and the G2 share. This is cheap, and it drops most candidates before any LLM call.
3. **Synthesis (larger model, one call per candidate):** input is the cluster's computed stats plus up to 15 representative evidence items; output is the `Opportunity` JSON, validated with pydantic. Rules:
   - Every claim must point to an evidence item.
   - Every number comes from the computed stats; the model writes no new numbers.
   - Plain-language rules (18.12).
   - 1–3 concrete angles with MVP scope.
4. **Critic (separate call, sees only the opportunity and its evidence):**
   - Checks each claim against its evidence.
   - Runs G2, G3, G5 and G6.
   - Rewrites the title if a student would not understand it.
   - Returns `keep`, `revise` or `reject`, with reasons.
5. **Score, store, publish.** Expire when there is no new evidence for 60 days *(tune)*.

**Cost:** synthesis runs only on candidates that pass the pre-gates. There is a monthly cap in config, using batch processing where possible.

### 18.11 Human review loop

- **Weekly**, the founder rates 20 random published opportunities and 10 rejected ones: "Would a builder actually ship this?" Answers are yes, no or unclear, plus a one-line reason. Opportunities flagged "I don't understand it" are added to the queue.
- **Stored in** `opportunity_reviews`. Used to:
  - tune gate thresholds
  - pick few-shot examples for the synthesis and critic prompts
  - later, train a classifier for G3 and G6
- **Target:** ≥ 70% "yes" on published items, and < 5% "I don't understand it" from users *(tune)*.
- **Tool:** a simple admin page or CLI is enough.

### 18.12 Plain-language rules for all generated text

- **Titles:**
  - ≤ 14 words
  - the buyer as subject, a concrete verb (pay, redo, wait, lose)
- **Words to avoid:**
  - no internal labels such as "tool gap", "change signal" or "paid demand"
  - no acronyms or trade terms unless the glossary defines them
- **Reading level:** around grade 10.
- **The test:** a college student with no industry experience can say who has the problem and what they could build after reading only the title and the plain words.

### 18.13 Data model additions

- New tables:
  - `job_clusters`, `job_cluster_posts`
  - `opportunities`, `opportunity_angles`
  - `opportunity_evidence` (opportunity_id, post_id | problem_id | job_cluster_id | inferred_id, role)
  - `opportunity_gates` (gate, passed, reason, checked_by)
  - `opportunity_reviews`
- New fields on `enriched_posts`: all fields from 18.3.
- `impressions` and `events` now reference `opportunity_id`.

### 18.14 API changes

| Endpoint | Change |
|---|---|
| `GET /feed` | returns opportunities with scores, meters and explanation |
| `GET /opportunities/{id}` | the full opportunity page |
| `GET /opportunities/{id}/evidence?type=&cursor=` | linked posts |
| `POST /admin/reviews` | weekly review answers |

### 18.15 Measuring it

- Review "would ship" rate on published opportunities (target ≥ 70%)
- "I don't understand it" rate (target < 5%)
- Save → idea → first-customer conversion, compared with the old post feed
- Gate rejection reasons over time; a gate rejecting almost everything is miscalibrated

### 18.16 Worked examples

**GitHub contributor list**
- 2 authors and 1 source type, so it fails G1.
- The evidence is `platform_owner_only` (GitHub) with no outside route, so it fails G2.
- Rejected, with both reasons logged.

**UK sauce label gig**
- Alone, it fails G1. It joins the job cluster "small food and drink brand needs print-ready product label".
- If that cluster passes the gates, the opportunity could read (illustrative; numbers come from the computed stats):
  - **Title:** Small food brands pay designers for every print-ready label
  - **Plain words:** Small sauce, snack and drink makers hire freelancers each time they need a label that a printer will accept. It takes days and often needs redoing to meet printing and food-labelling rules.
  - **Cost today:** [N] paid gigs in 90 days, median $[X], across [K] sites
  - **Angle:** a tool that turns a brand's artwork into print-ready label files and checks the required food-label information. MVP in about [W] weeks with design and Python skills.
  - **Reach:** freelance marketplaces, small-food-brand communities, local food incubators

### 18.17 Build order

1. Enrichment fields (18.3); backfill the existing corpus in batch.
2. Job clusters (18.4).
3. Pre-gates and the rejection log (18.5, code gates).
4. Synthesis, critic and scoring (18.6, 18.7, 18.10).
5. `GET /feed` and `GET /opportunities/{id}` serving opportunities behind a feature flag.
6. Admin review tool and the first weekly review (18.11).
7. Switch the feed to opportunities by default and remove standalone post items.

---

**What changes elsewhere:**
- Section 5.2's problem score still ranks problems internally, but the feed ranks by 18.7.
- Section 8.3's post features become opportunity features.
- Section 9's mix quotas apply to opportunities, with "Paid demand ≥ 15%" replaced by the Most paid tab and money-spent weighting.
- Section 17's inferred problems feed into opportunities and must pass the same gates.
